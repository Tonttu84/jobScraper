"""remotive fixture = a trimmed copy of the real remotive.com/api/remote-jobs response (2026-09-07).

The live call already asks for ``category=software-dev&limit=5`` and still comes back with the
whole 18-posting teaser feed — Sales, Writing, "All others" and all — so the fixture keeps one
non-software posting to pin the client-side category filter down.
"""

from datetime import UTC, datetime

from jobscraper.sources.remotive import (
    Remotive,
    matches_category,
    parse_record,
    region_country,
)

ROUTE = {"api/remote-jobs": "remotive.json"}


def all_jobs(make_ctx):
    """Every real record: switch the client-side category filter off."""
    return list(Remotive().fetch(make_ctx(ROUTE, options={"categories": []})))


def test_remotive_parses_fixture(make_ctx):
    jobs = all_jobs(make_ctx)

    assert len(jobs) == 3  # the record without id and url is skipped, not raised
    j = jobs[0]
    assert j.source == "remotive"
    assert j.source_id == "2091101"  # the feed sends the id as a number
    assert j.title == "Senior React Full-stack Developer"
    assert j.company == "Lemon.io"
    assert j.url == (
        "https://remotive.com/remote-jobs/software-development/senior-react-full-stack-developer-2091101"
    )
    assert j.remote == "remote"
    assert j.remote_region == "LATAM, Europe, USA, Canada, APAC"
    assert j.country is None  # several countries named: a region, not a country
    assert j.location_raw == "LATAM, Europe, USA, Canada, APAC"
    assert j.employment_type == "full_time"
    assert j.tags[0] == "Software Development" and ".Net" in j.tags  # category first, then tags
    assert j.salary_text is None  # the feed sends "" when no salary is published
    assert "marketplace that connects you" in j.description and "<" not in j.description
    assert j.posted_at == datetime(2026, 8, 27, 14, 36, 9, tzinfo=UTC)  # naive ISO, read as UTC


def test_remotive_maps_salary_and_single_country_regions(make_ctx):
    rails, reviewer = all_jobs(make_ctx)[1:]

    assert rails.title == "Tech Lead Full-Stack Rails Engineer"
    assert rails.salary_text == "$170k - $200k"
    assert rails.country is None  # "USA, Canada, USA timezones"

    assert reviewer.country == "US"  # "USA" names exactly one country
    assert reviewer.employment_type == "part_time"
    assert reviewer.salary_text == "$14/hour"


def test_remotive_drops_categories_the_api_refuses_to_filter(make_ctx):
    ctx = make_ctx(ROUTE)  # default categories: software development, devops, QA, IT, data
    titles = [j.title for j in Remotive().fetch(ctx)]

    assert titles == ["Senior React Full-stack Developer", "Tech Lead Full-Stack Rails Engineer"]
    assert "Content Reviewer - English US" not in titles  # category "All others"


def test_remotive_makes_exactly_one_request(make_ctx):
    ctx = make_ctx(ROUTE, options={"search": "python"})
    list(Remotive().fetch(ctx))

    assert len(ctx.http.calls) == 1
    url = str(ctx.http.calls[0].url)
    assert "category=software-dev" in url and "search=python" in url


def test_remotive_respects_limit(make_ctx):
    ctx = make_ctx(ROUTE, limit=1)
    assert len(list(Remotive().fetch(ctx))) == 1  # the API ignores &limit=, so we cut client-side


def test_matches_category_is_permissive():
    wanted = ["software development", "devops"]
    assert matches_category({"category": "Software Development"}, wanted)
    assert matches_category({"category": "Devops"}, wanted)
    assert not matches_category({"category": "Writing"}, wanted)
    assert matches_category({}, wanted)  # no category stated → keep it, the AI stages decide
    assert matches_category({"category": "Writing"}, [])  # filter off


def test_remotive_skips_a_record_that_is_not_an_object():
    assert parse_record("the feed sometimes has a stray string") is None


def test_remotive_does_not_repeat_the_category_in_the_tags():
    job = parse_record(
        {"id": 1, "title": "Dev", "category": "Software Development",
         "tags": ["Software Development", "Python"]}
    )
    assert job.tags == ["Software Development", "Python"]


def test_remotive_can_be_asked_without_a_category(make_ctx):
    ctx = make_ctx(ROUTE, options={"category": "", "categories": []})
    assert list(Remotive().fetch(ctx))
    assert "category=" not in str(ctx.http.calls[0].url)


def test_region_country_only_for_single_countries():
    assert region_country("Germany") == "DE"
    assert region_country("Europe") is None
    assert region_country("USA, Canada, USA timezones") is None
    assert region_country("Worldwide") is None
    assert region_country(None) is None
