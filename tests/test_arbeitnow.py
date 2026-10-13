from datetime import UTC, datetime

import pytest

from jobscraper.sources.arbeitnow import Arbeitnow


def test_arbeitnow_parses_fixture(make_ctx):
    ctx = make_ctx({"job-board-api": "arbeitnow.json"}, options={"max_pages": 1})
    jobs = list(Arbeitnow().fetch(ctx))
    assert len(jobs) == 8  # the broken record is skipped, not raised

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


def test_arbeitnow_city_with_german_state_is_germany(make_ctx):
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


@pytest.mark.parametrize(
    ("slug", "location", "country", "city"),
    [
        # Arbeitnow lists jobs across Europe: a UK or Swiss town is not Germany.
        ("software-engineer-solihull-512001", "Solihull", "GB", "Solihull"),
        ("backend-developer-geneva-512002", "Geneva", "CH", "Geneva"),
        # A German city the table used to miss (it was only DE through the blanket default).
        ("python-developer-karlsruhe-512003", "Karlsruhe", "DE", "Karlsruhe"),
        # An unknown town is placed by its German state suffix.
        ("data-engineer-ottobrunn-bavaria-512004", "Ottobrunn, Bavaria", "DE", "Ottobrunn"),
        # A place nothing recognises stays unknown instead of being guessed as Germany.
        ("frontend-developer-burghaun-512005", "Burghaun", None, "Burghaun"),
    ],
)
def test_arbeitnow_country_comes_from_the_location_not_a_default(make_ctx, slug, location, country, city):
    ctx = make_ctx({"job-board-api": "arbeitnow.json"}, options={"max_pages": 1})
    j = next(job for job in Arbeitnow().fetch(ctx) if job.source_id == slug)
    assert j.title and j.company
    assert j.url.endswith(f"/{slug}")
    assert j.location_raw == location
    assert j.country == country and j.city == city
    assert j.remote == "onsite"
    assert j.posted_at is not None and j.posted_at.tzinfo is not None


def test_arbeitnow_respects_limit(make_ctx):
    ctx = make_ctx({"job-board-api": "arbeitnow.json"}, options={"max_pages": 3}, limit=1)
    assert len(list(Arbeitnow().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1
