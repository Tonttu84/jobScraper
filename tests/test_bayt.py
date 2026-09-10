from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jobscraper.http import SourceHTTPError
from jobscraper.sources.bayt import (
    Bayt,
    description_of,
    job_cards,
    parse_card,
    parse_posted,
    slugify,
)

ROUTES = {
    "www.bayt.com/en/uae/jobs/q/": "bayt.html",
    "-5123456/": "bayt_job.html",
    "-5123499/": "bayt_job.html",
    "-5123500/": "bayt_job.html",
    "www.bayt.com/": "<html><body>home</body></html>",  # cookie handshake, matched last
}


def test_bayt_parses_listing(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["junior developer"], "max_pages": 1,
                                    "fetch_details": False})
    jobs = list(Bayt().fetch(ctx))

    assert len(jobs) == 3  # the sponsored card without a link is skipped
    j = jobs[0]
    assert j.source == "bayt"
    assert j.title == "Junior Software Developer"
    assert j.company == "Tech Horizon FZ-LLC"
    assert j.url == "https://www.bayt.com/en/uae/jobs/junior-software-developer-5123456/"
    assert j.source_id == "junior-software-developer-5123456"
    assert j.location_raw == "Dubai, United Arab Emirates"
    assert j.country == "AE" and j.city == "Dubai"
    assert j.remote == "onsite" or j.remote == "unknown"
    assert j.posted_at is not None
    assert datetime.now(UTC) - j.posted_at < timedelta(days=4)

    # second card uses the other markup variant (no data-js-job, no jb-* classes)
    assert jobs[1].title == "Remote Frontend Engineer (React)"
    assert jobs[1].company == "Gulf Digital Solutions"
    assert jobs[1].remote == "remote"
    assert jobs[2].company is None and jobs[2].location_raw is None  # tolerates missing fields


def test_bayt_slugifies_queries_and_does_the_cookie_handshake(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["junior developer"], "max_pages": 2,
                                    "fetch_details": False})
    list(Bayt().fetch(ctx))

    urls = [str(c.url) for c in ctx.http.calls]
    assert urls[0] == "https://www.bayt.com/"  # cookies first
    assert urls[1] == "https://www.bayt.com/en/uae/jobs/q/junior-developer/?page=1"
    assert urls[2].endswith("?page=2")


def test_bayt_fetches_descriptions(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["junior-developer"], "max_pages": 1,
                                    "fetch_details": True, "max_details": 1})
    jobs = list(Bayt().fetch(ctx))

    assert "Junior Software Developer" in jobs[0].description
    assert "Bachelor" in jobs[0].description and "<" not in jobs[0].description
    assert "Similar jobs you may like" not in jobs[0].description
    assert jobs[1].description is None  # max_details budget spent


def test_bayt_raises_on_bot_block(make_ctx):
    routes = dict(ROUTES)
    routes["www.bayt.com/en/uae/jobs/q/"] = lambda req: httpx.Response(
        403, text="<html><body>Access Denied</body></html>"
    )
    ctx = make_ctx(routes, options={"queries": ["junior-developer"], "fetch_details": False})
    with pytest.raises(SourceHTTPError, match="headless browser"):
        list(Bayt().fetch(ctx))


def test_bayt_respects_limit(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["junior-developer", "software-developer"],
                                    "fetch_details": False}, limit=1)
    assert len(list(Bayt().fetch(ctx))) == 1


def test_slugify_and_relative_dates():
    assert slugify("Software Developer (Junior)") == "software-developer-junior"
    assert parse_posted("Today") is not None
    assert parse_posted("30+ days ago") < datetime.now(UTC) - timedelta(days=29)
    assert parse_posted("2026-08-30") == datetime(2026, 8, 30, tzinfo=UTC)
    assert parse_posted(None) is None
    assert parse_posted("nonsense") is None


CARDS = """<html><body><ul>
<li data-js-job=""><h2><a href="">   </a></h2></li>
<li data-js-job=""><h2><a href="en/uae/jobs/data-engineer-777/">Data Engineer</a></h2>
  <span data-automation-id="job-active-date">3 days ago</span></li>
</ul></body></html>"""


