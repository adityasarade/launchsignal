"""Core value types.

Everything the monitor learns about the world is an `Evidence`. Everything it
tells a human is an `Alert`. Both are frozen so a record cannot be mutated
after it has been persisted or sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Source(StrEnum):
    YC_DIRECTORY = "yc_directory"
    A16Z_SPEEDRUN = "a16z_speedrun"
    X = "x"
    LINKEDIN = "linkedin"
    LINKEDIN_COMPANY = "linkedin_company"
    OFFICIAL_X = "official_x"
    OFFICIAL_LINKEDIN = "official_linkedin"


class SourceCategory(StrEnum):
    """How a source's evidence should be interpreted.

    Classification keys off the *category*, never off the source identity. That
    is what lets a fifth network be added by registering one adapter and one
    row in SOURCE_CATEGORIES below, with no edit to the classifier, the store,
    or the Slack renderer.
    """

    #: Establishes programme membership as a matter of public record.
    DIRECTORY = "directory"
    #: Carries a founder's own words, so it can only ever support a *claim*.
    SOCIAL = "social"
    #: A company presence that corroborates a claim but does not assert one.
    COMPANY_PAGE = "company_page"
    #: A snapshot of what a programme itself has said publicly.
    OFFICIAL = "official"


#: The single registration point for source semantics.
SOURCE_CATEGORIES: dict[Source, SourceCategory] = {
    Source.YC_DIRECTORY: SourceCategory.DIRECTORY,
    Source.A16Z_SPEEDRUN: SourceCategory.DIRECTORY,
    Source.X: SourceCategory.SOCIAL,
    Source.LINKEDIN: SourceCategory.SOCIAL,
    Source.LINKEDIN_COMPANY: SourceCategory.COMPANY_PAGE,
    Source.OFFICIAL_X: SourceCategory.OFFICIAL,
    Source.OFFICIAL_LINKEDIN: SourceCategory.OFFICIAL,
}


def category_of(source: Source) -> SourceCategory:
    return SOURCE_CATEGORIES.get(source, SourceCategory.SOCIAL)


DIRECTORY_SOURCES = frozenset(
    s for s, c in SOURCE_CATEGORIES.items() if c is SourceCategory.DIRECTORY
)
SOCIAL_SOURCES = frozenset(
    s for s, c in SOURCE_CATEGORIES.items() if c is SourceCategory.SOCIAL
)
OFFICIAL_SOURCES = frozenset(
    s for s, c in SOURCE_CATEGORIES.items() if c is SourceCategory.OFFICIAL
)

#: Human-facing source labels. The task owner's reference card shows
#: "X (Twitter)", not the raw enum value.
SOURCE_LABELS = {
    Source.YC_DIRECTORY: "YC Directory",
    Source.A16Z_SPEEDRUN: "a16z Speedrun Directory",
    Source.X: "X (Twitter)",
    Source.LINKEDIN: "LinkedIn",
    Source.LINKEDIN_COMPANY: "LinkedIn (company page)",
    Source.OFFICIAL_X: "Official X account",
    Source.OFFICIAL_LINKEDIN: "Official LinkedIn page",
}


class SignalKind(StrEnum):
    #: A directory lists the company. Membership is a matter of public record.
    CONFIRMED = "confirmed"
    #: A founder has publicly claimed acceptance. Membership is not yet proven.
    EARLY_FOUNDER_CLAIM = "early_founder_claim"
    #: A LinkedIn company page for a programme company was seen for the first time.
    COMPANY_PAGE_FIRST_SEEN = "company_page_first_seen"
    #: Matched a claim pattern but no company could be resolved. Goes to review.
    NEEDS_REVIEW = "needs_review"
    NONE = "none"


class OfficialState(StrEnum):
    #: Official snapshots were collected and none mentioned this company.
    NOT_SEEN = "not_seen"
    #: Official snapshots were collected and one mentioned this company.
    SEEN = "seen"
    #: No official snapshots were collected, so nothing can be said either way.
    NOT_CHECKED = "not_checked"


@dataclass(frozen=True)
class OfficialCheck:
    """The auditable result of comparing a claim against official accounts.

    `state` is deliberately three-valued. Collapsing NOT_CHECKED into NOT_SEEN
    is what lets a monitor assert "YC has not announced this" on the strength of
    zero evidence, so the two are kept apart all the way to the Slack card.
    """

    state: OfficialState
    accounts_checked: tuple[str, ...] = ()
    snapshots_seen: int = 0
    checked_at: datetime | None = None
    matched_url: str | None = None

    @property
    def performed(self) -> bool:
        return self.state is not OfficialState.NOT_CHECKED

    def describe(self) -> str:
        if self.state is OfficialState.NOT_CHECKED:
            return (
                "not checked - no official-snapshot source is configured, so this "
                "alert makes no claim about whether the programme has announced yet"
            )
        accounts = ", ".join(self.accounts_checked) or "none"
        stamp = self.checked_at.strftime("%Y-%m-%d %H:%M UTC") if self.checked_at else "unknown"
        if self.state is OfficialState.SEEN:
            return f"already announced by {accounts} (seen {stamp})"
        return (
            f"not found in {self.snapshots_seen} snapshot(s) from {accounts}, "
            f"checked {stamp}"
        )


@dataclass(frozen=True)
class Evidence:
    """One observation from one source at one point in time."""

    source: Source
    external_id: str
    url: str
    title: str
    excerpt: str
    observed_at: datetime = field(default_factory=utcnow)
    company_name: str | None = None
    programme: str = "YC"
    #: Batch/cohort label, e.g. "YC S26" or "SR003". Shown on the alert card.
    batch: str | None = None
    #: Founder display name, when the source publishes one.
    founder: str | None = None
    #: Founder social handle, e.g. "@janedoe".
    founder_handle: str | None = None
    #: Canonical programme profile URL, kept distinct from `url` so an alert can
    #: link to the evidence *and* to the directory record.
    profile_url: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"{self.title} {self.excerpt}".strip()


@dataclass(frozen=True)
class Alert:
    """A message the monitor intends to deliver exactly once."""

    alert_key: str
    company_key: str
    kind: SignalKind
    company_name: str
    programme: str
    source: Source
    source_url: str
    excerpt: str
    official: OfficialCheck
    batch: str | None = None
    founder: str | None = None
    founder_handle: str | None = None
    profile_url: str | None = None
    detected_at: datetime = field(default_factory=utcnow)

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source.value)


@dataclass(frozen=True)
class ReviewItem:
    """A pattern match the monitor refused to turn into an alert."""

    source: Source
    url: str
    excerpt: str
    reason: str
    observed_at: datetime = field(default_factory=utcnow)


@dataclass
class SourceOutcome:
    """Per-source result of one scan, so one bad source cannot hide the others."""

    source: str
    ok: bool
    observations: int = 0
    new: int = 0
    error: str | None = None
    duration_ms: int = 0
