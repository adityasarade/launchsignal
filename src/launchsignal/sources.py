"""Source adapters.

Each adapter reads one public source and yields `Evidence`. An adapter is the
only place that knows about a source's wire format, so adding a network means
writing one class and registering it -- the classifier, store and Slack renderer
never change.

Nothing here logs in, presents a session cookie, bypasses an anti-bot check, or
touches a page that requires an account.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import urllib.parse
import xml.etree.ElementTree as element_tree
from datetime import datetime, timezone
from typing import Iterable, Iterator, Protocol, runtime_checkable

from .http import NotModified, SourceError, get_json, get_text, request
from .models import Evidence, Source

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
        for slug, url, lastmod in entries:
            if slug in self.excluded_slugs:
                continue
            is_new = True
            if store is not None:
                is_new = not _already_observed(store, self.name.value, slug)
            detail: dict[str, object] = {}
            if enrich and is_new and enriched < self.max_enrich:
                detail = self._fetch_profile(slug)
                enriched += 1
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
        if enrich and enriched >= self.max_enrich:
            LOGGER.warning(
                "yc: profile enrichment capped at %d this cycle; remaining new "
                "companies will be enriched on the next scan",
                self.max_enrich,
            )
        if store is not None:
            store.set_etag(self.name.value, response.etag)

    def _fetch_profile(self, slug: str) -> dict[str, object]:
        """Pull batch, description and canonical name off a company profile.

        The page embeds an HTML-escaped JSON blob; parsing that is far more
        stable than scraping the rendered markup.
        """
        url = self.profile_template.format(slug=slug)
        try:
            body, _ = get_text(url, source=f"{self.name.value}:profile", attempts=2)
        except SourceError as error:
            LOGGER.warning("yc profile %s unavailable: %s", slug, error.message)
            return {}
        return _extract_yc_profile(body)


def _extract_yc_profile(body: str) -> dict[str, object]:
    unescaped = html.unescape(body)
    out: dict[str, object] = {}
    for key, target in (
        ("batch", "batch"),
        ("batch_name", "batch_name"),
        ("name", "name"),
        ("description", "description"),
        ("one_liner", "description"),
    ):
        match = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.){{0,400}})"', unescaped)
        if match and target not in out:
            value = match.group(1).encode().decode("unicode_escape", errors="replace")
            if value.strip():
                out[target] = value.strip()
    website = re.search(r'"url"\s*:\s*"(https?://(?:[^"\\]|\\.){0,200})"', unescaped)
    if website:
        out["website"] = website.group(1)
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


def _parse_sitemap(
    document: str, pattern: re.Pattern[str], source: str
) -> Iterator[tuple[str, str, str | None]]:
    try:
        root = element_tree.fromstring(document)
    except element_tree.ParseError as error:
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
    ) -> None:
        self.name = source
        self.queries = queries
        #: The freshness window. This is the actual early-detection lever: a
        #: founder post minutes old is what the task is asking for, so the
        #: founder lane asks the index for recent results explicitly.
        self.recency_minutes = recency_minutes
        #: For official-account snapshots, the account path that a result MUST
        #: be under. Without this any stranger's post counts as an official
        #: programme announcement and suppresses genuine early alerts.
        self.allowed_authors = tuple(a.strip("/").lower() for a in allowed_authors)

    @property
    def configured(self) -> bool:
        return bool(os.environ.get("TINYFISH_API_KEY"))

    def scan(self, store=None) -> Iterator[Evidence]:
        api_key = os.environ.get("TINYFISH_API_KEY")
        if not api_key:
            # Surfaced by the caller as a skipped source, never as a clean pass.
            raise SourceError(
                self.name.value,
                "TINYFISH_API_KEY is not set, so this source cannot be read",
            )
        for query in self.queries:
            params = {"query": query, "location": "US", "language": "en"}
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
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                url = str(record.get("url") or "")
                if not is_allowed_public_url(self.name, url, self.allowed_authors):
                    continue
                yield Evidence(
                    source=self.name,
                    external_id=_canonical_id(url),
                    url=url,
                    title=str(record.get("title") or ""),
                    excerpt=str(record.get("snippet") or ""),
                    metadata={
                        "query": query,
                        "site_name": record.get("site_name"),
                        "position": record.get("position"),
                        "recency_minutes": self.recency_minutes,
                    },
                )


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
        return host.endswith("linkedin.com") and "/company/" in path
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


def _titleise(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.split("-") if part)


def _already_observed(store, source: str, external_id: str) -> bool:
    row = store.connection.execute(
        "SELECT 1 FROM observations WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()
    return row is not None


# ------------------------------------------------------------------- registry

#: Batch labels to hunt for. Configurable so a new batch does not need a code
#: change; defaults cover the batches plausibly announcing now.
def _batches() -> list[str]:
    raw = os.environ.get("LAUNCHSIGNAL_BATCHES", "YC S26,YC F26,YC W26,YC X26")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _official_x_accounts() -> tuple[str, ...]:
    raw = os.environ.get("LAUNCHSIGNAL_OFFICIAL_X", "ycombinator,a16z,speedrun")
    return tuple(item.strip().lstrip("@") for item in raw.split(",") if item.strip())


def social_sources(*, recency_minutes: int | None = None) -> list[TinyfishSearchSource]:
    """Founder-claim discovery on X and LinkedIn, plus LinkedIn company pages."""
    batch_terms = [f'"{batch}"' for batch in _batches()]
    claim_terms = [
        '"got into YC"',
        '"accepted into Y Combinator"',
        '"got accepted into YC"',
        '"a16z Speedrun"',
    ]
    x_queries = [f"site:x.com {term}" for term in claim_terms + batch_terms]
    li_queries = [
        f"site:linkedin.com/posts {term}" for term in claim_terms + batch_terms
    ]
    # Requirement: new LinkedIn *company page* creations, not only posts. These
    # were missing entirely; a company-page query family is a separate source.
    company_queries = [
        f'site:linkedin.com/company {term}' for term in claim_terms + batch_terms
    ]
    return [
        TinyfishSearchSource(Source.X, x_queries, recency_minutes=recency_minutes),
        TinyfishSearchSource(Source.LINKEDIN, li_queries, recency_minutes=recency_minutes),
        TinyfishSearchSource(
            Source.LINKEDIN_COMPANY, company_queries, recency_minutes=recency_minutes
        ),
    ]


def official_sources() -> list[TinyfishSearchSource]:
    """Snapshots of what the programmes themselves have posted."""
    accounts = _official_x_accounts()
    batch_terms = [f'"{batch}"' for batch in _batches()]
    x_queries = [
        f"site:x.com/{account} {term}" for account in accounts for term in batch_terms
    ]
    li_queries = [
        f'site:linkedin.com/company/y-combinator {term}' for term in batch_terms
    ]
    return [
        TinyfishSearchSource(
            Source.OFFICIAL_X, x_queries, allowed_authors=accounts
        ),
        TinyfishSearchSource(
            Source.OFFICIAL_LINKEDIN, li_queries, allowed_authors=("y-combinator",)
        ),
    ]


def directory_sources() -> list[SourceAdapter]:
    return [YcSitemapSource(), A16zSpeedrunSource()]


def all_scan_sources(*, recency_minutes: int | None = None) -> list[SourceAdapter]:
    return [*directory_sources(), *social_sources(recency_minutes=recency_minutes)]
