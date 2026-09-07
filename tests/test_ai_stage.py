"""The AI stage cannot be called live in tests; mock the SDK's parse() and check the plumbing:
prompt content, structured-output mapping, caching of verdicts, refusal handling."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jobscraper.ai.client import AIStage, estimate_cost
from jobscraper.ai.prompts import PROMPT_VERSION, job_prompt, system_prompt
from jobscraper.ai.schemas import Ranking, Screening
from jobscraper.models import FilterResult, Job
from jobscraper.store import Store


def _job(**kw) -> Job:
    base = {"source": "test", "source_id": "1", "url": "https://x/1", "title": "Junior Developer",
            "company": "Acme", "country": "FI",
            "description": "We build things in Go. English working language. Helsinki."}
    base.update(kw)
    return Job(**base)


def _response(parsed, stop_reason="end_turn"):
    usage = SimpleNamespace(input_tokens=1200, output_tokens=150, cache_read_input_tokens=900, cache_creation_input_tokens=0)
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason, usage=usage)


@pytest.fixture
def stage(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    store = Store(tmp_path / "t.db")
    st = AIStage("prefilter", settings.profile, store)
    st.concurrency = 1
    return st


def test_prompts_mention_policy(settings):
    sys_pre = system_prompt("prefilter", settings.profile)
    sys_rank = system_prompt("rank", settings.profile)
    assert "PERMISSIVE" in sys_pre and "FI, EE" in sys_pre
    assert "FULL CV" in sys_rank and "Hive" in sys_rank
    fr = FilterResult(job_id="x", status="review", reasons=["asks for 3 years"], signals={"years_required": 3})
    p = job_prompt(_job(), fr, 100)
    assert "Junior Developer" in p and "asks for 3 years" in p and "years_required" in p
    assert "truncated" in job_prompt(_job(description="x" * 500), None, 100)


def test_judge_maps_structured_output(stage, monkeypatch):
    calls = {}

    def fake_parse(**kw):
        calls.update(kw)
        return _response(Screening(relevant=True, score=72, language_ok=True, seniority_ok=True, location_ok=True,
                                   summary="Junior Go role in Helsinki", concerns=["asks for 3 years"]))

    monkeypatch.setattr(stage.client.messages, "parse", fake_parse)
    v = stage.judge(_job(), None)
    assert v.score == 72 and v.relevant and v.model == stage.model and v.prompt_version == PROMPT_VERSION
    assert v.usage["cache_read"] == 900
    assert calls["output_format"] is Screening
    assert calls["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert calls["output_config"] == {"effort": "low"}
    assert calls["thinking"] == {"type": "adaptive"}


def test_refusal_keeps_job_for_manual_review(stage, monkeypatch):
    monkeypatch.setattr(stage.client.messages, "parse", lambda **kw: _response(None, stop_reason="refusal"))
    v = stage.judge(_job(), None)
    assert v.relevant and v.score == 50 and "No verdict" in v.summary


def test_run_is_incremental(stage, monkeypatch):
    n = {"calls": 0}

    def fake_parse(**kw):
        n["calls"] += 1
        return _response(Screening(relevant=False, score=10, language_ok=False, seniority_ok=True, location_ok=True,
                                   summary="Polish required"))

    monkeypatch.setattr(stage.client.messages, "parse", fake_parse)
    jobs = [_job(source_id="1"), _job(source_id="2")]
    first = stage.run(jobs, {})
    assert n["calls"] == 2 and len(first) == 2
    second = stage.run(jobs, {})
    assert n["calls"] == 2 and len(second) == 2  # nothing re-scored
    stage.run(jobs, {}, force=True)
    assert n["calls"] == 4


def test_rank_stage_uses_ranking_schema(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    st = AIStage("rank", settings.profile, Store(tmp_path / "r.db"))
    assert st.schema is Ranking and st.effort == "high" and st.model == settings.profile.ai.rank_model


def test_estimate_cost():
    from jobscraper.models import AIVerdict

    v = AIVerdict(job_id="a", stage="prefilter", model="claude-sonnet-5", prompt_version="v", relevant=True, score=1,
                  language_ok=True, seniority_ok=True, location_ok=True, summary="s",
                  usage={"input": 1_000_000, "output": 0, "cache_read": 0, "cache_write": 0})
    assert estimate_cost([v])["total"] == pytest.approx(2.0)


def test_prefilter_prompt_short_circuits_hard_failures(settings):
    """Owner: no need to weigh 15 vs 42 for a posting that fails a minimum requirement — score 0 at once."""
    sys_pre = system_prompt("prefilter", settings.profile)
    assert "score 0" in sys_pre.lower() or "score: 0" in sys_pre.lower()
    assert "immediately" in sys_pre.lower()
    assert "programming language" in sys_pre.lower()  # a language mismatch is explicitly NOT a hard failure
    assert PROMPT_VERSION != "2026-09-07.1"  # wording changed → cached verdicts must be recomputed
