"""Source adapters.

Each adapter reads one public source and yields `Evidence`. An adapter is the
only place that knows about a source's wire format, so adding a network means
writing one class and registering it -- the classifier, store and Slack renderer
never change.

Nothing here logs in, presents a session cookie, bypasses an anti-bot check, or
touches a page that requires an account.
"""

from __future__ import annotations

import contextlib
import hashlib
import html
import json
import logging
import os
import re
import urllib.parse
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable, Iterator, Mapping
from typing import Protocol, runtime_checkable

from .http import NotModified, SourceError, get_json, get_text
from .models import PROGRAMME_SPEEDRUN, PROGRAMME_YC, Evidence, Source

LOGGER = logging.getLogger("launchsignal.sources")

SITEMAP_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}


@runtime_checkable
class SourceAdapter(Protocol):
    """The whole contract a new source has to satisfy."""

    name: Source

    def scan(self, store=None) -> Iterable[Evidence]: ...


# --------------------------------------------------------------- YC directory


class YcSitemapSource:
    """YC's public company sitemap, plus a profile fetch for genuinely new slugs.

    The sitemap alone carries no batch and no description, and the task's own
    reference alert card shows a Batch field. Those live on the company profile
    page, so each *new* slug gets exactly one extra request. Because the scan is
    incremental this costs one fetch per newly listed company, not 6,000.
    """

    name = Source.YC_DIRECTORY
    sitemap_url = "https://www.ycombinator.com/companies/sitemap"
    profile_template = "https://www.ycombinator.com/companies/{slug}"
    #: Only /companies/<slug>; the sitemap also lists /companies/industry/<x>.
    slug_pattern = re.compile(r"^https://www\.ycombinator\.com/companies/([\w-]+)$")
    #: The directory lists YC's own entry alongside its portfolio. Alerting
    #: "NEW YC COMPANY: Y Combinator" is not a useful signal.
    excluded_slugs = frozenset({"y-combinator", "ycombinator"})

    def __init__(self, *, enrich: bool = True, max_enrich: int = 250) -> None:
        self.enrich = enrich
        #: Safety valve on profile fetches per cycle. It is set well above a
        #: realistic batch drop because an observation is recorded whether or
        #: not it was enriched: a company skipped by the cap would never be
        #: enriched again, and would alert with no batch and no description.
        self.max_enrich = max_enrich

    def scan(self, store=None) -> Iterator[Evidence]:
        # During the silent baseline every listing is recorded and nothing is
        # alerted, so per-company profile fetches would be ~6,000 requests spent
        # on detail no human will ever see. Enrich from the first real scan on.
        baseline = store is not None and not store.baseline_complete(self.name.value)
        enrich = self.enrich and not baseline
        etag = store.get_etag(self.name.value) if store else None
        try:
            document, response = get_text(
                self.sitemap_url, source=self.name.value, etag=etag
            )
        except NotModified:
            LOGGER.info("yc sitemap unchanged (ETag match); nothing to scan")
            return
        entries = list(_parse_sitemap(document, self.slug_pattern, self.name.value))
        if not entries:
            # A well-formed HTML error page parses as valid XML and yields zero
            # entries. Reporting success there would make the monitor silently
            # blind, so an empty sitemap is treated as a source failure.
            raise SourceError(
                self.name.value,
                "sitemap parsed but contained no company URLs -- "
                "the endpoint may be serving an error or consent page",
            )
        LOGGER.info("yc sitemap: %d company URLs", len(entries))

        enriched = 0
        deferred = 0
        for slug, url, lastmod in entries:
            if slug in self.excluded_slugs:
                continue
            is_new = store is None or not _observed(store, self.name.value, slug)
            detail: dict[str, object] = {}
            if enrich and is_new:
                if enriched >= self.max_enrich:
                    # Over the cap: skip this listing entirely rather than
                    # recording it without detail. It stays unobserved, so the
                    # next cycle still treats it as new and it alerts with a
                    # batch and a description instead of being stranded
                    # permanently as an enriched-never record.
                    deferred += 1
                    continue
                detail = self._fetch_profile(slug)
                enriched += 1
                if detail is None:
                    # Do not record a new company with the fallback name after
                    # a temporary profile outage. That would make the item no
                    # longer new and strand its batch/description forever.
                    # Leaving it unobserved gives it one bounded retry next
                    # cycle, never a new baseline of every YC profile.
                    deferred += 1
                    continue
            yield Evidence(
                source=self.name,
                external_id=slug,
                url=url,
                title=str(detail.get("name") or _titleise(slug)),
                excerpt=str(detail.get("description") or ""),
                company_name=str(detail.get("name") or _titleise(slug)),
                programme="YC",
                batch=_yc_batch_label(detail),
                profile_url=url,
                metadata={
                    "slug": slug,
                    "lastmod": lastmod,
                    "website": detail.get("website"),
                    "enriched": bool(detail),
                },
            )
            if store is not None and detail:
                store.mark_enriched(self.name.value, slug)
        if deferred:
            # Reported, not silently applied: a reader of the logs can see that
            # coverage this cycle was bounded and by how much.
            LOGGER.warning(
                "yc: %d new listing(s) deferred past this cycle's enrichment cap "
                "of %d; they remain unobserved and will be picked up next scan",
                deferred,
                self.max_enrich,
            )
        if store is not None:
            store.set_etag(self.name.value, response.etag)

    def _fetch_profile(self, slug: str) -> dict[str, object] | None:
        """Pull batch, description and canonical name off a company profile.

        The page embeds an HTML-escaped JSON blob; parsing that is far more
        stable than scraping the rendered markup.
        """
        url = self.profile_template.format(slug=slug)
        try:
            body, _ = get_text(url, source=f"{self.name.value}:profile", attempts=2)
        except SourceError as error:
            LOGGER.warning("yc profile %s unavailable: %s", slug, error.message)
            return None
        return _extract_yc_profile(body)


