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
