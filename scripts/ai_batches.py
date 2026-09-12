"""Run the AI stages without the API: export prompt batches for subagents, import their verdicts.

    python scripts/ai_batches.py hydrate linkedin [--top 480] [--max-fetch 480]
    python scripts/ai_batches.py export prefilter [--chunk 100] [--max-chars 1500] [--all] [--sample 20]
    python scripts/ai_batches.py import prefilter|rank|refine
    python scripts/ai_batches.py export rank [--window 15] [--top N] [--reset-round] [--chunk 15]
                                            [--max-fetch 60] [--sample 20]
    python scripts/ai_batches.py export refine [--top N] [--max-chars N] [--force]
    python scripts/ai_batches.py boost
    python scripts/ai_batches.py stability export [--top 30] [--chunk 15] [--seed 1] [--max-chars N]
    python scripts/ai_batches.py stability compare

Every action takes ``--profile NAME`` (or ``$JOBSCRAPER_PROFILE``), the same switch the CLI has:
it moves the config, the database and the exports into that candidate's own directories.

Intended sequence for one round:
``hydrate linkedin`` → ``export prefilter`` → (Sonnet subagent) → ``import prefilter`` →
the rank loop → the refine loop → ``boost`` → ``jobscraper report``.

Neither AI stage after the screen is a single step any more:

1. **Rank windows until the stop rule says stop.** Repeat ``export rank`` → (Opus subagent) →
   ``import rank``. Each export is ONE window of the queue and each import records what that
   window did to the effective top N, so both print a line like
   ``stop rule: miss run 12/30, 60/90 budget → continue``. Stop when it says
   ``→ stop (patience)``, ``→ stop (budget)`` or ``→ stop (queue empty)``. There is no fixed
   "best N by screen score" cut: measured 2026-09-11, the screen score has no relation to the
   rank score (Spearman -0.06), so the old cut was close to a random sample of the survivors and
   strong jobs were never looked at.
2. **Refine until the shortlist is fully refined.** Repeat ``export refine`` → (Fable subagent)
   → ``import refine`` until the export says *top N fully refined*. Each round's answers move
   the shortlist (see below), so the round after it asks about whatever moved into the top N.
3. **If the top N boundary dropped in step 2, go back to step 1** for one more window loop (the
   refine pass marks the shortlist down, and a lower boundary is exactly the state in which a
   job the ranker never saw could belong in the top), then repeat step 2. ``--reset-round`` on
   ``export rank`` clears the stop-rule state when a new scrape starts a genuinely new round.

Export writes ``data/exports/ai/<stage>/system.txt`` (the stage's system prompt) and
``chunk-NN.json`` (a JSON list of ``{"job_id", "prompt"}`` built with the pipeline's own
``job_prompt``). A subagent answers each chunk with ``verdicts/chunk-NN.jsonl`` (one JSON object per
job with the ``Screening``/``Ranking`` fields plus ``job_id``). Import validates those against the
schemas and stores them as ``AIVerdict`` rows, so ``jobscraper report`` renders them like API runs.

``refine`` is the odd one out: it is a single request, not a chunked one. ``export refine`` writes
``refine/system.txt`` and one ``refine/batch.json`` — ``{"prompt", "job_ids", "anchors"}`` — over the
shortlist. The subagent answers with one
``refine/verdicts/batch.json``, a JSON object ``{"items": [{job_id, position, score, summary}, ...]}``
matching the ``Refinement`` schema, and ``import refine`` stores it exactly as the API path would.

The shortlist is the best ``ai.refine_top_n`` jobs by *effective* score — the mean of the two
passes, with the refine scale calibrated onto the rank one, which is what the report orders by —
and not the best by rank score. A refined job sits at the average of two samples while an
unrefined neighbour still carries its single, selection-inflated rank score, so the window moves
as soon as answers come in: the jobs this round marked down drop out and their replacements are
what the next export asks about. Before anything has been refined the window is the wider
``ai.refine_first_pass``; ``--top`` overrides both widths. The width is a lower bound: a run of
equal scores at the boundary goes in whole, because the job id that would split it says nothing
about the job.

``export refine`` is incremental: shortlist members that already carry a refine verdict under the
current prompt version are *not* re-scored. They go into the prompt as fixed anchors — one line
each with the score and position they already have — and their ids go into ``anchors``;
``job_ids`` holds only the new ones, which is what the subagent answers for and what
``import refine`` validates against. So a second round pays for the jobs that joined the
shortlist, not for the whole list again. The printed line says how many are new and how many are
anchored; when nothing is new the export is skipped and says *top N fully refined*, which is the
signal to stop repeating the loop. ``--force`` re-scores the whole shortlist against itself
(useful for measuring drift, or after a prompt change).

``export rank`` is refill-aware in the same way and windowed on top of it: the queue is every
screen survivor the rule filter still keeps that has no rank verdict under the current prompt
version, best screen score first, and one export takes the next ``ai.rank_window`` of it
(``ai.rank_top_n`` for the first window of a round, which has nothing to stop on yet).
``--window N`` and ``--top N`` set that width by hand. The round's memory lives in
``data/exports/ai/rank/state.json`` — the ids each imported window ranked, how many of them
entered the effective top N, and the last stop verdict — because export and import are separate
processes. ``--reset-round`` starts that file over.

``export prefilter`` is refill-aware: it skips jobs that already carry a prefilter verdict under
the current prompt version, so after a hydration round only the cleared rows go back to Sonnet
(``--all`` forces the old export-everything behaviour).

``--sample N`` thins whatever the stage was going to export down to every N-th candidate
(positions 0, N, 2N, … of the ordered list, after ``--top``), so one round of the whole loop can be
tried on a handful of postings before committing a few hundred to it.

Descriptions: the scrape leaves LinkedIn rows title-only (per-job fetches are slow and
rate-limited). ``export rank`` tops up the few dozen jobs it is about to rank. ``hydrate
linkedin`` goes wider and earlier: it walks the best ``--top`` prefilter survivors, fetches the
title-only LinkedIn rows among them, and deletes their prefilter *and* rank verdicts so the next
passes re-judge them with the description in hand — a title-only row was screened on its title
alone, so a description can move it up as well as down. Every hydrated job is logged to
``data/exports/ai/hydration.jsonl`` with the position and scores it had before, and ``boost``
reads that log back to show what the descriptions changed and how deep ``--top`` needed to be.

``stability`` measures how much the ranking scores move for reasons that have nothing to do with
the job: the ranker sees a chunk at a time, so a score depends on which other postings shared the
chunk and in which order. ``stability export`` re-exports the best ``--top`` *already ranked* jobs
as two independent runs (``stability/run-a`` and ``stability/run-b``) shuffled with different
seeds, so chunk membership and order differ between them; it never writes to the database and
never fetches descriptions. Two Opus subagents answer them exactly like ``export rank``, and
``stability compare`` reports per-job and summary drift (median/p90 |A-B|, Spearman, top-10
overlap, relevant flips, and the same against the score already stored) to stdout and to
``stability/summary.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobscraper import config  # noqa: E402
from jobscraper.ai.client import refine_verdicts  # noqa: E402
from jobscraper.ai.prompts import (  # noqa: E402
    COMPATIBLE_PROMPT_VERSIONS,
    PROMPT_VERSION,
    job_prompt,
    rank_anchor_block,
    refine_user_prompt,
    system_prompt,
)
from jobscraper.ai.schemas import Ranking, Refinement, Screening  # noqa: E402
from jobscraper.config import load_settings, paths  # noqa: E402
from jobscraper.http import Http  # noqa: E402
from jobscraper.models import AIVerdict, Job  # noqa: E402
from jobscraper.prior import queue_prior  # noqa: E402
from jobscraper.report import (  # noqa: E402
    RANK_TOP_WATCH,
    effective_top,
    entered_top,
    miss_run,
    rank_anchors,
    rank_queue,
    refine_shortlist,
)
from jobscraper.sources import linkedin  # noqa: E402
from jobscraper.store import Store  # noqa: E402

MODEL = {"prefilter": "claude-sonnet-5 (subagent)", "rank": "claude-opus-5 (subagent)",
         "refine": "claude-fable-5-1 (subagent)"}

# Module-level indirection so tests can replace the network call.
fetch_description = linkedin.fetch_description


def _out_dir(stage: str) -> Path:
    d = paths().data / "exports" / "ai" / stage
    (d / "verdicts").mkdir(parents=True, exist_ok=True)
    return d


def _hydration_log() -> Path:
    path = paths().data / "exports" / "ai" / "hydration.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _survivors(store: Store, settings, filters: dict) -> list[AIVerdict]:
    """Prefilter survivors under the current prompt version that the rule filter still keeps.

    Best score first; the job id breaks ties so a position is the same on every run.
    """
    pre = store.verdicts("prefilter", COMPATIBLE_PROMPT_VERSIONS)
    min_score = settings.profile.ai.prefilter_min_score
    alive = {jid for jid, f in filters.items() if f.status != "drop"}
    return sorted((v for v in pre.values() if v.relevant and v.score >= min_score and v.job_id in alive),
                  key=lambda v: (-v.score, v.job_id))


def hydrate_top_linkedin(top: int, max_fetch: int) -> None:
    """Fetch descriptions for the title-only LinkedIn rows among the top ``top`` survivors.

    Those rows were screened from their title alone, so the description can move them up as well
    as down: every job that gets one loses its prefilter *and* rank verdicts and goes back through
    both passes. ``hydration.jsonl`` keeps the before picture (position and scores) so ``boost``
    can report afterwards how deep ``--top`` actually needed to reach.
    """
    settings = load_settings()
    store = Store()
    jobs = {j.id: j for j in store.jobs(seen_within_days=30)}
    survivors = _survivors(store, settings, store.filter_results())
    ranks = store.verdicts("rank", COMPATIBLE_PROMPT_VERSIONS)
    log = _hydration_log()
    http = Http()
    hydrated: list[str] = []
    tried = 0
    try:
        for position, v in enumerate(survivors[:top], start=1):
            if tried >= max_fetch:
                break
            job = jobs.get(v.job_id)
            if job is None or job.source != linkedin.NAME or job.description:
                continue
            tried += 1
            text = fetch_description(http, job.url)
            if not text:  # rate-limited, walled off, gone: leave the verdicts alone
                continue
            job.description = text
            store.upsert_jobs([job])
            rank_before = ranks.get(job.id)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"job_id": job.id, "title": job.title, "source": job.source,
                                     "when": datetime.now(UTC).isoformat(), "position_before": position,
                                     "prefilter_score_before": v.score,
                                     "rank_score_before": rank_before.score if rank_before else None},
                                    ensure_ascii=False) + "\n")
            hydrated.append(job.id)
    finally:
        http.close()
    cleared = store.delete_verdicts(hydrated)
    print(f"hydrated {len(hydrated)}/{tried} linkedin descriptions; {cleared} verdicts cleared")


def boost() -> None:
    """Show what the hydrated descriptions did to the scores, and how deep ``--top`` had to go."""
    log = _hydration_log()
    entries = {}
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                entries[rec["job_id"]] = rec  # a job hydrated twice counts once, latest wins
    if not entries:
        print(f"no hydration log yet ({log}); run `hydrate linkedin` first")
        return
    store = Store()
    pre = store.verdicts("prefilter", COMPATIBLE_PROMPT_VERSIONS)
    ranks = store.verdicts("rank", COMPATIBLE_PROMPT_VERSIONS)
    rows = sorted(entries.values(), key=lambda r: r["position_before"])

    def cell(before, after) -> str:
        if before is None and after is None:
            return " " * 9
        return f"{before if before is not None else '-':>3} → {after if after is not None else '-':<3}"

    print(f"{'title':<40}  {'pos':>4}  {'prefilter':<9}  {'rank':<9}  ranked")
    rose = fell = 0
    for r in rows:
        after = pre[r["job_id"]].score if r["job_id"] in pre else None
        ranked = ranks.get(r["job_id"])
        if after is not None and after > r["prefilter_score_before"]:
            rose += 1
        elif after is not None and after < r["prefilter_score_before"]:
            fell += 1
        print(f"{r['title'][:40]:<40}  {r['position_before']:>4}  "
              f"{cell(r['prefilter_score_before'], after)}  "
              f"{cell(r['rank_score_before'], ranked.score if ranked else None)}  "
              f"{'yes' if ranked else 'no'}")
    made_it = [r["position_before"] for r in rows if r["job_id"] in ranks]
    deepest = max(made_it) if made_it else "none"
    print(f"\nhydrated: {len(rows)}; prefilter score rose: {rose}, fell: {fell}; now ranked: {len(made_it)}")
    print(f"deepest position_before that made the final ranked list: {deepest}")


def hydrate_linkedin(store: Store, todo: list[Job], max_fetch: int) -> None:
    """Fill in missing LinkedIn descriptions for the jobs about to be ranked, in place.

    Best effort: each fetch either returns text or None (rate-limited, walled off, gone), and
    only the successes are written back. ``max_fetch`` bounds how long the stage can take.
    """
    missing = [j for j in todo if j.source == linkedin.NAME and not j.description]
    if not missing or max_fetch <= 0:
        return
    batch = missing[:max_fetch]
    http = Http()
    hydrated = 0
    try:
        for job in batch:
            text = fetch_description(http, job.url)
            if not text:
                continue
            job.description = text
            store.upsert_jobs([job])  # UPDATEs the stored row's data blob, description included
            hydrated += 1
    finally:
        http.close()
    print(f"hydrated {hydrated}/{len(batch)} linkedin descriptions")


def _write_chunks(stage: str, todo: list[Job], filters: dict, chunk: int, max_chars: int,
                  profile, candidates: int, sample: int, note: str = "") -> None:
    """Write ``system.txt`` and the ``chunk-NN.json`` batches for one export, and report."""
    out = _out_dir(stage)
    for old in out.glob("chunk-*.json"):
        old.unlink()
    (out / "system.txt").write_text(system_prompt(stage, profile), encoding="utf-8")
    n = 0  # nothing to do is a normal state: keep the counter defined for the summary line
    for n, start in enumerate(range(0, len(todo), chunk), start=1):
        batch = [{"job_id": j.id, "prompt": job_prompt(j, filters.get(j.id), max_chars)} for j in todo[start : start + chunk]]
        (out / f"chunk-{n:02d}.json").write_text(json.dumps(batch, ensure_ascii=False, indent=0), encoding="utf-8")
    sampled = f" (1 in {sample} of {candidates} candidates)" if sample > 1 else f" of {candidates} candidates"
    print(f"{stage}: {len(todo)} jobs{sampled}{note} → {n} chunks of ≤{chunk} in {out}")


def export(stage: str, chunk: int, max_chars: int, export_all: bool = False, sample: int = 1) -> None:
    """The prefilter export: every rule-filter survivor that has not been screened yet."""
    settings = load_settings()
    store = Store()
    jobs = {j.id: j for j in store.jobs(seen_within_days=30)}
    filters = store.filter_results()
    # Refill semantics: rule-filter survivors that don't already carry a prefilter verdict under
    # the current prompt version, so a hydration round only re-screens the jobs it cleared.
    already = set() if export_all else set(store.verdicts("prefilter", COMPATIBLE_PROMPT_VERSIONS))
    todo = [j for j in jobs.values()
            if filters.get(j.id) and filters[j.id].status in ("keep", "review") and j.id not in already]
    todo.sort(key=lambda j: (j.source, j.title.lower()))
    candidates = len(todo)
    if sample > 1:
        todo = todo[::sample]  # positions 0, N, 2N, ... of the stage's ordered candidate list
    _write_chunks(stage, todo, filters, chunk, max_chars, settings.profile, candidates, sample)


# ------------------------------------------------------- the rank round's stop rule
# The screen score does not sort (measured 2026-09-11: Spearman -0.06 against the rank score),
# so there is no "best N by screen score" worth cutting at. The round reads the queue one window
# at a time and stops when the effective top stops gaining entrants. Export and import are
# separate processes, so the round's memory lives in a small JSON file next to the exports.


def _rank_state_path() -> Path:
    return _out_dir("rank") / "state.json"


def _read_rank_state() -> dict:
    """This round's memory: the windows imported so far, and the window awaiting an answer."""
    path = _rank_state_path()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_rank_state(state: dict) -> None:
    _rank_state_path().write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def _new_rank_state() -> dict:
    return {"started": datetime.now(UTC).isoformat(), "windows": [], "exported": [], "top_before": []}


