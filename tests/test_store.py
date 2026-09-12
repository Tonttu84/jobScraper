"""Tests for :mod:`jobscraper.store` against a throwaway SQLite file."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from jobscraper.models import (
    AIVerdict,
    Decision,
    FilterResult,
    Job,
    JobFacets,
    ReportItem,
    ReportSnapshot,
)
from jobscraper.store import Store, still_listed


def make_job(source: str = "arbeitnow", source_id: str = "1", **kw) -> Job:
    base = {
        "source": source,
        "source_id": source_id,
        "url": f"https://jobs.example.test/{source}/{source_id}",
        "title": "Junior Software Developer",
        "company": "Example Oy",
        "description": "We build backend services in Python and Go.",
        "country": "FI",
        "city": "Helsinki",
        "remote": "onsite",
    }
    base.update(kw)
    return Job(**base)


def make_verdict(job_id: str, **kw) -> AIVerdict:
    base = {
        "job_id": job_id,
        "stage": "prefilter",
        "model": "claude-sonnet-5",
        "prompt_version": "v1",
        "relevant": True,
        "score": 70,
        "language_ok": True,
        "seniority_ok": True,
        "location_ok": True,
        "summary": "Looks like a solid junior backend role.",
    }
    base.update(kw)
    return AIVerdict(**base)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "jobs.db")
    yield s
    s.close()


def test_upsert_counts_only_new_jobs_and_refreshes_last_seen(store):
    jobs = [make_job(source_id="1"), make_job(source_id="2")]
    assert store.upsert_jobs(jobs) == 2

    first = store.job_meta(jobs[0].id)
    assert first is not None and first["first_seen"] == first["last_seen"]

    assert store.upsert_jobs(jobs) == 0  # already known
    second = store.job_meta(jobs[0].id)
    assert second["first_seen"] == first["first_seen"]
    assert second["last_seen"] > first["last_seen"]
    assert len(store.jobs()) == 2


def test_upsert_is_idempotent_per_id_and_updates_title(store):
    job = make_job(source_id="7")
    store.upsert_jobs([job])
    renamed = make_job(source_id="7", title="Junior Software Developer (m/f/d)")
    assert renamed.id == job.id
    assert store.upsert_jobs([renamed]) == 0
    (stored,) = store.jobs()
    assert stored.title == "Junior Software Developer (m/f/d)"


def test_jobs_filters_by_source_and_ids(store):
    a = make_job(source="arbeitnow", source_id="1")
    b = make_job(source="teamtailor", source_id="2")
    store.upsert_jobs([a, b])

    assert {j.source for j in store.jobs(source="arbeitnow")} == {"arbeitnow"}
    assert len(store.jobs(source="teamtailor")) == 1
    assert store.jobs(source="nope") == []
    assert [j.id for j in store.jobs(ids=[b.id])] == [b.id]
    assert store.jobs(ids=[]) == []
    assert len(store.jobs(seen_within_days=1)) == 2


def test_job_meta_is_none_for_unknown_job(store):
    assert store.job_meta("does-not-exist") is None


def test_filter_results_round_trip_and_status_filter(store):
    keep = FilterResult(job_id="a", status="keep", signals={"seniority": "kept_by_title"},
                        location_tier=1)
    review = FilterResult(job_id="b", status="review", reasons=["asks for 3 years"],
                          location_tier=2)
    drop = FilterResult(job_id="c", status="drop", reasons=["senior-level title"])
    store.save_filter_results([keep, review, drop], "test-rules")

    everything = store.filter_results()
    assert set(everything) == {"a", "b", "c"}
    assert everything["a"].signals == {"seniority": "kept_by_title"}
    assert everything["a"].location_tier == 1
    assert everything["b"].reasons == ["asks for 3 years"]

    assert set(store.filter_results("keep")) == {"a"}
    assert set(store.filter_results(["keep", "review"])) == {"a", "b"}
    assert set(store.filter_results(["drop"])) == {"c"}


def test_saving_filter_results_twice_replaces_them(store):
    store.save_filter_results([FilterResult(job_id="a", status="review")], "v1")
    store.save_filter_results([FilterResult(job_id="a", status="keep")], "v2")
    results = store.filter_results()
    assert len(results) == 1
    assert results["a"].status == "keep"


def test_verdicts_return_the_latest_per_job_and_stage(store):
    now = datetime.now(UTC)
    old = make_verdict("a", prompt_version="v1", score=40, created_at=now - timedelta(hours=2))
    new = make_verdict("a", prompt_version="v2", score=90, created_at=now)
    ranked = make_verdict("a", stage="rank", model="claude-opus-5", prompt_version="v2", score=77,
                          created_at=now)
    for v in (new, old, ranked):  # deliberately out of order
        store.save_verdict(v)

    prefilter = store.verdicts("prefilter")
    assert set(prefilter) == {"a"}
    assert prefilter["a"].score == 90  # latest created_at wins

    assert store.verdicts("prefilter", "v1")["a"].score == 40
    assert store.verdicts("rank")["a"].score == 77
    assert store.verdicts("rank", "v1") == {}


def test_verdicts_accept_several_compatible_prompt_versions(store):
    """A wording change that keeps the scoring scale must not hide the verdicts before it."""
    now = datetime.now(UTC)
    old = make_verdict("a", prompt_version="v1", score=40, created_at=now - timedelta(hours=2))
    new = make_verdict("a", prompt_version="v2", score=90, created_at=now)
    other = make_verdict("b", prompt_version="v3", score=55, created_at=now)
    for v in (old, new, other):
        store.save_verdict(v)

    both = store.verdicts("prefilter", ("v2", "v1"))
    assert set(both) == {"a"}          # "v3" is not compatible: job b is invisible
    assert both["a"].score == 90       # latest wins across the compatible versions

    assert store.verdicts("prefilter", ("v1",))["a"].score == 40
    assert store.verdicts("prefilter", "v1")["a"].score == 40   # a bare string is unchanged
    assert store.verdicts("prefilter", ("v9", "v8")) == {}


def test_save_verdict_replaces_the_same_key(store):
    store.save_verdict(make_verdict("a", score=10))
    store.save_verdict(make_verdict("a", score=55, summary="Second opinion."))
    verdicts = store.verdicts("prefilter")
    assert len(verdicts) == 1
    assert verdicts["a"].score == 55
    assert verdicts["a"].summary == "Second opinion."


def test_log_run_and_stats_counters(store):
    store.upsert_jobs([make_job(source="arbeitnow", source_id="1"),
                       make_job(source="arbeitnow", source_id="2"),
                       make_job(source="teamtailor", source_id="3")])
    store.save_filter_results(
        [FilterResult(job_id="x", status="keep"), FilterResult(job_id="y", status="drop"),
         FilterResult(job_id="z", status="drop")],
        "test-rules",
    )
    store.save_verdict(make_verdict("x"))

    store.log_run("arbeitnow", fetched=2, new=2)
    store.log_run("teamtailor", fetched=0, new=0, error="SourceHTTPError: 403")
    rows = list(store.conn.execute("SELECT source, fetched, new, error FROM runs ORDER BY id"))
    assert [tuple(r) for r in rows] == [
        ("arbeitnow", 2, 2, None),
        ("teamtailor", 0, 0, "SourceHTTPError: 403"),
    ]

    stats = store.stats()
    assert stats["jobs_per_source"] == {"arbeitnow": 2, "teamtailor": 1}
    assert stats["filter"] == {"keep": 1, "drop": 2}
    assert stats["ai"] == {"prefilter": 1}


def test_delete_verdicts_by_stage_and_for_every_stage(store):
    """Hydration re-screens a job: its old verdicts have to go, one stage or both."""
    store.save_verdict(make_verdict("a"))
    store.save_verdict(make_verdict("a", stage="rank", model="claude-opus-5", score=88))
    store.save_verdict(make_verdict("b"))

    assert store.delete_verdicts(["a"], stage="prefilter") == 1
    assert set(store.verdicts("prefilter")) == {"b"}
    assert set(store.verdicts("rank")) == {"a"}  # the other stage is untouched

    assert store.delete_verdicts(["a", "b"]) == 2  # both stages, both jobs
    assert store.verdicts("prefilter") == {}
    assert store.verdicts("rank") == {}


def test_delete_verdicts_with_nothing_to_delete(store):
    store.save_verdict(make_verdict("a"))
    assert store.delete_verdicts([]) == 0
    assert store.delete_verdicts(["missing"]) == 0
    assert set(store.verdicts("prefilter")) == {"a"}


def test_store_creates_parent_directories(tmp_path):
    s = Store(tmp_path / "nested" / "deeper" / "jobs.db")
    try:
        assert s.path.exists()
        assert s.stats() == {"jobs_per_source": {}, "filter": {}, "ai": {},
                             "decisions": {}, "reports": 0}
    finally:
        s.close()


# --------------------------------------------------------------------- facets


def make_facets(job_id: str, **kw) -> JobFacets:
    base = {
        "job_id": job_id,
        "facets_version": "v1",
        "posting_language": "en",
        "languages_required": ["en"],
        "languages_optional": ["fi"],
        "stacks": ["python", "go"],
        "web_dev": True,
    }
    base.update(kw)
    return JobFacets(**base)


def test_facets_round_trip_and_id_filter(store):
    a = make_facets("a")
    b = make_facets("b", posting_language="fi", stacks=["react"], web_dev=False,
                    languages_required=["fi"], languages_optional=[])
    store.save_facets([a, b])

    everything = store.facets()
    assert set(everything) == {"a", "b"}
    assert everything["a"].stacks == ["python", "go"]
    assert everything["a"].languages_optional == ["fi"]
    assert everything["a"].web_dev is True
    assert everything["b"].web_dev is False
    assert everything["b"].posting_language == "fi"

    assert set(store.facets(ids=["b"])) == {"b"}
    assert store.facets(ids=[]) == {}
    assert store.facets(ids=["nope"]) == {}


def test_save_facets_replaces_existing_row(store):
    store.save_facets([make_facets("a", stacks=["python"])])
    store.save_facets([make_facets("a", stacks=["rust"], facets_version="v2")])
    facets = store.facets()
    assert len(facets) == 1
    assert facets["a"].stacks == ["rust"]
    assert facets["a"].facets_version == "v2"


def test_save_facets_with_empty_list_is_a_no_op(store):
    store.save_facets([])
    assert store.facets() == {}


# ------------------------------------------------------------------ decisions


def test_save_decision_round_trip_and_replace(store):
    stored = store.save_decision(Decision(job_id="a", user="tont", status="interested"))
    assert stored.job_id == "a"
    assert stored.status == "interested"
    assert stored.updated_at.tzinfo is not None

    (only,) = store.decisions()
    assert only.status == "interested"
    assert only.note is None

    later = store.save_decision(Decision(job_id="a", user="tont", status="applied",
                                         note="sent CV"))
    decisions = store.decisions()
    assert len(decisions) == 1
    assert decisions[0].status == "applied"
    assert decisions[0].note == "sent CV"
    assert later.updated_at >= stored.updated_at


def test_save_decision_stamps_updated_at_now(store):
    stale = datetime(2020, 1, 1, tzinfo=UTC)
    stored = store.save_decision(Decision(job_id="a", user="tont", status="skipped",
                                          updated_at=stale))
    assert stored.updated_at > stale
    assert store.decisions()[0].updated_at > stale


def test_decisions_are_per_user(store):
    store.save_decision(Decision(job_id="a", user="tont", status="applied"))
    store.save_decision(Decision(job_id="a", user="mira", status="skipped"))
    store.save_decision(Decision(job_id="b", user="mira", status="interview"))

    assert len(store.decisions()) == 3
    assert {d.job_id for d in store.decisions(user="mira")} == {"a", "b"}
    assert [d.status for d in store.decisions(user="tont")] == ["applied"]
    assert store.decisions(user="nobody") == []
    assert store.decision_users() == ["mira", "tont"]


def test_decisions_are_newest_first(store):
    store.save_decision(Decision(job_id="a", user="tont", status="interested"))
    store.save_decision(Decision(job_id="b", user="tont", status="applied"))
    store.save_decision(Decision(job_id="c", user="tont", status="rejected"))
    updated = [d.updated_at for d in store.decisions()]
    assert updated == sorted(updated, reverse=True)


def test_delete_decision(store):
    store.save_decision(Decision(job_id="a", user="tont", status="applied"))
    store.save_decision(Decision(job_id="a", user="mira", status="skipped"))

    assert store.delete_decision("a", "tont") is True
    assert store.delete_decision("a", "tont") is False
    assert store.delete_decision("nope", "tont") is False
    assert [d.user for d in store.decisions()] == ["mira"]


def test_decision_users_is_empty_without_decisions(store):
    assert store.decision_users() == []


# -------------------------------------------------------------------- reports


def make_snapshot(**kw) -> ReportSnapshot:
    base = {
        "days": 7,
        "prompt_version": "v1",
        "counts": {"jobs": 3, "keep": 2, "review": 1, "drop": 0, "prefiltered": 2, "ranked": 1},
        "cost": {"total": 1.25},
        "path": "/tmp/report.md",
        "items": [
            ReportItem(job_id="a", section="ranked", position=1, score=91),
            ReportItem(job_id="b", section="prefilter", position=1, score=55),
            ReportItem(job_id="c", section="review", position=1, score=None),
        ],
    }
    base.update(kw)
    return ReportSnapshot(**base)


#: The reports table as it was before the refine pass had a scale to calibrate, plus the items
#: table, so a database written by an older version can be opened here.
_PRE_OFFSET_SCHEMA = """
CREATE TABLE reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    days INTEGER NOT NULL,
    prompt_version TEXT NOT NULL,
    counts TEXT NOT NULL,
    cost TEXT NOT NULL,
    path TEXT
);
CREATE TABLE report_items (
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL,
    section TEXT NOT NULL,
    position INTEGER NOT NULL,
    score INTEGER,
    PRIMARY KEY (report_id, job_id)
);
INSERT INTO reports (created_at, days, prompt_version, counts, cost, path)
VALUES ('2026-09-10T06:00:00+00:00', 7, 'v1', '{}', '{}', '/tmp/old.md');
"""


def _pre_offset_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(_PRE_OFFSET_SCHEMA)
    conn.commit()
    conn.close()
    return path


def test_save_report_keeps_the_calibration_offset(store):
    saved = store.save_report(make_snapshot(refine_offset=-21.4))
    assert saved.refine_offset == -21.4
    assert store.report(saved.id).refine_offset == -21.4
    assert store.reports()[0].refine_offset == -21.4


def test_a_report_written_without_a_refine_pass_has_no_offset(store):
    saved = store.save_report(make_snapshot())
    assert store.report(saved.id).refine_offset is None


def test_an_older_database_gains_the_offset_column_when_it_is_opened(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` cannot widen a table that exists: the open migrates it."""
    store = Store(_pre_offset_db(tmp_path / "old.db"))
    try:
        assert store.report().refine_offset is None  # the row written before the column existed
        saved = store.save_report(make_snapshot(refine_offset=12.5))
        assert store.report(saved.id).refine_offset == 12.5
    finally:
        store.close()


