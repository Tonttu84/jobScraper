"""wttj adapter tests.

The fixtures are trimmed *real* payloads: ``wttj_env.json`` is the two Algolia constants out of
the live ``window.env = {...};`` blob, ``wttj.json`` is three real hits of the
``wttj_jobs_production_en`` index (long text cut, ``_highlightResult``/ranking noise dropped) plus
one deliberately broken hit, and ``wttj_job.json`` is one real
``api.welcometothejungle.com/api/v1/organizations/{org}/jobs/{slug}`` document.
"""

import json
from datetime import UTC, datetime

import httpx

from jobscraper.sources.wttj import (
    API_KEY,
    APP_ID,
    WTTJ,
    build_filters,
    build_tags,
    credentials,
)
from tests.conftest import fixture_text

ENV_ROUTE = {"/api/env": "wttj_env.json"}
LIST_ROUTES = {**ENV_ROUTE, "algolia.net": "wttj.json"}


def _missing_detail(req: httpx.Request) -> httpx.Response:
    return httpx.Response(404, text='{"error":"not found"}')


# Only the CNPP job has a detail document; the other two 404 like a job pulled offline would.
ALL_ROUTES = {
    **ENV_ROUTE,
    "/api/v1/organizations/cnpp/jobs/": "wttj_job.json",
    "/api/v1/organizations/": _missing_detail,
    "algolia.net": "wttj.json",
}
NO_DETAILS = {"fetch_details": False, "max_pages": 1}


def test_wttj_parses_real_hits(make_ctx):
    ctx = make_ctx(LIST_ROUTES, options={"query": "junior developer", "countries": ["FI", "EE"], **NO_DETAILS})
    jobs = list(WTTJ().fetch(ctx))
    assert len(jobs) == 3  # the broken hit is skipped, not raised

    j = jobs[0]
    assert j.source == "wttj" and j.source_id == "2919acab-2be1-4c4c-b714-a264adfcf675"
    assert j.title == "Développeur Junior"
    assert j.company == "Groupe CNPP"
    assert j.url == (
        "https://www.welcometothejungle.com/en/companies/cnpp/jobs/developpeur-junior_saint-marcel"
    )
    assert j.country == "FR" and j.city == "Saint-Marcel"
    assert j.location_raw == "Saint-Marcel, France"
    assert j.remote == "hybrid"  # "partial"
    assert j.seniority_raw == "0+ years" and j.employment_type == "temporary"
    assert j.salary_text == "30,000 - 35,000 EUR/yearly"
    assert j.tags == [
        "Audit",
        "Cyber Security",
        "Job Training",
        "Software & Web Development",
        "Software Developer",
        "lang:fr",
    ]
    assert j.posted_at == datetime.fromtimestamp(1787944381, tz=UTC)
    assert j.description is not None
    assert "Rejoignez notre équipe" in j.description  # summary
    assert "premières expériences en développement" in j.description  # profile
    assert "- Réaliser des applicatifs" in j.description  # key_missions bullets
    assert "<" not in j.description and "&#39;" not in j.description

    berlin = jobs[1]
    assert berlin.title == "Junior iOS Engineer" and berlin.company == "Scalable Capital"
    assert berlin.country == "DE" and berlin.city == "Berlin"
    assert berlin.remote == "unknown"  # the index really does ship remote:"unknown"
    assert berlin.salary_text is None
    assert berlin.seniority_raw == "1+ years"
    assert berlin.tags == ["Finance", "Software & Web Development", "Mobile App Developer", "lang:en"]
    assert berlin.posted_at == datetime.fromtimestamp(1785543464, tz=UTC)

    us = jobs[2]
    assert us.remote == "remote" and us.country == "US"
    assert us.city is None and us.location_raw == "United States"


def test_wttj_build_tags_of_a_bare_hit():
    assert build_tags({}) == []
    assert build_tags({"sectors": ["not-a-dict"], "language": "DE"}) == ["lang:de"]


def test_wttj_uses_env_credentials_and_filters(make_ctx):
    ctx = make_ctx(
        LIST_ROUTES,
        options={"query": "junior", "countries": ["FI", "EE"], "include_remote": True, **NO_DETAILS},
    )
    list(WTTJ().fetch(ctx))
    env_call, query_call = ctx.http.calls[0], ctx.http.calls[1]
    assert str(env_call.url) == "https://www.welcometothejungle.com/api/env"
    assert str(query_call.url) == (  # httpx lower-cases the host
        "https://csekhvms53-dsn.algolia.net/1/indexes/wttj_jobs_production_en/query"
    )
    assert query_call.headers["x-algolia-application-id"] == "CSEKHVMS53"
    assert query_call.headers["x-algolia-api-key"] == "4bd8f6215d0cc52b26430765769e65a0"
    assert query_call.headers["referer"] == "https://www.welcometothejungle.com/"
    body = json.loads(query_call.content)
    assert body["query"] == "junior" and body["hitsPerPage"] == 100 and body["page"] == 0
    assert body["filters"] == "(offices.country_code:FI OR offices.country_code:EE) OR remote:fulltime"


