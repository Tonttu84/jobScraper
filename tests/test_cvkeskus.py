from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from conftest import fixture_text

from jobscraper.sources.cvkeskus import CvKeskus, parse_detail, parse_search_html, parse_sitemap

SEARCH = "/toopakkumised"
DETAIL_LD = "senior-full-stack-developer-tallinnas-wise-1052794"
DETAIL_PLAIN = "full-stack-developer-harjumaal-entringo-1053023"
SITEMAP = "sitemap-listings-information-technology-en.xml"
EET = timezone(timedelta(hours=3))

ROUTES = {
    SEARCH: "cvkeskus_search.html",
    DETAIL_LD: "cvkeskus_detail.html",
    DETAIL_PLAIN: "cvkeskus_detail_nojsonld.html",
    SITEMAP: "cvkeskus_sitemap.xml",
}


def search_calls(ctx):
    return [c for c in ctx.http.calls if SEARCH in str(c.url)]


def query(request):
    return parse_qsl(urlsplit(str(request.url)).query)


def test_cvkeskus_search_route(make_ctx):
    ctx = make_ctx(ROUTES)
    jobs = list(CvKeskus().fetch(ctx))

    assert len(jobs) == 2  # the card whose anchor lost its href never becomes a request
    j = jobs[0]
    assert j.source == "cvkeskus"
    assert j.source_id == "1052794"  # trailing numeric id from the slug
    assert j.url == "https://www.cvkeskus.ee/senior-full-stack-developer-tallinnas-wise-1052794"
    assert j.title == "Senior Full Stack Developer"
    assert j.company == "Wise"
    assert j.country == "EE" and j.city == "Tallinn"
    assert j.location_raw == "Tallinn"
    assert j.remote == "unknown"
    assert j.employment_type == "FULL_TIME"
    assert j.salary_text.startswith("5750 - 7083")
    assert "Scam Prevention" in j.description and "<strong>" not in j.description
    assert j.posted_at == datetime(2026, 9, 3, 12, 13, 19, tzinfo=EET)
    assert j.raw["valid_through"] == "2026-10-03T23:59:59+03:00"


def test_cvkeskus_search_filters_by_the_it_category(make_ctx):
    """``keyword=`` alone is ignored by the site: the results are the whole board's newest ads.

    Only ``op=search`` plus ``search[categories][]`` actually narrows anything down.
    """
    ctx = make_ctx(ROUTES)
    list(CvKeskus().fetch(ctx))

    params = query(search_calls(ctx)[0])
    assert ("op", "search") in params
    assert ("search[categories][]", "8") in params  # 8 = Infotehnoloogia
    assert not [k for k, _ in params if "keyword" in k]


def test_cvkeskus_search_adds_keyword_and_pages_with_start(make_ctx):
    ctx = make_ctx(ROUTES, options={"keywords": ["developer"], "categories": [8, 25]})
    list(CvKeskus().fetch(ctx))

    calls = search_calls(ctx)
    assert len(calls) == 2  # page 2 is requested, then stops because it repeats page 1
    first, second = (query(c) for c in calls)
    assert ("search[keyword]", "developer") in first
    assert [v for k, v in first if k == "search[categories][]"] == ["8", "25"]
    assert ("start", "0") not in first
    assert ("start", "30") in second


def test_cvkeskus_falls_back_to_card_when_no_jsonld(make_ctx):
    ctx = make_ctx(ROUTES)
    j = list(CvKeskus().fetch(ctx))[1]

    assert j.source_id == "1053023"
    assert j.title == "Full Stack-Developer"  # taken from the search card
    assert j.company == "Entringo" and j.city == "Harjumaa" and j.country == "EE"
    assert j.description is None and j.posted_at is None


def test_cvkeskus_sitemap_route(make_ctx):
    ctx = make_ctx(ROUTES, options={"use_sitemap": True, "max_details": 1})
    jobs = list(CvKeskus().fetch(ctx))

    assert len(jobs) == 1  # capped by max_details
    assert jobs[0].source_id == "1052794"
    # No search card here, so both of these have to come off the posting page itself.
    assert jobs[0].company == "Wise"
    assert jobs[0].city == "Tallinn"
    urls = [str(c.url) for c in ctx.http.calls]
    assert urls[0].endswith(SITEMAP) and SEARCH not in urls[0]


def test_cvkeskus_skips_a_broken_detail_page(make_ctx):
    routes = dict(ROUTES)
    routes[DETAIL_LD] = lambda req: httpx.Response(500, text="boom")
    ctx = make_ctx(routes)
    jobs = list(CvKeskus().fetch(ctx))
    assert [j.source_id for j in jobs] == ["1053023"]  # one bad page doesn't abort the fetch


