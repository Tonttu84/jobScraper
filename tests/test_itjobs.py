from datetime import UTC, date, datetime
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from conftest import fixture_text

from jobscraper.http import SourceHTTPError
from jobscraper.sources.itjobs import (
    ITJobs,
    has_next_page,
    parse_detail,
    parse_pt_date,
    parse_search_html,
)

SEARCH = "/emprego"

MINIMAL_PAGE = "<html><body><div class='content-block'><p>Anúncio</p></div></body></html>"

ROUTES = {
    "page=2": "itjobs_search_p2.html",           # must come first: the URL also contains /emprego
    SEARCH: "itjobs_search.html",
    "/oferta/516462": "itjobs_detail.html",
    "/oferta/516600": "itjobs_detail_nojsonld.html",
    "/oferta/": lambda req: httpx.Response(200, text=MINIMAL_PAGE),
}
LISTING_ONLY = {"queries": ["software developer"], "max_pages": 1, "fetch_details": False}


def search_calls(ctx):
    return [c for c in ctx.http.calls if "/emprego" in str(c.url)]


def query(request) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(str(request.url)).query))


# --------------------------------------------------------------------------- search listing


def test_itjobs_parses_the_search_listing(make_ctx):
    ctx = make_ctx(ROUTES, options=LISTING_ONLY)
    jobs = list(ITJobs().fetch(ctx))

    # the card whose title lost its <a href> is skipped; the sidebar "Em destaque" ad is not a card
    assert [j.source_id for j in jobs] == ["516462", "516600", "516441", "516329"]
    j = jobs[0]
    assert j.source == "itjobs"
    assert j.url == (
        "https://www.itjobs.pt/oferta/516462/"
        "software-developer-mid-level-senior-angular-net-core-c-sql-server"
    )
    assert j.title == "Software Developer (Mid-Level / Senior) | Angular | .NET Core (C#) | SQL Server"
    assert j.company == "To-Be Portugal"
    assert j.location_raw == "Coimbra" and j.city == "Coimbra" and j.country == "PT"
    assert j.remote == "hybrid"  # the card's "Modelo de trabalho" icon says Híbrido
    assert j.description is None  # fetch_details: false
    # the listing only prints day + Portuguese month; the year is inferred, so only check the day
    assert (j.posted_at.month, j.posted_at.day) == (8, 28)


def test_itjobs_search_params_and_query_list(make_ctx):
    ctx = make_ctx(ROUTES, options={**LISTING_ONLY, "queries": ["C++", "engenheiro de software"]})
    list(ITJobs().fetch(ctx))

    assert len(search_calls(ctx)) == 2
    assert query(search_calls(ctx)[0]) == {"q": "C++", "sort": "date"}
    assert query(search_calls(ctx)[1])["q"] == "engenheiro de software"


def test_itjobs_optional_filters_are_passed_through(make_ctx):
    ctx = make_ctx(ROUTES, options={**LISTING_ONLY, "work_model": 1, "location": 14, "sort": "relevance"})
    list(ITJobs().fetch(ctx))
    assert query(search_calls(ctx)[0]) == {
        "q": "software developer", "sort": "relevance", "work_model": "1", "location": "14",
    }


def test_itjobs_a_remote_international_card_is_not_forced_to_portugal(make_ctx):
    ctx = make_ctx(ROUTES, options=LISTING_ONLY)
    j = list(ITJobs().fetch(ctx))[1]
    assert j.title == "Remote Python Developer"
    assert j.location_raw == "Internacional"
    assert j.country is None and j.city is None  # "Internacional" is the site's abroad bucket
    assert j.remote == "remote"


def test_itjobs_country_defaults_to_pt_and_city_is_the_first_one(make_ctx):
    ctx = make_ctx(ROUTES, options=LISTING_ONLY)
    jobs = list(ITJobs().fetch(ctx))
    lisbon, no_location = jobs[2], jobs[3]
    assert lisbon.location_raw == "Lisboa, Porto" and lisbon.city == "Lisboa"
    assert lisbon.country == "PT" and lisbon.remote == "hybrid"
    # a card with no location icon at all still belongs to the Portuguese board
    assert no_location.location_raw is None and no_location.city is None
    assert no_location.country == "PT" and no_location.remote == "onsite"


