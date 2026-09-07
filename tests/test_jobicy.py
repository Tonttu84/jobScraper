from datetime import UTC, datetime

from jobscraper.sources.jobicy import Jobicy, salary_text


def test_jobicy_parses_fixture(make_ctx):
    ctx = make_ctx({"api/v2/remote-jobs": "jobicy.json"}, options={"geos": ["europe"]})
    jobs = list(Jobicy().fetch(ctx))

    assert len(jobs) == 2  # the record without a jobTitle is skipped
    j = jobs[0]
    assert j.source == "jobicy"
    assert j.source_id == "331902"
    assert j.title == "Junior Frontend Developer"
    assert j.company == "Helio Interactive"
    assert j.url == "https://jobicy.com/jobs/331902-junior-frontend-developer"
    assert j.remote == "remote" and j.remote_region == "Europe" and j.country is None
    assert j.seniority_raw == "Junior"
    assert j.employment_type == "full-time"
    assert j.tags == ["Software Engineering", "Web Development"]
    assert j.salary_text == "42,000 - 55,000 EUR"
    assert "React" in j.description and "<" not in j.description
    assert j.posted_at == datetime(2026, 8, 30, 7, 41, 2, tzinfo=UTC)

    assert jobs[1].country == "DE"  # jobGeo "Germany"
    assert jobs[1].employment_type == "full-time, contract"
    assert jobs[1].salary_text is None


def test_jobicy_queries_both_geos_and_dedupes(make_ctx):
    ctx = make_ctx({"api/v2/remote-jobs": "jobicy.json"})
    jobs = list(Jobicy().fetch(ctx))

    assert len(ctx.http.calls) == 2
    urls = [str(c.url) for c in ctx.http.calls]
    assert "geo=europe" in urls[0] and "geo=anywhere" in urls[1]
    assert "count=50" in urls[0] and "industry=dev" in urls[0]
    assert len(jobs) == 2  # same ids served twice, deduped by id


def test_jobicy_respects_limit(make_ctx):
    ctx = make_ctx({"api/v2/remote-jobs": "jobicy.json"}, limit=1)
    assert len(list(Jobicy().fetch(ctx))) == 1


def test_salary_text_tolerates_junk():
    assert salary_text(50000, None, "usd") == "50,000 USD"
    assert salary_text(None, None, "EUR") is None
    assert salary_text("nope", None, None) is None
