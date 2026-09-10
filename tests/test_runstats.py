"""Public, anonymous per-run statistics: the collected row, the JSONL file and the renderer.

Nothing here touches the repository's own ``stats/`` directory — ``conftest`` points
``JOBSCRAPER_STATS_DIR`` at ``tmp_path`` for every test.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from jobscraper import config, runstats
from jobscraper.config import Profile, Settings
from jobscraper.models import AIVerdict, FilterResult, Job, ReportSnapshot
from jobscraper.report import build_snapshot
from jobscraper.store import Store


def make_job(source: str, source_id: str, title: str = "Junior Backend Developer") -> Job:
    return Job(
        source=source,
        source_id=source_id,
        url=f"https://jobs.example.test/{source}/{source_id}",
        title=title,
        company="Example Oy",
        description="We build backend services in Python and Go.",
        country="FI",
        city="Helsinki",
        remote="onsite",
        posted_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


def make_verdict(job_id: str, stage: str, score: int, relevant: bool = True) -> AIVerdict:
    return AIVerdict(
        job_id=job_id,
        stage=stage,
        model="claude-opus-5" if stage == "rank" else "claude-sonnet-5",
        prompt_version="v1",
        relevant=relevant,
        score=score,
        language_ok=True,
        seniority_ok=True,
        location_ok=True,
        summary="Counts only, no posting text.",
    )


@pytest.fixture
def settings():
    """Plain defaults, so the row does not depend on the owner's private profile.yaml."""
    return Settings(profile=Profile(name="Test Candidate", summary="A test candidate."), sources={})


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "jobs.db")
    yield s
    s.close()


@pytest.fixture
def state(store):
    """A small database: three jobs, one of each rule status, two drops, one scrape per source."""
    keep = make_job("arbeitnow", "1")
    review = make_job("arbeitnow", "2", title="Software Developer")
    drop = make_job("jobly", "3", title="Head Chef")
    old = make_job("jobly", "4", title="Junior Developer")
    store.upsert_jobs([keep, review, drop, old])
    store.save_filter_results(
        [
            FilterResult(job_id=keep.id, status="keep"),
            FilterResult(job_id=review.id, status="review", reasons=["asks for 3 years of experience"]),
            FilterResult(job_id=drop.id, status="drop", reasons=["title not a software/IT role: chef"]),
            FilterResult(job_id=old.id, status="drop", reasons=["posting is 90 days old", "duplicate of x"]),
        ],
        "rules-test",
    )
    store.save_verdict(make_verdict(keep.id, "prefilter", 80))
    store.save_verdict(make_verdict(review.id, "prefilter", 5, relevant=False))
    store.save_verdict(make_verdict(keep.id, "rank", 91))
    store.log_run("arbeitnow", 10, 4)
    store.log_run("arbeitnow", 12, 0)  # the newest scrape of this source wins
    store.log_run("jobly", 0, 0, "HTTPError: 503")
    return store


def snapshot_for(store: Store, prompt_version: str = "v1") -> ReportSnapshot:
    jobs = store.jobs()
    filters = store.filter_results()
    pre = store.verdicts("prefilter", prompt_version)
    ranked = store.verdicts("rank", prompt_version)
    snap = build_snapshot(jobs, filters, pre, ranked, days=30, cost={"total": 1.25, "claude-opus-5": 1.25},
                          prompt_version=prompt_version)
    return store.save_report(snap)


# ------------------------------------------------------------------------- collect


def test_collect_builds_one_flat_anonymous_row(state, settings):
    snap = snapshot_for(state)
    row = runstats.collect(state, snap, settings, profile=None, usage_path=None)

    assert set(row) == {
        "recorded_at", "report_id", "report_created_at", "profile", "jobs_total", "new_jobs",
        "by_source", "rules", "drops_by_category", "prefilter", "ranked", "versions", "models",
        "cost_usd", "scrape",
    }
    assert row["report_id"] == snap.id
    assert row["report_created_at"] == snap.created_at.isoformat()
    assert row["profile"] == "default"
    assert row["jobs_total"] == 4
    assert row["by_source"] == {"arbeitnow": 2, "jobly": 2}
    assert row["rules"] == {"keep": 1, "review": 1, "drop": 2}
    assert row["drops_by_category"] == {"not_software_title": 1, "too_old": 1}
    assert row["prefilter"] == {"prefiltered": 2, "passed": 1}
    assert row["ranked"] == 1
    assert row["versions"]["prompt"] == "v1"
    assert row["versions"]["rules"]
    assert row["models"] == {"prefilter": settings.profile.ai.prefilter_model,
                             "rank": settings.profile.ai.rank_model}
    assert row["cost_usd"] == 1.25
    assert row["scrape"] == {"sources": 2, "fetched": 12, "new": 0, "errors": 1}
    datetime.fromisoformat(row["recorded_at"])  # parses


