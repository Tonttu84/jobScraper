"""jobly.fi — listing discovery through the headless browser + JSON-LD detail parsing.

The site could not be observed from the sandbox (no egress to job boards), so these tests pin
the *defensive* behaviour: a Drupal-ish search page, a posting with JSON-LD, a posting without
it, and the promise that ``mode: browser`` never touches the plain HTTP client.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit

import pytest
from conftest import fixture_text

from jobscraper.sources.jobly import (
    Jobly,
    parse_detail,
    parse_listing_html,
    salary_text,
)

EET = timezone(timedelta(hours=3))
LISTING = "/tyopaikat"
DETAIL_LD = "https://www.jobly.fi/tyopaikka/ohjelmistokehittaja-helsinki-12345"
DETAIL_PLAIN = "https://www.jobly.fi/tyopaikka/junior-developer-espoo-12346"

ROUTES = {
    "12346": "jobly_detail_nojsonld.html",
    "/tyopaikka/": "jobly_detail.html",
    "/tyopaikat": "jobly_search.html",
}


def listing_calls(ctx) -> list[str]:
    return [u for u in ctx.browser().calls if LISTING in u]


def detail_calls(ctx) -> list[str]:
    return [u for u in ctx.browser().calls if "/tyopaikka/" in u]


def query_of(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query))


def paged_listing(url: str) -> str:
    """A search page whose links differ per page, so paging is observable."""
    page = int(query_of(url).get("page", 0))
    rows = "".join(
        f'<div class="views-row"><h2><a href="/tyopaikka/dev-{page}-{i}">Dev {page}-{i}</a></h2></div>'
        for i in range(2)
    )
    return f'<html><body><div class="view-content">{rows}</div></body></html>'


# ------------------------------------------------------------------ listing parsing


def test_listing_parsing_keeps_job_links_only_absolute_and_deduped():
    urls = parse_listing_html(fixture_text("jobly_search.html"))
    assert urls == [
        DETAIL_LD,
        DETAIL_PLAIN,
        "https://www.jobly.fi/tyopaikka/full-stack-developer-tampere-12347",
    ]
    # the company page and the pager link are not postings
    assert not any("/yritykset/" in u or "/tyopaikat" in u for u in urls)


def test_listing_parsing_of_an_empty_page():
    assert parse_listing_html("<html><body><p>Ei tuloksia</p></body></html>") == []
    assert parse_listing_html("") == []


# ------------------------------------------------------------------ detail parsing


def test_detail_from_json_ld():
    job = parse_detail(fixture_text("jobly_detail.html"), DETAIL_LD)
    assert job.source == "jobly"
    assert job.source_id == "ohjelmistokehittaja-helsinki-12345"
    assert job.url == DETAIL_LD
    assert job.title == "Ohjelmistokehittäjä (Junior)"
    assert job.company == "Acme Software Oy"
    assert job.country == "FI" and job.city == "Helsinki"
    assert job.location_raw == "Helsinki"
    assert job.remote == "remote"  # "etätyönä" in the description
    assert job.employment_type == "FULL_TIME"
    assert job.salary_text == "3200 - 4000 EUR / MONTH"
    assert job.posted_at == datetime(2026, 9, 5, 9, 0, tzinfo=EET)
    assert "junior-ohjelmistokehittäjää" in job.description
    assert "<strong>" not in job.description  # HTML stripped
    assert job.raw["jsonld"]["validThrough"] == "2026-10-05T23:59:59+03:00"


def test_detail_without_json_ld_falls_back_to_the_markup():
    job = parse_detail(fixture_text("jobly_detail_nojsonld.html"), DETAIL_PLAIN)
    assert job.source_id == "junior-developer-espoo-12346"
    assert job.title == "Junior Developer"
    assert job.company == "Beta Consulting Oy"  # class contains "company"
    assert job.country == "FI"
    assert job.remote == "hybrid"
    assert job.posted_at is None
    assert job.salary_text is None
    assert "junior developer to join our platform team" in job.description
    assert job.raw["jsonld"] is None


def test_a_page_without_a_title_is_skipped():
    assert parse_detail("<html><body><p>404</p></body></html>", DETAIL_LD) is None


def test_off_site_and_unparseable_json_ld_are_ignored():
    html = """
    <html><body>
      <script type="application/ld+json">{not json at all}</script>
      <a href="https://www.linkedin.com/tyopaikka/ei-meidan-12">elsewhere</a>
      <a href="/tyopaikka/oikea-13">ours</a>
    </body></html>
    """
    assert parse_listing_html(html) == ["https://www.jobly.fi/tyopaikka/oikea-13"]
    job = parse_detail(html, "https://www.jobly.fi/tyopaikka/oikea-13")
    assert job is None  # no JSON-LD, no <h1>


def test_json_ld_inside_a_graph_with_a_list_type_is_found():
    """Some Drupal modules publish one ``@graph`` with ``"@type": ["JobPosting", "Thing"]``."""
    html = """
    <html><body><h1>ignored</h1>
    <script type="application/ld+json">
    {"@context": "https://schema.org", "@graph": [
      {"@type": "WebPage", "name": "x"},
      {"@type": ["JobPosting", "Thing"], "title": "Backend Developer",
       "employmentType": ["FULL_TIME", "PART_TIME"],
       "hiringOrganization": [{"@type": "Organization", "name": "Delta Oy"}],
       "jobLocation": [{"@type": "Place", "address": {"addressRegion": "Oulu",
                        "addressCountry": {"name": "Finland"}}}],
       "jobLocationType": "TELECOMMUTE",
       "description": "Kotoa käsin."}
    ]}
    </script></body></html>
    """
    job = parse_detail(html, "https://www.jobly.fi/tyopaikka/backend-oulu-1")
    assert job.title == "Backend Developer"
    assert job.company == "Delta Oy"
    assert job.employment_type == "FULL_TIME, PART_TIME"
    assert (job.country, job.city, job.location_raw) == ("FI", "Oulu", "Oulu")
    assert job.remote == "remote"  # jobLocationType, not a word in the text


def test_the_markup_fallbacks_take_what_they_can_find():
    html = """
    <html><body>
      <h1>  Trainee  </h1>
      <span class="posting-organization">Epsilon Oy</span>
      <span class="job-sijainti">Turku, Suomi</span>
      <article><p>Harjoittelupaikka kesäksi.</p></article>
    </body></html>
    """
    job = parse_detail(html, "https://www.jobly.fi/tyopaikka/trainee-turku-2/")
    assert job.source_id == "trainee-turku-2"  # trailing slash does not become the id
    assert job.title == "Trainee"
    assert job.company == "Epsilon Oy"  # class contains "organization"
    assert job.location_raw == "Turku, Suomi" and job.city == "Turku"
    assert "Harjoittelupaikka" in job.description


@pytest.mark.parametrize(
    ("base_salary", "expected"),
    [
        ("2500 e/kk", "2500 e/kk"),
        ({"currency": "EUR", "value": 3000}, "3000 EUR"),
        ({"currency": "EUR", "value": {"value": 3500.5, "unitText": "MONTH"}}, "3500.5 EUR / MONTH"),
        ({"currency": "EUR", "value": {"minValue": 3000, "maxValue": 3000}}, "3000 EUR"),
        ({"value": {"maxValue": 4000}}, "4000"),
        ({"salaryCurrency": "EUR"}, None),
        ({"value": {}}, None),
        (None, None),
        ([], None),
    ],
)
def test_salary_text_variants(base_salary, expected):
    assert salary_text(base_salary) == expected


# ------------------------------------------------------------------ fetch


def test_fetch_uses_the_browser_and_never_the_plain_client(make_ctx):
    # FakeHttp with no routes raises on any request, so a stray ctx.http.get would fail here.
    ctx = make_ctx({}, options={"queries": ["software developer"], "max_pages": 1}, browser_routes=ROUTES)
    jobs = list(Jobly().fetch(ctx))

    assert ctx.http.calls == []
    assert [j.source_id for j in jobs] == [
        "ohjelmistokehittaja-helsinki-12345",
        "junior-developer-espoo-12346",
        "full-stack-developer-tampere-12347",
    ]
    assert jobs[1].title == "Junior Developer"  # the no-JSON-LD template
    listing = listing_calls(ctx)
    assert len(listing) == 1
    assert query_of(listing[0]) == {"search": "software developer", "page": "0"}
    assert ctx.browser_calls == len(jobs) + 1


def test_links_are_deduped_across_queries_and_pages(make_ctx):
    ctx = make_ctx(
        {},
        options={"queries": ["a", "b"], "max_pages": 3},
        browser_routes=ROUTES,
    )
    jobs = list(Jobly().fetch(ctx))

    assert len(jobs) == 3
    assert len(detail_calls(ctx)) == 3  # each posting fetched once, for both queries together
    # page 1 of each query repeats page 0's links, so paging stops there
    assert [query_of(u)["page"] for u in listing_calls(ctx)] == ["0", "1", "0"]


def test_max_pages_caps_the_listing_requests(make_ctx):
    routes = {"/tyopaikat": paged_listing, "/tyopaikka/": "jobly_detail.html"}
    ctx = make_ctx({}, options={"queries": ["a"], "max_pages": 2}, browser_routes=routes)
    jobs = list(Jobly().fetch(ctx))

    assert [query_of(u)["page"] for u in listing_calls(ctx)] == ["0", "1"]
    assert [j.source_id for j in jobs] == ["dev-0-0", "dev-0-1", "dev-1-0", "dev-1-1"]


def test_max_details_caps_the_detail_requests(make_ctx):
    routes = {"/tyopaikat": paged_listing, "/tyopaikka/": "jobly_detail.html"}
    ctx = make_ctx(
        {}, options={"queries": ["a"], "max_pages": 5, "max_details": 3}, browser_routes=routes
    )
    jobs = list(Jobly().fetch(ctx))

    assert len(jobs) == 3
    assert len(detail_calls(ctx)) == 3
    assert len(listing_calls(ctx)) == 2  # stopped as soon as 3 links were known


def test_ctx_limit_stops_early(make_ctx):
    routes = {"/tyopaikat": paged_listing, "/tyopaikka/": "jobly_detail.html"}
    ctx = make_ctx({}, options={"queries": ["a"], "max_pages": 5}, limit=2, browser_routes=routes)
    jobs = list(Jobly().fetch(ctx))

    assert len(jobs) == 2
    assert len(detail_calls(ctx)) == 2  # no posting is fetched just to be thrown away
    assert len(listing_calls(ctx)) == 1


def test_a_broken_posting_is_skipped_not_fatal(make_ctx):
    def detail(url: str) -> str:
        if "12346" in url:
            return "<html><body>gone</body></html>"  # no title at all
        return fixture_text("jobly_detail.html")

    ctx = make_ctx(
        {}, options={"queries": ["a"], "max_pages": 1}, browser_routes={"/tyopaikat": "jobly_search.html", "/tyopaikka/": detail}
    )
    jobs = list(Jobly().fetch(ctx))
    assert len(jobs) == 2  # the titleless page yielded nothing, the other two came through


def test_a_search_page_without_links_is_reported_not_crashed(make_ctx, caplog):
    ctx = make_ctx(
        {},
        options={"queries": "software developer", "max_pages": 2},  # a bare string is one query
        browser_routes={"/tyopaikat": "<html><body>Ei tuloksia</body></html>"},
    )
    with caplog.at_level("WARNING"):
        assert list(Jobly().fetch(ctx)) == []
    assert len(listing_calls(ctx)) == 1  # no point asking for page 1
    assert any("no /tyopaikka/ links" in r.message for r in caplog.records)


def test_http_mode_uses_the_plain_client(make_ctx):
    """``mode: http`` (a machine that cannot run Chromium) still works against plain HTML."""
    ctx = make_ctx(
        {"/tyopaikka/": "jobly_detail.html", "/tyopaikat": "jobly_search.html"},
        options={"queries": ["a"], "max_pages": 1, "mode": "http"},
        browser_routes=ROUTES,
    )
    jobs = list(Jobly().fetch(ctx))
    assert len(jobs) == 3
    assert ctx.browser().calls == []
    assert ctx.browser_calls == 0