def test_a_readonly_store_reads_a_database_it_cannot_migrate(tmp_path):
    """A read-only copy is opened as it stands, so the offset simply reads as unknown."""
    store = Store(_pre_offset_db(tmp_path / "ro.db"), readonly=True)
    try:
        assert store.report().refine_offset is None
    finally:
        store.close()


def test_save_report_returns_a_copy_with_an_id(store):
    snap = make_snapshot()
    saved = store.save_report(snap)
    assert saved.id is not None
    assert snap.id is None  # the argument is not mutated
    assert saved.days == 7
    assert len(saved.items) == 3


def test_report_returns_the_latest_with_items_in_order(store):
    store.save_report(make_snapshot(prompt_version="old"))
    second = store.save_report(make_snapshot(prompt_version="new", days=14))

    latest = store.report()
    assert latest is not None
    assert latest.id == second.id
    assert latest.prompt_version == "new"
    assert latest.days == 14
    assert latest.counts == {"jobs": 3, "keep": 2, "review": 1, "drop": 0, "prefiltered": 2,
                             "ranked": 1}
    assert latest.cost == {"total": 1.25}
    assert latest.path == "/tmp/report.md"
    assert [(i.section, i.position, i.job_id, i.score) for i in latest.items] == [
        ("prefilter", 1, "b", 55),
        ("ranked", 1, "a", 91),
        ("review", 1, "c", None),
    ]


