"""cv.ee adapter.

Fixtures are real, trimmed responses captured on 2026-09-07:

* ``cvee.json``          — ``/api/v1/vacancy-search-service/search?categories[]=INFORMATION_TECHNOLOGY``
  cut down to three real vacancies (long ad bodies truncated) plus one deliberately broken row.
* ``cvee_locations.json`` — ``/api/v1/locations-service/list`` cut down to three towns/counties/countries.
* ``cvee_vacancy.html``   — ``https://cv.ee/en/vacancy/1652906`` with everything but the
  ``__NEXT_DATA__`` blob removed, that blob reduced to the keys the parser reads, and the
  recruiter contact block (name, e-mail, phone) stripped.
"""

from datetime import UTC, datetime

import httpx
from conftest import fixture_json

from jobscraper.sources.cvee import CvEe, parse_detail, parse_record

SEARCH = "vacancy-search-service/search"
LOCATIONS = "locations-service/list"
DETAIL_1 = "/en/vacancy/1652906"
DETAIL_2 = "/en/vacancy/1641665"


def _routes(**extra):
    """Search + locations + a detail page that works and one that 500s."""
    routes = {
        SEARCH: "cvee.json",
        LOCATIONS: "cvee_locations.json",
        DETAIL_1: "cvee_vacancy.html",
        DETAIL_2: lambda req: httpx.Response(500, text="boom"),
    }
    routes.update(extra)
    return routes


def _urls(ctx, needle):
    return [str(c.url) for c in ctx.http.calls if needle in str(c.url)]


def _detail_urls(ctx):
    return _urls(ctx, "/en/vacancy/")


def test_cvee_parses_fixture(make_ctx):
    ctx = make_ctx(_routes(), options={"max_pages": 1})
    jobs = list(CvEe().fetch(ctx))

    # 4 rows in, 3 out: the row with no id/title is skipped, not raised.
    assert len(jobs) == 3
    j = jobs[0]
    assert j.source == "cvee"
    assert j.source_id == "1652906"
    assert j.url == "https://cv.ee/en/vacancy/1652906"
    assert j.title == "Küberturbe nõunik (Cybersecurity Advisor)"
    assert j.company == "Security Software OÜ"
    assert j.country == "EE"
    assert j.city == "Tallinn"  # townId 312 resolved through the locations list
    assert j.location_raw == "Tallinn, Harjumaa, Estonia"
    assert j.remote == "hybrid"
    assert j.salary_text is None
    assert j.tags == []
    assert j.posted_at == datetime(2026, 9, 7, 13, 19, 48, 680000, tzinfo=UTC)
    # The search response already carries the whole ad body as plain text.
    assert j.description is not None
    assert j.description.startswith("Otsime oma Eesti turvanõustamise meeskonda")
    assert "<" not in j.description

    second = jobs[1]
    assert second.source_id == "1641665"
    assert second.title == "Java Developer"
    assert second.company == "Playtech Estonia OÜ"
    assert second.city == "Tartu"
    assert second.location_raw == "Tartu, Tartumaa, Estonia"
    assert second.salary_text == "2970 EUR"
    assert second.tags == ["Java", "spring"]
    assert second.description.startswith("Your influential mission.")

    third = jobs[2]
    assert third.source_id == "1646695"
    assert third.remote == "remote"  # remoteWorkType FULLY_REMOTE
    assert third.city is None  # townId is null, but the county still places it
    assert third.country == "EE"
    assert third.location_raw == "Harjumaa, Estonia"
    assert third.description is None  # a picture-only ad: the API returns a blank body


def test_cvee_search_uses_bracketed_category_param(make_ctx):
    """`categories[]` is the form the API filters on; a plain `categories=` is ignored."""
    ctx = make_ctx(_routes(), options={"max_pages": 1})
    list(CvEe().fetch(ctx))
    search = _urls(ctx, SEARCH)
    assert len(search) == 1
    assert "categories%5B%5D=INFORMATION_TECHNOLOGY" in search[0]
    assert "categories=" not in search[0]
    assert "sorting=LATEST" in search[0]