def _rank_stop(state: dict, queue_left: int, ai) -> tuple[str, int, int]:
    """``(reason, miss run, newly ranked)`` for the round recorded in ``state``.

    ``reason`` is ``continue`` or the name of the rule that ended the round: the queue running
    out, ``rank_patience`` rankings in a row that entered nothing, or ``rank_budget`` spent.
    """
    windows = [(len(w.get("ranked") or []), w.get("entered") or 0) for w in state.get("windows") or []]
    newly = sum(ranked for ranked, _entered in windows)
    misses = miss_run(windows)
    if not queue_left:
        return "queue empty", misses, newly
    if ai.rank_patience and misses >= ai.rank_patience:
        return "patience", misses, newly
    if ai.rank_budget and newly >= ai.rank_budget:
        return "budget", misses, newly
    return "continue", misses, newly


def _print_rank_stop(reason: str, misses: int, newly: int, ai) -> None:
    """The one line the owner reads to decide whether to run another export/import round."""
    budget = f"{newly}/{ai.rank_budget} budget" if ai.rank_budget else f"{newly} ranked this round"
    verdict = "continue" if reason == "continue" else f"stop ({reason})"
    print(f"stop rule: miss run {misses}/{ai.rank_patience}, {budget} → {verdict}")


def _rank_top_ids(store: Store, settings, filters: dict) -> list[str]:
    """The effective top ``ai.refine_top_n`` as it stands in the database right now."""
    ids, _edge = effective_top(store.jobs(seen_within_days=30), filters,
                               store.verdicts("rank", COMPATIBLE_PROMPT_VERSIONS),
                               store.verdicts("refine", COMPATIBLE_PROMPT_VERSIONS),
                               top=settings.profile.ai.refine_top_n or RANK_TOP_WATCH)
    return ids


