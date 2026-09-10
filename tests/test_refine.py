"""The refine stage: one request that ranks the whole shortlist against itself.

Measured on 2026-09-10: re-scoring the same top 30 twice drifted by a median of 3 points (p90 8,
Spearman 0.74, top-10 overlap 0.54), because the ranker only ever sees a 15-job chunk. One call
that sees the whole shortlist removes that chunk noise for the part of the list that matters.

Nothing here touches the API: ``messages.parse`` is monkeypatched, as in test_ai_stage.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from jobscraper.ai.client import RefineStage, estimate_cost
from jobscraper.ai.prompts import PROMPT_VERSION, refine_user_prompt, system_prompt
from jobscraper.ai.schemas import RefinedJob, Refinement
from jobscraper.models import AIVerdict, FilterResult, Job
from jobscraper.report import effective_score
from jobscraper.store import Store


def _job(n: int, title: str = "Junior Developer") -> Job:
    return Job(source="test", source_id=str(n), url=f"https://example.test/{n}", title=title,
               company="Acme", country="FI",
               description="We build backend services in Go. English working language.")


def _usage() -> SimpleNamespace:
    return SimpleNamespace(input_tokens=9000, output_tokens=800, cache_read_input_tokens=1200,
                           cache_creation_input_tokens=0)


def _response(parsed, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason, usage=_usage())


def _refinement(jobs, scores) -> Refinement:
    return Refinement(items=[RefinedJob(job_id=j.id, position=n, score=s, summary=f"#{n} of the list")
                             for n, (j, s) in enumerate(zip(jobs, scores), start=1)])


@pytest.fixture
def stage(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return RefineStage(settings.profile, Store(tmp_path / "refine.db"))


# ------------------------------------------------------------------------- schema


def test_refinement_schema_accepts_a_whole_shortlist():
    parsed = Refinement(items=[{"job_id": "a", "position": 1, "score": 88, "summary": "best fit"},
                               {"job_id": "b", "position": 2, "score": 70, "summary": "second"}])
    assert [i.job_id for i in parsed.items] == ["a", "b"]
    assert parsed.items[0].score == 88 and parsed.items[1].position == 2


def test_refinement_schema_rejects_a_score_outside_0_100():
    with pytest.raises(ValidationError):
        Refinement(items=[{"job_id": "a", "position": 1, "score": 140, "summary": "x"}])


# ------------------------------------------------------------------------ prompts


def test_refine_system_prompt_builds_on_the_ranking_prompt(settings):
    text = system_prompt("refine", settings.profile)
    assert system_prompt("rank", settings.profile) in text
    assert "against each other" in text
    assert "exactly once" in text
    assert "5 points" in text  # differences under 5 points are ties


def test_refine_user_prompt_concatenates_the_shortlist(settings):
    jobs = [_job(1, "Junior Go Developer"), _job(2, "Graduate Backend Engineer")]
    fr = FilterResult(job_id=jobs[0].id, status="review", reasons=["asks for 3 years"])
    text = refine_user_prompt(jobs, {jobs[0].id: fr}, 6000)
    for job in jobs:
        assert f"### job_id: {job.id}" in text
        assert job.title in text
    assert "asks for 3 years" in text  # the same per-job prompt the rank stage sends
    assert text.count("JOB POSTING") == 2


# ---------------------------------------------------------------------- one call


def test_run_sends_one_request_for_the_whole_shortlist(stage, monkeypatch):
    jobs = [_job(1), _job(2), _job(3)]
    calls: list[dict] = []

    def fake_parse(**kw):
        calls.append(kw)
        return _response(_refinement(jobs, [90, 80, 70]))

    monkeypatch.setattr(stage.client.messages, "parse", fake_parse)
    verdicts = stage.run(jobs, {})

    assert len(calls) == 1
    kw = calls[0]
    assert kw["model"] == "claude-fable-5-1" and kw["max_tokens"] == 16000
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["output_format"] is Refinement
    assert kw["output_config"] == {"effort": "high"}
    assert "thinking" not in kw  # always on for Fable; an explicit config is rejected
    assert kw["messages"][0]["content"].count("### job_id:") == 3

    assert [v.score for v in verdicts] == [90, 80, 70]
    assert [v.position for v in verdicts] == [1, 2, 3]
    assert all(v.stage == "refine" and v.prompt_version == PROMPT_VERSION for v in verdicts)
    assert all(v.relevant and v.language_ok and v.seniority_ok and v.location_ok for v in verdicts)
    assert verdicts[0].summary == "#1 of the list"


def test_run_stores_the_verdicts_and_bills_the_call_once(stage, monkeypatch):
    jobs = [_job(1), _job(2)]
    monkeypatch.setattr(stage.client.messages, "parse",
                        lambda **kw: _response(_refinement(jobs, [90, 60])))
    verdicts = stage.run(jobs, {})

    stored = stage.store.verdicts("refine", PROMPT_VERSION)
    assert {jid: v.score for jid, v in stored.items()} == {jobs[0].id: 90, jobs[1].id: 60}
    assert stored[jobs[0].id].position == 1
    # One request, so only one verdict carries its token counters.
    assert verdicts[0].usage["cache_read"] == 1200 and verdicts[1].usage == {}
    # Fable list price, $10/$50 per 1M, cache reads at a tenth of the input rate.
    assert estimate_cost(verdicts)["total"] == pytest.approx((9000 * 10 + 1200 * 1 + 800 * 50) / 1e6)


def test_run_keeps_thinking_adaptive_for_a_model_that_needs_it(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    st = RefineStage(settings.profile, Store(tmp_path / "s.db"), model="claude-sonnet-5")
    jobs = [_job(1)]
    calls: list[dict] = []

    def fake_parse(**kw):
        calls.append(kw)
        return _response(_refinement(jobs, [50]))

    monkeypatch.setattr(st.client.messages, "parse", fake_parse)
    st.run(jobs, {})
    assert calls[0]["thinking"] == {"type": "adaptive"}
    assert calls[0]["model"] == "claude-sonnet-5"


def test_run_omits_thinking_for_opus_too(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    st = RefineStage(settings.profile, Store(tmp_path / "o.db"), model="claude-opus-5")
    jobs = [_job(1)]
    calls: list[dict] = []

    def fake_parse(**kw):
        calls.append(kw)
        return _response(_refinement(jobs, [50]))

    monkeypatch.setattr(st.client.messages, "parse", fake_parse)
    st.run(jobs, {})
    assert "thinking" not in calls[0]


def test_run_with_an_empty_shortlist_never_calls_the_api(stage, monkeypatch):
    def boom(**kw):
        raise AssertionError("the API was called for an empty shortlist")

    monkeypatch.setattr(stage.client.messages, "parse", boom)
    assert stage.run([], {}) == []


# ------------------------------------------------------------------- bad answers


def test_refusal_leaves_the_rank_scores_alone(stage, monkeypatch, caplog):
    jobs = [_job(1)]
    monkeypatch.setattr(stage.client.messages, "parse",
                        lambda **kw: _response(None, stop_reason="refusal"))
    with caplog.at_level("WARNING"):
        assert stage.run(jobs, {}) == []
    assert "refusal" in caplog.text
    assert stage.store.verdicts("refine", PROMPT_VERSION) == {}


def test_an_unparseable_answer_leaves_the_rank_scores_alone(stage, monkeypatch, caplog):
    monkeypatch.setattr(stage.client.messages, "parse", lambda **kw: _response(None))
    with caplog.at_level("WARNING"):
        assert stage.run([_job(1)], {}) == []
    assert "no structured answer" in caplog.text


def test_unknown_and_duplicate_job_ids_are_dropped_and_missing_ones_logged(stage, monkeypatch, caplog):
    jobs = [_job(1), _job(2), _job(3)]
    answer = Refinement(items=[
        RefinedJob(job_id=jobs[0].id, position=1, score=90, summary="best"),
        RefinedJob(job_id=jobs[0].id, position=2, score=50, summary="said twice"),
        RefinedJob(job_id="0000000000000000", position=3, score=40, summary="never sent"),
        RefinedJob(job_id=jobs[1].id, position=4, score=30, summary="third"),
    ])
    monkeypatch.setattr(stage.client.messages, "parse", lambda **kw: _response(answer))
    with caplog.at_level("WARNING"):
        verdicts = stage.run(jobs, {})

    assert [(v.job_id, v.score) for v in verdicts] == [(jobs[0].id, 90), (jobs[1].id, 30)]
    assert "duplicate" in caplog.text and "unknown" in caplog.text
    assert jobs[2].id in caplog.text  # the one the model forgot


# --------------------------------------------------------------- effective score


def _verdict(stage_name: str, score: int, position: int | None = None) -> AIVerdict:
    return AIVerdict(job_id="a", stage=stage_name, model="m", prompt_version="v", relevant=True,
                     score=score, language_ok=True, seniority_ok=True, location_ok=True,
                     summary="s", position=position)


def test_effective_score_is_the_mean_when_both_stages_spoke():
    assert effective_score(_verdict("rank", 91), _verdict("refine", 85, 1)) == 88
    assert effective_score(_verdict("rank", 80), _verdict("refine", 75, 2)) == 78


def test_effective_score_falls_back_to_whichever_stage_spoke():
    assert effective_score(_verdict("rank", 91), None) == 91
    assert effective_score(None, _verdict("refine", 85, 1)) == 85
    assert effective_score(None, None) is None


def test_the_position_survives_a_store_round_trip(tmp_path):
    store = Store(tmp_path / "p.db")
    store.save_verdict(_verdict("refine", 85, position=3))
    assert store.verdicts("refine")["a"].position == 3
    store.close()
