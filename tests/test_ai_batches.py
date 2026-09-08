"""Tests for ``scripts/ai_batches.py``, the API-free way to run the two AI stages.

The script is not a package module, so it is loaded from its path with importlib. Both its own
``DATA_DIR`` and the store's are pointed at ``tmp_path``; nothing here touches the network or the
repo's ``data/``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from jobscraper import store as store_mod
from jobscraper.ai.prompts import PROMPT_VERSION
from jobscraper.models import AIVerdict, FilterResult, Job

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ai_batches.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("ai_batches", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ai_batches = _load_script()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the script and the store at tmp_path."""
    monkeypatch.setattr(ai_batches, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store_mod, "DATA_DIR", tmp_path)
    return tmp_path


def _run(monkeypatch, *args: str) -> None:
    """Drive the script through its real CLI, so the argument defaults are exercised too."""
    monkeypatch.setattr(sys, "argv", ["ai_batches.py", *args])
    ai_batches.main()


def _job(n: int, title: str, description: str | None = None) -> Job:
    return Job(source="test", source_id=str(n), url=f"https://example.test/{n}", title=title,
               company="Acme", country="FI",
               description=description or "We build backend services in Go. English working language.")


def _seed(jobs: list[Job], statuses: list[str]) -> None:
    store = store_mod.Store()
    try:
        store.upsert_jobs(jobs)
        store.save_filter_results(
            [FilterResult(job_id=j.id, status=s) for j, s in zip(jobs, statuses)], "test-rules"
        )
    finally:
        store.close()


def _chunk_entries(out: Path) -> list[dict]:
    entries: list[dict] = []
    for f in sorted(out.glob("chunk-*.json")):
        entries.extend(json.loads(f.read_text(encoding="utf-8")))
    return entries


def test_export_prefilter_writes_system_prompt_and_chunks(data_dir, monkeypatch, capsys):
    """Only rule-filter survivors are exported, chunked, with the pipeline's own prompts."""
    jobs = [_job(1, "Junior Go Developer"), _job(2, "Graduate Backend Engineer"),
            _job(3, "Trainee QA Automation"), _job(4, "Senior Staff Architect")]
    _seed(jobs, ["keep", "review", "keep", "drop"])

    _run(monkeypatch, "export", "prefilter", "--chunk", "2")

    out = data_dir / "exports" / "ai" / "prefilter"
    system = (out / "system.txt").read_text(encoding="utf-8")
    assert "PERMISSIVE" in system  # the prefilter system prompt
    assert "CANDIDATE" in system and "WHAT COUNTS AS A MATCH" in system  # the candidate policy block

    chunks = sorted(p.name for p in out.glob("chunk-*.json"))
    assert chunks == ["chunk-01.json", "chunk-02.json"]  # 3 survivors, 2 per chunk
    assert len(json.loads((out / "chunk-01.json").read_text(encoding="utf-8"))) == 2

    entries = _chunk_entries(out)
    by_id = {j.id: j for j in jobs}
    assert {e["job_id"] for e in entries} == {jobs[0].id, jobs[1].id, jobs[2].id}
    assert jobs[3].id not in {e["job_id"] for e in entries}  # the dropped job is not exported
    for e in entries:
        assert set(e) == {"job_id", "prompt"}
        assert e["prompt"].startswith("JOB POSTING")
        assert by_id[e["job_id"]].title in e["prompt"]
    assert "3 jobs" in capsys.readouterr().out


def test_export_with_nothing_to_do_reports_zero_chunks(data_dir, monkeypatch, capsys):
    """An empty store is a normal state (before the first `filter` run), not a crash."""
    _run(monkeypatch, "export", "prefilter")

    out = data_dir / "exports" / "ai" / "prefilter"
    assert (out / "system.txt").exists()
    assert list(out.glob("chunk-*.json")) == []
    assert "0 jobs" in capsys.readouterr().out


