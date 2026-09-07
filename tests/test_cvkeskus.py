from datetime import datetime, timedelta, timezone

import httpx
from conftest import fixture_text

from jobscraper.sources.cvkeskus import CvKeskus, parse_detail, parse_search_html, parse_sitemap

SEARCH = "/toopakkumised"
DETAIL_LD = "ai-agent-developer-tallinnas"
DETAIL_PLAIN = "junior-developer-tartus"
SITEMAP = "sitemap-listings-information-technology-en.xml"

ROUTES = {
    SEARCH: "cvkeskus_search.html",
    DETAIL_LD: "cvkeskus_detail.html",
    DETAIL_PLAIN: "cvkeskus_detail_nojsonld.html",
    SITEMAP: "cvkeskus_sitemap.xml",
}


def test_cvkeskus_search_route(make_ctx):
    ctx = make_ctx(ROUTES, options={"keywords": ["developer"]})
    jobs = list(CvKeskus().fetch(ctx))

    assert len(jobs) == 2  # the card with no link never becomes a request
    j = jobs[0]
    assert j.source == "cvkeskus"
    assert j.source_id == "1051058"  # trailing numeric id from the slug
    assert j.url == "https://www.cvkeskus.ee/ai-agent-developer-tallinnas-bolt-technology-ou-1051058"
    assert j.title == "AI / Agent Engineer"
    assert j.company == "Bolt Technology OÜ"  # hiringOrganization is an @id reference
    assert j.country == "EE" and j.city == "Tallinn"
    assert j.employment_type == "FULL_TIME"
    assert j.salary_text == "3500 - 5000 €"
    assert "agentic" in j.description and "<strong>" not in j.description
    assert j.posted_at == datetime(2026, 8, 26, 4, 32, 9, tzinfo=timezone(timedelta(hours=3)))
    assert j.raw["valid_through"] == "2026-09-25T23:59:59+03:00"


def test_cvkeskus_falls_back_to_card_when_no_jsonld(make_ctx):
    ctx = make_ctx(ROUTES, options={"keywords": ["developer"]})
    j = list(CvKeskus().fetch(ctx))[1]

    assert j.source_id == "1051099"
    assert j.title == "Junior Developer"  # taken from the search card
    assert j.company is None and j.city == "Tartu" and j.country == "EE"
    assert j.description is None and j.posted_at is None


def test_cvkeskus_sitemap_route(make_ctx):
    ctx = make_ctx(ROUTES, options={"use_sitemap": True, "max_details": 1})
    jobs = list(CvKeskus().fetch(ctx))

    assert len(jobs) == 1  # capped by max_details
    assert jobs[0].source_id == "1051058"
    assert jobs[0].company == "Bolt Technology OÜ"
    urls = [str(c.url) for c in ctx.http.calls]
    assert urls[0].endswith(SITEMAP) and SEARCH not in urls[0]


def test_cvkeskus_skips_a_broken_detail_page(make_ctx):
    routes = dict(ROUTES)
    routes[DETAIL_LD] = lambda req: httpx.Response(500, text="boom")
    ctx = make_ctx(routes, options={"keywords": ["developer"]})
    jobs = list(CvKeskus().fetch(ctx))
    assert [j.source_id for j in jobs] == ["1051099"]  # one bad page doesn't abort the fetch


def test_cvkeskus_respects_limit(make_ctx):
    ctx = make_ctx(ROUTES, options={"keywords": ["developer"]}, limit=1)
    assert len(list(CvKeskus().fetch(ctx))) == 1


def test_parsers_tolerate_junk():
    assert parse_search_html(None) == []
    assert parse_search_html("<html><body>nothing</body></html>") == []
    assert parse_sitemap(None) == []
    assert parse_sitemap("<urlset><url><loc>not-a-url</loc></url></urlset>") == []
    assert parse_sitemap(fixture_text("cvkeskus_sitemap.xml")) == [
        "https://www.cvkeskus.ee/ai-agent-developer-tallinnas-bolt-technology-ou-1051058",
        "https://www.cvkeskus.ee/junior-developer-tartus-example-ou-1051099",
    ]
    # No JSON-LD and no card → nothing to normalize, but no exception either.
    assert parse_detail("<html></html>", "https://www.cvkeskus.ee/whatever-123") is None
    assert parse_detail(None, "https://www.cvkeskus.ee/x-1", {"title": "Fallback"}).title == "Fallback"


def test_parse_search_html_reads_cards():
    records = parse_search_html(fixture_text("cvkeskus_search.html"))
    assert len(records) == 2
    assert records[0]["company"] == "Bolt Technology OÜ"
    assert records[0]["url"].startswith("https://www.cvkeskus.ee/")
    assert records[1]["location"] == "Tartu"
