"""Tests for the FastAPI app in :mod:`jobscraper.web`.

Everything runs against a temporary SQLite file seeded through :class:`~jobscraper.store.Store`;
no network, no AI calls.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from jobscraper.ai.prompts import PROMPT_VERSION
from jobscraper.facets import FACETS_VERSION, compute_all
from jobscraper.models import AIVerdict, Decision, FilterResult, Job, LivenessCheck
from jobscraper.report import build_snapshot
from jobscraper.store import Store
from jobscraper.web.app import create_app


def make_job(source_id: str, title: str, **kw) -> Job:
    base = {
        "source": "teamtailor",
        "source_id": source_id,
        "url": f"https://jobs.example.test/{source_id}",
        "title": title,
        "company": "Example Oy",
        "description": "We build backend services.",
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
        "model": {"rank": "claude-opus-5", "refine": "claude-fable-5-1"}.get(stage, "claude-sonnet-5"),
        "prompt_version": PROMPT_VERSION,
        "relevant": True,
        "score": score,
        "language_ok": True,
        "seniority_ok": True,
        "location_ok": True,
        "summary": f"Summary for {job_id}.",
    }
    base.update(kw)
    return AIVerdict(**base)


# --------------------------------------------------------------------------- seed


def _seed(db_path, *, save_report: bool = True, save_facets: bool = True) -> dict[str, Job]:
    """Four jobs: a ranked React/Node job in FI, a German C++ job, a remote Python job and a
    rule-filter 'review' leftover. Returns them keyed by a short handle."""
    web = make_job(
        "web-1",
        "Junior Frontend Developer",
        company="Reactive Oy",
        description="We build single page apps with React, Node.js and TypeScript.",
        country="FI",
        city="Helsinki",
        remote="hybrid",
        tags=["frontend"],
        employment_type="full_time",
        salary_text="3500-4200 EUR/month",
    )
    cpp = make_job(
        "cpp-1",
        "C++ Firmware Entwickler",
        company="Eingebettet GmbH",
        description="Wir entwickeln Steuerungssoftware in C++ für Mikrocontroller.",
        country="DE",
        city="Berlin",
        location_raw="Berlin, Deutschland",
        remote="onsite",
    )
    py = make_job(
        "py-1",
        "Backend Developer (Python)",
        company="Remote Labs",
        description="We build APIs in Python with Django.",
        country="PT",
        city=None,
        location_raw="Remote (Europe)",
        remote="remote",
        remote_region="Europe only",
    )
    rev = make_job(
        "rev-1",
        "Software Engineer",
        company="Nordic AB",
        description="Java and Spring Boot backend work. "
                    "We are building a talent pool for future openings.",
        country="SE",
        city="Stockholm",
        location_raw="Stockholm, Sweden",
        remote="unknown",
    )
    jobs = [web, cpp, py, rev]

    filters = {
        web.id: FilterResult(
            job_id=web.id,
            status="keep",
            location_tier=1,
            reasons=["junior title"],
            signals={
                "posting_language": "en",
                "languages_required": [],
                "work_rights_note": "EU citizens only; no visa sponsorship.",
            },
        ),
        cpp.id: FilterResult(
            job_id=cpp.id,
            status="keep",
            location_tier=2,
            signals={"posting_language": "de", "languages_required": ["de"]},
        ),
        py.id: FilterResult(
            job_id=py.id,
            status="keep",
            location_tier=0,
            signals={"posting_language": "en", "languages_required": [],
                     "languages_optional": ["pt"]},
        ),
        rev.id: FilterResult(
            job_id=rev.id,
            status="review",
            location_tier=2,
            reasons=["asks for 5 years of experience"],
            # the one seeded posting whose text stated a closing date — and the one that is a
            # talent pool rather than a vacancy
            signals={"posting_language": "en", "languages_required": [],
                     "deadline": "2026-09-13", "closes_in_days": 3,
                     "evergreen": "talent pool"},
        ),
    }
    prefilter = {
        web.id: make_verdict(web.id, "prefilter", 80),
        py.id: make_verdict(py.id, "prefilter", 70),
        # stored under an older prompt version: the app must fall back to it
        cpp.id: make_verdict(cpp.id, "prefilter", 55, prompt_version="old-version"),
    }
    ranked = {
        web.id: make_verdict(
            web.id, "rank", 91,
            # two sentences on purpose: the UI teaser shows only the first one
            summary="Strong React and Node fit for a junior developer. "
                    "The role is hybrid with three office days.",
            why_apply=["React and Node work", "junior friendly"],
            concerns=["hybrid, three days in the office"],
        ),
    }

    store = Store(db_path)
    store.upsert_jobs(jobs)
    store.save_filter_results(list(filters.values()), rules_version="test")
    for v in [*prefilter.values(), *ranked.values()]:
        store.save_verdict(v)
    if save_facets:
        store.save_facets(compute_all(jobs, filters))
    store.save_decision(Decision(job_id=cpp.id, user="ada", status="skipped", note="no German"))
    if save_report:
        snap = build_snapshot(jobs, filters, prefilter, ranked, days=30,
                              cost={"total": 1.25}, prompt_version=PROMPT_VERSION)
        store.save_report(snap)
    store.close()
    return {"web": web, "cpp": cpp, "py": py, "rev": rev}


@pytest.fixture
def seeded(tmp_path):
    jobs = _seed(tmp_path / "t.db")
    with TestClient(create_app(tmp_path / "t.db")) as client:
        yield client, jobs


@pytest.fixture
def unreported(tmp_path):
    """The same state, but nothing was ever written to the ``reports`` table."""
    jobs = _seed(tmp_path / "t.db", save_report=False)
    with TestClient(create_app(tmp_path / "t.db")) as client:
        yield client, jobs


@pytest.fixture
def unfaceted(tmp_path):
    """No ``job_facets`` rows at all: the app has to compute them per request."""
    jobs = _seed(tmp_path / "t.db", save_facets=False)
    with TestClient(create_app(tmp_path / "t.db")) as client:
        yield client, jobs


def ids(payload) -> list[str]:
    return [item["id"] for item in payload["items"]]


# ----------------------------------------------------------------------------- page


def test_index_page_is_served(seeded):
    client, _jobs = seeded
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "jobscraper" in resp.text


def test_the_app_description_names_no_particular_candidate(seeded):
    client, _jobs = seeded
    info = client.get("/openapi.json").json()["info"]
    assert info["description"] == "Ranked jobs with per-user decisions."


def test_static_index_file_resolves_from_the_package():
    from jobscraper.web import app as web_app

    assert web_app.INDEX_HTML.is_file()
    assert "<html" in web_app.INDEX_HTML.read_text(encoding="utf-8").lower()


# ----------------------------------------------------------------------------- meta


def test_meta_reports_counts_facets_and_users(seeded):
    client, _jobs = seeded
    meta = client.get("/api/meta").json()

    assert meta["report"]["id"] == 1
    assert meta["report"]["days"] == 30
    assert meta["report"]["prompt_version"] == PROMPT_VERSION
    assert meta["report"]["counts"]["jobs"] == 4
    assert meta["report"]["cost"] == {"total": 1.25}

    assert meta["sections"] == {"ranked": 1, "prefilter": 2, "review": 1}
    assert meta["languages"] == {"en": 3, "de": 1}
    assert meta["languages_required"] == {"de": 1}
    assert meta["languages_offered"] == ["de", "en"]  # observed + English, no profile given
    assert meta["stacks"]["web"] == 1
    assert meta["stacks"]["python"] == 1
    assert meta["countries"] == {"FI": 1, "DE": 1, "PT": 1, "SE": 1}
    assert meta["remote"] == {"hybrid": 1, "onsite": 1, "remote": 1, "unknown": 1}
    assert meta["sources"] == {"teamtailor": 4}
    assert meta["evergreen"] == 1  # the talent-pool leftover, for the sidebar's count
    assert meta["users"] == ["ada"]
    assert set(meta["decision_statuses"]) == {
        "interested", "applied", "skipped", "interview", "rejected", "offer", "closed"
    }
    assert meta["facets_version"] == FACETS_VERSION


def test_meta_offers_the_served_profiles_languages(tmp_path):
    """A Portuguese candidate gets a "pt" box even when no posting in the report is Portuguese."""
    _seed(tmp_path / "t.db")
    with TestClient(create_app(tmp_path / "t.db", languages=["pt", "EN"])) as client:
        meta = client.get("/api/meta").json()
    assert meta["languages_offered"] == ["de", "en", "pt"]


def test_meta_ui_block_describes_the_student_preset(seeded):
    """The default page is the one shared with fellow students: pick your languages, FSO box."""
    client, _jobs = seeded
    ui = client.get("/api/meta").json()["ui"]
    assert ui == {"preset": "student", "default_languages": ["en"], "web_dev_toggle": True,
                  "tailor": False}


def test_meta_ui_block_for_the_tailored_preset(tmp_path):
    """A one-person page offers exactly the languages that person speaks, and no FSO box."""
    _seed(tmp_path / "t.db")
    with TestClient(create_app(tmp_path / "t.db", languages=["PT", "en"], preset="tailored")) as client:
        meta = client.get("/api/meta").json()
    # the observed "de" is not offered: nobody else is going to use this page
    assert meta["languages_offered"] == ["en", "pt"]
    assert meta["ui"] == {"preset": "tailored", "default_languages": ["en", "pt"],
                          "web_dev_toggle": False, "tailor": False}
    # the observed counts are unchanged — only what is offered as a tick box narrows
    assert meta["languages"] == {"en": 3, "de": 1}


@pytest.mark.parametrize("preset", ["student", "tailored"])
def test_meta_ui_block_says_whether_to_show_the_tailor_cv_button(tmp_path, preset):
    """``serve`` turns the button on when the config has a CV parts library; off by default."""
    _seed(tmp_path / "t.db")
    with TestClient(create_app(tmp_path / "t.db", preset=preset)) as client:
        assert client.get("/api/meta").json()["ui"]["tailor"] is False
    with TestClient(create_app(tmp_path / "t.db", preset=preset, tailor=True)) as client:
        ui = client.get("/api/meta").json()["ui"]
    assert (ui["preset"], ui["tailor"]) == (preset, True)


def test_meta_is_null_for_an_on_the_fly_view(unreported):
    client, _jobs = unreported
    meta = client.get("/api/meta").json()
    assert meta["report"] is None
    assert meta["sections"] == {"ranked": 1, "prefilter": 2, "review": 1}


def test_unknown_report_id_is_404(seeded):
    client, _jobs = seeded
    assert client.get("/api/meta?report_id=999").status_code == 404
    assert client.get("/api/jobs?report_id=999").status_code == 404


# ---------------------------------------------------------------------------- jobs


def test_jobs_default_ordering_and_shape(seeded):
    client, jobs = seeded
    payload = client.get("/api/jobs").json()

    assert payload["total"] == 4
    assert ids(payload) == [jobs["web"].id, jobs["py"].id, jobs["cpp"].id, jobs["rev"].id]

    first = payload["items"][0]
    assert first["title"] == "Junior Frontend Developer"
    assert first["company"] == "Reactive Oy"
    assert first["url"] == jobs["web"].url
    assert first["source"] == "teamtailor"
    assert first["country"] == "FI"
    assert first["city"] == "Helsinki"
    assert first["remote"] == "hybrid"
    assert first["employment_type"] == "full_time"
    assert first["salary_text"] == "3500-4200 EUR/month"
    assert first["tags"] == ["frontend"]
    assert first["score"] == 91
    assert first["section"] == "ranked"
    assert first["position"] == 1
    assert first["rank"]["score"] == 91
    assert first["rank"]["model"] == "claude-opus-5"  # the UI labels the component scores by model
    assert first["prefilter"]["model"] == "claude-sonnet-5"
    assert first["rank"]["why_apply"] == ["React and Node work", "junior friendly"]
    assert first["prefilter"]["score"] == 80
    assert first["filter"]["status"] == "keep"
    assert first["filter"]["location_tier"] == 1
    assert first["filter"]["signals"]["work_rights_note"].startswith("EU citizens")
    assert first["facets"]["stacks"] == ["web"]
    assert first["facets"]["web_dev"] is True
    assert first["facets"]["posting_language"] == "en"
    assert first["decision"] is None
    assert first["deadline"] is None  # this posting stated no closing date
    assert first["closes_in_days"] is None
    assert first["evergreen"] is False  # and it is a real vacancy
    assert first["facets"]["evergreen"] is False
    assert "description" not in first

    # the review leftover has no AI score at all
    last = payload["items"][-1]
    assert last["score"] is None
    assert last["section"] == "review"
    assert last["prefilter"] is None
    assert last["rank"] is None
    assert last["filter"]["reasons"] == ["asks for 5 years of experience"]
    # the closing date is lifted out of the signal dict so the card can show it without digging
    assert last["deadline"] == "2026-09-13"
    assert last["closes_in_days"] == 3
    # and so is the talent-pool verdict, which is a facet as well so the sidebar can hide it
    assert last["evergreen"] is True
    assert last["facets"]["evergreen"] is True


def test_list_items_carry_the_verdict_summaries_the_teaser_needs(seeded):
    """The page's one-line teaser reads ``rank``/``prefilter`` straight off the list payload."""
    client, jobs = seeded
    by_id = {item["id"]: item for item in client.get("/api/jobs").json()["items"]}

    web = by_id[jobs["web"].id]
    assert web["rank"]["summary"].startswith("Strong React and Node fit for a junior developer.")
    # a prefilter-only job still carries a summary to show
    py = by_id[jobs["py"].id]
    assert py["rank"] is None
    assert py["prefilter"]["summary"] == f"Summary for {jobs['py'].id}."
    # and the rule-review leftover has nothing to teaser with
    rev = by_id[jobs["rev"].id]
    assert rev["rank"] is None and rev["prefilter"] is None


