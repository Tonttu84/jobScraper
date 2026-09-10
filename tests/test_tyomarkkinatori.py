"""tyomarkkinatori.fi — parsed against trimmed copies of real API responses.

``tests/fixtures/tyomarkkinatori.json`` is the real search envelope cut down to three
postings (plus one deliberately broken record); ``tyomarkkinatori_detail.json`` is a real
detail payload with the job description shortened and the ``recruiter`` block removed.
"""

import json
from datetime import UTC, datetime

import httpx
from conftest import fixture_json

from jobscraper.sources.tyomarkkinatori import (
    Tyomarkkinatori,
    employment_type,
    parse_record,
    pick_lang,
)

SEARCH = "search/v2/search"
DETAIL = "public/jobpostings/"

HUS = "4adc9851-ac18-4456-911b-21c0ad61bfac"


def test_tyomarkkinatori_parses_list_fixture(make_ctx):
    ctx = make_ctx(
        {SEARCH: "tyomarkkinatori.json"},
        options={"queries": ["ohjelmistokehittäjä"], "max_pages": 1, "fetch_details": False},
    )
    jobs = list(Tyomarkkinatori().fetch(ctx))

    assert len(jobs) == 3  # the id-less record is skipped, not raised
    j = jobs[0]
    assert j.source == "tyomarkkinatori"
    assert j.source_id == HUS
    assert j.title == "Ohjelmistoasiantuntija Tietohallinto"
    assert j.company == "HUS Helsingin yliopistollinen sairaala"
    assert j.url == f"https://tyomarkkinatori.fi/henkiloasiakkaat/avoimet-tyopaikat/{HUS}/fi"
    assert j.location_raw == "Helsinki, Finland"
    assert j.country == "FI" and j.city == "Helsinki"
    assert j.remote == "unknown"
    assert j.posted_at == datetime(2026, 9, 7, 2, 8, 24, 444000, tzinfo=UTC)
    assert j.employment_type == "full_time, permanent"  # workTime 01 + continuityOfWork 01
    assert j.description is None  # the list carries no body
    assert j.tags == []
    # Fields the Job model has no home for stay reachable in raw.
    assert j.raw["apply_url"] == "https://ars2.equest.com/?response_id=ee780d97ea48a071ed18ae6bee5f7809"
    assert j.raw["application_deadline"] == "2026-09-21T20:59:59Z"

    # A posting abroad: the ISO code comes from location.countries, not from the office town.
    dk = jobs[1]
    assert dk.title.startswith("PhD position in 3D Concrete Printing")
    assert dk.company == "Syddansk Universitet"
    assert dk.url.endswith("/697e796b-ee8a-4cdc-aadd-0aede9620cb8/en")  # en title → en page
    assert dk.country == "DK" and dk.city is None
    assert dk.location_raw == "Denmark"
    assert dk.employment_type == "full_time, fixed_term"
    assert dk.raw["apply_url"] is None  # applicationUrl.values is {}

    # Several municipalities: all of them in location_raw, the first one as the city.
    club = jobs[2]
    assert club.title == "Programming and game design club instructor"
    assert club.location_raw == "Espoo, Helsinki, Vantaa, Finland"
    assert club.country == "FI" and club.city == "Espoo"
    assert club.employment_type == "part_time, fixed_term, seasonal"  # 02 + [02, 0201]


def test_tyomarkkinatori_hydrates_from_detail(make_ctx):
    ctx = make_ctx(
        {SEARCH: "tyomarkkinatori.json", DETAIL: "tyomarkkinatori_detail.json"},
        options={"max_pages": 1, "fetch_details": True, "max_details": 1},
    )
    jobs = list(Tyomarkkinatori().fetch(ctx))

    assert len(jobs) == 3
    j = jobs[0]
    assert j.description.startswith("Haluatko olla mukana kehittämässä")
    assert "HUSin Tietohallinto on aktiivinen toimija" in j.description
    # ESCO occupation and skill labels plus the working language become tags.
    assert j.tags == [
        "software developer",
        "adapt to changes in technological development plans",
        "lang:fi",
    ]
    assert j.employment_type == "full_time, permanent"
    assert j.posted_at == datetime(2026, 9, 7, 2, 8, 24, 444416, tzinfo=UTC)  # application.published
    assert "recruiter" not in j.raw["detail"]  # contact details are dropped, never stored
    assert jobs[1].description is None  # max_details=1 stops after the first
    assert len(ctx.http.calls) == 2  # one search, one detail


