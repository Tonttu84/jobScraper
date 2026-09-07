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
    usage: dict[str, int] = Field(default_factory=dict)
