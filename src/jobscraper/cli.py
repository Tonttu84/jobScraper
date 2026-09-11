"""Command-line interface.

    jobscraper probe [SOURCE ...]   # fetch a few jobs per source, show normalized samples, report failures
    jobscraper scrape [SOURCE ...]  # fetch everything from enabled sources into data/jobs.db
    jobscraper filter               # rule filter over jobs seen in the last N days
    jobscraper audit-drops          # sample rule-dropped jobs to label by hand (--score grades them)
    jobscraper prefilter            # Sonnet pass over rule survivors (--batch: half price, async)
    jobscraper rank                 # Opus pass over the best prefilter survivors
    jobscraper refine               # one request that ranks the shortlist against itself
    jobscraper report               # markdown report + JSONL export, stored in the DB
    jobscraper diff                 # what changed since the previous report (see docs/SCHEDULING.md)
    jobscraper run                  # scrape → filter → prefilter → rank → refine → report
    jobscraper facets               # recompute the deterministic facets (backfill an old DB)
    jobscraper serve                # web UI over data/jobs.db
    jobscraper profiles             # candidate profiles under config/profiles
    jobscraper profile-init NAME    # start a new profile from the default config

Every command takes a global --profile NAME (or $JOBSCRAPER_PROFILE), which moves config, data
and results into config/profiles/NAME/, data/profiles/NAME/ and results/NAME/.
"""

from __future__ import annotations

import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from jobscraper import config
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
from jobscraper.sources.base import SourceContext, all_sources, get_source, take
from jobscraper.store import Store, copy_db, default_db_path, new_run_db

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
        for name in _enabled(settings, names or []):
            src = get_source(name)
            cfg = settings.sources.get(name)
            ctx = SourceContext(http=http, profile=settings.profile, options=cfg.options if cfg else {}, limit=limit,
                                browser=browser)
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
    finally:
        if browser is not None:
            browser.close()
    console.print(store.stats())


