"""Deterministic classification.

Two jobs, both of which must be conservative:

1. Decide whether a piece of evidence is news at all, and what kind.
2. Resolve which company it is about -- or refuse, and say why.

Refusing is a first-class outcome. A monitor that guesses a company name from a
tweet produces alert keys made of garbage, which silently breaks deduplication
and puts nonsense in front of a human. Anything unresolved goes to the review
queue instead.
"""

from __future__ import annotations

import re
import unicodedata

from .models import (
    PROGRAMME_SPEEDRUN,
    PROGRAMME_YC,
    Evidence,
    OfficialCheck,
    OfficialState,
    SignalKind,
    SourceCategory,
    category_of,
)

# --------------------------------------------------------------- claim detection

#: A first-person acceptance statement. "backed by" is deliberately excluded:
#: it is true of every YC alumnus forever and says nothing about a new batch.
_ACCEPTANCE = re.compile(
    r"""(?ix)
    \b(?:
        accepted \s+ (?:in)?to \s+ (?:yc|y \s* combinator)
      | got \s+ (?:in)?to \s+ (?:yc|y \s* combinator)
      | (?:we(?:'|’)?re|we \s+ are|i(?:'|’)?m) \s+ (?:in|part \s+ of) \s+ (?:yc|y \s* combinator)
      | joining \s+ (?:yc|y \s* combinator)
      | (?:yc|y \s* combinator) \s+ accepted \s+ us
      | part \s+ of \s+ (?:yc|y \s* combinator) (?:'|’)?s? \s+ (?:latest \s+ )?batch
    )\b
    """
)

#: A batch label such as "YC S26", "YC W25", "YC F24". Anchored so it does not
#: fire on "YC 2026" or a random alphanumeric run.
_BATCH = re.compile(
    r"(?ix) \b (?: yc | y \s* combinator ) \s*"
    r"( (?: [swf] | summer | winter | fall | spring ) \s* \d{2,4} ) \b"
)
_BATCH_SHORT = re.compile(r"(?i)\byc\s*([swf]\d{2})\b")

_SPEEDRUN_CLAIM = re.compile(
    r"""(?ix)
    \b(?:
        a16z \s+ speedrun
      | speedrun \s+ (?:batch|cohort|accelerator)
      | (?:accepted|got) \s+ (?:in)?to \s+ speedrun
      | \b sr \d{3} \b
    )\b
    """
)

# ------------------------------------------------------------ name resolution

#: Words that are never part of a company name and mark the end of one.
_STOP_WORDS = frozenset(
    """a an and the at in on to for of our we us i my me is are was were be been
    after before come join us joining building built build launch launched
    launching today now next excited thrilled proud happy announce announcing
    announced share sharing news huge big finally officially just been from with
    that this these those it its as so very really super along here there where
    when what who how why all more most some any their his her they he she you
    your yc combinator speedrun a16z batch cohort accelerator company startup
    startups sf san francisco york city team last year years month months week
    weeks day days going all backed funded raise raised round seed pre
    introducing launching announcing meet presenting welcome welcoming
    thrilled excited proud delighted stoked psyched""".split()
)

#: Patterns that name a company explicitly, most reliable first. The keyword
#: parts are case-insensitive via scoped `(?i:...)` groups, but the capture
#: itself stays case-SENSITIVE: a company name in a post is capitalised, and a
#: global `(?i)` flag would let `[A-Z]` match "the future of logistics".
_ENTITY = r"([A-Z][\w&'\-\.]*(?:\s+[A-Z0-9][\w&'\-\.]*){0,3})"

_NAME_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            _ENTITY
            + r"\s+(?:(?i:has\s+been|have\s+been|was|were|is|are|just)\s+)?"
            + r"(?i:got\s+accepted|been\s+accepted|accepted|joined|got)\s+"
            + r"(?i:in)?to\s+(?i:yc|y\s*combinator|speedrun)"
        ),
        "company-accepted",
    ),
    (
        re.compile(
            r"(?i:co-?founder|founder|ceo|cto|coo)\s+"
            r"(?:(?i:and)\s+\w+\s+)?(?i:of|at)\s+" + _ENTITY
        ),
        "founder-of",
    ),
    (
        re.compile(r"(?i:introducing|launching|announcing)\s+" + _ENTITY),
        "introducing",
    ),
    (
        # "Acme AI (@acmeai) on X" -- an X account title carrying a handle. The
        # handle disambiguates it even if the display name is a person, so this
        # stays actionable. The bare "<Person> on LinkedIn:" form is deliberately
        # NOT here: it names the author, and reading it as the company turns
        # every founder into a fake company record.
        re.compile(r"^" + _ENTITY + r"\s+\(@[\w]+\)\s+on\s+(?:X|Twitter)\b"),
        "x-account-title",
    ),
    (
        re.compile(r"(?i:we\s+are|we(?:'|\u2019)re)\s+" + _ENTITY),
        "we-are",
    ),
    (
        re.compile(r"(?i:building|behind)\s+" + _ENTITY),
        "building",
    ),
    (
        # Lowest confidence: a bare "at <Entity>". Kept last so a better pattern
        # always wins.
        re.compile(r"(?i:working\s+on|now\s+at|at)\s+" + _ENTITY),
        "at-entity",
    ),
)

