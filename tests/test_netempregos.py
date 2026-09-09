import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from conftest import fixture_text

from jobscraper.sources.netempregos import (
    IT_CATEGORIES,
    NetEmpregos,
    parse_detail,
    parse_pt_date,
    parse_search_html,
)

SEARCH = "pesquisa-empregos.asp"
PAGE2 = "page=2"
CPP = "/15867575/"
FULLSTACK = "/15935141/"
AWS = "/15835461/"

ROUTES = {
    PAGE2: "netempregos_search_empty.html",
    SEARCH: "netempregos_search.html",
    CPP: "netempregos_detail.html",
    FULLSTACK: "netempregos_detail_nojsonld.html",
    AWS: "netempregos_detail_remote.html",
}
ONE_QUERY = {"queries": ["programador"]}


def search_calls(ctx):
    return [c for c in ctx.http.calls if SEARCH in str(c.url)]


def detail_calls(ctx):
    return [c for c in ctx.http.calls if SEARCH not in str(c.url)]


def query(request):
    return dict(parse_qsl(urlsplit(str(request.url)).query))


def test_netempregos_reads_the_search_page_and_the_posting(make_ctx):
    ctx = make_ctx(ROUTES, options=ONE_QUERY)
    jobs = list(NetEmpregos().fetch(ctx))

    assert len(jobs) == 3  # the fourth card's anchor has no href, so it never becomes a request
    j = jobs[0]
    assert j.source == "netempregos"
    assert j.source_id == "15867575"  # the numeric segment of the posting URL
    assert j.url == "https://www.net-empregos.com/15867575/senior-c/"
    assert j.title == "Senior C++"
    assert j.company == "LOG OSCON LDA"
    assert j.location_raw == "Lisboa" and j.city == "Lisboa" and j.country == "PT"
    assert j.remote == "hybrid"  # "Regime híbrido" in the Portuguese description
    assert j.employment_type == "FULL_TIME"
    assert j.seniority_raw == "Senior"
    assert j.posted_at == datetime(2026, 8, 28, 15, 8, tzinfo=UTC)
    assert j.tags == ["Informática ( Programação )"]
    assert "Boost" in j.description and "<b>" not in j.description
    # The site appends its own URL to every description; it is not part of the ad.
    assert "net-empregos.com/15867575" not in j.description
    assert j.raw["card"]["category"] == "Informática ( Programação )"


def test_netempregos_reads_jsonld_with_raw_control_characters():
    """The ad text is pasted into the JSON with its CRs intact — strict json.loads rejects it.

    Every posting page observed 2026-09-09 was like this, so getting it wrong costs the
    description, the exact timestamp and the employment type on *every* record.
    """
    html = fixture_text("netempregos_detail.html")
    # The fixture keeps the site's raw CRs; reading it as text turns them into "\n", which is
    # just as invalid inside a strict JSON string. Keep the break — it is what this test tests.
    assert re.search(r"</b>[\r\n]<BR>", html)

    j = parse_detail(html, "https://www.net-empregos.com/15867575/senior-c/")
    assert j.employment_type == "FULL_TIME"  # JSON-LD only, not in the visible markup
    assert j.posted_at == datetime(2026, 8, 28, 15, 8, tzinfo=UTC)  # ditto for the time
    assert j.raw["valid_through"] == "2026-9-27 15:08 UTC"


def test_netempregos_search_uses_the_it_category_and_pages_with_page(make_ctx):
    ctx = make_ctx(ROUTES, options=ONE_QUERY)
    list(NetEmpregos().fetch(ctx))

    calls = search_calls(ctx)
    assert len(calls) == 2  # page 2 comes back empty, so the query stops there
    first, second = (query(c) for c in calls)
    assert first["chaves"] == "programador"
    assert first["categoria"] == str(IT_CATEGORIES["Informática ( Programação )"]) == "5"
    assert first["zona"] == "0" and first["tipo"] == "0"
    assert "page" not in first  # the site's own pager omits it on page 1
    assert second["page"] == "2"


def test_netempregos_reads_a_teletrabalho_posting(make_ctx):
    ctx = make_ctx(ROUTES, options=ONE_QUERY)
    j = next(j for j in NetEmpregos().fetch(ctx) if j.source_id == "15835461")

    assert j.remote == "remote"  # jobLocationType TELECOMMUTE
    # "( Todas as Zonas )" is the board's placeholder for "anywhere", not a town.
    assert j.location_raw is None and j.city is None
    assert j.country == "PT"
    assert j.title.endswith("Full Remote (M/F)")


def test_netempregos_falls_back_to_the_visible_markup_without_jsonld(make_ctx):
    ctx = make_ctx(ROUTES, options=ONE_QUERY)
    j = next(j for j in NetEmpregos().fetch(ctx) if j.source_id == "15935141")

    assert j.title == "Programador Full-Stack"
    assert j.company == "Instituto CRIAP"
    assert j.city == "Porto" and j.country == "PT"
    assert j.posted_at == datetime(2026, 9, 9, tzinfo=UTC)  # card date, "9-9-2026"
    assert j.employment_type == "Integral/Full-time"  # the "Tipo de oferta:" line
    assert j.seniority_raw is None
    assert "MySQL" in j.description
    assert j.raw["card"]["featured"] is True  # "Oferta em Destaque"


