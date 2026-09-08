"""remoteok fixture = a trimmed copy of the real https://remoteok.com/api response (2026-09-07).

Kept from the live feed: the legal-notice element 0, one software posting, and two scraped
hotel postings that show what the board really serves — a city in ``location`` with an empty
country half ("Budapest, "), HTML entities in ``company`` and auto-generated ``tags``.
"""

from datetime import UTC, datetime

from jobscraper.sources.remoteok import RemoteOK, clean_location, salary_text

ROUTE = {"remoteok.com/api": "remoteok.json"}


def all_jobs(make_ctx):
    """Every real record: the tag option is what narrows the feed, so switch it off here."""
    return list(RemoteOK().fetch(make_ctx(ROUTE, options={"tags": []})))


def test_remoteok_parses_fixture(make_ctx):
    jobs = all_jobs(make_ctx)

    # element 0 is the legal notice and the last record has a scalar "tags": both skipped
    assert [j.title for j in jobs] == ["QA Engineer", "Chief Steward", "Carpenter"]

    j = jobs[0]
    assert j.source == "remoteok" and j.source_id == "1137300"
    assert j.company == "SunnyData"
    assert j.url == "https://remoteOK.com/remote-jobs/remote-qa-engineer-sunnydata-1137300"
    assert j.remote == "remote"
    assert j.location_raw is None and j.country is None and j.remote_region is None
    assert j.tags[:3] == ["technical", "dev", "testing"]
    assert j.salary_text is None  # the live feed sends salary_min/max = 0 for ~95% of postings
    assert "DataBricks technology partner" in j.description
    assert "Please mention the word" not in j.description  # anti-bot trailer stripped
    assert j.posted_at == datetime(2026, 9, 3, 16, 0, 5, tzinfo=UTC)  # "date", not "epoch"


def test_remoteok_location_is_a_city_with_an_empty_country_half(make_ctx):
    steward = all_jobs(make_ctx)[1]

    assert steward.location_raw == "Budapest"  # the feed sends "Budapest, "
    assert steward.country == "HU"
    assert steward.remote_region is None  # a city is not a "who may apply" statement
    assert steward.posted_at == datetime(2026, 9, 2, 3, 32, 55, tzinfo=UTC)


def test_remoteok_unescapes_html_entities_in_company_and_title(make_ctx):
    carpenter = all_jobs(make_ctx)[2]

    assert carpenter.company == "St. Regis Hotels & Resorts"  # feed: "…Hotels &amp; Resorts"
    assert carpenter.title == "Carpenter"
    assert carpenter.location_raw == "Goa"


def test_remoteok_tag_option_matches_the_title_only(make_ctx):
    # The Carpenter posting carries the auto-tag "engineer" and the Chief Steward "customer
    # support"; only the QA Engineer says so in its title.
    ctx = make_ctx(ROUTE)  # default tags: dev / engineer / junior
    assert [j.title for j in RemoteOK().fetch(ctx)] == ["QA Engineer"]

    ctx = make_ctx(ROUTE, options={"tags": ["steward"]})
    assert [j.title for j in RemoteOK().fetch(ctx)] == ["Chief Steward"]


def test_remoteok_single_request_and_limit(make_ctx):
    ctx = make_ctx(ROUTE, options={"tags": []}, limit=1)
    assert len(list(RemoteOK().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1


def test_clean_location_strips_the_dangling_separator():
    assert clean_location("Visakhapatnam Rural mandal, ") == "Visakhapatnam Rural mandal"
    assert clean_location("Vancouver, BC, Canada") == "Vancouver, BC, Canada"
    assert clean_location("") is None
    assert clean_location(None) is None


def test_salary_text_uses_the_feeds_usd_integers():
    assert salary_text(0, 0) is None  # the usual case: no salary published
    assert salary_text(60000, 80000) == "60,000 - 80,000 USD"
    assert salary_text(20000, 20000) == "20,000 USD"  # feed repeats the figure, don't print a range
    assert salary_text(None, 90000) == "90,000 USD"
