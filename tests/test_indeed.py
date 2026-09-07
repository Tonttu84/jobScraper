"""indeed adapter — jobspy does the HTTP, so ``scrape_jobs`` is monkeypatched."""

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from conftest import fixture_json

from jobscraper.sources import indeed as mod
from jobscraper.sources.indeed import Indeed, country_iso


def frame() -> pd.DataFrame:
    df = pd.DataFrame(fixture_json("indeed_jobs.json"))
    # jobspy leaves missing descriptions/locations as NaN, not None
    df.loc[df["id"] == "in-d4e5f6", ["description", "location", "is_remote"]] = np.nan
    return df


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(mod, "sleep", slept.append)
    return slept


def patch_scrape(monkeypatch, results):
    """results: a DataFrame, or a list of per-call DataFrames/exceptions."""
    calls: list[dict] = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        item = results if isinstance(results, pd.DataFrame) else results[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(mod, "scrape_jobs", fake_scrape_jobs)
    return calls


def test_parses_dataframe(monkeypatch, make_ctx):
    calls = patch_scrape(monkeypatch, frame())
    ctx = make_ctx({}, options={"queries": ["junior developer"], "countries": ["finland"]})
    jobs = list(Indeed().fetch(ctx))

    # 4 rows in, 2 out: one duplicate apply URL and one title-less row are dropped
    assert len(jobs) == 2
    j = jobs[0]
    assert j.source == "indeed" and j.source_id == "in-a1b2c3"
    assert j.url == "https://careers.nordicsystems.fi/apply/a1b2c3"  # direct URL wins
    assert j.title == "Junior Software Developer" and j.company == "Nordic Systems"
    assert j.country == "FI" and j.city == "Helsinki"
    assert j.remote == "onsite"
    assert j.employment_type == "fulltime"
    assert j.salary_text == "3 000–3 800 EUR monthly"
    assert j.posted_at == datetime(2026, 9, 4, tzinfo=UTC)
    assert j.description.startswith("**Nordic Systems**")
    assert j.raw["query_country"] == "finland"

    nan_row = jobs[1]
    assert nan_row.description is None  # NaN → None
    assert nan_row.location_raw is None  # NaN → None
    assert nan_row.remote == "unknown"  # NaN is not a usable flag
    assert nan_row.country == "FI"  # no location text → the queried country
    assert nan_row.url == "https://ee.indeed.com/viewjob?jk=d4e5f6"
    assert nan_row.salary_text is None

    assert calls[0]["site_name"] == ["indeed"]
    assert calls[0]["search_term"] == "junior developer"
    assert calls[0]["country_indeed"] == "finland"
    assert calls[0]["location"] is None  # the country picks the Indeed domain
    assert calls[0]["results_wanted"] == 40 and calls[0]["hours_old"] == 168
    assert calls[0]["description_format"] == "markdown"


def test_country_name_maps_to_iso():
    assert country_iso("estonia") == "EE"
    assert country_iso("united arab emirates") == "AE"
    assert country_iso("uk") == "GB"
    assert country_iso("Finland ") == "FI"
    assert country_iso("atlantis") is None


def test_loops_countries_and_can_send_the_country_as_location(monkeypatch, make_ctx, no_sleep):
    calls = patch_scrape(monkeypatch, [frame(), frame(), frame(), frame()])
    ctx = make_ctx(
        {},
        options={
            "queries": ["a", "b"],
            "countries": ["finland", "estonia"],
            "delay": 0.25,
            "use_country_as_location": True,
            "results_per_query": 15,
            "hours_old": 72,
        },
    )
    jobs = list(Indeed().fetch(ctx))

    assert len(calls) == 4
    assert no_sleep == [0.25, 0.25, 0.25]  # between calls only
    assert len(jobs) == 2  # same rows every time → deduped by URL
    assert [(c["search_term"], c["country_indeed"], c["location"]) for c in calls] == [
        ("a", "finland", "finland"), ("a", "estonia", "estonia"),
        ("b", "finland", "finland"), ("b", "estonia", "estonia"),
    ]
    assert calls[0]["results_wanted"] == 15 and calls[0]["hours_old"] == 72


def test_one_failing_country_is_skipped(monkeypatch, make_ctx):
    patch_scrape(monkeypatch, [ValueError("Invalid country string"), frame()])
    ctx = make_ctx({}, options={"queries": ["a"], "countries": ["atlantis", "finland"]})
    assert len(list(Indeed().fetch(ctx))) == 2


def test_all_calls_failing_raises(monkeypatch, make_ctx):
    patch_scrape(monkeypatch, [RuntimeError("403"), RuntimeError("403")])
    ctx = make_ctx({}, options={"queries": ["a"], "countries": ["finland", "estonia"]})
    with pytest.raises(RuntimeError, match="all 2 jobspy calls failed"):
        list(Indeed().fetch(ctx))


def test_empty_frame_is_not_a_failure(monkeypatch, make_ctx):
    patch_scrape(monkeypatch, pd.DataFrame())
    ctx = make_ctx({}, options={"queries": ["a"], "countries": ["finland"]})
    assert list(Indeed().fetch(ctx)) == []


def test_limit_stops_early(monkeypatch, make_ctx):
    calls = patch_scrape(monkeypatch, frame())
    ctx = make_ctx({}, options={"queries": ["a"], "countries": ["finland", "estonia"]}, limit=1)
    assert len(list(Indeed().fetch(ctx))) == 1
    assert len(calls) == 1  # second country never queried


def test_missing_options_raise(make_ctx):
    with pytest.raises(ValueError, match="countries"):
        list(Indeed().fetch(make_ctx({}, options={"queries": ["a"]})))
