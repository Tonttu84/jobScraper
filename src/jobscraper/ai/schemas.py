"""Structured-output schemas for the AI stages."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Screening(BaseModel):
    relevant: bool = Field(description="Keep for the detailed review?")
    score: int = Field(ge=0, le=100)
    language_ok: bool
    seniority_ok: bool
    location_ok: bool
    summary: str = Field(description="One sentence: what the job is and the main reason for the score")
    concerns: list[str] = Field(default_factory=list)


class RefinedJob(BaseModel):
    """One posting's place in the shortlist, judged against all the others in the same request."""

    job_id: str
    position: int = Field(description="Place in the shortlist, 1 = best")
    score: int = Field(ge=0, le=100, description="Probability in percent that this one is worth an evening")
    summary: str = Field(description="one sentence: why it sits here relative to the others")


class Refinement(BaseModel):
    """The whole shortlist, re-ordered in a single answer."""

    items: list[RefinedJob]


class Ranking(BaseModel):
    relevant: bool
    score: int = Field(ge=0, le=100)
    language_ok: bool
    seniority_ok: bool
    location_ok: bool
    summary: str = Field(description="Two or three sentences on fit")
    concerns: list[str] = Field(default_factory=list, description="Risks or gaps, most important first")
    why_apply: list[str] = Field(default_factory=list, description="Matching strengths and application angle")
