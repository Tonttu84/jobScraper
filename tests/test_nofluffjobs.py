from datetime import UTC, datetime

import httpx

from jobscraper.sources.nofluffjobs import NoFluffJobs, build_description, salary_text
from tests.conftest import fixture_json, fixture_text

ROUTES = {
    ("POST", "/api/search/posting"): "nofluffjobs.json",
    ("GET", "/api/posting/"): "nofluffjobs_detail.json",
}


def test_nofluffjobs_parses_fixture(make_ctx):
    ctx = make_ctx(ROUTES, options={"regions": ["pl"], "max_pages": 1})
    jobs = list(NoFluffJobs().fetch(ctx))
    assert len(jobs) == 2  # the broken posting is skipped, not raised
    j = jobs[0]
    assert j.source == "nofluffjobs"
    assert j.source_id == "abcd1234"
    assert j.title == "Junior Python Developer"
    assert j.company == "Acme Software"
    assert j.url == "https://nofluffjobs.com/pl/job/junior-python-developer-acme-warszawa-abcd1234"
    assert j.country == "PL" and j.city == "Warszawa" and j.remote == "unknown"
    assert j.location_raw == "Warszawa, Kraków"
    assert j.seniority_raw == "Trainee, Junior"
    assert j.salary_text == "1,800 - 2,600 EUR (b2b)"
    assert j.tags == ["Python", "backend"]
    assert j.posted_at == datetime.fromtimestamp(1756900000, tz=UTC)
    assert "FastAPI" in j.description and "Must have: Python, SQL" in j.description
    assert "<" not in j.description
    assert jobs[1].remote == "remote" and jobs[1].country == "EE"
    assert jobs[1].location_raw == "Remote, Tallinn"


def test_nofluffjobs_search_request_shape(make_ctx):
    ctx = make_ctx(ROUTES, options={"regions": ["pl", "en"], "seniority": ["junior"], "max_pages": 1,
                                    "fetch_details": False})
    jobs = list(NoFluffJobs().fetch(ctx))
    assert len(jobs) == 2  # ids are deduped across regions
    search = ctx.http.calls[0]
    assert search.headers["content-type"] == "application/infiniteSearch+json"
    assert "region=pl" in str(search.url) and "pageSize=20" in str(search.url)
    assert "salaryCurrency=EUR" in str(search.url) and "sort=newest" in str(search.url)
    import json

    body = json.loads(search.content)
    assert body["criteriaSearch"]["seniority"] == ["junior"]
    assert body["pageSize"] == 20 and body["withSalaryMatch"] is True
    assert all("/api/posting/" not in str(c.url) for c in ctx.http.calls)


def test_nofluffjobs_falls_back_to_plain_json(make_ctx):
    seen: list[str] = []

    def search(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("content-type", ""))
        if len(seen) == 1:
            return httpx.Response(415, text="Unsupported Media Type")
        return httpx.Response(200, text=fixture_text("nofluffjobs.json"))

    ctx = make_ctx({("POST", "/api/search/posting"): search}, options={"regions": ["pl"], "max_pages": 1,
                                                                      "fetch_details": False})
    assert len(list(NoFluffJobs().fetch(ctx))) == 2
    assert seen == ["application/infiniteSearch+json", "application/json"]


def test_nofluffjobs_respects_limit(make_ctx):
    ctx = make_ctx(ROUTES, options={"regions": ["pl", "en"], "max_pages": 5, "fetch_details": False}, limit=1)
    assert len(list(NoFluffJobs().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1


def test_nofluffjobs_helpers_tolerate_junk():
    assert salary_text(None) is None
    assert salary_text({"from": None, "to": None}) is None
    assert salary_text({"from": 5000, "to": 5000, "currency": "pln"}) == "5,000 PLN"
    assert build_description({}) is None
    assert build_description("nope") is None
    assert "Nice to have: Docker, AWS" in build_description(fixture_json("nofluffjobs_detail.json"))
