"""The AI stage cannot be called live in tests; mock the SDK's parse() and check the plumbing:
prompt content, structured-output mapping, caching of verdicts, refusal handling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from jobscraper.ai.client import AIStage, estimate_cost, output_schema
from jobscraper.ai.prompts import PROMPT_VERSION, job_prompt, system_prompt
from jobscraper.ai.schemas import Ranking, Screening, ranking_schema
from jobscraper.models import AIVerdict, FilterResult, Job
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


def test_the_prompt_names_an_evergreen_advert_and_the_phrase_that_gave_it_away():
    """Both AI passes have to discount a pipeline advert the same way, or scores swing."""
    fr = FilterResult(job_id="x", status="keep", signals={"evergreen": "talent pool"})
    assert 'evergreen advert ("talent pool")' in job_prompt(_job(), fr, 400)


def test_a_live_vacancy_says_nothing_about_evergreen_adverts():
    fr = FilterResult(job_id="x", status="keep", signals={"years_required": 3})
    assert "evergreen" not in job_prompt(_job(), fr, 400)


_NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(("posted", "first_seen", "expected"), [
    (datetime(2026, 10, 11, tzinfo=UTC), datetime(2026, 9, 7, 6, 30, tzinfo=UTC),
     "Posted: 2026-10-11; first seen by this pipeline: 2026-09-07 (35 days ago; reposted or long open)\n"),
    (datetime(2026, 10, 11, tzinfo=UTC), datetime(2026, 10, 12, 1, 0, tzinfo=UTC),
     "Posted: 2026-10-11; first seen by this pipeline: 2026-10-12\n"),
    (None, datetime(2026, 9, 6, tzinfo=UTC),
     "Posted: unknown; first seen by this pipeline: 2026-09-06 (36 days ago; long open)\n"),
    (datetime(2026, 10, 11, tzinfo=UTC), None, "Posted: 2026-10-11\n"),
])
def test_the_prompt_shows_when_the_pipeline_first_saw_a_posting(posted, first_seen, expected):
    """Boards re-post: the board's date alone made a 36-day-old listing look one day old."""
    job = _job(posted_at=posted, first_seen=first_seen)
    assert expected in job_prompt(job, None, 400, now=_NOW)


def test_a_posting_listed_three_weeks_without_a_repost_reads_as_long_open():
    job = _job(posted_at=datetime(2026, 9, 20, tzinfo=UTC), first_seen=datetime(2026, 9, 21, tzinfo=UTC))
    assert ("Posted: 2026-09-20; first seen by this pipeline: 2026-09-21 (21 days ago; long open)\n"
            in job_prompt(job, None, 400, now=_NOW))


def test_a_listing_just_under_three_weeks_old_gets_no_note():
    job = _job(posted_at=datetime(2026, 9, 21, tzinfo=UTC), first_seen=datetime(2026, 9, 22, tzinfo=UTC))
    assert "first seen by this pipeline: 2026-09-22\n" in job_prompt(job, None, 400, now=_NOW)


def test_a_repost_inside_three_weeks_is_still_flagged():
    job = _job(posted_at=datetime(2026, 10, 11, tzinfo=UTC), first_seen=datetime(2026, 10, 1, tzinfo=UTC))
    assert ("first seen by this pipeline: 2026-10-01 (11 days ago; reposted or long open)\n"
            in job_prompt(job, None, 400, now=_NOW))


def test_the_first_seen_age_defaults_to_the_current_clock():
    job = _job(first_seen=datetime.now(UTC) - timedelta(days=30))
    assert "(30 days ago; long open)" in job_prompt(job, None, 400)


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
    assert issubclass(st.schema, Ranking) and st.effort == "high" and st.model == settings.profile.ai.rank_model


