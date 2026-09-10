"""Tests for :mod:`jobscraper.store` against a throwaway SQLite file."""

from __future__ import annotations

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
from jobscraper.store import Store


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
