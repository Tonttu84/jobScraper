"""`jobscraper compare-verdicts`: two sets of verdicts for the same jobs, side by side.

Small hand-built verdict sets, so every number can be checked by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobscraper import audit, compare
from jobscraper.models import AIVerdict, Job, RunNote

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _job(n: int) -> Job:
    return Job(source="test", source_id=str(n), url=f"https://example.test/{n}",
               title=f"Developer {n}", company=f"Co {n}", country="FI")


def _v(job: Job, score: int, *, stage: str = "rank", relevant: bool = True, version: str = "old",
       at: datetime = T0, criteria: dict | None = None) -> AIVerdict:
    return AIVerdict(job_id=job.id, stage=stage, model="m", prompt_version=version,
                     relevant=relevant, score=score, language_ok=True, seniority_ok=True,
                     location_ok=True, summary="s", criteria=criteria or {}, created_at=at)


def _note(job_id: str | None, at: datetime, kind: str = "profile_gap") -> RunNote:
    return RunNote(created_at=at, stage="rank", kind=kind, job_id=job_id, message="m")


JOBS = [_job(n) for n in range(6)]


@pytest.fixture
def sides():
    """Jobs 0-3 on both sides, 4 only old, 5 only new, plus an old verdict for a job not in JOBS."""
    old = {j.id: _v(j, s) for j, s in zip(JOBS[:5], [80, 60, 40, 20, 50])}
    old["aged-out"] = _v(_job(99), 90)
    new = {j.id: _v(j, s, version="new", at=T0 + timedelta(days=1))
           for j, s in zip([*JOBS[:4], JOBS[5]],[70, 70, 50, 20, 99])}
    return old, new


def test_counts_are_restricted_to_the_jobs_in_the_window(sides):
    old, new = sides
    out = compare.compare_verdicts(old, new, JOBS, stage="rank")
    assert out["counts"] == {"old_only": 1, "new_only": 1, "both": 4}


def test_mean_sd_and_median_of_new_minus_old(sides):
    old, new = sides
    out = compare.compare_verdicts(old, new, JOBS, stage="rank")
    # deltas: -10, +10, +10, 0
    assert out["delta"]["mean"] == pytest.approx(2.5)
    assert out["delta"]["sd"] == pytest.approx(9.574271, abs=1e-6)  # sample sd (n-1)
    assert out["delta"]["median"] == pytest.approx(5.0)


def test_delta_statistics_are_none_without_enough_pairs():
    one = compare.compare_verdicts({JOBS[0].id: _v(JOBS[0], 10)}, {JOBS[0].id: _v(JOBS[0], 30)},
                                   JOBS, stage="rank")
    assert one["delta"] == {"mean": 20.0, "sd": None, "median": 20.0}
    none = compare.compare_verdicts({}, {}, JOBS, stage="rank")
    assert none["delta"] == {"mean": None, "sd": None, "median": None}
    assert none["spearman"] is None and none["top"]["jaccard"] is None
    assert none["notes"] == {"since": None, "total": 0, "by_kind": {}}


def test_relevant_flips_are_counted_each_way():
    old = {j.id: _v(j, 50, relevant=r) for j, r in zip(JOBS[:4], [True, True, False, True])}
    new = {j.id: _v(j, 50, relevant=r) for j, r in zip(JOBS[:4], [False, True, True, False])}
    out = compare.compare_verdicts(old, new, JOBS, stage="prefilter")
    assert out["flips"] == {"true_to_false": 2, "false_to_true": 1}


def test_top_overlap_and_spearman_for_the_ranking_stages(sides):
    old, new = sides
    out = compare.compare_verdicts(old, new, JOBS, stage="rank", top=2)
    # old top 2 of the intersection: jobs 0, 1; new: 0 and 1 tie at 70, job 2 at 50 -> {0, 1}
    assert out["top"] == {"n": 2, "jaccard": pytest.approx(1.0)}
    assert out["spearman"] == pytest.approx(audit.spearman([80, 60, 40, 20], [70, 70, 50, 20]))

    refine = compare.compare_verdicts(old, new, JOBS, stage="refine", top=1)
    assert refine["top"]["n"] == 1 and refine["spearman"] is not None


def test_the_screen_gets_no_top_overlap_or_spearman(sides):
    old, new = sides
    out = compare.compare_verdicts(old, new, JOBS, stage="prefilter")
    assert out["top"] is None and out["spearman"] is None


def test_top_jaccard_counts_a_partial_overlap():
    old = {j.id: _v(j, s) for j, s in zip(JOBS[:4], [90, 80, 10, 5])}
    new = {j.id: _v(j, s) for j, s in zip(JOBS[:4], [90, 10, 80, 5])}
    out = compare.compare_verdicts(old, new, JOBS, stage="rank", top=2)
    assert out["top"]["jaccard"] == pytest.approx(1 / 3)  # {0,1} vs {0,2}


def test_the_biggest_movers_either_way_come_first_and_ten_at_most():
    jobs = [_job(n) for n in range(14)]
    old = {j.id: _v(j, 50) for j in jobs}
    new = {j.id: _v(j, 50 + (n if n % 2 else -n)) for n, j in enumerate(jobs)}  # 0 does not move
    out = compare.compare_verdicts(old, new, jobs, stage="rank")
    movers = out["movers"]
    assert len(movers) == 10
    assert [m["delta"] for m in movers[:3]] == [13, -12, 11]
    assert movers[0] == {"job_id": jobs[13].id, "title": "Developer 13", "company": "Co 13",
                         "old": 50, "new": 63, "delta": 13}
    assert all(m["delta"] for m in movers)


def test_a_job_that_did_not_move_is_no_mover(sides):
    old, new = sides
    out = compare.compare_verdicts(old, new, JOBS, stage="rank")
    assert JOBS[3].id not in {m["job_id"] for m in out["movers"]}
    assert len(out["movers"]) == 3


def test_criteria_deltas_per_key_where_both_sides_have_criteria():
    old = {JOBS[0].id: _v(JOBS[0], 50, criteria={"stack_fit": 60, "pay": 40}),
           JOBS[1].id: _v(JOBS[1], 50, criteria={"stack_fit": 80}),
           JOBS[2].id: _v(JOBS[2], 50)}
    new = {JOBS[0].id: _v(JOBS[0], 50, criteria={"stack_fit": 70, "pay": 20, "domain": 90}),
           JOBS[1].id: _v(JOBS[1], 50, criteria={"stack_fit": 60, "pay": 10}),
           JOBS[2].id: _v(JOBS[2], 50, criteria={"stack_fit": 0})}
    out = compare.compare_verdicts(old, new, JOBS, stage="rank")
    assert out["criteria"] == {"stack_fit": {"n": 2, "mean_delta": pytest.approx(-5.0)},
                               "pay": {"n": 1, "mean_delta": pytest.approx(-20.0)}}


def test_criteria_are_only_compared_for_the_rank_stage():
    old = {JOBS[0].id: _v(JOBS[0], 50, stage="refine", criteria={"pay": 1})}
    new = {JOBS[0].id: _v(JOBS[0], 50, stage="refine", criteria={"pay": 9})}
    assert compare.compare_verdicts(old, new, JOBS, stage="refine")["criteria"] == {}


def test_notes_on_the_intersection_since_the_old_sides_newest_verdict(sides):
    old, new = sides
    old[JOBS[1].id] = _v(JOBS[1], 60, at=T0 + timedelta(hours=5))  # the old side's newest
    since = T0 + timedelta(hours=5)
    notes = [_note(JOBS[0].id, since - timedelta(minutes=1)),        # before: not counted
             _note(JOBS[0].id, since + timedelta(hours=1)),
             _note(JOBS[2].id, since + timedelta(hours=2), "missing_input"),
             _note(JOBS[2].id, since + timedelta(hours=3)),
             _note(JOBS[4].id, since + timedelta(hours=1)),          # old-only job
             _note(None, since + timedelta(hours=1), "source_count_drop")]
    out = compare.compare_verdicts(old, new, JOBS, stage="rank", notes=notes)
    assert out["notes"] == {"since": since, "total": 3,
                            "by_kind": {"profile_gap": 2, "missing_input": 1}}


def test_a_small_intersection_is_flagged(sides):
    old, new = sides
    assert compare.compare_verdicts(old, new, JOBS, stage="rank")["small"] is True
    jobs = [_job(n) for n in range(10)]
    many = {j.id: _v(j, 50) for j in jobs}
    assert compare.compare_verdicts(many, many, jobs, stage="rank")["small"] is False


def test_load_comparison_reads_both_sides_from_the_store(tmp_path):
    from jobscraper.store import Store

    store = Store(tmp_path / "c.db")
    try:
        store.upsert_jobs(JOBS[:3])
        for j, s in zip(JOBS[:3], [10, 20, 30]):
            store.save_verdict(_v(j, s, version="v1").model_copy(update={"profile_hash": "aaa"}))
            store.save_verdict(_v(j, s + 5, version="v1", at=T0 + timedelta(days=1))
                               .model_copy(update={"model": "m2", "profile_hash": "bbb"}))
            store.save_verdict(_v(j, s + 50, version="v2", at=T0 + timedelta(days=2)))
        store.add_note("rank", "profile_gap", "x", job_id=JOBS[0].id)

        by_hash = compare.load_comparison(store, JOBS[:3], stage="rank", old_hash="aaa", new_hash="bbb")
        assert by_hash["counts"]["both"] == 3 and by_hash["delta"]["mean"] == pytest.approx(5.0)
        assert by_hash["notes"]["total"] == 1  # written now, after the old side's newest verdict

        by_version = compare.load_comparison(store, JOBS[:3], stage="rank", old_version="v1",
                                             new_version="v2")
        # the latest v1 verdict per job is the "bbb" one (+5), so v2 sits 45 above it
        assert by_version["delta"]["mean"] == pytest.approx(45.0)

        both = compare.load_comparison(store, JOBS[:3], stage="rank", old_version="v1", old_hash="aaa",
                                       new_version="v1", new_hash="bbb")
        assert both["delta"]["mean"] == pytest.approx(5.0)
        assert compare.load_comparison(store, JOBS[:3], stage="rank", old_version="v2",
                                       old_hash="aaa", new_version="v2")["counts"]["both"] == 0
    finally:
        store.close()


def test_spearman_is_the_one_shared_implementation():
    assert compare.spearman is audit.spearman
    assert compare.spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)


def test_top_set_and_jaccard_helpers():
    assert compare.top_set({"a": 5, "b": 9, "c": 9}, 2) == {"b", "c"}
    assert compare.top_set({"a": 5, "b": 9, "c": 9}, 1) == {"b"}  # the id breaks the tie
    assert compare.jaccard({"a"}, {"a", "b"}) == pytest.approx(0.5)
    assert compare.jaccard(set(), set()) == 0.0