def test_the_preamble_rides_in_front_of_the_job_prompt(settings, tmp_path, monkeypatch):
    """Reference scores are part of the user message, not the cached system prompt: they change
    every round, and the system prompt is what the cache is keyed on."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    store = Store(tmp_path / "p.db")
    calls: dict = {}

    def fake_parse(**kw):
        calls.update(kw)
        return _response(Ranking(relevant=True, score=81, language_ok=True, seniority_ok=True,
                                 location_ok=True, summary="Strong junior Go fit."))

    job = _job()
    plain = AIStage("rank", settings.profile, store)
    assert plain.preamble == ""
    monkeypatch.setattr(plain.client.messages, "parse", fake_parse)
    plain.judge(job, None)
    assert calls["messages"][0]["content"] == job_prompt(job, None, plain.max_chars)

    anchored = AIStage("rank", settings.profile, store,
                       preamble="REFERENCE SCORES (fixed)\n- something · score 85 · why\n\n")
    monkeypatch.setattr(anchored.client.messages, "parse", fake_parse)
    anchored.judge(job, None)
    content = calls["messages"][0]["content"]
    assert content.startswith("REFERENCE SCORES")
    assert content.endswith(job_prompt(job, None, anchored.max_chars))


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


def test_run_batch_carries_the_preamble_too(stage, batches):
    """A Message Batch is the same request as judge(), so the reference block rides along."""
    stage.preamble = "REFERENCE SCORES (fixed)\n- something · score 85 · why\n\n"
    job = _job(source_id="1")
    stage.run_batch([job], {})
    content = batches.submitted[0][0]["params"]["messages"][0]["content"]
    assert content == stage.preamble + job_prompt(job, None, stage.max_chars)


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


# ------------------------------------------------- input notes and criteria (API path)
# The stages report problems with their inputs in `input_notes`; the ranker returns its
# per-criterion judgement in `criteria`. Both land on the verdict, the notes also in run_notes.

WEIGHTS = {"stack_fit": 3, "pay": 1}


def test_the_new_fields_default_to_empty_so_old_verdicts_still_validate():
    screening = Screening(relevant=True, score=50, language_ok=True, seniority_ok=True,
                          location_ok=True, summary="s")
    ranking = Ranking(relevant=True, score=50, language_ok=True, seniority_ok=True,
                      location_ok=True, summary="s")
    assert screening.input_notes == [] and ranking.input_notes == [] and ranking.criteria == {}
    old = AIVerdict.model_validate_json(
        '{"job_id": "j", "stage": "rank", "model": "m", "prompt_version": "2026-09-09.1", '
        '"relevant": true, "score": 70, "language_ok": true, "seniority_ok": true, '
        '"location_ok": true, "summary": "s"}')
    assert old.input_notes == [] and old.criteria == {}


def test_without_weights_the_rank_schema_is_ranking_itself():
    assert ranking_schema({}) is Ranking


def test_with_weights_the_api_schema_requires_one_integer_per_criterion():
    """A free-form dict becomes an empty object in structured output (no properties,
    additionalProperties false), so the API path spells the criteria out, keyed as shown."""
    schema = ranking_schema(WEIGHTS)
    assert issubclass(schema, Ranking)
    full = output_schema(schema)["schema"]
    criteria = full["properties"]["criteria"]
    if "$ref" in criteria:  # the SDK keeps nested models as $defs
        criteria = full["$defs"][criteria["$ref"].rsplit("/", 1)[-1]]
    assert criteria["additionalProperties"] is False
    assert set(criteria["properties"]) == {"stack fit", "pay"}
    assert {p["type"] for p in criteria["properties"].values()} == {"integer"}
    assert sorted(criteria["required"]) == ["pay", "stack fit"]
    parsed = schema.model_validate_json(
        '{"relevant": true, "score": 80, "language_ok": true, "seniority_ok": true, '
        '"location_ok": true, "summary": "s", "criteria": {"stack fit": 85, "pay": 60}}')
    assert parsed.criteria.model_dump(by_alias=True) == {"stack fit": 85, "pay": 60}


def test_the_api_schema_asks_for_criteria_before_the_score():
    """Generated in property order: the criteria first, then the score they combine into."""
    props = list(output_schema(ranking_schema(WEIGHTS))["schema"]["properties"])
    assert props.index("criteria") < props.index("score")


@pytest.fixture
def rank_stage(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    profile = settings.profile.model_copy(update={"weights": dict(WEIGHTS)})
    st = AIStage("rank", profile, Store(tmp_path / "rank.db"))
    st.concurrency = 1
    return st


def _ranking(stage: AIStage, score: int, criteria: dict | None, notes: list[str] | None = None):
    fields = {"relevant": True, "score": score, "language_ok": True, "seniority_ok": True,
              "location_ok": True, "summary": "Go fit."}
    if criteria is not None:
        fields["criteria"] = criteria
    if notes is not None:
        fields["input_notes"] = notes
    return stage.schema.model_validate(fields)


def test_api_rank_stores_criteria_and_input_notes_and_writes_run_notes(rank_stage, monkeypatch):
    notes = ["missing_input: description is cut off mid-sentence",
             "profile_gap: whether the candidate holds a driving licence",
             "signal_conflict: years_required=12 is the company's age",
             "instruction_gap: the rubric and the deal-breakers disagree on consultancies",
             "the posting is in two languages"]
    parsed = _ranking(rank_stage, 78, {"stack fit": 80, "pay": 70}, notes)
    monkeypatch.setattr(rank_stage.client.messages, "parse", lambda **kw: _response(parsed))
    job = _job()

    rank_stage.run([job], {})

    v = rank_stage.store.verdicts("rank")[job.id]
    assert v.criteria == {"stack_fit": 80, "pay": 70}
    assert v.input_notes == notes
    stored = rank_stage.store.notes(stage="rank")
    assert [(n.kind, n.job_id, n.message) for n in stored] == [
        ("missing_input", job.id, notes[0]), ("profile_gap", job.id, notes[1]),
        ("signal_conflict", job.id, notes[2]), ("instruction_gap", job.id, notes[3]),
        ("uncategorised", job.id, notes[4])]


def test_a_prefilter_input_note_is_written_under_the_prefilter_stage(stage, monkeypatch):
    parsed = Screening(relevant=True, score=60, language_ok=True, seniority_ok=True, location_ok=True,
                       summary="Title only.", input_notes=["`missing_input:` no description at all"])
    monkeypatch.setattr(stage.client.messages, "parse", lambda **kw: _response(parsed))
    stage.run([_job(description=None)], {})
    [note] = stage.store.notes()
    assert (note.stage, note.kind) == ("prefilter", "missing_input")
    assert stage.store.verdicts("prefilter")[_job().id].criteria == {}


def test_a_clean_verdict_writes_no_notes(rank_stage, monkeypatch):
    parsed = _ranking(rank_stage, 76, {"stack fit": 80, "pay": 60})  # weighted 75
    monkeypatch.setattr(rank_stage.client.messages, "parse", lambda **kw: _response(parsed))
    rank_stage.run([_job()], {})
    assert rank_stage.store.notes() == []


@pytest.mark.parametrize(("score", "fires"), [(91, True), (90, False), (60, False), (59, True)])
def test_rubric_mismatch_fires_beyond_fifteen_points(rank_stage, monkeypatch, score, fires):
    parsed = _ranking(rank_stage, score, {"stack fit": 80, "pay": 60})  # weighted (240+60)/4 = 75
    monkeypatch.setattr(rank_stage.client.messages, "parse", lambda **kw: _response(parsed))
    rank_stage.run([_job()], {})
    notes = rank_stage.store.notes(kind="rubric_mismatch")
    assert bool(notes) is fires
    if fires:
        assert notes[0].stage == "rank" and notes[0].job_id == _job().id
        assert notes[0].data == {"score": score, "weighted": 75.0}
        assert "75" in notes[0].message and str(score) in notes[0].message


def test_the_score_still_comes_from_the_model_not_the_criteria(rank_stage, monkeypatch):
    parsed = _ranking(rank_stage, 20, {"stack fit": 90, "pay": 90})
    monkeypatch.setattr(rank_stage.client.messages, "parse", lambda **kw: _response(parsed))
    rank_stage.run([_job()], {})
    assert rank_stage.store.verdicts("rank")[_job().id].score == 20


def test_unknown_criteria_are_dropped_with_a_note_and_values_clamped(rank_stage, monkeypatch):
    """A plain Ranking (the shape the subagent path validates) can carry anything."""
    parsed = Ranking(relevant=True, score=80, language_ok=True, seniority_ok=True, location_ok=True,
                     summary="s", criteria={"pay": 140, "Stack_Fit": -5, "vibes": 90})
    monkeypatch.setattr(rank_stage.client.messages, "parse", lambda **kw: _response(parsed))
    rank_stage.run([_job()], {})
    v = rank_stage.store.verdicts("rank")[_job().id]
    assert list(v.criteria.items()) == [("stack_fit", 0), ("pay", 100)]  # the profile's order
    [note] = rank_stage.store.notes(kind="criteria_unknown_key")
    assert note.stage == "rank" and "vibes" in note.message
    assert note.data == {"keys": ["vibes"], "known": ["stack_fit", "pay"]}


def test_criteria_without_profile_weights_are_dropped(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    profile = settings.profile.model_copy(update={"weights": {}})
    st = AIStage("rank", profile, Store(tmp_path / "nw.db"))
    assert st.schema is Ranking
    parsed = Ranking(relevant=True, score=80, language_ok=True, seniority_ok=True, location_ok=True,
                     summary="s", criteria={"pay": 10})
    monkeypatch.setattr(st.client.messages, "parse", lambda **kw: _response(parsed))
    st.run([_job()], {})
    assert st.store.verdicts("rank")[_job().id].criteria == {}
    assert [n.kind for n in st.store.notes()] == ["criteria_unknown_key"]  # and no rubric_mismatch


def test_the_batch_path_records_the_same_notes(rank_stage, monkeypatch):
    fake = FakeBatches()
    monkeypatch.setattr(rank_stage.client.messages, "batches", fake)
    monkeypatch.setattr("jobscraper.ai.client.time.sleep", lambda _s: None)
    job = _job()
    text = ('{"relevant": true, "score": 95, "language_ok": true, "seniority_ok": true, '
            '"location_ok": true, "summary": "s", "criteria": {"stack fit": 70, "pay": 70}, '
            '"input_notes": ["profile_gap: notice period"]}')
    fake.results_by_id["msgbatch_1"] = [_ok(job.id, text)]
    rank_stage.run_batch([job], {})
    v = rank_stage.store.verdicts("rank")[job.id]
    assert v.criteria == {"stack_fit": 70, "pay": 70} and v.input_notes == ["profile_gap: notice period"]
    assert [n.kind for n in rank_stage.store.notes()] == ["profile_gap", "rubric_mismatch"]
