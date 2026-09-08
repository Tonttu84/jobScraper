"""Command-line interface.

    jobscraper probe [SOURCE ...]   # fetch a few jobs per source, show normalized samples, report failures
    jobscraper scrape [SOURCE ...]  # fetch everything from enabled sources into data/jobs.db
    jobscraper filter               # rule filter over jobs seen in the last N days
    jobscraper prefilter            # Sonnet pass over rule survivors
    jobscraper rank                 # Opus pass over the best prefilter survivors
    jobscraper report               # markdown report + JSONL export, stored in the DB
    jobscraper run                  # scrape → filter → prefilter → rank → report
    jobscraper facets               # recompute the deterministic facets (backfill an old DB)
    jobscraper serve                # web UI over data/jobs.db
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from jobscraper.config import DATA_DIR, load_settings
from jobscraper.facets import compute_all
from jobscraper.filters import RULES_VERSION, apply_rules, dedupe
from jobscraper.http import Http
from jobscraper.models import Job
from jobscraper.sources.base import SourceContext, all_sources, get_source, take
from jobscraper.store import Store

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _http(settings) -> Http:
    return Http(min_delay=float(settings.http.get("min_delay", 1.0)), timeout=float(settings.http.get("timeout", 30)))


def _enabled(settings, names: list[str]) -> list[str]:
    known = all_sources()
    if names:
        unknown = [n for n in names if n not in known]
        if unknown:
            raise typer.BadParameter(f"unknown source(s): {', '.join(unknown)}. Known: {', '.join(sorted(known))}")
        return names
    return [n for n in known if settings.sources.get(n) is None or settings.sources[n].enabled]


@app.command()
def sources() -> None:
    """List available source adapters."""
    settings = load_settings()
    table = Table("source", "enabled", "description")
    for name, src in sorted(all_sources().items()):
        cfg = settings.sources.get(name)
        table.add_row(name, "yes" if (cfg is None or cfg.enabled) else "no", src.description)
    console.print(table)


@app.command()
def probe(names: list[str] | None = typer.Argument(None), limit: int = 5, verbose: bool = False) -> None:
    """Fetch a handful of jobs from each source and show what came back. Run this first on your machine."""
    _setup_logging(verbose)
    settings = load_settings()
    http = _http(settings)
    ok, failed = [], []
    for name in _enabled(settings, names or []):
        src = get_source(name)
        cfg = settings.sources.get(name)
        ctx = SourceContext(http=http, profile=settings.profile, options=cfg.options if cfg else {}, limit=limit)
        t0 = time.monotonic()
        try:
            jobs = list(take(src.fetch(ctx), limit))
        except Exception as exc:
            failed.append((name, f"{type(exc).__name__}: {exc}"))
            console.print(f"[red]✗ {name}[/red]: {type(exc).__name__}: {exc}")
            continue
        dt = time.monotonic() - t0
        if not jobs:
            failed.append((name, "returned 0 jobs"))
            console.print(f"[yellow]? {name}[/yellow]: 0 jobs in {dt:.1f}s (query too narrow, or site changed)")
            continue
        ok.append(name)
        console.print(f"[green]✓ {name}[/green]: {len(jobs)} jobs in {dt:.1f}s")
        for j in jobs[:limit]:
            console.print(f"   • {j.title!r} @ {j.company or '?'} | {j.location_raw or '?'} [{j.country or '?'}/{j.remote}] | "
                          f"{(j.posted_at.date() if j.posted_at else '?')} | desc={len(j.description or '')} chars | {j.url}")
    console.print(f"\n{len(ok)} sources OK, {len(failed)} failed")
    for name, why in failed:
        console.print(f"  - {name}: {why}")


@app.command()
def scrape(names: list[str] | None = typer.Argument(None), verbose: bool = False, limit: int | None = None) -> None:
    """Fetch jobs from enabled sources into the database."""
    _setup_logging(verbose)
    settings = load_settings()
    http = _http(settings)
    store = Store()
    for name in _enabled(settings, names or []):
        src = get_source(name)
        cfg = settings.sources.get(name)
        ctx = SourceContext(http=http, profile=settings.profile, options=cfg.options if cfg else {}, limit=limit)
        t0 = time.monotonic()
        try:
            jobs = list(take(src.fetch(ctx), limit))
        except Exception as exc:
            console.print(f"[red]✗ {name}[/red]: {type(exc).__name__}: {exc}")
            store.log_run(name, 0, 0, f"{type(exc).__name__}: {exc}")
            continue
        new = store.upsert_jobs(jobs)
        store.log_run(name, len(jobs), new)
        console.print(f"[green]✓ {name}[/green]: {len(jobs)} jobs, {new} new ({time.monotonic() - t0:.0f}s)")
    console.print(store.stats())


@app.command("filter")
def filter_cmd(days: int = 30, verbose: bool = False) -> None:
    """Apply the rule filter to jobs seen within the last DAYS days."""
    _setup_logging(verbose)
    settings = load_settings()
    store = Store()
    jobs = store.jobs(seen_within_days=days)
    unique, dups = dedupe(jobs)
    results = apply_rules(unique, settings.profile)
    dup_results = []
    from jobscraper.models import FilterResult
    for kept, others in dups.items():
        for d in others:
            dup_results.append(FilterResult(job_id=d, status="drop", reasons=[f"duplicate of {kept}"], signals={"duplicate_of": kept}))
    store.save_filter_results(results + dup_results, RULES_VERSION)
    facets = compute_all(unique, {r.job_id: r for r in results})
    store.save_facets(facets)
    counts = {s: sum(r.status == s for r in results) for s in ("keep", "review", "drop")}
    console.print(f"{len(jobs)} jobs, {len(dups)} duplicate groups → {counts}")
    console.print(f"facets: {len(facets)} jobs, {sum(f.web_dev for f in facets)} web-dev")
    reasons: dict[str, int] = {}
    for r in results:
        if r.status == "drop":
            key = r.reasons[0].split(":")[0] if r.reasons else "?"
            reasons[key] = reasons.get(key, 0) + 1
    table = Table("drop reason", "count")
    for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])[:15]:
        table.add_row(k, str(v))
    console.print(table)


def _load_state(store: Store, days: int) -> tuple[list[Job], dict]:
    jobs = store.jobs(seen_within_days=days)
    filters = store.filter_results()
    return jobs, filters


@app.command()
def prefilter(days: int = 30, force: bool = False, model: str | None = None, verbose: bool = False, max_jobs: int | None = None) -> None:
    """Sonnet pass over rule-filter survivors (keep + review)."""
    _setup_logging(verbose)
    from jobscraper.ai.client import AIStage, estimate_cost

    settings = load_settings()
    store = Store()
    jobs, filters = _load_state(store, days)
    todo = [j for j in jobs if filters.get(j.id) and filters[j.id].status in ("keep", "review")]
    if max_jobs:
        todo = todo[:max_jobs]
    stage = AIStage("prefilter", settings.profile, store, model=model)
    verdicts = stage.run(todo, filters, force=force, progress=lambda v: console.print(f"  {v.score:3d} {'✓' if v.relevant else '✗'} {v.summary[:110]}"))
    kept = sum(1 for v in verdicts.values() if v.relevant and v.score >= settings.profile.ai.prefilter_min_score)
    console.print(f"prefilter: {len(verdicts)} scored, {kept} pass (score ≥ {settings.profile.ai.prefilter_min_score}); cost ≈ {estimate_cost(verdicts.values())}")


@app.command()
def rank(days: int = 30, top: int | None = None, force: bool = False, model: str | None = None, verbose: bool = False) -> None:
    """Opus pass over the best prefilter survivors."""
    _setup_logging(verbose)
    from jobscraper.ai.client import AIStage, estimate_cost
    from jobscraper.ai.prompts import PROMPT_VERSION

    settings = load_settings()
    store = Store()
    jobs, filters = _load_state(store, days)
    pre = store.verdicts("prefilter", PROMPT_VERSION)
    min_score = settings.profile.ai.prefilter_min_score
    candidates = sorted((v for v in pre.values() if v.relevant and v.score >= min_score), key=lambda v: -v.score)
    n = top or settings.profile.ai.rank_top_n
    ids = {v.job_id for v in candidates[:n]}
    todo = [j for j in jobs if j.id in ids]
    stage = AIStage("rank", settings.profile, store, model=model)
    verdicts = stage.run(todo, filters, force=force, progress=lambda v: console.print(f"  {v.score:3d} {v.summary[:110]}"))
    console.print(f"rank: {len(verdicts)} scored; cost ≈ {estimate_cost(verdicts.values())}")


@app.command()
def report(days: int = 30, out: Path | None = None) -> None:
    """Write the markdown report and a JSONL export, and store the report in the database."""
    from jobscraper.ai.client import estimate_cost
    from jobscraper.ai.prompts import PROMPT_VERSION
    from jobscraper.report import build_snapshot, export_jsonl, write_report

    store = Store()
    jobs, filters = _load_state(store, days)
    pre = store.verdicts("prefilter", PROMPT_VERSION)
    ranked = store.verdicts("rank", PROMPT_VERSION)
    cost = estimate_cost(list(pre.values()) + list(ranked.values()))
    path = write_report(jobs, filters, pre, ranked, out, cost)
    jsonl = export_jsonl([j for j in jobs if filters.get(j.id) and filters[j.id].status != "drop"], filters, {**pre, **ranked},
                         DATA_DIR / "exports" / "filtered.jsonl")
    snap = store.save_report(build_snapshot(jobs, filters, pre, ranked, days=days, cost=cost, path=path, prompt_version=PROMPT_VERSION))

    # The web UI needs facets for everything it shows; fill in whatever `filter` never saw.
    item_ids = list(dict.fromkeys(i.job_id for i in snap.items))
    known = store.facets(item_ids)
    by_id = {j.id: j for j in jobs}
    missing = [by_id[jid] for jid in item_ids if jid not in known and jid in by_id]
    if missing:
        store.save_facets(compute_all(missing, filters))

    sections = {s: sum(i.section == s for i in snap.items) for s in ("ranked", "prefilter", "review")}
    console.print(f"report: {path}\nexport: {jsonl}")
    console.print(f"report #{snap.id}: {sections['ranked']} ranked, {sections['prefilter']} prefilter, "
                  f"{sections['review']} review → {path}")


@app.command("facets")
def facets_cmd(days: int = 30, verbose: bool = False) -> None:
    """Recompute the deterministic facets (languages, stack tags, web_dev) for recent jobs.

    `filter` already does this; run it to backfill a database that predates the facet stage,
    or after bumping FACETS_VERSION, without paying for the AI stages again.
    """
    _setup_logging(verbose)
    store = Store()
    jobs = store.jobs(seen_within_days=days)
    computed = compute_all(jobs, store.filter_results())
    store.save_facets(computed)
    console.print(f"facets: {len(computed)} jobs, {sum(f.web_dev for f in computed)} web-dev")
    counts: dict[str, int] = {}
    for f in computed:
        for tag in f.stacks:
            counts[tag] = counts.get(tag, 0) + 1
    table = Table("stack", "jobs")
    for tag, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:15]:
        table.add_row(tag, str(n))
    console.print(table)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Run the web UI (latest report + per-user decisions) over data/jobs.db."""
    import uvicorn

    from jobscraper.web.app import create_app

    console.print(f"jobscraper web UI on http://{host}:{port} — no auth, keep it local or behind a proxy")
    if reload:
        uvicorn.run("jobscraper.web.app:create_app", host=host, port=port, reload=True, factory=True)
    else:
        uvicorn.run(create_app(), host=host, port=port)


@app.command()
def run(days: int = 30, skip_ai: bool = False, verbose: bool = False) -> None:
    """Full pipeline: scrape → filter → prefilter → rank → report."""
    scrape(None, verbose=verbose)
    filter_cmd(days=days, verbose=verbose)
    if not skip_ai:
        prefilter(days=days, verbose=verbose)
        rank(days=days, verbose=verbose)
    report(days=days)


@app.command()
def stats() -> None:
    """Database counters."""
    console.print(Store().stats())


if __name__ == "__main__":
    app()
