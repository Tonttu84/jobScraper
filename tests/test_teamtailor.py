"""Teamtailor adapter, checked against trimmed copies of the real UpCloud feeds.

``tests/fixtures/teamtailor.json`` and ``teamtailor.rss`` are the live
``upcloud.teamtailor.com`` responses with the job bodies cut to their first two
paragraphs (plus one deliberately broken JSON item).
"""

from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import httpx
import pytest

from jobscraper.sources.teamtailor import (
    Teamtailor,
    TeamtailorTenantError,
    parse_json_item,
    parse_rss,
    parse_rss_item,
)

TENANT = "upcloud"
EEST = timezone(timedelta(hours=3))


def _404(req):
    return httpx.Response(404, text="<html><body>Not found</body></html>")


def _dns_error(req):
    raise httpx.ConnectError("Name or service not known")


def test_json_feed_is_preferred_and_parsed(make_ctx):
    ctx = make_ctx(
        {f"{TENANT}.teamtailor.com/jobs.json": "teamtailor.json"},
        options={"tenants": [TENANT]},
    )
    jobs = list(Teamtailor().fetch(ctx))

    assert len(jobs) == 3  # the id/title/url-less item is skipped, not raised
    j = jobs[0]
    assert j.source == "teamtailor"
    # JSON Feed ids are the job's UUID; the numeric id only appears in the URL slug
    assert j.source_id == "upcloud:d8938509-ff5a-4755-a5f7-5f89a0ac791e"
    assert j.title == "Business Intelligence Analyst"
    assert j.company == "UpCloud"  # _jobposting.hiringOrganization.name
    assert j.url == "https://upcloud.teamtailor.com/jobs/8323453-business-intelligence-analyst"
    assert j.country == "FI" and j.city == "Helsinki"
    assert j.location_raw == "Helsinki, FI"
    # jobs.json carries no jobLocationType / remoteStatus: a posting with a real address
    # is reported on-site even when the RSS feed calls the same job hybrid or fully remote.
    assert j.remote == "onsite"
    assert j.employment_type is None  # no employmentType in any observed _jobposting
    assert j.tags == []  # department/role live in the RSS feed only
    assert "join UpCloud" in j.description and "<" not in j.description
    assert j.posted_at == datetime(2026, 9, 4, 14, 50, 13, tzinfo=EEST)

    # a fully remote role still looks on-site in jobs.json (only the street address says "Europe")
    assert jobs[1].title == "Full Stack Developer"
    assert jobs[1].remote == "onsite"
    # the first of several jobLocation entries wins
    assert jobs[2].city == "Helsinki" and jobs[2].country == "FI"

    # only jobs.json was requested — no needless RSS round-trip
    assert [str(c.url) for c in ctx.http.calls] == [
        f"https://{TENANT}.teamtailor.com/jobs.json"
    ]


def test_falls_back_to_rss_on_404(make_ctx):
    ctx = make_ctx(
        {
            f"{TENANT}.teamtailor.com/jobs.json": _404,
            f"{TENANT}.teamtailor.com/jobs.rss": "teamtailor.rss",
        },
        options={"tenants": [TENANT]},
    )
    jobs = list(Teamtailor().fetch(ctx))

    assert len(jobs) == 2
    j = jobs[0]
    assert j.source_id == "upcloud:8323453"  # numeric id from the URL, not the guid
    assert j.title == "Business Intelligence Analyst"
    assert j.company == "upcloud"  # RSS has no hiring organization → slug
    assert j.url == "https://upcloud.teamtailor.com/jobs/8323453-business-intelligence-analyst"
    assert j.location_raw == "Helsinki, Finland"
    assert j.country == "FI" and j.city == "Helsinki"
    assert j.remote == "hybrid"  # remoteStatus == hybrid
    assert j.tags == ["Finance", "Business Intelligence Analyst"]  # tt:department, tt:role
    assert "join UpCloud" in j.description and "<" not in j.description
    assert j.posted_at == datetime(2026, 9, 4, 14, 50, 13, tzinfo=EEST)

    remote_job = jobs[1]
    assert remote_job.title == "Full Stack Developer"
    assert remote_job.remote == "remote"  # remoteStatus == fully
    assert remote_job.city == "Helsinki"  # tt:city wins over the tt:name "EU"
    assert remote_job.tags == ["Product Engineering", "Full Stack Developer"]
    assert remote_job.posted_at == datetime(2026, 6, 23, 16, 35, 8, tzinfo=EEST)

    assert [str(c.url) for c in ctx.http.calls] == [
        f"https://{TENANT}.teamtailor.com/jobs.json",
        f"https://{TENANT}.teamtailor.com/jobs.rss?per_page=200",
    ]


def test_falls_back_to_rss_when_body_is_not_json(make_ctx):
    ctx = make_ctx(
        {
            f"{TENANT}.teamtailor.com/jobs.json": "<html>login</html>",
            f"{TENANT}.teamtailor.com/jobs.rss": "teamtailor.rss",
        },
        options={"tenants": [TENANT]},
    )
    assert len(list(Teamtailor().fetch(ctx))) == 2


