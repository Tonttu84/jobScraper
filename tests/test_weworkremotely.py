from datetime import UTC, datetime

import pytest

from jobscraper.http import SourceHTTPError
from jobscraper.sources.weworkremotely import WeWorkRemotely, split_company_title

FEED = "https://weworkremotely.com/categories/remote-programming-jobs.rss"
FEED2 = "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss"


def test_weworkremotely_parses_fixture(make_ctx):
    ctx = make_ctx({"remote-programming-jobs.rss": "weworkremotely.xml"}, options={"feeds": [FEED]})
    jobs = list(WeWorkRemotely().fetch(ctx))

    assert len(jobs) == 3  # the empty item is skipped
    j = jobs[0]
    assert j.source == "weworkremotely"
    assert j.title == "Junior Ruby on Rails Developer"
    assert j.company == "Aurora Metrics"  # split off the "Company: Title" RSS title
    assert j.url.endswith("/aurora-metrics-junior-ruby-on-rails-developer")
    assert j.source_id == j.url
    assert j.remote == "remote"
    assert j.remote_region == "Europe Only" and j.country is None
    assert j.employment_type == "Full-Time"
    assert j.tags == ["Back-End Programming", "Ruby", "Rails", "PostgreSQL"]
    assert "junior Rails developer" in j.description and "<" not in j.description
    assert j.posted_at == datetime(2026, 9, 1, 9, 12, tzinfo=UTC)

    assert jobs[1].remote_region == "Anywhere in the World" and jobs[1].country is None
    assert jobs[2].company is None  # title without the "Company: " prefix
    assert jobs[2].country == "DE"  # region "Germany Only"


def test_weworkremotely_dedupes_across_feeds(make_ctx):
    ctx = make_ctx(
        {"weworkremotely.com/categories/": "weworkremotely.xml"}, options={"feeds": [FEED, FEED2]}
    )
    jobs = list(WeWorkRemotely().fetch(ctx))

    assert len(ctx.http.calls) == 2
    assert len(jobs) == 3  # same items in both feeds, deduped by link


def test_weworkremotely_raises_on_broken_feed(make_ctx):
    routes = {"weworkremotely.com": "<rss><channel><item>truncated"}
    ctx = make_ctx(routes, options={"feeds": [FEED]})
    with pytest.raises(SourceHTTPError):
        list(WeWorkRemotely().fetch(ctx))


def test_weworkremotely_respects_limit(make_ctx):
    ctx = make_ctx(
        {"remote-programming-jobs.rss": "weworkremotely.xml"}, options={"feeds": [FEED]}, limit=1
    )
    assert len(list(WeWorkRemotely().fetch(ctx))) == 1


def test_split_company_title():
    assert split_company_title("Acme: Backend Engineer") == ("Acme", "Backend Engineer")
    assert split_company_title("Backend Engineer") == (None, "Backend Engineer")
