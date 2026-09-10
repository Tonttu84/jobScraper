"""Fixtures follow the payload shape observed on jobs.careers.microsoft.com (see the module
docstring of ``jobscraper.sources.microsoft``); they are synthetic, not captured responses."""

from datetime import UTC, datetime

import httpx
import pytest

from jobscraper.http import SourceHTTPError
from jobscraper.sources.microsoft import (
    Microsoft,
    detail_description,
    parse_record,
    work_site_remote,
)
from tests.conftest import fixture_text

SEARCH = "/search/api/v1/search"
DETAIL = "/search/api/v1/job/"
EMPTY_PAGE = '{"operationResult": {"result": {"totalJobs": 3, "jobs": []}}}'


def search_pages(*names: str):
    """Serve one fixture per ``pg`` value so a test can watch the adapter paginate."""

    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params.get("pg", "1"))
        if page > len(names):
            return httpx.Response(200, text=EMPTY_PAGE)
        return httpx.Response(200, text=fixture_text(names[page - 1]))

    return handler


ROUTES = {
    SEARCH: search_pages("microsoft_search.json", "microsoft_search_p2.json"),
    DETAIL: "microsoft_job.json",
}
ONE_QUERY = {"queries": ["software engineer"], "locations": ["Finland", "Germany"]}


def pages_requested(ctx) -> list[str]:
    return [c.url.params["pg"] for c in ctx.http.calls if SEARCH in str(c.url)]


def test_microsoft_paginates_and_parses_the_search_payload(make_ctx):
    ctx = make_ctx(ROUTES, options={**ONE_QUERY, "max_pages": 5, "fetch_details": False})
    jobs = list(Microsoft().fetch(ctx))

    # ``totalJobs`` is 3 and page 1 carries 2 records, so page 2 is fetched and page 3 is not.
    assert pages_requested(ctx) == ["1", "2"]
    assert len(jobs) == 3  # the record without a jobId is skipped, not raised

    j = jobs[0]
    assert j.source == "microsoft"
    assert j.source_id == "1790123"
    assert j.title == "Software Engineer"
    assert j.company == "Microsoft"
    assert j.url == "https://jobs.careers.microsoft.com/global/en/job/1790123/"
    assert j.location_raw == "Helsinki, Uusimaa, Finland"
    assert j.country == "FI" and j.city == "Helsinki"
    assert j.remote == "hybrid"  # "Up to 50% work from home"
    assert j.employment_type == "Full-Time"
    assert j.seniority_raw == "Students and graduates"  # the searched ``exp`` value
    assert j.tags == ["Software Engineering"]  # profession == discipline, kept once
    assert j.posted_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert j.description == "Come help us build the next generation of cloud storage from Helsinki."
    assert j.raw["jobId"] == "1790123"


def test_microsoft_work_site_flexibility_decides_the_remote_kind(make_ctx):
    ctx = make_ctx(ROUTES, options={**ONE_QUERY, "fetch_details": False})
    jobs = list(Microsoft().fetch(ctx))

    de = jobs[1]
    assert de.title == "Software Engineer II - Azure Storage"
    assert de.remote == "remote"  # "Up to 100% work from home"
    assert de.country == "DE" and de.city == "Berlin"
    assert de.tags == ["Software Engineering", "Cloud & AI"]

    nl = jobs[2]
    assert nl.source_id == "1790789"
    assert nl.remote == "onsite"  # "Microsoft on-site only"
    assert nl.country == "NL" and nl.city == "Amsterdam"
    assert nl.employment_type == "Internship"
    assert nl.posted_at == datetime(2026, 9, 5, tzinfo=UTC)


def test_work_site_remote_helper():
    assert work_site_remote(None) is None
    assert work_site_remote("") is None
    assert work_site_remote("Up to 100% work from home") == "remote"
    assert work_site_remote("100% work from home") == "remote"
    assert work_site_remote("Up to 50% work from home") == "hybrid"
    assert work_site_remote("Up to 0% work from home") == "onsite"
    assert work_site_remote("Microsoft on-site only") == "onsite"
    assert work_site_remote("Hybrid working") == "hybrid"
    assert work_site_remote("Fully remote") == "remote"
    assert work_site_remote("Flexible") is None  # unknown wording: let guess_remote decide