def test_tyomarkkinatori_stops_hydrating_after_403(make_ctx):
    ctx = make_ctx(
        {
            SEARCH: "tyomarkkinatori.json",
            DETAIL: lambda req: httpx.Response(403, text="rate limited"),
        },
        options={"max_pages": 1, "fetch_details": True, "max_details": 50},
    )
    jobs = list(Tyomarkkinatori().fetch(ctx))

    assert len(jobs) == 3  # every job is still yielded, just without descriptions
    assert all(j.description is None for j in jobs)
    assert len(ctx.http.calls) == 2  # the first 403 latches detail fetching off


def test_tyomarkkinatori_stops_at_last_page(make_ctx):
    """``lastPage`` is a page count, and ``pageNumber`` is zero-based (verified live)."""
    envelope = fixture_json("tyomarkkinatori.json")

    def paged(req: httpx.Request) -> httpx.Response:
        page = json.loads(req.content)["paging"]["pageNumber"]
        assert page < 2, f"page {page} requested although lastPage says there are 2 pages"
        first = {**envelope["content"][0], "id": "page-0"}
        records = [first] if page == 0 else [first, {**first, "id": "page-1"}]
        return httpx.Response(
            200, json={**envelope, "lastPage": 2, "totalElements": 120, "content": records}
        )

    ctx = make_ctx({SEARCH: paged}, options={"max_pages": 5, "fetch_details": False})
    jobs = list(Tyomarkkinatori().fetch(ctx))

    # The pages overlap (sorting=LATEST shifts as postings arrive); the repeat is dropped.
    assert [j.source_id for j in jobs] == ["page-0", "page-1"]
    assert len(ctx.http.calls) == 2


def test_tyomarkkinatori_respects_limit(make_ctx):
    ctx = make_ctx(
        {SEARCH: "tyomarkkinatori.json"},
        options={"max_pages": 3, "fetch_details": False},
        limit=2,
    )
    assert len(list(Tyomarkkinatori().fetch(ctx))) == 2
    assert len(ctx.http.calls) == 1


def test_tyomarkkinatori_detail_falls_back_to_marketing_text(make_ctx):
    detail = fixture_json("tyomarkkinatori_detail.json")
    detail["position"]["jobDescription"] = {"fi": "   "}  # employers do leave it blank
    detail["position"]["marketingDescription"] = {"fi": "<p>HUS on Suomen suurin sairaala.</p>"}
    detail["position"]["wagePrincipleInfo"] = {"fi": "3200-3800 e/kk"}
    detail["position"]["occupations"] = ["not a dict"]
    ctx = make_ctx(
        {SEARCH: "tyomarkkinatori.json", DETAIL: detail},
        options={"max_pages": 1, "fetch_details": True, "max_details": 1},
    )
    j = next(iter(Tyomarkkinatori().fetch(ctx)))

    assert j.description == "HUS on Suomen suurin sairaala."
    assert j.salary_text == "3200-3800 e/kk"
    assert j.tags == ["adapt to changes in technological development plans", "lang:fi"]


def test_tyomarkkinatori_tolerates_junk_in_location():
    rec = fixture_json("tyomarkkinatori.json")["content"][0]
    rec = {
        **rec,
        "location": {
            "foreignCountry": True,
            "municipalities": ["junk", {"label": {"fi": "Vantaa"}}],
            "countries": ["FI", {"value": "se", "label": {"en": "Sweden"}}],
        },
    }
    job = parse_record(rec)

    assert job.city == "Vantaa"
    assert job.location_raw == "Vantaa, Sweden"
    assert job.country == "SE"


def test_employment_type_maps_only_documented_codes():
    assert employment_type("01", ["01"]) == "full_time, permanent"
    assert employment_type("02", ["02", "0201"]) == "part_time, fixed_term, seasonal"
    assert employment_type("01", "0202") == "full_time, summer_job"
    assert employment_type("01", ["01", "01"]) == "full_time, permanent"  # deduplicated
    assert employment_type("07", ["07"]) is None  # unknown codes are never given a label
    assert employment_type(None, {"koodi": "01"}) is None


def test_pick_lang_tolerates_odd_shapes():
    assert pick_lang({"fi": "Otsikko", "en": "Title"}) == ("Title", "en")
    assert pick_lang({"ru": "Заголовок"}) == ("Заголовок", "ru")
    assert pick_lang("plain string") == ("plain string", "fi")
    assert pick_lang(None) == (None, "fi")
    assert pick_lang({"en": "  "}) == (None, "fi")


