"""Fixtures are trimmed copies of real nofluffjobs.com responses captured 2026-09-07."""

from datetime import UTC, datetime

import httpx

from jobscraper.filters.language import find_language_requirements
from jobscraper.sources.nofluffjobs import NoFluffJobs, build_description, salary_text
from tests.conftest import fixture_json, fixture_text

ROUTES = {
    ("POST", "/api/search/posting"): "nofluffjobs.json",
    ("GET", "/api/posting/"): "nofluffjobs_detail.json",
}

QVC_ID = "privacy-data-governance-analyst-qvc-group-global-business-services-Kraków"
QVC_URL = (
    "https://nofluffjobs.com/pl/job/"
    "privacy-data-governance-analyst-qvc-group-global-business-services-krakow"
)


def test_nofluffjobs_parses_fixture(make_ctx):
    ctx = make_ctx(ROUTES, options={"regions": ["pl"], "max_pages": 1})
    jobs = list(NoFluffJobs().fetch(ctx))
    assert len(jobs) == 3  # the broken posting is skipped, not raised
    j = jobs[0]
    assert j.source == "nofluffjobs"
    assert j.source_id == QVC_ID
    assert j.title == "Privacy & Data Governance Analyst"
    assert j.company == "QVC GROUP GLOBAL BUSINESS SERVICES"
    assert j.url == QVC_URL
    assert j.country == "PL" and j.city == "Kraków" and j.remote == "unknown"
    assert j.location_raw == "Kraków"
    assert j.seniority_raw == "Junior"
    assert j.salary_text == "2,084 - 3,242 EUR (permanent)"
    assert j.tags == ["security"]  # this posting carries no "technology"
    # "posted" is epoch *milliseconds*: seconds would land in the year 58000.
    assert j.posted_at == datetime.fromtimestamp(1787735594.863, tz=UTC)
    assert j.posted_at.strftime("%Y-%m-%d") == "2026-08-26"


def test_nofluffjobs_description_from_real_detail_blocks(make_ctx):
    ctx = make_ctx(ROUTES, options={"regions": ["pl"], "max_pages": 1})
    desc = next(iter(NoFluffJobs().fetch(ctx))).description
    assert desc.startswith("Required languages: English (C1).")  # requirements.languages
    assert "Information Governance team" in desc  # details.description
    assert "What You'll Bring" in desc  # requirements.description
    assert "- Develop, update, and maintain company data maps" in desc  # specs.dailyTasks
    assert "Must have: Data management, Stakeholder management, Compliance" in desc
    assert "Nice to have: TrustArc, Visio" in desc
    assert "<" not in desc and "&amp;" not in desc


def test_nofluffjobs_fully_remote_posting(make_ctx):
    ctx = make_ctx(ROUTES, options={"regions": ["pl"], "max_pages": 1, "fetch_details": False})
    jobs = list(NoFluffJobs().fetch(ctx))
    j = jobs[1]
    assert j.title == "Junior Software Engineer (Integrations)"
    assert j.remote == "remote"  # location.fullyRemote, not the top-level flag
    # The API prepends a pseudo-place {"city": "Remote"} to the real offices.
    assert j.location_raw == "Remote, Wrocław"
    assert j.city == "Wrocław"
    assert j.country == "PL"  # country.code is the 3-letter "POL" here
    assert j.tags == ["Java", "backend"]
    assert j.salary_text == "1,389 - 2,084 EUR (b2b)"
    assert j.posted_at == datetime.fromtimestamp(1788539049.853, tz=UTC)

    multi = jobs[2]
    assert multi.location_raw == "Remote, Warszawa, Kraków, Budapest, Wrocław"
    assert multi.city == "Warszawa" and multi.country == "PL"
    assert multi.salary_text == "2,315 EUR (permanent)"  # from == to


def test_nofluffjobs_dedupes_the_per_place_copies(make_ctx):
    """One page repeats a posting once per place: 133 records for 20 jobs on 2026-09-07."""
    copies = [r for r in fixture_json("nofluffjobs.json")["postings"] if r.get("reference") == "PHSSTKP3"]
    assert len(copies) == 2 and copies[0]["id"] != copies[1]["id"]  # same job, id per place
    ctx = make_ctx(ROUTES, options={"regions": ["pl"], "max_pages": 1, "fetch_details": False})
    ids = [j.source_id for j in NoFluffJobs().fetch(ctx)]
    # Only the first copy survives; ``reference`` is the stable per-posting key, ``id`` is not.
    assert ids.count(copies[0]["id"]) == 1
    assert copies[1]["id"] not in ids


def test_nofluffjobs_search_request_shape(make_ctx):
    ctx = make_ctx(ROUTES, options={"regions": ["pl", "en"], "seniority": ["junior"], "max_pages": 1,
                                    "fetch_details": False})
    jobs = list(NoFluffJobs().fetch(ctx))
    assert len(jobs) == 3  # ids are deduped across regions
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
    assert len(list(NoFluffJobs().fetch(ctx))) == 3
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
    assert "Nice to have: TrustArc, Visio" in build_description(fixture_json("nofluffjobs_detail.json"))


def test_nofluffjobs_language_requirements_become_text():
    """``requirements.languages`` is structured, so the prose never names the language."""
    detail = {
        "requirements": {
            "description": "<p>Some prose.</p>",
            "languages": [
                {"type": "MUST", "code": "pl", "level": "C2"},
                {"type": "MUST", "code": "en", "level": "B2"},
                {"type": "NICE", "code": "de", "level": "B1"},
            ],
        }
    }
    desc = build_description(detail)
    # First, so it survives any downstream truncation of the description.
    assert desc.startswith("Required languages: Polish (C2), English (B2).")
    assert "Nice-to-have languages: German (B1)." in desc
    assert "Some prose." in desc


def test_nofluffjobs_language_levels_and_unknown_codes():
    detail = {
        "requirements": {
            "languages": [
                {"type": "MUST", "code": "pl", "level": "NATIVE"},
                {"type": "MUST", "code": "en", "level": None},
                {"type": "MUST", "code": "zz", "level": "A1"},
                {"type": "NICE", "code": "", "level": "B2"},  # skipped: no code
                "junk",
            ]
        }
    }
    assert build_description(detail).strip() == "Required languages: Polish (native), English, ZZ (A1)."


def test_nofluffjobs_without_languages_says_nothing_about_them():
    detail = {"requirements": {"description": "<p>Some prose.</p>", "languages": None}}
    desc = build_description(detail)
    assert "Required languages" not in desc and "Nice-to-have languages" not in desc
    assert build_description({"details": {"description": "Hi there, this is the job."}}).strip() == (
        "Hi there, this is the job."
    )


def test_nofluffjobs_language_block_feeds_the_rule_filter(make_ctx):
    """End to end: the hydrated description makes the language detector fire."""
    detail = fixture_json("nofluffjobs_detail.json")
    detail["requirements"]["languages"] = [
        {"type": "MUST", "code": "pl", "level": "C2"},
        {"type": "MUST", "code": "en", "level": "B2"},
        {"type": "NICE", "code": "de", "level": "B1"},
        # "Germany" in the prose already trips the "de" name, so assert on a language the
        # fixture never mentions as well.
        {"type": "NICE", "code": "cs", "level": None},
    ]
    routes = {**ROUTES, ("GET", "/api/posting/"): detail}
    ctx = make_ctx(routes, options={"regions": ["pl"], "max_pages": 1})
    job = next(iter(NoFluffJobs().fetch(ctx)))
    found = find_language_requirements(job.description)
    assert "pl" in found.required and "en" in found.required
    assert "de" in found.optional and "cs" in found.optional
