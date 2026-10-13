"""What the AI stages say about their own inputs, turned into pipeline-health notes.

Both ways of running a stage — the API (:class:`jobscraper.ai.client.AIStage`) and the subagent
import (``scripts/ai_batches.py``) — store a verdict through :func:`store_verdict`, so the same
notes come out of either:

``missing_input`` / ``signal_conflict`` / ``instruction_gap`` / ``profile_gap``
    One per ``input_notes`` entry, by its category prefix; ``uncategorised`` when it has none.
``criteria_unknown_key``
    A criterion the profile's ``weights`` do not name; it is dropped from the verdict.
``rubric_mismatch``
    A rank (or ``sentinel_rank``) verdict whose score is more than :data:`RUBRIC_MISMATCH_POINTS`
    from the weighted mean of its own criteria. The score still ranks; the note says the two
    disagree.
``verdict_invalid``
    A subagent's verdict line that failed the schema (:func:`note_invalid_verdict`).

The notes never change a score, the ordering or the shortlist.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from jobscraper.ai.schemas import criterion_label
from jobscraper.models import mirrored_stage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jobscraper.models import AIVerdict
    from jobscraper.store import Store

#: The category tokens the prompts tell both stages to start an ``input_notes`` entry with.
INPUT_NOTE_KINDS = ("missing_input", "signal_conflict", "instruction_gap", "profile_gap")
#: The kind of an entry that starts with none of them.
UNCATEGORISED = "uncategorised"
#: A rank score further than this from its weighted criteria is noted as ``rubric_mismatch``.
RUBRIC_MISMATCH_POINTS = 15


def input_note_kind(entry: str) -> str:
    """``"profile_gap: notice period"`` → ``"profile_gap"``; backticks and case are forgiven."""
    head, sep, _ = entry.strip().lstrip("`").partition(":")
    token = head.strip().strip("`").lower()
    return token if sep and token in INPUT_NOTE_KINDS else UNCATEGORISED


def normalise_criteria(raw: Mapping[str, int], weights: Mapping[str, int]) -> tuple[dict[str, int], list[str]]:
    """``(criteria, dropped)``: keyed by the profile's weight keys, in their order, clamped to 0-100.

    A key matches a weight by its own name or by the label the prompt shows (``stack fit`` for
    ``stack_fit``), ignoring case. Everything else comes back in ``dropped``.
    """
    lookup: dict[str, str] = {}
    for key in weights:
        lookup[key.lower()] = key
        lookup[criterion_label(key).lower()] = key
    found: dict[str, int] = {}
    dropped: list[str] = []
    for name, value in raw.items():
        key = lookup.get(str(name).strip().lower())
        if key is None:
            dropped.append(str(name))
            continue
        found[key] = min(100, max(0, int(value)))
    return {key: found[key] for key in weights if key in found}, dropped


def weighted_criteria(criteria: Mapping[str, int], weights: Mapping[str, int]) -> float | None:
    """Σ criteria·weight / Σ weight over the criteria present; None when they weigh nothing."""
    keys = [key for key in criteria if key in weights]
    total = sum(weights[key] for key in keys)
    if not total:
        return None
    return sum(criteria[key] * weights[key] for key in keys) / total


def store_verdict(store: Store, verdict: AIVerdict, weights: Mapping[str, int]) -> AIVerdict:
    """Normalise the verdict's criteria, save it, and write its notes. Returns what was saved."""
    criteria, dropped = normalise_criteria(verdict.criteria, weights)
    verdict = verdict.model_copy(update={"criteria": criteria})
    store.save_verdict(verdict)
    for entry in verdict.input_notes:
        store.add_note(verdict.stage, input_note_kind(entry), entry, job_id=verdict.job_id)
    if dropped:
        store.add_note(verdict.stage, "criteria_unknown_key",
                       f"dropped criteria the profile's weights do not name: {', '.join(dropped)}",
                       job_id=verdict.job_id, data={"keys": dropped, "known": list(weights)})
    # A sentinel rank verdict is the ranker's own judgement, so it gets the same check; the note
    # carries the verdict's stage, which for a real rank verdict is "rank" as before.
    weighted = weighted_criteria(criteria, weights) if mirrored_stage(verdict.stage) == "rank" else None
    if weighted is not None and abs(verdict.score - weighted) > RUBRIC_MISMATCH_POINTS:
        store.add_note(verdict.stage, "rubric_mismatch",
                       f"score {verdict.score}, but its criteria weigh in at {weighted:.0f}",
                       job_id=verdict.job_id,
                       data={"score": verdict.score, "weighted": round(weighted, 1)})
    return verdict


def note_invalid_verdict(store: Store, stage: str, error: str, *, chunk: str) -> None:
    """A verdict line that failed the schema, so the report says answers went missing."""
    store.add_note(stage, "verdict_invalid", error, data={"chunk": chunk})