_HANDLE = re.compile(r"(?<![\w])@([A-Za-z0-9_]{2,15})\b")

_LEGAL_SUFFIX = re.compile(
    r"(?i)\b(?:inc|inc\.|llc|ltd|limited|corp|corporation|co|gmbh|bv|pty|plc|sas|srl)\b"
)


def _tokens(value: str) -> list[str]:
    """Lowercase alphanumeric tokens, accents folded, legal suffixes removed."""
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    text = _LEGAL_SUFFIX.sub(" ", text)
    return [token for token in re.split(r"[^A-Za-z0-9]+", text.lower()) if token]


def canonical_name(value: str) -> str:
    """Stable comparison form of a company name.

    Unlike a bare alphanumeric squash this keeps token boundaries, which is what
    stops "Arc" from matching inside "Arcade".
    """
    return " ".join(_tokens(value))


def _clean_candidate(raw: str) -> str | None:
    """Trim a regex capture down to a plausible company name, or reject it."""
    text = raw.strip().strip("\"'“”")
    # A capture may run past the name into the next clause. Cut at the first
    # sentence break, then drop trailing filler words.
    text = re.split(r"[.!?,;:—–]|\s+[-–]\s+", text)[0]
    words = [word for word in text.split() if word]
    while words and words[-1].lower().strip(".") in _STOP_WORDS:
        words.pop()
    while words and words[0].lower().strip(".") in _STOP_WORDS:
        words.pop(0)
    if not words or len(words) > 4:
        return None
    candidate = " ".join(words).strip(" .,-&")
    if len(candidate) < 2 or len(candidate) > 60:
        return None
    if not canonical_name(candidate):
        return None
    # Reject a "name" that is really just programme vocabulary.
    if all(word.lower().strip(".") in _STOP_WORDS for word in words):
        return None
    if _ACCEPTANCE.search(candidate) or _BATCH.search(candidate):
        return None
    return candidate


def _validate_source_name(name: str) -> str | None:
    """Validate a name a source stated outright.

    Deliberately permissive. `_clean_candidate` exists to salvage a name from a
    noisy tweet, and applying its rules here is wrong: a directory saying
    "Mark Cuban Cost Plus Drug Company PBC" *is* the name, and real YC companies
    are called "Super", "Here", "Welcome" and "Her". Trimming those to nothing
    silently discarded legitimate companies.
    """
    candidate = name.strip().strip("\"'\u201c\u201d")
    if not candidate or len(candidate) > 120:
        return None
    if not canonical_name(candidate):
        # No alphanumeric content at all, e.g. punctuation only.
        return None
    return candidate


def resolve_company(evidence: Evidence) -> tuple[str | None, str]:
    """Return (company_name, reason).

    A directory source states the name outright and is trusted. A social source
    must have the name extracted from prose, and when that fails the caller
    routes the item to review rather than inventing one.
    """
    if evidence.company_name:
        given = _validate_source_name(evidence.company_name)
        if given:
            return given, "source-provided"
    haystack = f"{evidence.title}\n{evidence.excerpt}"
    for pattern, reason in _NAME_PATTERNS:
        for match in pattern.finditer(haystack):
            candidate = _clean_candidate(match.group(1))
            if candidate:
                return candidate, reason
    return None, "unresolved"


_AUTHOR_TITLE = re.compile(
    r"^([A-Z][\w&'\-\.]*(?:\s+[A-Z][\w&'\-\.]*){0,3})"
    r"\s+(?:\(@[\w]+\)\s+)?on\s+(?:X|LinkedIn|Twitter)\b"
)


def author_name(evidence: Evidence) -> str | None:
    """Display name of whoever published a social post, when discoverable.

    Public search-result titles are consistently "<Author> on <Network>", which
    is where the alert card's Founder field comes from.
    """
    if evidence.founder:
        return evidence.founder
    match = _AUTHOR_TITLE.search(evidence.title.strip())
    if not match:
        return None
    candidate = match.group(1).strip()
    return candidate if 2 <= len(candidate) <= 60 else None