def test_itjobs_pages_and_dedupes_by_url(make_ctx):
    ctx = make_ctx(ROUTES, options={**LISTING_ONLY, "max_pages": 3})
    ids = [j.source_id for j in ITJobs().fetch(ctx)]

    # page 2 repeats 516329 and adds one new posting; its pager has no "next", so page 3 —
    # which the board would answer with a 404 — is never requested
    assert ids == ["516462", "516600", "516441", "516329", "516777"]
    assert len(search_calls(ctx)) == 2
    assert query(search_calls(ctx)[1])["page"] == "2"


def test_itjobs_a_404_past_the_last_page_ends_the_query_quietly(make_ctx):
    """The board answers 404 for a page beyond the end of a search ("C++" has one page)."""
    routes = {"page=2": lambda req: httpx.Response(404, text="Not Found"), SEARCH: "itjobs_search.html"}
    ctx = make_ctx(routes, options={"queries": ["C++", "engenheiro de software"],
                                    "max_pages": 3, "fetch_details": False})
    ids = [j.source_id for j in ITJobs().fetch(ctx)]

    assert ids == ["516462", "516600", "516441", "516329"]  # page 1 survives the dead page 2
    calls = search_calls(ctx)
    assert [query(c).get("page") for c in calls] == [None, "2", None]
    assert query(calls[2])["q"] == "engenheiro de software"  # the next query is still fetched


def test_itjobs_a_404_on_the_first_page_is_still_an_error(make_ctx):
    routes = {SEARCH: lambda req: httpx.Response(404, text="Not Found")}
    ctx = make_ctx(routes, options=LISTING_ONLY)
    with pytest.raises(SourceHTTPError):
        list(ITJobs().fetch(ctx))


def test_itjobs_a_server_error_on_a_later_page_is_still_an_error(make_ctx):
    routes = {"page=2": lambda req: httpx.Response(500, text="boom"), SEARCH: "itjobs_search.html"}
    ctx = make_ctx(routes, options={**LISTING_ONLY, "max_pages": 3})
    with pytest.raises(SourceHTTPError):
        list(ITJobs().fetch(ctx))


def test_itjobs_accepts_a_single_query_string(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": "C++", "max_pages": 1, "fetch_details": False})
    assert len(list(ITJobs().fetch(ctx))) == 4
    assert query(search_calls(ctx)[0])["q"] == "C++"


def test_itjobs_stops_on_a_page_without_cards(make_ctx):
    ctx = make_ctx({SEARCH: "<html><body>Sem resultados</body></html>"}, options={**LISTING_ONLY, "max_pages": 4})
    assert list(ITJobs().fetch(ctx)) == []
    assert len(search_calls(ctx)) == 1


def test_itjobs_respects_limit(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["a", "b"], "max_pages": 5}, limit=1)
    assert len(list(ITJobs().fetch(ctx))) == 1
    assert len(search_calls(ctx)) == 1  # discovery is lazy: no page 2, no second query


# --------------------------------------------------------------------------- posting pages


def test_itjobs_detail_adds_description_salary_and_the_exact_date(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["x"], "max_pages": 1, "max_details": 2})
    jobs = list(ITJobs().fetch(ctx))
    j = jobs[0]

    assert j.posted_at == datetime(2026, 8, 28, tzinfo=UTC)  # JSON-LD datePosted, not the day box
    assert j.employment_type == "Full-time"
    assert j.salary_text == "€20 000 - €40 000"
    assert j.city == "Coimbra" and j.country == "PT"
    assert "Angular" in j.description and "<p>" not in j.description
    # jobLocationType says TELECOMMUTE on this ad, but the page's own work model says Híbrido
    assert j.raw["jsonld"]["jobLocationType"] == "TELECOMMUTE"
    assert j.remote == "hybrid"
    assert j.raw["fields"]["referência"] == "CBR-20260828"

    detail_calls = [c for c in ctx.http.calls if "/oferta/" in str(c.url)]
    assert len(detail_calls) == 2  # max_details
    assert jobs[2].description is None and jobs[3].description is None


