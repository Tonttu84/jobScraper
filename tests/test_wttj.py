import json
from datetime import UTC, datetime

import httpx

from jobscraper.sources.wttj import API_KEY, APP_ID, WTTJ, build_filters, credentials
from tests.conftest import fixture_text

ROUTES = {"/api/env": "wttj_env.json", "algolia.net": "wttj.json"}


def test_wttj_parses_fixture(make_ctx):
    ctx = make_ctx(ROUTES, options={"query": "junior developer", "countries": ["FI", "EE"], "max_pages": 1})
    jobs = list(WTTJ().fetch(ctx))
    assert len(jobs) == 2  # the broken hit is skipped, not raised
    j = jobs[0]
    assert j.source == "wttj" and j.source_id == "1234567"
    assert j.title == "Junior Backend Developer"
    assert j.company == "Nordic Softworks"
    assert j.url == (
        "https://www.welcometothejungle.com/en/companies/nordic-softworks/jobs/"
        "junior-backend-developer_helsinki"
    )
    assert j.country == "FI" and j.city == "Helsinki" and j.remote == "hybrid"
    assert j.seniority_raw == "1+ years" and j.employment_type == "full_time"
    assert j.salary_text == "38,000 - 46,000 EUR/yearly"
    assert j.tags == ["SaaS / Cloud Services"]
    assert j.posted_at == datetime.fromtimestamp(1756792800, tz=UTC)
    assert "Django monolith" in j.description and "Nice to have" in j.description
    assert "<" not in j.description
    assert jobs[1].remote == "remote" and jobs[1].country == "EE"
    assert jobs[1].salary_text == "30,000 EUR/yearly"


def test_wttj_uses_env_credentials_and_filters(make_ctx):
    ctx = make_ctx(ROUTES, options={"query": "junior", "countries": ["FI", "EE"], "include_remote": True,
                                    "max_pages": 1})
    list(WTTJ().fetch(ctx))
    env_call, query_call = ctx.http.calls[0], ctx.http.calls[1]
    assert str(env_call.url) == "https://www.welcometothejungle.com/api/env"
    assert str(query_call.url) == (  # httpx lower-cases the host
        "https://testapp123-dsn.algolia.net/1/indexes/wttj_jobs_production_en/query"
    )
    assert query_call.headers["x-algolia-application-id"] == "TESTAPP123"
    assert query_call.headers["x-algolia-api-key"] == "0123456789abcdef0123456789abcdef"
    assert query_call.headers["referer"] == "https://www.welcometothejungle.com/"
    body = json.loads(query_call.content)
    assert body["query"] == "junior" and body["hitsPerPage"] == 100 and body["page"] == 0
    assert body["filters"] == "(offices.country_code:FI OR offices.country_code:EE) OR remote:fulltime"


def test_wttj_falls_back_to_builtin_credentials(make_ctx):
    def env(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    ctx = make_ctx({"/api/env": env, "algolia.net": "wttj.json"}, options={"max_pages": 1})
    assert len(list(WTTJ().fetch(ctx))) == 2
    query_call = ctx.http.calls[-1]
    assert f"https://{APP_ID.lower()}-dsn.algolia.net" in str(query_call.url)
    assert query_call.headers["x-algolia-api-key"] == API_KEY


def test_wttj_credentials_ignore_junk_env(make_ctx):
    ctx = make_ctx({"/api/env": "window.env = {\"PUBLIC_ALGOLIA_APPLICATION_ID\": \"\"};"})
    assert credentials(ctx) == (APP_ID, API_KEY)
    ctx = make_ctx({"/api/env": "window.env = " + fixture_text("wttj_env.json") + ";"})
    assert credentials(ctx) == ("TESTAPP123", "0123456789abcdef0123456789abcdef")


def test_wttj_build_filters():
    assert build_filters(["FI"], False) == "offices.country_code:FI"
    assert build_filters([], True) == "remote:fulltime"
    assert build_filters([], False) is None
    assert build_filters(["fi", "ee"], True) == "(offices.country_code:FI OR offices.country_code:EE) OR remote:fulltime"


def test_wttj_respects_limit(make_ctx):
    ctx = make_ctx(ROUTES, options={"max_pages": 3}, limit=1)
    assert len(list(WTTJ().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 2  # /api/env + one Algolia query
