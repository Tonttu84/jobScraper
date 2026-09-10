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


@pytest.mark.parametrize(("signals", "expected"), [
    ({"deadline": "2026-09-13", "closes_in_days": 3}, "deadline: 2026-09-13 (in 3 days)"),
    ({"deadline": "2026-09-10", "closes_in_days": 0}, "deadline: 2026-09-10 (today)"),
    ({"deadline": "2026-09-01"}, "deadline: 2026-09-01"),
])
def test_the_prompt_spells_out_a_deadline_next_to_the_raw_signals(signals, expected):
    """The signal dict alone buries the closing date; the AI stages should not have to dig."""
    fr = FilterResult(job_id="x", status="keep", signals=signals)
    assert expected in job_prompt(_job(), fr, 400)


def test_a_posting_without_a_deadline_adds_nothing_to_the_signal_line():
    fr = FilterResult(job_id="x", status="keep", signals={"years_required": 3})
    assert "deadline" not in job_prompt(_job(), fr, 400)


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


def test_stage_warns_when_no_api_credentials_are_set(settings, tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: SimpleNamespace(messages=None))
    with caplog.at_level("WARNING"):
        st = AIStage("prefilter", settings.profile, Store(tmp_path / "k.db"))
    assert "ANTHROPIC_API_KEY not set" in caplog.text
    assert st.stage == "prefilter"


def test_run_survives_an_api_error_and_reports_progress(stage, monkeypatch):
    import anthropic
    import httpx

    calls = {"n": 0}

    def fake_parse(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise anthropic.APIConnectionError(
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            )
        return _response(Screening(relevant=True, score=64, language_ok=True, seniority_ok=True,
                                   location_ok=True, summary="Junior Go role"))

    monkeypatch.setattr(stage.client.messages, "parse", fake_parse)
    scored: list = []
    results = stage.run([_job(source_id="1"), _job(source_id="2")], {}, progress=scored.append)

    assert calls["n"] == 2
    assert len(results) == 1  # the failed job simply has no verdict yet
    assert [v.score for v in scored] == [64]


def test_prompt_rules_for_a_profile_with_no_language_or_remote_policy(settings):
    from jobscraper.config import LanguagePolicy

    profile = settings.profile.model_copy(update={
        "languages": LanguagePolicy(ok=[], weak=[], drop_if_written_in=[]),
        "location": settings.profile.location.model_copy(update={"keep_all_remote": False}),
    })
    text = system_prompt("prefilter", profile)
    assert "any language the candidate reads" in text
    assert "Remote roles count only when their stated region overlaps" in text


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


def test_prompts_limit_seniority_to_entry_level(settings):
    """Owner (2026-09-07): the ranked results were all trainee/intern/junior and that is enough."""
    sys_pre = system_prompt("prefilter", settings.profile).lower()
    assert "mid-level" in sys_pre and "3+ years" in sys_pre
    assert "borderline but possible" not in sys_pre
    assert PROMPT_VERSION not in ("2026-09-07.1", "2026-09-07.2")


# ------------------------------------------------------------- Message Batches API
SCREENING_JSON = ('{"relevant": true, "score": 72, "language_ok": true, "seniority_ok": true, '
                  '"location_ok": true, "summary": "Junior Go role in Helsinki", "concerns": ["asks for 3 years"]}')


def _usage() -> SimpleNamespace:
    return SimpleNamespace(input_tokens=1200, output_tokens=150, cache_read_input_tokens=900,
                           cache_creation_input_tokens=0)


def _message(text: str | None, stop_reason: str = "end_turn") -> SimpleNamespace:
    content = [] if text is None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=_usage())