def test_import_prefilter_stores_valid_lines_and_reports_the_rest(data_dir, monkeypatch, capsys):
    jobs = [_job(1, "Junior Go Developer"), _job(2, "Graduate Backend Engineer"),
            _job(3, "Trainee QA Automation")]
    _seed(jobs, ["keep", "review", "keep"])
    _run(monkeypatch, "export", "prefilter", "--chunk", "2")
    capsys.readouterr()

    good = {"job_id": jobs[0].id, "relevant": True, "score": 72, "language_ok": True,
            "seniority_ok": True, "location_ok": True, "summary": "Junior Go role in Helsinki",
            "concerns": ["asks for 3 years"]}
    unknown = dict(good, job_id="0000000000000000")
    verdicts = data_dir / "exports" / "ai" / "prefilter" / "verdicts"
    verdicts.mkdir(parents=True, exist_ok=True)
    (verdicts / "chunk-01.jsonl").write_text(
        json.dumps(good) + "\n" + json.dumps(unknown) + "\n" + '{"job_id": "x", "score":\n',
        encoding="utf-8",
    )

    _run(monkeypatch, "import", "prefilter")

    store = store_mod.Store()
    try:
        stored = store.verdicts("prefilter", PROMPT_VERSION)
    finally:
        store.close()
    assert list(stored) == [jobs[0].id]
    v = stored[jobs[0].id]
    assert v.stage == "prefilter"
    assert v.model == "claude-sonnet-5 (subagent)"
    assert v.prompt_version == PROMPT_VERSION
    assert v.score == 72 and v.relevant and v.summary == "Junior Go role in Helsinki"
    assert v.concerns == ["asks for 3 years"]

    out = capsys.readouterr().out
    assert "imported 1 verdicts" in out
    assert "2 bad lines" in out  # the malformed line and the unknown job_id
    assert "2 missing" in out and "unknown job_id 0000000000000000" in out


LONG_DESCRIPTION = "Go backend work with Kubernetes. " * 60 + "TAIL-OF-THE-DESCRIPTION"


@pytest.fixture
def ranked(data_dir, monkeypatch):
    """Two prefilter verdicts (80 and 20) over two kept jobs; returns (passing, rejected)."""
    jobs = [_job(1, "Junior Go Developer", LONG_DESCRIPTION), _job(2, "Graduate Backend Engineer")]
    _seed(jobs, ["keep", "keep"])
    store = store_mod.Store()
    try:
        for job, score in zip(jobs, (80, 20)):
            store.save_verdict(AIVerdict(job_id=job.id, stage="prefilter", model="claude-sonnet-5 (subagent)",
                                         prompt_version=PROMPT_VERSION, relevant=True, score=score,
                                         language_ok=True, seniority_ok=True, location_ok=True,
                                         summary=f"scored {score}"))
    finally:
        store.close()
    return jobs


def test_export_rank_takes_only_the_prefilter_survivors(ranked, data_dir, monkeypatch, capsys):
    """Below the profile's prefilter_min_score (30) a job never reaches the ranking stage."""
    _run(monkeypatch, "export", "rank", "--top", "5")

    out = data_dir / "exports" / "ai" / "rank"
    assert "career advisor" in (out / "system.txt").read_text(encoding="utf-8")  # the rank system prompt
    entries = _chunk_entries(out)
    assert [e["job_id"] for e in entries] == [ranked[0].id]  # the 20-scorer is left out

    prompt = entries[0]["prompt"]
    assert "TAIL-OF-THE-DESCRIPTION" in prompt  # rank uses the 6000-char budget, not 1500
    assert "truncated" not in prompt
    assert "1 jobs" in capsys.readouterr().out