def test_cvee_ignores_the_keywords_option(make_ctx):
    """With a category set the API returns the same rows whatever `keywords` says.

    Verified live: `categories[]=INFORMATION_TECHNOLOGY` returns the same 175 ids with
    `keywords=developer`, with `keywords=kokk` and with no keyword at all. So a list of
    keywords must not turn into a request per keyword.
    """
    ctx = make_ctx(_routes(), options={"keywords": ["developer", "engineer", "intern"], "max_pages": 1})
    jobs = list(CvEe().fetch(ctx))
    assert len(jobs) == 3
    search = _urls(ctx, SEARCH)
    assert len(search) == 1
    assert "keywords" not in search[0]


def test_cvee_fetches_locations_once(make_ctx):
    ctx = make_ctx(
        _routes(),
        options={"categories": ["INFORMATION_TECHNOLOGY", "TELECOMMUNICATION"], "max_pages": 1},
    )
    list(CvEe().fetch(ctx))
    assert len(_urls(ctx, LOCATIONS)) == 1
    assert len(_urls(ctx, SEARCH)) == 2  # one request per category


def test_cvee_survives_a_broken_locations_endpoint(make_ctx):
    ctx = make_ctx(
        _routes(**{LOCATIONS: lambda req: httpx.Response(503, text="nope")}),
        options={"max_pages": 1},
    )
    jobs = list(CvEe().fetch(ctx))
    assert len(jobs) == 3
    assert jobs[0].city is None  # no id→name map, so no city …
    assert jobs[0].country == "EE"  # … but the board is Estonian
    assert jobs[0].location_raw is None


def test_cvee_detail_pages_are_opt_in(make_ctx):
    """Detail pages are ~700 KB each and only add the section headings, so they stay off."""
    ctx = make_ctx(_routes(), options={"max_pages": 1})
    list(CvEe().fetch(ctx))
    assert _detail_urls(ctx) == []


def test_cvee_detail_adds_section_headings(make_ctx):
    ctx = make_ctx(_routes(), options={"max_pages": 1, "fetch_details": True})
    jobs = list(CvEe().fetch(ctx))
    assert _detail_urls(ctx)[0] == "https://cv.ee/en/vacancy/1652906"
    body = jobs[0].description
    assert body.startswith("Rollist")  # standardDetails[].title
    assert "Tööülesanded" in body
    assert "Keda me otsime?" in body
    assert "Otsime oma Eesti turvanõustamise meeskonda" in body
    assert "Sporditoetus" in body  # highlights.additionalBenefits
    assert "<" not in body  # HTML stripped


def test_cvee_max_details_caps_detail_fetches(make_ctx):
    ctx = make_ctx(_routes(), options={"max_pages": 1, "fetch_details": True, "max_details": 1})
    jobs = list(CvEe().fetch(ctx))
    assert _detail_urls(ctx) == ["https://cv.ee/en/vacancy/1652906"]
    # Past the cap the list body is used unchanged.
    assert jobs[1].description.startswith("Your influential mission.")


def test_cvee_detail_failure_keeps_the_job(make_ctx):
    ctx = make_ctx(_routes(), options={"max_pages": 1, "fetch_details": True})
    jobs = list(CvEe().fetch(ctx))
    assert DETAIL_2 in " ".join(_detail_urls(ctx))  # it was tried …
    assert [j.source_id for j in jobs] == ["1652906", "1641665", "1646695"]  # … and 500ed
    assert jobs[1].description.startswith("Your influential mission.")  # list body survives


def test_cvee_drops_mediated_offers():
    """cv.ee resells agency ads under the employer name "Vahendatud pakkumised".

    No such row was on the board when the fixtures were captured (0 of 1332 vacancies), so
    this row is synthetic — the shape is copied from a real one, only the employer differs.
    """
    row = {"id": 1650102, "positionTitle": "Tarkvaraarendaja", "employerId": 60,
           "employerName": "Vahendatud pakkumised", "townId": 312, "countyId": 67, "countryId": 1}
    assert parse_record(row) is None


