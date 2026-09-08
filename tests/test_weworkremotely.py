"""weworkremotely fixture = a trimmed copy of the real Full-Stack Programming RSS feed (2026-09-07).

The leaf category feeds are the ones worth reading: they carry ``country``, ``state``,
``skills``, ``type`` and ``expires_at``, which the parent ``remote-programming-jobs.rss``
feed omits.
"""

from datetime import UTC, datetime

import pytest

from jobscraper.http import SourceHTTPError
from jobscraper.sources.weworkremotely import (
    FEEDS,
    WeWorkRemotely,
    skill_tags,
    split_company_title,
)

FEED = "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss"
DEVOPS = "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss"
ROUTE = {"remote-full-stack-programming-jobs.rss": "weworkremotely.xml"}


def test_weworkremotely_parses_fixture(make_ctx):
    ctx = make_ctx(ROUTE, options={"feeds": [FEED]})
    jobs = list(WeWorkRemotely().fetch(ctx))

    assert len(jobs) == 3  # the item without link and guid is skipped
    j = jobs[0]
    assert j.source == "weworkremotely"
    assert j.title == "Director of Production Engineering"
    assert j.company == "Legion"  # split off the "Company: Title" RSS title
    assert j.url == "https://weworkremotely.com/remote-jobs/legion-director-of-production-engineering"
    assert j.source_id == j.url
    assert j.remote == "remote"
    assert j.remote_region == "Anywhere in the World"
    assert j.country == "US"  # from <country> 🇺🇸 United States of America
    assert j.employment_type == "Full-Time"
    assert j.tags == ["Full-Stack Programming"]
    assert "Director of Engineering, DevOps & SRE" in j.description and "<" not in j.description
    assert j.posted_at == datetime(2026, 9, 7, 7, 30, 54, tzinfo=UTC)


def test_weworkremotely_maps_skills_and_multi_country_items(make_ctx):
    ctx = make_ctx(ROUTE, options={"feeds": [FEED]})
    lemon, ateam = list(WeWorkRemotely().fetch(ctx))[1:]

    assert lemon.company == "Lemon.io"
    assert lemon.country is None  # <country> lists seven of them: a region, not a country
    # <skills> is a prose list ending in ", and X" — the trailing "and" is not a skill
    assert lemon.tags == [
        "Full-Stack Programming",
        "Node.js",
        "React",
        "Engineer",
        "Developer",
        "Full Stack Dev",
        "Mobile Development",
    ]

    assert ateam.title == "Senior Independent Software Developer ($90-$170/hr)"
    assert ateam.employment_type == "Contract"
    assert ateam.country is None
    assert ateam.posted_at == datetime(2024, 6, 16, 17, 30, 51, tzinfo=UTC)


def test_weworkremotely_reads_the_leaf_category_feeds(make_ctx):
    # The parent remote-programming-jobs.rss feed is capped at 25 items and drops
    # country/state/skills/type, so it must not shadow the leaf feeds' richer items.
    assert "remote-programming-jobs.rss" not in [f.rsplit("/", 1)[-1] for f in FEEDS]
    assert DEVOPS in FEEDS

    ctx = make_ctx({"weworkremotely.com/categories/": "weworkremotely.xml"})
    jobs = list(WeWorkRemotely().fetch(ctx))

    assert len(ctx.http.calls) == len(FEEDS)
    assert len(jobs) == 3  # same items in every feed, deduped by link


def test_weworkremotely_raises_on_broken_feed(make_ctx):
    routes = {"weworkremotely.com": "<rss><channel><item>truncated"}
    ctx = make_ctx(routes, options={"feeds": [FEED]})
    with pytest.raises(SourceHTTPError):
        list(WeWorkRemotely().fetch(ctx))


def test_weworkremotely_respects_limit(make_ctx):
    ctx = make_ctx(ROUTE, options={"feeds": [FEED]}, limit=1)
    assert len(list(WeWorkRemotely().fetch(ctx))) == 1


def test_skill_tags():
    assert skill_tags("Node.js, React, and Mobile Development") == [
        "Node.js",
        "React",
        "Mobile Development",
    ]
    assert skill_tags("") == []
    assert skill_tags(None) == []


def test_split_company_title():
    assert split_company_title("Acme: Backend Engineer") == ("Acme", "Backend Engineer")
    assert split_company_title("Backend Engineer") == (None, "Backend Engineer")