def founder_handle(evidence: Evidence) -> str | None:
    if evidence.founder_handle:
        return evidence.founder_handle
    match = _HANDLE.search(evidence.text)
    return f"@{match.group(1)}" if match else None


def batch_label(evidence: Evidence) -> str | None:
    """Best available batch/cohort label, e.g. 'YC S26'."""
    if evidence.batch:
        return evidence.batch
    short = _BATCH_SHORT.search(evidence.text)
    if short:
        return f"YC {short.group(1).upper()}"
    match = _BATCH.search(evidence.text)
    if match:
        collapsed = re.sub(r"\s+", "", match.group(1)).upper()
        return f"YC {collapsed}"
    speedrun = re.search(r"(?i)\b(sr\d{3})\b", evidence.text)
    if speedrun:
        return speedrun.group(1).upper()
    return None


# ------------------------------------------------------------- classification


def resolve_programme(evidence: Evidence) -> str:
    """Which programme is this evidence about?

    A directory states its own programme and is trusted. Social evidence has to
    be read from the claim: a search adapter cannot know in advance, and
    defaulting to "YC" turned every Speedrun post into an "EARLY YC SIGNAL"
    with a YC company key, compared against YC's official accounts.
    """
    if category_of(evidence.source) is SourceCategory.DIRECTORY:
        return evidence.programme
    if _SPEEDRUN_CLAIM.search(evidence.text):
        return PROGRAMME_SPEEDRUN
    if _ACCEPTANCE.search(evidence.text) or _BATCH.search(evidence.text) or _BATCH_SHORT.search(
        evidence.text
    ):
        return PROGRAMME_YC
    return evidence.programme


def claim_kind(evidence: Evidence) -> SignalKind:
    category = category_of(evidence.source)
    if category is SourceCategory.DIRECTORY:
        return SignalKind.CONFIRMED
    if category is SourceCategory.COMPANY_PAGE:
        # A company page corroborates a claim; it does not assert one.
        return SignalKind.COMPANY_PAGE_FIRST_SEEN
    if category is not SourceCategory.SOCIAL:
        return SignalKind.NONE
    text = evidence.text
    if _ACCEPTANCE.search(text) or _SPEEDRUN_CLAIM.search(text):
        return SignalKind.EARLY_FOUNDER_CLAIM
    if _BATCH_SHORT.search(text) or _BATCH.search(text):
        return SignalKind.EARLY_FOUNDER_CLAIM
    return SignalKind.NONE


def official_check(
    company_name: str,
    official_evidence: list[Evidence],
    accounts_checked: tuple[str, ...],
    checked_at,
    *,
    min_snapshots: int = 1,
) -> OfficialCheck:
    """Compare a claim against official snapshots, honestly.

    NOT_SEEN requires that snapshots were actually obtained. A search that
    succeeds but returns nothing -- an unsupported operator, an account not yet
    indexed -- yields zero snapshots, and concluding "not yet announced" from
    that is the same zero-evidence error as not checking at all.
    """
    if not accounts_checked or len(official_evidence) < max(1, min_snapshots):
        return OfficialCheck(
            state=OfficialState.NOT_CHECKED,
            accounts_checked=accounts_checked,
            snapshots_seen=len(official_evidence),
            checked_at=checked_at if accounts_checked else None,
        )
    match = _first_official_match(company_name, official_evidence)
    if match is not None:
        return OfficialCheck(
            state=OfficialState.SEEN,
            accounts_checked=accounts_checked,
            snapshots_seen=len(official_evidence),
            checked_at=checked_at,
            matched_url=match,
        )
    return OfficialCheck(
        state=OfficialState.NOT_SEEN,
        accounts_checked=accounts_checked,
        snapshots_seen=len(official_evidence),
        checked_at=checked_at,
    )


def _first_official_match(company_name: str, official_evidence: list[Evidence]) -> str | None:
    target = _tokens(company_name)
    if not target:
        return None
    for item in official_evidence:
        if _contains_token_sequence(_tokens(item.text), target):
            return item.url
    return None


def _contains_token_sequence(haystack: list[str], needle: list[str]) -> bool:
    """Whole-token subsequence match.

    Substring matching on a squashed string reports "Arc" inside "Arcade" and
    "Ply" inside "Multiply", which silently suppresses genuine early alerts for
    every short company name.
    """
    if not needle or len(needle) > len(haystack):
        return False
    first = needle[0]
    span = len(needle)
    for index, token in enumerate(haystack):
        if token == first and haystack[index : index + span] == needle:
            return True
    return False


def official_mentions_company(company_name: str, official_evidence: list[Evidence]) -> bool:
    """Back-compat helper: did any collected snapshot name this company?"""
    return _first_official_match(company_name, official_evidence) is not None