def test_itjobs_detail_without_json_ld_falls_back_to_the_page(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["x"], "max_pages": 1, "max_details": 2})
    j = list(ITJobs().fetch(ctx))[1]

    assert j.posted_at == datetime(2026, 9, 9, tzinfo=UTC)  # ".over-title small": 9 de Setembro de 2026
    assert j.company == "Integer Consulting"
    assert "Django ou FastAPI" in j.description
    assert "teletrabalho" in j.description
    assert "Guardar" not in j.description  # the action pills are not part of the ad
    assert "Mozilla/5.0" not in j.description  # nor is the hidden watermark
    assert "Candidatar a este anúncio" not in j.description
    assert j.remote == "remote" and j.country is None
    assert j.raw["jsonld"] is None


def test_itjobs_a_broken_posting_page_is_skipped_not_raised(make_ctx):
    routes = {"/oferta/516441": lambda req: httpx.Response(500, text="boom"), **ROUTES}
    ctx = make_ctx(routes, options={"queries": ["x"], "max_pages": 1})
    ids = [j.source_id for j in ITJobs().fetch(ctx)]
    assert ids == ["516462", "516600", "516329"]


# --------------------------------------------------------------------------- parser units


def test_parse_search_html_reads_the_daily_date_box():
    cards = parse_search_html(fixture_text("itjobs_search.html"))
    assert [c["date_text"] for c in cards] == ["28 ago", "9 set", "9 set", "4 set"]
    assert cards[0]["work_model"] == "Híbrido" and cards[0]["location"] == "Coimbra"
    assert cards[3]["location"] is None  # the commented-out schedule icon is not read as text
    assert parse_search_html(None) == [] and parse_search_html("") == []


def test_has_next_page_reads_the_pager_and_shrugs_when_there_is_none():
    page1, page2 = fixture_text("itjobs_search.html"), fixture_text("itjobs_search_p2.html")
    assert has_next_page(page1, 1) is True       # the pager links to page 2
    assert has_next_page(page1, 2) is False      # ...and to nothing beyond it
    assert has_next_page(page2, 2) is False      # the last page only links back
    # no pager at all (or no page): unknown, so paging must not stop on this signal
    assert has_next_page("<html><body>Sem resultados</body></html>", 1) is None
    assert has_next_page(None, 1) is None


THIN_CARDS = """
<div class="block borderless">
  <ul class="list-unstyled listing">
    <li><div class="list-title"><a class="title" href="/oferta/1/a">Sem detalhes</a></div></li>
    <li><div class="list-title"><a class="title" href="/oferta/2/b">Com horário</a></div>
        <div class="list-details"><i class="fa fa-map-marker"></i>&nbsp;Faro&nbsp;&nbsp;
          <i class="fa fa-clock-o"></i>&nbsp;Full-time&nbsp;&nbsp;
          <i class="fa fa-tag"></i>&nbsp;
        </div></li>
  </ul>
</div>
"""


def test_parse_search_html_handles_cards_without_details_or_a_day_box():
    cards = parse_search_html(THIN_CARDS)
    assert cards[0] == {
        "url": "https://www.itjobs.pt/oferta/1/a", "title": "Sem detalhes", "company": None,
        "location": None, "work_model": None, "schedule": None, "date_text": None,
    }
    # the schedule icon is only rendered on some ads, and a trailing icon can carry no text
    assert cards[1]["location"] == "Faro" and cards[1]["schedule"] == "Full-time"
    assert cards[1]["work_model"] is None


BROKEN_LD_PAGE = """
<html><head><script type="application/ld+json">{ not json }</script></head>
<body><div class="block job-header"><h1 class="title">Programador Backend</h1>
<h4 class="thin grey"><a href="/empresa/acme">Acme</a></h4></div>
<div class="content-block"><p>Regime de teletrabalho a 100%.</p></div></body></html>
"""


def test_parse_detail_survives_broken_json_ld_and_reads_the_work_model_from_the_text():
    job = parse_detail(BROKEN_LD_PAGE, "https://www.itjobs.pt/oferta/1234/programador-backend")
    assert job.title == "Programador Backend" and job.company == "Acme"
    assert job.raw["jsonld"] is None and job.posted_at is None
    assert job.country == "PT" and job.remote == "remote"  # "teletrabalho" in the ad text

    hybrid = parse_detail(
        BROKEN_LD_PAGE.replace("teletrabalho a 100%", "modelo híbrido"),
        "https://www.itjobs.pt/oferta/1235/programador-backend",
    )
    assert hybrid.remote == "hybrid"