def _extract_yc_profile(body: str) -> dict[str, object]:
    """Pull the company fields out of the profile page's embedded JSON.

    Each field is decoded with json.loads on the quoted fragment. Running
    .encode().decode("unicode_escape") over the text instead corrupts any
    already-decoded UTF-8, turning "Cafe\u0301"-style names and emoji into
    mojibake.
    """
    unescaped = html.unescape(body)
    out: dict[str, object] = {}
    for key, target in (
        ("batch", "batch"),
        ("batch_name", "batch_name"),
        ("name", "name"),
        ("description", "description"),
        ("one_liner", "description"),
    ):
        if target in out:
            continue
        match = re.search(rf'"{key}"\s*:\s*("(?:[^"\\]|\\.){{0,400}}")', unescaped)
        if not match:
            continue
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(value, str) and value.strip():
            out[target] = value.strip()
    website = re.search(r'"url"\s*:\s*("https?://(?:[^"\\]|\\.){0,200}")', unescaped)
    if website:
        with contextlib.suppress(json.JSONDecodeError):
            out["website"] = json.loads(website.group(1))
    return out


def _yc_batch_label(detail: dict[str, object]) -> str | None:
    """Prefer the short code ("S26"), fall back to the long name ("Summer 2026")."""
    code = detail.get("batch")
    if isinstance(code, str) and re.fullmatch(r"(?i)[swfx]\d{2}", code.strip()):
        return f"YC {code.strip().upper()}"
    name = detail.get("batch_name")
    if isinstance(name, str) and name.strip():
        return f"YC {name.strip()}"
    if isinstance(code, str) and code.strip():
        return f"YC {code.strip().upper()}"
    return None


#: A sitemap this large is not a sitemap. Guards against a decompression or
#: entity-expansion bomb being handed to the parser.
MAX_SITEMAP_BYTES = 32 * 1024 * 1024