def test_report_by_id_and_item_position_order(store):
    first = store.save_report(make_snapshot(items=[
        ReportItem(job_id="z", section="ranked", position=2, score=10),
        ReportItem(job_id="y", section="ranked", position=1, score=80),
    ]))
    store.save_report(make_snapshot(prompt_version="newer"))

    fetched = store.report(first.id)
    assert fetched is not None
    assert fetched.id == first.id
    assert [i.job_id for i in fetched.items] == ["y", "z"]
    assert [i.position for i in fetched.items] == [1, 2]
    assert store.report(9999) is None


def test_reports_lists_newest_first_without_items(store):
    store.save_report(make_snapshot(prompt_version="old"))
    newest = store.save_report(make_snapshot(prompt_version="new"))

    listing = store.reports()
    assert [r.id for r in listing] == [newest.id, newest.id - 1]
    assert [r.prompt_version for r in listing] == ["new", "old"]
    assert all(r.items == [] for r in listing)
    assert listing[0].counts["jobs"] == 3


def test_report_on_an_empty_database_is_none(store):
    assert store.report() is None
    assert store.reports() == []


def test_report_before_returns_the_previous_report_with_items(store):
    first = store.save_report(make_snapshot(prompt_version="first"))
    second = store.save_report(make_snapshot(prompt_version="second", items=[
        ReportItem(job_id="b", section="ranked", position=1, score=80),
    ]))
    third = store.save_report(make_snapshot(prompt_version="third"))

    before = store.report_before(third.id)
    assert before is not None
    assert before.id == second.id
    assert before.prompt_version == "second"
    assert [i.job_id for i in before.items] == ["b"]

    assert store.report_before(first.id) is None      # nothing older than the first
    assert store.report_before(9999).id == third.id   # ids need not exist