def test_parse_pt_date_handles_the_forms_the_site_and_its_ads_use():
    today = date(2026, 9, 9)
    assert parse_pt_date("9 set", today) == datetime(2026, 9, 9, tzinfo=UTC)
    assert parse_pt_date("28 ago", today) == datetime(2026, 8, 28, tzinfo=UTC)
    assert parse_pt_date(" 4 de Setembro de 2026", today) == datetime(2026, 9, 4, tzinfo=UTC)
    # no year in the day box: a month still ahead of today belongs to last year
    assert parse_pt_date("15 dez", today) == datetime(2025, 12, 15, tzinfo=UTC)
    assert parse_pt_date("hoje", today) == datetime(2026, 9, 9, tzinfo=UTC)
    assert parse_pt_date("Ontem", today) == datetime(2026, 9, 8, tzinfo=UTC)
    assert parse_pt_date("anteontem", today) == datetime(2026, 9, 7, tzinfo=UTC)
    assert parse_pt_date("há 3 dias", today) == datetime(2026, 9, 6, tzinfo=UTC)
    assert parse_pt_date("há 2 semanas", today) == datetime(2026, 8, 26, tzinfo=UTC)
    assert parse_pt_date("há 5 horas", today) == datetime(2026, 9, 9, tzinfo=UTC)
    assert parse_pt_date("2026-08-28", today) == datetime(2026, 8, 28, tzinfo=UTC)
    assert parse_pt_date("31 fev", today) is None  # not a date
    assert parse_pt_date("brevemente", today) is None
    assert parse_pt_date("", today) is None and parse_pt_date("   ", today) is None
    assert parse_pt_date(None) is None


JSON_LD_PAGE = """
<html><head><script type="application/ld+json">
{"@context":"https://schema.org/","@type":"JobPosting",
 "title":"FullStack Developer (Java &amp; React)","description":"<p>Equipa &amp; projeto<\\/p>",
 "datePosted":"2026-09-09","employmentType":"FULL_TIME",
 "jobLocationType":"TELECOMMUTE",
 "hiringOrganization":{"@type":"Organization","name":"Integer &amp; Co"},
 "jobLocation":[{"@type":"Place","address":{"@type":"PostalAddress",
   "addressLocality":"International","addressRegion":"International","addressCountry":"PT"}}]}
</script></head><body>
<div class="item-details"><ul><li><div class="info"><span class="title">Localidade</span>
<span class="field">Portugal - <a href="/local/international">International</a></span></div></li>
<li><div class="info"><span class="title">Modelo de trabalho</span><span class="field">Híbrido</span></div></li>
</ul></div></body></html>
"""


def test_parse_detail_unescapes_json_ld_and_does_not_claim_international_ads_for_portugal():
    """The board stamps addressCountry PT on its International ads, and escapes its strings."""
    job = parse_detail(JSON_LD_PAGE, "https://www.itjobs.pt/oferta/516678/fullstack-developer-java-react")
    assert job.title == "FullStack Developer (Java & React)"
    assert job.company == "Integer & Co"
    assert job.location_raw == "International"
    assert job.country is None and job.city is None
    assert job.remote == "hybrid" and job.employment_type == "FULL_TIME"


def test_parse_detail_without_a_title_is_dropped():
    assert parse_detail("<html><body>404</body></html>", "https://www.itjobs.pt/oferta/1/x") is None


def test_parse_detail_uses_the_card_when_the_page_is_empty():
    card = {"title": "Backend Developer", "company": "Acme", "location": "Braga",
            "work_model": "Presencial", "date_text": "9 set"}
    job = parse_detail(None, "https://www.itjobs.pt/oferta/999/backend-developer", card, today=date(2026, 9, 9))
    assert job.title == "Backend Developer" and job.company == "Acme"
    assert job.source_id == "999" and job.country == "PT" and job.city == "Braga"
    assert job.remote == "onsite" and job.description is None
    assert job.posted_at == datetime(2026, 9, 9, tzinfo=UTC)
