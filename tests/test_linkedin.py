"""linkedin adapter — jobspy does the HTTP, so ``scrape_jobs`` is monkeypatched."""

from datetime import UTC, datetime

import httpx
import numpy as np
import pandas as pd
import pytest
from conftest import FakeHttp, fixture_json, fixture_text

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


# --------------------------------------------------------------- description hydration
# Search rows are title-only (``fetch_descriptions`` is off), so the ranking stage tops the
# few jobs it actually looks at up from the public guest page. The fixture is a trimmed real one.


def test_parse_description_reads_the_guest_page_markup():
    text = mod.parse_description(fixture_text("linkedin_job.html"))

    assert text.startswith("Atos Group is een wereldleider in digitale transformatie")
    assert text.endswith("Choose your future. Choose Atos.")
    assert "<" not in text and "show-more-less" not in text  # tags and class names gone
    assert "Show more" not in text  # the expand/collapse buttons are chrome, not description
    assert "Young Professionals" in text  # <strong> inside a paragraph survives
    assert "DevOps engineering" in text  # a <li> survives
    assert "Euronext Paris.\n\nHet doel van Atos Group" in text  # <br><br> -> paragraph break
    assert "\n\n\n" not in text  # but no runs of blank lines


def test_parse_description_falls_back_to_the_plain_description_block():
    html = """<html><body><div class="description__text">
        <p>We are hiring a graduate engineer.</p><p>Estonian office, English team.</p>
        <button class="show-more-less-html__button">Show more</button>
    </div></body></html>"""
    text = mod.parse_description(html)

    assert "We are hiring a graduate engineer." in text
    assert "Estonian office, English team." in text
    assert "<p>" not in text
    assert "Show more" not in text  # page chrome is stripped on this path too


def test_parse_description_returns_none_without_a_description_block():
    assert mod.parse_description("<html><body><h1>Sign in</h1></body></html>") is None
    assert mod.parse_description("") is None
    assert mod.parse_description(None) is None


def test_fetch_description_normalises_the_url_it_requests():
    http = FakeHttp({"/jobs/view/4392276998": "linkedin_job.html"})
    text = mod.fetch_description(
        http, "https://nl.linkedin.com/jobs/view/4392276998?refId=abc&trk=public_jobs&position=3"
    )

    assert text.startswith("Atos Group is een wereldleider")
    # the country subdomain and the tracking params are dropped before the request
    assert [str(c.url) for c in http.calls] == ["https://www.linkedin.com/jobs/view/4392276998"]


def test_fetch_description_ignores_non_linkedin_urls():
    http = FakeHttp({})  # any request at all would raise
    assert mod.fetch_description(http, "https://careers.examplia.fi/jobs/junior-dev") is None
    assert mod.fetch_description(http, "https://www.linkedin.com/company/atos") is None
    assert mod.fetch_description(http, "") is None
    assert http.calls == []


def test_fetch_description_returns_none_on_429():
    http = FakeHttp({"/jobs/view/": lambda req: httpx.Response(429, text="Too Many Requests")})
    assert mod.fetch_description(http, "https://www.linkedin.com/jobs/view/1111111111") is None


def test_fetch_description_returns_none_on_an_authwall():
    """LinkedIn walls a request off with a 200 + a redirect to /authwall, not with a 4xx.

    FakeHttp rewrites ``response.request`` to the request it built, so it cannot model a
    followed redirect; this needs a client that lands somewhere other than where it aimed.
    """

    class Redirecting:
        def get(self, url: str):
            landed = httpx.Request("GET", "https://www.linkedin.com/authwall?sessionRedirect=x")
            # An authwall page still carries a `description` shell, so the URL is what gives it away.
            body = '<html><body><section class="description">Sign in to view</section></body></html>'
            return httpx.Response(200, text=body, request=landed)

    assert mod.fetch_description(Redirecting(), "https://www.linkedin.com/jobs/view/1111111111") is None


def test_fetch_description_returns_none_when_the_block_is_missing():
    http = FakeHttp({"/jobs/view/": "<html><body><h1>Junior Developer</h1></body></html>"})
    assert mod.fetch_description(http, "https://www.linkedin.com/jobs/view/1111111111") is None


def test_fetch_description_never_raises_on_transport_errors():
    def boom(req):
        raise httpx.ConnectError("connection reset")

    http = FakeHttp({"/jobs/view/": boom})
    assert mod.fetch_description(http, "https://www.linkedin.com/jobs/view/1111111111") is None