def test_import_rank_keeps_why_apply(ranked, data_dir, monkeypatch, capsys):
    _run(monkeypatch, "export", "rank", "--top", "5")
    capsys.readouterr()

    line = {"job_id": ranked[0].id, "relevant": True, "score": 88, "language_ok": True,
            "seniority_ok": True, "location_ok": True, "summary": "Strong junior Go fit.",
            "concerns": ["no Kubernetes experience"],
            "why_apply": ["42/Hive projects in C and Go", "English-only team"]}
    verdicts = data_dir / "exports" / "ai" / "rank" / "verdicts"
    verdicts.mkdir(parents=True, exist_ok=True)
    (verdicts / "chunk-01.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")

    _run(monkeypatch, "import", "rank")

    store = store_mod.Store()
    try:
        stored = store.verdicts("rank", PROMPT_VERSION)
    finally:
        store.close()
    v = stored[ranked[0].id]
    assert v.model == "claude-opus-5 (subagent)"
    assert v.why_apply == ["42/Hive projects in C and Go", "English-only team"]
    assert v.concerns == ["no Kubernetes experience"] and v.score == 88
    assert "imported 1 verdicts, 0 bad lines, 0 missing" in capsys.readouterr().out


def test_export_rank_skips_dropped_and_already_ranked_jobs(ranked, data_dir, monkeypatch, capsys):
    """Refill semantics: a job the rule filter now drops is out even with a high prefilter score, and a
    job that already has a rank verdict under the current prompt version is not exported again."""
    dropped = _job(3, "Junior Polish-only Developer", LONG_DESCRIPTION)
    _seed([dropped], ["drop"])
    store = store_mod.Store()
    try:
        store.save_verdict(AIVerdict(job_id=dropped.id, stage="prefilter", model="claude-sonnet-5 (subagent)",
                                     prompt_version=PROMPT_VERSION, relevant=True, score=95,
                                     language_ok=True, seniority_ok=True, location_ok=True, summary="high but dropped"))
        store.save_verdict(AIVerdict(job_id=ranked[0].id, stage="rank", model="claude-opus-5 (subagent)",
                                     prompt_version=PROMPT_VERSION, relevant=True, score=88,
                                     language_ok=True, seniority_ok=True, location_ok=True, summary="already ranked"))
    finally:
        store.close()
    _run(monkeypatch, "export", "rank", "--top", "5")
    assert _chunk_entries(data_dir / "exports" / "ai" / "rank") == []
    assert "0 jobs" in capsys.readouterr().out


# ------------------------------------------------------- LinkedIn description hydration
# The linkedin adapter runs with `fetch_descriptions: false` (slow + rate-limited), so its rows
# are title-only. Ranking looks at ~60 jobs, so those few are topped up from the guest page.


def _bare(source: str, n: int, url: str) -> Job:
    """A job with no description at all — what the linkedin adapter normally yields."""
    return Job(source=source, source_id=str(n), url=url, title=f"Junior Developer {n}",
               company="Acme", country="FI")


def _prefilter_scores(jobs: list[Job], scores: list[int]) -> None:
    store = store_mod.Store()
    try:
        for job, score in zip(jobs, scores):
            store.save_verdict(AIVerdict(job_id=job.id, stage="prefilter", model="claude-sonnet-5 (subagent)",
                                         prompt_version=PROMPT_VERSION, relevant=True, score=score,
                                         language_ok=True, seniority_ok=True, location_ok=True,
                                         summary=f"scored {score}"))
    finally:
        store.close()


def _stored_jobs() -> dict[str, Job]:
    store = store_mod.Store()
    try:
        return {j.id: j for j in store.jobs()}
    finally:
        store.close()


