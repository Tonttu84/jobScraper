from datetime import datetime, timedelta, timezone

import httpx
import pytest

from jobscraper.sources.teamtailor import Teamtailor

JSON_TENANT = "examplia"
RSS_TENANT = "rssonly"


def _404(req):
    return httpx.Response(404, text="<html><body>Not found</body></html>")


def _dns_error(req):
    raise httpx.ConnectError("Name or service not known")


def test_json_feed_is_preferred_and_parsed(make_ctx):
    ctx = make_ctx(
        {f"{JSON_TENANT}.teamtailor.com/jobs.json": "teamtailor.json"},
        options={"tenants": [JSON_TENANT]},
    )
    jobs = list(Teamtailor().fetch(ctx))

    assert len(jobs) == 2  # the title-less item is skipped, not raised
    j = jobs[0]
    assert j.source == "teamtailor"
    assert j.source_id == "examplia:1234567"
    assert j.title == "Junior Software Developer"
    assert j.company == "Examplia Oy"
    assert j.url.endswith("/jobs/1234567-junior-software-developer")
    assert j.country == "FI" and j.city == "Helsinki"
    assert j.location_raw == "Helsinki, FI"
    assert j.remote == "onsite"
    assert j.employment_type == "FULL_TIME"
    assert "Junior Developer" in j.description and "<" not in j.description
    assert j.posted_at == datetime(2026, 9, 1, 8, 30, tzinfo=timezone(timedelta(hours=3)))

    remote_job = jobs[1]
    assert remote_job.remote == "remote"  # jobLocationType == TELECOMMUTE
    assert remote_job.country == "EE"
    # only jobs.json was requested — no needless RSS round-trip
    assert [str(c.url) for c in ctx.http.calls] == [
        f"https://{JSON_TENANT}.teamtailor.com/jobs.json"
    ]


def test_falls_back_to_rss_on_404(make_ctx):
    ctx = make_ctx(
        {
            f"{RSS_TENANT}.teamtailor.com/jobs.json": _404,
            f"{RSS_TENANT}.teamtailor.com/jobs.rss": "teamtailor.rss",
        },
        options={"tenants": [RSS_TENANT]},
    )
    jobs = list(Teamtailor().fetch(ctx))

    assert len(jobs) == 2  # the link-less item is skipped
    j = jobs[0]
    assert j.source_id == "rssonly:2222222"  # numeric id from the URL, not the guid
    assert j.title == "Trainee Frontend Developer"
    assert j.company == "rssonly"  # RSS has no hiring organization → slug
    assert j.url.endswith("/jobs/2222222-trainee-frontend-developer")
    assert j.location_raw == "Tampere, Finland" and j.country == "FI" and j.city == "Tampere"
    assert j.remote == "onsite"  # remoteStatus == none
    assert j.tags == ["Engineering", "Frontend Developer"]
    assert "frontend" in j.description and "<" not in j.description
    assert j.posted_at == datetime(2026, 8, 25, 9, 30, 4, tzinfo=timezone(timedelta(hours=3)))
    assert jobs[1].remote == "remote"  # remoteStatus == fully

    assert [str(c.url) for c in ctx.http.calls] == [
        f"https://{RSS_TENANT}.teamtailor.com/jobs.json",
        f"https://{RSS_TENANT}.teamtailor.com/jobs.rss?per_page=200",
    ]


def test_falls_back_to_rss_when_body_is_not_json(make_ctx):
    ctx = make_ctx(
        {
            f"{RSS_TENANT}.teamtailor.com/jobs.json": "<html>login</html>",
            f"{RSS_TENANT}.teamtailor.com/jobs.rss": "teamtailor.rss",
        },
        options={"tenants": [RSS_TENANT]},
    )
    assert len(list(Teamtailor().fetch(ctx))) == 2


def test_one_dead_tenant_does_not_stop_the_others(make_ctx, caplog):
    ctx = make_ctx(
        {
            "dead.teamtailor.com": _dns_error,
            f"{JSON_TENANT}.teamtailor.com/jobs.json": "teamtailor.json",
        },
        options={"tenants": ["dead", JSON_TENANT]},
    )
    jobs = list(Teamtailor().fetch(ctx))
    assert len(jobs) == 2
    assert all(j.source_id.startswith("examplia:") for j in jobs)


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


def test_limit_stops_early(make_ctx):
    ctx = make_ctx(
        {f"{JSON_TENANT}.teamtailor.com/jobs.json": "teamtailor.json"},
        options={"tenants": [JSON_TENANT, RSS_TENANT]},
        limit=1,
    )
    assert len(list(Teamtailor().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1  # second tenant never requested