def test_bayt_skips_an_empty_card_and_reads_the_other_date_markup():
    cards = job_cards(CARDS)
    assert len(cards) == 2
    assert parse_card(cards[0]) is None  # an anchor with neither text nor target

    job = parse_card(cards[1])
    assert job.title == "Data Engineer"
    assert job.url == "https://www.bayt.com/en/uae/jobs/data-engineer-777/"  # relative href
    # no jb-date span: the date comes off the data-automation-id element instead
    assert datetime.now(UTC) - job.posted_at < timedelta(days=4)


def test_bayt_description_needs_a_content_block():
    assert description_of("<html><body><div class='side-rail'>ads</div></body></html>") is None
    # the older markup is only consulted once the current one has found nothing
    assert description_of("<html><body><div class='jb-descr'><p>Old markup</p></div></body></html>") == "Old markup"


def test_bayt_scrapes_on_when_the_cookie_handshake_fails(make_ctx, caplog):
    routes = {k: v for k, v in ROUTES.items() if k != "www.bayt.com/"}
    ctx = make_ctx(routes, options={"queries": ["junior-developer"], "max_pages": 1,
                                    "fetch_details": False})
    with caplog.at_level("WARNING"):
        assert len(list(Bayt().fetch(ctx))) == 3
    assert "cookie handshake failed" in caplog.text


def test_bayt_stops_at_the_first_page_without_cards(make_ctx):
    routes = {"page=2": "<html><body><ul></ul></body></html>", **ROUTES}
    ctx = make_ctx(routes, options={"queries": ["junior-developer"], "max_pages": 4,
                                    "fetch_details": False})
    list(Bayt().fetch(ctx))

    listing_calls = [c for c in ctx.http.calls if "/jobs/q/" in str(c.url)]
    assert len(listing_calls) == 2  # page 3 is never asked for


def test_bayt_raises_on_a_broken_listing_page(make_ctx):
    routes = dict(ROUTES)
    routes["www.bayt.com/en/uae/jobs/q/"] = lambda req: httpx.Response(500, text="server error")
    ctx = make_ctx(routes, options={"queries": ["junior-developer"], "fetch_details": False})
    with pytest.raises(SourceHTTPError, match="HTTP 500"):
        list(Bayt().fetch(ctx))


def test_bayt_keeps_the_listing_row_when_the_detail_page_is_gone(make_ctx):
    routes = dict(ROUTES)
    routes["-5123456/"] = lambda req: httpx.Response(404, text="not found")
    ctx = make_ctx(routes, options={"queries": ["junior-developer"], "max_pages": 1,
                                    "fetch_details": True, "max_details": 1})
    jobs = list(Bayt().fetch(ctx))
    assert len(jobs) == 3 and jobs[0].description is None


def test_bayt_keeps_the_listing_row_when_the_detail_request_blows_up(make_ctx, caplog):
    def timeout(_req):
        raise httpx.ReadTimeout("the detail page never answered")

    routes = dict(ROUTES)
    routes["-5123456/"] = timeout
    ctx = make_ctx(routes, options={"queries": ["junior-developer"], "max_pages": 1,
                                    "fetch_details": True, "max_details": 1})
    with caplog.at_level("WARNING"):
        jobs = list(Bayt().fetch(ctx))
    assert len(jobs) == 3 and jobs[0].description is None
    assert "detail fetch failed" in caplog.text


def test_bayt_still_raises_when_the_detail_page_is_the_bot_wall(make_ctx):
    """A block on a detail page is the same run-stopping state as one on the listing."""
    routes = dict(ROUTES)
    routes["-5123456/"] = lambda req: httpx.Response(200, text="<html>Attention Required</html>")
    ctx = make_ctx(routes, options={"queries": ["junior-developer"], "max_pages": 1,
                                    "fetch_details": True, "max_details": 1})
    with pytest.raises(SourceHTTPError, match="headless browser"):
        list(Bayt().fetch(ctx))