def test_export_rank_hydrates_title_only_linkedin_jobs(data_dir, monkeypatch, capsys):
    li = _bare("linkedin", 11, "https://www.linkedin.com/jobs/view/4392276998")
    other = _bare("duunitori", 12, "https://duunitori.fi/tyopaikat/tyo/12")
    _seed([li, other], ["keep", "keep"])
    _prefilter_scores([li, other], [90, 80])

    fetched: list[str] = []

    def fake_fetch(http, url):
        fetched.append(url)
        return "Go and Kubernetes, English-speaking team.\n\nApply before May."

    monkeypatch.setattr(ai_batches, "fetch_description", fake_fetch)

    _run(monkeypatch, "export", "rank", "--top", "5")

    assert fetched == [li.url]  # only linkedin rows, and only the ones missing a description
    prompts = {e["job_id"]: e["prompt"] for e in _chunk_entries(data_dir / "exports" / "ai" / "rank")}
    assert "Go and Kubernetes, English-speaking team." in prompts[li.id]
    assert "Go and Kubernetes" not in prompts[other.id]  # the non-linkedin job is left alone

    stored = _stored_jobs()
    assert stored[li.id].description.startswith("Go and Kubernetes")  # persisted, not just in the prompt
    assert stored[other.id].description is None
    assert "hydrated 1/1 linkedin descriptions" in capsys.readouterr().out


def test_export_rank_leaves_linkedin_jobs_that_already_have_a_description(data_dir, monkeypatch, capsys):
    li = Job(source="linkedin", source_id="13", url="https://www.linkedin.com/jobs/view/13",
             title="Junior Developer 13", company="Acme", country="FI",
             description="Already scraped with fetch_descriptions on.")
    _seed([li], ["keep"])
    _prefilter_scores([li], [90])

    def explode(http, url):  # pragma: no cover - must never be called
        raise AssertionError(f"refetched {url}")

    monkeypatch.setattr(ai_batches, "fetch_description", explode)

    _run(monkeypatch, "export", "rank", "--top", "5")

    assert "hydrated" not in capsys.readouterr().out  # nothing to do, no summary line


def test_export_rank_caps_the_number_of_description_fetches(data_dir, monkeypatch, capsys):
    jobs = [_bare("linkedin", n, f"https://www.linkedin.com/jobs/view/{n}") for n in (21, 22, 23)]
    _seed(jobs, ["keep"] * 3)
    _prefilter_scores(jobs, [90, 80, 70])

    fetched: list[str] = []

    def fake_fetch(http, url):
        fetched.append(url)
        return "Fetched description text."

    monkeypatch.setattr(ai_batches, "fetch_description", fake_fetch)

    _run(monkeypatch, "export", "rank", "--top", "5", "--max-fetch", "2")

    assert fetched == [jobs[0].url, jobs[1].url]  # best prefilter scores first
    assert _stored_jobs()[jobs[2].id].description is None
    assert "hydrated 2/2 linkedin descriptions" in capsys.readouterr().out


def test_export_rank_survives_a_description_fetch_that_comes_back_empty(data_dir, monkeypatch, capsys):
    li = _bare("linkedin", 31, "https://www.linkedin.com/jobs/view/31")
    _seed([li], ["keep"])
    _prefilter_scores([li], [90])
    monkeypatch.setattr(ai_batches, "fetch_description", lambda http, url: None)

    _run(monkeypatch, "export", "rank", "--top", "5")

    prompts = {e["job_id"]: e["prompt"] for e in _chunk_entries(data_dir / "exports" / "ai" / "rank")}
    assert "no description available" in prompts[li.id]  # the job is still ranked, on its title
    assert _stored_jobs()[li.id].description is None
    out = capsys.readouterr().out
    assert "hydrated 0/1 linkedin descriptions" in out
    assert "1 jobs" in out


def test_export_prefilter_never_fetches_descriptions(data_dir, monkeypatch, capsys):
    """600+ title-only linkedin rows reach the prefilter; hydrating them all is the thing we avoid."""
    li = _bare("linkedin", 41, "https://www.linkedin.com/jobs/view/41")
    _seed([li], ["keep"])

    def explode(http, url):  # pragma: no cover - must never be called
        raise AssertionError(f"fetched {url} in the prefilter stage")

    monkeypatch.setattr(ai_batches, "fetch_description", explode)

    _run(monkeypatch, "export", "prefilter")

    assert "1 jobs" in capsys.readouterr().out


