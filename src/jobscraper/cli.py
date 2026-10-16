"""Command-line interface.

    jobscraper probe [SOURCE ...]   # fetch a few jobs per source, show normalized samples, report failures
    jobscraper scrape [SOURCE ...]  # fetch enabled sources into data/jobs.db (scrape.workers at once)
    jobscraper filter               # rule filter over jobs seen in the last N days
    jobscraper audit-drops          # sample rule-dropped jobs to label by hand (--score grades them)
    jobscraper audit-prior          # does the queue's lexical prior order better than the screen score?
    jobscraper eval                 # screen P/R and rank P@k against the web UI's applied/skipped decisions
    jobscraper compare-verdicts     # two verdict sets (prompt version / profile hash) side by side
    jobscraper prefilter            # Sonnet pass over rule survivors (--batch: half price, async)
    jobscraper rank                 # Opus pass in windows, until the top stops gaining entrants
    jobscraper refine               # one request that ranks the shortlist against itself
    jobscraper sentinel             # screen a few rule drops, rank a few screen rejects: gate checks
    jobscraper report               # markdown report + JSONL export, stored in the DB
    jobscraper diff                 # what changed since the previous report (see docs/SCHEDULING.md)
    jobscraper run                  # scrape → filter → prefilter → rank → refine → sentinel → report
    jobscraper facets               # recompute the deterministic facets (backfill an old DB)
    jobscraper serve                # web UI over data/jobs.db
    jobscraper profiles             # candidate profiles under config/profiles
    jobscraper profile-init NAME    # start a new profile from config/profile.example.yaml
    jobscraper cv-parts             # check config/cv-parts.yaml, print the default CV as markdown
    jobscraper tailor JOB_ID ...    # pick CV parts for a job; saves data/cv/tailored/*.md and *.pdf
    jobscraper cv-render [JOB_ID]   # print the default (or a job's tailored) CV to a one-page PDF

Every command takes a global --profile NAME (or $JOBSCRAPER_PROFILE), which moves config, data
and results into config/profiles/NAME/, data/profiles/NAME/ and results/NAME/.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from jobscraper import config, health
from jobscraper import store as store_mod
from jobscraper.audit import (
    read_labels,
    sample_drops,
    score_labels,
    totals_from_rows,
    write_rows,
)
from jobscraper.browser import BrowserFactory
from jobscraper.config import load_settings
from jobscraper.facets import compute_all
from jobscraper.filters import RULES_VERSION, apply_rules, dedupe
from jobscraper.http import Http
from jobscraper.models import Job
from jobscraper.sources.base import (
    Source,
    SourceContext,
    all_sources,
    enumerates_listing,
    get_source,
    needs_browser,
    take,
)
from jobscraper.store import (
    Store,
    copy_db,
    default_db_path,
    new_run_db,
    partition_listed,
    still_listed,
)

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
console = Console()
log = logging.getLogger(__name__)


@app.callback()
def main(db: Path | None = typer.Option(None, "--db", help="Database to work on (default: newest data/runs/*.db, else data/jobs.db)"),
         profile: str | None = typer.Option(None, "--profile", envvar="JOBSCRAPER_PROFILE",
                                            help="Candidate profile: config/profiles/NAME, data/profiles/NAME, results/NAME")) -> None:
    store_mod.DB_OVERRIDE = db
    try:
        config.use_profile(profile)
    except config.ProfileError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _http(settings) -> Http:
    return Http(min_delay=float(settings.http.get("min_delay", 1.0)), timeout=float(settings.http.get("timeout", 30)))


def _browser(settings) -> BrowserFactory | None:
    """The headless-browser factory sources may fall back to, or None if turned off.

    ``http.browser`` in ``config/sources.yaml``: ``auto`` (default) hands every source a
    factory — Chromium is only started if one actually asks for a page — and ``never`` hands
    out nothing, so a Cloudflare-protected source fails with the "needs a headless browser"
    hint instead of launching anything.
    """
    if str(settings.http.get("browser", "auto")).lower() == "never":
        return None
    return BrowserFactory(
        headless=bool(settings.http.get("browser_headless", True)),
        timeout=float(settings.http.get("browser_timeout", 45)),
        min_delay=float(settings.http.get("min_delay", 1.0)),
    )


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
def probe(names: list[str] | None = typer.Argument(None), limit: int = 5, verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Fetch a handful of jobs from each source and show what came back. Run this first on your machine."""
    _setup_logging(verbose)
    settings = load_settings()
    http = _http(settings)
    browser = _browser(settings)
    ok, failed = [], []
    try:
        for name in _enabled(settings, names or []):
            src = get_source(name)
            cfg = settings.sources.get(name)
            ctx = SourceContext(http=http, profile=settings.profile, options=cfg.options if cfg else {}, limit=limit,
                                browser=browser)
            t0 = time.monotonic()
            try:
                jobs = list(take(src.fetch(ctx), limit))
            except Exception as exc:
                failed.append((name, f"{type(exc).__name__}: {exc}"))
                console.print(f"[red]✗ {name}[/red]: {type(exc).__name__}: {exc}")
                continue
            dt = time.monotonic() - t0
            # "(browser)" means the plain client was not enough: that source needs Chromium.
            via = " (browser)" if ctx.browser_calls else ""
            if not jobs:
                failed.append((name, "returned 0 jobs"))
                console.print(f"[yellow]? {name}[/yellow]: 0 jobs in {dt:.1f}s{via} (query too narrow, or site changed)")
                continue
            ok.append(name)
            console.print(f"[green]✓ {name}[/green]: {len(jobs)} jobs in {dt:.1f}s{via}")
            for j in jobs[:limit]:
                console.print(f"   • {j.title!r} @ {j.company or '?'} | {j.location_raw or '?'} [{j.country or '?'}/{j.remote}] | "
                              f"{(j.posted_at.date() if j.posted_at else '?')} | desc={len(j.description or '')} chars | {j.url}")
    finally:
        if browser is not None:
            browser.close()
    console.print(f"\n{len(ok)} sources OK, {len(failed)} failed")
    for name, why in failed:
        console.print(f"  - {name}: {why}")


@app.command()
def scrape(names: list[str] | None = typer.Argument(None), verbose: bool = typer.Option(False, "--verbose", "-v"), limit: int | None = None) -> None:
    """Fetch jobs from enabled sources into the database."""
    _setup_logging(verbose)
    settings = load_settings()
    http = _http(settings)
    browser = _browser(settings)
    store = Store()
    try:
        enabled = [(name, get_source(name)) for name in _enabled(settings, names or [])]

        def context(name: str, with_browser: BrowserFactory | None) -> SourceContext:
            cfg = settings.sources.get(name)
            return SourceContext(http=http, profile=settings.profile, options=cfg.options if cfg else {},
                                 limit=limit, browser=with_browser)

        # The store is single-threaded by design (one sqlite connection, one caller at a time):
        # the pool only fetches, and every store call and console line stays on this thread.
        # A pooled fetch gets no browser — Playwright objects belong to the thread that made
        # them — so the sources that drive one run afterwards, here, one after another.
        pool = ThreadPoolExecutor(max_workers=_workers(settings), thread_name_prefix="source")
        try:
            running = {}
            for name, src in enabled:
                if needs_browser(src):
                    continue
                # The run row is opened first: its id is what stamps every posting this fetch
                # brings back, and the postings that keep an older id are the ones the source
                # has stopped listing. The clock starts at submit, so the time a source spent
                # queued for a free worker is counted in its seconds.
                booking = (name, src, store.start_run(name), time.monotonic())
                running[pool.submit(_fetch, src, context(name, None), limit)] = booking
            for future in as_completed(running):
                _book_fetch(store, *running[future], limit, future.result)
        finally:
            pool.shutdown(cancel_futures=True)
        for name, src in enabled:
            if needs_browser(src):
                booking = (name, src, store.start_run(name), time.monotonic())
                _book_fetch(store, *booking, limit, partial(_fetch, src, context(name, browser), limit))
    finally:
        if browser is not None:
            browser.close()
    console.print(store.stats())


def _workers(settings) -> int:
    """``scrape.workers`` in ``config/sources.yaml``: sources fetched at once (default 3)."""
    return max(1, int(settings.scrape.get("workers", 3)))