def test_report_before_on_an_empty_database_is_none(store):
    assert store.report_before(1) is None


def test_deleting_a_report_cascades_to_its_items(store):
    saved = store.save_report(make_snapshot())
    assert store.conn.execute("SELECT COUNT(*) FROM report_items").fetchone()[0] == 3

    with store.tx() as c:
        c.execute("DELETE FROM reports WHERE id=?", (saved.id,))
    assert store.conn.execute("SELECT COUNT(*) FROM report_items").fetchone()[0] == 0
    assert store.report() is None


def test_stats_includes_decisions_and_reports(store):
    store.save_decision(Decision(job_id="a", user="tont", status="applied"))
    store.save_decision(Decision(job_id="b", user="tont", status="applied"))
    store.save_decision(Decision(job_id="c", user="mira", status="skipped"))
    store.save_report(make_snapshot())

    stats = store.stats()
    assert stats["decisions"] == {"applied": 2, "skipped": 1}
    assert stats["reports"] == 1


def test_store_survives_being_used_from_another_thread(store):
    """FastAPI enters a sync generator dependency in one threadpool worker and then calls the
    endpoint in another, so a connection pinned to its creating thread makes the web app fail
    with a 500 whenever the two workers differ."""
    store.upsert_jobs([make_job()])
    seen: list[object] = []

    def read() -> None:
        try:
            seen.append(len(store.jobs()))
        except Exception as exc:  # the failure mode under test
            seen.append(exc)

    thread = threading.Thread(target=read)
    thread.start()
    thread.join(5)
    assert seen == [1]


