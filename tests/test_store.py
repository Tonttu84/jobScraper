"""Tests for :mod:`jobscraper.store` against a throwaway SQLite file."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobscraper.models import AIVerdict, FilterResult, Job
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
    keep = FilterResult(job_id="a", status="keep", signals={"seniority": "entry_by_title"},
                        location_tier=1)
    review = FilterResult(job_id="b", status="review", reasons=["asks for 3 years"],
                          location_tier=2)
    drop = FilterResult(job_id="c", status="drop", reasons=["senior-level title"])
    store.save_filter_results([keep, review, drop], "test-rules")

    everything = store.filter_results()
    assert set(everything) == {"a", "b", "c"}
    assert everything["a"].signals == {"seniority": "entry_by_title"}
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


def test_store_creates_parent_directories(tmp_path):
    s = Store(tmp_path / "nested" / "deeper" / "jobs.db")
    try:
        assert s.path.exists()
        assert s.stats() == {"jobs_per_source": {}, "filter": {}, "ai": {}}
    finally:
        s.close()
