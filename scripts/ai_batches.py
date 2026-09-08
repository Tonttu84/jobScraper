"""Run the AI stages without the API: export prompt batches for subagents, import their verdicts.

    python scripts/ai_batches.py export prefilter [--chunk 100] [--max-chars 1500]
    python scripts/ai_batches.py export rank [--top 60] [--chunk 15] [--max-fetch 60]
    python scripts/ai_batches.py import prefilter|rank

Export writes ``data/exports/ai/<stage>/system.txt`` (the stage's system prompt) and
``chunk-NN.json`` (a JSON list of ``{"job_id", "prompt"}`` built with the pipeline's own
``job_prompt``). A subagent answers each chunk with ``verdicts/chunk-NN.jsonl`` (one JSON object per
job with the ``Screening``/``Ranking`` fields plus ``job_id``). Import validates those against the
schemas and stores them as ``AIVerdict`` rows, so ``jobscraper report`` renders them like API runs.

``export rank`` also tops up missing LinkedIn descriptions: the scrape leaves those rows
title-only (per-job fetches are slow and rate-limited), but ranking only ever looks at a few
dozen jobs, so fetching just those is cheap and materially improves the verdicts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobscraper.ai.prompts import PROMPT_VERSION, job_prompt, system_prompt  # noqa: E402
from jobscraper.ai.schemas import Ranking, Screening  # noqa: E402
from jobscraper.config import DATA_DIR, load_settings  # noqa: E402
from jobscraper.http import Http  # noqa: E402
from jobscraper.models import AIVerdict, Job  # noqa: E402
from jobscraper.sources import linkedin  # noqa: E402
from jobscraper.store import Store  # noqa: E402

MODEL = {"prefilter": "claude-sonnet-5 (subagent)", "rank": "claude-opus-5 (subagent)"}

# Module-level indirection so tests can replace the network call.
fetch_description = linkedin.fetch_description


def _out_dir(stage: str) -> Path:
    d = DATA_DIR / "exports" / "ai" / stage
    (d / "verdicts").mkdir(parents=True, exist_ok=True)
    return d


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


def export(stage: str, chunk: int, max_chars: int, top: int, max_fetch: int = 0) -> None:
    settings = load_settings()
    store = Store()
    jobs = {j.id: j for j in store.jobs(seen_within_days=30)}
    filters = store.filter_results()
    if stage == "prefilter":
        todo = [j for j in jobs.values() if filters.get(j.id) and filters[j.id].status in ("keep", "review")]
        todo.sort(key=lambda j: (j.source, j.title.lower()))
    else:
        pre = store.verdicts("prefilter", PROMPT_VERSION)
        min_score = settings.profile.ai.prefilter_min_score
        # Refill semantics: the best `top` prefilter survivors that the rule filter still keeps, minus
        # the ones that already carry a rank verdict under the current prompt version.
        alive = {jid for jid, f in filters.items() if f.status != "drop"}
        already = set(store.verdicts("rank", PROMPT_VERSION))
        ranked = sorted((v for v in pre.values() if v.relevant and v.score >= min_score and v.job_id in alive),
                        key=lambda v: -v.score)
        todo = [jobs[v.job_id] for v in ranked[:top] if v.job_id in jobs and v.job_id not in already]
        hydrate_linkedin(store, todo, max_fetch)
    out = _out_dir(stage)
    for old in out.glob("chunk-*.json"):
        old.unlink()
    (out / "system.txt").write_text(system_prompt(stage, settings.profile), encoding="utf-8")
    n = 0  # nothing to do is a normal state: keep the counter defined for the summary line
    for n, start in enumerate(range(0, len(todo), chunk), start=1):
        batch = [{"job_id": j.id, "prompt": job_prompt(j, filters.get(j.id), max_chars)} for j in todo[start : start + chunk]]
        (out / f"chunk-{n:02d}.json").write_text(json.dumps(batch, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"{stage}: {len(todo)} jobs → {n} chunks of ≤{chunk} in {out}")


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
    ap.add_argument("action", choices=["export", "import"])
    ap.add_argument("stage", choices=["prefilter", "rank"])
    ap.add_argument("--chunk", type=int, default=None)
    ap.add_argument("--max-chars", type=int, default=None)
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--max-fetch", type=int, default=60,
                    help="rank only: how many missing LinkedIn descriptions to fetch (0 disables)")
    a = ap.parse_args()
    if a.action == "export":
        chunk = a.chunk or (100 if a.stage == "prefilter" else 15)
        max_chars = a.max_chars or (1500 if a.stage == "prefilter" else 6000)
        export(a.stage, chunk, max_chars, a.top, a.max_fetch)
    else:
        import_verdicts(a.stage)


if __name__ == "__main__":
    main()
