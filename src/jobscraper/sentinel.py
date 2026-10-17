"""Sentinels: measure the two gates whose false rejects nothing else can see.

The rule filter's drops never reach an AI, and the screen's rejects never reach the ranker, so a
good posting either gate throws away is gone without a trace — ``jobscraper eval`` only knows the
owner's decisions on jobs that passed both. Each run a small random sample of each gate's
rejects is judged by the stage after the gate anyway:

``sentinel_prefilter``
    a rule drop, screened with the normal screening prompt;
``sentinel_rank``
    a screen reject, ranked with the normal ranking prompt (no reference scores).

The verdicts are stored under those stage names (:data:`jobscraper.models.SENTINEL_STAGES`), so
the queue, the shortlist, the anchors and the report's sections — all keyed on ``prefilter`` /
``rank`` / ``refine`` — never see them. The report's ``## Sentinels`` section turns them into an
estimated false-reject rate per gate (:func:`jobscraper.report.sentinel_summary`).

The job prompt is the stage's own, with one line in front saying which gate rejected the posting
and asking for an unbiased judgement (:func:`sentinel_prefix`).
"""

from __future__ import annotations

import random
from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, date, datetime

from jobscraper.audit import reason_category
from jobscraper.models import AIVerdict, FilterResult, Job

#: A screen reject the sentinel ranker scores at least this is a find the owner should read, and
#: counts as a false reject of the screen.
SENTINEL_FIND_SCORE = 70

#: Rule-drop reason categories (:func:`jobscraper.audit.reason_category`) that are right by
#: construction: a posting past its age limit or its own closing date, or one whose page says it
#: is closed or gone (``check-live``). A drop with only these reasons is left out of the
#: rule-drop pool, since sampling it would only dilute the estimate.
STALE_REASON_CATEGORIES = frozenset({"too_old", "deadline_passed", "closed"})


def default_seed(today: date | None = None) -> int:
    """``20261013`` for 13 October 2026: each day samples differently, a same-day re-run repeats."""
    day = today or datetime.now(UTC).date()
    return int(f"{day:%Y%m%d}")


def _sample(pool: Iterable[Job], n: int, seed: int) -> list[Job]:
    """``n`` of ``pool`` (all of it when smaller), the same ones for the same seed and pool.

    The pool is ordered by job id first, so the order the jobs came in does not matter.
    """
    ordered = sorted(pool, key=lambda j: j.id)
    if n <= 0 or not ordered:
        return []
    return random.Random(seed).sample(ordered, min(n, len(ordered)))


def sample_rule_drops(jobs: Iterable[Job], filters: Mapping[str, FilterResult], n: int, seed: int,
                      *, exclude: Collection[str] = ()) -> list[Job]:
    """A random sample of the rule filter's drops among ``jobs`` (the listed ones in the window).

    ``exclude`` holds the jobs that already carry a ``sentinel_prefilter`` verdict: a posting is
    measured once. A drop whose every reason is staleness (:data:`STALE_REASON_CATEGORIES`) is
    not in the pool; one dropped for its age *and* something else is.
    """
    pool = [j for j in jobs
            if (fr := filters.get(j.id)) is not None and fr.status == "drop"
            and not _stale_only(fr) and j.id not in exclude]
    return _sample(pool, n, seed)


def _stale_only(fr: FilterResult) -> bool:
    """True when ``fr`` has reasons and all of them are age / deadline / closed-page ones."""
    return bool(fr.reasons) and all(reason_category(r) in STALE_REASON_CATEGORIES
                                    for r in fr.reasons)


def sample_screen_rejects(jobs: Iterable[Job], filters: Mapping[str, FilterResult],
                          prefilter: Mapping[str, AIVerdict], n: int, seed: int, *, min_score: int,
                          exclude: Collection[str] = ()) -> list[Job]:
    """A random sample of the screen's rejects among ``jobs``.

    A reject is a job the rule filter kept (``keep``/``review``) whose screening verdict says
    ``relevant=False`` or scores under ``min_score`` (``ai.prefilter_min_score``) — exactly the
    jobs :func:`jobscraper.report.rank_queue` leaves out. ``exclude`` holds the jobs that already
    carry a ``sentinel_rank`` verdict (and, from the callers, a real ``rank`` one: such a job is
    in the report already, so it was not lost).
    """
    def rejected(job: Job) -> bool:
        fr, v = filters.get(job.id), prefilter.get(job.id)
        return (fr is not None and fr.status != "drop" and v is not None
                and (not v.relevant or v.score < min_score))

    return _sample((j for j in jobs if j.id not in exclude and rejected(j)), n, seed)


def rule_drop_gate(fr: FilterResult | None) -> str:
    """How the SENTINEL line names a rule drop: the rule filter and its reasons."""
    reasons = "; ".join(fr.reasons) if fr and fr.reasons else "no reason recorded"
    return f"rule filter: {reasons}"


def screen_gate(verdict: AIVerdict) -> str:
    """How the SENTINEL line names a screen reject: its screen score and one-line summary."""
    return f"screen: score {verdict.score}, {verdict.summary}"


def sentinel_prefix(gate: str) -> str:
    """The line in front of a sentinel's job prompt (``job_prompt(..., prefix=...)``)."""
    return (f"SENTINEL: this posting was rejected by an earlier gate ({gate}). Judge it exactly "
            "as you would any other posting; do not assume the rejection was right or wrong.")
