from datetime import UTC, datetime

import httpx
import pytest

from jobscraper.http import SourceHTTPError
from jobscraper.sources.landingjobs import (
    LandingJobs,
    company_from_url,
    parse_record,
    salary_text,
)

ROUTES = {"/api/v1/jobs": "landingjobs.json"}


def test_landingjobs_parses_fixture(make_ctx):
    ctx = make_ctx(ROUTES, options={"max_pages": 1})
    jobs = list(LandingJobs().fetch(ctx))
    assert len(jobs) == 3  # the broken row is skipped, not raised
    j = jobs[0]
    assert j.source == "landingjobs" and j.source_id == "19412"
    assert j.title == "Senior AI Engineer (Forward Deployed)"
    assert j.company == "Ki Performance"  # the feed has no company field; derived from /at/<company>/
    assert j.url == "https://landing.jobs/at/ki-performance/senior-ai-engineer-forward-deployed"
    assert j.employment_type == "Full-time"
    assert j.salary_text == "50,000 - 65,000 EUR"  # currency comes from currency_code
    assert j.tags[:2] == ["Databricks", "Microsoft Azure"]
    assert j.remote == "onsite"
    # three offices in two countries: keep each city next to its own code
    assert j.location_raw == "Munich (DE), Lisbon (PT), Cologne (DE)"
    assert j.city == "Munich" and j.country == "DE"
    assert "Senior AI Engineer" in j.description and "Git and CI/CD" in j.description
    assert "<" not in j.description
    assert j.posted_at == datetime(2025, 11, 24, 9, 52, 25, 780000, tzinfo=UTC)


def test_landingjobs_remote_row_without_a_city(make_ctx):
    ctx = make_ctx(ROUTES, options={"max_pages": 1})
    j = list(LandingJobs().fetch(ctx))[1]
    assert j.company == "Touchpoints Health"
    assert j.remote == "remote"
    assert j.location_raw == "PT, Remote"  # locations carries only a country_code
    assert j.city is None and j.country == "PT"
    assert j.salary_text == "45,000 - 50,000 EUR"
    assert j.posted_at == datetime(2026, 6, 11, 8, 49, 24, 689000, tzinfo=UTC)


def test_landingjobs_uses_published_at_not_the_bulk_updated_at(make_ctx):
    """The feed keeps long-lived postings and re-stamps updated_at in bulk; published_at is the date."""
    ctx = make_ctx(ROUTES, options={"max_pages": 1})
    j = list(LandingJobs().fetch(ctx))[2]
    assert j.title == "Senior Java Software Developer"
    assert j.posted_at == datetime(2025, 2, 26, 9, 38, 38, 127000, tzinfo=UTC)
    assert j.raw["updated_at"].startswith("2026-09-02")
    assert j.location_raw == "Lisbon (PT)" and j.city == "Lisbon" and j.country == "PT"
    assert j.salary_text == "50,000 - 67,000 EUR"


def test_landingjobs_pages_with_offset_and_stops(make_ctx):
    ctx = make_ctx(ROUTES, options={"max_pages": 5})
    list(LandingJobs().fetch(ctx))
    assert len(ctx.http.calls) == 1  # a short page is the last page
    url = str(ctx.http.calls[0].url)
    assert "limit=50" in url and "offset=0" in url


def test_landingjobs_stops_on_empty_page(make_ctx):
    pages = [[{"id": i, "title": f"Junior Dev {i}",
               "url": f"https://landing.jobs/at/acme/junior-dev-{i}"} for i in range(50)], []]
    calls: list[int] = []

    def route(req):
        import httpx

        calls.append(1)
        return httpx.Response(200, json=pages[min(len(calls) - 1, 1)])

    ctx = make_ctx({"/api/v1/jobs": route}, options={"max_pages": 5})
    jobs = list(LandingJobs().fetch(ctx))
    assert len(jobs) == 50 and len(calls) == 2
    assert "offset=50" in str(ctx.http.calls[1].url)


def test_landingjobs_respects_limit(make_ctx):
    ctx = make_ctx(ROUTES, options={"max_pages": 3}, limit=1)
    assert len(list(LandingJobs().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1


def test_landingjobs_helpers_tolerate_junk():
    assert company_from_url(None) is None
    assert company_from_url("https://landing.jobs/jobs/123") is None
    assert company_from_url("https://landing.jobs/at/acme_labs/x") == "Acme Labs"
    assert salary_text({}) is None
    assert salary_text({"gross_salary_low": 30000}) == "30,000"
    assert salary_text({"gross_salary_low": 30000, "currency_code": "brl"}) == "30,000 BRL"
    # a figure the feed wrote as text is kept verbatim rather than dropped
    assert salary_text({"gross_salary_low": "40k"}) == "40k"


def test_landingjobs_reads_the_half_filled_location_rows():
    job = parse_record(
        {
            "id": 1,
            "title": "Dev",
            "url": "https://landing.jobs/at/acme/dev",
            "locations": [{}, {"city": "Porto"}, {"country_code": "123"}],
        }
    )
    assert job.location_raw == "Porto"
    assert job.city == "Porto" and job.country is None  # "123" is not a country code

    plain = parse_record(
        {
            "id": 2,
            "title": "Dev",
            "url": "https://landing.jobs/at/acme/dev-2",
            "location": "  Braga, Portugal  ",  # some rows carry a string instead of a list
        }
    )
    assert plain.location_raw == "Braga, Portugal"
    assert plain.city == "Braga" and plain.country == "PT"

    assert parse_record(["not", "an", "object"]) is None


def test_landingjobs_unwraps_an_array_the_feed_put_in_an_envelope(make_ctx):
    row = {"id": 9, "title": "Dev", "url": "https://landing.jobs/at/acme/dev-9"}
    ctx = make_ctx({"/api/v1/jobs": {"jobs": [row]}}, options={"max_pages": 1})
    assert [j.source_id for j in LandingJobs().fetch(ctx)] == ["9"]


def test_landingjobs_raises_when_the_feed_is_not_a_list(make_ctx):
    ctx = make_ctx({"/api/v1/jobs": lambda _req: httpx.Response(200, json=42)})
    with pytest.raises(SourceHTTPError, match="expected a list"):
        list(LandingJobs().fetch(ctx))


def test_landingjobs_dedupes_across_pages_and_stops_at_max_pages(make_ctx):
    """The feed is not date-ordered, so a full second page can repeat the first one entirely."""
    page = [
        {"id": i, "title": f"Junior Dev {i}", "url": f"https://landing.jobs/at/acme/junior-dev-{i}"}
        for i in range(50)
    ]
    ctx = make_ctx({"/api/v1/jobs": page}, options={"max_pages": 2})
    jobs = list(LandingJobs().fetch(ctx))

    assert len(jobs) == 50  # the repeats are dropped
    assert len(ctx.http.calls) == 2
