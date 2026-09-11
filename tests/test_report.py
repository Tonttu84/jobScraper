"""Tests for :mod:`jobscraper.report`: the markdown report and the JSONL export."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from jobscraper.models import AIVerdict, FilterResult, Job, ReportItem, ReportSnapshot
from jobscraper.report import (
    build_snapshot,
    calibrated_refine,
    diff_reports,
    effective_score,
    effective_top,
    entered_top,
    export_jsonl,
    miss_run,
    previous_report,
    rank_queue,
    refine_offset,
    refine_shortlist,
    render_diff,
    render_new_section,
    write_report,
)


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


def test_write_report_shows_the_work_rights_note_of_a_ranked_job(tmp_path, state):
    """The note lives on the filter result and only reaches the report through the Opus block."""
    jobs, filters, prefilter, ranked = state
    ranked_job = jobs[0]
    filters[ranked_job.id] = FilterResult(
        job_id=ranked_job.id, status="keep", location_tier=3,
        signals={"work_rights_note": "Georgia: visa-free for one year."},
    )
    text = write_report(jobs, filters, prefilter, ranked, tmp_path / "r.md").read_text(
        encoding="utf-8"
    )
    assert "**Work rights:** Georgia: visa-free for one year." in text


def _with_deadline(state, days: int | None, tmp_path):
    """The report text with the ranked job carrying (or missing) a ``closes_in_days`` signal."""
    jobs, filters, prefilter, ranked = state
    signals = {} if days is None else {"deadline": "2026-09-13", "closes_in_days": days}
    filters[jobs[0].id] = FilterResult(job_id=jobs[0].id, status="keep", location_tier=1,
                                       signals=signals)
    return write_report(jobs, filters, prefilter, ranked, tmp_path / "r.md").read_text(
        encoding="utf-8"
    )


def test_write_report_says_when_a_ranked_job_closes(tmp_path, state):
    text = _with_deadline(state, 3, tmp_path)
    assert "· closes in 3 days*" in text
    assert "- **Closes in 3 days**" in text


def test_write_report_says_closes_today_on_the_last_day(tmp_path, state):
    text = _with_deadline(state, 0, tmp_path)
    assert "· closes today*" in text
    assert "- **Closes today**" in text


def test_write_report_leaves_a_distant_deadline_in_the_meta_line_only(tmp_path, state):
    """Three weeks out is worth knowing, not worth a bullet above the Opus summary."""
    text = _with_deadline(state, 21, tmp_path)
    assert "· closes in 21 days*" in text
    assert "Closes in 21 days**" not in text


def test_write_report_says_nothing_about_a_job_with_no_known_deadline(tmp_path, state):
    assert "closes" not in _with_deadline(state, None, tmp_path)


def _with_signals(state, signals: dict, tmp_path):
    """The report text with the ranked job's filter result carrying ``signals``."""
    jobs, filters, prefilter, ranked = state
    filters[jobs[0].id] = FilterResult(job_id=jobs[0].id, status="keep", location_tier=1,
                                       signals=signals)
    return write_report(jobs, filters, prefilter, ranked, tmp_path / "r.md").read_text(
        encoding="utf-8"
    )


def test_write_report_flags_an_evergreen_advert_in_the_heading_and_a_bullet(tmp_path, state):
    """A pipeline advert is not a live vacancy: the reader should see that above the summary."""
    text = _with_signals(state, {"evergreen": "talent pool"}, tmp_path)
    assert "· evergreen advert*" in text
    assert '- **Evergreen advert:** not a live vacancy ("talent pool")' in text


def test_an_evergreen_advert_with_a_deadline_says_both(tmp_path, state):
    text = _with_signals(
        state, {"deadline": "2026-09-13", "closes_in_days": 3, "evergreen": "talent pool"}, tmp_path
    )
    assert "· closes in 3 days · evergreen advert*" in text
    assert "- **Closes in 3 days**" in text
    assert '- **Evergreen advert:** not a live vacancy ("talent pool")' in text


def test_write_report_says_nothing_about_a_live_vacancy(tmp_path, state):
    assert "evergreen" not in _with_signals(state, {"years_required": 3}, tmp_path)


def test_write_report_ignores_verdicts_whose_job_is_gone(tmp_path, state):
    """A verdict can outlive its job row (a re-scrape that dropped the posting)."""
    jobs, filters, prefilter, ranked = state
    ghost = "teamtailor:vanished"
    ranked[ghost] = make_verdict(ghost, "rank", 99)
    prefilter[ghost] = make_verdict(ghost, "prefilter", 70)
    filters[ghost] = FilterResult(job_id=ghost, status="keep")
    # ... and one that only the rule filter ever saw
    stale_review = "teamtailor:also-gone"
    filters[stale_review] = FilterResult(
        job_id=stale_review, status="review", reasons=["location unknown"]
    )

    text = write_report(jobs, filters, prefilter, ranked, tmp_path / "r.md").read_text(
        encoding="utf-8"
    )
    assert "vanished" not in text and "also-gone" not in text
    assert "### 91 · " in text  # the surviving ranked job is still there

    snapshot = build_snapshot(jobs, filters, prefilter, ranked, days=30, prompt_version="v1")
    assert all(i.job_id not in (ghost, stale_review) for i in snapshot.items)