def test_microsoft_falls_back_to_the_title_when_flexibility_is_unknown(make_ctx):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"operationResult": {"result": {"totalJobs": 1, "jobs": [
            {"jobId": "42", "title": "Remote Software Engineer",
             "properties": {"workSiteFlexibility": "Flexible", "primaryLocation": "Dublin, Dublin, Ireland"}},
        ]}}})

    ctx = make_ctx({SEARCH: handler}, options={**ONE_QUERY, "fetch_details": False})
    job = next(iter(Microsoft().fetch(ctx)))
    assert job.remote == "remote" and job.country == "IE"


def test_microsoft_search_request_shape(make_ctx):
    ctx = make_ctx(
        ROUTES,
        options={
            "queries": ["software engineer"],
            "locations": ["Finland", "Estonia", "Germany"],
            "experience": ["Students and graduates"],
            "profession": "Software Engineering",
            "max_pages": 1,
            "fetch_details": False,
        },
    )
    list(Microsoft().fetch(ctx))
    url = ctx.http.calls[0].url
    # One request per (query, experience) with *every* location as a repeated ``lc`` param.
    assert url.params.get_list("lc") == ["Finland", "Estonia", "Germany"]
    assert url.params["q"] == "software engineer"
    assert url.params["exp"] == "Students and graduates"
    assert url.params["p"] == "Software Engineering"
    assert url.params["pg"] == "1" and url.params["pgSz"] == "20"
    assert url.params["l"] == "en_us" and url.params["o"] == "Recent" and url.params["flt"] == "true"


def test_microsoft_omits_experience_and_profession_when_unset(make_ctx):
    ctx = make_ctx(
        ROUTES,
        options={**ONE_QUERY, "experience": [], "profession": None, "max_pages": 1, "fetch_details": False},
    )
    jobs = list(Microsoft().fetch(ctx))
    url = ctx.http.calls[0].url
    assert "exp" not in url.params and "p" not in url.params
    assert url.params["q"] == "software engineer"
    # Without a searched experience level the job's own ``jobType`` is the seniority label.
    assert jobs[0].seniority_raw == "Individual Contributor"


def test_microsoft_merges_the_detail_text(make_ctx):
    ctx = make_ctx(ROUTES, options={**ONE_QUERY, "max_pages": 1})
    job = next(iter(Microsoft().fetch(ctx)))
    desc = job.description
    assert "The Azure Storage team in Helsinki" in desc  # description
    assert "Design, build and ship features end to end" in desc  # responsibilities
    assert "Bachelor's Degree in Computer Science" in desc  # qualifications
    assert "<" not in desc and "&#39;" not in desc and "&amp;" not in desc
    detail_call = next(c for c in ctx.http.calls if DETAIL in str(c.url))
    assert str(detail_call.url).endswith("/search/api/v1/job/1790123?lang=en_us")


def test_microsoft_detail_failure_falls_back_to_the_search_description(make_ctx):
    routes = {SEARCH: ROUTES[SEARCH], DETAIL: lambda req: httpx.Response(500, text="boom")}
    ctx = make_ctx(routes, options={**ONE_QUERY, "max_pages": 1})
    jobs = list(Microsoft().fetch(ctx))
    assert len(jobs) == 2  # a broken detail never drops a job
    assert jobs[0].description == "Come help us build the next generation of cloud storage from Helsinki."


def test_microsoft_caps_detail_requests(make_ctx):
    ctx = make_ctx(ROUTES, options={**ONE_QUERY, "max_details": 1})
    jobs = list(Microsoft().fetch(ctx))
    assert len([c for c in ctx.http.calls if DETAIL in str(c.url)]) == 1
    assert "The Azure Storage team" in jobs[0].description
    assert jobs[1].description == "Azure Storage is hiring across Germany."


