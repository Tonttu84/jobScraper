"""Normalized job model shared by every source, the filters, and the AI stages."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

RemoteKind = Literal["remote", "hybrid", "onsite", "unknown"]


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or None


class Job(BaseModel):
    """One posting as seen by one source.

    Everything the rule filter and the AI stages need must be here. ``raw`` keeps the
    original payload so a source can be re-normalized later without re-fetching.
    """

    source: str = Field(description="Source adapter name, e.g. 'duunitori'")
    source_id: str = Field(description="Stable id within the source (slug, numeric id, URL)")
    url: str
    title: str
    company: str | None = None
    description: str | None = Field(None, description="Plain text, HTML stripped")
    location_raw: str | None = Field(None, description="Location string as shown by the source")
    country: str | None = Field(None, description="ISO-3166 alpha-2, upper case, when known")
    city: str | None = None
    remote: RemoteKind = "unknown"
    remote_region: str | None = Field(
        None, description="Free text the source gives about who may apply remotely, e.g. 'Europe only'"
    )
    seniority_raw: str | None = Field(None, description="Seniority label from the source, if any")
    employment_type: str | None = Field(None, description="full_time/part_time/internship/contract/…")
    posting_language: str | None = Field(None, description="ISO-639-1 of the text, set by filters")
    salary_text: str | None = None
    tags: list[str] = Field(default_factory=list)
    posted_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def id(self) -> str:
        """Deterministic id: source + source_id."""
        return hashlib.sha1(f"{self.source}:{self.source_id}".encode()).hexdigest()[:16]

    @property
    def text(self) -> str:
        """Title + description, used by every text-based rule."""
        return "\n".join(p for p in (self.title, self.description) if p)

    def model_post_init(self, __context: Any) -> None:
        self.title = _clean(self.title) or self.title
        self.description = _clean(self.description)
        self.company = _clean(self.company)
        self.location_raw = _clean(self.location_raw)
        if self.country:
            self.country = self.country.strip().upper()[:2]


FilterStatus = Literal["keep", "review", "drop"]


class FilterResult(BaseModel):
    """Outcome of the rule filter for one job."""

    job_id: str
    status: FilterStatus
    reasons: list[str] = Field(default_factory=list, description="Why it was dropped or flagged")
    signals: dict[str, Any] = Field(default_factory=dict, description="Detected facts for the AI stage")
    location_tier: int | None = Field(None, description="1=FI/EE, 2=EU/EEA, 3=extended, 0=remote-only")


class AIVerdict(BaseModel):
    """Structured output of an AI stage for one job."""

    job_id: str
    stage: Literal["prefilter", "rank"]
    model: str
    prompt_version: str
    relevant: bool
    score: int = Field(ge=0, le=100)
    language_ok: bool
    seniority_ok: bool
    location_ok: bool
    summary: str
    concerns: list[str] = Field(default_factory=list)
    why_apply: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    usage: dict[str, int | bool] = Field(
        default_factory=dict,
        description='Token counters, plus "batch": True when the verdict came from a Message Batch (half price)',
    )


DecisionStatus = Literal["interested", "applied", "skipped", "interview", "rejected", "offer"]


class Decision(BaseModel):
    """What one user decided about one job (the clickable state in the web UI)."""

    job_id: str
    user: str = Field(description="Free-form handle chosen in the UI; several students can share one DB")
    status: DecisionStatus
    note: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


ReportSection = Literal["ranked", "prefilter", "review"]


class ReportItem(BaseModel):
    job_id: str
    section: ReportSection
    position: int = Field(description="1-based order within the section")
    score: int | None = Field(None, description="rank score for 'ranked', prefilter score for 'prefilter', None for 'review'")


class ReportSnapshot(BaseModel):
    """The structured form of one `jobscraper report` run."""

    id: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    days: int
    prompt_version: str
    counts: dict[str, int] = Field(default_factory=dict)   # jobs, keep, review, drop, prefiltered, ranked
    cost: dict[str, float] = Field(default_factory=dict)
    path: str | None = Field(None, description="Markdown file written alongside, if any")
    items: list[ReportItem] = Field(default_factory=list)


class ReportMove(BaseModel):
    """A job ranked in two consecutive reports whose score moved noticeably."""

    job_id: str
    old_score: int | None = None
    new_score: int | None = None
    old_position: int
    new_position: int

    @property
    def delta(self) -> int:
        """New score minus old score (0 when either side has no score)."""
        if self.old_score is None or self.new_score is None:
            return 0
        return self.new_score - self.old_score


class ReportDiff(BaseModel):
    """What changed between two :class:`ReportSnapshot`s — the "what's new" of a scheduled run.

    ``old_id`` is None when there is nothing to diff against: then everything in the newer
    report counts as new.
    """

    old_id: int | None = None
    new_id: int | None = None
    old_created_at: datetime | None = None
    new_ranked: list[ReportItem] = Field(default_factory=list, description="Ranked now, not ranked before")
    gone_ranked: list[ReportItem] = Field(default_factory=list, description="Ranked before, not ranked now")
    new_prefilter: list[ReportItem] = Field(default_factory=list, description="In prefilter now, in neither section before")
    moved: list[ReportMove] = Field(default_factory=list, description="Ranked in both, score moved by ≥ MOVED_THRESHOLD")


class JobFacets(BaseModel):
    """Deterministic, filterable attributes derived from the posting text and filter signals."""

    job_id: str
    facets_version: str
    posting_language: str | None = Field(None, description="ISO-639-1 the posting is written in")
    languages_required: list[str] = Field(default_factory=list, description="ISO-639-1 codes explicitly required")
    languages_optional: list[str] = Field(default_factory=list, description="ISO-639-1 codes mentioned as a plus")
    stacks: list[str] = Field(default_factory=list, description="Sorted stack tags, see facets.STACKS")
    web_dev: bool = Field(False, description="True when the stack overlaps Full Stack Open (React/Node/TS web work)")

    def language_ok(self, spoken: set[str] | list[str]) -> bool:
        """True when someone who speaks ``spoken`` can work this job: the posting language (if
        known) and every explicitly required language are in ``spoken``."""
        have = {code.lower() for code in spoken}
        if self.posting_language and self.posting_language.lower() not in have:
            return False
        return all(code.lower() in have for code in self.languages_required)