def test_cvee_paginates_until_total(make_ctx):
    pages = {"n": 0}

    def search(req: httpx.Request) -> httpx.Response:
        pages["n"] += 1
        offset = int(dict(req.url.params).get("offset", 0))
        return httpx.Response(
            200,
            json={
                "total": 4,
                "vacancies": [
                    {"id": 1000 + offset + i, "positionTitle": f"Dev {offset + i}",
                     "employerName": "Example OÜ", "countryId": 1, "townId": 314}
                    for i in range(2)
                ],
            },
        )

    ctx = make_ctx(_routes(**{SEARCH: search}), options={"page_size": 2, "max_pages": 5})
    jobs = list(CvEe().fetch(ctx))
    assert [j.source_id for j in jobs] == ["1000", "1001", "1002", "1003"]
    assert pages["n"] == 2  # stops once offset reaches `total`
    assert jobs[0].city == "Tartu"


def test_cvee_respects_limit(make_ctx):
    ctx = make_ctx(_routes(), options={"max_pages": 3}, limit=1)
    jobs = list(CvEe().fetch(ctx))
    assert len(jobs) == 1
    assert len(_urls(ctx, SEARCH)) == 1


def _next_data(payload: str) -> str:
    return f'<script id="__NEXT_DATA__" type="application/json">{payload}</script>'


def test_cvee_parse_detail_on_a_page_without_usable_next_data():
    assert parse_detail("<html><body>maintenance</body></html>", 1652906) is None
    assert parse_detail(_next_data("{oops"), 1) is None
    # A picture-only ad: the page renders, but there is no text to take from it.
    assert parse_detail(
        _next_data(
            '{"props":{"pageProps":{"vacancy":{"7":{"details":{"standardDetails":['
            'null,{"title":"Tööülesanded","content":"  "}]},'
            '"highlights":{"additionalBenefits":null}}}}}}'
        ),
        7,
    ) is None


def test_cvee_detail_without_a_body_keeps_the_list_body(make_ctx):
    """A picture ad's page parses fine but yields no text; the search body stands."""
    ctx = make_ctx(
        _routes(**{DETAIL_1: _next_data('{"props":{"pageProps":{"vacancy":{}}}}')}),
        options={"max_pages": 1, "fetch_details": True, "max_details": 1},
    )
    jobs = list(CvEe().fetch(ctx))
    assert _detail_urls(ctx) == ["https://cv.ee/en/vacancy/1652906"]
    assert jobs[0].description.startswith("Otsime oma Eesti turvanõustamise meeskonda")


def test_cvee_accepts_a_single_category_as_a_string(make_ctx):
    ctx = make_ctx(_routes(), options={"categories": "INFORMATION_TECHNOLOGY", "max_pages": 1})
    assert len(list(CvEe().fetch(ctx))) == 3
    assert len(_urls(ctx, SEARCH)) == 1


def test_cvee_stops_on_an_empty_page(make_ctx):
    """A page with no `vacancies` array ends the category instead of looping `max_pages` times."""
    ctx = make_ctx(
        _routes(**{SEARCH: lambda req: httpx.Response(200, json={"total": 99, "vacancies": []})}),
        options={"max_pages": 5},
    )
    assert list(CvEe().fetch(ctx)) == []
    assert len(_urls(ctx, SEARCH)) == 1


def test_cvee_stops_on_a_short_page(make_ctx):
    """`total` sometimes overshoots what the API will actually hand out; a short page ends it."""
    overshooting = dict(fixture_json("cvee.json"), total=9999)
    ctx = make_ctx(
        _routes(**{SEARCH: lambda req: httpx.Response(200, json=overshooting)}),
        options={"page_size": 250, "max_pages": 5},
    )
    assert len(list(CvEe().fetch(ctx))) == 3
    assert len(_urls(ctx, SEARCH)) == 1