def test_a_refine_verdict_averages_the_score_and_is_exposed_on_its_own(tmp_path):
    """The list orders by the mean of the two AI passes; the UI can still show each of them."""
    jobs = _seed(tmp_path / "t.db")
    store = Store(tmp_path / "t.db")
    store.save_verdict(make_verdict(jobs["web"].id, "refine", 71, model="claude-fable-5-1",
                                    position=1, summary="Best of the shortlist."))
    store.close()
    with TestClient(create_app(tmp_path / "t.db")) as client:
        first = next(i for i in client.get("/api/jobs").json()["items"] if i["id"] == jobs["web"].id)
    assert first["rank"]["score"] == 91
    assert first["refine"] == {"score": 71, "relevant": True, "summary": "Best of the shortlist.",
                               "model": "claude-fable-5-1", "concerns": [], "why_apply": [], "position": 1,
                               "calibrated": 71, "offset": 0.0}
    assert first["score"] == 81  # (91 + 71) / 2, one shared job being too few to calibrate


def _seed_calibrated(db_path) -> dict[str, Job]:
    """The seeded database with three jobs scored by both AI passes, at a +22.0 refine offset."""
    jobs = _seed(db_path)
    store = Store(db_path)
    for handle, rank_score, refine_score, position in [("web", 91, 63, 1), ("cpp", 80, 60, 2),
                                                       ("py", 70, 52, 3)]:
        job_id = jobs[handle].id
        if handle != "web":  # "web" is the one job the seed ranks
            store.save_verdict(make_verdict(job_id, "rank", rank_score))
        store.save_verdict(make_verdict(job_id, "refine", refine_score, position=position,
                                        model="claude-fable-5-1",
                                        summary=f"Place {position} of the shortlist."))
    store.close()
    return jobs


