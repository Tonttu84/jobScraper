"""Tests for the FastAPI app in :mod:`jobscraper.web`.

Everything runs against a temporary SQLite file seeded through :class:`~jobscraper.store.Store`;
no network, no AI calls.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jobscraper.ai.prompts import PROMPT_VERSION
from jobscraper.facets import FACETS_VERSION, compute_all
from jobscraper.models import AIVerdict, Decision, FilterResult, Job
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
        "model": "claude-opus-5" if stage == "rank" else "claude-sonnet-5",
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
        description="Java and Spring Boot backend work.",
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
            signals={"posting_language": "en", "languages_required": []},
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
    assert meta["users"] == ["ada"]
    assert set(meta["decision_statuses"]) == {
        "interested", "applied", "skipped", "interview", "rejected", "offer"
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
    assert ui == {"preset": "student", "default_languages": ["en"], "web_dev_toggle": True}


def test_meta_ui_block_for_the_tailored_preset(tmp_path):
    """A one-person page offers exactly the languages that person speaks, and no FSO box."""
    _seed(tmp_path / "t.db")
    with TestClient(create_app(tmp_path / "t.db", languages=["PT", "en"], preset="tailored")) as client:
        meta = client.get("/api/meta").json()
    # the observed "de" is not offered: nobody else is going to use this page
    assert meta["languages_offered"] == ["en", "pt"]
    assert meta["ui"] == {"preset": "tailored", "default_languages": ["en", "pt"],
                          "web_dev_toggle": False}
    # the observed counts are unchanged — only what is offered as a tick box narrows
    assert meta["languages"] == {"en": 3, "de": 1}


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
    assert first["rank"]["why_apply"] == ["React and Node work", "junior friendly"]
    assert first["prefilter"]["score"] == 80
    assert first["filter"]["status"] == "keep"
    assert first["filter"]["location_tier"] == 1
    assert first["filter"]["signals"]["work_rights_note"].startswith("EU citizens")
    assert first["facets"]["stacks"] == ["web"]
    assert first["facets"]["web_dev"] is True
    assert first["facets"]["posting_language"] == "en"
    assert first["decision"] is None
    assert "description" not in first

    # the review leftover has no AI score at all
    last = payload["items"][-1]
    assert last["score"] is None
    assert last["section"] == "review"
    assert last["prefilter"] is None
    assert last["rank"] is None
    assert last["filter"]["reasons"] == ["asks for 5 years of experience"]


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
                               "concerns": [], "why_apply": [], "position": 1}
    assert first["score"] == 81  # (91 + 71) / 2


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