def _ok(custom_id: str, text: str | None = SCREENING_JSON, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(custom_id=custom_id,
                           result=SimpleNamespace(type="succeeded", message=_message(text, stop_reason)))


def _bad(custom_id: str, kind: str = "errored", error: str | None = "overloaded") -> SimpleNamespace:
    result = SimpleNamespace(type=kind) if error is None else SimpleNamespace(type=kind, error=error)
    return SimpleNamespace(custom_id=custom_id, result=result)


def _counts(**kw) -> SimpleNamespace:
    base = {"processing": 0, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0}
    base.update(kw)
    return SimpleNamespace(**base)


class FakeBatches:
    """Stand-in for ``client.messages.batches``: records submissions, serves canned results."""

    def __init__(self) -> None:
        self.submitted: list[list] = []
        self.results_by_id: dict[str, list] = {}
        self.statuses: list[str] = []
        self.retrieved: list[str] = []

    def create(self, *, requests):
        requests = list(requests)
        self.submitted.append(requests)
        batch_id = f"msgbatch_{len(self.submitted)}"
        self.results_by_id.setdefault(batch_id, [_ok(r["custom_id"]) for r in requests])
        return SimpleNamespace(id=batch_id, processing_status="in_progress",
                               request_counts=_counts(processing=len(requests)))

    def retrieve(self, batch_id):
        self.retrieved.append(batch_id)
        status = self.statuses.pop(0) if self.statuses else "ended"
        n = len(self.results_by_id.get(batch_id, []))
        counts = _counts(processing=n) if status != "ended" else _counts(succeeded=n)
        return SimpleNamespace(id=batch_id, processing_status=status, request_counts=counts)

    def results(self, batch_id):
        return iter(self.results_by_id.get(batch_id, []))


@pytest.fixture
def batches(stage, monkeypatch):
    fake = FakeBatches()
    monkeypatch.setattr(stage.client.messages, "batches", fake)
    monkeypatch.setattr("jobscraper.ai.client.time.sleep", lambda _s: None)
    return fake


def test_run_batch_request_shape(stage, batches):
    jobs = [_job(source_id="1"), _job(source_id="2")]
    stage.run_batch(jobs, {})
    assert len(batches.submitted) == 1
    reqs = batches.submitted[0]
    assert [r["custom_id"] for r in reqs] == [j.id for j in jobs]
    params = reqs[0]["params"]
    assert params["model"] == stage.model and params["max_tokens"] == 4000
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["thinking"] == {"type": "adaptive"}
    assert params["messages"][0]["content"] == job_prompt(jobs[0], None, stage.max_chars)
    cfg = params["output_config"]
    assert cfg["effort"] == "low"
    assert cfg["format"]["type"] == "json_schema"
    assert set(cfg["format"]["schema"]["properties"]) >= {"relevant", "score", "summary"}


def test_run_batch_stores_verdicts_marked_as_batch(stage, batches):
    job = _job(source_id="1")
    out = stage.run_batch([job], {})
    v = out[job.id]
    assert v.score == 72 and v.relevant and v.model == stage.model and v.prompt_version == PROMPT_VERSION
    assert v.usage["batch"] is True and v.usage["cache_read"] == 900
    assert stage.store.verdicts("prefilter", PROMPT_VERSION)[job.id].score == 72


def test_run_batch_refusal_keeps_job_for_manual_review(stage, batches):
    job = _job(source_id="1")
    batches.results_by_id["msgbatch_1"] = [_ok(job.id, text=None, stop_reason="refusal")]
    v = stage.run_batch([job], {})[job.id]
    assert v.score == 50 and v.relevant and "No verdict" in v.summary and v.usage["batch"] is True


def test_run_batch_unparseable_answer_keeps_job_for_manual_review(stage, batches):
    job = _job(source_id="1")
    batches.results_by_id["msgbatch_1"] = [_ok(job.id, text='{"score": "not a number"}')]
    v = stage.run_batch([job], {})[job.id]
    assert v.score == 50 and "No verdict" in v.summary


def test_run_batch_errored_entry_leaves_no_verdict(stage, batches, caplog):
    job = _job(source_id="1")
    batches.results_by_id["msgbatch_1"] = [_bad(job.id)]
    with caplog.at_level("WARNING"):
        out = stage.run_batch([job], {})
    assert job.id not in out and "overloaded" in caplog.text


def test_run_batch_expired_entry_without_error_is_logged(stage, batches, caplog):
    job = _job(source_id="1")
    batches.results_by_id["msgbatch_1"] = [_bad(job.id, kind="expired", error=None)]
    with caplog.at_level("WARNING"):
        out = stage.run_batch([job], {})
    assert job.id not in out and "expired" in caplog.text


def test_run_batch_chunks_requests(stage, batches):
    stage.batch_chunk = 2
    jobs = [_job(source_id=str(i)) for i in range(5)]
    stage.run_batch(jobs, {})
    assert [len(c) for c in batches.submitted] == [2, 2, 1]


def test_run_batch_polls_until_ended(stage, batches, monkeypatch):
    slept: list[int] = []
    monkeypatch.setattr("jobscraper.ai.client.time.sleep", slept.append)
    batches.statuses = ["in_progress", "in_progress", "ended"]
    stage.run_batch([_job(source_id="1")], {}, poll_seconds=7)
    assert slept == [7, 7]


def test_run_batch_without_wait_only_submits(stage, batches):
    job = _job(source_id="1")
    out = stage.run_batch([job], {}, wait=False)
    assert out == {}
    assert len(batches.submitted) == 1
    assert stage.store.pending_batches("prefilter")[0]["job_ids"] == [job.id]


def test_run_batch_resumes_a_pending_batch(stage, batches):
    job = _job(source_id="1")
    stage.run_batch([job], {}, wait=False)
    assert not stage.store.verdicts("prefilter", PROMPT_VERSION)
    out = stage.run_batch([job], {})
    assert len(batches.submitted) == 1  # not resubmitted
    assert out[job.id].score == 72
    assert stage.store.pending_batches("prefilter") == []


def test_run_batch_waits_for_a_batch_still_in_flight(stage, batches):
    job, other = _job(source_id="1"), _job(source_id="2")
    stage.run_batch([job], {}, wait=False)
    batches.statuses = ["in_progress", "in_progress", "ended"]
    out = stage.run_batch([job, other], {})
    assert len(batches.submitted) == 2  # the pending job is not resubmitted, the new one is
    assert [r["custom_id"] for r in batches.submitted[1]] == [other.id]
    assert out[job.id].score == 72 and out[other.id].score == 72


def test_run_batch_ignores_a_pending_batch_from_another_model(stage, batches):
    job = _job(source_id="1")
    stage.store.save_batch("msgbatch_other", "prefilter", "claude-haiku-4-5", PROMPT_VERSION, [job.id])
    stage.run_batch([job], {})
    assert batches.retrieved == ["msgbatch_1"]  # the foreign batch was not touched
    assert [b["id"] for b in stage.store.pending_batches("prefilter")] == ["msgbatch_other"]


def test_run_batch_is_incremental(stage, batches):
    job = _job(source_id="1")
    stage.run_batch([job], {})
    stage.run_batch([job], {})
    assert len(batches.submitted) == 1
    stage.run_batch([job], {}, force=True)
    assert len(batches.submitted) == 2


def test_run_batch_reports_progress(stage, batches):
    seen = []
    stage.run_batch([_job(source_id="1")], {}, progress=seen.append)
    assert [v.score for v in seen] == [72]


def test_run_batch_with_nothing_to_do(stage, batches):
    assert stage.run_batch([], {}) == {}
    assert batches.submitted == []


def test_estimate_cost_halves_batch_usage():
    from jobscraper.models import AIVerdict

    base = {"job_id": "a", "stage": "prefilter", "model": "claude-sonnet-5", "prompt_version": "v",
            "relevant": True, "score": 1, "language_ok": True, "seniority_ok": True, "location_ok": True,
            "summary": "s"}
    usage = {"input": 1_000_000, "output": 0, "cache_read": 0, "cache_write": 0}
    live = AIVerdict(**base, usage=usage)
    batched = AIVerdict(**base, usage={**usage, "batch": True})
    assert estimate_cost([live])["total"] == pytest.approx(2.0)
    assert estimate_cost([batched])["total"] == pytest.approx(1.0)