def test_collect_never_writes_posting_text(state, settings):
    row = runstats.collect(state, snapshot_for(state), settings, profile=None, usage_path=None)
    blob = json.dumps(row)
    for secret in ("Junior", "Chef", "Example Oy", "https://", "Helsinki", "chef"):
        assert secret not in blob


def test_collect_names_the_active_profile(state, settings):
    row = runstats.collect(state, snapshot_for(state), settings, profile="ana", usage_path=None)
    assert row["profile"] == "ana"


def test_new_jobs_counts_what_arrived_since_the_previous_report(store, settings):
    store.upsert_jobs([make_job("arbeitnow", "old")])
    snapshot_for(store)  # the previous report
    store.upsert_jobs([make_job("arbeitnow", "fresh-1"), make_job("arbeitnow", "fresh-2")])

    row = runstats.collect(store, snapshot_for(store), settings, profile=None, usage_path=None)
    assert row["jobs_total"] == 3
    assert row["new_jobs"] == 2


def test_without_a_previous_report_every_job_counts_as_new(state, settings):
    row = runstats.collect(state, snapshot_for(state), settings, profile=None, usage_path=None)
    assert row["new_jobs"] == row["jobs_total"] == 4


def test_an_unsaved_snapshot_has_no_previous_report(state, settings):
    """Defensive: ``collect`` is called after ``save_report``, but an id-less snapshot must work."""
    jobs, filters = state.jobs(), state.filter_results()
    snap = build_snapshot(jobs, filters, {}, {}, days=30, prompt_version="v1")
    row = runstats.collect(state, snap, settings, profile=None, usage_path=None)
    assert row["report_id"] is None
    assert row["new_jobs"] == 4


def test_collect_sums_tokens_per_stage_from_the_usage_file(state, settings, tmp_path):
    usage = tmp_path / "usage.jsonl"
    usage.write_text(
        '{"stage":"prefilter","chunk":"01","model":"sonnet","jobs":100,"tokens":1000,"seconds":9}\n'
        '{"stage":"prefilter","chunk":"02","model":"sonnet","jobs":100,"tokens":2500,"seconds":9}\n'
        '{"stage":"rank","chunk":"01","model":"opus","jobs":60,"tokens":700,"seconds":9}\n'
        '{"stage":"rank","chunk":"02"}\n'          # no token counter: ignored
        '{"chunk":"03","tokens":5}\n'              # no stage: ignored
        "\n",
        encoding="utf-8",
    )
    row = runstats.collect(state, snapshot_for(state), settings, profile=None, usage_path=usage)
    assert row["tokens_by_stage"] == {"prefilter": 3500, "rank": 700}


def test_collect_omits_tokens_when_the_usage_file_is_missing(state, settings, tmp_path):
    row = runstats.collect(state, snapshot_for(state), settings, profile=None,
                           usage_path=tmp_path / "nope.jsonl")
    assert "tokens_by_stage" not in row


def test_collect_omits_tokens_when_the_usage_file_has_none(state, settings, tmp_path):
    usage = tmp_path / "usage.jsonl"
    usage.write_text('{"stage":"prefilter","chunk":"01"}\n', encoding="utf-8")
    row = runstats.collect(state, snapshot_for(state), settings, profile=None, usage_path=usage)
    assert "tokens_by_stage" not in row


def test_scrape_block_is_empty_without_any_run_rows(store, settings):
    store.upsert_jobs([make_job("arbeitnow", "1")])
    row = runstats.collect(store, snapshot_for(store), settings, profile=None, usage_path=None)
    assert row["scrape"] == {"sources": 0, "fetched": 0, "new": 0, "errors": 0}


# -------------------------------------------------------------------------- record


def _row(report_id: int, profile: str = "default", **kw) -> dict:
    row = {"recorded_at": "2026-09-10T10:00:00+00:00", "report_id": report_id,
           "report_created_at": f"2026-09-{report_id:02d}T10:00:00+00:00", "profile": profile,
           "jobs_total": 10, "new_jobs": 1, "by_source": {"arbeitnow": 10},
           "rules": {"keep": 1, "review": 2, "drop": 7}, "drops_by_category": {"too_old": 7},
           "prefilter": {"prefiltered": 3, "passed": 2}, "ranked": 1,
           "versions": {"rules": "r1", "prompt": "v1"},
           "models": {"prefilter": "claude-sonnet-5", "rank": "claude-opus-5"},
           "cost_usd": 0.0, "scrape": {"sources": 1, "fetched": 10, "new": 1, "errors": 0}}
    row.update(kw)
    return row


def test_record_appends_rows(tmp_path):
    path = tmp_path / "runs.jsonl"
    runstats.record(_row(1), path)
    runstats.record(_row(2), path)
    rows = runstats.read_rows(path)
    assert [r["report_id"] for r in rows] == [1, 2]


