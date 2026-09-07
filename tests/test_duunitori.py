from datetime import UTC, datetime

import httpx
import pytest
from conftest import fixture_text

from jobscraper.http import SourceHTTPError
from jobscraper.sources.duunitori import Duunitori, parse_search_html


def test_duunitori_parses_api_fixture(make_ctx):
    ctx = make_ctx({"jobentries": "duunitori.json"}, options={"queries": ["python"], "max_pages": 1})
    jobs = list(Duunitori().fetch(ctx))

    assert len(jobs) == 2  # the record with no slug/heading is skipped, not raised
    j = jobs[0]
    assert j.source == "duunitori"
    assert j.source_id == "esimerkki-oy-junior-software-developer-9911001"
    assert j.title == "Junior Software Developer"
    assert j.company == "Esimerkki Oy"
    assert j.url == "https://duunitori.fi/tyopaikat/tyo/esimerkki-oy-junior-software-developer-9911001"
    assert j.country == "FI" and j.city == "Helsinki"
    assert j.remote == "unknown"
    assert "junior-kehittäjää" in j.description and "<" not in j.description
    assert j.posted_at == datetime(2026, 9, 1, tzinfo=UTC)

    assert jobs[1].remote == "remote"  # "Etätyö" in the municipality string
    assert jobs[1].salary_text == "3500 - 4500 €/kk"


def test_duunitori_dedupes_across_queries(make_ctx):
    ctx = make_ctx(
        {"jobentries": "duunitori.json"},
        options={"queries": ["python", "django"], "max_pages": 1},
    )
    jobs = list(Duunitori().fetch(ctx))
    assert len(jobs) == 2  # same slugs returned twice, emitted once
    assert len(ctx.http.calls) == 2


def test_duunitori_respects_limit(make_ctx):
    ctx = make_ctx({"jobentries": "duunitori.json"}, options={"max_pages": 3}, limit=1)
    assert len(list(Duunitori().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1


def test_duunitori_falls_back_to_html_when_api_returns_html(make_ctx):
    ctx = make_ctx(
        {
            "jobentries": lambda req: httpx.Response(
                200, text="<html><body><h1>Api Root</h1></body></html>"
            ),
            "/tyopaikat": "duunitori_search.html",
        },
        options={"queries": ["developer"], "max_pages": 1},
    )
    jobs = list(Duunitori().fetch(ctx))

    assert [j.title for j in jobs] == ["Junior Frontend Developer", "DevOps Engineer, etätyö"]
    first = jobs[0]
    assert first.company == "HTMLfirma Oy"  # card puts the employer in .job-box__job-posted
    assert first.city == "Espoo" and first.country == "FI"
    assert first.url.endswith("/tyopaikat/tyo/htmlfirma-oy-junior-frontend-developer-8800111")
    assert jobs[1].company == "Kaukotyö Oy"  # relative date in job-posted is not a company
    assert jobs[1].posted_at is None
    assert jobs[1].remote == "remote"
    assert len(ctx.http.calls) == 2  # API attempt, then the search page


def test_parse_search_html_is_tolerant_of_junk():
    assert parse_search_html(None) == []
    assert parse_search_html("<html><body>no cards here</body></html>") == []
    records = parse_search_html(fixture_text("duunitori_search.html"))
    assert len(records) == 2  # the card without a link is dropped


def test_duunitori_raises_on_cloudflare_challenge(make_ctx):
    ctx = make_ctx(
        {"jobentries": lambda req: httpx.Response(403, text="<html>Just a moment... cloudflare</html>")}
    )
    with pytest.raises(SourceHTTPError):
        list(Duunitori().fetch(ctx))