def export_rank(chunk: int, max_chars: int, top: int | None, window: int | None, max_fetch: int,
                sample: int, reset_round: bool) -> None:
    """Export ONE rank window from the queue, and say where the round's stop rule stands.

    The queue is every screen survivor the rule filter still keeps that has no rank verdict yet,
    ordered by the lexical prior (:mod:`jobscraper.prior`, ``ai.rank_order``) rather than by the
    screen score, which does not sort. A window is ``ai.rank_window`` jobs — the first one of a round
    ``ai.rank_top_n``, since nothing has been ranked to stop on — and ``--window``/``--top``
    override that. ``import rank`` records what the window did to the effective top N, so the
    next export can tell whether the round is still paying for itself.
    """
    settings = load_settings()
    ai = settings.profile.ai
    store = Store()
    filters = store.filter_results()
    jobs = store.jobs(seen_within_days=30)
    ranked = store.verdicts("rank", COMPATIBLE_PROMPT_VERSIONS)
    queue = rank_queue(jobs, filters,
                       store.verdicts("prefilter", COMPATIBLE_PROMPT_VERSIONS), ranked,
                       min_score=ai.prefilter_min_score,
                       prior=queue_prior(jobs, settings.profile))
    state = _read_rank_state()
    if reset_round or not state:
        state = _new_rank_state()
    done = len(state.get("windows") or [])
    reason, misses, newly = _rank_stop(state, len(queue), ai)
    if reason in ("patience", "budget"):
        for old in _out_dir("rank").glob("chunk-*.json"):
            old.unlink()  # the round is over: leave nothing behind that looks like a batch to answer
        _print_rank_stop(reason, misses, newly, ai)
        print(f"rank: nothing exported — {newly} jobs ranked in {done} windows this round and the "
              "stop rule is met; `export rank --reset-round` starts a new round")
        return

    width = window or top or (ai.rank_window if ranked else ai.rank_top_n)
    if ai.rank_budget and not top:
        width = max(1, min(width, ai.rank_budget - newly))  # the last window is only what the round can still pay for
    todo = queue[:width]
    candidates = len(todo)
    if sample > 1:
        todo = todo[::sample]  # positions 0, N, 2N, ... of the window
    # After sampling: a job that is not going to be exported is not worth a description fetch.
    hydrate_linkedin(store, todo, max_fetch)
    # A round is many windows now, and the previous window's answers have already been imported:
    # leaving them behind would have the next import read them again as unknown job ids.
    for old in (_out_dir("rank") / "verdicts").glob("chunk-*.jsonl"):
        old.unlink()
    # Every window of the round is graded against the same fixed reference scores, so a slice of
    # weak postings cannot talk itself into an 80 and a strong slice is not flattened to fill the
    # range. The agents read this file after system.txt and never answer for the postings in it.
    anchors = rank_anchors(jobs, ranked)
    anchors_file = _out_dir("rank") / "anchors.txt"
    if anchors:
        anchors_file.write_text(rank_anchor_block(anchors), encoding="utf-8")
    elif anchors_file.exists():
        anchors_file.unlink()  # a previous round's scale is not this profile's scale
    anchor_note = (f" + anchors.txt ({len(anchors)} reference scores)" if anchors
                   else " + no reference scores yet")
    _write_chunks("rank", todo, filters, chunk, max_chars, settings.profile, candidates, sample,
                  note=f" (window {done + 1} of this round, {len(queue)} in the queue)"
                       f"{anchor_note}")
    state["exported"] = [j.id for j in todo]
    state["top_before"] = _rank_top_ids(store, settings, filters)
    _write_rank_state(state)
    _print_rank_stop(reason, misses, newly, ai)