@app.command("filter")
def filter_cmd(days: int = 30, verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
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


def _load_state(store: Store, days: int) -> tuple[list[Job], dict]:
    jobs = store.jobs(seen_within_days=days)
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


@app.command()
def rank(days: int = 30, top: int | None = None, force: bool = False, model: str | None = None, verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
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


def _record_run_stats(store: Store, snap) -> None:
    """Append this report's anonymous counters to the public ``stats/`` files.

    Bookkeeping must never cost the owner a report: anything that goes wrong here is a warning,
    not a failed command.
    """
    from jobscraper import runstats

    try:
        row = runstats.collect(store, snap, load_settings(), profile=config.active_profile(),
                               usage_path=config.paths().data / "exports" / "ai" / "usage.jsonl")
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
    """One request that ranks the best-ranked jobs against each other (Fable by default).

    The rank stage scores each posting alone inside a chunk, so its score carries chunk noise.
    This pass sees the whole shortlist at once; the report orders by the mean of the two.

    Incremental: only shortlist members without a refine verdict are scored, and the ones already
    placed are sent as fixed anchors so the new ones land in the same ordering. ``--force``
    re-scores the whole shortlist against itself.
    """
    _setup_logging(verbose)
    from jobscraper.ai.client import RefineStage, estimate_cost
    from jobscraper.ai.prompts import PROMPT_VERSION
    from jobscraper.report import calibrated_refine, effective_score, refine_offset

    settings = load_settings()
    n = top or settings.profile.ai.refine_top_n
    if not n:
        console.print("refine: ai.refine_top_n is 0 and no --top given — stage skipped", soft_wrap=True)
        return
    store = Store()
    jobs, filters = _load_state(store, days)
    ranked = store.verdicts("rank", PROMPT_VERSION)
    by_id = {j.id: j for j in jobs}
    alive = {jid for jid, f in filters.items() if f.status != "drop"}
    best = sorted((v for v in ranked.values() if v.job_id in alive and v.job_id in by_id),
                  key=lambda v: (-v.score, v.job_id))[:n]
    todo = [by_id[v.job_id] for v in best]
    if not todo:
        console.print("refine: nothing ranked under the current prompt version — run "
                      "`jobscraper rank` first", soft_wrap=True)
        return

    placed = {} if force else store.verdicts("refine", PROMPT_VERSION)
    fresh = [j for j in todo if j.id not in placed]
    if not fresh:
        console.print(f"refine: all {len(todo)} shortlisted jobs already carry a refine verdict; "
                      "nothing new to compare (--force re-scores them)", soft_wrap=True)
        return

    verdicts = RefineStage(settings.profile, store, model=model).run(todo, filters, force=force)
    if not verdicts:
        console.print(f"refine: no verdicts came back for {len(fresh)} jobs; the rank scores stand",
                      soft_wrap=True)
        return

    # The table is the whole shortlist, anchors included, so the new jobs are read in context.
    scored = {v.job_id for v in verdicts}
    refined = store.verdicts("refine", PROMPT_VERSION)
    rows = sorted(todo, key=lambda j: _refine_sort_key(refined.get(j.id)))
    # This pass marks the whole shortlist lower than the rank pass marks one posting; the table
    # shows its scores where the mean uses them, on the rank scale.
    offset = refine_offset(ranked, refined)
    table = Table("pos", "effective", "rank", "refine", "new", "title")
    for job in rows:
        rv, r = refined.get(job.id), ranked.get(job.id)
        table.add_row(str(rv.position) if rv and rv.position is not None else "—",
                      str(effective_score(r, rv, offset)), str(r.score) if r else "—",
                      str(calibrated_refine(rv.score, offset)) if rv else "—",
                      "*" if job.id in scored else "", job.title[:60])
    console.print(table)
    console.print(f"refine: {len(verdicts)} of {len(fresh)} new jobs placed against "
                  f"{len(todo) - len(fresh)} already refined; calibration {offset:+.1f} to the "
                  f"rank scale; cost ≈ {estimate_cost(verdicts)}", soft_wrap=True)


@app.command()
def report(days: int = 30, out: Path | None = None) -> None:
    """Write the markdown report and a JSONL export, and store the report in the database."""
    from jobscraper.ai.client import estimate_cost
    from jobscraper.ai.prompts import PROMPT_VERSION
    from jobscraper.report import (
        build_snapshot,
        diff_reports,
        export_jsonl,
        prepend_section,
        previous_report,
        render_diff,
        render_new_section,
        write_report,
    )

    store = Store()
    jobs, filters = _load_state(store, days)
    pre = store.verdicts("prefilter", PROMPT_VERSION)
    ranked = store.verdicts("rank", PROMPT_VERSION)
    refined = store.verdicts("refine", PROMPT_VERSION)
    cost = estimate_cost(list(pre.values()) + list(ranked.values()) + list(refined.values()))
    path = write_report(jobs, filters, pre, ranked, out, cost, refine=refined)
    jsonl = export_jsonl([j for j in jobs if filters.get(j.id) and filters[j.id].status != "drop"], filters, {**pre, **ranked},
                         config.paths().data / "exports" / "filtered.jsonl")
    snap = store.save_report(build_snapshot(jobs, filters, pre, ranked, days=days, cost=cost, path=path,
                                            prompt_version=PROMPT_VERSION, refine=refined))

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

    from jobscraper.web.app import create_app

    serve_dir = None if db else (dir or config.paths().data / "serve")
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
        # page falls back to the observed languages and the student preset.
        uvicorn.run("jobscraper.web.app:create_app", host=host, port=port, reload=True, factory=True)
    else:
        uvicorn.run(create_app(db, serve_dir, languages=languages, preset=profile.web.preset),
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
    """Copy the default config/profile.yaml and config/sources.yaml into config/profiles/NAME/."""
    base = config.base_paths()
    try:
        dst = config.profile_dir(name, base)
    except config.ProfileError as exc:
        raise typer.BadParameter(str(exc)) from exc
    for filename in config.PROFILE_FILES:
        if not (base.config / filename).is_file():
            raise typer.BadParameter(f"{base.config / filename} does not exist; nothing to copy from")
        if (dst / filename).exists():
            raise typer.BadParameter(f"{dst / filename} already exists; refusing to overwrite")
    dst.mkdir(parents=True, exist_ok=True)
    for filename in config.PROFILE_FILES:
        shutil.copyfile(base.config / filename, dst / filename)
    console.print(f"created {dst} — edit profile.yaml, then run commands with --profile {name}")


@app.command()
def run(names: list[str] | None = typer.Argument(None, help="Sources to scrape (default: all enabled)"),
        days: int = 30, skip_ai: bool = False, verbose: bool = typer.Option(False, "--verbose", "-v"),
        fresh: bool = typer.Option(False, help="Start from an empty database instead of copying the previous run forward"),
        batch: bool | None = typer.Option(None, "--batch/--no-batch",
                                          help="Run the prefilter through the Message Batches API (half price, "
                                               "asynchronous; default: profile ai.prefilter_batch)")) -> None:
    """Full pipeline in a new per-run database: scrape → filter → prefilter → rank → refine → report."""
    path = new_run_db(fresh=fresh)
    store_mod.DB_OVERRIDE = path
    console.print(f"run database: {path}" + ("" if fresh else " (copied forward from the previous run)"))
    scrape(names, verbose=verbose)
    filter_cmd(days=days, verbose=verbose)
    if not skip_ai:
        prefilter(days=days, verbose=verbose, batch=batch, wait=True)
        rank(days=days, verbose=verbose)
        refine(days=days, verbose=verbose)
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


if __name__ == "__main__":
    app()  # pragma: no cover - only reached by `python -m jobscraper.cli`