def test_ai_batches_round_trip(store):
    store.save_batch("msgbatch_1", "prefilter", "claude-sonnet-5", "v1", ["a", "b"])
    store.save_batch("msgbatch_2", "rank", "claude-opus-5", "v1", ["c"])

    pending = store.pending_batches("prefilter")
    assert [b["id"] for b in pending] == ["msgbatch_1"]
    assert pending[0]["job_ids"] == ["a", "b"]
    assert pending[0]["model"] == "claude-sonnet-5" and pending[0]["prompt_version"] == "v1"
    assert pending[0]["status"] == "submitted" and pending[0]["created_at"]
    assert len(store.pending_batches()) == 2

    store.finish_batch("msgbatch_1", "done")
    assert store.pending_batches("prefilter") == []
    assert [b["id"] for b in store.pending_batches("prefilter", status="done")] == ["msgbatch_1"]


def test_latest_runs_returns_the_newest_row_per_source(store):
    store.log_run("arbeitnow", 10, 4)
    store.log_run("arbeitnow", 12, 0)
    store.log_run("jobly", 0, 0, "HTTPError: 503")

    rows = store.latest_runs()
    assert [r["source"] for r in rows] == ["arbeitnow", "jobly"]  # ordered by source
    assert (rows[0]["fetched"], rows[0]["new"], rows[0]["error"]) == (12, 0, None)
    assert rows[1]["error"] == "HTTPError: 503"
    assert rows[0]["started_at"]


