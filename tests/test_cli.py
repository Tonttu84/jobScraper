"""End-to-end tests for the Typer CLI, with the network faked and the data dir in tmp_path.

The AI commands (``prefilter``/``rank``) are deliberately not exercised here — they would call
the Anthropic API. Everything up to ``report`` runs for real against a temporary SQLite file.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from jobscraper import cli as cli_mod
from jobscraper import report as report_mod
from jobscraper import store as store_mod
from jobscraper.models import FilterResult
from tests.conftest import FakeHttp

runner = CliRunner()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point every module-level DATA_DIR at tmp_path so nothing touches the repo's data/."""
    monkeypatch.setenv("JOBSCRAPER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("jobscraper.config.DATA_DIR", tmp_path)
    monkeypatch.setattr(store_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(report_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli_mod, "DATA_DIR", tmp_path)
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
        assert len(jobs) == 2  # the third fixture record is broken and skipped
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
        assert statuses["Junior Software Developer (m/w/d)"] == "keep"
        assert statuses["Senior Java Architect"] == "drop"
        # one facet row per non-duplicate job, with the TypeScript/React job flagged web_dev
        facets = store.facets()
        assert set(facets) == set(results)
        by_id = {j.id: j for j in store.jobs()}
        web = {by_id[jid].title for jid, f in facets.items() if f.web_dev}
        assert web == {"Junior Software Developer (m/w/d)"}
    finally:
        store.close()

    reported = runner.invoke(cli_mod.app, ["report"])
    assert reported.exit_code == 0, reported.output
    assert "report #1" in reported.output

    reports = list((data_dir / "reports").glob("report-*.md"))
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
    assert rec["title"] == "Junior Software Developer (m/w/d)"
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
    assert "web" in result.output  # the stack-tag table

    store = store_mod.Store()
    try:
        facets = store.facets()
        assert len(facets) == 2
        assert any(f.web_dev for f in facets.values())
        assert any("java" in f.stacks for f in facets.values())
    finally:
        store.close()


def test_report_backfills_missing_facets(data_dir, fake_http):
    """A report over a DB whose facets were wiped stores them again for its own items."""
    assert runner.invoke(cli_mod.app, ["scrape", "arbeitnow", "--limit", "2"]).exit_code == 0
    assert runner.invoke(cli_mod.app, ["filter"]).exit_code == 0

    store = store_mod.Store()
    try:  # a `review` job puts an item in the snapshot without running the AI stages
        job = next(j for j in store.jobs() if j.title.startswith("Junior"))
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
        assert stored[job.id].web_dev is True
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