def _record_rank_window(store: Store, imported: list[str]) -> None:
    """Add the window just imported to the round's state, with what it did to the top N."""
    settings = load_settings()
    state = _read_rank_state() or _new_rank_state()
    filters = store.filter_results()
    after = _rank_top_ids(store, settings, filters)
    state.setdefault("windows", []).append(
        {"ranked": imported, "entered": entered_top(set(state.get("top_before") or []), after, imported)})
    state["exported"], state["top_before"] = [], after
    _write_rank_state(state)
    jobs = store.jobs(seen_within_days=30)
    queue = rank_queue(jobs, filters,
                       store.verdicts("prefilter", COMPATIBLE_PROMPT_VERSIONS),
                       store.verdicts("rank", COMPATIBLE_PROMPT_VERSIONS),
                       min_score=settings.profile.ai.prefilter_min_score,
                       prior=queue_prior(jobs, settings.profile))
    reason, misses, newly = _rank_stop(state, len(queue), settings.profile.ai)
    state["stop_reason"] = reason
    _write_rank_state(state)
    _print_rank_stop(reason, misses, newly, settings.profile.ai)


def export_refine(top: int | None, max_chars: int, force: bool = False) -> None:
    """Write the shortlist as ONE prompt: the refine pass is a single request, not chunks.

    The shortlist is the best ``ai.refine_top_n`` jobs by *effective* score — the mean of the two
    passes that the report orders by — so a job the refine pass marked down leaves it and the
    unrefined neighbour that took its place is what the next export asks about. Before anything
    has been refined the window is the wider ``ai.refine_first_pass``. ``--top`` overrides both.

    Incremental, like the API path: a shortlisted job that already carries a refine verdict under
    the current prompt version is not re-scored, it goes into the prompt as a fixed anchor (one
    line: title, company, its score and position) and its id lands in ``anchors``. ``job_ids``
    holds the new ones — the ones the subagent has to answer for. When nothing is new the top N
    is fully refined and the export is skipped; ``--force`` re-scores the whole shortlist against
    itself, e.g. to measure drift.
    """
    settings = load_settings()
    store = Store()
    filters = store.filter_results()
    n = top or settings.profile.ai.refine_top_n
    placed = store.verdicts("refine", COMPATIBLE_PROMPT_VERSIONS)
    shortlist, _offset = refine_shortlist(store.jobs(seen_within_days=30), filters,
                                          store.verdicts("rank", COMPATIBLE_PROMPT_VERSIONS), placed,
                                          top=n, first_pass=top or settings.profile.ai.refine_first_pass)
    refined = {} if force else placed
    todo = [j for j in shortlist if j.id not in refined]
    anchors = [(j, refined[j.id]) for j in shortlist if j.id in refined]
    anchors.sort(key=lambda pair: (pair[1].position is None, pair[1].position or 0, -pair[1].score))
    if shortlist and not todo:
        print(f"refine: all {len(shortlist)} shortlisted jobs already carry a refine verdict and the "
              f"shortlist has not changed; nothing new to compare — top {n} fully refined "
              "(--force re-runs it)")
        return
    out = _out_dir("refine")
    (out / "system.txt").write_text(system_prompt("refine", settings.profile), encoding="utf-8")
    batch = {"prompt": refine_user_prompt(todo, filters, max_chars, anchors=anchors or None),
             "job_ids": [j.id for j in todo], "anchors": [j.id for j, _ in anchors]}
    (out / "batch.json").write_text(json.dumps(batch, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"refine: {len(todo)} new jobs against {len(anchors)} already placed "
          f"(effective top {n}, plus anything tied with its last member) → {out / 'batch.json'}")


def import_refine() -> None:
    """Store one subagent ``Refinement`` answer, with the same tolerance as the API path."""
    out = _out_dir("refine")
    batch_file = out / "batch.json"
    if not batch_file.is_file():
        print(f"no export to import against ({batch_file}); run `export refine` first")
        return
    expected = json.loads(batch_file.read_text(encoding="utf-8"))["job_ids"]
    answer = out / "verdicts" / "batch.json"
    if not answer.is_file():
        print(f"no answer yet ({answer}); the subagent writes the Refinement object there")
        return
    try:
        parsed = Refinement.model_validate_json(answer.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a hand-written answer, report and stop
        print(f"  {answer.name}: not a Refinement object ({exc})")
        return
    store = Store()
    verdicts = refine_verdicts(parsed.items, expected, MODEL["refine"])
    for v in verdicts:
        store.save_verdict(v)
    print(f"refine: imported {len(verdicts)} verdicts of {len(expected)} exported")


def read_verdict_lines(path: Path, schema) -> Iterator[tuple[str | None, object, str | None]]:
    """Yield ``(job_id, parsed, error)`` for every verdict line of a subagent's ``.jsonl`` answer.

    Tolerant on purpose, because a subagent sometimes wraps its answer in a JSON array or leaves a
    truncated last line behind: bare brackets and trailing commas are dropped, and a line that will
    not parse or validate comes back as an ``error`` string instead of raising. ``error`` is None
    exactly on the lines that produced a verdict.
    """
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().rstrip(",")
        if not line or line in ("[", "]"):
            continue
        try:
            rec = json.loads(line)
            job_id = str(rec.pop("job_id"))
            parsed = schema(**{k: v for k, v in rec.items() if k in schema.model_fields})
        except Exception as exc:  # noqa: BLE001 - report and continue
            yield None, None, f"bad line ({exc}): {line[:100]}"
            continue
        yield job_id, parsed, None


def import_verdicts(stage: str) -> None:
    schema = Screening if stage == "prefilter" else Ranking
    out = _out_dir(stage)
    expected: dict[str, str] = {}
    for f in sorted(out.glob("chunk-*.json")):
        for rec in json.loads(f.read_text(encoding="utf-8")):
            expected[rec["job_id"]] = f.stem
    store = Store()
    seen: set[str] = set()
    bad = 0
    for f in sorted((out / "verdicts").glob("chunk-*.jsonl")):
        for job_id, parsed, error in read_verdict_lines(f, schema):
            if error:
                bad += 1
                print(f"  {f.name}: {error}")
                continue
            if job_id not in expected:
                bad += 1
                print(f"  {f.name}: unknown job_id {job_id}")
                continue
            if job_id in seen:
                continue
            seen.add(job_id)
            store.save_verdict(AIVerdict(job_id=job_id, stage=stage, model=MODEL[stage], prompt_version=PROMPT_VERSION,
                                         relevant=parsed.relevant, score=parsed.score, language_ok=parsed.language_ok,
                                         seniority_ok=parsed.seniority_ok, location_ok=parsed.location_ok,
                                         summary=parsed.summary, concerns=parsed.concerns,
                                         why_apply=getattr(parsed, "why_apply", [])))
    missing = sorted(set(expected) - seen)
    by_chunk: dict[str, int] = {}
    for job_id in missing:
        by_chunk[expected[job_id]] = by_chunk.get(expected[job_id], 0) + 1
    print(f"{stage}: imported {len(seen)} verdicts, {bad} bad lines, {len(missing)} missing" + (f" {by_chunk}" if by_chunk else ""))
    if stage == "rank":
        # The round's memory: what this window did to the effective top N, and whether to go on.
        _record_rank_window(store, [job_id for job_id in expected if job_id in seen])


# --------------------------------------------------------------------------- stability
# The ranker grades a chunk at a time, so a job's score depends on the company it kept in that
# chunk. Asking for the same jobs twice, shuffled differently, puts a number on that noise.

STABILITY_RUNS = ("run-a", "run-b")


def _stability_dir() -> Path:
    d = paths().data / "exports" / "ai" / "stability"
    d.mkdir(parents=True, exist_ok=True)
    return d


def stability_export(top: int, chunk: int, max_chars: int, seed: int) -> None:
    """Re-export the best ``top`` already-ranked jobs as two differently shuffled runs.

    Read-only on the database, and no LinkedIn hydration: every job goes back to the ranker with
    exactly the description it had when it earned the score we are comparing against. Only the
    order changes, so whatever the second answer differs by is noise, not new information.
    """
    settings = load_settings()
    store = Store()
    jobs = {j.id: j for j in store.jobs(seen_within_days=30)}
    filters = store.filter_results()
    scored = sorted((v for v in store.verdicts("rank", COMPATIBLE_PROMPT_VERSIONS).values() if v.job_id in jobs),
                    key=lambda v: (-v.score, v.job_id))[:top]
    todo = [jobs[v.job_id] for v in scored]
    root = _stability_dir()
    n = 0  # nothing to compare is a normal state before the first `import rank`
    for offset, name in enumerate(STABILITY_RUNS):
        out = root / name
        (out / "verdicts").mkdir(parents=True, exist_ok=True)
        for old in out.glob("chunk-*.json"):
            old.unlink()
        (out / "system.txt").write_text(system_prompt("rank", settings.profile), encoding="utf-8")
        order = list(todo)
        random.Random(seed + offset).shuffle(order)
        for n, start in enumerate(range(0, len(order), chunk), start=1):
            batch = [{"job_id": j.id, "prompt": job_prompt(j, filters.get(j.id), max_chars)}
                     for j in order[start : start + chunk]]
            (out / f"chunk-{n:02d}.json").write_text(json.dumps(batch, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"stability: {len(todo)} jobs → {n} chunks of ≤{chunk} in each of "
          f"{' and '.join(STABILITY_RUNS)} under {root} (seeds {seed}, {seed + 1})")


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile (``q`` between 0 and 1) of a non-empty list."""
    xs = sorted(values)
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _average_ranks(values: list[float]) -> list[float]:
    """1-based ranks of ``values``, tied values sharing the average of the places they occupy."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman's rho: the Pearson correlation of the average ranks. None where it is undefined."""
    if len(xs) < 2:
        return None
    rx, ry = _average_ranks(xs), _average_ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx)
    dy = sum((b - my) ** 2 for b in ry)
    if dx == 0 or dy == 0:  # one run gave every job the same score: no ordering to correlate
        return None
    return num / (dx * dy) ** 0.5


def _fmt(value: float | None, digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _drift_line(label: str, xs: list[float], ys: list[float]) -> str:
    """One summary line of how far ``xs`` sits from ``ys``, the two aligned by position."""
    if not xs:
        return f"- {label}: no overlap"
    diffs = [x - y for x, y in zip(xs, ys)]
    absd = [abs(d) for d in diffs]
    return (f"- {label}: n={len(xs)}, median |d| {_percentile(absd, 0.5):.2f}, "
            f"p90 |d| {_percentile(absd, 0.9):.2f}, mean signed {sum(diffs) / len(diffs):+.2f}, "
            f"Spearman {_fmt(_spearman(xs, ys))}")


def _top_set(scores: dict[str, int], n: int) -> set[str]:
    """The ``n`` best-scoring job ids; the job id breaks ties so the set is the same every run."""
    return {jid for jid, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:n]}


def _jaccard(x: set[str], y: set[str]) -> float:
    return len(x & y) / len(x | y) if x or y else 0.0


def _read_run(run: Path) -> tuple[dict[str, Ranking], int]:
    """Every ranking a stability run answered (first line per job wins) and its bad-line count."""
    out: dict[str, Ranking] = {}
    bad = 0
    for f in sorted((run / "verdicts").glob("chunk-*.jsonl")):
        for job_id, parsed, error in read_verdict_lines(f, Ranking):
            if error:
                bad += 1
                print(f"  {run.name}/{f.name}: {error}")
                continue
            out.setdefault(job_id, parsed)
    return out, bad


def _exported_ids(run: Path) -> set[str]:
    return {rec["job_id"] for f in sorted(run.glob("chunk-*.json"))
            for rec in json.loads(f.read_text(encoding="utf-8"))}


def stability_compare(top_n: int = 10) -> None:
    """Turn the two answer sets into a drift report, on stdout and in ``stability/summary.md``."""
    root = _stability_dir()
    a, bad_a = _read_run(root / "run-a")
    b, bad_b = _read_run(root / "run-b")
    store = Store()
    titles = {j.id: j.title for j in store.jobs()}
    original = {jid: v.score for jid, v in store.verdicts("rank", COMPATIBLE_PROMPT_VERSIONS).items()}
    both = sorted(set(a) & set(b), key=lambda jid: (-original.get(jid, -1), jid))

    md = ["# Ranking stability", "",
          "The same jobs, scored twice by the ranker in a different order and chunking. Whatever",
          "the two answers differ by is chunk noise, not information about the job.", ""]
    if bad_a or bad_b:
        md += [f"Unparsable verdict lines: run-a {bad_a}, run-b {bad_b}.", ""]
    # The refine pass saw all these jobs in one request, so its score is the drift-free reference
    # to read the two chunked runs against; the column only appears once that pass has run.
    refined = {jid: v.score for jid, v in store.verdicts("refine", COMPATIBLE_PROMPT_VERSIONS).items()}
    head, rule = (" refine |", " ---: |") if refined else ("", "")
    md += [f"| job | title | orig |{head} A | B | \\|A-B\\| | flip |",
           f"| --- | --- | ---: |{rule} ---: | ---: | ---: | --- |"]
    for jid in both:
        va, vb = a[jid], b[jid]
        orig = original.get(jid)
        cell = f" {refined.get(jid, '-')} |" if refined else ""
        md.append(f"| {jid[:12]} | {titles.get(jid, '?')[:40]} | {'-' if orig is None else orig} |"
                  f"{cell} {va.score} | {vb.score} | {abs(va.score - vb.score)} | "
                  f"{'relevant flip' if va.relevant != vb.relevant else ''} |")
    md += ["", "## Summary", ""]
    if not both:
        md += ["No jobs are present in both runs; nothing to compare.", ""]
    else:
        sa = [float(a[jid].score) for jid in both]
        sb = [float(b[jid].score) for jid in both]
        diffs = [x - y for x, y in zip(sa, sb)]
        absd = [abs(d) for d in diffs]
        with_orig = [jid for jid in both if jid in original]
        md += [
            f"- jobs in both runs: {len(both)}",
            f"- median |A-B|: {_percentile(absd, 0.5):.2f}",
            f"- 90th percentile |A-B|: {_percentile(absd, 0.9):.2f}",
            f"- mean signed (A-B): {sum(diffs) / len(diffs):.2f}",
            f"- Spearman rank correlation A vs B: {_fmt(_spearman(sa, sb))}",
            f"- top-{top_n} overlap (Jaccard): "
            f"{_jaccard(_top_set({jid: a[jid].score for jid in both}, top_n), _top_set({jid: b[jid].score for jid in both}, top_n)):.3f}",
            f"- relevant flips: {sum(1 for jid in both if a[jid].relevant != b[jid].relevant)}",
            "",
            "Drift against the score already stored for each job:",
            "",
            _drift_line("A vs original", [float(a[jid].score) for jid in with_orig],
                        [float(original[jid]) for jid in with_orig]),
            _drift_line("B vs original", [float(b[jid].score) for jid in with_orig],
                        [float(original[jid]) for jid in with_orig]),
            "",
        ]
    expected = _exported_ids(root / "run-a") or (set(a) | set(b))
    md += ["## Missing", ""]
    for name, answered in zip(STABILITY_RUNS, (a, b)):
        gone = sorted(expected - set(answered))
        md.append(f"- missing from {name}: {len(gone)}"
                  + (f" ({', '.join(jid[:12] for jid in gone)})" if gone else ""))
    text = "\n".join(md).rstrip() + "\n"
    print(text)
    (root / "summary.md").write_text(text, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["export", "import", "hydrate", "boost", "stability"])
    ap.add_argument("stage", nargs="?",
                    choices=["prefilter", "rank", "refine", "linkedin", "export", "compare"],
                    default=None,
                    help="stage for export/import, 'linkedin' for hydrate, 'export'/'compare' for"
                         " stability; ignored by boost")
    ap.add_argument("--chunk", type=int, default=None)
    ap.add_argument("--max-chars", type=int, default=None)
    ap.add_argument("--all", action="store_true",
                    help="export prefilter only: export every survivor, not just the unscreened ones")
    ap.add_argument("--force", action="store_true",
                    help="export refine: re-score every shortlisted job instead of anchoring the "
                         "ones that already carry a refine verdict")
    ap.add_argument("--top", type=int, default=None,
                    help="export rank: the next N of the queue as one window (default: the stop "
                         "rule's window width); export refine: 20; hydrate linkedin: 480")
    ap.add_argument("--window", type=int, default=None,
                    help="export rank: jobs in this window, overriding ai.rank_window")
    ap.add_argument("--reset-round", action="store_true",
                    help="export rank: forget the current round's stop-rule state and start over "
                         "(a new scrape, or after changing the rules)")
    ap.add_argument("--max-fetch", type=int, default=None,
                    help="how many missing LinkedIn descriptions to fetch (0 disables); rank: 60, hydrate: 480")
    ap.add_argument("--seed", type=int, default=1,
                    help="stability export only: run-a shuffles with this seed, run-b with seed+1")
    ap.add_argument("--sample", type=int, default=1, metavar="N",
                    help="export only: keep every N-th candidate (positions 0, N, 2N, ... after --top); 1 = all")
    ap.add_argument("--profile", default=os.environ.get("JOBSCRAPER_PROFILE") or None, metavar="NAME",
                    help="candidate profile: config/profiles/NAME, data/profiles/NAME (default $JOBSCRAPER_PROFILE)")
    a = ap.parse_args()
    if a.sample < 1:
        ap.error("--sample needs a positive integer")
    # Before anything opens a Store or reads settings: every directory below is profile-relative.
    config.use_profile(a.profile)
    if a.action == "boost":
        boost()
        return
    if a.action == "stability":
        if a.stage == "export":
            stability_export(a.top or 30, a.chunk or 15, a.max_chars or 6000, a.seed)
        elif a.stage == "compare":
            stability_compare()
        else:
            ap.error("stability needs a sub-action: export or compare")
        return
    if a.action == "hydrate":
        if a.stage not in (None, "linkedin"):
            ap.error("hydrate only knows the 'linkedin' stage")
        hydrate_top_linkedin(a.top or 480, 480 if a.max_fetch is None else a.max_fetch)
        return
    if a.stage not in ("prefilter", "rank", "refine"):
        ap.error(f"{a.action} needs a stage: prefilter, rank or refine")
    if a.stage == "refine":
        # One request over the whole shortlist: no chunking, no sampling, no hydration.
        if a.action == "export":
            export_refine(a.top, a.max_chars or 6000, force=a.force)
        else:
            import_refine()
        return
    if a.action == "export":
        chunk = a.chunk or (100 if a.stage == "prefilter" else 15)
        max_chars = a.max_chars or (1500 if a.stage == "prefilter" else 6000)
        if a.stage == "rank":
            export_rank(chunk, max_chars, a.top, a.window,
                        60 if a.max_fetch is None else a.max_fetch, a.sample, a.reset_round)
        else:
            export(a.stage, chunk, max_chars, a.all, a.sample)
    else:
        import_verdicts(a.stage)


if __name__ == "__main__":
    main()
