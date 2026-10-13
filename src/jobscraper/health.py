"""Scrape-side pipeline-health checks: each writes a :class:`~jobscraper.models.RunNote` when
a source's output looks wrong, so the report can say so instead of quietly shrinking.

The checks never change what is stored or scored. The one decision they feed is the takedown
counter (``Store.mark_missing``): a listing that came back at a fraction of its usual size says
nothing about which postings were taken down, so ``scrape`` skips the count for that run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jobscraper.models import Job
    from jobscraper.store import Store

STAGE = "scrape"

#: A previous run smaller than this is too small a sample to call a drop against.
COUNT_DROP_MIN_PREVIOUS = 20
#: A run that fetched less than this share of the previous good run is flagged.
COUNT_DROP_RATIO = 0.5
#: A description shorter than this is as good as missing for the AI stages.
SHORT_DESCRIPTION_CHARS = 200
#: Flag a source when at least this share of its postings came without a usable description...
EMPTY_DESCRIPTION_SHARE = 0.25
#: ...and it fetched at least this many (a handful of postings is not a pattern).
EMPTY_DESCRIPTION_MIN_FETCHED = 10


def check_count(store: Store, source: str, run_id: int, fetched: int) -> int | None:
    """Flag a run that fetched under half of the source's previous good run (``source_count_drop``).

    Returns the previous run's count when the drop condition holds, else None — the caller uses
    it to keep the takedown counter still. The baseline is :meth:`Store.last_good_run`.
    """
    previous = store.last_good_run(source, run_id)
    if previous is None or previous["fetched"] < COUNT_DROP_MIN_PREVIOUS:
        return None
    if fetched >= previous["fetched"] * COUNT_DROP_RATIO:
        return None
    share = fetched / previous["fetched"]
    store.add_note(
        STAGE, "source_count_drop",
        f"fetched {fetched}, the previous complete run fetched {previous['fetched']} ({share:.0%})",
        source=source,
        data={"fetched": fetched, "previous": previous["fetched"],
              "previous_run_id": previous["id"], "run_id": run_id},
    )
    return previous["fetched"]


def note_source_error(store: Store, source: str, error: str) -> None:
    """Make the error that ended a source's fetch visible in the report (``source_error``)."""
    store.add_note(STAGE, "source_error", error, source=source)


def note_takedown_skipped(store: Store, source: str, *, fetched: int, previous: int) -> None:
    """Say that a complete-listing source's misses were not counted this run (``takedown_skipped``)."""
    store.add_note(
        STAGE, "takedown_skipped",
        f"listing came back with {fetched} of the previous {previous} postings; "
        "no posting was counted as taken down",
        source=source, data={"fetched": fetched, "previous": previous},
    )


def check_descriptions(store: Store, source: str, jobs: list[Job]) -> float | None:
    """Flag a source whose postings mostly arrive without a description (``empty_descriptions``).

    The screen and the ranker read the description; without one they guess, and a fair share
    of the screen's rejections say so. Returns the thin share when it was flagged, else None.
    """
    fetched = len(jobs)
    if fetched < EMPTY_DESCRIPTION_MIN_FETCHED:
        return None
    missing = sum(1 for j in jobs if not j.description)
    thin = sum(1 for j in jobs if len(j.description or "") < SHORT_DESCRIPTION_CHARS)
    share = thin / fetched
    if share < EMPTY_DESCRIPTION_SHARE:
        return None
    store.add_note(
        STAGE, "empty_descriptions",
        f"{thin} of {fetched} postings ({share:.0%}) came without a description of "
        f"{SHORT_DESCRIPTION_CHARS}+ characters ({missing} with none at all)",
        source=source,
        data={"thin": thin, "missing": missing, "fetched": fetched, "share": round(share, 3)},
    )
    return share
