"""Two sets of verdicts for the same jobs, side by side (``jobscraper compare-verdicts``).

A side is the latest verdict per job among the ones written under a prompt version, a profile
fingerprint (:func:`jobscraper.ai.prompts.profile_fingerprint`), or both. The comparison runs
over the jobs both sides scored, inside the day window the other stages read:

- how many jobs only one side scored, and how many both did;
- the score delta (new − old): mean, sample standard deviation and median;
- ``relevant`` flips in each direction;
- for the ranking stages, the top-N overlap (Jaccard) and the Spearman of the two score lists;
- the biggest movers either way;
- for ``rank``, the mean delta per rubric criterion where both sides carry ``criteria``;
- how many run notes were written about these jobs since the old side's newest verdict — what
  the new pass said about its inputs.

Nothing here changes a score or an ordering; it only reads.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from jobscraper.audit import spearman
from jobscraper.models import AIVerdict, Job, RunNote
from jobscraper.store import Store

__all__ = ["compare_verdicts", "jaccard", "load_comparison", "spearman", "top_set"]

#: The stages whose score is an ordering worth correlating; the screen's is a gate.
RANKING_STAGES = ("rank", "refine")
#: Below this many jobs in both sets the statistics are anecdotes; the CLI says so.
SMALL_INTERSECTION = 10
#: How many movers the comparison lists.
MOVERS = 10


def top_set(scores: Mapping[str, int], n: int) -> set[str]:
    """The ``n`` best-scoring job ids; the job id breaks ties so the set is the same every run."""
    return {jid for jid, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:n]}


def jaccard(x: set[str], y: set[str]) -> float:
    return len(x & y) / len(x | y) if x or y else 0.0


def _utc(when: datetime) -> datetime:
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _delta_stats(deltas: list[int]) -> dict[str, float | None]:
    if not deltas:
        return {"mean": None, "sd": None, "median": None}
    return {"mean": statistics.fmean(deltas),
            "sd": statistics.stdev(deltas) if len(deltas) > 1 else None,
            "median": float(statistics.median(deltas))}


def _criteria_deltas(old: Mapping[str, AIVerdict], new: Mapping[str, AIVerdict],
                     ids: list[str]) -> dict[str, dict[str, float]]:
    """Mean (new − old) per criterion key, over the jobs where both sides scored that key."""
    deltas: dict[str, list[int]] = {}
    for job_id in ids:
        before, after = old[job_id].criteria, new[job_id].criteria
        for key, value in after.items():
            if key in before:
                deltas.setdefault(key, []).append(value - before[key])
    return {key: {"n": len(values), "mean_delta": statistics.fmean(values)}
            for key, values in deltas.items()}


def _note_summary(notes: Iterable[RunNote], ids: set[str], since: datetime | None) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    if since is not None:
        for note in notes:
            if note.job_id in ids and _utc(note.created_at) >= since:
                by_kind[note.kind] = by_kind.get(note.kind, 0) + 1
    return {"since": since, "total": sum(by_kind.values()), "by_kind": by_kind}


def compare_verdicts(old: Mapping[str, AIVerdict], new: Mapping[str, AIVerdict],
                     jobs: Iterable[Job], *, stage: str, top: int = 30,
                     notes: Iterable[RunNote] = ()) -> dict[str, Any]:
    """Every statistic of the module docstring, as a dict the CLI prints.

    ``old`` / ``new`` map job id → verdict (the latest per job on that side); only the ids in
    ``jobs`` count. ``notes`` are the run notes to count (any order; filtered here).
    """
    by_id = {j.id: j for j in jobs}
    old = {k: v for k, v in old.items() if k in by_id}
    new = {k: v for k, v in new.items() if k in by_id}
    both = sorted(set(old) & set(new))
    deltas = {job_id: new[job_id].score - old[job_id].score for job_id in both}

    ranking = stage in RANKING_STAGES
    top_overlap = spearman_rho = None
    if ranking:
        old_scores = {k: old[k].score for k in both}
        new_scores = {k: new[k].score for k in both}
        top_overlap = {"n": top,
                       "jaccard": jaccard(top_set(old_scores, top), top_set(new_scores, top)) if both else None}
        spearman_rho = spearman([float(old_scores[k]) for k in both], [float(new_scores[k]) for k in both])

    moved = sorted((k for k in both if deltas[k]), key=lambda k: (-abs(deltas[k]), k))[:MOVERS]
    movers = [{"job_id": k, "title": by_id[k].title, "company": by_id[k].company or "unknown",
               "old": old[k].score, "new": new[k].score, "delta": deltas[k]} for k in moved]

    since = max((_utc(v.created_at) for v in old.values()), default=None)
    return {
        "counts": {"old_only": len(set(old) - set(new)), "new_only": len(set(new) - set(old)),
                   "both": len(both)},
        "delta": _delta_stats(list(deltas.values())),
        "flips": {"true_to_false": sum(1 for k in both if old[k].relevant and not new[k].relevant),
                  "false_to_true": sum(1 for k in both if not old[k].relevant and new[k].relevant)},
        "top": top_overlap,
        "spearman": spearman_rho,
        "movers": movers,
        "criteria": _criteria_deltas(old, new, both) if stage == "rank" else {},
        "notes": _note_summary(notes, set(both), since),
        "small": len(both) < SMALL_INTERSECTION,
    }


def load_comparison(store: Store, jobs: Iterable[Job], *, stage: str,
                    old_version: str | None = None, old_hash: str | None = None,
                    new_version: str | None = None, new_hash: str | None = None,
                    top: int = 30) -> dict[str, Any]:
    """Read both sides from ``store`` (latest verdict per job, see :meth:`Store.verdicts`) and
    compare them. A side given both a version and a hash takes the verdicts matching both."""
    jobs = list(jobs)
    old = store.verdicts(stage, old_version, profile_hash=old_hash)
    new = store.verdicts(stage, new_version, profile_hash=new_hash)
    ids = {j.id for j in jobs}
    since = max((v.created_at for v in old.values() if v.job_id in ids), default=None)
    notes = store.notes(since=since) if since is not None else []  # filtered again by job there
    return compare_verdicts(old, new, jobs, stage=stage, top=top, notes=notes)
