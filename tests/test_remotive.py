from datetime import UTC, datetime

from jobscraper.sources.remotive import Remotive, region_country


def test_remotive_parses_fixture(make_ctx):
    ctx = make_ctx({"api/remote-jobs": "remotive.json"})
    jobs = list(Remotive().fetch(ctx))

    assert len(jobs) == 2  # the record without id/url is skipped, not raised
    j = jobs[0]
    assert j.source == "remotive"
    assert j.source_id == "1948321"
    assert j.title == "Junior Backend Engineer (Python)"
    assert j.company == "Sunrise Labs"
    assert j.url.endswith("junior-backend-engineer-1948321")
    assert j.remote == "remote"
    assert j.remote_region == "Europe"
    assert j.country is None  # "Europe" is a region, not a country
    assert j.employment_type == "full_time"
    assert j.tags[0] == "Software Development" and "python" in j.tags
    assert j.salary_text == "€45,000 - €58,000"
    assert "Junior Backend Engineer" in j.description and "<" not in j.description
    assert j.posted_at == datetime(2026, 8, 28, 9, 15, 11, tzinfo=UTC)

    assert jobs[1].country == "US"  # "USA Only" names exactly one country
    assert jobs[1].salary_text is None


def test_remotive_makes_exactly_one_request(make_ctx):
    ctx = make_ctx({"api/remote-jobs": "remotive.json"}, options={"search": "python"})
    list(Remotive().fetch(ctx))

    assert len(ctx.http.calls) == 1
    url = str(ctx.http.calls[0].url)
    assert "category=software-dev" in url and "search=python" in url


def test_remotive_respects_limit(make_ctx):
    ctx = make_ctx({"api/remote-jobs": "remotive.json"}, limit=1)
    assert len(list(Remotive().fetch(ctx))) == 1


def test_region_country_only_for_single_countries():
    assert region_country("Germany") == "DE"
    assert region_country("Europe") is None
    assert region_country("UK, USA, Canada") is None
    assert region_country("Anywhere in the World") is None
    assert region_country(None) is None