def test_cvkeskus_respects_limit(make_ctx):
    ctx = make_ctx(ROUTES, limit=1)
    assert len(list(CvKeskus().fetch(ctx))) == 1


def test_parsers_tolerate_junk():
    assert parse_search_html(None) == []
    assert parse_search_html("<html><body>nothing</body></html>") == []
    assert parse_sitemap(None) == []
    assert parse_sitemap("<urlset><url><loc>not-a-url</loc></url></urlset>") == []
    assert parse_sitemap(fixture_text("cvkeskus_sitemap.xml")) == [
        "https://www.cvkeskus.ee/senior-full-stack-developer-tallinnas-wise-1052794",
        "https://www.cvkeskus.ee/ai-agent-developer-tallinnas-bolt-technology-ou-1051058",
        "https://www.cvkeskus.ee/back-end-developer-tartus-turnit-ou-1047845",
    ]
    # No JSON-LD and no card → nothing to normalize, but no exception either.
    assert parse_detail("<html></html>", "https://www.cvkeskus.ee/whatever-123") is None
    assert parse_detail(None, "https://www.cvkeskus.ee/x-1", {"title": "Fallback"}).title == "Fallback"


def test_parse_search_html_reads_cards():
    records = parse_search_html(fixture_text("cvkeskus_search.html"))
    assert len(records) == 2
    assert records[0]["company"] == "Wise"
    assert records[0]["url"].startswith("https://www.cvkeskus.ee/")
    assert records[0]["location"] == "Tallinn"
    assert records[0]["salary"].startswith("5750 - 7083")
    assert records[1]["location"] == "Harjumaa"


@pytest.mark.parametrize(
    ("location", "expected_remote", "expected_city"),
    [
        ("Tallinn", "unknown", "Tallinn"),
        ("Kodukontor", "remote", None),  # the board's own label for a home-office posting
        ("Kodukontori võimalusega", "remote", None),
        ("Tallinn / Kaugtöö", "remote", "Tallinn"),
        ("Jüri / Aruküla", "unknown", "Jüri"),  # two towns, not a remote tag
        ("Eesti", "unknown", None),  # the whole country: a country, never a city
    ],
)
def test_cvkeskus_reads_the_estonian_location_labels(location, expected_remote, expected_city):
    job = parse_detail(None, "https://www.cvkeskus.ee/x-1", {"title": "Dev", "location": location})
    assert job.remote == expected_remote
    assert job.city == expected_city
    assert job.country == "EE"


EXTRA_PAGE = """<html><body>
<article data-component="jobad">
  <a class="jobad-url" href="/back-end-developer-tartus-turnit-ou-1047845">link text</a>
  <h2></h2><h3>Back-End Developer</h3>
  <div class="job-company">Turnit</div>
  <span class="location">Tartu</span><span class="location"></span><span class="location">Tartu</span>
</article></body></html>"""


def test_cvkeskus_card_reader_survives_empty_headings_and_repeated_spans():
    """Live cards ship an empty ``h2`` and the town twice; neither may reach the record."""
    from jobscraper.sources.cvkeskus import _text

    assert _text(None) is None
    records = parse_search_html(EXTRA_PAGE)
    assert len(records) == 1
    assert records[0]["title"] == "Back-End Developer"  # the empty h2 is skipped for the h3
    assert records[0]["location"] == "Tartu"  # not "Tartu / Tartu"


GRAPH_DETAIL = """<html><body>
<script type="application/ld+json">{ this is not json </script>
<script type="application/ld+json">[
  "a bare string the site puts in the array",
  {"@context": "https://schema.org", "@graph": [
    {"@id": "https://cv.ee/#/schema/Address/listing-9", "@type": "PostalAddress",
     "addressRegion": "Tartu", "addressCountry": {"name": "Estonia"}},
    {"@id": "https://cv.ee/#/schema/Place/listing-9", "@type": "Place",
     "address": {"@id": "https://cv.ee/#/schema/PostalAddress/listing-9"}},
    {"@id": "https://cv.ee/#/schema/JobPosting/listing-9", "@type": "JobPosting",
     "title": "Platform Engineer",
     "hiringOrganization": [null, {"@id": "https://cv.ee/#/schema/Organization/listing-9"}],
     "jobLocation": {"@id": "https://cv.ee/#/schema/Place/listing-9"},
     "employmentType": ["FULL_TIME", "PART_TIME"],
     "jobLocationType": "TELECOMMUTE",
     "description": "<p>Build the platform.</p>",
     "datePosted": "2026-09-01T10:00:00+03:00"},
    {"@id": "https://cv.ee/#/schema/Organization/listing-9",
     "@type": ["Organization", "Thing"], "name": "Acme O\\u00dc"}
  ]}
]</script>
</body></html>"""


