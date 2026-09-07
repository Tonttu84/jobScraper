from datetime import UTC, datetime

import httpx

from jobscraper.sources.tyomarkkinatori import Tyomarkkinatori, pick_lang

SEARCH = "search/v2/search"
DETAIL = "public/jobpostings/"


def test_tyomarkkinatori_parses_list_fixture(make_ctx):
    ctx = make_ctx(
        {SEARCH: "tyomarkkinatori.json"},
        options={"queries": ["developer"], "max_pages": 1, "fetch_details": False},
    )
    jobs = list(Tyomarkkinatori().fetch(ctx))

    assert len(jobs) == 3  # the id-less record is skipped, not raised
    j = jobs[0]
    assert j.source == "tyomarkkinatori"
    assert j.source_id == "TE-1234567"
    assert j.title == "Software Developer"  # English preferred over Finnish
    assert j.company == "Example Company Ltd"
    assert j.url == "https://tyomarkkinatori.fi/henkiloasiakkaat/avoimet-tyopaikat/TE-1234567/en"
    assert j.location_raw == "Helsinki, Espoo, Finland"
    assert j.country == "FI" and j.city == "Helsinki"
    assert j.posted_at == datetime(2026, 9, 2, 6, 0, tzinfo=UTC)
    assert j.description is None  # the list carries no body

    # Plain-string multilingual values and an empty ownerName map both survive.
    assert jobs[1].company == "Kotikonttori Oy"
    assert jobs[1].url.endswith("/TE-7654321/fi")
    assert jobs[1].remote == "remote"
    assert jobs[2].company == "Tallinn branch"
    assert jobs[2].country == "EE"  # foreignCountry → resolved from the municipality label


def test_tyomarkkinatori_hydrates_descriptions(make_ctx):
    ctx = make_ctx(
        {SEARCH: "tyomarkkinatori.json", DETAIL: "tyomarkkinatori_detail.json"},
        options={"max_pages": 1, "fetch_details": True, "max_details": 1},
    )
    jobs = list(Tyomarkkinatori().fetch(ctx))

    assert len(jobs) == 3
    assert jobs[0].title == "Software Developer (Python)"  # detail wins over the list
    assert "FastAPI" in jobs[0].description and "<b>" not in jobs[0].description
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


def test_tyomarkkinatori_respects_limit(make_ctx):
    ctx = make_ctx(
        {SEARCH: "tyomarkkinatori.json"},
        options={"max_pages": 3, "fetch_details": False},
        limit=2,
    )
    assert len(list(Tyomarkkinatori().fetch(ctx))) == 2
    assert len(ctx.http.calls) == 1


def test_pick_lang_tolerates_odd_shapes():
    assert pick_lang({"fi": "Otsikko", "en": "Title"}) == ("Title", "en")
    assert pick_lang({"ru": "Заголовок"}) == ("Заголовок", "ru")
    assert pick_lang("plain string") == ("plain string", "fi")
    assert pick_lang(None) == (None, "fi")
    assert pick_lang({"en": "  "}) == (None, "fi")
