"""linkedin adapter — jobspy does the HTTP, so ``scrape_jobs`` is monkeypatched."""

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from conftest import fixture_json

from jobscraper.sources import linkedin as mod
from jobscraper.sources.linkedin import LinkedIn


def frame() -> pd.DataFrame:
    df = pd.DataFrame(fixture_json("linkedin_jobs.json"))
    # jobspy leaves missing descriptions as NaN, not None
    df.loc[df["id"] == "li-2222222222", "description"] = np.nan
    return df


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Never pace for real in tests; hand back the recorded delays."""
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
    ctx = make_ctx({}, options={"queries": ["junior developer"], "locations": ["Finland"]})
    jobs = list(LinkedIn().fetch(ctx))

    # 4 rows in, 2 out: one duplicate apply URL and one title-less row are dropped
    assert len(jobs) == 2
    j = jobs[0]
    assert j.source == "linkedin" and j.source_id == "li-1111111111"
    assert j.url == "https://careers.examplia.fi/jobs/junior-software-developer"  # direct URL wins
    assert j.title == "Junior Software Developer" and j.company == "Examplia"
    assert j.country == "FI" and j.city == "Helsinki"
    assert j.remote == "onsite"
    assert j.employment_type == "fulltime"
    assert j.salary_text == "3 500–4 200 EUR monthly"
    assert j.posted_at == datetime(2026, 9, 3, tzinfo=UTC)
    assert j.description.startswith("## About the role")
    assert j.raw["query_location"] == "Finland"

    remote_job = jobs[1]
    assert remote_job.description is None  # NaN → None
    assert remote_job.remote == "remote"  # is_remote flag
    assert remote_job.country == "EE"
    assert remote_job.url == "https://www.linkedin.com/jobs/view/2222222222"  # no direct URL
    assert remote_job.salary_text is None

    assert calls[0]["site_name"] == ["linkedin"]
    assert calls[0]["search_term"] == "junior developer"
    assert calls[0]["location"] == "Finland"
    assert calls[0]["results_wanted"] == 40 and calls[0]["hours_old"] == 168
    assert calls[0]["linkedin_fetch_description"] is False  # slow + rate-limited by default
    assert calls[0]["description_format"] == "markdown"


def test_country_falls_back_to_the_queried_location(monkeypatch, make_ctx):
    df = frame()
    df.loc[:, "location"] = "Greater Helsinki Metropolitan Area"
    patch_scrape(monkeypatch, df)
    ctx = make_ctx({}, options={"queries": ["dev"], "locations": ["Finland"]})
    assert {j.country for j in LinkedIn().fetch(ctx)} == {"FI"}


def test_paces_and_dedupes_across_calls(monkeypatch, make_ctx, no_sleep):
    calls = patch_scrape(monkeypatch, [frame(), frame(), frame(), frame()])
    ctx = make_ctx(
        {},
        options={
            "queries": ["a", "b"],
            "locations": ["Finland", "Estonia"],
            "delay": 0.5,
            "fetch_descriptions": True,
            "results_per_query": 10,
            "hours_old": 24,
        },
    )
    jobs = list(LinkedIn().fetch(ctx))

    assert len(calls) == 4
    assert no_sleep == [0.5, 0.5, 0.5]  # between calls only
    assert len(jobs) == 2  # same rows every time → deduped by URL
    assert calls[0]["linkedin_fetch_description"] is True
    assert calls[0]["results_wanted"] == 10 and calls[0]["hours_old"] == 24
    assert [(c["search_term"], c["location"]) for c in calls] == [
        ("a", "Finland"), ("a", "Estonia"), ("b", "Finland"), ("b", "Estonia")
    ]


def test_one_failing_call_is_skipped(monkeypatch, make_ctx):
    patch_scrape(monkeypatch, [RuntimeError("429 Too Many Requests"), frame()])
    ctx = make_ctx({}, options={"queries": ["a"], "locations": ["Berlin", "Finland"]})
    assert len(list(LinkedIn().fetch(ctx))) == 2


def test_all_calls_failing_raises(monkeypatch, make_ctx):
    patch_scrape(monkeypatch, [RuntimeError("429"), RuntimeError("429")])
    ctx = make_ctx({}, options={"queries": ["a"], "locations": ["Berlin", "Finland"]})
    with pytest.raises(RuntimeError, match="all 2 jobspy calls failed"):
        list(LinkedIn().fetch(ctx))


def test_empty_frame_is_not_a_failure(monkeypatch, make_ctx):
    patch_scrape(monkeypatch, pd.DataFrame())
    ctx = make_ctx({}, options={"queries": ["a"], "locations": ["Finland"]})
    assert list(LinkedIn().fetch(ctx)) == []


def test_limit_stops_early(monkeypatch, make_ctx):
    calls = patch_scrape(monkeypatch, frame())
    ctx = make_ctx({}, options={"queries": ["a"], "locations": ["Finland", "Estonia"]}, limit=1)
    assert len(list(LinkedIn().fetch(ctx))) == 1
    assert len(calls) == 1  # second location never queried


def test_missing_options_raise(make_ctx):
    with pytest.raises(ValueError, match="queries"):
        list(LinkedIn().fetch(make_ctx({}, options={"locations": ["Finland"]})))
