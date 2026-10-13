"""The ``run_notes`` table, its Store API, and the scrape-side producers in :mod:`jobscraper.health`."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from jobscraper import health
from jobscraper.models import Job, ReportSnapshot, RunNote
from jobscraper.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "jobs.db")
    yield s
    s.close()


def _job(n: int, description: str | None = "x" * 300, source: str = "remotive") -> Job:
    return Job(source=source, source_id=str(n), url=f"https://jobs.example.test/{n}",
               title=f"Developer {n}", company="Example Oy", description=description)


# ------------------------------------------------------------------ table + API


def test_add_note_returns_its_id_and_notes_reads_it_back(store):
    first = store.add_note("scrape", "source_error", "RuntimeError: down", source="jobicy")
    second = store.add_note("filter", "language_borderline", "pl at 0.80", job_id="abc",
                            data={"language": "pl", "confidence": 0.8})
    assert second > first

    notes = store.notes()
    assert [n.id for n in notes] == [first, second]
    assert isinstance(notes[0], RunNote)
    assert (notes[0].stage, notes[0].kind, notes[0].source, notes[0].job_id) == (
        "scrape", "source_error", "jobicy", None)
    assert notes[0].message == "RuntimeError: down" and notes[0].data is None
    assert notes[1].data == {"language": "pl", "confidence": 0.8}
    assert notes[1].created_at.tzinfo is not None


def test_notes_filter_by_stage_kind_and_time(store):
    store.add_note("scrape", "source_error", "a")
    store.add_note("scrape", "source_count_drop", "b")
    store.add_note("filter", "language_borderline", "c")
    later = datetime.now(UTC) + timedelta(hours=1)

    assert [n.message for n in store.notes(stage="scrape")] == ["a", "b"]
    assert [n.message for n in store.notes(kind="language_borderline")] == ["c"]
    assert [n.message for n in store.notes(stage="scrape", kind="source_error")] == ["a"]
    assert store.notes(since=later) == []
    assert len(store.notes(since=datetime.now(UTC) - timedelta(hours=1))) == 3


def test_notes_since_a_naive_datetime_reads_it_as_utc(store):
    store.add_note("scrape", "source_error", "a")
    naive_past = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None)
    assert len(store.notes(since=naive_past)) == 1


def test_note_counts_group_by_stage_and_kind(store):
    store.add_note("scrape", "empty_descriptions", "a")
    store.add_note("scrape", "empty_descriptions", "b")
    store.add_note("report", "pass_disagreement", "c")
    assert store.note_counts(None) == {("scrape", "empty_descriptions"): 2,
                                       ("report", "pass_disagreement"): 1}
    assert store.note_counts(datetime.now(UTC) + timedelta(hours=1)) == {}


def test_an_older_database_gains_the_run_notes_table_when_it_is_opened(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE reports (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,"
                 " days INTEGER NOT NULL, prompt_version TEXT NOT NULL, counts TEXT NOT NULL,"
                 " cost TEXT NOT NULL, path TEXT, refine_offset REAL)")
    conn.execute("INSERT INTO reports (created_at, days, prompt_version, counts, cost)"
                 " VALUES ('2026-09-01T00:00:00+00:00', 7, 'v1', '{}', '{}')")
    conn.commit()
    conn.close()

    store = Store(path)
    try:
        store.add_note("scrape", "source_error", "a")
        assert len(store.notes()) == 1
        assert store.report().note_counts == {}   # a report written before the column existed
    finally:
        store.close()


def test_a_readonly_copy_without_the_table_has_no_notes(tmp_path):
    path = tmp_path / "old.db"
    sqlite3.connect(path).close()
    ro = Store(path, readonly=True)
    try:
        assert ro.notes() == []
        assert ro.note_counts(None) == {}
    finally:
        ro.close()


def test_a_report_keeps_its_note_counts(store):
    saved = store.save_report(ReportSnapshot(days=7, prompt_version="v1",
                                             note_counts={"source_error": 2}))
    assert store.report(saved.id).note_counts == {"source_error": 2}
    assert store.reports()[0].note_counts == {"source_error": 2}


# ------------------------------------------------------------- previous good run


def test_last_good_run_skips_errors_empty_runs_and_later_runs(store):
    good = store.log_run("remotive", 120, 5)
    store.log_run("remotive", 0, 0, "RuntimeError: down")
    store.log_run("remotive", 0, 0)          # unfinished or empty: no baseline
    store.log_run("himalayas", 300, 1)       # another source
    current = store.start_run("remotive")
    later = store.log_run("remotive", 999, 0)

    prev = store.last_good_run("remotive", current)
    assert prev is not None and prev["id"] == good and prev["fetched"] == 120
    assert store.last_good_run("remotive", good) is None
    assert store.last_good_run("remotive", later)["fetched"] == 120


def test_a_run_flagged_for_its_count_is_never_the_baseline(store):
    """Otherwise a listing that stays broken would read as healthy from its second bad run on,
    and the takedown counter would start charging misses against it."""
    store.log_run("remotive", 120, 5)
    bad = store.start_run("remotive")
    store.finish_run(bad, 10, 0)
    assert health.check_count(store, "remotive", bad, 10) == 120
    still_bad = store.start_run("remotive")
    store.finish_run(still_bad, 12, 0)
    assert health.check_count(store, "remotive", still_bad, 12) == 120
    assert len(store.notes(kind="source_count_drop")) == 2


# --------------------------------------------------------- scrape producers


def test_count_drop_fires_below_half_of_a_previous_run_of_twenty_or_more(store):
    store.log_run("remotive", 120, 5)
    run_id = store.start_run("remotive")
    assert health.check_count(store, "remotive", run_id, 59) == 120
    [note] = store.notes(kind="source_count_drop")
    assert (note.stage, note.source) == ("scrape", "remotive")
    assert note.data == {"fetched": 59, "previous": 120, "previous_run_id": 1, "run_id": run_id}
    assert "59" in note.message and "120" in note.message


@pytest.mark.parametrize(("previous", "fetched"), [(120, 60), (19, 0), (None, 0)])
def test_count_drop_stays_quiet_at_half_on_a_small_baseline_or_with_none(store, previous, fetched):
    if previous is not None:
        store.log_run("remotive", previous, 0)
    run_id = store.start_run("remotive")
    assert health.check_count(store, "remotive", run_id, fetched) is None
    assert store.notes() == []


def test_source_error_note_carries_the_error_text(store):
    health.note_source_error(store, "jobicy", "RuntimeError: the board is down")
    [note] = store.notes()
    assert (note.stage, note.kind, note.source) == ("scrape", "source_error", "jobicy")
    assert note.message == "RuntimeError: the board is down"


def test_takedown_skipped_note_names_both_counts(store):
    health.note_takedown_skipped(store, "teamtailor", fetched=3, previous=80)
    [note] = store.notes()
    assert (note.kind, note.source) == ("takedown_skipped", "teamtailor")
    assert "3" in note.message and "80" in note.message
    assert note.data == {"fetched": 3, "previous": 80}


def test_empty_descriptions_fire_at_exactly_a_quarter(store):
    jobs = [_job(n) for n in range(15)] + [_job(n, None) for n in range(15, 20)]
    assert health.check_descriptions(store, "remotive", jobs) == pytest.approx(0.25)
    assert len(store.notes(kind="empty_descriptions")) == 1


def test_empty_descriptions_count_missing_and_short_ones(store):
    jobs = [_job(n) for n in range(7)] + [_job(7, None), _job(8, "short"), _job(9, "y" * 199)]
    share = health.check_descriptions(store, "remotive", jobs)
    assert share == pytest.approx(0.3)
    [note] = store.notes(kind="empty_descriptions")
    assert note.source == "remotive"
    assert note.data == {"thin": 3, "missing": 1, "fetched": 10, "share": 0.3}
    assert "3 of 10" in note.message and "30%" in note.message


@pytest.mark.parametrize("jobs", [
    [_job(n) for n in range(8)] + [_job(8, None), _job(9, "z" * 200)],   # 10 %, 200 chars is enough
    [_job(n, None) for n in range(9)],                                    # under ten fetched
])
def test_empty_descriptions_stay_quiet_under_the_share_or_the_size(store, jobs):
    assert health.check_descriptions(store, "remotive", jobs) is None
    assert store.notes() == []