def _rss_item(body: str) -> ET.Element:
    return ET.fromstring(
        f'<item xmlns:tt="https://teamtailor.com/locations">{body}</item>'
    )


def test_rss_item_without_a_link_is_skipped():
    assert parse_rss_item(_rss_item("<title>Ghost</title>"), TENANT) is None


def test_rss_item_without_a_title_is_skipped():
    link = "https://upcloud.teamtailor.com/jobs/1-x"
    assert parse_rss_item(_rss_item(f"<link>{link}</link>"), TENANT) is None


def test_rss_falls_back_to_the_location_name_and_the_title():
    """No tt:city/tt:country and an unknown remoteStatus: name + title carry the location."""
    job = parse_rss_item(
        _rss_item(
            "<title>Backend Developer (Remote)</title>"
            "<link>https://upcloud.teamtailor.com/jobs/9-backend</link>"
            "<remoteStatus>temporary</remoteStatus>"
            "<tt:locations><tt:location><tt:name>Stockholm</tt:name></tt:location></tt:locations>"
        ),
        TENANT,
    )
    assert job is not None
    assert job.city == "Stockholm" and job.country == "SE"
    assert job.remote == "remote"  # from the title, no usable remoteStatus


def test_one_dead_tenant_does_not_stop_the_others(make_ctx, caplog):
    ctx = make_ctx(
        {
            "dead.teamtailor.com": _dns_error,
            f"{TENANT}.teamtailor.com/jobs.json": "teamtailor.json",
        },
        options={"tenants": ["dead", TENANT]},
    )
    jobs = list(Teamtailor().fetch(ctx))
    assert len(jobs) == 3
    assert all(j.source_id.startswith("upcloud:") for j in jobs)


def test_all_tenants_failing_raises(make_ctx):
    ctx = make_ctx({"teamtailor.com": _404}, options={"tenants": ["dead1", "dead2"]})
    with pytest.raises(RuntimeError, match="all 2 tenants failed"):
        list(Teamtailor().fetch(ctx))


def test_invalid_slug_is_never_requested(make_ctx):
    ctx = make_ctx({}, options={"tenants": ["evil.com/../x"]})
    with pytest.raises(RuntimeError, match="all 1 tenants failed"):
        list(Teamtailor().fetch(ctx))
    assert ctx.http.calls == []


def test_missing_tenants_option_raises(make_ctx):
    with pytest.raises(ValueError, match="no tenant slugs"):
        list(Teamtailor().fetch(make_ctx({})))


def _json_item(posting: dict) -> dict:
    return {
        "id": "x",
        "title": "Dev",
        "url": "https://upcloud.teamtailor.com/jobs/1-dev",
        "_jobposting": posting,
    }


def test_json_feed_reads_every_shape_of_job_location():
    """One address object instead of a list, and a nested schema.org Country."""
    job = parse_json_item(
        _json_item(
            {"jobLocation": {"address": {"addressLocality": "Oslo",
                                         "addressCountry": {"name": "Norway"}}}}
        ),
        TENANT,
    )
    assert job.city == "Oslo" and job.country == "NO"
    assert job.location_raw == "Oslo, Norway" and job.remote == "onsite"


def test_json_feed_survives_job_locations_it_cannot_read():
    job = parse_json_item(
        _json_item({"jobLocation": ["not an object", {"address": "Helsinki"}, {"address": {}}]}),
        TENANT,
    )
    assert job.city is None and job.country is None and job.location_raw is None
    assert job.company == TENANT  # no hiringOrganization: the slug stands in
    assert job.remote == "unknown"  # no address at all, so nothing says on-site


def test_rss_that_is_not_a_feed_is_reported():
    with pytest.raises(TeamtailorTenantError, match="not XML"):
        parse_rss("<html><body>login", TENANT)
    with pytest.raises(TeamtailorTenantError, match="is not <rss>"):
        parse_rss("<html><body>login</body></html>", TENANT)


def test_falls_back_to_rss_when_the_json_feed_has_no_items(make_ctx, caplog):
    ctx = make_ctx(
        {
            f"{TENANT}.teamtailor.com/jobs.json": {"version": "https://jsonfeed.org/version/1.1"},
            f"{TENANT}.teamtailor.com/jobs.rss": "teamtailor.rss",
        },
        options={"tenants": [TENANT]},
    )
    with caplog.at_level("INFO"):
        assert len(list(Teamtailor().fetch(ctx))) == 2
    assert "no items[]" in caplog.text


def test_limit_stops_early(make_ctx):
    ctx = make_ctx(
        {f"{TENANT}.teamtailor.com/jobs.json": "teamtailor.json"},
        options={"tenants": [TENANT, "other"]},
        limit=1,
    )
    assert len(list(Teamtailor().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1  # second tenant never requested
