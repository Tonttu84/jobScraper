"""End-to-end tests for the Typer CLI, with the network faked and the data dir in tmp_path.

The AI commands (``prefilter``/``rank``) are deliberately not exercised for real here — they would
call the Anthropic API; only the flag plumbing is checked, against a stubbed ``AIStage``.
Everything up to ``report`` runs for real against a temporary SQLite file.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from jobscraper import cli as cli_mod
from jobscraper import store as store_mod
from jobscraper.browser import BrowserFactory
from jobscraper.models import FilterResult
from tests.conftest import FakeBrowser, FakeHttp

runner = CliRunner()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point config.paths() at tmp_path so nothing touches the repo's data/ or results/."""
    monkeypatch.setenv("JOBSCRAPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JOBSCRAPER_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(store_mod, "DB_OVERRIDE", None)
    monkeypatch.delenv("JOBSCRAPER_DB", raising=False)
    return tmp_path


@pytest.fixture
def fake_http(monkeypatch):
    """Serve the arbeitnow fixture instead of hitting the network."""
    http = FakeHttp({"job-board-api": "arbeitnow.json"})
    monkeypatch.setattr(cli_mod, "_http", lambda settings: http)
    return http


def test_sources_lists_arbeitnow(data_dir):
    result = runner.invoke(cli_mod.app, ["sources"])
    assert result.exit_code == 0, result.output
    assert "arbeitnow" in result.output


def test_unknown_source_is_rejected(data_dir):
    result = runner.invoke(cli_mod.app, ["scrape", "not-a-source"])
    assert result.exit_code != 0
    assert "unknown source" in result.output.lower()


def test_scrape_filter_report_pipeline(data_dir, fake_http):
    scrape = runner.invoke(cli_mod.app, ["scrape", "arbeitnow", "--limit", "2"])
    assert scrape.exit_code == 0, scrape.output
    assert "arbeitnow" in scrape.output
    assert (data_dir / "jobs.db").exists()

    store = store_mod.Store()
    try:
        jobs = store.jobs()
        assert len(jobs) == 2  # --limit 2 stops after the first two fixture records
        assert {j.source for j in jobs} == {"arbeitnow"}
        runs = list(store.conn.execute("SELECT source, fetched, new FROM runs"))
        assert [tuple(r) for r in runs] == [("arbeitnow", 2, 2)]
    finally:
        store.close()

    # a second scrape finds no new jobs
    again = runner.invoke(cli_mod.app, ["scrape", "arbeitnow", "--limit", "2"])
    assert again.exit_code == 0, again.output
    assert "0 new" in again.output

    filtered = runner.invoke(cli_mod.app, ["filter"])
    assert filtered.exit_code == 0, filtered.output
    assert "2 jobs" in filtered.output
    assert "'keep'" in filtered.output and "'drop'" in filtered.output
    assert "facets:" in filtered.output

    store = store_mod.Store()
    try:
        results = store.filter_results()
        assert len(results) == 2
        by_title = {j.id: j.title for j in store.jobs()}
        statuses = {by_title[jid]: r.status for jid, r in results.items()}
        # one facet row per non-duplicate job; neither fixture job is web work, both are devops
        facets = store.facets()
        assert set(facets) == set(results)
        assert not any(f.web_dev for f in facets.values())
        assert all("devops" in f.stacks for f in facets.values())
        assert statuses["DevOps Engineer (m/w/d)"] == "keep"
        assert statuses["Senior Python Engineer, Platform Libraries (m/f/d)"] == "drop"
    finally:
        store.close()

    reported = runner.invoke(cli_mod.app, ["report"])
    assert reported.exit_code == 0, reported.output
    assert "report #1" in reported.output

    reports = list((data_dir / "results").glob("report-*.md"))  # reports live in results/, not data/
    assert len(reports) == 1
    assert "# Job report" in reports[0].read_text(encoding="utf-8")

    store = store_mod.Store()
    try:
        snap = store.report()
        assert snap is not None
        assert snap.counts["jobs"] == 2
        assert snap.path is not None and snap.path.endswith(".md")
        assert {i.job_id for i in snap.items} <= set(store.facets())  # every item has facets
    finally:
        store.close()

    export = data_dir / "exports" / "filtered.jsonl"
    assert export.exists()
    lines = export.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1  # only the non-dropped job is exported
    rec = json.loads(lines[0])
    assert rec["title"] == "DevOps Engineer (m/w/d)"
    assert rec["filter"]["status"] == "keep"
    assert rec["verdict"] is None


def test_report_honours_an_explicit_output_path(data_dir, fake_http, tmp_path):
    assert runner.invoke(cli_mod.app, ["scrape", "arbeitnow", "--limit", "2"]).exit_code == 0
    assert runner.invoke(cli_mod.app, ["filter"]).exit_code == 0

    out = tmp_path / "custom" / "my-report.md"
    result = runner.invoke(cli_mod.app, ["report", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "# Job report" in out.read_text(encoding="utf-8")


def test_stats_command_reports_the_database(data_dir, fake_http):
    assert runner.invoke(cli_mod.app, ["scrape", "arbeitnow", "--limit", "2"]).exit_code == 0
    result = runner.invoke(cli_mod.app, ["stats"])
    assert result.exit_code == 0, result.output
    assert "arbeitnow" in result.output


def test_facets_command_backfills_an_existing_database(data_dir, fake_http):
    """`jobscraper facets` recomputes facets for old rows without re-running the AI stages."""
    assert runner.invoke(cli_mod.app, ["scrape", "arbeitnow", "--limit", "2"]).exit_code == 0
    assert runner.invoke(cli_mod.app, ["filter"]).exit_code == 0

    store = store_mod.Store()
    try:  # pretend this DB predates the facet stage
        with store.tx() as c:
            c.execute("DELETE FROM job_facets")
        assert store.facets() == {}
    finally:
        store.close()

    result = runner.invoke(cli_mod.app, ["facets", "--days", "30"])
    assert result.exit_code == 0, result.output
    assert "2 jobs" in result.output
    assert "devops" in result.output  # the stack-tag table

    store = store_mod.Store()
    try:
        facets = store.facets()
        assert len(facets) == 2
        assert not any(f.web_dev for f in facets.values())
        assert any("python" in f.stacks for f in facets.values())
    finally:
        store.close()


def test_report_backfills_missing_facets(data_dir, fake_http):
    """A report over a DB whose facets were wiped stores them again for its own items."""
    assert runner.invoke(cli_mod.app, ["scrape", "arbeitnow", "--limit", "2"]).exit_code == 0
    assert runner.invoke(cli_mod.app, ["filter"]).exit_code == 0

    store = store_mod.Store()
    try:  # a `review` job puts an item in the snapshot without running the AI stages
        job = next(j for j in store.jobs() if j.title.startswith("DevOps"))
        store.save_filter_results([FilterResult(job_id=job.id, status="review", reasons=["manual"])], "test")
        with store.tx() as c:
            c.execute("DELETE FROM job_facets")
    finally:
        store.close()

    assert runner.invoke(cli_mod.app, ["report"]).exit_code == 0

    store = store_mod.Store()
    try:
        snap = store.report()
        assert snap is not None
        assert [i.section for i in snap.items] == ["review"]
        stored = store.facets()
        assert set(stored) >= {i.job_id for i in snap.items}
        assert "devops" in stored[job.id].stacks
    finally:
        store.close()


def _fake_uvicorn(monkeypatch) -> dict:
    calls: dict = {}

    def fake_run(app, **kwargs):
        calls["app"] = app
        calls["kwargs"] = kwargs

    monkeypatch.setattr("uvicorn.run", fake_run)
    return calls


def test_serve_runs_uvicorn_with_the_app(data_dir, monkeypatch):
    calls = _fake_uvicorn(monkeypatch)
    result = runner.invoke(cli_mod.app, ["serve", "--host", "0.0.0.0", "--port", "8123"])
    assert result.exit_code == 0, result.output
    assert "8123" in result.output
    assert calls["kwargs"]["host"] == "0.0.0.0"
    assert calls["kwargs"]["port"] == 8123
    assert hasattr(calls["app"], "routes")  # a real FastAPI instance, not the import string


def test_serve_with_reload_passes_the_factory_string(data_dir, monkeypatch):
    calls = _fake_uvicorn(monkeypatch)
    result = runner.invoke(cli_mod.app, ["serve", "--reload"])
    assert result.exit_code == 0, result.output
    assert calls["app"] == "jobscraper.web.app:create_app"
    assert calls["kwargs"]["factory"] is True
    assert calls["kwargs"]["reload"] is True
    assert calls["kwargs"]["port"] == 8000

def test_short_verbose_flag_is_accepted(data_dir, fake_http):
    """README documents ``-v`` as the short form of ``--verbose`` on every command that logs."""
    for args in (["probe", "-v", "arbeitnow"], ["scrape", "-v", "arbeitnow"], ["filter", "-v"]):
        result = runner.invoke(cli_mod.app, args)
        assert result.exit_code == 0, f"{args}: {result.output}"


def test_run_creates_its_own_database_and_publish_copies_it(data_dir, fake_http, monkeypatch):
    monkeypatch.delenv("JOBSCRAPER_DB", raising=False)
    result = runner.invoke(cli_mod.app, ["run", "arbeitnow", "--skip-ai"])
    assert result.exit_code == 0, result.output
    run_dbs = sorted((data_dir / "runs").glob("*.db"))
    assert len(run_dbs) == 1
    assert not (data_dir / "jobs.db").exists()
    store = store_mod.Store(run_dbs[0])
    try:
        n_jobs = len(store.jobs())
        assert n_jobs > 0
    finally:
        store.close()

    listed = runner.invoke(cli_mod.app, ["runs"])
    assert listed.exit_code == 0, listed.output
    assert run_dbs[0].name in listed.output

    # a second run copies the first forward: same jobs, new file
    second = runner.invoke(cli_mod.app, ["run", "arbeitnow", "--skip-ai"])
    assert second.exit_code == 0, second.output
    assert len(list((data_dir / "runs").glob("*.db"))) == 2
    fresh = runner.invoke(cli_mod.app, ["run", "arbeitnow", "--skip-ai", "--fresh"])
    assert fresh.exit_code == 0, fresh.output
    assert len(list((data_dir / "runs").glob("*.db"))) == 3

    published = runner.invoke(cli_mod.app, ["publish", "--name", "week37"])
    assert published.exit_code == 0, published.output
    assert (data_dir / "serve" / "week37.db").exists()
    copy = store_mod.Store(data_dir / "serve" / "week37.db")
    try:
        assert len(copy.jobs()) == n_jobs
    finally:
        copy.close()


def test_db_option_overrides_database_for_a_command(data_dir, fake_http):
    result = runner.invoke(cli_mod.app, ["--db", str(data_dir / "custom.db"), "scrape", "arbeitnow", "--limit", "1"])
    assert result.exit_code == 0, result.output
    assert (data_dir / "custom.db").exists()
    assert not (data_dir / "jobs.db").exists()


# --------------------------------------------------------------- prefilter --batch
@pytest.fixture
def spy_ai(monkeypatch):
    """Record which AIStage entry point the CLI picks, without ever touching the API."""
    from jobscraper.ai import client as ai_client

    calls: dict[str, dict] = {}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def init(self, stage, profile, store, model=None) -> None:
        self.stage = stage

    def record(name):
        def entry(self, jobs, filters, **kw):
            calls[f"{name}:{self.stage}"] = kw
            calls.setdefault(name, kw)
            return {}

        return entry

    monkeypatch.setattr(ai_client.AIStage, "__init__", init)
    monkeypatch.setattr(ai_client.AIStage, "run", record("run"))
    monkeypatch.setattr(ai_client.AIStage, "run_batch", record("run_batch"))
    return calls


def test_prefilter_uses_the_live_path_by_default(data_dir, fake_http, spy_ai):
    runner.invoke(cli_mod.app, ["scrape", "arbeitnow", "--limit", "2"])
    runner.invoke(cli_mod.app, ["filter"])
    result = runner.invoke(cli_mod.app, ["prefilter"])
    assert result.exit_code == 0, result.output
    assert "run" in spy_ai and "run_batch" not in spy_ai


def test_prefilter_batch_flag_uses_the_batches_api(data_dir, fake_http, spy_ai):
    runner.invoke(cli_mod.app, ["scrape", "arbeitnow", "--limit", "2"])
    runner.invoke(cli_mod.app, ["filter"])
    result = runner.invoke(cli_mod.app, ["prefilter", "--batch", "--no-wait"])
    assert result.exit_code == 0, result.output
    assert "run" not in spy_ai
    assert spy_ai["run_batch"]["wait"] is False
    assert spy_ai["run_batch"]["poll_seconds"] == 30


def test_run_passes_batch_through_to_prefilter(data_dir, fake_http, spy_ai):
    _queued_jobs(2)  # screen survivors, so the rank stage has a queue to work through
    result = runner.invoke(cli_mod.app, ["run", "arbeitnow", "--batch"])
    assert result.exit_code == 0, result.output
    assert spy_ai["run_batch:prefilter"]["wait"] is True
    assert "run:rank" in spy_ai  # ranking still goes through the live path
    assert "run_batch:rank" not in spy_ai


# ----------------------------------------------------------- rank: the stop rule
# The screen score does not sort (measured 2026-09-11), so `rank` reads the queue in windows and
# stops when the effective top stops gaining entrants, not at a fixed cut.


@pytest.fixture
def spy_rank(monkeypatch):
    """A rank stage that stores verdicts without the API; ``scores`` sets them per job title."""
    from jobscraper.ai import client as ai_client
    from jobscraper.ai.prompts import PROMPT_VERSION
    from jobscraper.models import AIVerdict

    calls: dict = {"windows": [], "scored": [], "scores": {}, "force": []}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def init(self, stage, profile, store, model=None) -> None:
        self.stage, self.store = stage, store
        self.model = model or profile.ai.rank_model

    def run(self, jobs, filters, *, force=False, progress=None):
        jobs = list(jobs)
        if self.stage != "rank":  # the screening pass is seeded by hand in these tests
            return {}
        calls["windows"].append([j.title for j in jobs])
        calls["force"].append(force)
        out = {}
        for job in jobs:
            verdict = AIVerdict(job_id=job.id, stage="rank", model=self.model,
                                prompt_version=PROMPT_VERSION, relevant=True,
                                score=calls["scores"].get(job.title, 10), language_ok=True,
                                seniority_ok=True, location_ok=True, summary=f"ranked {job.title}")
            self.store.save_verdict(verdict)
            out[job.id] = verdict
            calls["scored"].append(job.title)
            if progress:
                progress(verdict)
        return out

    monkeypatch.setattr(ai_client.AIStage, "__init__", init)
    monkeypatch.setattr(ai_client.AIStage, "run", run)
    return calls


def _ai_config(monkeypatch, **values):
    """Pin the ai policy for one test, whatever the repo's profile.yaml says."""
    settings = cli_mod.load_settings()
    for key, value in values.items():
        setattr(settings.profile.ai, key, value)
    monkeypatch.setattr(cli_mod, "load_settings", lambda *a, **kw: settings)
    return settings


def _queued_jobs(count: int) -> list:
    """``count`` rule-kept jobs with descending screen scores, so the queue order is known."""
    from datetime import UTC, datetime

    from jobscraper.models import Job

    jobs = [Job(source="arbeitnow", source_id=f"q-{i:02d}", title=f"Queued Developer {i:02d}",
                url=f"https://jobs.example.test/q-{i:02d}", company="Example Oy",
                description="We build backend services in Python.", country="FI",
                remote="hybrid", posted_at=datetime.now(UTC))
            for i in range(count)]
    store = store_mod.Store()
    try:
        store.upsert_jobs(jobs)
        store.save_filter_results(
            [FilterResult(job_id=j.id, status="keep", location_tier=1) for j in jobs], "rules-test")
        for i, job in enumerate(jobs):
            _seed_verdict(store, job, "prefilter", 99 - i)
    finally:
        store.close()
    return jobs


def test_rank_stops_when_the_top_stops_gaining_entrants(data_dir, spy_rank, monkeypatch):
    """Patience: two windows in a row that changed nothing end the round well short of the queue."""
    _queued_jobs(10)
    _ai_config(monkeypatch, rank_top_n=2, rank_window=2, rank_patience=4, rank_budget=0,
               refine_top_n=2, prefilter_min_score=30)
    spy_rank["scores"] = {"Queued Developer 00": 90, "Queued Developer 01": 80}

    result = runner.invoke(cli_mod.app, ["rank"])
    assert result.exit_code == 0, result.output

    # window 1 fills the top 2; windows 2 and 3 are all 10s, and the run of 4 misses stops it
    assert spy_rank["scored"] == [f"Queued Developer {i:02d}" for i in range(6)]
    assert "window 1: 2 ranked, 2 entered the top 2, miss run 0/4" in result.output
    assert "window 3: 2 ranked, 0 entered the top 2, miss run 4/4" in result.output
    assert "rank: 6 scored in 3 windows, 2 entered the top 2, 4 left in the queue" in result.output
    assert "stopped: 4 rankings without a new top-2 entrant" in result.output


def test_rank_stops_when_the_round_budget_is_spent(data_dir, spy_rank, monkeypatch):
    _queued_jobs(10)
    _ai_config(monkeypatch, rank_top_n=2, rank_window=2, rank_patience=0, rank_budget=3,
               refine_top_n=2, prefilter_min_score=30)

    result = runner.invoke(cli_mod.app, ["rank"])
    assert result.exit_code == 0, result.output
    assert len(spy_rank["scored"]) == 3  # the second window is narrowed to what is left of it
    assert "window 2: 1 ranked" in result.output and "3/3 budget" in result.output
    assert "stopped: the 3-job budget for this round is spent" in result.output


def test_rank_reads_the_whole_queue_when_it_is_small(data_dir, spy_rank, monkeypatch):
    """The point of the rule: a small pool is ranked whole, not cut at a fixed N."""
    _queued_jobs(3)
    _ai_config(monkeypatch, rank_top_n=2, rank_window=2, rank_patience=0, rank_budget=0,
               refine_top_n=2, prefilter_min_score=30)

    result = runner.invoke(cli_mod.app, ["rank"])
    assert result.exit_code == 0, result.output
    assert len(spy_rank["scored"]) == 3
    assert "0 left in the queue; stopped: the queue is empty" in result.output

    again = runner.invoke(cli_mod.app, ["rank"])
    assert again.exit_code == 0, again.output
    assert "nothing to rank" in again.output
    assert len(spy_rank["scored"]) == 3  # a second round asks about nothing


def test_rank_top_ranks_exactly_that_many_and_stops(data_dir, spy_rank, monkeypatch):
    """--top is the manual escape hatch: the next N of the queue, patience rule not applied."""
    _queued_jobs(10)
    _ai_config(monkeypatch, rank_top_n=2, rank_window=2, rank_patience=1, rank_budget=90,
               refine_top_n=2, prefilter_min_score=30)

    result = runner.invoke(cli_mod.app, ["rank", "--top", "3"])
    assert result.exit_code == 0, result.output
    assert len(spy_rank["scored"]) == 3
    assert "stopped: --top 3 reached" in result.output


def test_rank_force_puts_the_ranked_jobs_back_into_the_queue(data_dir, spy_rank, monkeypatch):
    _queued_jobs(3)
    _ai_config(monkeypatch, rank_top_n=3, rank_window=3, rank_patience=0, rank_budget=0,
               refine_top_n=2, prefilter_min_score=30)
    assert runner.invoke(cli_mod.app, ["rank"]).exit_code == 0

    result = runner.invoke(cli_mod.app, ["rank", "--force"])
    assert result.exit_code == 0, result.output
    assert len(spy_rank["scored"]) == 6  # the same three jobs, scored a second time
    assert spy_rank["force"] == [False, True]


def test_rank_watches_the_top_20_when_the_refine_stage_is_off(data_dir, spy_rank, monkeypatch):
    """``refine_top_n = 0`` turns the refine pass off; the report still has a head to watch."""
    _queued_jobs(2)
    _ai_config(monkeypatch, rank_top_n=2, rank_window=2, rank_patience=30, rank_budget=0,
               refine_top_n=0, prefilter_min_score=30)

    result = runner.invoke(cli_mod.app, ["rank"])
    assert result.exit_code == 0, result.output
    assert "entered the top 20" in result.output


# --------------------------------------------------------------------- refine
@pytest.fixture
def spy_refine(monkeypatch):
    """Record what the CLI hands the refine stage, without ever touching the API."""
    from jobscraper.ai import client as ai_client
    from jobscraper.ai.prompts import PROMPT_VERSION
    from jobscraper.models import AIVerdict

    calls: dict[str, list] = {}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def init(self, profile, store, model=None) -> None:
        self.store = store
        self.model = model or profile.ai.refine_model

    def run(self, jobs, filters, *, force=False):
        """The real stage's split, without the API: already-placed jobs ride along as anchors."""
        calls["jobs"] = list(jobs)
        calls["model"] = [self.model]
        calls["force"] = force
        placed = {} if force else self.store.verdicts("refine", PROMPT_VERSION)
        todo = [j for j in jobs if j.id not in placed]
        calls["scored"] = [j.id for j in todo]
        out = [AIVerdict(job_id=j.id, stage="refine", model=self.model, prompt_version=PROMPT_VERSION,
                         relevant=True, score=90 - 10 * n, position=n + 1, language_ok=True,
                         seniority_ok=True, location_ok=True, summary=f"place {n + 1}")
               for n, j in enumerate(todo)]
        for v in out:
            self.store.save_verdict(v)
        return out

    monkeypatch.setattr(ai_client.RefineStage, "__init__", init)
    monkeypatch.setattr(ai_client.RefineStage, "run", run)
    return calls


def _rank_one_job(data_dir):
    """Scrape, filter, and hand-write a rank verdict so `refine` has a shortlist."""
    _scrape_and_filter(data_dir)
    store = store_mod.Store()
    try:
        job = _job(store, "DevOps")
        _seed_verdict(store, job, "rank", 80)
        return job
    finally:
        store.close()


def test_refine_scores_the_shortlist_and_prints_the_table(data_dir, fake_http, spy_refine):
    job = _rank_one_job(data_dir)
    result = runner.invoke(cli_mod.app, ["refine"])
    assert result.exit_code == 0, result.output
    assert [j.id for j in spy_refine["jobs"]] == [job.id]
    assert spy_refine["model"] == ["claude-fable-5-1"]
    assert "refine" in result.output
    assert "1 of 1 new jobs placed against 0 already refined" in result.output

    store = store_mod.Store()
    try:
        from jobscraper.ai.prompts import PROMPT_VERSION

        stored = store.verdicts("refine", PROMPT_VERSION)
        assert stored[job.id].score == 90 and stored[job.id].position == 1
    finally:
        store.close()


def test_refine_takes_top_and_model_from_the_flags(data_dir, fake_http, spy_refine):
    _rank_one_job(data_dir)
    result = runner.invoke(cli_mod.app, ["refine", "--top", "1", "--model", "claude-opus-5"])
    assert result.exit_code == 0, result.output
    assert len(spy_refine["jobs"]) == 1
    assert spy_refine["model"] == ["claude-opus-5"]


def test_refine_is_skipped_when_the_profile_turns_it_off(data_dir, fake_http, spy_refine, monkeypatch):
    _rank_one_job(data_dir)
    settings = cli_mod.load_settings()
    settings.profile.ai.refine_top_n = 0
    monkeypatch.setattr(cli_mod, "load_settings", lambda *a, **kw: settings)
    result = runner.invoke(cli_mod.app, ["refine"])
    assert result.exit_code == 0, result.output
    assert "stage skipped" in result.output
    assert "jobs" not in spy_refine


def test_refine_without_anything_ranked_says_so(data_dir, fake_http, spy_refine):
    _scrape_and_filter(data_dir)
    result = runner.invoke(cli_mod.app, ["refine"])
    assert result.exit_code == 0, result.output
    assert "nothing ranked" in result.output
    assert "jobs" not in spy_refine


def test_refine_with_no_verdicts_back_leaves_the_rank_scores(data_dir, fake_http, spy_refine, monkeypatch):
    from jobscraper.ai import client as ai_client

    _rank_one_job(data_dir)
    monkeypatch.setattr(ai_client.RefineStage, "run", lambda self, jobs, filters, force=False: [])
    result = runner.invoke(cli_mod.app, ["refine"])
    assert result.exit_code == 0, result.output
    assert "the rank scores stand" in result.output


def _rank_two_jobs(data_dir):
    """Two ranked jobs, the DevOps one the better of the pair."""
    _scrape_and_filter(data_dir)
    store = store_mod.Store()
    try:
        first, second = _job(store, "DevOps"), _job(store, "Core Developer")
        _seed_verdict(store, first, "rank", 80)
        _seed_verdict(store, second, "rank", 70)
        return first, second
    finally:
        store.close()


def test_refine_scores_only_the_new_shortlist_members(data_dir, fake_http, spy_refine):
    """A job that already carries a refine verdict is an anchor, not a second bill."""
    first, second = _rank_two_jobs(data_dir)
    store = store_mod.Store()
    try:
        _seed_verdict(store, first, "refine", 84, position=1)
    finally:
        store.close()

    result = runner.invoke(cli_mod.app, ["refine"])
    assert result.exit_code == 0, result.output
    assert [j.id for j in spy_refine["jobs"]] == [first.id, second.id]  # the whole shortlist goes in
    assert spy_refine["scored"] == [second.id]  # only the new one is scored
    assert spy_refine["force"] is False
    assert "1 of 1 new jobs placed against 1 already refined" in result.output
    # The table still shows both, and marks the one this call scored.
    assert "DevOps" in result.output and "Core Developer" in result.output


def _rank_three_jobs(data_dir):
    """The two scraped jobs plus one written straight into the store — enough to calibrate on."""
    from datetime import UTC, datetime

    from jobscraper.models import Job

    _scrape_and_filter(data_dir)
    store = store_mod.Store()
    try:
        third = Job(source="arbeitnow", source_id="cal-1", title="Junior Data Engineer",
                    url="https://jobs.example.test/cal-1", company="Example Oy",
                    description="We build data pipelines in Python.", country="FI",
                    remote="hybrid", posted_at=datetime.now(UTC))
        store.upsert_jobs([third])
        store.save_filter_results(
            [FilterResult(job_id=third.id, status="keep", location_tier=1)], "rules-test")
        first, second = _job(store, "DevOps"), _job(store, "Core Developer")
        _seed_verdict(store, first, "rank", 80)
        _seed_verdict(store, second, "rank", 66)
        _seed_verdict(store, third, "rank", 52)
        return first, second, third
    finally:
        store.close()


def test_refine_table_shows_the_refine_scores_on_the_rank_scale(data_dir, fake_http, spy_refine):
    """Three jobs scored twice: the table reports the calibrated number, and names the shift."""
    _rank_three_jobs(data_dir)
    result = runner.invoke(cli_mod.app, ["refine"])
    assert result.exit_code == 0, result.output
    # the stage scores them 90 / 80 / 70 against ranks of 80 / 66 / 52
    assert "calibration -14.0 to the rank scale" in result.output
    row = next(ln for ln in result.output.splitlines() if "DevOps" in ln)
    assert "76" in row      # 90 read on the rank scale
    assert "90" not in row  # never the raw refine score
    assert "78" in row      # round((80 + 76) / 2)


def test_refine_says_so_when_nothing_is_new(data_dir, fake_http, spy_refine):
    job = _rank_one_job(data_dir)
    store = store_mod.Store()
    try:
        _seed_verdict(store, job, "refine", 84, position=1)
    finally:
        store.close()

    result = runner.invoke(cli_mod.app, ["refine"])
    assert result.exit_code == 0, result.output
    assert "nothing new to compare" in result.output
    assert "jobs" not in spy_refine  # the stage was never asked


def test_refine_table_shows_an_anchor_stored_without_a_position(data_dir, fake_http, spy_refine):
    """A verdict from a pass that stored no position still gets a row, at the bottom."""
    first, second = _rank_two_jobs(data_dir)
    store = store_mod.Store()
    try:
        _seed_verdict(store, first, "refine", 84)  # no position
    finally:
        store.close()

    result = runner.invoke(cli_mod.app, ["refine"])
    assert result.exit_code == 0, result.output
    assert spy_refine["scored"] == [second.id]
    assert "1 of 1 new jobs placed against 1 already refined" in result.output
    assert "DevOps" in result.output and "Core Developer" in result.output


def test_refine_table_lists_a_shortlisted_job_with_no_refine_verdict(data_dir, fake_http, monkeypatch):
    """The model answered for one of the two new jobs; the other keeps its rank score in the table."""
    from jobscraper.ai import client as ai_client
    from jobscraper.ai.prompts import PROMPT_VERSION
    from jobscraper.models import AIVerdict

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _rank_two_jobs(data_dir)
    _widths(monkeypatch, top=20, first_pass=25, max_passes=1)

    def init(self, profile, store, model=None) -> None:
        self.store = store
        self.model = model or profile.ai.refine_model

    def run(self, jobs, filters, *, force=False):
        v = AIVerdict(job_id=jobs[0].id, stage="refine", model=self.model,
                      prompt_version=PROMPT_VERSION, relevant=True, score=77, position=1,
                      language_ok=True, seniority_ok=True, location_ok=True, summary="only one")
        self.store.save_verdict(v)
        return [v]

    monkeypatch.setattr(ai_client.RefineStage, "__init__", init)
    monkeypatch.setattr(ai_client.RefineStage, "run", run)

    result = runner.invoke(cli_mod.app, ["refine"])
    assert result.exit_code == 0, result.output
    assert "1 of 2 new jobs placed against 0 already refined" in result.output
    assert "DevOps" in result.output and "Core Developer" in result.output


# ------------------------------------------------- iterating to a fully refined top N


def _widths(monkeypatch, *, top: int, first_pass: int, max_passes: int = 5):
    """Pin the three refine widths for one test, whatever the repo's profile says."""
    settings = cli_mod.load_settings()
    settings.profile.ai.refine_top_n = top
    settings.profile.ai.refine_first_pass = first_pass
    settings.profile.ai.refine_max_passes = max_passes
    monkeypatch.setattr(cli_mod, "load_settings", lambda *a, **kw: settings)
    return settings


def _rank_six_jobs(data_dir, scores: dict[str, int]) -> dict[str, str]:
    """Six rule-kept jobs written straight into the store with the given rank scores."""
    from datetime import UTC, datetime

    from jobscraper.models import Job

    store = store_mod.Store()
    try:
        ids = {}
        for n, (name, score) in enumerate(scores.items(), start=1):
            job = Job(source="arbeitnow", source_id=f"loop-{n}", title=f"{name} Engineer",
                      url=f"https://jobs.example.test/loop-{n}", company="Example Oy",
                      description="We build backend services in Python.", country="FI",
                      remote="hybrid", posted_at=datetime.now(UTC))
            store.upsert_jobs([job])
            store.save_filter_results(
                [FilterResult(job_id=job.id, status="keep", location_tier=1)], "rules-test")
            _seed_verdict(store, job, "rank", score)
            ids[name] = job.id
        return ids
    finally:
        store.close()


@pytest.fixture
def scripted_refine(monkeypatch):
    """A refine stage that answers from a score table, so a test can steer the calibration."""
    from jobscraper.ai import client as ai_client
    from jobscraper.ai.prompts import PROMPT_VERSION
    from jobscraper.models import AIVerdict

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    scores: dict[str, int] = {}       # job id -> the score this stage will answer with
    passes: list[list[str]] = []      # the ids scored by each call, in order

    def init(self, profile, store, model=None) -> None:
        self.store = store
        self.model = model or profile.ai.refine_model

    def run(self, jobs, filters, *, force=False):
        placed = {} if force else self.store.verdicts("refine", PROMPT_VERSION)
        todo = [j for j in jobs if j.id not in placed]
        passes.append([j.id for j in todo])
        ordered = sorted(todo, key=lambda j: -scores[j.id])
        out = [AIVerdict(job_id=j.id, stage="refine", model=self.model,
                         prompt_version=PROMPT_VERSION, relevant=True, score=scores[j.id],
                         position=n, language_ok=True, seniority_ok=True, location_ok=True,
                         summary=f"place {n}") for n, j in enumerate(ordered, start=1)]
        for v in out:
            self.store.save_verdict(v)
        return out

    monkeypatch.setattr(ai_client.RefineStage, "__init__", init)
    monkeypatch.setattr(ai_client.RefineStage, "run", run)
    return scores, passes


def test_refine_iterates_until_the_effective_top_n_is_fully_refined(data_dir, fake_http,
                                                                    scripted_refine, monkeypatch):
    """Calibration drops three of the first five, so a sixth job enters the top 3 and is scored."""
    scores, passes = scripted_refine
    _widths(monkeypatch, top=3, first_pass=5)
    ids = _rank_six_jobs(data_dir, {"Alpha": 95, "Bravo": 91, "Charlie": 90, "Delta": 89,
                                    "Echo": 88, "Foxtrot": 87})
    scores.update({ids["Alpha"]: 80, ids["Bravo"]: 78, ids["Charlie"]: 30, ids["Delta"]: 29,
                   ids["Echo"]: 28, ids["Foxtrot"]: 30})

    result = runner.invoke(cli_mod.app, ["refine"])
    assert result.exit_code == 0, result.output

    # First pass: the five widest. Foxtrot was 6th by rank, but on one scale it beats the three
    # the second pass marked down, so the second pass picks it up.
    assert passes == [[ids[n] for n in ("Alpha", "Bravo", "Charlie", "Delta", "Echo")],
                      [ids["Foxtrot"]]]
    assert "pass 1: 5 new against 0 placed" in result.output
    assert "pass 2: 1 new against 2 placed" in result.output
    assert "top 3 fully refined after 2 passes" in result.output
    assert "6 of 6 new jobs placed against 0 already refined" in result.output


def test_refine_stops_at_refine_max_passes(data_dir, fake_http, scripted_refine, monkeypatch):
    """A shortlist that keeps changing is bounded: the stage says what it left unrefined."""
    scores, passes = scripted_refine
    _widths(monkeypatch, top=3, first_pass=3, max_passes=2)
    ids = _rank_six_jobs(data_dir, {"Alpha": 95, "Bravo": 91, "Charlie": 90, "Delta": 89,
                                    "Echo": 88, "Foxtrot": 87})
    # Every pass marks its new jobs down far below the one that sets the scale, so the next
    # unrefined neighbour always takes the place they leave and the window never settles.
    scores.update({ids["Alpha"]: 80, ids["Bravo"]: 20, ids["Charlie"]: 20, ids["Delta"]: 20,
                   ids["Echo"]: 20, ids["Foxtrot"]: 20})

    result = runner.invoke(cli_mod.app, ["refine"])
    assert result.exit_code == 0, result.output
    assert passes == [[ids[n] for n in ("Alpha", "Bravo", "Charlie")],
                      [ids[n] for n in ("Delta", "Echo")]]
    assert "stopped after 2 passes with 1 shortlist member still unrefined" in result.output
    assert "ai.refine_max_passes" in result.output
    assert "fully refined" not in result.output


def test_refine_force_rescores_once_and_then_iterates(data_dir, fake_http, scripted_refine,
                                                      monkeypatch):
    """--force re-scores the whole shortlist, and the passes after it are incremental again."""
    scores, passes = scripted_refine
    _widths(monkeypatch, top=3, first_pass=3)
    ids = _rank_six_jobs(data_dir, {"Alpha": 95, "Bravo": 91, "Charlie": 90, "Delta": 89,
                                    "Echo": 88, "Foxtrot": 87})
    scores.update({ids["Alpha"]: 80, ids["Bravo"]: 78, ids["Charlie"]: 30, ids["Delta"]: 76,
                   ids["Echo"]: 28, ids["Foxtrot"]: 27})
    store = store_mod.Store()
    try:
        for name in ("Alpha", "Bravo", "Charlie"):
            job = next(j for j in store.jobs() if j.id == ids[name])
            _seed_verdict(store, job, "refine", 50, position=1)
    finally:
        store.close()

    result = runner.invoke(cli_mod.app, ["refine", "--force"])
    assert result.exit_code == 0, result.output
    assert passes[0] == [ids[n] for n in ("Alpha", "Bravo", "Charlie")]  # all three, re-scored
    assert passes[1] == [ids["Delta"]]                                   # incremental from here
    assert "top 3 fully refined after 2 passes" in result.output


def test_refine_force_re_scores_the_whole_shortlist(data_dir, fake_http, spy_refine):
    job = _rank_one_job(data_dir)
    store = store_mod.Store()
    try:
        _seed_verdict(store, job, "refine", 84, position=1)
    finally:
        store.close()

    result = runner.invoke(cli_mod.app, ["refine", "--force"])
    assert result.exit_code == 0, result.output
    assert spy_refine["force"] is True
    assert spy_refine["scored"] == [job.id]
    assert "1 of 1 new jobs placed against 0 already refined" in result.output

    from jobscraper.ai.prompts import PROMPT_VERSION

    store = store_mod.Store()
    try:
        assert store.verdicts("refine", PROMPT_VERSION)[job.id].score == 90
    finally:
        store.close()


def test_report_orders_by_the_effective_score(data_dir, fake_http, spy_refine):
    job = _rank_one_job(data_dir)
    assert runner.invoke(cli_mod.app, ["refine"]).exit_code == 0
    result = runner.invoke(cli_mod.app, ["report"])
    assert result.exit_code == 0, result.output

    text = next((data_dir / "results").glob("report-*.md")).read_text(encoding="utf-8")
    # One shared job is too few to measure the two stages' scale difference: nothing is moved.
    assert "### 85 (rank 80 · refine 90, calibrated +0.0) · [" in text  # (80 + 90) / 2
    store = store_mod.Store()
    try:
        item = next(i for i in store.report().items if i.job_id == job.id)
        assert item.section == "ranked" and item.score == 85
    finally:
        store.close()


def test_run_calls_refine_after_rank(data_dir, fake_http, spy_ai, spy_refine):
    _queued_jobs(2)
    result = runner.invoke(cli_mod.app, ["run", "arbeitnow"])
    assert result.exit_code == 0, result.output
    assert "run:rank" in spy_ai
    # the stubbed rank stage stores nothing, so refine has an empty shortlist and says so
    assert "refine: nothing ranked" in result.output


def test_run_ranks_again_when_refine_lowers_the_top_boundary(data_dir, fake_http, spy_rank,
                                                             spy_refine, monkeypatch):
    """The refine pass marks the shortlist down; a lower boundary means unranked jobs may belong."""
    _queued_jobs(6)
    _ai_config(monkeypatch, rank_top_n=2, rank_window=2, rank_patience=0, rank_budget=2,
               refine_top_n=2, refine_first_pass=2, refine_max_passes=2, prefilter_min_score=30)
    spy_rank["scores"] = {"Queued Developer 00": 90, "Queued Developer 01": 88}

    result = runner.invoke(cli_mod.app, ["run", "arbeitnow"])
    assert result.exit_code == 0, result.output

    # round 1 ranks 00 and 01 (boundary 88); the refine pass scores them 90/80, so the effective
    # boundary falls to 84 and the second round pays for the next window of the queue
    assert "run: the top 2 boundary fell 88 → 84 in the refine pass; ranking further (round 2 of 2)" \
        in result.output
    assert spy_rank["scored"] == ["Queued Developer 00", "Queued Developer 01",
                                  "Queued Developer 02", "Queued Developer 03"]


def test_run_stops_after_refine_when_the_boundary_held(data_dir, fake_http, spy_rank, spy_refine,
                                                       monkeypatch):
    _queued_jobs(6)
    _ai_config(monkeypatch, rank_top_n=2, rank_window=2, rank_patience=0, rank_budget=2,
               refine_top_n=2, refine_first_pass=2, refine_max_passes=3, prefilter_min_score=30)
    # the refine pass scores its two jobs 90 and 80, which lifts a pair ranked 60/50
    spy_rank["scores"] = {"Queued Developer 00": 60, "Queued Developer 01": 50}

    result = runner.invoke(cli_mod.app, ["run", "arbeitnow"])
    assert result.exit_code == 0, result.output
    assert "run: the top 2 boundary held at 65 through the refine pass" in result.output
    assert spy_rank["scored"] == ["Queued Developer 00", "Queued Developer 01"]


def test_run_stops_when_the_refine_passes_are_spent(data_dir, fake_http, spy_rank, spy_refine,
                                                    monkeypatch):
    _queued_jobs(6)
    _ai_config(monkeypatch, rank_top_n=2, rank_window=2, rank_patience=0, rank_budget=2,
               refine_top_n=2, refine_first_pass=2, refine_max_passes=1, prefilter_min_score=30)
    spy_rank["scores"] = {"Queued Developer 00": 90, "Queued Developer 01": 88}

    result = runner.invoke(cli_mod.app, ["run", "arbeitnow"])
    assert result.exit_code == 0, result.output
    assert "ai.refine_max_passes (1) is spent; stopping here" in result.output
    assert len(spy_rank["scored"]) == 2


def test_run_stops_when_a_further_round_finds_nothing_to_rank(data_dir, fake_http, spy_rank,
                                                              spy_refine, monkeypatch):
    """The boundary fell, but the queue is empty: there is nothing left to look at."""
    _queued_jobs(2)
    _ai_config(monkeypatch, rank_top_n=2, rank_window=2, rank_patience=0, rank_budget=0,
               refine_top_n=2, refine_first_pass=2, refine_max_passes=3, prefilter_min_score=30)
    spy_rank["scores"] = {"Queued Developer 00": 90, "Queued Developer 01": 88}

    result = runner.invoke(cli_mod.app, ["run", "arbeitnow"])
    assert result.exit_code == 0, result.output
    assert "run: nothing new was ranked; the shortlist is final" in result.output


def test_run_skips_refine_with_skip_ai(data_dir, fake_http, spy_ai, spy_refine):
    result = runner.invoke(cli_mod.app, ["run", "arbeitnow", "--skip-ai"])
    assert result.exit_code == 0, result.output
    assert "refine:" not in result.output
    assert "jobs" not in spy_refine


# ------------------------------------------------- "what's new" between reports


def _seed_verdict(store, job, stage: str, score: int, **kw) -> None:
    """Store an AI verdict by hand so `report` renders a ranked/prefilter section without the API."""
    from jobscraper.ai.prompts import PROMPT_VERSION
    from jobscraper.models import AIVerdict

    store.save_verdict(AIVerdict(
        job_id=job.id, stage=stage,
        model="claude-opus-5" if stage == "rank" else "claude-sonnet-5",
        prompt_version=PROMPT_VERSION, relevant=True, score=score,
        language_ok=True, seniority_ok=True, location_ok=True,
        summary=f"The {stage} stage likes this one.", **kw))


def _job(store, prefix: str):
    return next(j for j in store.jobs() if j.title.startswith(prefix))


def _scrape_and_filter(data_dir):
    assert runner.invoke(cli_mod.app, ["scrape", "arbeitnow"]).exit_code == 0
    assert runner.invoke(cli_mod.app, ["filter"]).exit_code == 0


def test_report_writes_a_diff_file_and_prepends_the_new_section(data_dir, fake_http):
    _scrape_and_filter(data_dir)
    store = store_mod.Store()
    try:
        _seed_verdict(store, _job(store, "DevOps"), "rank", 88, why_apply=["Kubernetes work"])
    finally:
        store.close()

    first = runner.invoke(cli_mod.app, ["report"])
    assert first.exit_code == 0, first.output
    assert "first report in this database: 1 ranked, 0 prefilter" in first.output

    diffs = list((data_dir / "results").glob("diff-*.md"))
    assert len(diffs) == 1
    diff_text = diffs[0].read_text(encoding="utf-8")
    assert "First report in this database — nothing to diff against." in diff_text
    assert "### 88 · [DevOps Engineer (m/w/d)]" in diff_text

    report_md = next((data_dir / "results").glob("report-*.md")).read_text(encoding="utf-8")
    assert "## New in this report" in report_md
    assert report_md.index("## New in this report") < report_md.index("## Ranked (Opus)")
    assert "1 new ranked · 0 new in prefilter · 0 dropped out of ranked" in report_md

    # a second report over the same state: nothing new, but a prefilter survivor appears
    store = store_mod.Store()
    try:
        _seed_verdict(store, _job(store, "Core Developer"), "prefilter", 61)
    finally:
        store.close()

    second = runner.invoke(cli_mod.app, ["report"])
    assert second.exit_code == 0, second.output
    assert "new since report #1: 0 ranked, 1 prefilter" in second.output
    assert second.output.rstrip().splitlines()[-1].startswith("new since report #1")

    diff_text = next((data_dir / "results").glob("diff-*.md")).read_text(encoding="utf-8")
    assert "0 new ranked · 1 new in prefilter · 0 dropped out of ranked since report #1" in diff_text
    assert "| 61 | [Core Developer - Platform]" in diff_text

    report_md = next((data_dir / "results").glob("report-*.md")).read_text(encoding="utf-8")
    assert "## New since report #1" in report_md
    assert "### 88 · [DevOps Engineer (m/w/d)]" in report_md  # still in the ranked section
    assert report_md.count("### 88 · [DevOps Engineer (m/w/d)]") == 1  # not repeated at the top


def test_diff_command_compares_the_two_latest_reports(data_dir, fake_http, tmp_path):
    _scrape_and_filter(data_dir)
    assert runner.invoke(cli_mod.app, ["report"]).exit_code == 0     # report #1: nothing ranked

    store = store_mod.Store()
    try:
        _seed_verdict(store, _job(store, "DevOps"), "rank", 88, why_apply=["Kubernetes work"])
    finally:
        store.close()
    assert runner.invoke(cli_mod.app, ["report"]).exit_code == 0     # report #2: one ranked

    result = runner.invoke(cli_mod.app, ["diff"])
    assert result.exit_code == 0, result.output
    assert "1 new ranked · 0 new in prefilter · 0 dropped out of ranked since report #1" in result.output
    assert "DevOps Engineer (m/w/d)" in result.output
    assert "Kubernetes work" in result.output

    out = tmp_path / "elsewhere" / "whats-new.md"
    written = runner.invoke(cli_mod.app, ["diff", "--out", str(out)])
    assert written.exit_code == 0, written.output
    text = out.read_text(encoding="utf-8")
    assert "1 new ranked · 0 new in prefilter · 0 dropped out of ranked since report #1" in text
    assert "### 88 · [DevOps Engineer (m/w/d)]" in text


def test_diff_command_takes_explicit_report_ids(data_dir, fake_http):
    _scrape_and_filter(data_dir)
    assert runner.invoke(cli_mod.app, ["report"]).exit_code == 0     # #1
    store = store_mod.Store()
    try:
        _seed_verdict(store, _job(store, "DevOps"), "rank", 88)
    finally:
        store.close()
    assert runner.invoke(cli_mod.app, ["report"]).exit_code == 0     # #2
    assert runner.invoke(cli_mod.app, ["report"]).exit_code == 0     # #3, same as #2

    same = runner.invoke(cli_mod.app, ["diff", "--report", "3", "--against", "2"])
    assert same.exit_code == 0, same.output
    assert "0 new ranked · 0 new in prefilter · 0 dropped out of ranked since report #2" in same.output

    across = runner.invoke(cli_mod.app, ["diff", "--report", "3", "--against", "1"])
    assert across.exit_code == 0, across.output
    assert "1 new ranked · 0 new in prefilter · 0 dropped out of ranked since report #1" in across.output


def test_diff_command_without_any_reports_says_so(data_dir, fake_http):
    result = runner.invoke(cli_mod.app, ["diff"])
    assert result.exit_code == 0, result.output
    assert "no report" in result.output.lower()


def test_diff_command_rejects_an_unknown_report_id(data_dir, fake_http):
    _scrape_and_filter(data_dir)
    assert runner.invoke(cli_mod.app, ["report"]).exit_code == 0
    result = runner.invoke(cli_mod.app, ["diff", "--report", "999"])
    assert result.exit_code == 0, result.output
    assert "no report" in result.output.lower()


# ------------------------------------------------------------------ headless browser


class _FakeFactory:
    """Stands in for the BrowserFactory cli._browser() builds: hands out one FakeBrowser."""

    def __init__(self, routes):
        self.browser = FakeBrowser(routes)
        self.closed = False

    def __call__(self):
        return self.browser

    def close(self):
        self.closed = True


def test_browser_switch_reads_the_http_block(data_dir):
    assert cli_mod._browser(SimpleNamespace(http={"browser": "never"})) is None
    factory = cli_mod._browser(SimpleNamespace(http={}))
    assert isinstance(factory, BrowserFactory)
    assert factory.kwargs == {"headless": True, "timeout": 45.0, "min_delay": 1.0}
    tuned = cli_mod._browser(SimpleNamespace(http={"browser_headless": False, "browser_timeout": 10}))
    assert tuned.kwargs["headless"] is False and tuned.kwargs["timeout"] == 10.0


def test_probe_marks_the_sources_that_needed_the_browser(data_dir, monkeypatch):
    """jobly is disabled in the config, but naming it explicitly probes it anyway."""
    factory = _FakeFactory({"/tyopaikat": "jobly_search.html", "/tyopaikka/": "jobly_detail.html"})
    monkeypatch.setattr(cli_mod, "_browser", lambda settings: factory)
    monkeypatch.setattr(cli_mod, "_http", lambda settings: FakeHttp({}))  # no plain request allowed

    result = runner.invoke(cli_mod.app, ["probe", "jobly", "--limit", "2"])
    assert result.exit_code == 0, result.output
    assert "✓ jobly" in result.output
    assert "(browser)" in result.output
    assert factory.closed is True  # Chromium is not left running


def test_probe_does_not_mark_sources_that_did_not_need_it(data_dir, fake_http, monkeypatch):
    factory = _FakeFactory({})
    monkeypatch.setattr(cli_mod, "_browser", lambda settings: factory)
    result = runner.invoke(cli_mod.app, ["probe", "arbeitnow", "--limit", "1"])
    assert result.exit_code == 0, result.output
    assert "✓ arbeitnow" in result.output
    assert "(browser)" not in result.output
    assert factory.browser.calls == []


def test_enabled_defaults_to_every_source_the_config_does_not_disable(data_dir):
    from jobscraper.config import load_settings

    settings = load_settings()
    names = cli_mod._enabled(settings, [])
    assert "arbeitnow" in names
    assert all(settings.sources.get(n) is None or settings.sources[n].enabled for n in names)


def test_probe_reports_a_source_that_returns_nothing(data_dir, monkeypatch):
    monkeypatch.setattr(cli_mod, "_browser", lambda settings: None)
    monkeypatch.setattr(cli_mod, "_http", lambda settings: FakeHttp({"job-board-api": {"data": []}}))
    result = runner.invoke(cli_mod.app, ["probe", "arbeitnow", "--limit", "2"])

    assert result.exit_code == 0, result.output
    assert "0 jobs" in result.output
    assert "0 sources OK, 1 failed" in result.output
    assert "arbeitnow: returned 0 jobs" in result.output


def test_probe_reports_a_source_that_raises(data_dir, monkeypatch):
    monkeypatch.setattr(cli_mod, "_browser", lambda settings: None)
    monkeypatch.setattr(cli_mod, "_http", lambda settings: FakeHttp({}))  # every request fails
    result = runner.invoke(cli_mod.app, ["probe", "arbeitnow", "--limit", "1"])

    assert result.exit_code == 0, result.output  # a dead source is a report, not a crash
    assert "✗ arbeitnow" in result.output
    assert "0 sources OK, 1 failed" in result.output


def test_scrape_logs_a_failing_source_and_carries_on(data_dir, monkeypatch):
    monkeypatch.setattr(cli_mod, "_browser", lambda settings: None)
    monkeypatch.setattr(cli_mod, "_http", lambda settings: FakeHttp({}))
    result = runner.invoke(cli_mod.app, ["scrape", "arbeitnow"])

    assert result.exit_code == 0, result.output
    assert "✗ arbeitnow" in result.output
    store = store_mod.Store()
    try:
        rows = [tuple(r) for r in store.conn.execute("SELECT source, fetched, new FROM runs")]
    finally:
        store.close()
    assert rows == [("arbeitnow", 0, 0)]


def test_filter_marks_duplicates_as_drops(data_dir, fake_http):
    from jobscraper.models import Job

    store = store_mod.Store()
    try:
        store.upsert_jobs([
            Job(source="linkedin", source_id="a", url="https://x.test/1", country="FI",
                title="Junior Backend Developer", company="Acme Oy"),
            Job(source="arbeitnow", source_id="b", url="https://x.test/2", country="FI",
                title="Junior Backend Developer", company="Acme"),
        ])
    finally:
        store.close()

    result = runner.invoke(cli_mod.app, ["filter"])
    assert result.exit_code == 0, result.output
    assert "1 duplicate groups" in result.output

    store = store_mod.Store()
    try:
        dropped = [r for r in store.filter_results().values() if r.status == "drop"]
    finally:
        store.close()
    assert any("duplicate of" in r.reasons[0] for r in dropped)


def test_prefilter_prints_each_verdict_and_honours_max_jobs(data_dir, fake_http, monkeypatch):
    from jobscraper.ai import client as ai_client

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    seen: dict[str, int] = {}

    def init(self, stage, profile, store, model=None) -> None:
        self.stage = stage

    def run(self, jobs, filters, **kw):
        seen["jobs"] = len(list(jobs))
        kw["progress"](SimpleNamespace(score=87, relevant=True, summary="A good junior match"))
        return {}

    monkeypatch.setattr(ai_client.AIStage, "__init__", init)
    monkeypatch.setattr(ai_client.AIStage, "run", run)

    runner.invoke(cli_mod.app, ["scrape", "arbeitnow", "--limit", "2"])
    runner.invoke(cli_mod.app, ["filter"])
    result = runner.invoke(cli_mod.app, ["prefilter", "--max-jobs", "1"])

    assert result.exit_code == 0, result.output
    assert seen["jobs"] == 1
    assert "A good junior match" in result.output


def test_serve_with_a_single_database_has_no_serve_directory(data_dir, monkeypatch, tmp_path):
    calls = _fake_uvicorn(monkeypatch)
    db = tmp_path / "copy.db"
    db.touch()
    result = runner.invoke(cli_mod.app, ["serve", "--db-file", str(db)])

    assert result.exit_code == 0, result.output
    assert "serving copies from" not in result.output
    assert hasattr(calls["app"], "routes")


def test_serve_with_reload_and_a_single_database_sets_no_serve_dir(data_dir, monkeypatch, tmp_path):
    import os

    monkeypatch.delenv("JOBSCRAPER_SERVE_DIR", raising=False)
    calls = _fake_uvicorn(monkeypatch)
    db = tmp_path / "copy.db"
    db.touch()
    result = runner.invoke(cli_mod.app, ["serve", "--reload", "--db-file", str(db)])

    assert result.exit_code == 0, result.output
    assert calls["app"] == "jobscraper.web.app:create_app"
    assert "JOBSCRAPER_SERVE_DIR" not in os.environ


def test_publish_without_a_database_is_rejected(data_dir):
    result = runner.invoke(cli_mod.app, ["publish"])
    assert result.exit_code != 0
    assert "does not exist" in result.output


# ----------------------------------------------------------- public run statistics


@pytest.fixture
def stats_dir(tmp_path, monkeypatch):
    """Where `report` is allowed to write the committed, public counters."""
    directory = tmp_path / "public-stats"
    monkeypatch.setenv("JOBSCRAPER_STATS_DIR", str(directory))
    return directory


def test_report_records_public_run_statistics(data_dir, fake_http, stats_dir):
    assert runner.invoke(cli_mod.app, ["scrape", "arbeitnow", "--limit", "2"]).exit_code == 0
    assert runner.invoke(cli_mod.app, ["filter"]).exit_code == 0

    result = runner.invoke(cli_mod.app, ["report"])
    assert result.exit_code == 0, result.output
    assert "run stats" in result.output

    rows = [json.loads(ln) for ln in (stats_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["report_id"] == 1
    assert rows[0]["profile"] == "default"
    assert rows[0]["jobs_total"] == 2
    assert rows[0]["by_source"] == {"arbeitnow": 2}
    assert rows[0]["scrape"] == {"sources": 1, "fetched": 2, "new": 2, "errors": 0}
    assert "DevOps" not in (stats_dir / "runs.jsonl").read_text(encoding="utf-8")
    assert "#1" in (stats_dir / "README.md").read_text(encoding="utf-8")

    # a second report is a second row; re-reporting the same one does not duplicate it
    assert runner.invoke(cli_mod.app, ["report"]).exit_code == 0
    rows = [json.loads(ln) for ln in (stats_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["report_id"] for r in rows] == [1, 2]


def test_report_survives_a_broken_stats_hook(data_dir, fake_http, stats_dir, monkeypatch, caplog):
    from jobscraper import runstats

    def boom(*a, **kw):
        raise RuntimeError("no disk")

    monkeypatch.setattr(runstats, "collect", boom)
    assert runner.invoke(cli_mod.app, ["scrape", "arbeitnow", "--limit", "2"]).exit_code == 0
    assert runner.invoke(cli_mod.app, ["filter"]).exit_code == 0

    result = runner.invoke(cli_mod.app, ["report"])
    assert result.exit_code == 0, result.output
    assert "report #1" in result.output          # the report itself still happened
    assert not (stats_dir / "runs.jsonl").exists()
    assert "no disk" in caplog.text


def test_stats_public_renders_the_markdown_without_a_database(data_dir, stats_dir):
    stats_dir.mkdir(parents=True)
    (stats_dir / "runs.jsonl").write_text(
        json.dumps({"report_id": 4, "profile": "default", "jobs_total": 7,
                    "report_created_at": "2026-09-09T10:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )
    result = runner.invoke(cli_mod.app, ["stats", "--public"])
    assert result.exit_code == 0, result.output
    assert "#4" in result.output
    assert "#4" in (stats_dir / "README.md").read_text(encoding="utf-8")
    assert not (data_dir / "jobs.db").exists()  # no database was opened


def test_stats_public_without_any_rows_says_so(data_dir, stats_dir):
    result = runner.invoke(cli_mod.app, ["stats", "--public"])
    assert result.exit_code == 0, result.output
    assert "no runs recorded yet" in result.output.lower()