# ------------------------------------------------- wide hydration, refill export and `boost`
# `hydrate linkedin` fetches descriptions for the title-only linkedin rows among the best `--top`
# prefilter survivors and clears their verdicts, so the next prefilter pass re-screens them with
# the description in hand. hydration.jsonl records where each job stood before that, so the owner
# can see from real data how deep `--top` has to go.


def _rank_scores(jobs: list[Job], scores: list[int]) -> None:
    store = store_mod.Store()
    try:
        for job, score in zip(jobs, scores):
            store.save_verdict(AIVerdict(job_id=job.id, stage="rank", model="claude-opus-5 (subagent)",
                                         prompt_version=PROMPT_VERSION, relevant=True, score=score,
                                         language_ok=True, seniority_ok=True, location_ok=True,
                                         summary=f"ranked {score}"))
    finally:
        store.close()


def _stored_verdicts(stage: str) -> dict:
    store = store_mod.Store()
    try:
        return store.verdicts(stage, PROMPT_VERSION)
    finally:
        store.close()


def _hydration_log(data_dir: Path) -> list[dict]:
    path = data_dir / "exports" / "ai" / "hydration.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_hydrate_linkedin_logs_the_before_picture_and_clears_the_verdicts(data_dir, monkeypatch, capsys):
    inside = _bare("linkedin", 51, "https://www.linkedin.com/jobs/view/51")
    other = _bare("duunitori", 52, "https://duunitori.fi/tyopaikat/tyo/52")
    outside = _bare("linkedin", 53, "https://www.linkedin.com/jobs/view/53")
    _seed([inside, other, outside], ["keep", "keep", "keep"])
    _prefilter_scores([inside, other, outside], [90, 80, 70])
    _rank_scores([inside], [55])

    fetched: list[str] = []

    def fake_fetch(http, url):
        fetched.append(url)
        return "Go and Kubernetes, English-speaking team."

    monkeypatch.setattr(ai_batches, "fetch_description", fake_fetch)

    _run(monkeypatch, "hydrate", "linkedin", "--top", "2")

    assert fetched == [inside.url]  # linkedin only, and only inside the top 2

    (line,) = _hydration_log(data_dir)
    assert line["job_id"] == inside.id
    assert line["title"] == inside.title and line["source"] == "linkedin"
    assert line["position_before"] == 1  # 1-based rank among the prefilter survivors
    assert line["prefilter_score_before"] == 90
    assert line["rank_score_before"] == 55
    assert line["when"].endswith("+00:00")

    stored = _stored_jobs()
    assert stored[inside.id].description.startswith("Go and Kubernetes")
    assert stored[outside.id].description is None and stored[other.id].description is None

    pre, ranks = _stored_verdicts("prefilter"), _stored_verdicts("rank")
    assert set(pre) == {other.id, outside.id}  # the hydrated job is queued for re-screening
    assert ranks == {}
    assert "hydrated 1/1 linkedin descriptions; 2 verdicts cleared" in capsys.readouterr().out


def test_hydrate_linkedin_keeps_the_verdicts_when_the_fetch_comes_back_empty(data_dir, monkeypatch, capsys):
    li = _bare("linkedin", 61, "https://www.linkedin.com/jobs/view/61")
    _seed([li], ["keep"])
    _prefilter_scores([li], [90])
    monkeypatch.setattr(ai_batches, "fetch_description", lambda http, url: None)

    _run(monkeypatch, "hydrate", "linkedin")

    assert _hydration_log(data_dir) == []
    assert set(_stored_verdicts("prefilter")) == {li.id}  # nothing changed, nothing to re-screen
    assert "hydrated 0/1 linkedin descriptions; 0 verdicts cleared" in capsys.readouterr().out