def _fetch(src: Source, ctx: SourceContext, limit: int | None) -> list[Job]:
    """The whole of one source's fetch — the only part of ``scrape`` that runs in the pool."""
    return list(take(src.fetch(ctx), limit))


def _book_fetch(store: Store, name: str, src: Source, run_id: int, t0: float, limit: int | None,
                result: Callable[[], list[Job]]) -> None:
    """Write one finished fetch (``result`` returns its jobs or raises its error) to the store.

    Main thread only: the pooled and the browser sources both end here.
    """
    try:
        jobs = result()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        console.print(f"[red]✗ {name}[/red]: {error}")
        store.finish_run(run_id, 0, 0, error)
        health.note_source_error(store, name, error)
        return
    new = store.upsert_jobs(jobs, run_id)
    store.finish_run(run_id, len(jobs), new)
    # A --limit'ed fetch is short on purpose; holding it to the last full run says nothing.
    previous = health.check_count(store, name, run_id, len(jobs)) if limit is None else None
    health.check_descriptions(store, name, jobs)
    # Only a run that really enumerated the whole listing may call anything gone: an
    # empty fetch, a keyword-search source and a --limit'ed fetch all say nothing — and
    # neither does a listing that came back at a fraction of its usual size.
    if jobs and limit is None and enumerates_listing(src):
        if previous is not None:
            health.note_takedown_skipped(store, name, fetched=len(jobs), previous=previous)
        else:
            log.debug("%s: %d posting(s) did not come back in this run", name,
                      store.mark_missing(name, run_id))
    console.print(f"[green]✓ {name}[/green]: {len(jobs)} jobs, {new} new ({time.monotonic() - t0:.0f}s)")


