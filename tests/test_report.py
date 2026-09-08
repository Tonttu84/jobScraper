"""Tests for :mod:`jobscraper.report`: the markdown report and the JSONL export."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from jobscraper.models import AIVerdict, FilterResult, Job
from jobscraper.report import build_snapshot, export_jsonl, write_report


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


# ------------------------------------------------------------ build_snapshot


def test_build_snapshot_sections_positions_and_scores(state):
    jobs, filters, prefilter, ranked = state
    ranked_job, pre_job, review_job = jobs

    snap = build_snapshot(jobs, filters, prefilter, ranked, days=7, prompt_version="v1")

    assert snap.id is None
    assert snap.days == 7
    assert snap.prompt_version == "v1"

    by_section = {}
    for item in snap.items:
        by_section.setdefault(item.section, []).append(item)

    assert [(i.job_id, i.position, i.score) for i in by_section["ranked"]] == [
        (ranked_job.id, 1, 91)
    ]
    assert [(i.job_id, i.position, i.score) for i in by_section["prefilter"]] == [
        (pre_job.id, 1, 55)
    ]
    assert [(i.job_id, i.position, i.score) for i in by_section["review"]] == [
        (review_job.id, 1, None)
    ]

    # the ranked job is never repeated in the prefilter section
    assert ranked_job.id not in {i.job_id for i in by_section["prefilter"]}


def test_build_snapshot_counts_match_the_markdown_header(tmp_path, state):
    jobs, filters, prefilter, ranked = state
    snap = build_snapshot(jobs, filters, prefilter, ranked, days=3, prompt_version="v1")
    assert snap.counts == {"jobs": 3, "keep": 2, "review": 1, "drop": 0,
                           "prefiltered": 2, "ranked": 1}

    text = write_report(jobs, filters, prefilter, ranked, tmp_path / "r.md").read_text("utf-8")
    assert f"Jobs in DB: {snap.counts['jobs']}" in text
    assert f"rule-kept: {snap.counts['keep']}" in text
    assert f"review: {snap.counts['review']}" in text
    assert f"dropped: {snap.counts['drop']}" in text
    assert f"prefiltered: {snap.counts['prefiltered']}" in text
    assert f"ranked: {snap.counts['ranked']}" in text


def test_build_snapshot_cost_and_path(tmp_path, state):
    jobs, filters, prefilter, ranked = state
    md = tmp_path / "report.md"

    snap = build_snapshot(jobs, filters, prefilter, ranked, days=7,
                          cost={"total": 2.5, "claude-opus-5": 2.5}, path=md,
                          prompt_version="v2")
    assert snap.cost == {"total": 2.5, "claude-opus-5": 2.5}
    assert snap.path == str(md)

    bare = build_snapshot(jobs, filters, prefilter, ranked, days=7, prompt_version="v2")
    assert bare.cost == {}
    assert bare.path is None


def test_build_snapshot_orders_sections_by_score_desc(state):
    jobs, filters, prefilter, ranked = state
    ranked_job, pre_job, _review_job = jobs
    extra = make_job("rank-2", "Junior Data Engineer")
    extra_low = make_job("pre-2", "Junior QA Engineer")
    jobs = [*jobs, extra, extra_low]
    filters = {**filters,
               extra.id: FilterResult(job_id=extra.id, status="keep", location_tier=1),
               extra_low.id: FilterResult(job_id=extra_low.id, status="keep", location_tier=1)}
    ranked = {**ranked, extra.id: make_verdict(extra.id, "rank", 95)}
    prefilter = {**prefilter,
                 extra.id: make_verdict(extra.id, "prefilter", 60),
                 extra_low.id: make_verdict(extra_low.id, "prefilter", 70)}

    snap = build_snapshot(jobs, filters, prefilter, ranked, days=7, prompt_version="v1")
    ranked_items = [i for i in snap.items if i.section == "ranked"]
    pre_items = [i for i in snap.items if i.section == "prefilter"]

    assert [(i.job_id, i.position, i.score) for i in ranked_items] == [
        (extra.id, 1, 95), (ranked_job.id, 2, 91)]
    assert [(i.job_id, i.position, i.score) for i in pre_items] == [
        (extra_low.id, 1, 70), (pre_job.id, 2, 55)]


def test_build_snapshot_skips_unknown_jobs_and_irrelevant_prefilter(state):
    jobs, filters, prefilter, ranked = state
    ranked = {**ranked, "ghost": make_verdict("ghost", "rank", 99)}
    prefilter = {**prefilter,
                 "ghost2": make_verdict("ghost2", "prefilter", 88),
                 jobs[2].id: make_verdict(jobs[2].id, "prefilter", 20, relevant=False)}

    snap = build_snapshot(jobs, filters, prefilter, ranked, days=7, prompt_version="v1")
    ids = {i.job_id for i in snap.items}
    assert "ghost" not in ids
    assert "ghost2" not in ids
    # the irrelevant prefilter verdict is not listed, and its job left the review section
    assert jobs[2].id not in ids
    assert snap.counts["prefiltered"] == 4


def test_build_snapshot_caps_the_review_section_at_300(state):
    jobs, filters, _prefilter, _ranked = state
    many = [make_job(f"rev-{n}", f"Developer {n}") for n in range(400)]
    filters = {**filters,
               **{j.id: FilterResult(job_id=j.id, status="review", reasons=["x"]) for j in many}}
    snap = build_snapshot([*jobs, *many], filters, {}, {}, days=7, prompt_version="v1")

    review = [i for i in snap.items if i.section == "review"]
    assert len(review) == 300
    assert [i.position for i in review[:3]] == [1, 2, 3]
    assert all(i.score is None for i in review)
