from datetime import UTC, datetime

import httpx

from jobscraper.sources.justjoin import JustJoin, salary_text

ROUTES = {
    ("GET", "/v1/offers/"): "justjoin_detail.json",
    ("GET", "/v2/user-panel/offers/by-cursor"): "justjoin.json",
}


def test_justjoin_parses_fixture(make_ctx):
    ctx = make_ctx(ROUTES, options={"max_pages": 1})
    jobs = list(JustJoin().fetch(ctx))
    assert len(jobs) == 2  # the broken offer is skipped, not raised
    j = jobs[0]
    assert j.source == "justjoin"
    assert j.source_id == "9f1c2e33-junior-python-developer"
    assert j.title == "Junior Python Developer"
    assert j.company == "Acme Sp. z o.o."
    assert j.url == "https://justjoin.it/job-offer/acme-warsaw-junior-python-developer"
    assert j.country == "PL" and j.city == "Warszawa" and j.remote == "hybrid"
    assert j.seniority_raw == "junior" and j.employment_type == "b2b"
    assert j.salary_text == "8,000 - 12,000 PLN (b2b)"
    assert j.tags == ["Python", "SQL", "FastAPI"]
    assert j.posted_at == datetime(2025, 9, 3, 8, 15, tzinfo=UTC)
    assert "FastAPI" in j.description and "<" not in j.description
    assert jobs[1].remote == "remote" and jobs[1].country == "EE"


def test_justjoin_sends_only_configured_filters(make_ctx):
    ctx = make_ctx(ROUTES, options={"experience_levels": ["junior", "mid"], "max_pages": 1,
                                    "fetch_details": False})
    list(JustJoin().fetch(ctx))
    url = str(ctx.http.calls[0].url)
    assert "experienceLevels%5B%5D=junior" in url and "experienceLevels%5B%5D=mid" in url
    assert "workplaceTypes" not in url
    assert all("/v1/offers/" not in str(c.url) for c in ctx.http.calls)

    ctx = make_ctx(ROUTES, options={"workplace_types": ["remote"], "max_pages": 1, "fetch_details": False})
    list(JustJoin().fetch(ctx))
    assert "workplaceTypes%5B%5D=remote" in str(ctx.http.calls[0].url)


def test_justjoin_follows_cursor_then_stops(make_ctx):
    pages = [
        {"data": [{"guid": "a", "slug": "a-slug", "title": "Junior Dev", "companyName": "A", "city": "Kraków",
                   "countryCode": "PL", "workplaceType": "office", "publishedAt": "2025-09-05T00:00:00Z"}],
         "meta": {"next": {"cursor": 42}}},
        {"data": [{"guid": "b", "slug": "b-slug", "title": "Junior Dev 2", "companyName": "B", "city": "Gdańsk",
                   "countryCode": "PL", "workplaceType": "remote", "publishedAt": "2025-09-04T00:00:00Z"}],
         "meta": {"next": {"cursor": 42}}},  # cursor did not advance → stop
    ]
    calls: list[httpx.Request] = []

    def route(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json=pages[min(len(calls) - 1, len(pages) - 1)])

    ctx = make_ctx({("GET", "by-cursor"): route}, options={"max_pages": 5, "fetch_details": False})
    jobs = list(JustJoin().fetch(ctx))
    assert [j.source_id for j in jobs] == ["a", "b"]
    assert len(calls) == 2 and "from=42" in str(calls[1].url)
    assert jobs[0].remote == "onsite"


def test_justjoin_respects_limit(make_ctx):
    ctx = make_ctx(ROUTES, options={"max_pages": 3, "fetch_details": False}, limit=1)
    assert len(list(JustJoin().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1


def test_justjoin_salary_text_tolerates_junk():
    assert salary_text(None) is None
    assert salary_text("broken") is None
    assert salary_text([{"from": None, "to": None}, "x"]) is None
    assert salary_text([{"from": 2500, "currency": "eur", "type": "permanent"}]) == "2,500 EUR (permanent)"
