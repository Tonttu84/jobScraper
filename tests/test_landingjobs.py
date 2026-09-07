from datetime import UTC, datetime

from jobscraper.sources.landingjobs import LandingJobs, company_from_url, salary_text

ROUTES = {"/api/v1/jobs": "landingjobs.json"}


def test_landingjobs_parses_fixture(make_ctx):
    ctx = make_ctx(ROUTES, options={"max_pages": 1})
    jobs = list(LandingJobs().fetch(ctx))
    assert len(jobs) == 2  # the broken row is skipped, not raised
    j = jobs[0]
    assert j.source == "landingjobs" and j.source_id == "88121"
    assert j.title == "Junior Backend Developer (Python)"
    assert j.company == "Acme Labs"  # derived from the /at/<company>/ URL path
    assert j.url == "https://landing.jobs/at/acme-labs/junior-backend-developer-python"
    assert j.country == "PT" and j.city == "Lisboa" and j.remote == "onsite"
    assert j.location_raw == "Lisboa, PT"
    assert j.employment_type == "Full-time" and j.salary_text == "28,000 - 34,000 EUR"
    assert j.tags == ["python", "django", "postgresql"]
    assert j.posted_at == datetime(2025, 9, 3, 11, 20, tzinfo=UTC)
    assert "Django" in j.description and "Docker, AWS" in j.description and "<" not in j.description

    second = jobs[1]
    assert second.company == "Remote Collective" and second.remote == "remote"
    assert second.country == "DE" and second.city == "Berlin"
    assert second.location_raw == "Berlin, Germany, Remote"
    assert second.posted_at == datetime(2025, 8, 28, 8, 0, tzinfo=UTC)  # falls back to created_at


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
