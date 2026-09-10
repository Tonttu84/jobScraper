from datetime import UTC, datetime

import httpx

from jobscraper.sources.himalayas import Himalayas, _strings, region_country

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
    assert j.country == "KR"  # the country table learned the non-European names on 2026-09-10
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


def test_himalayas_region_country_needs_exactly_one_country():
    assert region_country(None) is None
    assert region_country("Anywhere in the World") is None  # a worldwide posting names no country
    assert region_country("Germany or Austria") is None  # two countries: no single answer
    assert region_country("Germany") == "DE"


def test_himalayas_restriction_lists_come_in_every_shape():
    assert _strings("Poland") == ["Poland"]  # a bare string where a list is normal
    assert _strings("   ") == []
    assert _strings(["Poland", None, {"country": "PL"}, True, -7.5]) == ["Poland", "UTC-7.5"]
    assert _strings(42) == []  # not a list and not a string


def test_himalayas_skips_a_record_that_is_not_an_object(make_ctx):
    routes = {
        "cursor=": {"jobs": [], "nextCursor": None},
        "himalayas.app/jobs/api": {"jobs": ["a bare string", {"title": "Dev", "guid": "g1"}]},
    }
    jobs = list(Himalayas().fetch(make_ctx(routes)))
    assert [j.source_id for j in jobs] == ["g1"]


def test_himalayas_stops_when_the_cursor_stops_moving(make_ctx):
    """A feed that keeps handing back the same cursor must not be paged forever."""
    page = {"jobs": [{"title": "Dev", "guid": "g1"}], "nextCursor": CURSOR}
    ctx = make_ctx({"himalayas.app/jobs/api": page}, options={"max_pages": 5})
    assert len(list(Himalayas().fetch(ctx))) == 2  # page 1, then the repeat, then stop
    assert len(ctx.http.calls) == 2


def test_himalayas_stops_at_max_pages(make_ctx):
    """Fresh cursors all the way down: only the page budget ends the loop."""
    cursors = iter(["c1", "c2", "c3", "c4"])

    def page(_req):
        return httpx.Response(
            200, json={"jobs": [{"title": "Dev", "guid": next(cursors)}], "nextCursor": next(cursors)}
        )

    ctx = make_ctx({"himalayas.app/jobs/api": page}, options={"max_pages": 2})
    assert len(list(Himalayas().fetch(ctx))) == 2
    assert len(ctx.http.calls) == 2
