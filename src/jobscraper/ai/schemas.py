"""Structured-output schemas for the AI stages."""

from __future__ import annotations

from pydantic import BaseModel, Field, create_model

_INPUT_NOTES = Field(
    default_factory=list,
    description="Problems with the input you were given, not with the job; usually empty. One short "
                "entry per problem, starting with its category: missing_input:, signal_conflict:, "
                "instruction_gap: or profile_gap:",
)


class Screening(BaseModel):
    relevant: bool = Field(description="Keep for the detailed review?")
    score: int = Field(ge=0, le=100)
    language_ok: bool
    seniority_ok: bool
    location_ok: bool
    summary: str = Field(description="One sentence: what the job is and the main reason for the score")
    concerns: list[str] = Field(default_factory=list)
    input_notes: list[str] = _INPUT_NOTES


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
    #: Before ``score`` on purpose: structured output is generated in property order, so the
    #: criteria are judged first and the score combines them (see the SCORING RUBRIC).
    criteria: dict[str, int] = Field(
        default_factory=dict,
        description="0-100 per SCORING RUBRIC criterion, keyed exactly by the criterion names shown; "
                    "empty when the prompt has no rubric",
    )
    score: int = Field(ge=0, le=100)
    language_ok: bool
    seniority_ok: bool
    location_ok: bool
    summary: str = Field(description="Two or three sentences on fit")
    concerns: list[str] = Field(default_factory=list, description="Risks or gaps, most important first")
    why_apply: list[str] = Field(default_factory=list, description="Matching strengths and application angle")
    input_notes: list[str] = _INPUT_NOTES


def criterion_label(key: str) -> str:
    """How a ``weights`` key is shown to the ranker and in the report: ``stack_fit`` → ``stack fit``."""
    return key.replace("_", " ")


def ranking_schema(weights: dict[str, int]) -> type[Ranking]:
    """The rank stage's API schema: :class:`Ranking`, with ``criteria`` spelled out per criterion.

    Structured output turns a free-form ``dict[str, int]`` into an object with no properties and
    ``additionalProperties: false`` — the model could only ever answer ``{}``. So when the profile
    has weights, the API path asks for one required integer per criterion, keyed by the label the
    SCORING RUBRIC shows. Without weights it is :class:`Ranking` itself, unchanged. The subagent
    path validates against plain :class:`Ranking` (a JSON line can carry any dict).
    """
    if not weights:
        return Ranking
    fields = {f"c{n}": (int, Field(alias=criterion_label(key))) for n, key in enumerate(weights)}
    criteria = create_model("Criteria", **fields)
    return create_model(
        "Ranking", __base__=Ranking,
        criteria=(criteria, Field(description=Ranking.model_fields["criteria"].description)),
    )