def test_record_replaces_the_row_of_the_same_profile_and_report(tmp_path):
    path = tmp_path / "runs.jsonl"
    runstats.record(_row(1, jobs_total=10), path)
    runstats.record(_row(1, jobs_total=99), path)
    rows = runstats.read_rows(path)
    assert len(rows) == 1 and rows[0]["jobs_total"] == 99


def test_record_keeps_the_same_report_id_of_another_profile(tmp_path):
    path = tmp_path / "runs.jsonl"
    runstats.record(_row(1, profile="default"), path)
    runstats.record(_row(1, profile="ana"), path)
    assert {r["profile"] for r in runstats.read_rows(path)} == {"default", "ana"}


def test_read_rows_skips_blank_and_broken_lines(tmp_path, caplog):
    path = tmp_path / "runs.jsonl"
    path.write_text('{"report_id": 1}\n\nnot json\n{"report_id": 2}\n', encoding="utf-8")
    assert [r["report_id"] for r in runstats.read_rows(path)] == [1, 2]
    assert "unreadable" in caplog.text


def test_read_rows_of_a_missing_file_is_empty(tmp_path):
    assert runstats.read_rows(tmp_path / "nothing.jsonl") == []


# -------------------------------------------------------------------------- render


def test_render_of_an_empty_file_says_so(tmp_path):
    text = runstats.render(tmp_path / "runs.jsonl")
    assert "no runs recorded yet" in text.lower()
    assert "counts only" in text.lower()


def test_render_tables_are_newest_first_and_per_profile(tmp_path):
    path = tmp_path / "runs.jsonl"
    runstats.record(_row(1, report_created_at="2026-09-01T10:00:00+00:00"), path)
    runstats.record(_row(2, report_created_at="2026-09-09T10:00:00+00:00", jobs_total=42), path)
    runstats.record(_row(7, profile="ana", report_created_at="2026-09-08T10:00:00+00:00",
                          drops_by_category={"location": 3, "too_old": 5}), path)

    text = runstats.render(path)
    assert "## default" in text and "## ana" in text
    assert text.index("## default") < text.index("## ana")  # the unnamed profile leads
    rows = [ln for ln in text.splitlines() if ln.startswith("| 2026-09-")]
    assert rows[0].startswith("| 2026-09-09")  # newest first inside the profile
    assert "| 42 |" in rows[0]
    assert "#2" in rows[0]
    # the drop breakdown covers the newest row of each profile only
    assert "| ana | too_old | 5 |" in text
    assert "| default | too_old | 7 |" in text
    assert "| ana | location | 3 |" in text


def test_render_survives_a_row_from_an_older_schema(tmp_path):
    path = tmp_path / "runs.jsonl"
    path.write_text(json.dumps({"report_id": 3, "profile": "default"}) + "\n", encoding="utf-8")
    text = runstats.render(path)
    assert "#3" in text


def test_render_notes_when_a_run_recorded_no_drops(tmp_path):
    path = tmp_path / "runs.jsonl"
    runstats.record(_row(1, drops_by_category={}), path)
    text = runstats.render(path)
    assert "#1" in text
    assert "no rule drops" in text.lower()


# ------------------------------------------------------------------- file locations


def test_paths_follow_the_stats_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSCRAPER_STATS_DIR", str(tmp_path / "public"))
    assert config.stats_dir() == tmp_path / "public"
    assert runstats.runs_path() == tmp_path / "public" / "runs.jsonl"
    assert runstats.readme_path() == tmp_path / "public" / "README.md"


def test_the_stats_directory_ignores_the_active_profile(tmp_path, monkeypatch):
    """The file is committed and shared: --profile must not move it."""
    monkeypatch.setenv("JOBSCRAPER_STATS_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("JOBSCRAPER_CONFIG_DIR", str(tmp_path / "config"))
    directory = tmp_path / "config" / "profiles" / "ana"
    directory.mkdir(parents=True)
    for name in ("profile.yaml", "sources.yaml"):
        (directory / name).write_text("name: ana\nsummary: s\n", encoding="utf-8")
    config.use_profile("ana")
    assert config.stats_dir() == tmp_path / "public"


def test_stats_dir_defaults_to_the_repository_root(monkeypatch):
    monkeypatch.delenv("JOBSCRAPER_STATS_DIR", raising=False)
    assert config.stats_dir() == config.ROOT / "stats"


def test_refresh_writes_the_markdown_next_to_the_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSCRAPER_STATS_DIR", str(tmp_path / "public"))
    runstats.record(_row(1), runstats.runs_path())
    out = runstats.refresh()
    assert out == tmp_path / "public" / "README.md"
    assert "#1" in out.read_text(encoding="utf-8")