def test_a_refine_score_is_read_on_the_rank_scale_before_the_two_are_averaged(tmp_path):
    """Three jobs scored twice are enough to measure the gap between the two raters' scales."""
    jobs = _seed_calibrated(tmp_path / "t.db")
    with TestClient(create_app(tmp_path / "t.db")) as client:
        first = next(i for i in client.get("/api/jobs").json()["items"] if i["id"] == jobs["web"].id)
    assert first["refine"]["score"] == 63       # what the refine pass actually said
    assert first["refine"]["offset"] == 22.0    # mean of 91-63, 80-60, 70-52
    assert first["refine"]["calibrated"] == 85  # 63 read on the rank stage's scale
    assert first["score"] == 88                 # round((91 + 85) / 2), not 77


def test_a_sentinel_verdict_never_changes_what_a_card_shows(tmp_path):
    """Sentinels measure the gates; a card's score and verdicts come from the real stages only."""
    jobs = _seed(tmp_path / "t.db")
    store = Store(tmp_path / "t.db")
    for handle in ("web", "cpp", "rev"):
        store.save_verdict(make_verdict(jobs[handle].id, "sentinel_rank", 3, summary="sentinel says"))
        store.save_verdict(make_verdict(jobs[handle].id, "sentinel_prefilter", 99, summary="sentinel says"))
    store.close()
    with TestClient(create_app(tmp_path / "t.db")) as client:
        items = {i["id"]: i for i in client.get("/api/jobs").json()["items"]}
        detail = client.get(f"/api/jobs/{jobs['rev'].id}").json()
    web, cpp, rev = (items[jobs[h].id] for h in ("web", "cpp", "rev"))
    assert web["score"] == 91 and web["rank"]["score"] == 91
    assert cpp["score"] == 55 and cpp["prefilter"]["score"] == 55 and cpp["rank"] is None
    assert rev["rank"] is None and rev["prefilter"] is None
    assert "sentinel says" not in str(items) and "sentinel says" not in str(detail)


def test_a_job_without_a_refine_verdict_keeps_its_rank_score(seeded):
    client, jobs = seeded
    first = next(i for i in client.get("/api/jobs").json()["items"] if i["id"] == jobs["web"].id)
    assert first["refine"] is None and first["score"] == 91


def test_prefilter_verdict_under_an_old_prompt_version_is_used(seeded):
    client, jobs = seeded
    payload = client.get("/api/jobs").json()
    cpp = next(i for i in payload["items"] if i["id"] == jobs["cpp"].id)
    assert cpp["prefilter"]["score"] == 55
    assert cpp["score"] == 55


def test_on_the_fly_view_lists_jobs_without_a_stored_report(unreported):
    client, jobs = unreported
    payload = client.get("/api/jobs").json()
    assert payload["total"] == 4
    assert ids(payload)[0] == jobs["web"].id


