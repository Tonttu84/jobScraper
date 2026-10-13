"""Sentinels: a small random sample of each gate's rejects, judged by the stage after the gate.

The rule filter's drops never reach an AI and the screen's rejects never reach the ranker, so
neither gate's false rejects show up anywhere else; ``eval`` only sees what passed both.
"""

from __future__ import annotations

from datetime import date

import pytest

from jobscraper.ai.prompts import job_prompt, profile_fingerprint, system_prompt
from jobscraper.models import SENTINEL_STAGES, AIVerdict, FilterResult, Job, mirrored_stage
from jobscraper.sentinel import (
    default_seed,
    rule_drop_gate,
    sample_rule_drops,
    sample_screen_rejects,
    screen_gate,
    sentinel_prefix,
)


def _job(n: int, title: str = "Junior Developer") -> Job:
    return Job(source="test", source_id=str(n), url=f"https://example.test/{n}", title=f"{title} {n:02d}",
               company="Acme", country="FI", description="Go backend work. English.")


def _verdict(job_id: str, stage: str, score: int, *, relevant: bool = True, **kw) -> AIVerdict:
    return AIVerdict(job_id=job_id, stage=stage, model="m", prompt_version="v", relevant=relevant,
                     score=score, language_ok=True, seniority_ok=True, location_ok=True,
                     **{"summary": f"{stage} {score}", **kw})


@pytest.fixture
def pool():
    """Twenty jobs: 0-9 dropped by the rules, 10-19 kept; of the kept, 10-14 the screen rejects
    (14 is relevant but below the bar), 15-19 pass it."""
    jobs = [_job(n) for n in range(20)]
    filters = {}
    for n, job in enumerate(jobs):
        status = "drop" if n < 10 else ("keep" if n % 2 else "review")
        filters[job.id] = FilterResult(job_id=job.id, status=status,
                                       reasons=["asks for 5 years"] if status == "drop" else [])
    prefilter = {}
    for n, job in enumerate(jobs[10:], start=10):
        if n < 14:
            prefilter[job.id] = _verdict(job.id, "prefilter", 0, relevant=False)
        elif n == 14:
            prefilter[job.id] = _verdict(job.id, "prefilter", 20)  # relevant, but under 30
        else:
            prefilter[job.id] = _verdict(job.id, "prefilter", 60)
    return jobs, filters, prefilter


def _ids(jobs) -> list[str]:
    return [j.id for j in jobs]


# ----------------------------------------------------------------------- the two pools


def test_rule_drops_sample_only_drops(pool):
    jobs, filters, _pre = pool
    picked = sample_rule_drops(jobs, filters, 5, seed=1)
    assert len(picked) == 5
    assert all(filters[j.id].status == "drop" for j in picked)
    assert len(set(_ids(picked))) == 5


def test_screen_rejects_are_irrelevant_or_below_the_bar_and_never_rule_drops(pool):
    jobs, filters, pre = pool
    picked = sample_screen_rejects(jobs, filters, pre, 50, seed=1, min_score=30)
    assert sorted(_ids(picked)) == sorted(_ids(jobs[10:15]))


def test_a_kept_job_the_screen_never_saw_is_not_a_screen_reject(pool):
    jobs, filters, pre = pool
    del pre[jobs[10].id]
    picked = sample_screen_rejects(jobs, filters, pre, 50, seed=1, min_score=30)
    assert jobs[10].id not in _ids(picked)


def test_only_the_listed_jobs_qualify(pool):
    """A filter result or a verdict whose job is not in ``jobs`` (gone, or out of the window)."""
    jobs, filters, pre = pool
    assert sorted(_ids(sample_rule_drops(jobs[5:], filters, 50, seed=1))) == sorted(_ids(jobs[5:10]))
    assert sorted(_ids(sample_screen_rejects(jobs[:12], filters, pre, 50, seed=1, min_score=30))) \
        == sorted(_ids(jobs[10:12]))


