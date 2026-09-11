"""Tests for ``scripts/ai_batches.py``, the API-free way to run the two AI stages.

The script is not a package module, so it is loaded from its path with importlib. Both its own
data directory is pointed at ``tmp_path`` through ``JOBSCRAPER_DATA_DIR``; nothing here touches
the network or the repo's ``data/``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from jobscraper import config
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
    monkeypatch.setenv("JOBSCRAPER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store_mod, "DB_OVERRIDE", None)
    monkeypatch.delenv("JOBSCRAPER_DB", raising=False)
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


def test_a_verdict_from_a_compatible_prompt_version_still_counts_as_ranked(ranked, data_dir,
                                                                           monkeypatch, capsys):
    """Owner, 2026-09-11: a wording change that keeps the scale does not re-queue the whole list —
    the old verdicts stay in use until their jobs age out."""
    from jobscraper.ai.prompts import COMPATIBLE_PROMPT_VERSIONS

    older = next(v for v in COMPATIBLE_PROMPT_VERSIONS if v != PROMPT_VERSION)
    store = store_mod.Store()
    try:
        store.save_verdict(AIVerdict(job_id=ranked[0].id, stage="rank",
                                     model="claude-opus-5 (subagent)", prompt_version=older,
                                     relevant=True, score=88, language_ok=True, seniority_ok=True,
                                     location_ok=True, summary="ranked before the wording change"))
    finally:
        store.close()

    _run(monkeypatch, "export", "rank", "--top", "5")

    assert _chunk_entries(data_dir / "exports" / "ai" / "rank") == []
    assert "0 jobs" in capsys.readouterr().out


def test_export_rank_writes_the_reference_scores_next_to_the_system_prompt(ranked, data_dir,
                                                                          monkeypatch, capsys):
    """The window is a slice of a queue; the anchors are what keeps every slice on one scale."""
    already = _job(3, "Junior Kotlin Developer", LONG_DESCRIPTION)
    _seed([already], ["keep"])
    store = store_mod.Store()
    try:
        store.save_verdict(AIVerdict(job_id=already.id, stage="prefilter",
                                     model="claude-sonnet-5 (subagent)", prompt_version=PROMPT_VERSION,
                                     relevant=True, score=90, language_ok=True, seniority_ok=True,
                                     location_ok=True, summary="screened"))
        store.save_verdict(AIVerdict(job_id=already.id, stage="rank", model="claude-opus-5 (subagent)",
                                     prompt_version=PROMPT_VERSION, relevant=True, score=86,
                                     language_ok=True, seniority_ok=True, location_ok=True,
                                     summary="Strong Kotlin fit."))
    finally:
        store.close()

    _run(monkeypatch, "export", "rank", "--top", "5")

    anchors = (data_dir / "exports" / "ai" / "rank" / "anchors.txt").read_text(encoding="utf-8")
    assert anchors.startswith("REFERENCE SCORES (fixed;")
    assert "- Junior Kotlin Developer · Acme · FI · score 86 · Strong Kotlin fit." in anchors
    assert [e["job_id"] for e in _chunk_entries(data_dir / "exports" / "ai" / "rank")] == [ranked[0].id]
    assert "+ anchors.txt (1 reference scores)" in capsys.readouterr().out


def test_export_rank_without_a_single_ranked_job_removes_a_stale_anchor_file(ranked, data_dir,
                                                                            monkeypatch, capsys):
    out = data_dir / "exports" / "ai" / "rank"
    (out / "verdicts").mkdir(parents=True, exist_ok=True)
    (out / "anchors.txt").write_text("REFERENCE SCORES from a previous profile\n", encoding="utf-8")

    _run(monkeypatch, "export", "rank", "--top", "5")

    assert not (out / "anchors.txt").exists()
    assert "no reference scores yet" in capsys.readouterr().out


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


def test_hydrate_linkedin_defaults_to_the_top_480_window(data_dir, monkeypatch):
    """Two measured runs found useful hits down to position 462, so the window covers 480."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(ai_batches, "hydrate_top_linkedin",
                        lambda top, max_fetch: calls.append((top, max_fetch)))

    _run(monkeypatch, "hydrate", "linkedin")

    assert calls == [(480, 480)]


def test_hydrate_linkedin_explicit_top_and_max_fetch_win_over_the_defaults(data_dir, monkeypatch):
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(ai_batches, "hydrate_top_linkedin",
                        lambda top, max_fetch: calls.append((top, max_fetch)))

    _run(monkeypatch, "hydrate", "linkedin", "--top", "240", "--max-fetch", "100")

    assert calls == [(240, 100)]


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


# ------------------------------------------------------------------ named profiles
# The script has no Typer callback of its own, so `--profile NAME` (or $JOBSCRAPER_PROFILE) has to
# switch `jobscraper.config` before anything opens a Store or reads settings.

PROFILE_YAML = """\
name: Felipe
summary: A second candidate.
prompt:
  role_label: embedded systems engineer
"""
PROFILE_SOURCES_YAML = "sources:\n  arbeitnow:\n    enabled: true\n"