def test_cvkeskus_walks_the_whole_json_ld_graph():
    """Broken script, array envelope, list ``@type``, list ``hiringOrganization``, dict country."""
    job = parse_detail(GRAPH_DETAIL, "https://www.cvkeskus.ee/platform-engineer-tartus-9999")

    assert job.title == "Platform Engineer"
    assert job.company == "Acme OÜ"  # the Organization node is reached through its @id
    # Place.address points at .../PostalAddress/listing-9 but the node is .../Address/listing-9
    assert job.location_raw == "Tartu" and job.city == "Tartu"
    assert job.country == "EE"  # from the addressCountry object, not the location string
    assert job.employment_type == "FULL_TIME, PART_TIME"
    assert job.remote == "remote"  # jobLocationType
    assert job.description == "Build the platform."


def test_cvkeskus_gives_up_on_an_employer_link_it_cannot_read():
    """The Organization node is missing from the graph; the page link is the only fallback."""
    not_an_id = """<html><body><script type="application/ld+json">
    {"@type": "JobPosting", "title": "Dev",
     "hiringOrganization": {"@id": "https://cv.ee/#/schema/Organization/wise"}}
    </script></body></html>"""
    assert parse_detail(not_an_id, "https://www.cvkeskus.ee/dev-1").company is None

    textless_link = """<html><body><script type="application/ld+json">
    {"@type": "JobPosting", "title": "Dev",
     "hiringOrganization": {"@id": "https://cv.ee/#/schema/Organization/179285"}}
    </script>
    <a href="/wise-toopakkumised-179285"><img src="/logo.png"/></a>
    </body></html>"""
    assert parse_detail(textless_link, "https://www.cvkeskus.ee/dev-1").company is None

    empty_list = """<html><body><script type="application/ld+json">
    {"@type": "JobPosting", "title": "Dev", "hiringOrganization": []}
    </script></body></html>"""
    assert parse_detail(empty_list, "https://www.cvkeskus.ee/dev-1").company is None


def test_cvkeskus_accepts_a_bare_category_and_a_bare_keyword(make_ctx):
    ctx = make_ctx(ROUTES, options={"categories": 8, "keywords": "developer"})
    list(CvKeskus().fetch(ctx))

    params = query(search_calls(ctx)[0])
    assert [v for k, v in params if k == "search[categories][]"] == ["8"]
    assert ("search[keyword]", "developer") in params


def test_cvkeskus_keeps_paging_while_each_page_is_new(make_ctx):
    routes = {"start=30": EXTRA_PAGE, "1047845": "cvkeskus_detail_nojsonld.html", **ROUTES}
    ctx = make_ctx(routes, options={"max_details": 60})
    jobs = list(CvKeskus().fetch(ctx))

    assert [j.source_id for j in jobs] == ["1052794", "1053023", "1047845"]
    assert len(search_calls(ctx)) == 2  # the page budget, not a repeat, ends the loop


def test_cvkeskus_stops_quietly_when_a_later_page_runs_out(make_ctx):
    """Only a first page without cards is worth a warning; a short last page is normal."""
    routes = {"start=30": "<html><body>end of results</body></html>", **ROUTES}
    ctx = make_ctx(routes, options={"max_details": 60})
    jobs = list(CvKeskus().fetch(ctx))

    assert len(jobs) == 2
    assert len(search_calls(ctx)) == 2


def test_cvkeskus_warns_once_when_the_first_page_has_no_cards(make_ctx, caplog):
    ctx = make_ctx({SEARCH: "<html><body>no cards at all</body></html>"})
    with caplog.at_level("WARNING"):
        assert list(CvKeskus().fetch(ctx)) == []
    assert len(search_calls(ctx)) == 1
    assert "no job cards found" in caplog.text


def test_cvkeskus_stops_asking_further_keywords_once_it_has_enough(make_ctx):
    ctx = make_ctx(ROUTES, options={"keywords": ["developer", "engineer"], "max_details": 1})
    jobs = list(CvKeskus().fetch(ctx))

    assert len(jobs) == 1
    assert len(search_calls(ctx)) == 1  # the second keyword is never asked for