def test_tyomarkkinatori_reads_a_record_that_names_almost_nothing():
    """Office name instead of owner name, unlabelled municipality, country without a code."""
    job = parse_record(
        {
            "id": "sparse-1",
            "title": {"en": "Site Reliability Engineer"},
            "employer": {"ownerOfficeName": "  Nordic Cloud Oy  "},
            "location": {
                "foreignCountry": True,
                "municipalities": [{"label": {}}, {"label": {"en": "Tallinn"}}],
                "countries": [{"value": "   "}],
            },
            "applicationUrl": {"en": "https://apply.example/1"},  # no "values" wrapper
            "tags": ["cloud", "cloud", "", "linux"],
        }
    )
    assert job.company == "Nordic Cloud Oy"
    assert job.location_raw == "Tallinn"  # a foreign posting is never labelled "Finland"
    assert job.country == "EE" and job.city == "Tallinn"
    assert job.tags == ["cloud", "linux"]  # the repeat and the blank are dropped
    assert job.raw["apply_url"] == "https://apply.example/1"


def test_tyomarkkinatori_detail_that_adds_nothing_keeps_the_list_values(make_ctx):
    """An empty detail must not blank the title, company, type or date the list already gave."""
    ctx = make_ctx(
        {
            SEARCH: "tyomarkkinatori.json",
            DETAIL: {"position": {"occupations": [{}], "skills": []}, "owner": {}, "application": {}},
        },
        options={"max_pages": 1, "fetch_details": True, "max_details": 1},
    )
    j = next(iter(Tyomarkkinatori().fetch(ctx)))

    assert j.title == "Ohjelmistoasiantuntija Tietohallinto"
    assert j.company == "HUS Helsingin yliopistollinen sairaala"
    assert j.employment_type == "full_time, permanent"
    assert j.posted_at == datetime(2026, 9, 7, 2, 8, 24, 444000, tzinfo=UTC)  # the list date
    assert j.description is None and j.tags == []
    assert j.raw["application_deadline"] == "2026-09-21T20:59:59Z"


def test_tyomarkkinatori_accepts_a_single_query_string(make_ctx):
    ctx = make_ctx(
        {SEARCH: "tyomarkkinatori.json"},
        options={"queries": "devops", "max_pages": 1, "fetch_details": False},
    )
    list(Tyomarkkinatori().fetch(ctx))

    assert json.loads(ctx.http.calls[0].content)["query"] == "devops"


def _one_page(record: dict, page: int, **envelope) -> dict:
    return {"content": [{**record, "id": f"page-{page}"}], **envelope}


def test_tyomarkkinatori_pages_until_the_budget_runs_out(make_ctx):
    """No ``lastPage`` in the envelope: only ``max_pages`` stops the loop."""
    record = fixture_json("tyomarkkinatori.json")["content"][0]

    def paged(req: httpx.Request) -> httpx.Response:
        page = json.loads(req.content)["paging"]["pageNumber"]
        return httpx.Response(200, json=_one_page(record, page))

    ctx = make_ctx({SEARCH: paged}, options={"max_pages": 2, "fetch_details": False})
    jobs = list(Tyomarkkinatori().fetch(ctx))

    assert [j.source_id for j in jobs] == ["page-0", "page-1"]
    assert len(ctx.http.calls) == 2


def test_tyomarkkinatori_believes_a_boolean_last_page(make_ctx):
    record = fixture_json("tyomarkkinatori.json")["content"][0]
    ctx = make_ctx(
        {SEARCH: _one_page(record, 0, lastPage=True)},
        options={"max_pages": 5, "fetch_details": False},
    )
    assert len(list(Tyomarkkinatori().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1


def test_tyomarkkinatori_stops_on_an_empty_page(make_ctx):
    record = fixture_json("tyomarkkinatori.json")["content"][0]

    def paged(req: httpx.Request) -> httpx.Response:
        page = json.loads(req.content)["paging"]["pageNumber"]
        if page == 0:
            return httpx.Response(200, json=_one_page(record, page))
        return httpx.Response(200, json={"content": []})

    ctx = make_ctx({SEARCH: paged}, options={"max_pages": 4, "fetch_details": False})
    assert len(list(Tyomarkkinatori().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 2


def test_tyomarkkinatori_keeps_going_when_one_detail_breaks(make_ctx):
    """A 500 (or unreadable JSON) costs that job its description and nothing else."""
    ctx = make_ctx(
        {SEARCH: "tyomarkkinatori.json", DETAIL: lambda req: httpx.Response(500, text="boom")},
        options={"max_pages": 1, "fetch_details": True, "max_details": 50},
    )
    jobs = list(Tyomarkkinatori().fetch(ctx))
    assert len(jobs) == 3 and all(j.description is None for j in jobs)
    assert len(ctx.http.calls) == 4  # one search, three details: 500 does not latch off

    ctx = make_ctx(
        {SEARCH: "tyomarkkinatori.json", DETAIL: "<html>not json at all</html>"},
        options={"max_pages": 1, "fetch_details": True, "max_details": 1},
    )
    jobs = list(Tyomarkkinatori().fetch(ctx))
    assert len(jobs) == 3 and jobs[0].description is None