def test_netempregos_can_skip_the_posting_pages(make_ctx):
    ctx = make_ctx(ROUTES, options={**ONE_QUERY, "fetch_details": False})
    jobs = list(NetEmpregos().fetch(ctx))

    assert detail_calls(ctx) == []
    j = jobs[0]
    assert j.title == "Senior C++" and j.company == "LOG OSCON LDA"
    assert j.city == "Lisboa" and j.country == "PT"
    assert j.posted_at == datetime(2026, 8, 28, tzinfo=UTC)  # card date only
    assert j.description is None


def test_netempregos_caps_detail_fetches_with_max_details(make_ctx):
    ctx = make_ctx(ROUTES, options={**ONE_QUERY, "max_details": 1})
    jobs = list(NetEmpregos().fetch(ctx))

    assert len(detail_calls(ctx)) == 1
    assert jobs[0].description is not None
    assert jobs[1].description is None  # still yielded, just from the card


def test_netempregos_ignores_the_similar_jobs_of_an_empty_result_page(make_ctx):
    """A search with no hits still renders cards — for completely unrelated jobs."""
    ctx = make_ctx({SEARCH: "netempregos_search_empty.html"}, options=ONE_QUERY)
    assert list(NetEmpregos().fetch(ctx)) == []
    assert len(search_calls(ctx)) == 1  # nothing on page 1 means no page 2


def test_netempregos_dedupes_across_queries(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["programador", "software developer"]})
    jobs = list(NetEmpregos().fetch(ctx))

    assert len(jobs) == 3
    assert len({j.url for j in jobs}) == 3
    assert [query(c)["chaves"] for c in search_calls(ctx)][:3] == [
        "programador",
        "programador",
        "software developer",
    ]
    assert len(detail_calls(ctx)) == 3  # the second query re-finds the same three postings


def test_netempregos_respects_limit(make_ctx):
    ctx = make_ctx(ROUTES, limit=1)
    assert len(list(NetEmpregos().fetch(ctx))) == 1


def test_netempregos_skips_a_broken_posting_page(make_ctx):
    routes = dict(ROUTES)
    routes[CPP] = lambda req: httpx.Response(500, text="boom")
    ctx = make_ctx(routes, options=ONE_QUERY)

    ids = [j.source_id for j in NetEmpregos().fetch(ctx)]
    assert ids == ["15935141", "15835461"]  # one bad page doesn't abort the fetch


def test_netempregos_multiple_categories(make_ctx):
    ctx = make_ctx(ROUTES, options={**ONE_QUERY, "category": [5, 38], "max_pages": 1})
    list(NetEmpregos().fetch(ctx))

    assert [query(c)["categoria"] for c in search_calls(ctx)] == ["5", "38"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("9-9-2026", datetime(2026, 9, 9, tzinfo=UTC)),          # card: D-M-YYYY, no padding
        ("28-8-2026", datetime(2026, 8, 28, tzinfo=UTC)),
        ("2026-8-28 15:08 UTC", datetime(2026, 8, 28, 15, 8, tzinfo=UTC)),  # JSON-LD datePosted
        ("2026-12-01 09:00 UTC", datetime(2026, 12, 1, 9, 0, tzinfo=UTC)),
        ("2026-08-28T15:08:00Z", datetime(2026, 8, 28, 15, 8, tzinfo=UTC)),  # plain ISO still works
        ("", None),
        (None, None),
        ("brevemente", None),
        ("31-2-2026", None),  # a real date format, an impossible date
    ],
)
def test_parse_pt_date(value, expected):
    assert parse_pt_date(value) == expected


def test_parse_search_html_reads_the_cards():
    cards = parse_search_html(fixture_text("netempregos_search.html"))

    assert [c["source_id"] for c in cards] == ["15867575", "15935141", "15835461"]
    assert cards[0]["title"] == "Senior C++"
    assert cards[0]["company"] == "LOG OSCON LDA"
    assert cards[0]["location"] == "Lisboa"
    assert cards[0]["posted"] == "28-8-2026"
    assert cards[0]["featured"] is False
    assert cards[1]["featured"] is True
    assert cards[2]["location"] is None  # "( Todas as Zonas )" is dropped
    assert cards[0]["url"].startswith("https://www.net-empregos.com/")


def test_parsers_tolerate_junk():
    assert parse_search_html(None) == []
    assert parse_search_html("<html><body>nothing here</body></html>") == []
    # No JSON-LD, no visible title and no card: nothing to normalize, but no exception either.
    assert parse_detail("<html></html>", "https://www.net-empregos.com/1/x/") is None
    assert parse_detail(None, "https://www.net-empregos.com/1/x/", {"title": "Fallback"}).title == (
        "Fallback"
    )
    # Broken JSON in the ld+json block falls back to the visible markup.
    broken = '<script type="application/ld+json">{"title": oops}</script><h1 class="title">Dev</h1>'
    assert parse_detail(broken, "https://www.net-empregos.com/2/y/").title == "Dev"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Trabalho em regime de teletrabalho", "remote"),
        ("100% remoto a partir de Portugal", "remote"),
        ("Full Remote (M/F)", "remote"),
        ("Regime híbrido, 2 dias no escritório", "hybrid"),
        ("Estágio AI Builder (Porto - Hibrido)", "hybrid"),  # the board's ads drop the accent
        ("Programador Full-Stack no Porto", "unknown"),
    ],
)
def test_netempregos_reads_the_portuguese_remote_wording(text, expected):
    job = parse_detail(None, "https://www.net-empregos.com/3/z/", {"title": text})
    assert job.remote == expected
    assert job.country == "PT"
