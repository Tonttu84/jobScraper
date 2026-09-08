from datetime import UTC, datetime

from jobscraper.sources.arbeitnow import Arbeitnow


def test_arbeitnow_parses_fixture(make_ctx):
    ctx = make_ctx({"job-board-api": "arbeitnow.json"}, options={"max_pages": 1})
    jobs = list(Arbeitnow().fetch(ctx))
    assert len(jobs) == 3  # the broken record is skipped, not raised

    j = jobs[0]
    assert j.source == "arbeitnow"
    assert j.source_id == "senior-python-engineer-platform-libraries-munchen-363975"
    assert j.title == "Senior Python Engineer, Platform Libraries (m/f/d)"
    assert j.company == "Unitelabs"
    assert j.url.endswith("/unitelabs/senior-python-engineer-platform-libraries-munchen-363975")
    assert j.country == "DE" and j.city == "München"
    assert j.remote == "onsite"
    assert "Senior Python Engineer" in j.description and "<" not in j.description
    assert j.tags[:2] == ["Python", "Developer"]
    assert j.employment_type == "Experienced, Permanent, Full time"
    assert j.posted_at == datetime(2026, 9, 7, 2, 9, 2, tzinfo=UTC)


def test_arbeitnow_falls_back_to_germany_for_unknown_towns(make_ctx):
    ctx = make_ctx({"job-board-api": "arbeitnow.json"}, options={"max_pages": 1})
    j = list(Arbeitnow().fetch(ctx))[1]
    assert j.title == "DevOps Engineer (m/w/d)"
    assert j.location_raw == "Leipzig, Sachsen"
    assert j.city == "Leipzig" and j.country == "DE"
    assert j.posted_at == datetime(2026, 9, 6, 23, 5, 29, tzinfo=UTC)


def test_arbeitnow_remote_location_is_not_a_german_city(make_ctx):
    """``location`` is often just "Remote"/"Remote job" — that is not a place in Germany."""
    ctx = make_ctx({"job-board-api": "arbeitnow.json"}, options={"max_pages": 1})
    j = list(Arbeitnow().fetch(ctx))[2]
    assert j.title == "Core Developer - Platform"
    assert j.company == "Parity"
    assert j.remote == "remote"
    assert j.location_raw == "Remote"
    assert j.city is None
    assert j.country is None
    assert j.posted_at == datetime(2026, 9, 6, 13, 40, 5, tzinfo=UTC)


def test_arbeitnow_respects_limit(make_ctx):
    ctx = make_ctx({"job-board-api": "arbeitnow.json"}, options={"max_pages": 3}, limit=1)
    assert len(list(Arbeitnow().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1