@pytest.fixture
def profile_dirs(tmp_path, monkeypatch):
    """Base config/data under tmp_path plus one complete profile directory named 'x'."""
    monkeypatch.setenv("JOBSCRAPER_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("JOBSCRAPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(store_mod, "DB_OVERRIDE", None)
    monkeypatch.delenv("JOBSCRAPER_DB", raising=False)
    d = tmp_path / "config" / "profiles" / "x"
    d.mkdir(parents=True)
    (d / "profile.yaml").write_text(PROFILE_YAML, encoding="utf-8")
    (d / "sources.yaml").write_text(PROFILE_SOURCES_YAML, encoding="utf-8")
    return tmp_path


def _seed_in_profile(name: str, jobs: list[Job], statuses: list[str]) -> None:
    """Seed the profile's own database, then leave the process without an active profile."""
    config.use_profile(name)
    try:
        _seed(jobs, statuses)
    finally:
        config.use_profile(None)


def test_export_uses_the_named_profiles_data_dir_and_prompts(profile_dirs, monkeypatch, capsys):
    jobs = [_job(1, "Junior Go Developer"), _job(2, "Graduate Backend Engineer")]
    _seed_in_profile("x", jobs, ["keep", "keep"])

    _run(monkeypatch, "export", "prefilter", "--profile", "x")

    out = profile_dirs / "data" / "profiles" / "x" / "exports" / "ai" / "prefilter"
    assert (out / "system.txt").read_text(encoding="utf-8").startswith(
        "You are screening job postings for a specific embedded systems engineer."
    )
    assert {e["job_id"] for e in _chunk_entries(out)} == {j.id for j in jobs}
    assert not (profile_dirs / "data" / "exports").exists()  # nothing under the default profile
    assert "2 jobs" in capsys.readouterr().out


def test_the_profile_can_come_from_the_environment(profile_dirs, monkeypatch, capsys):
    _seed_in_profile("x", [_job(1, "Junior Go Developer")], ["keep"])
    monkeypatch.setenv("JOBSCRAPER_PROFILE", "x")

    _run(monkeypatch, "export", "prefilter")

    out = profile_dirs / "data" / "profiles" / "x" / "exports" / "ai" / "prefilter"
    assert len(_chunk_entries(out)) == 1
    assert "1 jobs" in capsys.readouterr().out


def test_an_unknown_profile_is_a_clear_error(profile_dirs, monkeypatch):
    with pytest.raises(config.ProfileError) as exc:
        _run(monkeypatch, "export", "prefilter", "--profile", "ghost")
    assert "ghost" in str(exc.value)


def test_import_and_boost_also_follow_the_profile(profile_dirs, monkeypatch, capsys):
    job = _job(1, "Junior Go Developer")
    _seed_in_profile("x", [job], ["keep"])
    _run(monkeypatch, "export", "prefilter", "--profile", "x")
    capsys.readouterr()

    verdicts = profile_dirs / "data" / "profiles" / "x" / "exports" / "ai" / "prefilter" / "verdicts"
    verdicts.mkdir(parents=True, exist_ok=True)
    (verdicts / "chunk-01.jsonl").write_text(
        json.dumps({"job_id": job.id, "relevant": True, "score": 72, "language_ok": True,
                    "seniority_ok": True, "location_ok": True, "summary": "fits"}) + "\n",
        encoding="utf-8",
    )

    _run(monkeypatch, "import", "prefilter", "--profile", "x")
    assert "imported 1 verdicts" in capsys.readouterr().out

    config.use_profile("x")
    try:
        store = store_mod.Store()
        try:
            assert list(store.verdicts("prefilter", PROMPT_VERSION)) == [job.id]
        finally:
            store.close()
    finally:
        config.use_profile(None)

    _run(monkeypatch, "boost", "--profile", "x")
    out = capsys.readouterr().out
    assert "no hydration log yet" in out
    assert str(profile_dirs / "data" / "profiles" / "x") in out


def test_hydrate_follows_the_profile(profile_dirs, monkeypatch, capsys):
    li = _bare("linkedin", 91, "https://www.linkedin.com/jobs/view/91")
    config.use_profile("x")
    try:
        _seed([li], ["keep"])
        _prefilter_scores([li], [90])
    finally:
        config.use_profile(None)
    monkeypatch.setattr(ai_batches, "fetch_description", lambda http, url: "Go, Kubernetes, English.")

    _run(monkeypatch, "hydrate", "linkedin", "--profile", "x")

    log = profile_dirs / "data" / "profiles" / "x" / "exports" / "ai" / "hydration.jsonl"
    assert [json.loads(line)["job_id"] for line in log.read_text(encoding="utf-8").splitlines()] == [li.id]
    assert "hydrated 1/1 linkedin descriptions" in capsys.readouterr().out


# ------------------------------------------------------------------------ --sample
# A "1 in N" sanity run: keep positions 0, N, 2N, ... of the stage's ordered candidate list, so a
# handful of postings can be pushed through the whole subagent loop before committing to it.


def test_export_prefilter_sample_keeps_every_nth_candidate(data_dir, monkeypatch, capsys):
    jobs = [_job(n, f"Junior Developer {n:02d}") for n in range(45)]  # titles sort like the index
    _seed(jobs, ["keep"] * 45)

    _run(monkeypatch, "export", "prefilter", "--sample", "20")

    out = data_dir / "exports" / "ai" / "prefilter"
    assert [e["job_id"] for e in _chunk_entries(out)] == [jobs[0].id, jobs[20].id, jobs[40].id]
    printed = capsys.readouterr().out
    assert "3 jobs" in printed
    assert "45" in printed  # how many candidates the 3 were sampled out of


def test_export_prefilter_sample_one_exports_everything(data_dir, monkeypatch, capsys):
    jobs = [_job(n, f"Junior Developer {n:02d}") for n in range(5)]
    _seed(jobs, ["keep"] * 5)

    _run(monkeypatch, "export", "prefilter", "--sample", "1")

    assert len(_chunk_entries(data_dir / "exports" / "ai" / "prefilter")) == 5
    assert "5 jobs" in capsys.readouterr().out


def test_export_rank_sample_applies_after_top(data_dir, monkeypatch, capsys):
    jobs = [_job(n, f"Junior Developer {n:02d}", LONG_DESCRIPTION) for n in range(10)]
    _seed(jobs, ["keep"] * 10)
    _prefilter_scores(jobs, list(range(99, 89, -1)))  # jobs[0] best, jobs[9] worst

    _run(monkeypatch, "export", "rank", "--top", "6", "--sample", "3")

    entries = _chunk_entries(data_dir / "exports" / "ai" / "rank")
    assert [e["job_id"] for e in entries] == [jobs[0].id, jobs[3].id]  # positions 0 and 3 of the top 6
    printed = capsys.readouterr().out
    assert "2 jobs" in printed and "6" in printed


def test_export_rank_sample_bounds_the_description_fetches(data_dir, monkeypatch, capsys):
    """Sampling happens before hydration: a job that is not exported is not fetched either."""
    jobs = [_bare("linkedin", n, f"https://www.linkedin.com/jobs/view/{n}") for n in range(101, 105)]
    _seed(jobs, ["keep"] * 4)
    _prefilter_scores(jobs, [93, 92, 91, 90])

    fetched: list[str] = []

    def fake_fetch(http, url):
        fetched.append(url)
        return "Fetched description text."

    monkeypatch.setattr(ai_batches, "fetch_description", fake_fetch)

    _run(monkeypatch, "export", "rank", "--top", "4", "--sample", "2")

    assert fetched == [jobs[0].url, jobs[2].url]
    assert "2 jobs" in capsys.readouterr().out


# ------------------------------------------------- the rank round and its stop rule
# `export rank` hands out ONE window of the queue and `import rank` records what that window did
# to the effective top N, so the owner's loop has a termination signal instead of a fixed cut.


def _rank_rule(monkeypatch, **values):
    """Pin the rank stop rule (and the top it watches) for one test."""
    settings = ai_batches.load_settings()
    for key, value in values.items():
        setattr(settings.profile.ai, key, value)
    monkeypatch.setattr(ai_batches, "load_settings", lambda *a, **kw: settings)
    return settings


@pytest.fixture
def queue(data_dir):
    """Eight rule-kept jobs with descending screen scores: a queue with a known order."""
    jobs = [_job(n, f"Junior Developer {n:02d}", LONG_DESCRIPTION) for n in range(8)]
    _seed(jobs, ["keep"] * 8)
    _prefilter_scores(jobs, list(range(99, 91, -1)))
    return jobs


def _rank_state(data_dir: Path) -> dict:
    return json.loads((data_dir / "exports" / "ai" / "rank" / "state.json").read_text(encoding="utf-8"))


def _answer_rank(data_dir: Path, scores: dict[str, int] | None = None, default: int = 10) -> None:
    """Answer whatever the last `export rank` wrote, as the Opus subagent would."""
    out = data_dir / "exports" / "ai" / "rank"
    scores = scores or {}
    lines = [json.dumps({"job_id": e["job_id"], "relevant": True,
                         "score": scores.get(e["job_id"], default), "language_ok": True,
                         "seniority_ok": True, "location_ok": True, "summary": "ranked",
                         "concerns": [], "why_apply": []})
             for e in _chunk_entries(out)]
    (out / "verdicts").mkdir(parents=True, exist_ok=True)
    (out / "verdicts" / "chunk-01.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_export_rank_opens_a_round_with_the_first_window_then_narrows(queue, data_dir, monkeypatch,
                                                                      capsys):
    """Nothing ranked yet means nothing to stop on, so the first window is ai.rank_top_n deep."""
    _rank_rule(monkeypatch, rank_top_n=3, rank_window=2, rank_patience=30, rank_budget=0,
               refine_top_n=2)

    _run(monkeypatch, "export", "rank")
    exported = [e["job_id"] for e in _chunk_entries(data_dir / "exports" / "ai" / "rank")]
    assert exported == [j.id for j in queue[:3]]
    out = capsys.readouterr().out
    assert "window 1 of this round, 8 in the queue" in out
    assert "stop rule: miss run 0/30, 0 ranked this round → continue" in out
    assert _rank_state(data_dir)["exported"] == exported

    _answer_rank(data_dir, {queue[0].id: 95})
    _run(monkeypatch, "import", "rank")
    capsys.readouterr()

    _run(monkeypatch, "export", "rank")
    # the round is under way now: the windows after the first are ai.rank_window wide
    assert [e["job_id"] for e in _chunk_entries(data_dir / "exports" / "ai" / "rank")] == \
        [j.id for j in queue[3:5]]
    assert "window 2 of this round, 5 in the queue" in capsys.readouterr().out


def test_import_rank_records_the_window_and_the_entrants(queue, data_dir, monkeypatch, capsys):
    _rank_rule(monkeypatch, rank_top_n=3, rank_window=2, rank_patience=30, rank_budget=0,
               refine_top_n=2)
    _run(monkeypatch, "export", "rank")
    _answer_rank(data_dir, {queue[0].id: 95, queue[1].id: 90})  # the third is a 10
    capsys.readouterr()

    _run(monkeypatch, "import", "rank")

    state = _rank_state(data_dir)
    assert [w["ranked"] for w in state["windows"]] == [[j.id for j in queue[:3]]]
    assert state["windows"][0]["entered"] == 2  # the 95 and the 90 fill the watched top 2
    assert state["top_before"] == [queue[0].id, queue[1].id]
    assert state["exported"] == [] and state["stop_reason"] == "continue"
    assert "stop rule: miss run 0/30, 3 ranked this round → continue" in capsys.readouterr().out


def test_the_rank_round_stops_when_the_top_stops_gaining_entrants(queue, data_dir, monkeypatch,
                                                                  capsys):
    """Two windows of nothing new: patience runs out and the export refuses to ask for more."""
    _rank_rule(monkeypatch, rank_top_n=2, rank_window=2, rank_patience=4, rank_budget=0,
               refine_top_n=2)
    _run(monkeypatch, "export", "rank")
    _answer_rank(data_dir, {queue[0].id: 95, queue[1].id: 90})
    _run(monkeypatch, "import", "rank")
    for _ in range(2):  # two windows of 10s, which enter nothing
        _run(monkeypatch, "export", "rank")
        _answer_rank(data_dir)
        _run(monkeypatch, "import", "rank")
    assert "stop rule: miss run 4/4, 6 ranked this round → stop (patience)" in capsys.readouterr().out

    _run(monkeypatch, "export", "rank")

    out = capsys.readouterr().out
    assert "→ stop (patience)" in out
    assert "nothing exported — 6 jobs ranked in 3 windows this round" in out
    assert not list((data_dir / "exports" / "ai" / "rank").glob("chunk-*.json"))


def test_the_rank_round_stops_when_the_budget_is_spent(queue, data_dir, monkeypatch, capsys):
    _rank_rule(monkeypatch, rank_top_n=2, rank_window=2, rank_patience=0, rank_budget=2,
               refine_top_n=2)
    _run(monkeypatch, "export", "rank")
    _answer_rank(data_dir, {queue[0].id: 95, queue[1].id: 90})
    _run(monkeypatch, "import", "rank")
    assert "stop rule: miss run 0/0, 2/2 budget → stop (budget)" in capsys.readouterr().out

    _run(monkeypatch, "export", "rank")
    assert "→ stop (budget)" in capsys.readouterr().out


def test_export_rank_caps_the_window_to_the_remaining_budget(queue, data_dir, monkeypatch, capsys):
    """Seen live: 75 of 90 spent, a 30-job window went out anyway. The export must size the last
    window to what the round can still pay for (an explicit --top keeps its exact meaning)."""
    _rank_rule(monkeypatch, rank_top_n=2, rank_window=3, rank_patience=0, rank_budget=4, refine_top_n=2)
    _run(monkeypatch, "export", "rank")
    _answer_rank(data_dir, {queue[0].id: 95, queue[1].id: 90})
    _run(monkeypatch, "import", "rank")
    capsys.readouterr()
    _run(monkeypatch, "export", "rank")
    out = capsys.readouterr().out
    assert "rank: 2 jobs of 2 candidates (window 2 of this round" in out
    assert [e["job_id"] for e in _chunk_entries(data_dir / "exports" / "ai" / "rank")] == [queue[2].id, queue[3].id]


def test_the_rank_round_stops_when_the_queue_runs_out(data_dir, monkeypatch, capsys):
    jobs = [_job(n, f"Junior Developer {n:02d}", LONG_DESCRIPTION) for n in range(2)]
    _seed(jobs, ["keep"] * 2)
    _prefilter_scores(jobs, [99, 98])
    _rank_rule(monkeypatch, rank_top_n=5, rank_window=5, rank_patience=30, rank_budget=90,
               refine_top_n=2)

    _run(monkeypatch, "export", "rank")
    _answer_rank(data_dir)
    _run(monkeypatch, "import", "rank")

    assert "stop rule: miss run 0/30, 2/90 budget → stop (queue empty)" in capsys.readouterr().out


def test_export_rank_reset_round_starts_the_stop_rule_over(queue, data_dir, monkeypatch, capsys):
    _rank_rule(monkeypatch, rank_top_n=2, rank_window=2, rank_patience=2, rank_budget=0,
               refine_top_n=2)
    _run(monkeypatch, "export", "rank")
    _answer_rank(data_dir, {queue[0].id: 95, queue[1].id: 90})  # both fill the watched top 2
    _run(monkeypatch, "import", "rank")
    _run(monkeypatch, "export", "rank")
    _answer_rank(data_dir)  # a window of 10s enters nothing, and patience is 2
    _run(monkeypatch, "import", "rank")
    _run(monkeypatch, "export", "rank")
    assert "→ stop (patience)" in capsys.readouterr().out

    _run(monkeypatch, "export", "rank", "--reset-round")

    out = capsys.readouterr().out
    assert "stop rule: miss run 0/2, 0 ranked this round → continue" in out
    assert _rank_state(data_dir)["windows"] == []
    # the queue skips the four jobs the first round ranked, so the new round opens below them
    assert [e["job_id"] for e in _chunk_entries(data_dir / "exports" / "ai" / "rank")] == \
        [j.id for j in queue[4:6]]


def test_export_rank_window_and_top_set_the_width_by_hand(queue, data_dir, monkeypatch, capsys):
    _rank_rule(monkeypatch, rank_top_n=2, rank_window=2, rank_patience=30, rank_budget=0,
               refine_top_n=2)

    _run(monkeypatch, "export", "rank", "--window", "4")
    assert len(_chunk_entries(data_dir / "exports" / "ai" / "rank")) == 4

    _run(monkeypatch, "export", "rank", "--top", "5")
    assert len(_chunk_entries(data_dir / "exports" / "ai" / "rank")) == 5
    capsys.readouterr()


def test_export_rank_clears_the_previous_window_answers(queue, data_dir, monkeypatch, capsys):
    """Each window is answered in the same verdicts/ directory; a stale answer must not linger."""
    _rank_rule(monkeypatch, rank_top_n=2, rank_window=2, rank_patience=30, rank_budget=0,
               refine_top_n=2)
    _run(monkeypatch, "export", "rank")
    _answer_rank(data_dir)
    _run(monkeypatch, "import", "rank")

    _run(monkeypatch, "export", "rank")
    assert not list((data_dir / "exports" / "ai" / "rank" / "verdicts").glob("chunk-*.jsonl"))
    _answer_rank(data_dir)
    _run(monkeypatch, "import", "rank")
    assert "imported 2 verdicts, 0 bad lines, 0 missing" in capsys.readouterr().out


# --------------------------------------------------------------------------- refine
# The refine pass is a single request over the whole shortlist, so its export is one batch.json
# rather than chunks, and its answer is one JSON object matching the Refinement schema.


def _refine_dir(data_dir: Path) -> Path:
    return data_dir / "exports" / "ai" / "refine"


@pytest.fixture
def shortlist(data_dir):
    """Three ranked jobs (90/80/70) the rule filter keeps, plus one it drops at 95."""
    jobs = [_job(n, f"Junior Developer {n}", LONG_DESCRIPTION) for n in range(1, 4)]
    dropped = _job(9, "Junior Polish-only Developer", LONG_DESCRIPTION)
    _seed([*jobs, dropped], ["keep", "keep", "keep", "drop"])
    _rank_scores([*jobs, dropped], [90, 80, 70, 95])
    return jobs


def test_export_refine_writes_one_prompt_for_the_whole_shortlist(shortlist, data_dir, monkeypatch, capsys):
    _run(monkeypatch, "export", "refine", "--top", "2")

    out = _refine_dir(data_dir)
    system = (out / "system.txt").read_text(encoding="utf-8")
    assert "career advisor" in system and "REFINEMENT PASS" in system

    batch = json.loads((out / "batch.json").read_text(encoding="utf-8"))
    assert batch["job_ids"] == [shortlist[0].id, shortlist[1].id]  # best two, the drop left out
    assert set(batch) == {"prompt", "job_ids", "anchors"}
    assert batch["anchors"] == []  # first round: nothing is placed yet
    for job in shortlist[:2]:
        assert f"### job_id: {job.id}" in batch["prompt"]
    assert shortlist[2].id not in batch["prompt"]
    assert "TAIL-OF-THE-DESCRIPTION" in batch["prompt"]  # the 6000-char budget, as for rank
    assert "ALREADY PLACED" not in batch["prompt"]
    assert not list(out.glob("chunk-*.json"))
    assert "2 new jobs against 0 already placed" in capsys.readouterr().out


def _refine_scores(jobs: list[Job], scores: list[int]) -> None:
    store = store_mod.Store()
    try:
        for position, (job, score) in enumerate(zip(jobs, scores), start=1):
            store.save_verdict(AIVerdict(job_id=job.id, stage="refine", model="claude-fable-5-1 (subagent)",
                                         prompt_version=PROMPT_VERSION, relevant=True, score=score,
                                         position=position, language_ok=True, seniority_ok=True,
                                         location_ok=True, summary=f"refined {score}"))
    finally:
        store.close()


def test_export_refine_skips_a_shortlist_that_was_already_refined(shortlist, data_dir, monkeypatch, capsys):
    """The pass compares the shortlist against itself: unchanged membership means nothing new to compare."""
    _refine_scores(shortlist[:2], [85, 75])
    _run(monkeypatch, "export", "refine", "--top", "2")
    assert not (_refine_dir(data_dir) / "batch.json").exists()
    out = capsys.readouterr().out
    assert "already carry a refine verdict" in out
    # The owner repeats export -> subagent -> import until this sentence appears.
    assert "top 2 fully refined" in out


def _widths(monkeypatch, *, top: int, first_pass: int):
    """Pin the two shortlist widths for one test, whatever the repo's profile says."""
    settings = ai_batches.load_settings()
    settings.profile.ai.refine_top_n = top
    settings.profile.ai.refine_first_pass = first_pass
    monkeypatch.setattr(ai_batches, "load_settings", lambda *a, **kw: settings)


@pytest.fixture
def wide_shortlist(data_dir):
    """Six rule-kept jobs ranked 95 down to 87 — enough to see the window move."""
    jobs = [_job(n, f"Junior Developer {n}", LONG_DESCRIPTION) for n in range(1, 7)]
    _seed(jobs, ["keep"] * 6)
    _rank_scores(jobs, [95, 91, 90, 89, 88, 87])
    return jobs


def test_export_refine_takes_the_effective_top_not_the_rank_top(wide_shortlist, data_dir,
                                                                monkeypatch, capsys):
    """Three of the five refined jobs were marked down, so the sixth-ranked one takes their place."""
    _refine_scores(wide_shortlist[:5], [80, 78, 30, 29, 28])

    _run(monkeypatch, "export", "refine", "--top", "3")

    batch = json.loads((_refine_dir(data_dir) / "batch.json").read_text(encoding="utf-8"))
    assert batch["job_ids"] == [wide_shortlist[5].id]   # never in the rank-score top 3
    assert batch["anchors"] == [wide_shortlist[0].id, wide_shortlist[1].id]
    assert "1 new jobs against 2 already placed" in capsys.readouterr().out


def test_export_refine_opens_with_the_wider_first_pass_width(wide_shortlist, data_dir,
                                                             monkeypatch, capsys):
    """Nothing refined yet: the first request reaches past the width it has to keep refined."""
    _widths(monkeypatch, top=2, first_pass=4)

    _run(monkeypatch, "export", "refine")

    batch = json.loads((_refine_dir(data_dir) / "batch.json").read_text(encoding="utf-8"))
    assert batch["job_ids"] == [j.id for j in wide_shortlist[:4]]
    assert "4 new jobs against 0 already placed" in capsys.readouterr().out


def test_export_refine_narrows_to_the_top_once_something_is_refined(wide_shortlist, data_dir,
                                                                    monkeypatch, capsys):
    """After the opening pass the width is the invariant one, not the wider first-pass one."""
    _widths(monkeypatch, top=2, first_pass=4)
    _refine_scores(wide_shortlist[:1], [95])   # the second pass agreed, so it keeps its place

    _run(monkeypatch, "export", "refine")

    batch = json.loads((_refine_dir(data_dir) / "batch.json").read_text(encoding="utf-8"))
    assert batch["anchors"] == [wide_shortlist[0].id]
    assert batch["job_ids"] == [wide_shortlist[1].id]   # the pair is the whole shortlist now


def test_export_refine_exports_only_the_new_jobs_and_anchors_the_rest(shortlist, data_dir, monkeypatch, capsys):
    """The second-best job is new to the shortlist; the best one rides along as a fixed anchor."""
    _refine_scores(shortlist[:1], [85])
    _run(monkeypatch, "export", "refine", "--top", "2")

    batch = json.loads((_refine_dir(data_dir) / "batch.json").read_text(encoding="utf-8"))
    assert batch["job_ids"] == [shortlist[1].id]  # only the new one is scored
    assert batch["anchors"] == [shortlist[0].id]
    assert f"### job_id: {shortlist[1].id}" in batch["prompt"]
    assert f"### job_id: {shortlist[0].id}" not in batch["prompt"]
    assert "ALREADY PLACED" in batch["prompt"]
    assert f"{shortlist[0].id} · " in batch["prompt"]  # the anchor's one-liner
    assert "refine score 85" in batch["prompt"]
    assert "1 new jobs against 1 already placed" in capsys.readouterr().out


def test_export_refine_force_rewrites_an_unchanged_shortlist(shortlist, data_dir, monkeypatch, capsys):
    _refine_scores(shortlist[:2], [85, 75])
    _run(monkeypatch, "export", "refine", "--top", "2", "--force")
    batch = json.loads((_refine_dir(data_dir) / "batch.json").read_text(encoding="utf-8"))
    assert batch["job_ids"] == [shortlist[0].id, shortlist[1].id]
    assert batch["anchors"] == []  # --force re-scores everything, so nothing is fixed
    assert "ALREADY PLACED" not in batch["prompt"]
    assert "2 new jobs against 0 already placed" in capsys.readouterr().out


def test_import_refine_stores_the_answer_like_the_api_path(shortlist, data_dir, monkeypatch, capsys):
    _run(monkeypatch, "export", "refine")
    capsys.readouterr()
    answer = {"items": [
        {"job_id": shortlist[1].id, "position": 1, "score": 88, "summary": "Best against the rest."},
        {"job_id": shortlist[0].id, "position": 2, "score": 60, "summary": "Weaker than it looked."},
        {"job_id": shortlist[1].id, "position": 3, "score": 10, "summary": "said twice"},
        {"job_id": "0000000000000000", "position": 4, "score": 10, "summary": "never sent"},
    ]}
    verdicts = _refine_dir(data_dir) / "verdicts"
    verdicts.mkdir(parents=True, exist_ok=True)
    (verdicts / "batch.json").write_text(json.dumps(answer), encoding="utf-8")

    _run(monkeypatch, "import", "refine")

    store = store_mod.Store()
    try:
        stored = store.verdicts("refine", PROMPT_VERSION)
    finally:
        store.close()
    assert set(stored) == {shortlist[0].id, shortlist[1].id}
    best = stored[shortlist[1].id]
    assert best.score == 88 and best.position == 1 and best.relevant
    assert best.model == "claude-fable-5-1 (subagent)" and best.prompt_version == PROMPT_VERSION
    assert best.summary == "Best against the rest."
    assert stored[shortlist[0].id].score == 60
    assert "imported 2 verdicts of 3 exported" in capsys.readouterr().out



def test_import_refine_ignores_a_re_scored_anchor(shortlist, data_dir, monkeypatch, capsys):
    """Only the exported (new) ids are stored; an anchor the subagent tried to re-score is dropped."""
    _refine_scores(shortlist[:1], [85])
    _run(monkeypatch, "export", "refine", "--top", "2")
    capsys.readouterr()
    answer = {"items": [
        {"job_id": shortlist[1].id, "position": 1, "score": 91, "summary": "beats the placed one"},
        {"job_id": shortlist[0].id, "position": 2, "score": 12, "summary": "re-scored an anchor"},
    ]}
    verdicts = _refine_dir(data_dir) / "verdicts"
    verdicts.mkdir(parents=True, exist_ok=True)
    (verdicts / "batch.json").write_text(json.dumps(answer), encoding="utf-8")

    _run(monkeypatch, "import", "refine")

    store = store_mod.Store()
    try:
        stored = store.verdicts("refine", PROMPT_VERSION)
    finally:
        store.close()
    assert stored[shortlist[1].id].score == 91 and stored[shortlist[1].id].position == 1
    assert stored[shortlist[0].id].score == 85  # the anchor keeps the verdict it already had
    assert "imported 1 verdicts of 1 exported" in capsys.readouterr().out


def test_import_refine_without_an_export_says_so(data_dir, monkeypatch, capsys):
    _run(monkeypatch, "import", "refine")
    assert "run `export refine` first" in capsys.readouterr().out


def test_import_refine_without_an_answer_says_so(shortlist, data_dir, monkeypatch, capsys):
    _run(monkeypatch, "export", "refine")
    _run(monkeypatch, "import", "refine")
    assert "no answer yet" in capsys.readouterr().out


def test_import_refine_rejects_an_answer_that_is_not_a_refinement(shortlist, data_dir, monkeypatch, capsys):
    _run(monkeypatch, "export", "refine")
    verdicts = _refine_dir(data_dir) / "verdicts"
    verdicts.mkdir(parents=True, exist_ok=True)
    (verdicts / "batch.json").write_text('{"items": [{"job_id": "x"}]}', encoding="utf-8")

    _run(monkeypatch, "import", "refine")

    assert "not a Refinement object" in capsys.readouterr().out
    store = store_mod.Store()
    try:
        assert store.verdicts("refine", PROMPT_VERSION) == {}
    finally:
        store.close()


# ------------------------------------------------------------------------ stability
# The ranker scores a chunk at a time, so the same job can come back with a different number
# depending on which other jobs shared its chunk and in which order. `stability export` re-exports
# already-ranked jobs as two independently shuffled runs; `stability compare` turns the two answer
# sets into a drift number.


def _stability(data_dir: Path) -> Path:
    return data_dir / "exports" / "ai" / "stability"


def _write_verdicts(run: Path, name: str, lines: list[dict]) -> None:
    (run / "verdicts").mkdir(parents=True, exist_ok=True)
    (run / "verdicts" / name).write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def _ranking_line(job: Job, score: int, relevant: bool = True) -> dict:
    return {"job_id": job.id, "relevant": relevant, "score": score, "language_ok": True,
            "seniority_ok": True, "location_ok": True, "summary": f"scored {score}",
            "concerns": [], "why_apply": []}


def test_stability_export_writes_two_runs_with_the_same_jobs_in_a_different_order(data_dir, monkeypatch, capsys):
    jobs = [_job(n, f"Junior Developer {n:02d}", LONG_DESCRIPTION) for n in range(12)]
    _seed(jobs, ["keep"] * 12)
    _rank_scores(jobs, list(range(99, 87, -1)))
    before_jobs, before_verdicts = _stored_jobs(), _stored_verdicts("rank")

    _run(monkeypatch, "stability", "export", "--top", "12", "--chunk", "5")

    a, b = _stability(data_dir) / "run-a", _stability(data_dir) / "run-b"
    for run in (a, b):
        assert "career advisor" in (run / "system.txt").read_text(encoding="utf-8")
        assert sorted(p.name for p in run.glob("chunk-*.json")) == [
            "chunk-01.json", "chunk-02.json", "chunk-03.json"]  # 12 jobs, 5 per chunk
    order_a = [e["job_id"] for e in _chunk_entries(a)]
    order_b = [e["job_id"] for e in _chunk_entries(b)]
    assert set(order_a) == set(order_b) == {j.id for j in jobs}  # same job set
    assert order_a != order_b  # different order, so different chunk membership
    assert order_a != [j.id for j in jobs]  # and neither run is the stored order
    for e in _chunk_entries(a):
        assert set(e) == {"job_id", "prompt"}
        assert "TAIL-OF-THE-DESCRIPTION" in e["prompt"]  # the rank stage's 6000-char budget

    assert _stored_jobs().keys() == before_jobs.keys()  # the database is not touched
    assert {k: v.score for k, v in _stored_verdicts("rank").items()} == {
        k: v.score for k, v in before_verdicts.items()}
    assert "12 jobs" in capsys.readouterr().out


def test_stability_export_is_reproducible_for_a_seed_and_moves_with_it(data_dir, monkeypatch, capsys):
    jobs = [_job(n, f"Junior Developer {n:02d}") for n in range(12)]
    _seed(jobs, ["keep"] * 12)
    _rank_scores(jobs, list(range(99, 87, -1)))

    _run(monkeypatch, "stability", "export", "--top", "12", "--chunk", "5")
    first = [e["job_id"] for e in _chunk_entries(_stability(data_dir) / "run-a")]
    _run(monkeypatch, "stability", "export", "--top", "12", "--chunk", "5")
    again = [e["job_id"] for e in _chunk_entries(_stability(data_dir) / "run-a")]
    _run(monkeypatch, "stability", "export", "--top", "12", "--chunk", "5", "--seed", "7")
    other = [e["job_id"] for e in _chunk_entries(_stability(data_dir) / "run-a")]

    assert first == again  # same seed, same shuffle
    assert first != other
    capsys.readouterr()


def test_stability_export_takes_the_best_ranked_jobs_and_wipes_old_chunks(data_dir, monkeypatch, capsys):
    jobs = [_job(n, f"Junior Developer {n:02d}") for n in range(4)]
    unranked = _job(9, "Never Ranked")
    _seed([*jobs, unranked], ["keep"] * 5)
    _rank_scores(jobs, [90, 80, 70, 60])

    _run(monkeypatch, "stability", "export", "--top", "4", "--chunk", "1")
    assert len(list((_stability(data_dir) / "run-a").glob("chunk-*.json"))) == 4

    _run(monkeypatch, "stability", "export", "--top", "2", "--chunk", "5")

    a = _stability(data_dir) / "run-a"
    assert [p.name for p in sorted(a.glob("chunk-*.json"))] == ["chunk-01.json"]  # old chunks gone
    assert {e["job_id"] for e in _chunk_entries(a)} == {jobs[0].id, jobs[1].id}
    assert unranked.id not in {e["job_id"] for e in _chunk_entries(a)}
    assert "2 jobs" in capsys.readouterr().out


def test_stability_export_never_fetches_linkedin_descriptions(data_dir, monkeypatch, capsys):
    li = _bare("linkedin", 201, "https://www.linkedin.com/jobs/view/201")
    _seed([li], ["keep"])
    _rank_scores([li], [90])

    def explode(http, url):  # pragma: no cover - must never be called
        raise AssertionError(f"fetched {url} in the stability stage")

    monkeypatch.setattr(ai_batches, "fetch_description", explode)

    _run(monkeypatch, "stability", "export")

    assert "1 jobs" in capsys.readouterr().out


def test_stability_needs_export_or_compare(data_dir, monkeypatch):
    with pytest.raises(SystemExit):
        _run(monkeypatch, "stability", "rank")


@pytest.fixture
def five_scored(data_dir, monkeypatch):
    """Five ranked jobs plus a hand-made pair of verdict runs with a known Spearman rho.

    A: 90 80 70 60 50, B: 88 75 75 65 40 (a tie in B), so the average-rank correlation is
    9.5 / sqrt(10 * 9.5) = 0.9747 and the |A-B| list is 2 5 5 5 10.
    """
    jobs = [_job(n, f"Junior Developer {n:02d}") for n in range(5)]
    _seed(jobs, ["keep"] * 5)
    _rank_scores(jobs, [85, 84, 83, 82, 81])
    _run(monkeypatch, "stability", "export", "--top", "5")
    root = _stability(data_dir)
    _write_verdicts(root / "run-a", "chunk-01.jsonl",
                    [_ranking_line(j, s) for j, s in zip(jobs, [90, 80, 70, 60, 50])])
    _write_verdicts(root / "run-b", "chunk-01.jsonl",
                    [_ranking_line(j, s, relevant=(j is not jobs[4]))
                     for j, s in zip(jobs, [88, 75, 75, 65, 40])])
    return jobs


def test_stability_compare_reports_the_hand_computed_statistics(five_scored, data_dir, monkeypatch, capsys):
    capsys.readouterr()

    _run(monkeypatch, "stability", "compare")

    out = capsys.readouterr().out
    assert "jobs in both runs: 5" in out
    assert "median |A-B|: 5.00" in out
    assert "90th percentile |A-B|: 8.00" in out
    assert "mean signed (A-B): 1.40" in out
    assert "Spearman rank correlation A vs B: 0.9747" in out
    assert "top-10 overlap (Jaccard): 1.000" in out
    assert "relevant flips: 1" in out
    assert "A vs original" in out and "B vs original" in out
    for job in five_scored:  # the per-job table
        assert job.title[:40] in out
    assert "flip" in out


def test_stability_compare_adds_a_refine_column_once_that_pass_has_run(five_scored, data_dir, monkeypatch, capsys):
    """The refine score saw every job in one request, so it is the reference the two runs drift from."""
    _run(monkeypatch, "stability", "compare")
    assert "| refine |" not in capsys.readouterr().out  # nothing refined yet

    store = store_mod.Store()
    try:
        store.save_verdict(AIVerdict(job_id=five_scored[0].id, stage="refine",
                                     model="claude-fable-5-1 (subagent)", prompt_version=PROMPT_VERSION,
                                     relevant=True, score=77, position=1, language_ok=True,
                                     seniority_ok=True, location_ok=True, summary="best"))
    finally:
        store.close()

    _run(monkeypatch, "stability", "compare")

    out = capsys.readouterr().out
    assert "| refine |" in out
    assert f"| {five_scored[0].id[:12]} | {five_scored[0].title[:40]} | 85 | 77 | 90 |" in out
    assert f"| {five_scored[1].id[:12]} | {five_scored[1].title[:40]} | 84 | - | 80 |" in out


def test_stability_compare_writes_the_same_report_as_markdown(five_scored, data_dir, monkeypatch, capsys):
    _run(monkeypatch, "stability", "compare")
    printed = capsys.readouterr().out

    text = (_stability(data_dir) / "summary.md").read_text(encoding="utf-8")
    assert text.startswith("# Ranking stability")
    assert "median |A-B|: 5.00" in text
    assert text.strip() in printed  # the printed report and the file say the same thing


def test_stability_compare_lists_jobs_missing_from_a_run(data_dir, monkeypatch, capsys):
    jobs = [_job(n, f"Junior Developer {n:02d}") for n in range(3)]
    _seed(jobs, ["keep"] * 3)
    _rank_scores(jobs, [90, 80, 70])
    _run(monkeypatch, "stability", "export", "--top", "3")
    root = _stability(data_dir)
    _write_verdicts(root / "run-a", "chunk-01.jsonl", [_ranking_line(j, 70) for j in jobs])
    _write_verdicts(root / "run-b", "chunk-01.jsonl", [_ranking_line(j, 70) for j in jobs[:2]])
    capsys.readouterr()

    _run(monkeypatch, "stability", "compare")

    out = capsys.readouterr().out
    assert "jobs in both runs: 2" in out
    assert "missing from run-b: 1" in out
    assert jobs[2].id[:12] in out


def test_stability_compare_tolerates_array_wrappers_and_broken_lines(data_dir, monkeypatch, capsys):
    jobs = [_job(n, f"Junior Developer {n:02d}") for n in range(2)]
    _seed(jobs, ["keep"] * 2)
    _rank_scores(jobs, [90, 80])
    _run(monkeypatch, "stability", "export", "--top", "2")
    root = _stability(data_dir)
    good = "\n".join(json.dumps(_ranking_line(j, 70)) + "," for j in jobs)
    (root / "run-a" / "verdicts").mkdir(parents=True, exist_ok=True)
    (root / "run-a" / "verdicts" / "chunk-01.jsonl").write_text(
        "[\n" + good + '\n{"job_id": "x", "score":\n]\n', encoding="utf-8")
    _write_verdicts(root / "run-b", "chunk-01.jsonl", [_ranking_line(j, 72) for j in jobs])
    capsys.readouterr()

    _run(monkeypatch, "stability", "compare")

    out = capsys.readouterr().out
    assert "jobs in both runs: 2" in out  # the array brackets and the truncated line are skipped
    assert "bad line" in out


def test_stability_compare_without_any_verdicts(data_dir, monkeypatch, capsys):
    _run(monkeypatch, "stability", "compare")

    out = capsys.readouterr().out
    assert "no jobs" in out.lower()
    assert (_stability(data_dir) / "summary.md").exists()


def test_stability_compare_top_ten_overlap_is_a_jaccard(data_dir, monkeypatch, capsys):
    jobs = [_job(n, f"Junior Developer {n:02d}") for n in range(12)]
    _seed(jobs, ["keep"] * 12)
    _rank_scores(jobs, list(range(99, 87, -1)))
    _run(monkeypatch, "stability", "export", "--top", "12")
    root = _stability(data_dir)
    a_scores = [100, 99, 98, 97, 96, 95, 94, 93, 92, 91, 90, 89]
    b_scores = [100, 99, 98, 97, 96, 95, 94, 93, 92, 10, 91, 89]  # job 9 drops out, job 10 moves in
    _write_verdicts(root / "run-a", "chunk-01.jsonl",
                    [_ranking_line(j, s) for j, s in zip(jobs, a_scores)])
    _write_verdicts(root / "run-b", "chunk-01.jsonl",
                    [_ranking_line(j, s) for j, s in zip(jobs, b_scores)])
    capsys.readouterr()

    _run(monkeypatch, "stability", "compare")

    out = capsys.readouterr().out
    assert "top-10 overlap (Jaccard): 0.818" in out  # 9 shared of 11 in the union
    assert "relevant flips: 0" in out


def test_stability_follows_the_profile(profile_dirs, monkeypatch, capsys):
    job = _job(1, "Junior Go Developer")
    _seed_in_profile("x", [job], ["keep"])
    config.use_profile("x")
    try:
        _rank_scores([job], [90])
    finally:
        config.use_profile(None)

    _run(monkeypatch, "stability", "export", "--profile", "x")

    out = profile_dirs / "data" / "profiles" / "x" / "exports" / "ai" / "stability"
    assert [e["job_id"] for e in _chunk_entries(out / "run-a")] == [job.id]
    assert not (profile_dirs / "data" / "exports").exists()
    assert "1 jobs" in capsys.readouterr().out