@app.command("filter")
def filter_cmd(days: int = 30, verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Apply the rule filter to jobs seen within the last DAYS days."""
    _setup_logging(verbose)
    settings = load_settings()
    store = Store()
    jobs = store.jobs(seen_within_days=days)
    unique, dups = dedupe(jobs, settings.profile.location, aliases=settings.company_aliases)
    borderline: list = []
    results = apply_rules(unique, settings.profile, borderline=borderline)
    # A drop decided by a detection barely over the threshold is the rule filter's least certain
    # call; the report lists them so a misread language is visible (the decision stands).
    minimum = settings.profile.languages.min_confidence
    for b in borderline:
        store.add_note("filter", "language_borderline",
                       f"dropped as written in {b.language} at confidence {b.confidence:.2f} "
                       f"(threshold {minimum:.2f})",
                       source=b.source, job_id=b.job_id,
                       data={"language": b.language, "confidence": round(b.confidence, 3),
                             "min_confidence": minimum})
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


def _pct(rate: float | None) -> str:
    return "—" if rate is None else f"{rate * 100:.1f}%"


def _count(value: float | None) -> str:
    return "—" if value is None else f"{value:,.0f}"


def _score_drop_audit(path: Path) -> None:
    """Print what a hand-labelled sample says about the rules' false negatives."""
    if not path.is_file():
        raise typer.BadParameter(f"{path} does not exist")
    rows = read_labels(path)
    scored = score_labels(rows, totals_from_rows(rows))
    table = Table("category", "dropped", "sampled", "labelled", "good", "bad", "unsure",
                  "FN rate", "est. good lost")
    for name, c in scored["categories"].items():
        table.add_row(name, str(c["total"]), str(c["sampled"]), str(c["labelled"]), str(c["good"]),
                      str(c["bad"]), str(c["unsure"]), _pct(c["rate"]), _count(c["estimated_lost"]))
    console.print(table)
    o = scored["overall"]
    console.print(f"overall: {o['labelled']} labelled of {o['sampled']} sampled "
                  f"({o['good']} good, {o['bad']} bad, {o['unsure']} unsure, {o['unlabelled']} unlabelled)",
                  soft_wrap=True)
    if o["invalid"]:
        console.print(f"[yellow]{o['invalid']} unrecognised label(s) ignored[/yellow] — write good, bad, "
                      "unsure, or leave the field empty", soft_wrap=True)
    # One line to paste into the commit message that acts on this audit.
    console.print(f"drop audit: {o['labelled']}/{o['sampled']} labelled, FN rate {_pct(o['rate'])} "
                  f"→ ≈{_count(o['estimated_lost'])} good jobs lost of {o['total']} rule-dropped",
                  soft_wrap=True)


@app.command("audit-drops")
def audit_drops(
    per_reason: int = typer.Option(15, "--per-reason", help="How many dropped jobs to sample per drop reason"),
    seed: int = typer.Option(1, "--seed", help="Random seed; the same seed over the same database draws the same sample"),
    out: Path | None = typer.Option(None, "--out", help="Where to write the sample (default: data/labels/drop-audit-<date>.jsonl)"),
    score: Path | None = typer.Option(None, "--score", help="Score a labelled sample file instead of drawing a new one"),
) -> None:
    """Stratified sample of rule-dropped jobs to label by hand — how many good ones do the rules lose?

    Draw a sample, write good/bad/unsure into each row's ``label`` field (``note`` is free text),
    then run the same command with ``--score FILE`` for the false-negative rate per drop reason.
    """
    if score is not None:
        _score_drop_audit(score)
        return
    store = Store()
    rows = sample_drops(store, per_reason=per_reason, seed=seed)
    if not rows:
        console.print("no dropped jobs in this database — run `jobscraper filter` first")
        return
    path = out or config.paths().data / "labels" / f"drop-audit-{datetime.now(UTC):%Y-%m-%d}.jsonl"
    if path.exists():
        raise typer.BadParameter(f"{path} already exists; move it aside or pass --out")
    write_rows(path, rows)

    totals = totals_from_rows(rows)
    sampled: dict[str, int] = {}
    for row in rows:
        sampled[row["category"]] = sampled.get(row["category"], 0) + 1
    table = Table("category", "total dropped", "sampled")
    for name in sorted(totals, key=lambda k: (-totals[k], k)):
        table.add_row(name, str(totals[name]), str(sampled[name]))
    console.print(table)
    console.print(f"{len(rows)} of {sum(totals.values())} dropped jobs sampled (seed {seed}) → {path}", soft_wrap=True)
    console.print("label each row good / bad / unsure, then: jobscraper audit-drops --score "
                  f"{path.name}", soft_wrap=True)


def _rho(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}"


def _parse_depths(text: str) -> tuple[int, ...]:
    """``"20,40,60"`` → ``(20, 40, 60)``; anything else is a usage error, not a traceback."""
    try:
        depths = tuple(int(part) for part in text.split(",") if part.strip())
    except ValueError as exc:
        raise typer.BadParameter(f"--depths takes a comma-separated list of numbers: {text!r}") from exc
    if not depths or any(d < 1 for d in depths):
        raise typer.BadParameter(f"--depths takes positive numbers: {text!r}")
    return depths


@app.command("audit-prior")
def audit_prior(
    days: int = 30,
    top: int | None = typer.Option(None, "--top", help="Width of the effective top to look for (default: profile ai.refine_top_n)"),
    depths: str = typer.Option("20,40,60", "--depths", help="How deep into the queue to count, comma-separated"),
) -> None:
    """Was ordering the rank queue by the lexical prior better than ordering it by the screen score?

    Replays both orderings over the jobs that already carry a rank verdict and prints, side by
    side, their Spearman against the rank score and how deep each one buries the current
    effective top N. The ordering decides what the owner ever sees — only ~90 of ~1 600 screen
    survivors are ranked per round — so it is worth a number rather than an opinion.
    """
    from jobscraper.ai.prompts import COMPATIBLE_PROMPT_VERSIONS
    from jobscraper.audit import prior_comparison
    from jobscraper.report import RANK_TOP_WATCH

    settings = load_settings()
    cuts = _parse_depths(depths)
    store = Store()
    jobs, filters = _load_state(store, days)
    width = top or settings.profile.ai.refine_top_n or RANK_TOP_WATCH
    out = prior_comparison(jobs, filters,
                           store.verdicts("prefilter", COMPATIBLE_PROMPT_VERSIONS),
                           store.verdicts("rank", COMPATIBLE_PROMPT_VERSIONS),
                           store.verdicts("refine", COMPATIBLE_PROMPT_VERSIONS),
                           settings.profile, top=width, depths=cuts)
    if not out["sample"]:
        console.print("audit-prior: nothing to measure — no job in this database carries a rank "
                      "verdict under the current prompt versions", soft_wrap=True)
        return

    console.print(f"audit-prior: {out['sample']} jobs carry a rank verdict; the effective top "
                  f"{len(out['top'])} is what each ordering is asked to reach", soft_wrap=True)
    table = Table("ordering", "pairs", "Spearman vs rank score",
                  *[f"top-{len(out['top'])} in first {d}" for d in cuts], "median position")
    for name, row in out["orderings"].items():
        table.add_row(name, str(row["n"]), _rho(row["spearman"]),
                      *[f"{row['at'][d]}/{row['found']}" for d in cuts],
                      "—" if row["median_position"] is None else f"{row['median_position']:.1f}")
    console.print(table)
    console.print("Caveat: only jobs that already carry a rank verdict can be measured, and those "
                  "were selected by the screen ordering itself — the comparison understates any "
                  "new ordering's advantage.", soft_wrap=True)


def _load_state(store: Store, days: int) -> tuple[list[Job], dict]:
    """The jobs every stage after the rule filter works on, and their filter results.

    A posting its board has stopped listing is left out here, once, so no stage below spends a
    rank call or a report line on something nobody can apply to. Its row, verdicts and history
    stay in the database; only the rule filter still sees it, because dropping it there would
    rewrite history rather than the shortlist.
    """
    jobs = still_listed(store.jobs(seen_within_days=days), store,
                        load_settings().profile.gone_after_misses)
    filters = store.filter_results()
    return jobs, filters


@app.command()
def prefilter(days: int = 30, force: bool = False, model: str | None = None, verbose: bool = typer.Option(False, "--verbose", "-v"), max_jobs: int | None = None,
              batch: bool | None = typer.Option(None, "--batch/--no-batch",
                                                help="Score through the Message Batches API: same verdicts at half "
                                                     "price, but asynchronous (default: profile ai.prefilter_batch)"),
              wait: bool = typer.Option(True, "--wait/--no-wait",
                                        help="With --batch: poll until the batch ends. --no-wait submits and exits; "
                                             "run prefilter again later to collect the results")) -> None:
    """Sonnet pass over rule-filter survivors (keep + review)."""
    _setup_logging(verbose)
    from jobscraper.ai.client import AIStage, estimate_cost

    settings = load_settings()
    ai = settings.profile.ai
    store = Store()
    jobs, filters = _load_state(store, days)
    todo = [j for j in jobs if filters.get(j.id) and filters[j.id].status in ("keep", "review")]
    if max_jobs:
        todo = todo[:max_jobs]
    stage = AIStage("prefilter", settings.profile, store, model=model)

    def show(v) -> None:
        console.print(f"  {v.score:3d} {'✓' if v.relevant else '✗'} {v.summary[:110]}")

    if (ai.prefilter_batch if batch is None else batch):
        verdicts = stage.run_batch(todo, filters, force=force, progress=show, wait=wait,
                                   poll_seconds=ai.batch_poll_seconds)
    else:
        verdicts = stage.run(todo, filters, force=force, progress=show)
    kept = sum(1 for v in verdicts.values() if v.relevant and v.score >= settings.profile.ai.prefilter_min_score)
    console.print(f"prefilter: {len(verdicts)} scored, {kept} pass (score ≥ {settings.profile.ai.prefilter_min_score}); cost ≈ {estimate_cost(verdicts.values())}")


def _budget_text(newly: int, budget: int) -> str:
    """How far into the round's budget the rank loop is; an unbounded round just counts."""
    return f"{newly}/{budget} budget" if budget else f"{newly} ranked this round"


def _top_boundary(days: int, n: int) -> int | None:
    """The effective score at the bottom of the effective top ``n``, read from the database.

    ``run`` compares it across the refine loop: the refine pass reads a shortlist against itself
    and marks most of it down, so a boundary that fell means jobs that were never ranked could
    now be in the top — and the ranker is worth another window loop.
    """
    from jobscraper.ai.prompts import COMPATIBLE_PROMPT_VERSIONS
    from jobscraper.report import effective_top

    store = Store()
    jobs, filters = _load_state(store, days)
    _ids, edge = effective_top(jobs, filters, store.verdicts("rank", COMPATIBLE_PROMPT_VERSIONS),
                               store.verdicts("refine", COMPATIBLE_PROMPT_VERSIONS), top=n)
    return edge


@app.command()
def rank(days: int = 30, top: int | None = None, force: bool = False, model: str | None = None, verbose: bool = typer.Option(False, "--verbose", "-v")) -> int:
    """Opus pass over the prefilter survivors, in windows, until the top stops gaining entrants.

    There is no "best N by screen score" cut any more. Measured on 2026-09-11 over the ranked
    jobs, the screen score has no relation to the rank score (Spearman -0.06) and two thirds of
    the Opus-80+ jobs sat within five screen points of the old cut: the cut was close to a random
    sample of some 1 600 survivors, so strong jobs were simply never being looked at.

    The screen is a gate now, not a sorter: the queue is ordered by the free lexical prior in
    :mod:`jobscraper.prior` (``ai.rank_order``), which beat the screen score on the same stored
    verdicts. ``jobscraper audit-prior`` re-measures both orderings against this database.

    The queue (:func:`jobscraper.report.rank_queue`) is read in windows of
    ``ai.rank_window`` — the first one ``ai.rank_top_n`` deep, because a fresh round has no
    evidence to stop on — and after every window the effective top ``ai.refine_top_n`` is
    rebuilt. The round ends when the queue runs out, when ``ai.rank_patience`` jobs in a row have
    failed to enter that top, or when ``ai.rank_budget`` jobs have been scored. So a small pool
    is ranked whole and a huge one stays bounded.

    ``--top N`` is the manual escape hatch: rank exactly the next N of the queue and stop.
    ``--force`` puts the already-ranked jobs back into the queue and re-scores them.
    """
    _setup_logging(verbose)
    from jobscraper.ai.client import AIStage, estimate_cost
    from jobscraper.ai.prompts import (
        COMPATIBLE_PROMPT_VERSIONS,
        profile_fingerprint,
        rank_anchor_block,
    )
    from jobscraper.prior import queue_prior
    from jobscraper.report import (
        RANK_TOP_WATCH,
        effective_top,
        entered_top,
        miss_run,
        rank_anchors,
        rank_queue,
        same_profile,
    )

    settings = load_settings()
    ai = settings.profile.ai
    store = Store()
    jobs, filters = _load_state(store, days)
    pre = store.verdicts("prefilter", COMPATIBLE_PROMPT_VERSIONS)
    ranked = store.verdicts("rank", COMPATIBLE_PROMPT_VERSIONS)
    refined = store.verdicts("refine", COMPATIBLE_PROMPT_VERSIONS)  # fixed for the whole round
    queue = rank_queue(jobs, filters, pre, {} if force else ranked,
                       min_score=ai.prefilter_min_score,
                       prior=queue_prior(jobs, settings.profile))
    if not queue:
        console.print("rank: nothing to rank — every screen survivor already carries a rank "
                      "verdict (--force re-scores them)", soft_wrap=True)
        return 0

    n = ai.refine_top_n or RANK_TOP_WATCH
    budget = top or ai.rank_budget  # --top names the whole round
    # Fixed for the round, like `refined`: every window is graded against the same reference
    # scores, so a weak window cannot inflate itself into the top and a strong one is not
    # flattened to fill the range. Only verdicts written under the current profile qualify: after
    # a profile change (or on the first round after the stamp existed) the round runs with no
    # anchors, and its own verdicts become the next round's.
    current = same_profile(ranked, profile_fingerprint("rank", settings.profile))
    stage = AIStage("rank", settings.profile, store, model=model,
                    preamble=rank_anchor_block(rank_anchors(jobs, current)))
    before, _edge = effective_top(jobs, filters, ranked, refined, top=n)
    opening = not ranked  # nothing ranked yet: the first window opens at ai.rank_top_n
    windows: list[tuple[int, int]] = []
    scored: list = []
    taken = 0
    reason = "the queue is empty (every screen survivor has been ranked)"
    while taken < len(queue):
        width = ai.rank_top_n if opening and not windows else ai.rank_window
        width = min(width, budget - len(scored)) if budget else width
        batch = queue[taken : taken + width]
        taken += len(batch)
        got = stage.run(batch, filters, force=force,
                        progress=lambda v: console.print(f"  {v.score:3d} {v.summary[:110]}"))
        new_ids = [j.id for j in batch if j.id in got]
        scored += [got[job_id] for job_id in new_ids]
        ranked = store.verdicts("rank", COMPATIBLE_PROMPT_VERSIONS)
        after, _edge = effective_top(jobs, filters, ranked, refined, top=n)
        windows.append((len(new_ids), entered_top(set(before), after, new_ids)))
        before = after
        misses = miss_run(windows)
        console.print(f"window {len(windows)}: {len(new_ids)} ranked, {windows[-1][1]} entered the "
                      f"top {n}, miss run {misses}/{ai.rank_patience}, "
                      f"{_budget_text(len(scored), budget)}", soft_wrap=True)
        if budget and len(scored) >= budget:
            reason = (f"--top {top} reached" if top else
                      f"the {budget}-job budget for this round is spent (ai.rank_budget)")
            break
        if not top and ai.rank_patience and misses >= ai.rank_patience:
            reason = f"{misses} rankings without a new top-{n} entrant (ai.rank_patience)"
            break

    entrants = sum(gained for _ranked, gained in windows)
    console.print(f"rank: {len(scored)} scored in {len(windows)} window"
                  f"{'s' if len(windows) != 1 else ''}, {entrants} entered the top {n}, "
                  f"{len(queue) - taken} left in the queue; stopped: {reason}; "
                  f"cost ≈ {estimate_cost(scored)}", soft_wrap=True)
    return len(scored)


def _record_run_stats(store: Store, snap) -> None:
    """Append this report's anonymous counters to the public ``stats/`` files.

    Bookkeeping must never cost the owner a report: anything that goes wrong here is a warning,
    not a failed command.
    """
    from jobscraper import runstats

    try:
        exports = config.paths().data / "exports" / "ai"
        row = runstats.collect(store, snap, load_settings(), profile=config.active_profile(),
                               usage_path=exports / "usage.jsonl",
                               rank_state_path=exports / "rank" / "state.json")
        runs = runstats.record(row, runstats.runs_path())
        console.print(f"run stats: {runs} (+ {runstats.refresh()})", soft_wrap=True)
    except Exception as exc:
        log.warning("run statistics not recorded: %s: %s", type(exc).__name__, exc)


def _refine_sort_key(verdict) -> tuple[float, int]:
    """Table order for a shortlisted job: its placed position, best score first, unplaced last."""
    position = verdict.position if verdict is not None else None
    return (position if position is not None else float("inf"),
            -verdict.score if verdict is not None else 0)


@app.command()
def refine(days: int = 30, top: int | None = None, model: str | None = None,
           force: bool = False, verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Rank the best jobs against each other until that top N is fully refined (Fable by default).

    The rank stage scores each posting alone inside a chunk, so its score carries chunk noise.
    This pass sees the whole shortlist at once; the report orders by the mean of the two.

    The shortlist is the best ``ai.refine_top_n`` jobs by that mean, not by the rank score, so a
    refined job that fell leaves it and the unrefined neighbour that took its place is scored on
    the next pass — up to ``ai.refine_max_passes`` times. The first request, before anything has
    been refined, is ``ai.refine_first_pass`` wide, and any width takes everything tied with its
    last member rather than splitting a run of equal scores. Each pass is incremental: only the members
    without a refine verdict are scored, the rest ride along as fixed anchors. ``--force``
    re-scores the whole shortlist once, and then iterates as usual.
    """
    _setup_logging(verbose)
    from jobscraper.ai.client import RefineStage, estimate_cost
    from jobscraper.ai.prompts import COMPATIBLE_PROMPT_VERSIONS
    from jobscraper.report import calibrated_refine, effective_score, refine_shortlist

    settings = load_settings()
    ai = settings.profile.ai
    n = top or ai.refine_top_n
    if not n:
        console.print("refine: ai.refine_top_n is 0 and no --top given — stage skipped", soft_wrap=True)
        return
    # An explicit --top names one width; otherwise the opening pass reaches deeper than the rest.
    first_pass = top or ai.refine_first_pass
    store = Store()
    jobs, filters = _load_state(store, days)
    ranked = store.verdicts("rank", COMPATIBLE_PROMPT_VERSIONS)
    refined = store.verdicts("refine", COMPATIBLE_PROMPT_VERSIONS)
    shortlist, offset = refine_shortlist(jobs, filters, ranked, refined, top=n, first_pass=first_pass)
    if not shortlist:
        console.print("refine: nothing ranked under the current prompt version — run "
                      "`jobscraper rank` first", soft_wrap=True)
        return

    stage = RefineStage(settings.profile, store, model=model)
    verdicts: list = []     # every verdict this invocation brought back, over all passes
    attempted = 0           # how many jobs those passes asked about
    anchored = None         # how many were already refined when the first pass started
    passes = 0
    force_now = force
    for _ in range(max(1, ai.refine_max_passes)):
        fresh = list(shortlist) if force_now else [j for j in shortlist if j.id not in refined]
        if anchored is None:
            anchored = len(shortlist) - len(fresh)
        if not fresh:
            break
        new = stage.run(shortlist, filters, force=force_now)
        if not new:
            console.print(f"refine: no verdicts came back for {len(fresh)} jobs; the rank scores "
                          "stand", soft_wrap=True)
            break
        passes += 1
        attempted += len(fresh)
        verdicts += new
        console.print(f"pass {passes}: {len(fresh)} new against {len(shortlist) - len(fresh)} "
                      f"placed, offset {offset:+.1f}", soft_wrap=True)
        force_now = False
        refined = store.verdicts("refine", COMPATIBLE_PROMPT_VERSIONS)
        shortlist, offset = refine_shortlist(jobs, filters, ranked, refined, top=n,
                                             first_pass=first_pass)

    unrefined = [j for j in shortlist if j.id not in refined]
    if not verdicts:
        if not unrefined:
            console.print(f"refine: all {len(shortlist)} shortlisted jobs already carry a refine "
                          f"verdict; nothing new to compare — the top {n} is fully refined "
                          "(--force re-scores it)", soft_wrap=True)
        return

    # The table is the whole shortlist, anchors included, so the new jobs are read in context.
    scored = {v.job_id for v in verdicts}
    rows = sorted(shortlist, key=lambda j: _refine_sort_key(refined.get(j.id)))
    # This pass marks the whole shortlist lower than the rank pass marks one posting; the table
    # shows its scores where the mean uses them, on the rank scale.
    table = Table("pos", "effective", "rank", "refine", "new", "title")
    for job in rows:
        rv, r = refined.get(job.id), ranked.get(job.id)
        table.add_row(str(rv.position) if rv and rv.position is not None else "—",
                      str(effective_score(r, rv, offset)), str(r.score) if r else "—",
                      str(calibrated_refine(rv.score, offset)) if rv else "—",
                      "*" if job.id in scored else "", job.title[:60])
    console.print(table)
    ended = (f"top {n} fully refined after {passes} pass{'es' if passes != 1 else ''}"
             if not unrefined else
             f"stopped after {passes} passes with {len(unrefined)} shortlist member"
             f"{'s' if len(unrefined) != 1 else ''} still unrefined (ai.refine_max_passes)")
    console.print(f"refine: {len(verdicts)} of {attempted} new jobs placed against "
                  f"{anchored} already refined; {ended}; calibration {offset:+.1f} to the "
                  f"rank scale; cost ≈ {estimate_cost(verdicts)}", soft_wrap=True)


@app.command()
def sentinel(n: int | None = None, seed: int | None = None, days: int = 30,
             verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Measure the two gates nothing downstream can see: screen a random sample of rule drops
    and rank a random sample of screen rejects.

    ``--n`` per gate (default ``ai.sentinel_sample``; 0 does nothing), ``--seed`` for the draw
    (default: today's date, so a re-run on the same day repeats the sample). A job that already
    carries a sentinel verdict is not drawn again, and a screen reject that has a real rank
    verdict is not lost, so it is not drawn either. The verdicts go under ``sentinel_prefilter``
    and ``sentinel_rank`` and never enter the queue, the shortlist or the report's sections; the
    report's ``## Sentinels`` section turns them into a false-reject rate per gate.
    """
    _setup_logging(verbose)
    from jobscraper.ai.client import AIStage, estimate_cost
    from jobscraper.ai.prompts import COMPATIBLE_PROMPT_VERSIONS
    from jobscraper.sentinel import (
        default_seed,
        rule_drop_gate,
        sample_rule_drops,
        sample_screen_rejects,
        screen_gate,
        sentinel_prefix,
    )

    settings = load_settings()
    ai = settings.profile.ai
    n = ai.sentinel_sample if n is None else n
    if n <= 0:
        console.print("sentinel: ai.sentinel_sample is 0 and no --n given — skipped", soft_wrap=True)
        return
    seed = default_seed() if seed is None else seed
    store = Store()
    jobs, filters = _load_state(store, days)
    pre = store.verdicts("prefilter", COMPATIBLE_PROMPT_VERSIONS)
    drops = sample_rule_drops(
        jobs, filters, n, seed,
        exclude=set(store.verdicts("sentinel_prefilter", COMPATIBLE_PROMPT_VERSIONS)))
    rejects = sample_screen_rejects(
        jobs, filters, pre, n, seed, min_score=ai.prefilter_min_score,
        exclude=set(store.verdicts("sentinel_rank", COMPATIBLE_PROMPT_VERSIONS))
        | set(store.verdicts("rank", COMPATIBLE_PROMPT_VERSIONS)))

    def show(v) -> None:
        console.print(f"  {v.stage} {v.score:3d} {'✓' if v.relevant else '✗'} {v.summary[:100]}")

    spent: list = []
    for stage_name, sample, gate in (
            ("sentinel_prefilter", drops, lambda job: rule_drop_gate(filters.get(job.id))),
            ("sentinel_rank", rejects, lambda job: screen_gate(pre[job.id]))):
        if not sample:  # an empty pool costs nothing, not even a client
            continue
        stage = AIStage(stage_name, settings.profile, store,
                        prefixes={job.id: sentinel_prefix(gate(job)) for job in sample})
        got = stage.run(sample, filters, progress=show)
        spent += [got[job.id] for job in sample if job.id in got]
    console.print(f"sentinel: {len(drops)} rule drops screened, {len(rejects)} screen rejects "
                  f"ranked (seed {seed}); cost ≈ {estimate_cost(spent)}", soft_wrap=True)


@app.command()
def report(days: int = 30, out: Path | None = None) -> None:
    """Write the markdown report and a JSONL export, and store the report in the database."""
    from jobscraper.ai.client import estimate_cost
    from jobscraper.ai.prompts import COMPATIBLE_PROMPT_VERSIONS, PROMPT_VERSION
    from jobscraper.report import (
        build_snapshot,
        diff_reports,
        export_jsonl,
        notes_anchor,
        prepend_section,
        previous_report,
        record_report_notes,
        record_sentinel_notes,
        render_diff,
        render_new_section,
        sentinel_summary,
        write_report,
    )

    settings = load_settings()
    store = Store()
    jobs, gone = partition_listed(store.jobs(seen_within_days=days), store,
                                  settings.profile.gone_after_misses)
    filters = store.filter_results()
    pre = store.verdicts("prefilter", COMPATIBLE_PROMPT_VERSIONS)
    ranked = store.verdicts("rank", COMPATIBLE_PROMPT_VERSIONS)
    refined = store.verdicts("refine", COMPATIBLE_PROMPT_VERSIONS)
    cost = estimate_cost(list(pre.values()) + list(ranked.values()) + list(refined.values()))
    # This run's pipeline-health notes: everything since the previous report, plus the ones only
    # the finished state can show (two passes far apart, one posting ranked twice).
    since = notes_anchor(store)
    record_report_notes(store, jobs, filters, ranked, refined, aliases=settings.company_aliases)
    # The sentinels written since the previous report: measurements of the two gates, kept out of
    # every section above and shown in their own (see jobscraper.sentinel).
    screened = store.verdicts("sentinel_prefilter", COMPATIBLE_PROMPT_VERSIONS)
    sentinel_ranked = store.verdicts("sentinel_rank", COMPATIBLE_PROMPT_VERSIONS)
    sentinel_ids = list(dict.fromkeys([*screened, *sentinel_ranked]))
    sentinels = sentinel_summary(screened, sentinel_ranked, since=since,
                                 min_score=settings.profile.ai.prefilter_min_score,
                                 jobs={j.id: j for j in store.jobs(ids=sentinel_ids)} if sentinel_ids else {})
    record_sentinel_notes(store, sentinels)
    notes = store.notes(since=since)
    path = write_report(jobs, filters, pre, ranked, out, cost, refine=refined, gone=len(gone),
                        notes=notes, sentinels=sentinels)
    jsonl = export_jsonl([j for j in jobs if filters.get(j.id) and filters[j.id].status != "drop"], filters, {**pre, **ranked},
                         config.paths().data / "exports" / "filtered.jsonl")
    snap = store.save_report(build_snapshot(jobs, filters, pre, ranked, days=days, cost=cost, path=path,
                                            prompt_version=PROMPT_VERSION, refine=refined, gone=len(gone),
                                            notes=notes))

    # The web UI needs facets for everything it shows; fill in whatever `filter` never saw.
    item_ids = list(dict.fromkeys(i.job_id for i in snap.items))
    known = store.facets(item_ids)
    by_id = {j.id: j for j in jobs}
    missing = [by_id[jid] for jid in item_ids if jid not in known and jid in by_id]
    if missing:
        store.save_facets(compute_all(missing, filters))

    _record_run_stats(store, snap)

    sections = {s: sum(i.section == s for i in snap.items) for s in ("ranked", "prefilter", "review")}
    console.print(f"report: {path}\nexport: {jsonl}")
    console.print(f"report #{snap.id}: {sections['ranked']} ranked, {sections['prefilter']} prefilter, "
                  f"{sections['review']} review → {path}")

    # What changed since the previous snapshot: a file of its own, plus a short section on top of
    # the report. Printed last so it is the tail of a cron mail.
    diff = diff_reports(snap, previous_report(store, snap))
    diff_path = path.parent / f"diff-{datetime.now(UTC):%Y-%m-%d}.md"
    diff_path.write_text(render_diff(diff, by_id, ranked, pre), encoding="utf-8")
    prepend_section(path, render_new_section(diff, by_id, ranked))
    since = "first report in this database" if diff.old_id is None else f"new since report #{diff.old_id}"
    console.print(f"{since}: {len(diff.new_ranked)} ranked, {len(diff.new_prefilter)} prefilter ({diff_path})",
                  soft_wrap=True)


@app.command("diff")
def diff_cmd(report_id: int | None = typer.Option(None, "--report", help="Report to inspect (default: the newest)"),
             against: int | None = typer.Option(None, "--against", help="Report to compare against (default: the one before it)"),
             out: Path | None = typer.Option(None, "--out", help="Also write the markdown to this file")) -> None:
    """What changed between two stored report snapshots — the "what's new" of a scheduled run."""
    from jobscraper.report import diff_reports, previous_report, render_diff

    store = Store()
    new = store.report(report_id)
    if new is None:
        console.print("no report to diff — run `jobscraper report` first")
        return
    old = store.report(against) if against is not None else previous_report(store, new)
    diff = diff_reports(new, old)
    ids = list(dict.fromkeys(i.job_id for i in [*new.items, *(old.items if old else [])]))
    jobs_by_id = {j.id: j for j in store.jobs(ids=ids)}
    text = render_diff(diff, jobs_by_id, store.verdicts("rank", new.prompt_version),
                       store.verdicts("prefilter", new.prompt_version))
    typer.echo(text)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        console.print(f"diff written to {out}", soft_wrap=True)


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
def serve(dir: Path | None = typer.Option(None, "--dir", help="Directory of database copies to serve (default data/serve)"),
          db: Path | None = typer.Option(None, "--db-file", help="Serve this single writable database instead"),
          host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Run the web UI over the copies in the serve directory (newest wins, read-only, decisions in decisions.db)."""
    import uvicorn

    from jobscraper.cv.parts import CV_PARTS_FILE
    from jobscraper.web.app import create_app

    serve_dir = None if db else (dir or config.paths().data / "serve")
    # The cards' "Tailor CV" hand-off is only useful where `jobscraper tailor` can run.
    tailor = (config.paths().config / CV_PARTS_FILE).is_file()
    profile = load_settings().profile
    languages = list(profile.languages.ok)
    if serve_dir is not None:
        serve_dir.mkdir(parents=True, exist_ok=True)
        console.print(f"serving copies from {serve_dir} (put a database there with `jobscraper publish`)")
    console.print(f"jobscraper web UI on http://{host}:{port} — no auth, keep it local or behind a proxy")
    if reload:
        import os

        if serve_dir is not None:
            os.environ["JOBSCRAPER_SERVE_DIR"] = str(serve_dir)
        # --reload builds the app in a worker process from the factory string, which takes no
        # arguments: the profile's spoken languages and web preset are not applied there — the
        # page falls back to the observed languages and the student preset, and the "Tailor CV"
        # button is off.
        uvicorn.run("jobscraper.web.app:create_app", host=host, port=port, reload=True, factory=True)
    else:
        uvicorn.run(create_app(db, serve_dir, languages=languages, preset=profile.web.preset,
                               tailor=tailor),
                    host=host, port=port)


@app.command()
def runs() -> None:
    """List the per-run databases under data/runs (newest last, * = current)."""
    current = default_db_path().resolve()
    table = Table("database", "size", "modified", "jobs", "")
    runs_dir = config.paths().data / "runs"
    for path in sorted(runs_dir.glob("*.db")) if runs_dir.is_dir() else []:
        st = path.stat()
        n = Store(path, readonly=True)
        try:
            jobs = n.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        finally:
            n.close()
        table.add_row(path.name, f"{st.st_size / 1e6:.1f} MB", time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                      str(jobs), "*" if path.resolve() == current else "")
    console.print(table)


@app.command()
def publish(name: str | None = typer.Option(None, help="File name for the copy (default: the source name)"),
            dir: Path | None = typer.Option(None, "--dir", help="Serve directory (default data/serve)")) -> None:
    """Copy the current database (or --db) into the serve directory for the web UI."""
    src = default_db_path()
    if not src.exists():
        raise typer.BadParameter(f"{src} does not exist")
    dst = (dir or config.paths().data / "serve") / f"{(name or src.stem).removesuffix('.db')}.db"
    copy_db(src, dst)
    console.print(f"published {src} → {dst}")


@app.command()
def profiles() -> None:
    """List the candidate profiles under config/profiles (* = the one this command used)."""
    base = config.base_paths()
    active = config.active_profile()
    table = Table("profile", "config", "")
    table.add_row("default", str(base.config), "*" if active is None else "")
    for name in config.known_profiles():
        table.add_row(name, str(config.profile_dir(name, base)), "*" if name == active else "")
    console.print(table)


@app.command("profile-init")
def profile_init(name: str = typer.Argument(..., help="Name of the new profile")) -> None:
    """Start config/profiles/NAME/ from the template config/profile.example.yaml and config/sources.yaml.

    The new profile.yaml is the template, never the default config/profile.yaml: that file is the
    owner's own CV, dossier and weights, and a new candidate must not start from them. The boards
    in sources.yaml are not personal, so they are copied as they are.
    """
    base = config.base_paths()
    try:
        dst = config.profile_dir(name, base)
    except config.ProfileError as exc:
        raise typer.BadParameter(str(exc)) from exc
    sources = {"profile.yaml": base.config / "profile.example.yaml",
               "sources.yaml": base.config / "sources.yaml"}
    for filename in config.PROFILE_FILES:
        if not sources[filename].is_file():
            raise typer.BadParameter(f"{sources[filename]} does not exist; nothing to copy from")
        if (dst / filename).exists():
            raise typer.BadParameter(f"{dst / filename} already exists; refusing to overwrite")
    dst.mkdir(parents=True, exist_ok=True)
    for filename in config.PROFILE_FILES:
        shutil.copyfile(sources[filename], dst / filename)
    console.print(f"created {dst} — edit profile.yaml, then run commands with --profile {name}")


@app.command("cv-parts")
def cv_parts() -> None:
    """Validate config/cv-parts.yaml and print the untailored (default) CV as markdown."""
    import yaml

    from jobscraper.cv.parts import default_selection, load_cv_parts, resolve, to_markdown

    try:
        parts = load_cv_parts()
        markdown = to_markdown(resolve(parts, default_selection(parts)))
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        typer.echo(f"cv-parts: {exc}", err=True)
        raise typer.Exit(1) from exc
    slots = parts.slots
    # typer.echo, not the rich console: CV text may contain [brackets] rich would eat as markup.
    typer.echo(f"cv parts {parts.version}: {len(parts.summaries)} summaries, {len(parts.projects)} "
               f"projects, {len(parts.work)} work entries; slots: {slots.projects} projects × "
               f"{slots.project_bullets} bullets, {slots.work_bullets} bullets per work entry")
    typer.echo("")
    typer.echo(markdown, nl=False)


def _write_cv_pdf(cv, pdf_path: Path, *, png: bool = False, required: bool = False) -> None:
    """Print ``cv`` to ``pdf_path`` and say where; warn when it overflows one page. Without a
    browser that is an error when the PDF is the point (``required``), else a warning."""
    from jobscraper.cv import render

    png_path = pdf_path.with_suffix(".png") if png else None
    try:
        result = render.render_pdf(render.to_html(cv), pdf_path, png_path)
    except render.RenderUnavailable as exc:
        if required:
            typer.echo(f"cv-render: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"WARNING: no PDF: {exc}")
        return
    if result.overflow_mm > 0:
        typer.echo(f"WARNING: the CV overflows one page by {result.overflow_mm:.1f} mm; "
                   "choose fewer bullets")
    pages = "" if result.pages == 1 else f" ({result.pages} pages)"
    typer.echo(f"pdf {pdf_path}{pages}")
    if png_path is not None:
        typer.echo(f"png {png_path}")


def _resolve_job_id(store: Store, given: str) -> str:
    """An exact job id, or a prefix that matches exactly one; else a BadParameter."""
    if store.jobs(ids=[given]):
        return given
    matches = store.job_ids_with_prefix(given)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise typer.BadParameter(f"no job {given!r} in {store.path.name}")
    raise typer.BadParameter(f"{given!r} matches several jobs: {', '.join(matches)}")


@app.command()
def tailor(job_ids: list[str] = typer.Argument(..., help="Job ids (or unique prefixes) from the web UI or report"),
           export: bool = typer.Option(False, "--export", help="Write the prompt for a Claude Code subagent instead of calling the API"),
           import_: bool = typer.Option(False, "--import", help="Read the subagent's answer written after --export"),
           force: bool = typer.Option(False, "--force", help="Ask again even when a tailoring is cached"),
           model: str | None = typer.Option(None, help="Model to use (default: ai.rank_model; 'subagent' with --import)"),
           no_pdf: bool = typer.Option(False, "--no-pdf", help="Only the markdown hand-off, no PDF")) -> None:
    """Pick CV parts for each job (ids only, from config/cv-parts.yaml) and print the hand-off.

    With an API key this is one call per job. Without one: `--export` writes
    data/cv/tailored/prompts/<id>.md, a subagent writes data/cv/tailored/answers/<id>.json, and
    `--import` validates it. Each hand-off is saved to data/cv/tailored/<date>-<id>.md and the CV
    printed next to it as <date>-<id>.pdf (unless --no-pdf). Cached per job and parts library, so
    asking again is free until the library or the prompt changes.
    """
    import yaml

    from jobscraper.cv.parts import load_cv_parts
    from jobscraper.cv.tailor import Tailor, TailorError, export_prompt, to_handoff_markdown

    if export and import_:
        typer.echo("tailor: use either --export or --import, not both", err=True)
        raise typer.Exit(1)
    try:
        parts = load_cv_parts()
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        typer.echo(f"tailor: {exc}", err=True)
        raise typer.Exit(1) from exc
    store = Store()
    try:
        try:
            ids = [_resolve_job_id(store, given) for given in job_ids]
        except typer.BadParameter as exc:
            typer.echo(f"tailor: {exc}", err=True)
            raise typer.Exit(1) from exc
        tailor_stage = Tailor(load_settings().profile, store, parts,
                              model=model or ("subagent" if import_ else None))
        filters, ranked = store.filter_results(), store.verdicts("rank")
        out_dir = config.paths().data / "cv" / "tailored"
        for sub in ("prompts", "answers"):
            (out_dir / sub).mkdir(parents=True, exist_ok=True)
        failed = False
        for job in store.jobs(ids=ids):
            answer_path = out_dir / "answers" / f"{job.id}.json"
            if export:
                prompt_path = out_dir / "prompts" / f"{job.id}.md"
                prompt_path.write_text(export_prompt(tailor_stage, job, filters.get(job.id),
                                                     ranked.get(job.id), answer_path), encoding="utf-8")
                typer.echo(f"{job.id} ({job.title}): prompt {prompt_path}; answer goes to {answer_path}")
                continue
            try:
                if import_:
                    if not answer_path.is_file():
                        raise TailorError(f"no answer at {answer_path}; run --export and a subagent first")
                    result = tailor_stage.import_answer(job, answer_path.read_text(encoding="utf-8"))
                else:
                    result = tailor_stage.tailor(job, filters.get(job.id), ranked.get(job.id), force=force)
            except TailorError as exc:
                typer.echo(f"tailor: {job.id} ({job.title}): {exc}", err=True)
                failed = True
                continue
            markdown = to_handoff_markdown(job, result)
            path = out_dir / f"{datetime.now(UTC):%Y-%m-%d}-{job.id}.md"
            path.write_text(markdown, encoding="utf-8")
            # typer.echo, not the rich console: CV text may contain [brackets] rich would eat.
            typer.echo(markdown)
            typer.echo(f"saved {path}")
            if not no_pdf:
                _write_cv_pdf(result.cv, path.with_suffix(".pdf"))
        if failed:
            raise typer.Exit(1)
    finally:
        store.close()


@app.command("cv-render")
def cv_render(job_id: str | None = typer.Argument(None, help="A tailored job's id (or unique prefix); default: the untailored CV"),
              out: Path | None = typer.Option(None, "--out", help="PDF path (default: data/cv/cv-default.pdf, or data/cv/tailored/<date>-<id>.pdf)"),
              png: bool = typer.Option(False, "--png", help="Also write a PNG of the page next to the PDF")) -> None:
    """Print the CV to a one-page PDF in the Canva layout.

    Without JOB_ID the default selection from config/cv-parts.yaml; with one, that job's cached
    tailoring under the current library (run `jobscraper tailor JOB_ID` first).
    """
    import yaml

    from jobscraper.cv.parts import default_selection, load_cv_parts, resolve
    from jobscraper.cv.tailor import TailorAnswer, tailoring_key_prefix, to_selection

    try:
        parts = load_cv_parts()
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        typer.echo(f"cv-render: {exc}", err=True)
        raise typer.Exit(1) from exc
    cv_dir = config.paths().data / "cv"
    if job_id is None:
        cv = resolve(parts, default_selection(parts))
        pdf_path = out or cv_dir / "cv-default.pdf"
    else:
        store = Store()
        try:
            try:
                job_id = _resolve_job_id(store, job_id)
            except typer.BadParameter as exc:
                typer.echo(f"cv-render: {exc}", err=True)
                raise typer.Exit(1) from exc
            stored = store.latest_tailoring(job_id, tailoring_key_prefix(parts))
        finally:
            store.close()
        if stored is None:
            typer.echo(f"cv-render: no tailoring for {job_id} under the current library; run "
                       f"`jobscraper tailor {job_id}` first", err=True)
            raise typer.Exit(1)
        # Stored only after validation against this very library (the fingerprint is in the key).
        cv = resolve(parts, to_selection(TailorAnswer.model_validate_json(stored)))
        pdf_path = out or cv_dir / "tailored" / f"{datetime.now(UTC):%Y-%m-%d}-{job_id}.pdf"
    _write_cv_pdf(cv, pdf_path, png=png, required=True)


def _rank_and_refine(days: int, verbose: bool) -> None:
    """The two AI stages as one loop: rank windows, refine, and rank again if the top moved down.

    ``refine`` reads the shortlist against itself and marks most of it down, which lowers the
    score at the bottom of the effective top N — and a lower boundary is exactly the state in
    which a job the ranker never looked at could belong there. So whenever the boundary falls,
    the rank stop rule gets another run (with a fresh miss-run counter) and, if it found
    anything, the refine loop follows it. ``ai.refine_max_passes`` bounds the outer rounds.
    """
    from jobscraper.report import RANK_TOP_WATCH

    ai = load_settings().profile.ai
    n = ai.refine_top_n or RANK_TOP_WATCH
    rounds = max(1, ai.refine_max_passes)
    rank(days=days, verbose=verbose)
    round_n = 1
    while True:  # every way out of the loop is a break, each with a line saying which one
        before = _top_boundary(days, n)
        refine(days=days, verbose=verbose)
        after = _top_boundary(days, n)
        if before is None or after is None or after >= before:
            if after is not None:
                console.print(f"run: the top {n} boundary held at {after} through the refine "
                              "pass; nothing further to rank", soft_wrap=True)
            break
        if round_n == rounds:
            console.print(f"run: the top {n} boundary fell {before} → {after}, but "
                          f"ai.refine_max_passes ({rounds}) is spent; stopping here", soft_wrap=True)
            break
        round_n += 1
        console.print(f"run: the top {n} boundary fell {before} → {after} in the refine pass; "
                      f"ranking further (round {round_n} of {rounds})", soft_wrap=True)
        if not rank(days=days, verbose=verbose):
            console.print("run: nothing new was ranked; the shortlist is final", soft_wrap=True)
            break


@app.command()
def run(names: list[str] | None = typer.Argument(None, help="Sources to scrape (default: all enabled)"),
        days: int = 30, skip_ai: bool = False, verbose: bool = typer.Option(False, "--verbose", "-v"),
        fresh: bool = typer.Option(False, help="Start from an empty database instead of copying the previous run forward"),
        batch: bool | None = typer.Option(None, "--batch/--no-batch",
                                          help="Run the prefilter through the Message Batches API (half price, "
                                               "asynchronous; default: profile ai.prefilter_batch)")) -> None:
    """Full pipeline in a new per-run database: scrape → filter → prefilter → rank → refine →
    sentinel (when ``ai.sentinel_sample`` > 0) → report."""
    path = new_run_db(fresh=fresh)
    store_mod.DB_OVERRIDE = path
    console.print(f"run database: {path}" + ("" if fresh else " (copied forward from the previous run)"))
    scrape(names, verbose=verbose)
    filter_cmd(days=days, verbose=verbose)
    if not skip_ai:
        prefilter(days=days, verbose=verbose, batch=batch, wait=True)
        _rank_and_refine(days=days, verbose=verbose)
        if load_settings().profile.ai.sentinel_sample > 0:
            sentinel(days=days, verbose=verbose)
    report(days=days)


@app.command()
def stats(public: bool = typer.Option(False, "--public",
                                      help="Rebuild stats/README.md from stats/runs.jsonl and print it "
                                           "(anonymous per-run counters; no database is opened)")) -> None:
    """Database counters — or, with --public, the committed anonymous run statistics."""
    if public:
        from jobscraper import runstats

        out = runstats.refresh()
        typer.echo(out.read_text(encoding="utf-8"))
        console.print(f"wrote {out}", soft_wrap=True)
        return
    console.print(Store().stats())


@app.command("eval")
def eval_cmd(
    days: int = 30,
    user: str | None = typer.Option(None, "--user", help="Only this user's decisions (default: every user; "
                                                         "the latest decision per job wins)"),
    min_decisions: int = typer.Option(10, "--min-decisions",
                                      help="Refuse to grade on fewer usable (non-closed) decisions"),
    json_out: Path | None = typer.Option(None, "--json", help="Also write the full metrics to this JSON file"),
    decisions: Path | None = typer.Option(None, "--decisions",
                                          help="Decisions database to read (default: the web UI's "
                                               "data/serve/decisions.db when it exists, else this "
                                               "database's own decisions table)"),
) -> None:
    """Grade the Sonnet screen and the Opus ranking against the owner's decisions in the web UI.

    Applied / interview / offer / rejected / interested count as wanted, skipped as not wanted,
    closed as neither (see ``audit.DECISION_LABELS``). Prints screen precision/recall, precision
    at 10 and 20 on the report's effective score, and one summary line for the commit message of
    the rule or prompt change being measured.
    """
    import json

    from jobscraper.ai.prompts import COMPATIBLE_PROMPT_VERSIONS
    from jobscraper.audit import decision_eval, eval_summary
    from jobscraper.web.app import DECISIONS_DB

    if decisions is not None and not decisions.is_file():
        raise typer.BadParameter(f"{decisions} does not exist")
    settings = load_settings()
    sidecar = decisions or config.paths().data / "serve" / DECISIONS_DB
    store = Store(decisions_path=sidecar if sidecar.is_file() else None)
    try:
        jobs, filters = _load_state(store, days)
        out = decision_eval(jobs, filters,
                            store.verdicts("prefilter", COMPATIBLE_PROMPT_VERSIONS),
                            store.verdicts("rank", COMPATIBLE_PROMPT_VERSIONS),
                            store.verdicts("refine", COMPATIBLE_PROMPT_VERSIONS),
                            store.decisions(user), settings.profile)
    finally:
        store.close()

    counts = out["counts"]
    if counts["usable"] < min_decisions:
        # Counting skip reasons needs no minimum: it is the data being collected.
        _print_skip_reasons(out["skip_reasons"])
        console.print(f"eval: {counts['usable']} usable decisions (closed ones excluded) — at least "
                      f"{min_decisions} are needed for a number worth quoting; record more in the "
                      "web UI or lower --min-decisions", soft_wrap=True)
        return

    def num(value: float | None) -> str:
        return "—" if value is None else f"{value:.2f}"

    table = Table("decisions", "count")
    for key, value in counts.items():
        table.add_row(key.replace("_", " "), str(value))
    console.print(table)

    screen = out["screen"]
    table = Table(f"screen (pass = relevant and score ≥ {screen['min_score']})", "value")
    for key in ("screened", "passed", "positives_screened", "positives_passed", "negatives_passed"):
        table.add_row(key.replace("_", " "), str(screen[key]))
    table.add_row("precision", num(screen["precision"]))
    table.add_row("recall", num(screen["recall"]))
    unseen = screen["unscreened_positives"]
    for category, n in unseen["rule_dropped"].items():
        table.add_row(f"positive rule-dropped: {category}", str(n))
    table.add_row("positive never screened", str(unseen["never_screened"]))
    console.print(table)

    ranked = out["rank"]
    table = Table("effective top", "jobs", "positive", "negative", "unlabelled", "closed")
    for row in ranked["global"]:
        table.add_row(str(row["k"]), str(row["size"]), str(row["positive"]), str(row["negative"]),
                      str(row["unlabelled"]), str(row["excluded"]))
    console.print(table)

    labelled = ranked["labelled"]
    table = Table("labelled only: precision at", "k used", "positives", "precision")
    for row in labelled["at"]:
        table.add_row(str(row["k"]), str(row["k_used"]), str(row["positives"]), num(row["precision"]))
    console.print(table)
    console.print(f"{labelled['scored']} labelled jobs carry an effective score (k is capped at that); "
                  f"mean score positive {num(labelled['mean_positive'])} vs negative "
                  f"{num(labelled['mean_negative'])}, Spearman score vs label {_rho(labelled['spearman'])}, "
                  f"refine offset {ranked['offset']:+.1f}", soft_wrap=True)
    _print_skip_reasons(out["skip_reasons"])

    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        console.print(f"wrote {json_out}", soft_wrap=True)
    # One line to paste into the commit message of the change being measured.
    console.print(eval_summary(out), soft_wrap=True)


def _print_skip_reasons(skips: dict) -> None:
    """The "Skip reasons" section of ``eval``: count and mean effective score per reason."""
    if not skips["with_reasons"]:
        console.print(f"skip reasons: none of the {skips['skipped']} skipped decisions carries a "
                      "reason yet (tap them under 'skipped' in the web UI)", soft_wrap=True)
        return
    table = Table("reason", "skips", "scored", "mean score", title="Skip reasons")
    for row in skips["reasons"]:
        mean = "—" if row["mean_score"] is None else f"{row['mean_score']:.1f}"
        table.add_row(row["label"], str(row["count"]), str(row["scored"]), mean)
    console.print(table)
    console.print(f"{skips['with_reasons']} of {skips['skipped']} skipped decisions carry a reason; "
                  "a skip with several reasons counts under each", soft_wrap=True)


def _signed(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}"


def _side_label(version: str | None, profile_hash: str | None) -> str:
    parts = [f"prompt_version {version}" if version else "", f"profile_hash {profile_hash}" if profile_hash else ""]
    return " + ".join(p for p in parts if p)


@app.command("compare-verdicts")
def compare_verdicts_cmd(
    stage: str = typer.Option("rank", "--stage", help="prefilter, rank or refine"),
    old: str | None = typer.Option(None, "--old", help="Old side: verdicts under this prompt_version"),
    new: str | None = typer.Option(None, "--new", help="New side: verdicts under this prompt_version"),
    old_hash: str | None = typer.Option(None, "--old-hash", help="Old side: verdicts under this profile_hash"),
    new_hash: str | None = typer.Option(None, "--new-hash", help="New side: verdicts under this profile_hash"),
    top: int = typer.Option(30, "--top", help="Width of the top-N overlap (rank/refine)"),
    days: int = typer.Option(60, "--days", help="Only jobs seen within this many days, as the stages read them"),
) -> None:
    """Compare two sets of verdicts for the same jobs: before/after a prompt or profile change.

    Each side is the latest verdict per job under a prompt version (--old/--new), a profile
    fingerprint (--old-hash/--new-hash), or both at once. Prints the counts, the score delta,
    relevant flips, the top-N overlap and Spearman (rank/refine), the biggest movers, the mean
    delta per rubric criterion (rank) and the run notes written about these jobs since the old
    side's newest verdict.
    """
    from jobscraper.compare import load_comparison

    if stage not in ("prefilter", "rank", "refine"):
        raise typer.BadParameter(f"--stage must be prefilter, rank or refine, not {stage!r}")
    if not (old or old_hash) or not (new or new_hash):
        raise typer.BadParameter("each side needs --old/--old-hash and --new/--new-hash (either or both)")
    store = Store()
    try:
        jobs, _filters = _load_state(store, days)
        out = load_comparison(store, jobs, stage=stage, old_version=old, old_hash=old_hash,
                              new_version=new, new_hash=new_hash, top=top)
    finally:
        store.close()

    def say(text: str) -> None:
        console.print(text, soft_wrap=True, markup=False, highlight=False)

    counts = out["counts"]
    say(f"compare-verdicts {stage}: old = {_side_label(old, old_hash)}; new = {_side_label(new, new_hash)}")
    say(f"jobs seen in the last {days} days: old only {counts['old_only']}, new only "
        f"{counts['new_only']}, both {counts['both']}")
    if not counts["both"]:
        say("nothing to compare — no job carries a verdict on both sides")
        return
    delta = out["delta"]
    sd = "n/a" if delta["sd"] is None else f"{delta['sd']:.2f}"
    say(f"mean delta (new - old) {_signed(delta['mean'])} (sd {sd}, median {_signed(delta['median'])})")
    flips = out["flips"]
    say(f"relevant flips: true→false {flips['true_to_false']}, false→true {flips['false_to_true']}")
    if out["top"] is not None:
        say(f"top-{out['top']['n']} overlap (Jaccard) {out['top']['jaccard']:.2f}; "
            f"Spearman {_rho(out['spearman'])}")
    if out["movers"]:
        say("biggest movers:")
        for m in out["movers"]:
            say(f"  {m['title']} · {m['company']} · {m['old']} → {m['new']} ({m['delta']:+d})")
    if out["criteria"]:
        say("mean delta per criterion (new - old):")
        for key, row in out["criteria"].items():
            say(f"  {key}: {_signed(row['mean_delta'])} over {row['n']} jobs")
    notes = out["notes"]
    since = notes["since"].isoformat(timespec="minutes")  # set: both sides scored something
    if notes["total"]:
        kinds = ", ".join(f"{kind} {n}" for kind, n in notes["by_kind"].items())
        say(f"{notes['total']} run note{'s' if notes['total'] != 1 else ''} on these jobs since "
            f"{since} (the old side's newest verdict): {kinds}")
    else:
        say(f"no run notes on these jobs since {since} (the old side's newest verdict)")
    if out["small"]:
        say(f"hint: fewer than 10 jobs carry a verdict on both sides ({counts['both']}); read these "
            "numbers as anecdotes, or widen --days")


if __name__ == "__main__":
    app()  # pragma: no cover - only reached by `python -m jobscraper.cli`
