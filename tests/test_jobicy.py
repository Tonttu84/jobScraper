from datetime import UTC, datetime

from jobscraper.sources.jobicy import (
    Jobicy,
    _strings,
    parse_record,
    region_country,
    salary_text,
)


def test_jobicy_parses_fixture(make_ctx):
    ctx = make_ctx({"api/v2/remote-jobs": "jobicy.json"}, options={"geos": ["europe"]})
    jobs = list(Jobicy().fetch(ctx))

    assert len(jobs) == 3  # the record without a jobTitle is skipped
    j = jobs[0]
    assert j.source == "jobicy"
    assert j.source_id == "152625"
    assert j.title == "Software Engineer II - Poland"
    assert j.company == "Housecall Pro"
    assert j.url == "https://jobicy.com/jobs/152625-software-engineer-ii-poland"
    assert j.remote == "remote" and j.remote_region == "Poland" and j.country == "PL"
    assert j.seniority_raw == "Senior"
    assert j.employment_type == "Contract"
    assert j.tags == ["Software Engineering"]
    assert j.salary_text == "5,300 - 6,600 USD/month"  # salaryPeriod is "monthly", not yearly
    assert "home service professionals" in j.description and "<" not in j.description
    assert j.posted_at == datetime(2026, 9, 6, 10, 49, 12, tzinfo=UTC)


def test_jobicy_keeps_multi_country_geos_out_of_country(make_ctx):
    ctx = make_ctx({"api/v2/remote-jobs": "jobicy.json"}, options={"geos": ["europe"]})
    jobs = list(Jobicy().fetch(ctx))

    dune = jobs[1]
    assert dune.company == "Dune"
    assert dune.remote_region == "Europe, USA" and dune.country is None
    assert dune.seniority_raw == "Entry-Level, Junior"  # jobLevel is a string, not a list
    assert dune.salary_text is None  # the salary keys are simply absent
    assert dune.posted_at == datetime(2026, 9, 3, 16, 27, 24, tzinfo=UTC)

    clickup = jobs[2]
    # the feed pads its separators: "Bulgaria,  Czechia,  ..."
    assert clickup.remote_region == "Bulgaria, Czechia, Hungary, Ireland, Poland, Ukraine"
    assert clickup.location_raw == clickup.remote_region
    assert clickup.country is None


def test_jobicy_queries_both_geos_and_dedupes(make_ctx):
    ctx = make_ctx({"api/v2/remote-jobs": "jobicy.json"})
    jobs = list(Jobicy().fetch(ctx))

    assert len(ctx.http.calls) == 2
    urls = [str(c.url) for c in ctx.http.calls]
    assert "geo=europe" in urls[0] and "geo=anywhere" in urls[1]
    assert "count=50" in urls[0] and "industry=dev" in urls[0]
    assert len(jobs) == 3  # same ids served twice, deduped by id


def test_jobicy_respects_limit(make_ctx):
    ctx = make_ctx({"api/v2/remote-jobs": "jobicy.json"}, limit=1)
    assert len(list(Jobicy().fetch(ctx))) == 1


def test_jobicy_can_be_asked_without_a_geo_or_an_industry(make_ctx):
    ctx = make_ctx(
        {"api/v2/remote-jobs": "jobicy.json"},
        options={"geos": [], "industry": "", "tag": "python"},
    )
    jobs = list(Jobicy().fetch(ctx))

    url = str(ctx.http.calls[0].url)
    assert "geo=" not in url and "industry=" not in url
    assert "tag=python" in url
    assert len(jobs) == 3


def test_jobicy_region_and_list_helpers():
    assert region_country(None) is None
    assert region_country("Anywhere in the World") is None  # not a country statement
    assert region_country("Poland") == "PL"
    assert _strings(42) == []  # jobLevel/jobType are strings or lists, never numbers
    assert parse_record(["not", "an", "object"]) is None


def test_salary_text_tolerates_junk():
    assert salary_text(50000, None, "usd", "yearly") == "50,000 USD/year"
    assert salary_text(18, 18, "USD", "hourly") == "18 - 18 USD/hour"
    assert salary_text(50000, None, "usd", None) == "50,000 USD"
    assert salary_text(None, None, "EUR", "yearly") is None
    assert salary_text("nope", None, None, None) is None
