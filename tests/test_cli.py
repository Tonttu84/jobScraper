"""End-to-end tests for the Typer CLI, with the network faked and the data dir in tmp_path.

The AI commands (``prefilter``/``rank``) are deliberately not exercised for real here — they would
call the Anthropic API; only the flag plumbing is checked, against a stubbed ``AIStage``.
Everything up to ``report`` runs for real against a temporary SQLite file.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from jobscraper import cli as cli_mod
from jobscraper import store as store_mod
from jobscraper.models import FilterResult
from tests.conftest import FakeHttp

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
    result = runner.invoke(cli_mod.app, ["run", "arbeitnow", "--batch"])
    assert result.exit_code == 0, result.output
    assert spy_ai["run_batch:prefilter"]["wait"] is True
    assert "run:rank" in spy_ai  # ranking still goes through the live path
    assert "run_batch:rank" not in spy_ai


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