def test_filter_by_spoken_languages(seeded):
    client, jobs = seeded
    en = client.get("/api/jobs?langs=en").json()
    assert jobs["cpp"].id not in ids(en)
    assert en["total"] == 3

    both = client.get("/api/jobs?langs=en,de").json()
    assert jobs["cpp"].id in ids(both)
    assert both["total"] == 4

    # repeated parameters work like the comma-separated form
    repeated = client.get("/api/jobs?langs=en&langs=DE").json()
    assert repeated["total"] == 4


def test_filter_by_web_dev_stack_country_and_remote(seeded):
    client, jobs = seeded
    assert ids(client.get("/api/jobs?web_dev=true").json()) == [jobs["web"].id]
    assert ids(client.get("/api/jobs?web_dev=false").json()) == [
        jobs["py"].id, jobs["cpp"].id, jobs["rev"].id]
    assert ids(client.get("/api/jobs?stack=python").json()) == [jobs["py"].id]
    assert ids(client.get("/api/jobs?stack=python,c_cpp").json()) == [
        jobs["py"].id, jobs["cpp"].id]
    assert ids(client.get("/api/jobs?country=fi").json()) == [jobs["web"].id]
    assert ids(client.get("/api/jobs?remote=remote").json()) == [jobs["py"].id]
    assert ids(client.get("/api/jobs?source=teamtailor").json()) == [
        jobs["web"].id, jobs["py"].id, jobs["cpp"].id, jobs["rev"].id]
    assert ids(client.get("/api/jobs?source=duunitori").json()) == []


def test_filter_out_the_evergreen_adverts(seeded):
    """``evergreen=false`` is the sidebar's "hide evergreen adverts"; nothing is dropped by default."""
    client, jobs = seeded
    assert ids(client.get("/api/jobs").json()) == [
        jobs["web"].id, jobs["py"].id, jobs["cpp"].id, jobs["rev"].id]
    assert ids(client.get("/api/jobs?evergreen=false").json()) == [
        jobs["web"].id, jobs["py"].id, jobs["cpp"].id]
    assert ids(client.get("/api/jobs?evergreen=true").json()) == [jobs["rev"].id]


def test_filter_by_section_min_score_and_query(seeded):
    client, jobs = seeded
    assert ids(client.get("/api/jobs?section=review").json()) == [jobs["rev"].id]
    assert ids(client.get("/api/jobs?section=ranked,prefilter").json()) == [
        jobs["web"].id, jobs["py"].id, jobs["cpp"].id]

    # a job without a score never passes min_score
    assert ids(client.get("/api/jobs?min_score=60").json()) == [jobs["web"].id, jobs["py"].id]
    assert ids(client.get("/api/jobs?min_score=91").json()) == [jobs["web"].id]

    assert ids(client.get("/api/jobs?q=python").json()) == [jobs["py"].id]      # title
    assert ids(client.get("/api/jobs?q=REACTIVE").json()) == [jobs["web"].id]   # company
    assert ids(client.get("/api/jobs?q=nothing-here").json()) == []

    # empty parts in a comma-separated list are ignored
    assert ids(client.get("/api/jobs?stack=,python, ,").json()) == [jobs["py"].id]


def test_pagination(seeded):
    client, jobs = seeded
    page = client.get("/api/jobs?limit=1&offset=1").json()
    assert page["total"] == 4
    assert ids(page) == [jobs["py"].id]
    assert ids(client.get("/api/jobs?limit=2&offset=3").json()) == [jobs["rev"].id]
    assert client.get("/api/jobs?limit=5000").status_code == 422


def test_decision_filter_requires_a_user(seeded):
    client, _jobs = seeded
    assert client.get("/api/jobs?decision=none").status_code == 400
    assert client.get("/api/jobs?decision=applied").status_code == 400


def test_decisions_are_attached_and_filterable(seeded):
    client, jobs = seeded
    payload = client.get("/api/jobs?user=ada").json()
    cpp = next(i for i in payload["items"] if i["id"] == jobs["cpp"].id)
    assert cpp["decision"]["status"] == "skipped"
    assert cpp["decision"]["note"] == "no German"

    assert ids(client.get("/api/jobs?user=ada&decision=skipped").json()) == [jobs["cpp"].id]
    none = client.get("/api/jobs?user=ada&decision=none").json()
    assert none["total"] == 3
    assert jobs["cpp"].id not in ids(none)


def test_decision_takes_several_values(seeded):
    """The page's decision box is a multi-select: ``none`` and statuses mix freely."""
    client, jobs = seeded
    client.put(f"/api/jobs/{jobs['web'].id}/decision",
               json={"user": "ada", "status": "applied"})
    client.put(f"/api/jobs/{jobs['py'].id}/decision",
               json={"user": "ada", "status": "rejected"})

    # two statuses, no "none"
    both = client.get("/api/jobs?user=ada&decision=applied,skipped").json()
    assert set(ids(both)) == {jobs["web"].id, jobs["cpp"].id}

    # "none" alongside a status: undecided jobs plus that one
    mixed = client.get("/api/jobs?user=ada&decision=none,applied").json()
    assert set(ids(mixed)) == {jobs["web"].id, jobs["rev"].id}

    # repeated parameters say the same thing as the comma list
    repeated = client.get("/api/jobs?user=ada&decision=none&decision=applied").json()
    assert ids(repeated) == ids(mixed)

    # the page's default: everything except skipped and rejected
    default = "decision=none&decision=applied&decision=interested&decision=interview&decision=offer"
    assert set(ids(client.get(f"/api/jobs?user=ada&{default}").json())) == {
        jobs["web"].id, jobs["rev"].id
    }

    # case and spacing are normalized like the other list params
    assert ids(client.get("/api/jobs?user=ada&decision=APPLIED, skipped").json()) == ids(both)

    # an empty value is no filter at all (and needs no user)
    assert client.get("/api/jobs?decision=").json()["total"] == 4


# ------------------------------------------------------------------------ detail


def test_job_detail_includes_the_description(seeded):
    client, jobs = seeded
    detail = client.get(f"/api/jobs/{jobs['py'].id}").json()
    assert detail["description"] == "We build APIs in Python with Django."
    assert detail["facets"]["stacks"] == ["python"]
    assert detail["decision"] is None

    with_user = client.get(f"/api/jobs/{jobs['cpp'].id}?user=ada").json()
    assert with_user["decision"]["status"] == "skipped"