def test_microsoft_dedupes_across_queries(make_ctx):
    ctx = make_ctx(
        ROUTES,
        options={"queries": ["software engineer", "software developer"], "locations": ["Finland"],
                 "max_pages": 5, "fetch_details": False},
    )
    jobs = list(Microsoft().fetch(ctx))
    assert [j.source_id for j in jobs] == ["1790123", "1790456", "1790789"]
    # Both queries are searched (2 pages each); the second one adds no new jobIds.
    queries = [c.url.params["q"] for c in ctx.http.calls if SEARCH in str(c.url)]
    assert queries == ["software engineer"] * 2 + ["software developer"] * 2


def test_microsoft_respects_limit(make_ctx):
    ctx = make_ctx(ROUTES, options={**ONE_QUERY, "max_pages": 5}, limit=1)
    assert len(list(Microsoft().fetch(ctx))) == 1
    assert len(pages_requested(ctx)) == 1


def test_microsoft_stops_on_an_empty_page(make_ctx):
    ctx = make_ctx({SEARCH: EMPTY_PAGE}, options={**ONE_QUERY, "max_pages": 5, "fetch_details": False})
    assert list(Microsoft().fetch(ctx)) == []
    assert pages_requested(ctx) == ["1"]


def test_microsoft_raises_when_the_search_payload_is_not_an_object(make_ctx):
    ctx = make_ctx({SEARCH: lambda req: httpx.Response(200, json=["nope"])}, options=ONE_QUERY)
    with pytest.raises(SourceHTTPError):
        list(Microsoft().fetch(ctx))


def test_microsoft_search_http_error_raises(make_ctx):
    ctx = make_ctx({SEARCH: lambda req: httpx.Response(503, text="down")}, options=ONE_QUERY)
    with pytest.raises(SourceHTTPError):
        list(Microsoft().fetch(ctx))


def test_microsoft_tolerates_a_missing_operation_result_wrapper(make_ctx):
    """If the envelope is ever dropped, the bare result object still parses."""
    payload = {"totalJobs": 1, "jobs": [{"jobId": "7", "title": "Software Engineer",
                                         "properties": {"primaryLocation": "Tallinn, Harjumaa, Estonia"}}]}
    ctx = make_ctx({SEARCH: lambda req: httpx.Response(200, json=payload)},
                   options={**ONE_QUERY, "fetch_details": False})
    job = next(iter(Microsoft().fetch(ctx)))
    assert job.source_id == "7" and job.country == "EE" and job.city == "Tallinn"
    assert job.posted_at is None and job.description is None


def test_microsoft_search_without_query_or_locations(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": [], "locations": [], "max_pages": 1, "fetch_details": False})
    jobs = list(Microsoft().fetch(ctx))
    url = ctx.http.calls[0].url
    assert "q" not in url.params and "lc" not in url.params
    assert len(jobs) == 2


def test_microsoft_accepts_bare_string_options(make_ctx):
    """``queries: software engineer`` in YAML is a string, not a one-item list."""
    ctx = make_ctx(ROUTES, options={"queries": "software engineer", "locations": "Finland",
                                    "max_pages": 1, "fetch_details": False})
    list(Microsoft().fetch(ctx))
    url = ctx.http.calls[0].url
    assert url.params["q"] == "software engineer" and url.params.get_list("lc") == ["Finland"]


def test_microsoft_empty_detail_keeps_the_search_description(make_ctx):
    routes = {SEARCH: ROUTES[SEARCH], DETAIL: {"operationResult": {"result": {"jobId": "1790123"}}}}
    ctx = make_ctx(routes, options={**ONE_QUERY, "max_pages": 1})
    job = next(iter(Microsoft().fetch(ctx)))
    assert job.description == "Come help us build the next generation of cloud storage from Helsinki."
    assert "detail" not in job.raw


def test_microsoft_parse_record_skips_junk():
    assert parse_record("nope") is None
    assert parse_record({"title": "No jobId"}) is None
    assert parse_record({"jobId": "1"}) is None


def test_detail_description_tolerates_junk():
    assert detail_description(None) is None
    assert detail_description({"operationResult": {"result": {}}}) is None
    assert detail_description({"description": "<p>Only a description.</p>"}).strip() == "Only a description."