def test_wttj_falls_back_to_builtin_credentials(make_ctx):
    def env(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    ctx = make_ctx({"/api/env": env, "algolia.net": "wttj.json"}, options=NO_DETAILS)
    assert len(list(WTTJ().fetch(ctx))) == 3
    query_call = ctx.http.calls[-1]
    assert f"https://{APP_ID.lower()}-dsn.algolia.net" in str(query_call.url)
    assert query_call.headers["x-algolia-api-key"] == API_KEY


def test_wttj_credentials_parse_the_window_env_blob(make_ctx):
    # The fixture is the live pair, so it also pins the built-in fallback constants.
    ctx = make_ctx({"/api/env": fixture_text("wttj_env.json")})
    assert credentials(ctx) == ("CSEKHVMS53", "4bd8f6215d0cc52b26430765769e65a0")
    assert credentials(ctx) == (APP_ID, API_KEY)


def test_wttj_credentials_prefer_a_rotated_env_over_the_constants(make_ctx):
    rotated = 'window.env = {"PUBLIC_ALGOLIA_APPLICATION_ID":"ROTATED1","PUBLIC_ALGOLIA_API_KEY_CLIENT":"%s"};'
    ctx = make_ctx({"/api/env": rotated % ("a" * 32)})
    assert credentials(ctx) == ("ROTATED1", "a" * 32)


def test_wttj_credentials_ignore_junk_env(make_ctx):
    ctx = make_ctx({"/api/env": 'window.env = {"PUBLIC_ALGOLIA_APPLICATION_ID": ""};'})
    assert credentials(ctx) == (APP_ID, API_KEY)


def test_wttj_build_filters():
    assert build_filters(["FI"], False) == "offices.country_code:FI"
    assert build_filters([], True) == "remote:fulltime"
    assert build_filters([], False) is None
    assert build_filters(["fi", "ee"], True) == (
        "(offices.country_code:FI OR offices.country_code:EE) OR remote:fulltime"
    )


def test_wttj_respects_limit(make_ctx):
    ctx = make_ctx(LIST_ROUTES, options={"max_pages": 3, "fetch_details": False}, limit=1)
    assert len(list(WTTJ().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 2  # /api/env + one Algolia query


def test_wttj_stops_at_nbpages(make_ctx):
    """The fixture says nbPages 2, so max_pages 5 must still stop after the second query."""
    ctx = make_ctx(LIST_ROUTES, options={"max_pages": 5, "fetch_details": False})
    jobs = list(WTTJ().fetch(ctx))
    assert len(jobs) == 3  # page 2 repeats the fixture; the URL dedupe drops it
    assert len(ctx.http.calls) == 3  # /api/env + two Algolia queries


def test_wttj_hydrates_description_from_the_detail_api(make_ctx):
    ctx = make_ctx(ALL_ROUTES, options={"max_pages": 1})
    jobs = list(WTTJ().fetch(ctx))
    assert len(jobs) == 3
    detail_calls = [c for c in ctx.http.calls if "/api/v1/organizations/" in str(c.url)]
    assert str(detail_calls[0].url) == (
        "https://api.welcometothejungle.com/api/v1/organizations/cnpp/jobs/"
        "developpeur-junior_saint-marcel"
    )
    assert len(detail_calls) == 3

    j = jobs[0]
    assert "Votre challenge" in j.description  # only in the detail document
    assert "Python, PHP, JavaScript" in j.description  # the untruncated profile
    assert "Rejoignez notre équipe" in j.description  # the index summary is kept as the lead
    assert "<" not in j.description and "&#39;" not in j.description
    assert j.raw["apply_url"].startswith("https://job.mytalentplug.com/")


def test_wttj_keeps_the_job_when_the_detail_fetch_fails(make_ctx):
    ctx = make_ctx(ALL_ROUTES, options={"max_pages": 1})
    jobs = list(WTTJ().fetch(ctx))
    berlin = jobs[1]  # its detail 404s
    assert "Join our team as a Junior iOS Engineer" in berlin.description
    assert "apply_url" not in berlin.raw


def test_wttj_caps_the_number_of_detail_fetches(make_ctx):
    ctx = make_ctx(ALL_ROUTES, options={"max_pages": 1, "max_details": 1})
    jobs = list(WTTJ().fetch(ctx))
    assert len(jobs) == 3
    assert len([c for c in ctx.http.calls if "/api/v1/organizations/" in str(c.url)]) == 1
    assert "Votre challenge" in jobs[0].description


def test_wttj_detail_fetch_can_be_turned_off(make_ctx):
    ctx = make_ctx(ALL_ROUTES, options={"max_pages": 1, "fetch_details": False})
    jobs = list(WTTJ().fetch(ctx))
    assert not [c for c in ctx.http.calls if "/api/v1/organizations/" in str(c.url)]
    assert "Votre challenge" not in jobs[0].description
