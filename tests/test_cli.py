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

    store = store_mod.Store()
    try:
        results = store.filter_results()
        assert len(results) == 2
        by_title = {j.id: j.title for j in store.jobs()}
        statuses = {by_title[jid]: r.status for jid, r in results.items()}
        assert statuses["DevOps Engineer (m/w/d)"] == "keep"
        assert statuses["Senior Python Engineer, Platform Libraries (m/f/d)"] == "drop"
    finally:
        store.close()

    reported = runner.invoke(cli_mod.app, ["report"])
    assert reported.exit_code == 0, reported.output

    reports = list((data_dir / "reports").glob("report-*.md"))
    assert len(reports) == 1
    assert "# Job report" in reports[0].read_text(encoding="utf-8")

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


def test_short_verbose_flag_is_accepted(data_dir, fake_http):
    """README documents ``-v`` as the short form of ``--verbose`` on every command that logs."""
    for args in (["probe", "-v", "arbeitnow"], ["scrape", "-v", "arbeitnow"], ["filter", "-v"]):
        result = runner.invoke(cli_mod.app, args)
        assert result.exit_code == 0, f"{args}: {result.output}"
