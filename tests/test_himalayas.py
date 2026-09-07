from datetime import UTC, datetime

from jobscraper.sources.himalayas import Himalayas

CURSOR = "MjAyNi0wOS0wN1QwNzoxNzowMy4yNTMzMjJafDIxNzI4OTk"


def _one_page(make_ctx, **kwargs):
    """Serve the fixture once, then an empty page, so pagination terminates."""
    routes = {"cursor=": {"jobs": [], "nextCursor": None}, "himalayas.app/jobs/api": "himalayas.json"}
    return make_ctx(routes, **kwargs)


def test_himalayas_parses_fixture(make_ctx):
    ctx = _one_page(make_ctx)
    jobs = list(Himalayas().fetch(ctx))

    assert len(jobs) == 3  # the record without title/link is skipped
    j = jobs[0]
    assert j.source == "himalayas"
    assert j.title == "North American Junior Authorization Line Specialist"
    assert j.company == "U.S. Bank"
    assert j.url.endswith("/jobs/north-american-junior-authorization-line-specialist")
    assert j.source_id == j.url  # guid doubles as the stable id
    assert j.remote == "remote" and j.remote_region == "Poland" and j.country == "PL"
    assert j.seniority_raw == "Entry-level"
    assert j.employment_type == "Full Time"
    assert j.tags[:2] == ["Customer Service", "Customer-Service"]  # parentCategories first
    assert j.salary_text == "61,030 - 71,800 PLN/year"
    assert "smarter financial decisions" in j.description and "<" not in j.description
    assert j.posted_at == datetime(2026, 9, 7, 7, 33, 35, tzinfo=UTC)


def test_himalayas_keeps_the_salary_period(make_ctx):
    """minSalary/maxSalary are per ``salaryPeriod``; 80-160 USD is hourly, not a yearly range."""
    j = list(Himalayas().fetch(_one_page(make_ctx)))[1]
    assert j.company == "mercor"
    assert j.salary_text == "80 - 160 USD/hour"
    assert j.country == "FR" and j.remote_region == "France"


def test_himalayas_falls_back_to_numeric_timezone_restrictions(make_ctx):
    """``timezoneRestrictions`` are UTC offsets as numbers, not strings."""
    j = list(Himalayas().fetch(_one_page(make_ctx)))[2]
    assert j.title == "Change Business Partner - MRP"
    assert j.remote_region == "South Korea"  # locationRestrictions win when present
    assert j.country is None  # "South Korea" is not in the country table; better None than wrong
    assert j.raw["timezoneRestrictions"] == [9]
    assert j.salary_text is None
    assert j.posted_at == datetime(2026, 9, 7, 7, 35, 12, tzinfo=UTC)


def test_himalayas_paginates_by_cursor(make_ctx):
    """``comments`` says offset is deprecated: page with ?cursor=<nextCursor>."""
    ctx = _one_page(make_ctx, options={"max_pages": 5, "page_size": 20})
    list(Himalayas().fetch(ctx))

    assert len(ctx.http.calls) == 2  # the second page comes back empty
    first, second = (str(c.url) for c in ctx.http.calls)
    assert "cursor=" not in first and "offset=" not in first
    assert "limit=20" in first
    assert f"cursor={CURSOR}" in second
    assert "offset=" not in second


def test_himalayas_respects_limit(make_ctx):
    ctx = _one_page(make_ctx, limit=1)
    assert len(list(Himalayas().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1