def test_latest_runs_is_empty_without_any_scrape(store):
    assert store.latest_runs() == []


def test_new_jobs_since_counts_by_first_seen(store):
    store.upsert_jobs([make_job(source_id="1")])
    cut = store.job_meta(store.jobs()[0].id)["first_seen"]
    store.upsert_jobs([make_job(source_id="2"), make_job(source_id="3")])

    assert store.new_jobs_since(None) == 3
    assert store.new_jobs_since(cut) == 3  # the boundary row itself counts (>=)
    assert store.new_jobs_since(datetime.now(UTC) + timedelta(days=1)) == 0


# ------------------------------------------------ postings a board no longer lists


def test_start_run_returns_an_id_before_the_jobs_are_stored(store):
    """The run row has to exist first: its id is what the jobs are stamped with."""
    run_id = store.start_run("arbeitnow")
    row = store.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    assert (row["source"], row["fetched"], row["new"], row["error"]) == ("arbeitnow", 0, 0, None)
    assert row["started_at"]

    store.finish_run(run_id, fetched=7, new=3)
    row = store.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    assert (row["fetched"], row["new"], row["error"]) == (7, 3, None)


def test_finish_run_records_the_error_of_a_failed_source(store):
    run_id = store.start_run("jobly")
    store.finish_run(run_id, 0, 0, "SourceHTTPError: 503")
    row = store.conn.execute("SELECT fetched, new, error FROM runs WHERE id=?", (run_id,)).fetchone()
    assert tuple(row) == (0, 0, "SourceHTTPError: 503")


def test_log_run_still_writes_one_completed_row(store):
    """`log_run` is start+finish, so a caller that only wants the record keeps working."""
    run_id = store.log_run("arbeitnow", 5, 2)
    row = store.conn.execute("SELECT source, fetched, new, error FROM runs WHERE id=?", (run_id,)).fetchone()
    assert tuple(row) == ("arbeitnow", 5, 2, None)


def _gone_columns(store, job_id):
    row = store.conn.execute("SELECT last_run_id, missed_runs FROM jobs WHERE id=?", (job_id,)).fetchone()
    return (row["last_run_id"], row["missed_runs"])


def test_upsert_without_a_run_id_leaves_the_run_columns_alone(store):
    job = make_job(source_id="1")
    store.upsert_jobs([job])
    assert _gone_columns(store, job.id) == (None, 0)

    run_id = store.start_run("arbeitnow")
    store.upsert_jobs([job], run_id)
    store.mark_missing("arbeitnow", store.start_run("arbeitnow"))
    assert _gone_columns(store, job.id) == (run_id, 1)

    store.upsert_jobs([job])  # a caller that knows nothing about runs must not reset the counter
    assert _gone_columns(store, job.id) == (run_id, 1)


def test_upsert_with_a_run_id_stamps_new_and_known_jobs(store):
    run_id = store.start_run("arbeitnow")
    job = make_job(source_id="1")
    assert store.upsert_jobs([job], run_id) == 1
    assert _gone_columns(store, job.id) == (run_id, 0)

    later = store.start_run("arbeitnow")
    assert store.upsert_jobs([job], later) == 0
    assert _gone_columns(store, job.id) == (later, 0)