def test_jobs_already_carrying_a_sentinel_verdict_are_excluded(pool):
    jobs, filters, pre = pool
    done = {j.id for j in jobs[:8]}
    assert sorted(_ids(sample_rule_drops(jobs, filters, 50, seed=1, exclude=done))) == \
        sorted(_ids(jobs[8:10]))
    done = {jobs[10].id, jobs[11].id}
    assert sorted(_ids(sample_screen_rejects(jobs, filters, pre, 50, seed=1, min_score=30,
                                             exclude=done))) == sorted(_ids(jobs[12:15]))


def test_n_larger_than_the_pool_takes_the_whole_pool(pool):
    jobs, filters, _pre = pool
    assert sorted(_ids(sample_rule_drops(jobs, filters, 99, seed=3))) == sorted(_ids(jobs[:10]))


def test_n_zero_samples_nothing(pool):
    jobs, filters, pre = pool
    assert sample_rule_drops(jobs, filters, 0, seed=1) == []
    assert sample_screen_rejects(jobs, filters, pre, 0, seed=1, min_score=30) == []


def test_sampling_is_deterministic_for_a_seed_and_moves_with_it(pool):
    jobs, filters, _pre = pool
    first = _ids(sample_rule_drops(jobs, filters, 4, seed=7))
    assert _ids(sample_rule_drops(list(reversed(jobs)), filters, 4, seed=7)) == first  # input order irrelevant
    assert _ids(sample_rule_drops(jobs, filters, 4, seed=8)) != first


def test_the_default_seed_is_the_date():
    """Each day samples differently; a re-run on the same day repeats the same sample."""
    assert default_seed(date(2026, 10, 13)) == 20261013
    assert default_seed(date(2026, 10, 14)) != default_seed(date(2026, 10, 13))
    assert isinstance(default_seed(), int)


# ------------------------------------------------------------------------- the prompts


def test_the_gate_texts_name_the_rule_reasons_and_the_screen_verdict():
    fr = FilterResult(job_id="x", status="drop", reasons=["asks for 5 years", "Polish required"])
    assert rule_drop_gate(fr) == "rule filter: asks for 5 years; Polish required"
    assert rule_drop_gate(None) == "rule filter: no reason recorded"
    assert screen_gate(_verdict("x", "prefilter", 12, relevant=False, summary="Senior role.")) == \
        "screen: score 12, Senior role."


def test_the_sentinel_prefix_leads_the_job_prompt_and_leaves_the_rest_unchanged():
    job = _job(1)
    fr = FilterResult(job_id=job.id, status="drop", reasons=["asks for 5 years"])
    line = sentinel_prefix(rule_drop_gate(fr))
    assert line == ("SENTINEL: this posting was rejected by an earlier gate (rule filter: asks for "
                    "5 years). Judge it exactly as you would any other posting; do not assume the "
                    "rejection was right or wrong.")
    plain = job_prompt(job, fr, 400)
    assert job_prompt(job, fr, 400, prefix=line) == line + "\n" + plain
    assert job_prompt(job, fr, 400, prefix="") == plain


@pytest.mark.parametrize(("sentinel", "mirror"), list(SENTINEL_STAGES.items()))
def test_a_sentinel_stage_uses_the_mirrored_stage_prompt_and_fingerprint(settings, sentinel, mirror):
    assert mirrored_stage(sentinel) == mirror
    assert system_prompt(sentinel, settings.profile) == system_prompt(mirror, settings.profile)
    assert profile_fingerprint(sentinel, settings.profile) == profile_fingerprint(mirror, settings.profile)


def test_the_regular_stages_mirror_themselves():
    assert [mirrored_stage(s) for s in ("prefilter", "rank", "refine")] == ["prefilter", "rank", "refine"]
    assert SENTINEL_STAGES == {"sentinel_prefilter": "prefilter", "sentinel_rank": "rank"}


def test_a_verdict_accepts_the_sentinel_stage_names():
    for stage in SENTINEL_STAGES:
        assert _verdict("x", stage, 50).stage == stage