def _parse_sitemap(
    document: str, pattern: re.Pattern[str], source: str
) -> Iterator[tuple[str, str, str | None]]:
    if len(document) > MAX_SITEMAP_BYTES:
        raise SourceError(
            source, f"sitemap exceeds {MAX_SITEMAP_BYTES} bytes; refusing to parse"
        )
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", document[:4096], re.IGNORECASE):
        # A sitemap has no legitimate reason to declare a doctype or entities,
        # and both are the vehicle for entity-expansion and external-entity
        # attacks against the stdlib parser.
        raise SourceError(source, "sitemap declares a DOCTYPE or ENTITY; refusing to parse")
    try:
        root = ElementTree.fromstring(document)  # noqa: S314 - guarded above
    except ElementTree.ParseError as error:
        raise SourceError(source, f"sitemap is not valid XML: {error}") from None
    for node in root.findall(".//s:url", SITEMAP_NS):
        loc = node.find("s:loc", SITEMAP_NS)
        url = (loc.text or "").strip() if loc is not None else ""
        match = pattern.match(url)
        if not match:
            continue
        lastmod_node = node.find("s:lastmod", SITEMAP_NS)
        lastmod = (lastmod_node.text or "").strip() if lastmod_node is not None else None
        yield match.group(1), url, lastmod


# ------------------------------------------------------------ a16z Speedrun


class A16zSpeedrunSource:
    """The public a16z Speedrun company API.

    Labelled `a16z Speedrun` everywhere, never as a YC programme. a16z runs
    Speedrun; YC does not. The payload already contains cohort, description,
    founders and social URLs, so no scraping is required.
    """

    name = Source.A16Z_SPEEDRUN
    page_size = 100
    endpoint = (
        "https://speedrun-api.a16z.com/api/companies/companies/"
        "?limit={limit}&offset={offset}&ordering=name"
    )
    profile_template = "https://speedrun.a16z.com/companies/{slug}"

    def scan(self, store=None) -> Iterator[Evidence]:
        offset = 0
        seen = 0
        guard = 0
        while True:
            guard += 1
            if guard > 200:
                raise SourceError(self.name.value, "pagination exceeded 200 pages")
            url = self.endpoint.format(limit=self.page_size, offset=offset)
            payload, _ = get_json(url, source=self.name.value)
            if not isinstance(payload, dict):
                raise SourceError(self.name.value, "expected a JSON object")
            records = payload.get("results")
            if not isinstance(records, list):
                raise SourceError(self.name.value, "'results' was not a list")
            for record in records:
                if isinstance(record, dict):
                    evidence = self._to_evidence(record)
                    if evidence is not None:
                        seen += 1
                        yield evidence
            # Advance by the requested page size, not by len(records). Using the
            # record count means an empty page with a non-null "next" leaves the
            # offset unchanged and the loop requests the same URL forever.
            offset += self.page_size
            if not payload.get("next"):
                LOGGER.info("a16z speedrun: %d companies", seen)
                return
            if not records:
                raise SourceError(
                    self.name.value,
                    f"empty page at offset {offset - self.page_size} but 'next' was set",
                )

    def _to_evidence(self, record: dict) -> Evidence | None:
        identifier = str(record.get("id") or "").strip()
        name = str(record.get("name") or "").strip()
        if not identifier or not name:
            return None
        slug = str(record.get("slug") or "").strip()
        profile = self.profile_template.format(slug=slug) if slug else ""
        founders = record.get("founder_set")
        founder_name = None
        if isinstance(founders, list) and founders:
            first = founders[0]
            if isinstance(first, dict):
                parts = [first.get("first_name"), first.get("last_name")]
                founder_name = " ".join(str(p) for p in parts if p).strip() or None
        cohort = str(record.get("cohort") or "").strip() or None
        return Evidence(
            source=self.name,
            external_id=identifier,
            # Link to the Speedrun profile, not the company's marketing site:
            # the alert's Source field should point at the programme record.
            url=profile or str(record.get("website_url") or ""),
            title=name,
            excerpt=str(record.get("description") or record.get("preamble") or ""),
            company_name=name,
            programme="a16z Speedrun",
            batch=cohort,
            founder=founder_name,
            founder_handle=_handle_from_url(record.get("x_url")),
            profile_url=profile or None,
            metadata={
                "slug": slug,
                "cohort": cohort,
                "website": record.get("website_url"),
                "x_url": record.get("x_url"),
                "linkedin_url": record.get("linkedin_url"),
                "industries": record.get("industries"),
                "team_size": record.get("team_size"),
            },
        )


