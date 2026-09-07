from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jobscraper.http import SourceHTTPError
from jobscraper.sources.bayt import Bayt, parse_posted, slugify

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
