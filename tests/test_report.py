"""Tests for :mod:`jobscraper.report`: the markdown report and the JSONL export."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from jobscraper.models import AIVerdict, FilterResult, Job
from jobscraper.report import export_jsonl, write_report


def make_job(source_id: str, title: str, **kw) -> Job:
    base = {
        "source": "teamtailor",
        "source_id": source_id,
        "url": f"https://jobs.example.test/{source_id}",
        "title": title,
        "company": "Example Oy",
        "description": "We build backend services in Python and Go.",
        "location_raw": "Helsinki, Finland",
        "country": "FI",
        "remote": "hybrid",
        "posted_at": datetime(2026, 9, 1, tzinfo=UTC),
    }
    base.update(kw)
    return Job(**base)


def make_verdict(job_id: str, stage: str, score: int, **kw) -> AIVerdict:
    base = {
        "job_id": job_id,
        "stage": stage,
        "model": "claude-opus-5" if stage == "rank" else "claude-sonnet-5",
        "prompt_version": "v1",
        "relevant": True,
        "score": score,
        "language_ok": True,
        "seniority_ok": True,
        "location_ok": True,
        "summary": f"Summary for {job_id}.",
    }
    base.update(kw)
    return AIVerdict(**base)


@pytest.fixture
def state():
    """A ranked job, a prefilter-only job and a job the rule filter parked in 'review'."""
    ranked_job = make_job("rank-1", "Junior Backend Developer")
    pre_job = make_job("pre-1", "Junior Platform Engineer", location_raw="Tbilisi, Georgia",
                       country="GE")
    review_job = make_job("rev-1", "Software Developer", country="EE",
                          location_raw="Tallinn, Estonia")
    jobs = [ranked_job, pre_job, review_job]
    filters = {
        ranked_job.id: FilterResult(job_id=ranked_job.id, status="keep", location_tier=1),
        pre_job.id: FilterResult(job_id=pre_job.id, status="keep", location_tier=3,
                                 signals={"work_rights_note": "Georgia: visa-free for one year."}),
        review_job.id: FilterResult(job_id=review_job.id, status="review",
                                    reasons=["asks for 3 years of experience"], location_tier=1),
    }
    prefilter = {
        ranked_job.id: make_verdict(ranked_job.id, "prefilter", 80),
        pre_job.id: make_verdict(pre_job.id, "prefilter", 55),
    }
    ranked = {
        ranked_job.id: make_verdict(ranked_job.id, "rank", 91, why_apply=["C++ and Go work"],
                                    concerns=["office is hybrid"]),
    }
    return jobs, filters, prefilter, ranked


def test_write_report_renders_all_three_sections(tmp_path, state):
    jobs, filters, prefilter, ranked = state
    ranked_job, pre_job, review_job = jobs

    path = write_report(jobs, filters, prefilter, ranked, tmp_path / "report.md",
                        cost={"total": 1.5, "claude-opus-5": 1.5})
    assert path == tmp_path / "report.md"
    text = path.read_text(encoding="utf-8")

    # header line with the counters
    assert "Jobs in DB: 3" in text
    assert "rule-kept: 2" in text
    assert "review: 1" in text
    assert "Estimated API cost" in text

    # ranked section: title rendered as a markdown link, plus the Opus commentary
    assert "## Ranked (Opus)" in text
    assert f"### 91 · [{ranked_job.title}]({ranked_job.url})" in text
    assert "**Why apply:** C++ and Go work" in text
    assert "**Concerns:** office is hybrid" in text

    # prefilter table: only the survivor that was not ranked in detail
    assert "## Prefilter survivors not ranked in detail (Sonnet)" in text
    assert f"| 55 | [{pre_job.title}]({pre_job.url}) | Example Oy | " \
           "Tbilisi, Georgia / hybrid | teamtailor |" in text
    assert f"[{ranked_job.title}]({ranked_job.url}) | " not in text  # ranked job is not repeated
    assert "**Work rights:**" not in text  # the note belongs to a job that was not ranked

    # review table: the rule-filter leftover with its reason
    assert "## Rule filter: needs review (not yet AI-scored)" in text
    assert f"| [{review_job.title}]({review_job.url}) | Example Oy | teamtailor | " \
           "asks for 3 years of experience |" in text


def test_write_report_creates_missing_directories(tmp_path, state):
    jobs, filters, prefilter, ranked = state
    path = write_report(jobs, filters, prefilter, ranked, tmp_path / "deep" / "nested" / "r.md")
    assert path.exists()
    assert "Ranked (Opus)" in path.read_text(encoding="utf-8")


def test_write_report_without_any_ai_verdicts(tmp_path, state):
    jobs, filters, _prefilter, _ranked = state
    path = write_report(jobs, filters, {}, {}, tmp_path / "empty.md")
    text = path.read_text(encoding="utf-8")
    assert "## Ranked (Opus)" not in text
    assert "## Prefilter survivors" not in text
    # with no prefilter verdicts the review row is still listed
    assert "## Rule filter: needs review (not yet AI-scored)" in text


def test_export_jsonl_has_one_line_per_job(tmp_path, state):
    jobs, filters, prefilter, ranked = state
    ranked_job, pre_job, review_job = jobs

    path = export_jsonl(jobs, filters, {**prefilter, **ranked}, tmp_path / "out" / "jobs.jsonl")
    assert path == tmp_path / "out" / "jobs.jsonl"

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    records = [json.loads(line) for line in lines]
    assert [r["id"] for r in records] == [j.id for j in jobs]
    for rec in records:
        assert "filter" in rec and "verdict" in rec
        assert "raw" not in rec
        assert rec["filter"]["job_id"] == rec["id"]

    by_id = {r["id"]: r for r in records}
    assert by_id[ranked_job.id]["verdict"]["stage"] == "rank"
    assert by_id[ranked_job.id]["verdict"]["score"] == 91
    assert by_id[pre_job.id]["verdict"]["stage"] == "prefilter"
    assert by_id[review_job.id]["verdict"] is None
    assert by_id[review_job.id]["filter"]["status"] == "review"


def test_write_report_hides_jobs_the_rule_filter_now_drops(tmp_path, state):
    """Rules tighten between runs (max age, seniority, language); stale verdicts must not resurrect a drop."""
    jobs, filters, prefilter, ranked = state
    ranked_job, pre_job, _ = jobs
    filters[ranked_job.id] = FilterResult(job_id=ranked_job.id, status="drop", reasons=["requires pl: 'Polish C1'"])
    filters[pre_job.id] = FilterResult(job_id=pre_job.id, status="drop", reasons=["posting is 40 days old (max 20)"])
    text = write_report(jobs, filters, prefilter, ranked, tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Junior Backend Developer" not in text
    assert "Junior Platform Engineer" not in text