def _handle_from_url(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = urllib.parse.urlsplit(value).path.strip("/")
    return f"@{path}" if path and "/" not in path else None


class ConfigurableDirectorySource:
    """A directory adapter pointed at a URL supplied by configuration.

    The brief asks for a "YC Speedrun page" and describes it as YC's own
    sub-programme, but gives no URL. No such YC directory is public: YC's
    /speedrun path returns 404 and its company directory contains no reference
    to Speedrun. Speedrun is a16z's accelerator, which `A16zSpeedrunSource`
    reads and labels accordingly.

    This adapter closes the gap without guessing. Point
    LAUNCHSIGNAL_DIRECTORY_URL at any sitemap, JSON list, or JSON API and it is
    monitored and tagged under LAUNCHSIGNAL_DIRECTORY_PROGRAMME, with no code
    change. If the intended page turns out to be somewhere else, it is one
    environment variable away.
    """

    name = Source.CUSTOM_DIRECTORY

    def __init__(self, url: str | None = None, programme: str | None = None) -> None:
        self.url = url or os.environ.get("LAUNCHSIGNAL_DIRECTORY_URL", "").strip()
        self.programme = (
            programme
            or os.environ.get("LAUNCHSIGNAL_DIRECTORY_PROGRAMME", "").strip()
            or "Speedrun"
        )

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def scan(self, store=None) -> Iterator[Evidence]:
        if not self.url:
            return
        body, response = get_text(self.url, source=self.name.value)
        stripped = body.lstrip()
        if stripped.startswith("<"):
            yield from self._from_sitemap(body)
        else:
            yield from self._from_json(body)
        if store is not None:
            store.set_etag(self.name.value, response.etag)

    def _from_sitemap(self, body: str) -> Iterator[Evidence]:
        host = urllib.parse.urlsplit(self.url).netloc
        pattern = re.compile(rf"^https?://{re.escape(host)}/.+/([\w-]+)/?$")
        for slug, url, lastmod in _parse_sitemap(body, pattern, self.name.value):
            yield Evidence(
                source=self.name,
                external_id=slug,
                url=url,
                title=_titleise(slug),
                excerpt="",
                company_name=_titleise(slug),
                programme=self.programme,
                profile_url=url,
                metadata={"slug": slug, "lastmod": lastmod, "directory": self.url},
            )

    def _from_json(self, body: str) -> Iterator[Evidence]:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise SourceError(
                self.name.value,
                f"{_safe_label(self.url)} is neither XML nor JSON: {error}",
            ) from None
        records = payload
        if isinstance(payload, dict):
            for key in ("results", "companies", "data", "items"):
                if isinstance(payload.get(key), list):
                    records = payload[key]
                    break
        if not isinstance(records, list):
            raise SourceError(self.name.value, "no list of records found in the response")
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            name = str(
                record.get("name") or record.get("company") or record.get("title") or ""
            ).strip()
            if not name:
                continue
            identifier = str(
                record.get("id") or record.get("slug") or record.get("uuid") or f"{index}:{name}"
            )
            yield Evidence(
                source=self.name,
                external_id=identifier,
                url=str(record.get("url") or record.get("website_url") or self.url),
                title=name,
                excerpt=str(record.get("description") or record.get("one_liner") or ""),
                company_name=name,
                programme=self.programme,
                batch=str(record.get("batch") or record.get("cohort") or "") or None,
                metadata={"directory": self.url},
            )


def _safe_label(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return f"{parsed.netloc}{parsed.path}"


# ----------------------------------------------------- public-index discovery


class TinyfishSearchSource:
    """Public-web search discovery for X and LinkedIn.

    This reads a public search index. It never logs into a social network, never
    presents a cookie, and never requests a page that requires an account. Every
    result is filtered down to canonical public post/company URLs before it is
    allowed to become evidence.
    """

    endpoint = "https://api.search.tinyfish.ai"

    def __init__(
        self,
        source: Source,
        queries: list[str],
        *,
        recency_minutes: int | None = None,
        allowed_authors: tuple[str, ...] = (),
        programme: str | None = None,
        max_pages: int = 3,
    ) -> None:
        if not 1 <= max_pages <= 11:
            raise ValueError("max_pages must be between 1 and Tinyfish's 11-page limit")
        self.name = source
        self.queries = queries
        #: For an official source, the programme whose announcements it carries.
        #: Comparing a YC claim against a16z's account (or the reverse) lets one
        #: programme's post suppress the other programme's early signal.
        self.programme = programme
        #: The freshness window. This is the actual early-detection lever: a
        #: founder post minutes old is what the task is asking for, so the
        #: founder lane asks the index for recent results explicitly.
        self.recency_minutes = recency_minutes
        #: For official-account snapshots, the account path that a result MUST
        #: be under. Without this any stranger's post counts as an official
        #: programme announcement and suppresses genuine early alerts.
        self.allowed_authors = tuple(a.strip("/").lower() for a in allowed_authors)
        #: Tinyfish pages are zero-based and currently stop at page 10. Keep a
        #: smaller per-query budget by default, even when the response says
        #: more results exist, so one broad query cannot consume the scan.
        self.max_pages = max_pages

    @property
    def configured(self) -> bool:
        return bool(os.environ.get("TINYFISH_API_KEY"))

    @property
    def account_labels(self) -> tuple[str, ...]:
        """The actual accounts this source reads, for the alert's audit trail."""
        return tuple(f"@{author}" for author in self.allowed_authors) or (self.name.value,)

    def scan(self, store=None) -> Iterator[Evidence]:
        api_key = os.environ.get("TINYFISH_API_KEY")
        if not api_key:
            # Surfaced by the caller as a skipped source, never as a clean pass.
            raise SourceError(
                self.name.value,
                "TINYFISH_API_KEY is not set, so this source cannot be read",
            )
        seen: set[str] = set()
        for query in self.queries:
            for page in range(self.max_pages):
                params = {
                    "query": query,
                    "location": "US",
                    "language": "en",
                    "page": str(page),
                }
                if self.recency_minutes:
                    params["recency_minutes"] = str(self.recency_minutes)
                encoded = urllib.parse.urlencode(params)
                payload, _ = get_json(
                    f"{self.endpoint}?{encoded}",
                    source=self.name.value,
                    headers={"X-API-Key": api_key},
                )
                if not isinstance(payload, dict):
                    raise SourceError(self.name.value, "expected a JSON object")
                records = payload.get("results")
                if not isinstance(records, list):
                    LOGGER.warning("%s: no 'results' array for a query", self.name.value)
                    break
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    url = str(record.get("url") or "")
                    if not is_allowed_public_url(self.name, url, self.allowed_authors):
                        continue
                    external_id = _canonical_id(url)
                    if external_id in seen:
                        continue
                    seen.add(external_id)
                    yield Evidence(
                        source=self.name,
                        external_id=external_id,
                        url=url,
                        title=str(record.get("title") or ""),
                        excerpt=str(record.get("snippet") or ""),
                        metadata={
                            "query": query,
                            "page": page,
                            "site_name": record.get("site_name"),
                            "position": record.get("position"),
                            "recency_minutes": self.recency_minutes,
                        },
                    )
                if not records or not _tinyfish_has_more(payload, page):
                    break


def _tinyfish_has_more(payload: dict[str, object], page: int) -> bool:
    """Follow only explicit pagination signals; never guess from a full page."""
    pagination = payload.get("pagination")
    containers = [payload]
    if isinstance(pagination, dict):
        containers.append(pagination)
    for container in containers:
        for key in ("has_more", "hasMore", "more_results_available"):
            value = container.get(key)
            if isinstance(value, bool):
                return value
        for key in ("next_page", "nextPage"):
            value = container.get(key)
            if value is not None and value is not False:
                return True
        total_pages = container.get("total_pages", container.get("totalPages"))
        if isinstance(total_pages, int):
            return page + 1 < total_pages
    return False


def _canonical_id(url: str) -> str:
    """Stable id for a post URL, ignoring tracking parameters.

    x.com/a/status/1?s=20 and x.com/a/status/1 are the same post; hashing the
    raw URL would treat them as two and alert twice.
    """
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("mobile.")
    if host in {"twitter.com", "x.com"}:
        match = re.search(r"/status/(\d+)", parsed.path)
        if match:
            return f"x:{match.group(1)}"
    if host.endswith("linkedin.com"):
        activity = re.search(r"activity[-:](\d+)", parsed.path)
        if activity:
            return f"li:{activity.group(1)}"
        company = re.search(r"/company/([\w\-.%]+)", parsed.path)
        if company:
            return f"li-company:{company.group(1).lower()}"
    normalised = urllib.parse.urlunsplit(
        (parsed.scheme, host, parsed.path.rstrip("/"), "", "")
    )
    return hashlib.sha256(normalised.encode()).hexdigest()[:32]


_X_HOSTS = {"x.com", "twitter.com", "mobile.x.com", "mobile.twitter.com"}
#: /company/<slug> and nothing deeper.
_COMPANY_ROOT = re.compile(r"/company/[\w\-.%]+/?")


def is_allowed_public_url(
    source: Source, url: str, allowed_authors: tuple[str, ...] = ()
) -> bool:
    """Is this a canonical, public URL of the shape this source expects?

    `allowed_authors` additionally pins a result to a specific account path,
    which is what makes an "official announcement" snapshot actually official.
    """
    if not url:
        return False
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path
    if source in {Source.X, Source.OFFICIAL_X}:
        if host not in _X_HOSTS or "/status/" not in path:
            return False
        return _author_allowed(path.strip("/").split("/")[0], allowed_authors)
    if source in {Source.LINKEDIN, Source.OFFICIAL_LINKEDIN}:
        if not host.endswith("linkedin.com"):
            return False
        if "/posts/" not in path and "/feed/update/" not in path:
            return False
        return _author_allowed(_linkedin_owner(path), allowed_authors)
    if source is Source.LINKEDIN_COMPANY:
        # Must be the company root. "/company/acme/posts/x_activity-1" is a
        # post, and labelling it "company page first observed" is simply wrong.
        return bool(host.endswith("linkedin.com") and _COMPANY_ROOT.fullmatch(path))
    return False


def _author_allowed(owner: str, allowed: tuple[str, ...]) -> bool:
    if not allowed:
        return True
    return owner.lower() in allowed


def _linkedin_owner(path: str) -> str:
    match = re.search(r"/(?:posts|company|in)/([\w\-.%]+)", path)
    if not match:
        return ""
    # LinkedIn post slugs look like "jane-doe_activity-123"; the owner is the
    # part before the first underscore.
    return match.group(1).split("_")[0]


def _observed(store, source: str, external_id: str) -> bool:
    row = store.connection.execute(
        "SELECT 1 FROM observations WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()
    return row is not None


def _titleise(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.split("-") if part)


# ------------------------------------------------------------------- registry

#: Batch labels to hunt for. Configurable so a new batch does not need a code
#: change; defaults cover the batches plausibly announcing now.
def _batches() -> list[str]:
    raw = os.environ.get("LAUNCHSIGNAL_BATCHES", "YC S26,YC F26,YC W26,YC X26")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _accounts(name: str, default: str) -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    return tuple(item.strip().lstrip("@") for item in raw.split(",") if item.strip())


#: Claim phrasings, split by programme so a Speedrun post is never keyed,
#: labelled or compared as a YC claim.
_YC_TERMS = (
    '"got into YC"',
    '"got into Y Combinator"',
    '"accepted into Y Combinator"',
    '"got accepted into YC"',
)
_SPEEDRUN_TERMS = (
    '"a16z Speedrun"',
    '"joined Speedrun"',
)


def _claim_terms() -> list[str]:
    return [*_YC_TERMS, *(f'"{batch}"' for batch in _batches()), *_SPEEDRUN_TERMS]


def social_sources(*, recency_minutes: int | None = None) -> list[TinyfishSearchSource]:
    """Founder-claim discovery on X and LinkedIn, plus LinkedIn company pages.

    The programme is not fixed per query family: one search can return both, and
    a post can mention either. It is resolved from the claim text by
    `classifier.resolve_programme`.
    """
    terms = _claim_terms()
    return [
        TinyfishSearchSource(
            Source.X,
            [f"site:x.com {term}" for term in terms],
            recency_minutes=recency_minutes,
        ),
        TinyfishSearchSource(
            Source.LINKEDIN,
            [f"site:linkedin.com/posts {term}" for term in terms],
            recency_minutes=recency_minutes,
        ),
        # Requirement: new LinkedIn *company page* creations, not only posts.
        TinyfishSearchSource(
            Source.LINKEDIN_COMPANY,
            [f"site:linkedin.com/company {term}" for term in terms],
            recency_minutes=recency_minutes,
        ),
    ]


_MAX_OFFICIAL_IDENTITIES = 10
_OFFICIAL_IDENTITY_GROUP_SIZE = 5


def _identity_query_terms(names: Iterable[str]) -> list[str]:
    """Make a bounded set of exact-name OR groups for official-account search."""
    clean: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = " ".join(str(raw).split()).strip()[:120]
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        # Search syntax treats quotes and backslashes specially. Strip them
        # rather than letting a company name alter the surrounding query.
        safe_name = name.replace('"', "").replace("\\", "")
        if not safe_name:
            continue
        clean.append(f'"{safe_name}"')
        if len(clean) == _MAX_OFFICIAL_IDENTITIES:
            break
    return [
        f"({' OR '.join(clean[index:index + _OFFICIAL_IDENTITY_GROUP_SIZE])})"
        for index in range(0, len(clean), _OFFICIAL_IDENTITY_GROUP_SIZE)
    ]


def official_sources(
    company_names: Mapping[str, Iterable[str]] | None = None,
) -> list[TinyfishSearchSource]:
    """Official snapshots, including bounded exact searches for current candidates.

    `company_names` is supplied from same-cycle social discovery by the monitor.
    It is capped to ten identities per programme, grouped five at a time, so a
    noisy result set cannot fan out into unbounded public-index requests.
    """
    batch_terms = [f'"{batch}"' for batch in _batches()]
    company_names = company_names or {}
    yc_terms = [*batch_terms, *_identity_query_terms(company_names.get(PROGRAMME_YC, ()))]
    speedrun_terms = [
        '"Speedrun"',
        *_identity_query_terms(company_names.get(PROGRAMME_SPEEDRUN, ())),
    ]
    yc_x = _accounts("LAUNCHSIGNAL_OFFICIAL_X", "ycombinator")
    yc_li = _accounts("LAUNCHSIGNAL_OFFICIAL_LINKEDIN", "y-combinator")
    sr_x = _accounts("LAUNCHSIGNAL_OFFICIAL_SPEEDRUN_X", "a16z,speedrun")
    return [
        TinyfishSearchSource(
            Source.OFFICIAL_X,
            [f"site:x.com/{account} {term}" for account in yc_x for term in yc_terms],
            allowed_authors=yc_x,
            programme=PROGRAMME_YC,
            max_pages=2,
        ),
        TinyfishSearchSource(
            Source.OFFICIAL_LINKEDIN,
            [f"site:linkedin.com/company/{account} {term}" for account in yc_li for term in yc_terms],
            allowed_authors=yc_li,
            programme=PROGRAMME_YC,
            max_pages=2,
        ),
        TinyfishSearchSource(
            Source.OFFICIAL_X,
            [f"site:x.com/{account} {term}" for account in sr_x for term in speedrun_terms],
            allowed_authors=sr_x,
            programme=PROGRAMME_SPEEDRUN,
            max_pages=2,
        ),
    ]


def directory_sources() -> list[SourceAdapter]:
    """The directory adapters, plus any operator-supplied one."""
    adapters: list[SourceAdapter] = [YcSitemapSource(), A16zSpeedrunSource()]
    custom = ConfigurableDirectorySource()
    if custom.configured:
        adapters.append(custom)
    return adapters


def all_scan_sources(*, recency_minutes: int | None = None) -> list[SourceAdapter]:
    return [*directory_sources(), *social_sources(recency_minutes=recency_minutes)]