def test_hydrate_linkedin_honours_max_fetch_and_the_rule_filter(data_dir, monkeypatch, capsys):
    jobs = [_bare("linkedin", n, f"https://www.linkedin.com/jobs/view/{n}") for n in (71, 72, 73)]
    dropped = _bare("linkedin", 74, "https://www.linkedin.com/jobs/view/74")
    _seed(jobs, ["keep"] * 3)
    _seed([dropped], ["drop"])
    _prefilter_scores(jobs, [90, 80, 70])
    _prefilter_scores([dropped], [95])

    fetched: list[str] = []

    def fake_fetch(http, url):
        fetched.append(url)
        return "Fetched description text."

    monkeypatch.setattr(ai_batches, "fetch_description", fake_fetch)

    _run(monkeypatch, "hydrate", "linkedin", "--top", "10", "--max-fetch", "2")

    assert fetched == [jobs[0].url, jobs[1].url]  # best prefilter scores first, dropped job skipped
    assert _stored_jobs()[jobs[2].id].description is None
    assert [line["position_before"] for line in _hydration_log(data_dir)] == [1, 2]
    assert "hydrated 2/2 linkedin descriptions; 2 verdicts cleared" in capsys.readouterr().out


def test_export_prefilter_only_exports_jobs_without_a_verdict_unless_all(data_dir, monkeypatch, capsys):
    """Refill semantics: after hydration only the cleared rows go back to the Sonnet pass."""
    jobs = [_job(1, "Junior Go Developer"), _job(2, "Graduate Backend Engineer"),
            _job(3, "Trainee QA Automation")]
    _seed(jobs, ["keep", "keep", "review"])
    _prefilter_scores(jobs[:2], [90, 80])

    _run(monkeypatch, "export", "prefilter")

    out = data_dir / "exports" / "ai" / "prefilter"
    assert [e["job_id"] for e in _chunk_entries(out)] == [jobs[2].id]
    assert "1 jobs" in capsys.readouterr().out

    _run(monkeypatch, "export", "prefilter", "--all")

    assert {e["job_id"] for e in _chunk_entries(out)} == {j.id for j in jobs}
    assert "3 jobs" in capsys.readouterr().out


def _hydrate_two(data_dir, monkeypatch) -> list[Job]:
    """Hydrate two linkedin jobs (prefilter 90 and 80) and return them."""
    jobs = [_bare("linkedin", n, f"https://www.linkedin.com/jobs/view/{n}") for n in (81, 82)]
    _seed(jobs, ["keep", "keep"])
    _prefilter_scores(jobs, [90, 80])
    monkeypatch.setattr(ai_batches, "fetch_description", lambda http, url: "Go, Kubernetes, English.")
    _run(monkeypatch, "hydrate", "linkedin")
    return jobs


def test_boost_shows_the_score_moves_and_the_deepest_useful_position(data_dir, monkeypatch, capsys):
    risen, fallen = _hydrate_two(data_dir, monkeypatch)
    capsys.readouterr()
    _prefilter_scores([risen, fallen], [95, 40])  # re-screened with the description in hand
    _rank_scores([risen], [88])

    _run(monkeypatch, "boost")

    out = capsys.readouterr().out
    assert risen.title[:40] in out and fallen.title[:40] in out
    assert "90 → 95" in out and "80 → 40" in out
    assert "88" in out
    assert "hydrated: 2" in out
    assert "rose: 1" in out and "fell: 1" in out
    assert "now ranked: 1" in out
    assert "deepest position_before that made the final ranked list: 1" in out


def test_boost_without_a_single_ranked_job(data_dir, monkeypatch, capsys):
    jobs = _hydrate_two(data_dir, monkeypatch)
    capsys.readouterr()
    _prefilter_scores(jobs, [90, 80])  # same scores back, nothing ranked yet

    _run(monkeypatch, "boost")

    out = capsys.readouterr().out
    assert "now ranked: 0" in out
    assert "rose: 0" in out and "fell: 0" in out
    assert "deepest position_before that made the final ranked list: none" in out


def test_boost_without_a_hydration_log(data_dir, monkeypatch, capsys):
    _run(monkeypatch, "boost")
    assert "no hydration" in capsys.readouterr().out.lower()
