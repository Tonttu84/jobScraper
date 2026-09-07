from datetime import UTC, datetime

from jobscraper.sources.himalayas import Himalayas


def test_himalayas_parses_fixture(make_ctx):
    ctx = make_ctx({"himalayas.app/jobs/api": "himalayas.json"})
    jobs = list(Himalayas().fetch(ctx))

    assert len(jobs) == 2  # the record without title/link is skipped
    j = jobs[0]
    assert j.source == "himalayas"
    assert j.title == "Junior Software Engineer"
    assert j.company == "Lumen Labs"
    assert j.url.endswith("/jobs/junior-software-engineer")
    assert j.source_id == j.url  # guid doubles as the stable id
    assert j.remote == "remote" and j.remote_region == "Europe" and j.country is None
    assert j.seniority_raw == "Entry-level, Junior"
    assert j.employment_type == "Full Time"
    assert j.tags == ["Software Engineering", "Full Stack"]
    assert j.salary_text == "40,000 - 60,000 EUR"
    assert "junior software engineer" in j.description and "<" not in j.description
    assert j.posted_at == datetime.fromtimestamp(1756881000, tz=UTC)

    second = jobs[1]
    assert second.url == "https://orbital.example.com/careers/staff-data-engineer"
    assert second.source_id.startswith("https://himalayas.app/")  # guid, not the apply link
    assert second.country == "PT"  # locationRestrictions ["Portugal"]
    assert second.posted_at == datetime(2026, 8, 25, 10, 30, tzinfo=UTC)


def test_himalayas_stops_at_total_count(make_ctx):
    ctx = make_ctx({"himalayas.app/jobs/api": "himalayas.json"}, options={"max_pages": 5})
    list(Himalayas().fetch(ctx))

    assert len(ctx.http.calls) == 1  # totalCount 3 is covered by the first page
    assert "offset=0" in str(ctx.http.calls[0].url)
    assert "limit=50" in str(ctx.http.calls[0].url)


def test_himalayas_respects_limit(make_ctx):
    ctx = make_ctx({"himalayas.app/jobs/api": "himalayas.json"}, limit=1)
    assert len(list(Himalayas().fetch(ctx))) == 1