def test_job_detail_for_a_job_outside_the_report(seeded, tmp_path):
    client, _jobs = seeded
    extra = make_job("orphan-1", "Unfiltered Job", description="Nothing ran on this one yet.")
    store = Store(tmp_path / "t.db")
    store.upsert_jobs([extra])
    store.close()

    detail = client.get(f"/api/jobs/{extra.id}").json()
    assert detail["id"] == extra.id
    assert detail["section"] is None
    assert detail["position"] is None
    assert detail["filter"] is None
    assert detail["score"] is None
    assert detail["facets"]["stacks"] == []          # computed on the fly, never stored
    assert detail["description"] == "Nothing ran on this one yet."
    assert extra.id not in ids(client.get("/api/jobs").json())


def test_job_detail_unknown_id_is_404(seeded):
    client, _jobs = seeded
    assert client.get("/api/jobs/does-not-exist").status_code == 404


def test_facets_are_computed_when_none_are_stored(unfaceted):
    client, jobs = unfaceted
    payload = client.get("/api/jobs").json()
    assert payload["total"] == 4
    web = next(i for i in payload["items"] if i["id"] == jobs["web"].id)
    assert web["facets"]["stacks"] == ["web"]
    assert web["facets"]["web_dev"] is True
    # the on-the-fly facets still drive the filters
    assert ids(client.get("/api/jobs?langs=en").json()) == [
        jobs["web"].id, jobs["py"].id, jobs["rev"].id]


def test_stage_verdicts_uses_the_current_prompt_version_when_complete(tmp_path):
    from jobscraper.web.app import _stage_verdicts

    jobs = _seed(tmp_path / "t.db")
    store = Store(tmp_path / "t.db")
    try:
        current = _stage_verdicts(store, "rank", [jobs["web"].id])
        assert list(current) == [jobs["web"].id]
        assert current[jobs["web"].id].prompt_version == PROMPT_VERSION
        # the German prefilter verdict only exists under the old version
        assert jobs["cpp"].id not in store.verdicts("prefilter", PROMPT_VERSION)
        assert _stage_verdicts(store, "prefilter", [jobs["cpp"].id])[jobs["cpp"].id].score == 55
    finally:
        store.close()


# ---------------------------------------------------------------------- decisions