def test_write_report_skips_an_irrelevant_prefilter_survivor(tmp_path, state):
    jobs, filters, prefilter, ranked = state
    pre_job = jobs[1]
    prefilter[pre_job.id] = make_verdict(pre_job.id, "prefilter", 20, relevant=False)

    text = write_report(jobs, filters, prefilter, ranked, tmp_path / "r.md").read_text(
        encoding="utf-8"
    )
    assert "## Prefilter survivors not ranked in detail (Sonnet)" in text
    assert pre_job.url not in text


def test_write_report_omits_an_empty_prefilter_table(tmp_path, state):
    """Every prefilter survivor was ranked in detail: there is no leftover table to print."""
    jobs, filters, prefilter, ranked = state
    ranked_job, pre_job, _review = jobs
    prefilter.pop(pre_job.id)
    text = write_report(jobs, filters, {ranked_job.id: prefilter[ranked_job.id]}, ranked,
                        tmp_path / "r.md").read_text(encoding="utf-8")
    assert "## Prefilter survivors not ranked in detail (Sonnet)" not in text
    assert "## Ranked (Opus)" in text


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

def test_write_report_hides_jobs_the_rule_filter_now_drops(tmp_path, state):
    """Rules tighten between runs (max age, seniority, language); stale verdicts must not resurrect a drop."""
    jobs, filters, prefilter, ranked = state
    ranked_job, pre_job, _ = jobs
    filters[ranked_job.id] = FilterResult(job_id=ranked_job.id, status="drop", reasons=["requires pl: 'Polish C1'"])
    filters[pre_job.id] = FilterResult(job_id=pre_job.id, status="drop", reasons=["posting is 40 days old (max 20)"])
    text = write_report(jobs, filters, prefilter, ranked, tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Junior Backend Developer" not in text
    assert "Junior Platform Engineer" not in text


def test_build_snapshot_hides_jobs_the_rule_filter_now_drops(state):
    """Mirror of the markdown rule: a stale verdict must not resurrect a job the rules now drop."""
    jobs, filters, prefilter, ranked = state
    ranked_job, pre_job, review_job = jobs
    filters[ranked_job.id] = FilterResult(job_id=ranked_job.id, status="drop", reasons=["requires pl"])
    filters[pre_job.id] = FilterResult(job_id=pre_job.id, status="drop", reasons=["too old"])
    snap = build_snapshot(jobs, filters, prefilter, ranked, days=7, prompt_version="v1")
    assert [(i.job_id, i.section) for i in snap.items] == [(review_job.id, "review")]
    assert snap.counts["prefiltered"] == 0 and snap.counts["ranked"] == 0


# ------------------------------------------------------------------- refine pass


def make_refine(job_id: str, score: int, position: int, summary: str = "Second of the shortlist.") -> AIVerdict:
    return AIVerdict(job_id=job_id, stage="refine", model="claude-fable-5-1", prompt_version="v1",
                     relevant=True, score=score, language_ok=True, seniority_ok=True,
                     location_ok=True, summary=summary, position=position)


def test_write_report_shows_both_scores_when_the_refine_pass_spoke(tmp_path, state):
    jobs, filters, prefilter, ranked = state
    ranked_job = jobs[0]
    refine = {ranked_job.id: make_refine(ranked_job.id, 85, 1, "Best of the shortlist by a nose.")}

    text = write_report(jobs, filters, prefilter, ranked, tmp_path / "r.md",
                        refine=refine).read_text(encoding="utf-8")
    # One shared job is too few to measure a scale difference, so nothing is moved.
    assert f"### 88 (rank 91 · refine 85, calibrated +0.0) · [{ranked_job.title}]({ranked_job.url})" in text
    assert "**Against the rest of the shortlist:** Best of the shortlist by a nose." in text


def test_write_report_keeps_the_bare_rank_score_without_a_refine_verdict(tmp_path, state):
    jobs, filters, prefilter, ranked = state
    text = write_report(jobs, filters, prefilter, ranked, tmp_path / "r.md").read_text(encoding="utf-8")
    assert "### 91 · [" in text
    assert "refine" not in text


def test_build_snapshot_orders_the_ranked_section_by_the_effective_score(state):
    """The refine pass can overrule the rank order: 70+96 beats 91+60 on the mean."""
    jobs, filters, prefilter, ranked = state
    ranked_job = jobs[0]
    other = make_job("rank-2", "Junior Data Engineer")
    jobs = [*jobs, other]
    filters = {**filters, other.id: FilterResult(job_id=other.id, status="keep", location_tier=1)}
    ranked = {**ranked, other.id: make_verdict(other.id, "rank", 70)}
    refine = {ranked_job.id: make_refine(ranked_job.id, 60, 2),
              other.id: make_refine(other.id, 96, 1)}

    snap = build_snapshot(jobs, filters, prefilter, ranked, days=7, prompt_version="v1", refine=refine)
    assert [(i.job_id, i.position, i.score) for i in snap.items if i.section == "ranked"] == [
        (other.id, 1, 83), (ranked_job.id, 2, 76)]


def test_build_snapshot_breaks_a_tie_on_the_refine_position(state):
    """Same effective score: the pass that saw the whole list decides which one is first."""
    jobs, filters, prefilter, ranked = state
    ranked_job = jobs[0]
    other = make_job("rank-2", "Junior Data Engineer")
    jobs = [*jobs, other]
    filters = {**filters, other.id: FilterResult(job_id=other.id, status="keep", location_tier=1)}
    ranked = {**ranked, other.id: make_verdict(other.id, "rank", 91)}
    refine = {ranked_job.id: make_refine(ranked_job.id, 85, 2),
              other.id: make_refine(other.id, 85, 1)}

    snap = build_snapshot(jobs, filters, prefilter, ranked, days=7, prompt_version="v1", refine=refine)
    assert [i.job_id for i in snap.items if i.section == "ranked"] == [other.id, ranked_job.id]


def test_build_snapshot_sorts_an_unrefined_job_after_a_refined_one(state):
    """Nothing is dropped when the refine answer forgot a job: it keeps its own rank score."""
    jobs, filters, prefilter, ranked = state
    ranked_job = jobs[0]
    other = make_job("rank-2", "Junior Data Engineer")
    jobs = [*jobs, other]
    filters = {**filters, other.id: FilterResult(job_id=other.id, status="keep", location_tier=1)}
    ranked = {**ranked, other.id: make_verdict(other.id, "rank", 88)}
    refine = {ranked_job.id: make_refine(ranked_job.id, 85, 1)}

    snap = build_snapshot(jobs, filters, prefilter, ranked, days=7, prompt_version="v1", refine=refine)
    assert [(i.job_id, i.score) for i in snap.items if i.section == "ranked"] == [
        (ranked_job.id, 88), (other.id, 88)]


# --------------------------------------------- calibrating refine onto the rank scale


def _pairs(*rows) -> tuple[dict[str, AIVerdict], dict[str, AIVerdict]]:
    """``(rank, refine)`` verdict dicts from ``(job_id, rank_score, refine_score)`` rows.

    A ``None`` on either side means that stage never scored the job.
    """
    rank, refine = {}, {}
    for pos, (job_id, rank_score, refine_score) in enumerate(rows, 1):
        if rank_score is not None:
            rank[job_id] = make_verdict(job_id, "rank", rank_score)
        if refine_score is not None:
            refine[job_id] = make_refine(job_id, refine_score, pos)
    return rank, refine


def test_refine_offset_averages_the_gap_over_the_jobs_both_stages_scored():
    """Only the overlap counts: a job one stage never saw says nothing about the scale."""
    rank, refine = _pairs(("a", 91, 70), ("b", 80, 60), ("c", 70, 48),
                          ("d", 95, None), ("e", None, 20))
    assert refine_offset(rank, refine) == 21.0


def test_refine_offset_is_zero_when_fewer_than_three_jobs_overlap():
    """Two shared jobs are noise, not a scale: leave the scores where the raters put them."""
    rank, refine = _pairs(("a", 91, 60), ("b", 80, 50))
    assert refine_offset(rank, refine) == 0.0
    assert refine_offset({}, {}) == 0.0


def test_refine_offset_rounds_to_one_decimal():
    rank, refine = _pairs(("a", 90, 70), ("b", 90, 69), ("c", 90, 69))
    assert refine_offset(rank, refine) == 20.7


def test_calibrated_refine_moves_a_score_onto_the_rank_scale():
    assert calibrated_refine(63, 22.0) == 85
    assert calibrated_refine(63, 0.0) == 63
    assert calibrated_refine(63, -2.4) == 61  # a refine pass that scores higher than rank
    assert calibrated_refine(95, 18.0) == 100  # the shift can never push a score past the scale
    assert calibrated_refine(3, -10.0) == 0


def test_calibrated_refine_stays_inside_the_scale():
    assert calibrated_refine(95, 22.0) == 100
    assert calibrated_refine(5, -22.0) == 0


def test_effective_score_averages_the_rank_score_with_the_calibrated_one():
    rank, refine = _pairs(("a", 91, 63))
    assert effective_score(rank["a"], refine["a"], 22.0) == 88
    assert effective_score(rank["a"], refine["a"]) == 77  # uncalibrated, as before


def test_effective_score_ignores_the_offset_when_only_one_pass_spoke():
    """Calibration is a comparison between the two; with one rater there is nothing to compare."""
    rank, refine = _pairs(("a", 91, 63))
    assert effective_score(rank["a"], None, 22.0) == 91
    assert effective_score(None, refine["a"], 22.0) == 63
    assert effective_score(None, None, 22.0) is None


@pytest.fixture
def calibrated_state():
    """Four ranked jobs; three of them refined, at a measurable −22.0 offset.

    ``lift`` is refined 26 points below its rank score and ``bare`` was never refined, so the
    two change places once the refine scores are put back on the rank stage's scale.
    """
    jobs = [make_job("lift", "Junior Backend Developer"), make_job("bare", "Junior Data Engineer"),
            make_job("top", "Junior Platform Engineer"), make_job("low", "Junior QA Engineer")]
    filters = {j.id: FilterResult(job_id=j.id, status="keep", location_tier=1) for j in jobs}
    lift, bare, top, low = (j.id for j in jobs)
    ranked, refine = _pairs((lift, 80, 54), (bare, 74, None), (top, 90, 70), (low, 60, 40))
    return jobs, filters, ranked, refine


def test_build_snapshot_orders_the_ranked_section_by_the_calibrated_score(calibrated_state):
    """Raw averaging would put the unrefined job second; on one scale it is third."""
    jobs, filters, ranked, refine = calibrated_state
    lift, bare, top, low = (j.id for j in jobs)

    snap = build_snapshot(jobs, filters, {}, ranked, days=7, prompt_version="v1", refine=refine)
    assert [(i.job_id, i.score) for i in snap.items if i.section == "ranked"] == [
        (top, 91), (lift, 78), (bare, 74), (low, 61)]
    # the same input averaged raw would have read: top 80, bare 74, lift 67, low 50
    assert [i.job_id for i in snap.items if i.section == "ranked"] != [top, bare, lift, low]


def test_build_snapshot_stores_the_offset_it_calibrated_with(calibrated_state):
    jobs, filters, ranked, refine = calibrated_state
    snap = build_snapshot(jobs, filters, {}, ranked, days=7, prompt_version="v1", refine=refine)
    assert snap.refine_offset == 22.0


def test_build_snapshot_has_no_offset_without_a_refine_pass(calibrated_state):
    """Nothing to calibrate against: the field stays empty rather than claiming a zero gap."""
    jobs, filters, ranked, _refine = calibrated_state
    snap = build_snapshot(jobs, filters, {}, ranked, days=7, prompt_version="v1")
    assert snap.refine_offset is None


def test_write_report_heading_shows_the_calibrated_refine_score_and_the_offset(tmp_path,
                                                                              calibrated_state):
    jobs, filters, ranked, refine = calibrated_state
    top = jobs[2]

    text = write_report(jobs, filters, {}, ranked, tmp_path / "r.md",
                        refine=refine).read_text(encoding="utf-8")
    assert f"### 91 (rank 90 · refine 92, calibrated +22.0) · [{top.title}]({top.url})" in text
    assert "### 74 · [Junior Data Engineer]" in text  # the unrefined job says nothing about it


# ------------------------------------------- the shortlist the refine pass works on


@pytest.fixture
def shortlist_state():
    """Four rule-kept ranked jobs, one the rules drop, and one the ranker never saw.

    Three of the four were refined, which puts the offset at +27.3. ``sinks`` was marked down
    far enough by the second pass that ``rises`` — never refined, so still carrying its single
    rank score — passes it once both are read on the same scale.
    """
    jobs = [make_job("best", "Junior Backend Developer"), make_job("second", "Junior SRE"),
            make_job("sinks", "Junior Platform Engineer"), make_job("rises", "Junior QA Engineer"),
            make_job("dropped", "Junior Polish-only Developer"),
            make_job("unranked", "Junior Data Engineer")]
    best, second, sinks, rises, dropped, unranked = (j.id for j in jobs)
    filters = {j.id: FilterResult(job_id=j.id, status="keep", location_tier=1) for j in jobs}
    filters[dropped] = FilterResult(job_id=dropped, status="drop", reasons=["Polish required"])
    rank, refine = _pairs((best, 90, 66), (second, 86, 62), (sinks, 84, 50), (rises, 83, None),
                          (dropped, 95, None), (unranked, None, None))
    return jobs, filters, rank, refine


def _ids(jobs) -> list[str]:
    return [j.id for j in jobs]


def test_refine_shortlist_takes_the_effective_top_not_the_rank_top(shortlist_state):
    """The window is the one the report shows, so a refined job that fell can leave it again."""
    jobs, filters, rank, refine = shortlist_state
    best, second, sinks, rises = (j.id for j in jobs[:4])

    picked, _offset = refine_shortlist(jobs, filters, rank, refine, top=3, first_pass=5)

    assert _ids(picked) == [best, second, rises]
    # by rank score alone it would have been the three best-ranked jobs, sinks among them
    assert sorted(_ids(picked)) != sorted([best, second, sinks])


def test_refine_shortlist_returns_the_offset_it_ordered_with(shortlist_state):
    jobs, filters, rank, refine = shortlist_state
    _picked, offset = refine_shortlist(jobs, filters, rank, refine, top=3, first_pass=5)
    assert offset == 27.3
    assert refine_shortlist(jobs, filters, rank, {}, top=3, first_pass=5)[1] == 0.0


def test_refine_shortlist_leaves_out_drops_and_jobs_the_ranker_never_saw(shortlist_state):
    """A wide window still only holds jobs the pipeline would report on."""
    jobs, filters, rank, refine = shortlist_state
    dropped, unranked = jobs[4].id, jobs[5].id

    picked, _offset = refine_shortlist(jobs, filters, rank, refine, top=99, first_pass=99)

    assert len(picked) == 4
    assert dropped not in _ids(picked) and unranked not in _ids(picked)


def test_refine_shortlist_is_wider_until_something_has_been_refined(shortlist_state):
    """The first request reaches deeper: calibration reshuffles the window it produced."""
    jobs, filters, rank, _refine = shortlist_state

    assert len(refine_shortlist(jobs, filters, rank, {}, top=2, first_pass=4)[0]) == 4

    one = {jobs[0].id: make_refine(jobs[0].id, 66, 1)}
    assert len(refine_shortlist(jobs, filters, rank, one, top=2, first_pass=4)[0]) == 2


def test_refine_shortlist_keeps_a_tie_that_straddles_the_cut_together():
    """Whole-number scores tie for real: refining half a run of 82s leaves the rest stuck inside."""
    jobs = [make_job("clear", "Junior Backend Developer"), make_job("tie-a", "Junior SRE"),
            make_job("tie-b", "Junior QA Engineer"), make_job("tie-c", "Junior Data Engineer"),
            make_job("below", "Junior Platform Engineer")]
    filters = {j.id: FilterResult(job_id=j.id, status="keep", location_tier=1) for j in jobs}
    clear, tie_a, tie_b, tie_c, below = (j.id for j in jobs)
    rank = {j.id: make_verdict(j.id, "rank", score)
            for j, score in zip(jobs, [90, 82, 82, 82, 70])}

    picked, _offset = refine_shortlist(jobs, filters, rank, {}, top=2, first_pass=2)

    assert _ids(picked) == [clear, *sorted([tie_a, tie_b, tie_c])]
    assert below not in _ids(picked)


def test_refine_shortlist_breaks_a_tie_by_position_then_rank_score_then_id():
    """Equal effective scores: the placed jobs first, best-ranked first, and the id decides."""
    jobs = [make_job("placed-high", "Junior Backend Developer"), make_job("placed-low", "Junior SRE"),
            make_job("bare-a", "Junior QA Engineer"), make_job("bare-b", "Junior Data Engineer")]
    high, low, bare_a, bare_b = (j.id for j in jobs)
    filters = {j.id: FilterResult(job_id=j.id, status="keep", location_tier=1) for j in jobs}
    # two verdicts overlap, which is too few to calibrate on: every job is worth exactly 80
    rank = {high: make_verdict(high, "rank", 90), low: make_verdict(low, "rank", 70),
            bare_a: make_verdict(bare_a, "rank", 80), bare_b: make_verdict(bare_b, "rank", 80)}
    refine = {high: make_refine(high, 70, 1), low: make_refine(low, 90, 1)}  # the same position

    picked, offset = refine_shortlist(jobs, filters, rank, refine, top=4, first_pass=4)

    assert offset == 0.0
    assert _ids(picked) == [high, low, *sorted([bare_a, bare_b])]


# ------------------------------------------------------- diff between reports


def make_snapshot(report_id: int | None = None, *, ranked=(), prefilter=(), review=(),
                  created_at: datetime | None = None) -> ReportSnapshot:
    """A snapshot with hand-made items: ``ranked``/``prefilter`` are (job_id, score) pairs."""
    items: list[ReportItem] = []
    for pos, (job_id, score) in enumerate(ranked, 1):
        items.append(ReportItem(job_id=job_id, section="ranked", position=pos, score=score))
    for pos, (job_id, score) in enumerate(prefilter, 1):
        items.append(ReportItem(job_id=job_id, section="prefilter", position=pos, score=score))
    for pos, job_id in enumerate(review, 1):
        items.append(ReportItem(job_id=job_id, section="review", position=pos, score=None))
    return ReportSnapshot(id=report_id, days=7, prompt_version="v1", counts={}, items=items,
                          created_at=created_at or datetime(2026, 9, 10, 6, 0, tzinfo=UTC))


def test_diff_reports_splits_new_gone_and_moved():
    old = make_snapshot(1, ranked=[("a", 90), ("b", 80)], prefilter=[("c", 60)])
    new = make_snapshot(2, ranked=[("a", 95), ("c", 70)], prefilter=[("d", 50)])

    diff = diff_reports(new, old)

    assert (diff.old_id, diff.new_id) == (1, 2)
    assert diff.old_created_at == old.created_at
    assert [i.job_id for i in diff.new_ranked] == ["c"]          # c moved prefilter -> ranked
    assert [i.job_id for i in diff.gone_ranked] == ["b"]
    assert [i.job_id for i in diff.new_prefilter] == ["d"]
    assert [(m.job_id, m.old_score, m.new_score) for m in diff.moved] == []  # 90 -> 95 is < 10


def test_diff_reports_keeps_new_ranked_in_position_order():
    old = make_snapshot(3, ranked=[("keep", 70)])
    new = make_snapshot(4, ranked=[("x", 99), ("keep", 70), ("y", 65), ("z", 60)])

    diff = diff_reports(new, old)
    assert [i.job_id for i in diff.new_ranked] == ["x", "y", "z"]
    assert [i.position for i in diff.new_ranked] == [1, 3, 4]


def test_diff_reports_moved_only_lists_swings_of_ten_or_more():
    old = make_snapshot(1, ranked=[("big", 50), ("small", 50), ("down", 90)])
    new = make_snapshot(2, ranked=[("down", 60), ("big", 61), ("small", 59)])

    diff = diff_reports(new, old)
    moved = {m.job_id: m for m in diff.moved}
    assert set(moved) == {"big", "down"}       # small moved by 9 only
    assert (moved["big"].old_score, moved["big"].new_score) == (50, 61)
    assert (moved["big"].old_position, moved["big"].new_position) == (1, 2)
    assert moved["big"].delta == 11
    assert moved["down"].delta == -30


def test_a_move_without_both_scores_has_no_delta():
    """An unscored side (a report written before scores were stored) is not a swing of -70."""
    from jobscraper.models import ReportMove

    move = ReportMove(job_id="a", old_score=None, new_score=70, old_position=3, new_position=1)
    assert move.delta == 0


def test_diff_reports_new_prefilter_ignores_jobs_that_were_ranked_before():
    old = make_snapshot(1, ranked=[("a", 90)], prefilter=[("b", 60)])
    new = make_snapshot(2, ranked=[], prefilter=[("a", 55), ("b", 60), ("c", 40)])

    diff = diff_reports(new, old)
    assert [i.job_id for i in diff.new_prefilter] == ["c"]   # a was ranked, b was in prefilter
    assert [i.job_id for i in diff.gone_ranked] == ["a"]


def test_diff_reports_against_nothing_is_all_new():
    new = make_snapshot(1, ranked=[("a", 90)], prefilter=[("b", 60)], review=["c"])

    diff = diff_reports(new, None)
    assert diff.old_id is None
    assert diff.old_created_at is None
    assert diff.new_id == 1
    assert [i.job_id for i in diff.new_ranked] == ["a"]
    assert [i.job_id for i in diff.new_prefilter] == ["b"]
    assert diff.gone_ranked == [] and diff.moved == []


def test_previous_report_returns_the_snapshot_before_with_items(tmp_path):
    from jobscraper.store import Store

    store = Store(tmp_path / "jobs.db")
    try:
        first = store.save_report(make_snapshot(ranked=[("a", 90)]))
        second = store.save_report(make_snapshot(ranked=[("b", 80)]))
        third = store.save_report(make_snapshot(ranked=[("c", 70)]))

        prev = previous_report(store, third)
        assert prev is not None and prev.id == second.id
        assert [i.job_id for i in prev.items] == ["b"]
        assert previous_report(store, first) is None
    finally:
        store.close()


# ----------------------------------------------------------------- render_diff


@pytest.fixture
def diff_state():
    """Two jobs newly ranked / newly prefiltered, one that dropped out of the ranking."""
    fresh = make_job("fresh-1", "Junior Go Developer")
    pre = make_job("pre-9", "Graduate Cloud Engineer", location_raw="Tallinn, Estonia", country="EE")
    gone = make_job("gone-1", "Junior Rust Developer")
    jobs_by_id = {j.id: j for j in (fresh, pre, gone)}
    ranked_verdicts = {
        fresh.id: make_verdict(fresh.id, "rank", 88, why_apply=["Go and Kubernetes"],
                               concerns=["no salary range"]),
        gone.id: make_verdict(gone.id, "rank", 72),
    }
    prefilter_verdicts = {pre.id: make_verdict(pre.id, "prefilter", 61)}
    old = make_snapshot(7, ranked=[(gone.id, 72)], created_at=datetime(2026, 9, 9, 6, 30, tzinfo=UTC))
    new = make_snapshot(8, ranked=[(fresh.id, 88)], prefilter=[(pre.id, 61)])
    return jobs_by_id, ranked_verdicts, prefilter_verdicts, old, new


def test_render_diff_header_blocks_table_and_dropped_list(diff_state):
    jobs_by_id, ranked_verdicts, prefilter_verdicts, old, new = diff_state
    fresh = next(j for j in jobs_by_id.values() if j.source_id == "fresh-1")
    pre = next(j for j in jobs_by_id.values() if j.source_id == "pre-9")
    gone = next(j for j in jobs_by_id.values() if j.source_id == "gone-1")

    text = render_diff(diff_reports(new, old), jobs_by_id, ranked_verdicts, prefilter_verdicts)

    assert ("1 new ranked · 1 new in prefilter · 1 dropped out of ranked since report #7 "
            "(created 2026-09-09 06:30 UTC)") in text
    # the new ranked job gets the full write_report-style block
    assert f"### 88 · [{fresh.title}]({fresh.url}) — Example Oy" in text
    assert "*Helsinki, Finland · hybrid · teamtailor · posted 2026-09-01*" in text
    assert f"Summary for {fresh.id}." in text
    assert "**Why apply:** Go and Kubernetes" in text
    # the new prefilter survivor gets a compact table row
    assert "| score | title | company | location | source |" in text
    assert f"| 61 | [{pre.title}]({pre.url}) | Example Oy | Tallinn, Estonia / hybrid | teamtailor |" in text
    # and the job that fell out of the ranking is a bullet with its old score
    assert f"- [{gone.title}]({gone.url}) — Example Oy (was 72)" in text


def test_render_diff_without_a_previous_report(diff_state):
    jobs_by_id, ranked_verdicts, prefilter_verdicts, _old, new = diff_state
    text = render_diff(diff_reports(new, None), jobs_by_id, ranked_verdicts, prefilter_verdicts)
    assert "First report in this database — nothing to diff against." in text
    assert "1 new ranked · 1 new in prefilter · 0 dropped out of ranked" in text
    assert "since report #" not in text


def test_render_diff_lists_score_moves(diff_state):
    jobs_by_id, ranked_verdicts, prefilter_verdicts, _old, _new = diff_state
    gone = next(j for j in jobs_by_id.values() if j.source_id == "gone-1")
    old = make_snapshot(1, ranked=[(gone.id, 40)])
    new = make_snapshot(2, ranked=[(gone.id, 72)])

    text = render_diff(diff_reports(new, old), jobs_by_id, ranked_verdicts, prefilter_verdicts)
    assert f"- [{gone.title}]({gone.url}) — Example Oy: 40 → 72 (+32)" in text


def test_render_diff_with_nothing_new_says_so(diff_state):
    jobs_by_id, ranked_verdicts, prefilter_verdicts, _old, _new = diff_state
    job_id = next(iter(jobs_by_id))
    text = render_diff(diff_reports(make_snapshot(2, ranked=[(job_id, 88)]),
                                    make_snapshot(1, ranked=[(job_id, 88)])),
                       jobs_by_id, ranked_verdicts, prefilter_verdicts)
    assert "0 new ranked · 0 new in prefilter · 0 dropped out of ranked" in text
    assert "Nothing new since the previous report." in text


def test_render_diff_skips_items_whose_job_is_unknown(diff_state):
    jobs_by_id, ranked_verdicts, prefilter_verdicts, old, _new = diff_state
    new = make_snapshot(8, ranked=[("ghost", 99)], prefilter=[("ghost2", 50)])
    text = render_diff(diff_reports(new, old), jobs_by_id, ranked_verdicts, prefilter_verdicts)
    assert "ghost" not in text
    assert "1 new ranked · 1 new in prefilter" in text  # counts still describe the diff


def test_render_new_section_heading_counts_and_unknown_jobs(diff_state):
    """The short section `jobscraper report` prepends: heading, counts, new-ranked blocks only."""
    jobs_by_id, ranked_verdicts, _prefilter_verdicts, old, new = diff_state
    fresh = next(j for j in jobs_by_id.values() if j.source_id == "fresh-1")

    section = render_new_section(diff_reports(new, old), jobs_by_id, ranked_verdicts)
    assert section.startswith("## New since report #7")
    assert "1 new ranked · 1 new in prefilter · 1 dropped out of ranked since report #7" in section
    assert f"### 88 · [{fresh.title}]({fresh.url}) — Example Oy" in section
    assert "| score |" not in section        # the prefilter table stays in the diff file
    assert "(was 72)" not in section         # so does the dropped-out list

    first = render_new_section(diff_reports(new, None), {}, ranked_verdicts)
    assert first.startswith("## New in this report")
    assert "###" not in first                # nothing to render: the job is not in jobs_by_id


def test_render_diff_falls_back_to_the_item_score_without_a_verdict(diff_state):
    jobs_by_id, _ranked_verdicts, prefilter_verdicts, old, new = diff_state
    fresh = next(j for j in jobs_by_id.values() if j.source_id == "fresh-1")
    text = render_diff(diff_reports(new, old), jobs_by_id, {}, prefilter_verdicts)
    assert f"### 88 · [{fresh.title}]({fresh.url}) — Example Oy" in text
    assert "**Why apply:**" not in text


# ------------------------------------------- the queue the rank stage works through
# Measured 2026-09-11: the screen score does not predict the rank score (Spearman -0.06), so the
# queue's order is only a prior, and the stop rule reading it is what decides when to stop.


@pytest.fixture
def queue_state():
    """Six jobs: four screen survivors (one already ranked), one too weak, one the rules drop."""
    jobs = [make_job("best", "Junior Backend Developer"), make_job("tie-a", "Junior SRE"),
            make_job("tie-b", "Junior QA Engineer"), make_job("ranked", "Junior Data Engineer"),
            make_job("weak", "Junior Platform Engineer"),
            make_job("dropped", "Junior Polish-only Developer")]
    best, tie_a, tie_b, ranked, weak, dropped = (j.id for j in jobs)
    filters = {j.id: FilterResult(job_id=j.id, status="keep", location_tier=1) for j in jobs}
    filters[dropped] = FilterResult(job_id=dropped, status="drop", reasons=["Polish required"])
    prefilter = {jid: make_verdict(jid, "prefilter", score)
                 for jid, score in [(best, 90), (tie_a, 70), (tie_b, 70), (ranked, 80),
                                    (weak, 12), (dropped, 95)]}
    return jobs, filters, prefilter, {ranked: make_verdict(ranked, "rank", 55)}


def test_rank_queue_orders_the_unranked_survivors_by_screen_score(queue_state):
    jobs, filters, prefilter, rank = queue_state
    best, tie_a, tie_b = (j.id for j in jobs[:3])

    queue = rank_queue(jobs, filters, prefilter, rank, min_score=30)

    # the already-ranked 80, the 12 below the threshold and the dropped 95 are all out
    assert _ids(queue) == [best, *sorted([tie_a, tie_b])]


def test_rank_queue_leaves_out_screen_rejects_and_jobs_outside_the_window(queue_state):
    jobs, filters, prefilter, _rank = queue_state
    best = jobs[0].id
    prefilter[best] = make_verdict(best, "prefilter", 90, relevant=False)

    # only the two 70s are left, and a job the day window no longer returns is not in the queue
    assert _ids(rank_queue(jobs[:3], filters, prefilter, {}, min_score=30)) == sorted(
        [jobs[1].id, jobs[2].id])
    assert rank_queue([], filters, prefilter, {}, min_score=30) == []


def test_effective_top_returns_the_ids_and_the_score_at_the_boundary(shortlist_state):
    jobs, filters, rank, refine = shortlist_state
    best, second, _sinks, rises = (j.id for j in jobs[:4])

    ids, edge = effective_top(jobs, filters, rank, refine, top=3)

    assert ids == [best, second, rises]
    assert edge == 83  # `rises` carries its rank score alone, and that is the boundary
    assert effective_top(jobs, filters, {}, {}, top=3) == ([], None)


def test_effective_top_keeps_a_tie_at_the_boundary_whole():
    jobs = [make_job("clear", "Junior Backend Developer"), make_job("tie-a", "Junior SRE"),
            make_job("tie-b", "Junior QA Engineer")]
    filters = {j.id: FilterResult(job_id=j.id, status="keep", location_tier=1) for j in jobs}
    rank = {j.id: make_verdict(j.id, "rank", score) for j, score in zip(jobs, [90, 82, 82])}

    ids, edge = effective_top(jobs, filters, rank, {}, top=2)

    assert ids == [jobs[0].id, *sorted([jobs[1].id, jobs[2].id])]
    assert edge == 82


def test_entered_top_counts_only_the_new_arrivals():
    assert entered_top({"a"}, ["a", "b", "c"], ["b", "c"]) == 2
    assert entered_top({"a"}, ["a", "b"], ["c"]) == 0        # ranked, but nowhere near the top
    assert entered_top({"a", "b"}, ["a", "b"], ["b"]) == 0   # already there: not an entrant
    assert entered_top(set(), [], ["a"]) == 0


def test_miss_run_counts_the_rankings_since_the_last_entrant():
    assert miss_run([]) == 0
    assert miss_run([(15, 1), (15, 0), (15, 0)]) == 30
    assert miss_run([(15, 0), (15, 2)]) == 0        # an entrant resets the run
    assert miss_run([(15, 0), (0, 0)]) == 15        # a window that scored nothing adds nothing
