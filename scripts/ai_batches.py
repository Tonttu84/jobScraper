"""Run the AI stages without the API: export prompt batches for subagents, import their verdicts.

    python scripts/ai_batches.py hydrate linkedin [--top 120] [--max-fetch 120]
    python scripts/ai_batches.py export prefilter [--chunk 100] [--max-chars 1500] [--all] [--sample 20]
    python scripts/ai_batches.py import prefilter|rank
    python scripts/ai_batches.py export rank [--top 60] [--chunk 15] [--max-fetch 60] [--sample 20]
    python scripts/ai_batches.py boost

Every action takes ``--profile NAME`` (or ``$JOBSCRAPER_PROFILE``), the same switch the CLI has:
it moves the config, the database and the exports into that candidate's own directories.

Intended sequence for one round:
``hydrate linkedin`` → ``export prefilter`` → (Sonnet subagent) → ``import prefilter`` →
``export rank`` → (Opus subagent) → ``import rank`` → ``boost`` → ``jobscraper report``.

Export writes ``data/exports/ai/<stage>/system.txt`` (the stage's system prompt) and
``chunk-NN.json`` (a JSON list of ``{"job_id", "prompt"}`` built with the pipeline's own
``job_prompt``). A subagent answers each chunk with ``verdicts/chunk-NN.jsonl`` (one JSON object per
job with the ``Screening``/``Ranking`` fields plus ``job_id``). Import validates those against the
schemas and stores them as ``AIVerdict`` rows, so ``jobscraper report`` renders them like API runs.

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
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobscraper import config  # noqa: E402
from jobscraper.ai.prompts import PROMPT_VERSION, job_prompt, system_prompt  # noqa: E402
from jobscraper.ai.schemas import Ranking, Screening  # noqa: E402
from jobscraper.config import load_settings, paths  # noqa: E402
from jobscraper.http import Http  # noqa: E402
from jobscraper.models import AIVerdict, Job  # noqa: E402
from jobscraper.sources import linkedin  # noqa: E402
from jobscraper.store import Store  # noqa: E402

MODEL = {"prefilter": "claude-sonnet-5 (subagent)", "rank": "claude-opus-5 (subagent)"}

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
    pre = store.verdicts("prefilter", PROMPT_VERSION)
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
    ranks = store.verdicts("rank", PROMPT_VERSION)
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
    pre = store.verdicts("prefilter", PROMPT_VERSION)
    ranks = store.verdicts("rank", PROMPT_VERSION)
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


def export(stage: str, chunk: int, max_chars: int, top: int, max_fetch: int = 0, export_all: bool = False,
           sample: int = 1) -> None:
    settings = load_settings()
    store = Store()
    jobs = {j.id: j for j in store.jobs(seen_within_days=30)}
    filters = store.filter_results()
    if stage == "prefilter":
        # Refill semantics: rule-filter survivors that don't already carry a prefilter verdict under
        # the current prompt version, so a hydration round only re-screens the jobs it cleared.
        already = set() if export_all else set(store.verdicts("prefilter", PROMPT_VERSION))
        todo = [j for j in jobs.values()
                if filters.get(j.id) and filters[j.id].status in ("keep", "review") and j.id not in already]
        todo.sort(key=lambda j: (j.source, j.title.lower()))
    else:
        # Refill semantics: the best `top` prefilter survivors that the rule filter still keeps, minus
        # the ones that already carry a rank verdict under the current prompt version.
        already = set(store.verdicts("rank", PROMPT_VERSION))
        ranked = _survivors(store, settings, filters)
        todo = [jobs[v.job_id] for v in ranked[:top] if v.job_id in jobs and v.job_id not in already]
    candidates = len(todo)
    if sample > 1:
        todo = todo[::sample]  # positions 0, N, 2N, ... of the stage's ordered candidate list
    if stage == "rank":
        # After sampling: a job that is not going to be exported is not worth a description fetch.
        hydrate_linkedin(store, todo, max_fetch)
    out = _out_dir(stage)
    for old in out.glob("chunk-*.json"):
        old.unlink()
    (out / "system.txt").write_text(system_prompt(stage, settings.profile), encoding="utf-8")
    n = 0  # nothing to do is a normal state: keep the counter defined for the summary line
    for n, start in enumerate(range(0, len(todo), chunk), start=1):
        batch = [{"job_id": j.id, "prompt": job_prompt(j, filters.get(j.id), max_chars)} for j in todo[start : start + chunk]]
        (out / f"chunk-{n:02d}.json").write_text(json.dumps(batch, ensure_ascii=False, indent=0), encoding="utf-8")
    sampled = f" (1 in {sample} of {candidates} candidates)" if sample > 1 else f" of {candidates} candidates"
    print(f"{stage}: {len(todo)} jobs{sampled} → {n} chunks of ≤{chunk} in {out}")


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
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip().rstrip(",")
            if not line or line in ("[", "]"):
                continue
            try:
                rec = json.loads(line)
                job_id = str(rec.pop("job_id"))
                parsed = schema(**{k: v for k, v in rec.items() if k in schema.model_fields})
            except Exception as exc:  # noqa: BLE001 - report and continue
                bad += 1
                print(f"  {f.name}: bad line ({exc}): {line[:100]}")
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["export", "import", "hydrate", "boost"])
    ap.add_argument("stage", nargs="?", choices=["prefilter", "rank", "linkedin"], default=None,
                    help="stage for export/import, 'linkedin' for hydrate; ignored by boost")
    ap.add_argument("--chunk", type=int, default=None)
    ap.add_argument("--max-chars", type=int, default=None)
    ap.add_argument("--all", action="store_true",
                    help="export prefilter only: export every survivor, not just the unscreened ones")
    ap.add_argument("--top", type=int, default=None, help="export rank: 60; hydrate linkedin: 120")
    ap.add_argument("--max-fetch", type=int, default=None,
                    help="how many missing LinkedIn descriptions to fetch (0 disables); rank: 60, hydrate: 120")
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
    if a.action == "hydrate":
        if a.stage not in (None, "linkedin"):
            ap.error("hydrate only knows the 'linkedin' stage")
        hydrate_top_linkedin(a.top or 120, 120 if a.max_fetch is None else a.max_fetch)
        return
    if a.stage not in ("prefilter", "rank"):
        ap.error(f"{a.action} needs a stage: prefilter or rank")
    if a.action == "export":
        chunk = a.chunk or (100 if a.stage == "prefilter" else 15)
        max_chars = a.max_chars or (1500 if a.stage == "prefilter" else 6000)
        export(a.stage, chunk, max_chars, a.top or 60, 60 if a.max_fetch is None else a.max_fetch, a.all,
               a.sample)
    else:
        import_verdicts(a.stage)


if __name__ == "__main__":
    main()
