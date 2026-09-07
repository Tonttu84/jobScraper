from datetime import UTC, datetime

import httpx

from jobscraper.sources.thehub import TheHub

LIST = "api/v2/jobs"
DETAIL = "api/jobs/"


def test_thehub_parses_fixture(make_ctx):
    ctx = make_ctx(
        {LIST: "thehub.json", DETAIL: "thehub_detail.json"},
        options={"countries": ["FI"], "max_pages": 1},
    )
    jobs = list(TheHub().fetch(ctx))

    # 4 docs in, 2 out: the EXPIRED one and the id/title-less one are dropped.
    assert len(jobs) == 2
    j = jobs[0]
    assert j.source == "thehub"
    assert j.source_id == "68b0c1d2e3f4a5b6c7d8e9f0"
    assert j.url == "https://thehub.io/jobs/68b0c1d2e3f4a5b6c7d8e9f0"
    assert j.title == "Junior Backend Developer"
    assert j.company == "Nordic Startup Oy"
    assert j.country == "FI" and j.city == "Mannerheimintie 12"
    assert j.remote == "onsite"
    assert j.employment_type == "Full-time"
    assert j.salary_text == "competitive"
    assert "Go" in j.description and "<p>" not in j.description
    assert j.posted_at == datetime(2026, 9, 3, 7, 0, tzinfo=UTC)
    assert j.raw["apply_url"] == "https://jobs.lever.co/nordic-startup/junior-backend"


def test_thehub_hydrates_missing_description(make_ctx):
    ctx = make_ctx(
        {LIST: "thehub.json", DETAIL: "thehub_detail.json"},
        options={"countries": ["FI"], "max_pages": 1},
    )
    remote_job = list(TheHub().fetch(ctx))[1]

    assert remote_job.source_id == "68b0c1d2e3f4a5b6c7d8e9f1"
    assert remote_job.remote == "remote"
    assert remote_job.country == "SE"
    assert "React" in remote_job.description  # only available from the detail endpoint
    assert remote_job.salary_text == "500000 - 650000 SEK"
    assert "apply_url" not in remote_job.raw  # link='' is not a usable apply URL
    detail_calls = [c for c in ctx.http.calls if "api/jobs/" in str(c.url)]
    assert len(detail_calls) == 1  # only the doc without a description is hydrated


def test_thehub_survives_a_broken_detail_endpoint(make_ctx):
    ctx = make_ctx(
        {LIST: "thehub.json", DETAIL: lambda req: httpx.Response(500, text="boom")},
        options={"countries": ["FI"], "max_pages": 1},
    )
    jobs = list(TheHub().fetch(ctx))
    assert len(jobs) == 2 and jobs[1].description is None


def test_thehub_iterates_countries_and_dedupes(make_ctx):
    ctx = make_ctx(
        {LIST: "thehub.json", DETAIL: "thehub_detail.json"},
        options={"countries": ["FI", "SE"], "max_pages": 1, "search": "developer"},
    )
    jobs = list(TheHub().fetch(ctx))
    assert len(jobs) == 2  # same ids on both country pages, emitted once
    list_calls = [c for c in ctx.http.calls if "api/v2/jobs" in str(c.url)]
    assert len(list_calls) == 2
    assert "countryCode=FI" in str(list_calls[0].url) and "search=developer" in str(list_calls[0].url)


def test_thehub_respects_limit(make_ctx):
    ctx = make_ctx(
        {LIST: "thehub.json", DETAIL: "thehub_detail.json"},
        options={"countries": ["FI", "SE"], "max_pages": 5},
        limit=1,
    )
    assert len(list(TheHub().fetch(ctx))) == 1