def test_decision_round_trip(seeded):
    client, jobs = seeded
    job_id = jobs["web"].id

    resp = client.put(f"/api/jobs/{job_id}/decision",
                      json={"user": "bob", "status": "applied", "note": "sent CV"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id
    assert body["user"] == "bob"
    assert body["status"] == "applied"
    assert body["note"] == "sent CV"
    assert body["updated_at"]

    listed = client.get("/api/jobs?user=bob").json()
    first = next(i for i in listed["items"] if i["id"] == job_id)
    assert first["decision"]["status"] == "applied"
    assert ids(client.get("/api/jobs?user=bob&decision=applied").json()) == [job_id]

    mine = client.get("/api/decisions?user=bob").json()
    assert [d["job_id"] for d in mine] == [job_id]
    assert len(client.get("/api/decisions").json()) == 2  # ada's seeded one too
    assert client.get("/api/meta").json()["users"] == ["ada", "bob"]

    assert client.delete(f"/api/jobs/{job_id}/decision?user=bob").status_code == 204
    assert client.delete(f"/api/jobs/{job_id}/decision?user=bob").status_code == 404
    assert client.get("/api/decisions?user=bob").json() == []


def test_decision_validation(seeded):
    client, jobs = seeded
    job_id = jobs["web"].id
    assert client.put("/api/jobs/ghost/decision",
                      json={"user": "bob", "status": "applied"}).status_code == 404
    assert client.put(f"/api/jobs/{job_id}/decision",
                      json={"user": "bob", "status": "nope"}).status_code == 422
    assert client.put(f"/api/jobs/{job_id}/decision",
                      json={"user": "   ", "status": "applied"}).status_code == 422
    assert client.put(f"/api/jobs/{job_id}/decision",
                      json={"user": "x" * 65, "status": "applied"}).status_code == 422
    # the handle is stripped before it is stored
    stored = client.put(f"/api/jobs/{job_id}/decision",
                        json={"user": "  bob  ", "status": "interested"}).json()
    assert stored["user"] == "bob"
    assert client.delete(f"/api/jobs/{job_id}/decision?user=nobody").status_code == 404


# ------------------------------------------------------------------------ reports


def test_reports_history(seeded):
    client, _jobs = seeded
    reports = client.get("/api/reports").json()
    assert len(reports) == 1
    assert reports[0]["id"] == 1
    assert reports[0]["counts"]["jobs"] == 4
    assert reports[0]["items"] == []


def test_reports_history_is_empty_without_a_stored_report(unreported):
    client, _jobs = unreported
    assert client.get("/api/reports").json() == []


def test_the_page_is_served_with_no_cache_so_a_deploy_reaches_open_browsers(seeded):
    client, _ = seeded
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    api = client.get("/api/jobs?limit=1")
    assert api.headers["cache-control"] == "no-store"


def test_the_rank_badge_follows_the_live_order_not_a_stale_report(tmp_path):
    """A report written before a refine pass (or before a scoring change) stores positions that no
    longer match the order the page shows; the badge is numbered from the live order instead."""
    db = tmp_path / "t.db"
    jobs = _seed(db)
    store = Store(db)
    store.save_verdict(make_verdict(jobs["py"].id, "rank", 85))
    snap = build_snapshot(list(store.jobs()), store.filter_results(),
                          store.verdicts("prefilter", PROMPT_VERSION), store.verdicts("rank", PROMPT_VERSION),
                          days=30, cost={}, prompt_version=PROMPT_VERSION)
    store.save_report(snap)
    stored = {i.job_id: i.position for i in snap.items if i.section == "ranked"}
    assert stored == {jobs["web"].id: 1, jobs["py"].id: 2}
    # a refine pass after the report flips them: web (91 + 60) / 2 = 76, py (85 + 90) / 2 = 88
    # (two shared jobs are too few to calibrate, so the raw refine scores count as they are)
    store.save_verdict(make_verdict(jobs["web"].id, "refine", 60, position=2, model="claude-fable-5-1"))
    store.save_verdict(make_verdict(jobs["py"].id, "refine", 90, position=1, model="claude-fable-5-1"))
    store.close()
    with TestClient(create_app(db)) as client:
        items = client.get("/api/jobs").json()["items"]
    ranked = [(i["id"], i["position"], i["score"]) for i in items if i["section"] == "ranked"]
    assert ranked == [(jobs["py"].id, 1, 88), (jobs["web"].id, 2, 76)]


def test_sections_never_interleave_even_when_a_screen_score_beats_a_rank_score(tmp_path):
    """Sonnet's screen score and Opus's rank score are different scales: a prefiltered-only job
    must not sort above a ranked one, or it would sit in the "top 20" without ever being ranked."""
    db = tmp_path / "t.db"
    jobs = _seed(db)
    store = Store(db)
    store.save_verdict(make_verdict(jobs["web"].id, "rank", 60))  # the one ranked job, now scored low
    store.close()
    with TestClient(create_app(db)) as client:
        items = client.get("/api/jobs").json()["items"]
    assert [(i["section"], i["score"]) for i in items[:3]] == [("ranked", 60), ("prefilter", 70), ("prefilter", 55)]


# ------------------------------------------------------------------ skip reasons


def test_meta_lists_the_skip_reasons_in_order_with_labels(seeded):
    client, _jobs = seeded
    reasons = client.get("/api/meta").json()["skip_reasons"]
    assert [r["key"] for r in reasons] == [
        "wrong_stack", "wrong_level", "location", "consultancy", "company",
        "duplicate_or_applied", "posting_gone", "pay", "other",
    ]
    assert reasons[0] == {"key": "wrong_stack", "label": "Wrong stack"}
    assert reasons[5]["label"] == "Duplicate / already applied"


def test_a_skip_round_trips_its_reasons_through_every_endpoint(seeded):
    client, jobs = seeded
    job_id = jobs["web"].id
    body = client.put(f"/api/jobs/{job_id}/decision",
                      json={"user": "bob", "status": "skipped", "note": "Angular shop",
                            "reasons": ["pay", "wrong_stack"]}).json()
    assert body["reasons"] == ["wrong_stack", "pay"]  # vocabulary order
    detail = client.get(f"/api/jobs/{job_id}?user=bob").json()
    assert detail["decision"]["reasons"] == ["wrong_stack", "pay"]
    listed = client.get("/api/jobs?user=bob").json()
    assert next(i for i in listed["items"] if i["id"] == job_id)["decision"]["reasons"] == \
        ["wrong_stack", "pay"]
    assert client.get("/api/decisions?user=bob").json()[0]["reasons"] == ["wrong_stack", "pay"]

    # re-PUT with fewer reasons replaces the set
    again = client.put(f"/api/jobs/{job_id}/decision",
                       json={"user": "bob", "status": "skipped", "reasons": []}).json()
    assert again["reasons"] == []


def test_a_decision_without_reasons_keeps_the_old_shape(seeded):
    client, jobs = seeded
    body = client.put(f"/api/jobs/{jobs['web'].id}/decision",
                      json={"user": "bob", "status": "applied"}).json()
    assert body["reasons"] == []
    seeded_skip = client.get(f"/api/jobs/{jobs['cpp'].id}?user=ada").json()["decision"]
    assert seeded_skip["reasons"] == []


def test_an_unknown_skip_reason_is_422(seeded):
    client, jobs = seeded
    resp = client.put(f"/api/jobs/{jobs['web'].id}/decision",
                      json={"user": "bob", "status": "skipped", "reasons": ["boring"]})
    assert resp.status_code == 422
    assert client.get("/api/decisions?user=bob").json() == []


def test_reason_filter_narrows_to_decisions_carrying_any_of_them(seeded):
    client, jobs = seeded
    put = lambda key, status, reasons: client.put(  # noqa: E731
        f"/api/jobs/{jobs[key].id}/decision",
        json={"user": "ada", "status": status, "reasons": reasons})
    put("cpp", "skipped", ["location"])
    put("web", "skipped", ["pay", "consultancy"])
    put("py", "skipped", [])

    assert client.get("/api/jobs?reason=pay").status_code == 400  # needs user=
    assert ids(client.get("/api/jobs?user=ada&reason=pay").json()) == [jobs["web"].id]
    assert set(ids(client.get("/api/jobs?user=ada&reason=pay,location").json())) == \
        {jobs["web"].id, jobs["cpp"].id}
    assert set(ids(client.get("/api/jobs?user=ada&reason=PAY&reason=location").json())) == \
        {jobs["web"].id, jobs["cpp"].id}
    # together with decision=: both must hold
    assert ids(client.get("/api/jobs?user=ada&decision=none,applied&reason=pay").json()) == []
    assert ids(client.get("/api/jobs?user=ada&decision=skipped&reason=location").json()) == \
        [jobs["cpp"].id]
    # an empty value is no filter at all
    assert client.get("/api/jobs?reason=").json()["total"] == 4


# ------------------------------------------------------- seen, scored, missing from a scrape


@contextmanager
def _clock(start: str):
    """Run the store's writes on a fake clock that starts at ``start`` and ticks a second per
    stamp, so "seen before / after that scrape" never depends on the platform clock's grain."""
    now = [datetime.fromisoformat(start)]

    def tick() -> str:
        now[0] += timedelta(seconds=1)
        return now[0].isoformat()

    with patch("jobscraper.store._now", tick):
        yield


def _seed_stale(db_path) -> dict[str, Job]:
    """The seeded jobs, first stored on 2026-10-12; a teamtailor scrape on 2026-10-17 brought
    back all of them but ``py`` — the posting that may have been taken down."""
    with _clock("2026-10-12T08:00:00+00:00"):
        jobs = _seed(db_path)
    with _clock("2026-10-17T06:00:00+00:00"):
        store = Store(db_path)
        run_id = store.start_run("teamtailor")
        store.upsert_jobs([jobs[h] for h in ("web", "cpp", "rev")], run_id)
        store.finish_run(run_id, fetched=3, new=0)
        store.close()
    return jobs


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def test_a_card_carries_when_the_job_was_first_and_last_seen(tmp_path):
    jobs = _seed_stale(tmp_path / "t.db")
    with TestClient(create_app(tmp_path / "t.db")) as client:
        items = {i["id"]: i for i in client.get("/api/jobs").json()["items"]}
    web, py = items[jobs["web"].id], items[jobs["py"].id]
    assert _when(web["first_seen"]).date().isoformat() == "2026-10-12"
    assert _when(web["last_seen"]).date().isoformat() == "2026-10-17"  # the latest scrape saw it
    assert _when(py["first_seen"]).date().isoformat() == "2026-10-12"
    assert _when(py["last_seen"]).date().isoformat() == "2026-10-12"   # and it did not see this one


def test_a_job_the_latest_scrape_of_its_source_did_not_return_is_flagged_with_that_run(tmp_path):
    jobs = _seed_stale(tmp_path / "t.db")
    with TestClient(create_app(tmp_path / "t.db")) as client:
        items = {i["id"]: i for i in client.get("/api/jobs").json()["items"]}
        detail = client.get(f"/api/jobs/{jobs['py'].id}").json()
    # the run's own start (the clock's first tick on the 17th), not the time it finished
    assert _when(items[jobs["py"].id]["missing_since_run"]) == datetime(2026, 10, 17, 6, 0, 1, tzinfo=UTC)
    assert _when(detail["missing_since_run"]) == datetime(2026, 10, 17, 6, 0, 1, tzinfo=UTC)
    # the jobs that run brought back are not flagged
    for handle in ("web", "cpp", "rev"):
        assert items[jobs[handle].id]["missing_since_run"] is None


def test_no_job_is_flagged_when_its_source_has_no_run_row(seeded):
    client, _jobs = seeded
    items = client.get("/api/jobs").json()["items"]
    assert [i["missing_since_run"] for i in items] == [None] * 4
    assert all(i["last_seen"] for i in items)


def test_a_failed_or_empty_later_run_does_not_clear_or_move_the_flag(tmp_path):
    """Only a run that brought postings back says what the board lists; a 503 says nothing."""
    db = tmp_path / "t.db"
    jobs = _seed_stale(db)
    with _clock("2026-10-18T06:00:00+00:00"):
        store = Store(db)
        store.log_run("teamtailor", 0, 0, "SourceHTTPError: 503")
        store.log_run("teamtailor", 0, 0)
        store.close()
    with TestClient(create_app(db)) as client:
        items = {i["id"]: i for i in client.get("/api/jobs").json()["items"]}
    assert _when(items[jobs["py"].id]["missing_since_run"]).date().isoformat() == "2026-10-17"
    assert items[jobs["web"].id]["missing_since_run"] is None


def test_a_run_of_another_source_never_flags_a_job(tmp_path):
    db = tmp_path / "t.db"
    jobs = _seed(db)
    store = Store(db)
    store.log_run("arbeitnow", 40, 2)
    store.close()
    with TestClient(create_app(db)) as client:
        items = client.get("/api/jobs").json()["items"]
    assert {i["id"] for i in items} == {j.id for j in jobs.values()}
    assert [i["missing_since_run"] for i in items] == [None] * 4


def test_scored_at_is_the_newest_verdict_of_the_ai_stages(tmp_path):
    db = tmp_path / "t.db"
    jobs = _seed(db)
    web_id, py_id = jobs["web"].id, jobs["py"].id
    store = Store(db)
    day = lambda d: datetime(2026, 10, d, 9, tzinfo=UTC)  # noqa: E731
    store.save_verdict(make_verdict(web_id, "prefilter", 80, created_at=day(12)))
    store.save_verdict(make_verdict(web_id, "rank", 91, created_at=day(14)))
    store.save_verdict(make_verdict(web_id, "refine", 70, created_at=day(13), model="claude-fable-5-1"))
    # a sentinel pass is not a verdict the card shows, so it does not date one either
    store.save_verdict(make_verdict(web_id, "sentinel_rank", 3, created_at=day(16)))
    store.save_verdict(make_verdict(py_id, "prefilter", 70, created_at=day(11)))
    store.close()
    with TestClient(create_app(db)) as client:
        items = {i["id"]: i for i in client.get("/api/jobs").json()["items"]}
    assert _when(items[web_id]["scored_at"]) == day(14)
    assert _when(items[py_id]["scored_at"]) == day(11)
    assert items[jobs["rev"].id]["scored_at"] is None  # the rule-review leftover was never scored


# ------------------------------------------------- a page check outweighs a missed scrape

#: Start of the 2026-10-17 teamtailor scrape in ``_seed_stale`` (the clock's first tick).
MISSING_RUN = datetime(2026, 10, 17, 6, 0, 1, tzinfo=UTC)


def _check(job: Job, status: str, when: datetime, reason: str = "HTTP 200",
           http_status: int | None = 200) -> LivenessCheck:
    return LivenessCheck(job_id=job.id, status=status, reason=reason, checked_at=when,
                         http_status=http_status)


def _with_checks(db_path, checks: list[LivenessCheck]) -> None:
    store = Store(db_path)
    store.save_liveness(checks)
    store.close()


def _items(db_path) -> dict[str, dict]:
    with TestClient(create_app(db_path)) as client:
        return {i["id"]: i for i in client.get("/api/jobs").json()["items"]}


def _detail(db_path, job: Job) -> dict:
    with TestClient(create_app(db_path)) as client:
        return client.get(f"/api/jobs/{job.id}").json()


def _seed_checked(db_path) -> dict[str, Job]:
    """The seeded jobs stored on 2026-10-12; the 2026-10-17 teamtailor scrape returned ``web``
    and ``cpp`` only. ``py``'s page was found open after that scrape (then a 403), ``rev``'s only
    the day before it; ``web``, which the scrape did return, was found open too."""
    with _clock("2026-10-12T08:00:00+00:00"):
        jobs = _seed(db_path)
    with _clock("2026-10-17T06:00:00+00:00"):
        store = Store(db_path)
        run_id = store.start_run("teamtailor")
        store.upsert_jobs([jobs["web"], jobs["cpp"]], run_id)
        store.finish_run(run_id, fetched=2, new=0)
        store.close()
    _with_checks(db_path, [
        _check(jobs["py"], "open", datetime(2026, 10, 17, 9, tzinfo=UTC)),
        _check(jobs["py"], "unknown", datetime(2026, 10, 17, 12, tzinfo=UTC), reason="HTTP 403",
               http_status=403),
        _check(jobs["rev"], "open", datetime(2026, 10, 16, 9, tzinfo=UTC)),
        _check(jobs["web"], "open", datetime(2026, 10, 17, 9, tzinfo=UTC)),
    ])
    return jobs


def test_the_checked_seed_has_one_confirmed_and_one_unconfirmed_missing_job(tmp_path):
    jobs = _seed_checked(tmp_path / "t.db")
    items = _items(tmp_path / "t.db")
    assert [items[jobs[h].id]["stale"] for h in ("py", "rev", "web", "cpp")] == [False, True, False, False]
    assert items[jobs["py"].id]["missing_since_run"] and items[jobs["rev"].id]["missing_since_run"]


def test_a_missing_job_without_a_page_check_is_stale(tmp_path):
    jobs = _seed_stale(tmp_path / "t.db")
    items = _items(tmp_path / "t.db")
    py = items[jobs["py"].id]
    assert py["stale"] is True
    assert py["checked_open_at"] is None
    for handle in ("web", "cpp", "rev"):  # the scrape brought these back
        assert items[jobs[handle].id]["stale"] is False


def test_an_open_check_after_the_missed_scrape_clears_stale(tmp_path):
    """LinkedIn and Indeed are searches: a day's search missing a posting is common, and the
    posting's own page saying it is open is the stronger signal."""
    db = tmp_path / "t.db"
    jobs = _seed_stale(db)
    later = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)
    _with_checks(db, [_check(jobs["py"], "open", later)])
    py = _items(db)[jobs["py"].id]
    assert py["stale"] is False
    assert _when(py["checked_open_at"]) == later
    assert _when(py["missing_since_run"]) == MISSING_RUN  # still reported as it is


def test_an_open_check_at_the_scrapes_start_counts_as_after_it(tmp_path):
    db = tmp_path / "t.db"
    jobs = _seed_stale(db)
    _with_checks(db, [_check(jobs["py"], "open", MISSING_RUN)])
    assert _items(db)[jobs["py"].id]["stale"] is False


def test_an_open_check_older_than_the_missed_scrape_leaves_it_stale(tmp_path):
    db = tmp_path / "t.db"
    jobs = _seed_stale(db)
    earlier = datetime(2026, 10, 16, 12, tzinfo=UTC)
    _with_checks(db, [_check(jobs["py"], "open", earlier)])
    py = _items(db)[jobs["py"].id]
    assert py["stale"] is True
    assert _when(py["checked_open_at"]) == earlier


def test_an_unknown_check_does_not_clear_stale(tmp_path):
    db = tmp_path / "t.db"
    jobs = _seed_stale(db)
    _with_checks(db, [_check(jobs["py"], "unknown", datetime(2026, 10, 17, 9, tzinfo=UTC),
                             reason="HTTP 403", http_status=403)])
    py = _items(db)[jobs["py"].id]
    assert py["stale"] is True
    assert py["checked_open_at"] is None


def test_a_closed_check_after_an_open_one_is_not_checked_open(tmp_path):
    db = tmp_path / "t.db"
    jobs = _seed_stale(db)
    _with_checks(db, [_check(jobs["py"], "open", datetime(2026, 10, 17, 8, tzinfo=UTC)),
                      _check(jobs["py"], "closed", datetime(2026, 10, 17, 9, tzinfo=UTC),
                             reason="LinkedIn: no longer accepting applications")])
    py = _items(db)[jobs["py"].id]
    assert py["checked_open_at"] is None
    assert py["stale"] is True


def test_a_job_the_scrape_returned_is_never_stale_open_check_or_not(tmp_path):
    db = tmp_path / "t.db"
    jobs = _seed_stale(db)
    when = datetime(2026, 10, 17, 9, tzinfo=UTC)
    _with_checks(db, [_check(jobs["web"], "open", when)])
    web = _items(db)[jobs["web"].id]
    assert web["stale"] is False
    assert _when(web["checked_open_at"]) == when


def test_the_detail_carries_a_newer_inconclusive_check(tmp_path):
    db = tmp_path / "t.db"
    jobs = _seed_stale(db)
    opened, refused = datetime(2026, 10, 17, 8, tzinfo=UTC), datetime(2026, 10, 17, 9, tzinfo=UTC)
    _with_checks(db, [_check(jobs["py"], "open", opened),
                      _check(jobs["py"], "unknown", refused, reason="HTTP 403", http_status=403)])
    detail = _detail(db, jobs["py"])
    assert _when(detail["checked_open_at"]) == opened
    assert detail["stale"] is False
    last = detail["last_check"]
    assert last["status"] == "unknown" and last["reason"] == "HTTP 403"
    assert _when(last["checked_at"]) == refused


def test_the_detail_carries_a_closed_check_when_there_is_no_open_one(tmp_path):
    db = tmp_path / "t.db"
    jobs = _seed_stale(db)
    _with_checks(db, [_check(jobs["py"], "gone", datetime(2026, 10, 17, 9, tzinfo=UTC),
                             reason="HTTP 404", http_status=404)])
    detail = _detail(db, jobs["py"])
    assert detail["checked_open_at"] is None
    assert detail["last_check"]["status"] == "gone"


def test_the_detail_has_no_last_check_when_the_open_check_is_the_latest(tmp_path):
    db = tmp_path / "t.db"
    jobs = _seed_stale(db)
    _with_checks(db, [_check(jobs["py"], "unknown", datetime(2026, 10, 17, 8, tzinfo=UTC),
                             reason="HTTP 429", http_status=429),
                      _check(jobs["py"], "open", datetime(2026, 10, 17, 9, tzinfo=UTC))])
    detail = _detail(db, jobs["py"])
    assert detail["checked_open_at"] is not None
    assert detail["last_check"] is None
    assert _detail(db, jobs["web"])["last_check"] is None  # never checked at all


def test_a_job_outside_the_report_still_carries_its_dates(seeded, tmp_path):
    client, _jobs = seeded
    store = Store(tmp_path / "t.db")
    extra = make_job("extra-1", "Data Engineer")
    store.upsert_jobs([extra])
    store.close()
    detail = client.get(f"/api/jobs/{extra.id}").json()
    assert detail["first_seen"] and detail["last_seen"]
    assert detail["scored_at"] is None and detail["missing_since_run"] is None
    assert detail["stale"] is False and detail["checked_open_at"] is None