def test_mark_missing_counts_only_the_jobs_of_that_source(store):
    first = store.start_run("arbeitnow")
    kept = make_job(source="arbeitnow", source_id="1")
    dropped = make_job(source="arbeitnow", source_id="2")
    other = make_job(source="teamtailor", source_id="3")
    store.upsert_jobs([kept, dropped], first)
    store.upsert_jobs([other], store.start_run("teamtailor"))

    second = store.start_run("arbeitnow")
    store.upsert_jobs([kept], second)
    assert store.mark_missing("arbeitnow", second) == 1

    assert _gone_columns(store, kept.id) == (second, 0)
    assert _gone_columns(store, dropped.id) == (first, 1)
    assert _gone_columns(store, other.id)[1] == 0  # another source's run says nothing about it


def test_mark_missing_counts_a_job_that_predates_the_mechanism(store):
    """A row written before ``last_run_id`` existed reads as NULL, which is not this run."""
    job = make_job(source_id="1")
    store.upsert_jobs([job])
    assert store.mark_missing("arbeitnow", store.start_run("arbeitnow")) == 1
    assert _gone_columns(store, job.id) == (None, 1)


def test_a_job_that_comes_back_starts_counting_from_zero_again(store):
    job = make_job(source_id="1")
    store.upsert_jobs([job], store.start_run("arbeitnow"))
    for _ in range(2):
        store.mark_missing("arbeitnow", store.start_run("arbeitnow"))
    assert store.gone_ids(1) == {job.id}

    back = store.start_run("arbeitnow")
    store.upsert_jobs([job], back)
    assert _gone_columns(store, job.id) == (back, 0)
    assert store.gone_ids(1) == set()


def test_gone_ids_at_the_threshold_and_with_the_mechanism_off(store):
    once = make_job(source_id="1")
    twice = make_job(source_id="2")
    store.upsert_jobs([once, twice], store.start_run("arbeitnow"))
    for _ in range(2):  # two complete runs that only `once` came back in
        run_id = store.start_run("arbeitnow")
        store.upsert_jobs([once], run_id)
        store.mark_missing("arbeitnow", run_id)

    assert store.gone_ids(1) == {twice.id}
    assert store.gone_ids(2) == {twice.id}
    assert store.gone_ids(3) == set()
    assert store.gone_ids(0) == set()   # 0 turns the mechanism off
    assert store.gone_ids(-1) == set()


def test_still_listed_keeps_everything_the_board_still_shows(store):
    here = make_job(source_id="1")
    gone = make_job(source_id="2")
    store.upsert_jobs([here, gone], store.start_run("arbeitnow"))
    store.mark_missing("arbeitnow", store.start_run("arbeitnow"))
    store.upsert_jobs([here], store.start_run("arbeitnow"))

    jobs = store.jobs()
    assert {j.id for j in still_listed(jobs, store, 1)} == {here.id}
    assert {j.id for j in still_listed(jobs, store, 0)} == {here.id, gone.id}
    # the row itself is untouched: a lookup by id still finds it
    assert [j.id for j in store.jobs(ids=[gone.id])] == [gone.id]


#: The jobs table as it was before postings could be marked as no longer listed.
_PRE_GONE_SCHEMA = """
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    country TEXT,
    city TEXT,
    remote TEXT,
    posted_at TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    data TEXT NOT NULL
);
"""


def test_an_older_database_gains_the_gone_columns_when_it_is_opened(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` cannot widen a table that exists: the open migrates it."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(_PRE_GONE_SCHEMA)
    job = make_job(source_id="1")
    conn.execute(
        "INSERT INTO jobs (id, source, source_id, url, title, first_seen, last_seen, data)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (job.id, job.source, job.source_id, job.url, job.title, "2026-09-01T00:00:00+00:00",
         "2026-09-01T00:00:00+00:00", job.model_dump_json()),
    )
    conn.commit()
    conn.close()

    store = Store(path)
    try:
        assert _gone_columns(store, job.id) == (None, 0)
        assert store.gone_ids(1) == set()
        assert store.mark_missing("arbeitnow", store.start_run("arbeitnow")) == 1
        assert store.gone_ids(1) == {job.id}
    finally:
        store.close()
