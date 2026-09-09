from datetime import UTC, datetime

import httpx
import pytest
from conftest import fixture_text

from jobscraper.sources.expressoemprego import (
    ExpressoEmprego,
    parse_detail,
    parse_meta,
    parse_search_html,
    parse_total,
    remote_kind,
    search_url,
    slugify,
)

SEARCH = "/emprego/pesquisa/"
IT_PATH = "habilitacao/tecnologias-de-informacao"
ENBW = "2465761"
ADECCO = "2462786"

ROUTES = {
    SEARCH: "expressoemprego_search.html",
    "/emprego/software-developer-": "expressoemprego_detail.html",
    "/emprego/analista-de-contact-center": "expressoemprego_detail_hibrido.html",
    "/emprego/comprador---construcao": "expressoemprego_detail_xslt.html",
}


def search_calls(ctx):
    return [str(c.url) for c in ctx.http.calls if SEARCH in str(c.url)]


# --------------------------------------------------------------------- URL model


def test_slugify_matches_the_sites_own_encodeuriextended():
    assert slugify("software developer") == "software-developer"
    assert slugify("Tecnologias de Informação") == "tecnologias-de-informacao"
    assert slugify("C++") == "c--"
    assert slugify("  Engenheiro de Software  ") == "engenheiro-de-software"


def test_search_url_fills_the_unused_route_slots_with_their_own_names():
    # A query alone: every slot after it is a placeholder, so the whole tail is dropped.
    assert search_url(query="software developer") == (
        "https://www.expressoemprego.pt/emprego/pesquisa/software-developer?order=data"
    )
    # The IT function sits in slot 8, so slots 1-7 have to be spelled out.
    assert search_url(funcao="Tecnologias de Informação") == (
        "https://www.expressoemprego.pt/emprego/pesquisa/"
        "query/localizacao/reference/data/tipo/exp/habilitacao/"
        "tecnologias-de-informacao?order=data"
    )
    assert search_url(query="programador", funcao="Tecnologias de Informação") == (
        "https://www.expressoemprego.pt/emprego/pesquisa/"
        "programador/localizacao/reference/data/tipo/exp/habilitacao/"
        "tecnologias-de-informacao?order=data"
    )
    assert search_url(funcao="Tecnologias de Informação", page=3).endswith(
        "tecnologias-de-informacao?order=data&page=3"
    )
    assert search_url(query="x", page=1).endswith("/pesquisa/x?order=data")  # page 1 is implicit


def test_search_url_without_criteria_browses_the_whole_board():
    assert search_url() == "https://www.expressoemprego.pt/ofertas-emprego?order=data"
    assert search_url(page=2).endswith("/ofertas-emprego?order=data&page=2")


# ------------------------------------------------------------------- card parsing


def test_parse_search_html_reads_the_result_cards():
    records = parse_search_html(fixture_text("expressoemprego_search.html"))

    assert [r["source_id"] for r in records] == [ENBW, ADECCO, "2466292"]  # broken card dropped
    enbw = records[0]
    assert enbw["url"] == (
        "https://www.expressoemprego.pt/emprego/"
        "software-developer-(java---spring-boot)-(f-m-d)/2465761"
    )
    assert enbw["title"] == "Software Developer (Java / Spring Boot) (F/M/D)"
    assert enbw["company"] == "EnBW Energie Baden-Württemberg AG"
    assert enbw["location"] is None  # the board leaves the location blank on this one
    assert enbw["posted"] == datetime(2026, 9, 8, tzinfo=UTC)
    assert records[1]["location"] == "Lisboa, Portugal"
    assert records[2]["company"] == "Michael Page Portugal"
    assert records[2]["teaser"].startswith("Procuramos um/a Comprador/a")


def test_parse_meta_splits_the_date_location_reference_line():
    assert parse_meta("26.08.2026 | Lisboa, Portugal | Referência: 2462786") == (
        datetime(2026, 8, 26, tzinfo=UTC),
        "Lisboa, Portugal",
    )
    assert parse_meta("08.09.2026 | | Referência: 2465761") == (
        datetime(2026, 9, 8, tzinfo=UTC),
        None,
    )
    assert parse_meta("26.08.2026 | Marinha Grande") == (
        datetime(2026, 8, 26, tzinfo=UTC),
        "Marinha Grande",
    )
    assert parse_meta("") == (None, None)


# ------------------------------------------------------------------------- fetch


def test_expressoemprego_fetches_the_it_function_and_normalizes(make_ctx):
    ctx = make_ctx(ROUTES)
    jobs = list(ExpressoEmprego().fetch(ctx))

    assert [j.source_id for j in jobs] == [ENBW, ADECCO, "2466292"]
    j = jobs[0]
    assert j.source == "expressoemprego"
    assert j.url.endswith("/emprego/software-developer-(java---spring-boot)-(f-m-d)/2465761")
    assert j.title == "Software Developer (Java / Spring Boot) (F/M/D)"  # og:title, not the "(M/F)" h1
    assert j.company == "EnBW Energie Baden-Württemberg AG"
    assert j.country == "PT"  # no location on this ad: the board is Portuguese
    assert j.city is None and j.location_raw is None
    assert j.remote == "hybrid"  # "Hybrid working" in the ad body
    assert j.posted_at == datetime(2026, 9, 8, tzinfo=UTC)
    assert "EnBW Tech Hub in Portugal" in j.description
    assert "<strong>" not in j.description
    assert j.employment_type is None and j.seniority_raw is None
    assert j.raw["card"]["source_id"] == ENBW

    adecco = jobs[1]
    assert adecco.location_raw == "Lisboa, Portugal"
    assert adecco.city == "Lisboa" and adecco.country == "PT"
    assert adecco.remote == "hybrid"  # "Regime Híbrido" / "teletrabalho"
    assert adecco.company == "Adecco"
    assert adecco.posted_at == datetime(2026, 8, 26, tzinfo=UTC)


def test_expressoemprego_searches_the_it_function_by_default(make_ctx):
    ctx = make_ctx(ROUTES)
    list(ExpressoEmprego().fetch(ctx))

    assert IT_PATH in search_calls(ctx)[0]
    assert "order=data" in search_calls(ctx)[0]


def test_expressoemprego_drops_the_stylesheet_from_an_xslt_ad_body(make_ctx):
    """Some employers' ads carry their own <style> block inside the description div."""
    ctx = make_ctx(ROUTES)
    job = list(ExpressoEmprego().fetch(ctx))[2]

    assert job.title == "Comprador - Construção | Lisboa"
    assert "border-top-right-radius" not in job.description
    assert "Perfil desejado" in job.description
    assert job.city == "Lisboa" and job.country == "PT"
    assert job.remote == "unknown"


def test_expressoemprego_pages_while_the_board_says_there_is_more(make_ctx):
    """The fixture page claims 50 hits but carries 3 cards, so page 2 is fetched."""
    ctx = make_ctx(ROUTES, options={"max_pages": 5, "queries": []})
    jobs = list(ExpressoEmprego().fetch(ctx))

    calls = search_calls(ctx)
    assert len(calls) == 2  # page 2 is asked for, then repeats page 1 and the loop stops
    assert "page=" not in calls[0] and calls[1].endswith("page=2")
    assert len(jobs) == 3


def test_expressoemprego_stops_on_an_empty_results_page(make_ctx):
    routes = {"page=2": "expressoemprego_search_empty.html", **ROUTES}
    ctx = make_ctx(routes, options={"max_pages": 4, "queries": []})
    jobs = list(ExpressoEmprego().fetch(ctx))

    assert len(search_calls(ctx)) == 2
    assert len(jobs) == 3


def test_expressoemprego_stops_when_the_hit_count_is_exhausted(make_ctx):
    page = fixture_text("expressoemprego_search.html").replace("<b>50</b>", "<b>3</b>")
    routes = dict(ROUTES)
    routes[SEARCH] = lambda req: httpx.Response(200, text=page)
    ctx = make_ctx(routes, options={"queries": [], "max_pages": 5})
    jobs = list(ExpressoEmprego().fetch(ctx))

    assert len(search_calls(ctx)) == 1  # 3 of 3 seen, so there is no page 2 to ask for
    assert len(jobs) == 3


def test_expressoemprego_accepts_a_single_query_string(make_ctx):
    ctx = make_ctx(ROUTES, options={"funcao": None, "queries": "programador", "max_pages": 1})
    list(ExpressoEmprego().fetch(ctx))

    assert [c.split("/pesquisa/")[1].split("?")[0] for c in search_calls(ctx)] == ["programador"]


def test_expressoemprego_notes_a_search_that_simply_has_no_hits(make_ctx, caplog):
    """The board drops the count line as well, so this is not a markup change."""
    ctx = make_ctx({SEARCH: "expressoemprego_search_empty.html"}, options={"queries": []})

    with caplog.at_level("INFO"):
        assert list(ExpressoEmprego().fetch(ctx)) == []
    assert "no hits" in caplog.text and "markup change" not in caplog.text
    assert len(search_calls(ctx)) == 1


def test_expressoemprego_warns_when_a_page_has_hits_but_no_cards(make_ctx, caplog):
    counted = fixture_text("expressoemprego_search_empty.html").replace(
        "</body>",
        '<span class="px14"><b>50</b></span> '
        '<span class="px15">empregos para a sua pesquisa</span></body>',
    )
    ctx = make_ctx({SEARCH: lambda req: httpx.Response(200, text=counted)},
                   options={"queries": []})

    with caplog.at_level("WARNING"):
        assert list(ExpressoEmprego().fetch(ctx)) == []
    assert "50 hits but no cards" in caplog.text


def test_parse_total_reads_the_boards_own_hit_count():
    assert parse_total(fixture_text("expressoemprego_search.html")) == 50
    assert parse_total('<span class="px14"><b>1 880</b></span> <span class="px15">'
                       "empregos para a sua pesquisa</span>") == 1880
    assert parse_total(fixture_text("expressoemprego_search_empty.html")) is None
    assert parse_total(None) is None


def test_expressoemprego_runs_the_queries_after_the_function(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["C++", "programador"], "max_pages": 1})
    jobs = list(ExpressoEmprego().fetch(ctx))

    calls = search_calls(ctx)
    assert [c.split("/pesquisa/")[1].split("?")[0] for c in calls] == [
        "query/localizacao/reference/data/tipo/exp/habilitacao/tecnologias-de-informacao",
        "c--",
        "programador",
    ]
    assert len(jobs) == 3  # the same three ads: a URL is only ever emitted once


def test_expressoemprego_can_skip_the_function_pass(make_ctx):
    ctx = make_ctx(ROUTES, options={"funcao": None, "queries": ["programador"], "max_pages": 1})
    assert [c.split("/pesquisa/")[1].split("?")[0] for c in search_calls(ctx)] == []
    list(ExpressoEmprego().fetch(ctx))
    assert [c.split("/pesquisa/")[1].split("?")[0] for c in search_calls(ctx)] == ["programador"]


def test_expressoemprego_without_details_uses_the_card_teaser(make_ctx):
    ctx = make_ctx({SEARCH: "expressoemprego_search.html"}, options={"fetch_details": False})
    jobs = list(ExpressoEmprego().fetch(ctx))

    assert len(jobs) == 3
    assert [str(c.url) for c in ctx.http.calls] == search_calls(ctx)  # no detail requests
    assert jobs[0].description.startswith("About the company")
    assert jobs[0].title == "Software Developer (Java / Spring Boot) (F/M/D)"
    assert jobs[0].posted_at == datetime(2026, 9, 8, tzinfo=UTC)
    assert jobs[1].city == "Lisboa" and jobs[1].country == "PT"


def test_expressoemprego_caps_discovery_with_max_details(make_ctx):
    ctx = make_ctx(ROUTES, options={"max_details": 2})
    jobs = list(ExpressoEmprego().fetch(ctx))

    assert [j.source_id for j in jobs] == [ENBW, ADECCO]
    assert len(search_calls(ctx)) == 1  # the cap stops the pager too


def test_expressoemprego_respects_limit(make_ctx):
    ctx = make_ctx(ROUTES, limit=1)

    assert len(list(ExpressoEmprego().fetch(ctx))) == 1
    # A limit also caps discovery, so `probe` doesn't crawl the whole IT function first.
    assert len(search_calls(ctx)) == 1
    assert len(ctx.http.calls) == 2  # one search page, then one ad page: details are lazy


def test_expressoemprego_skips_a_broken_detail_page(make_ctx):
    routes = dict(ROUTES)
    routes["/emprego/software-developer-"] = lambda req: httpx.Response(500, text="boom")
    ctx = make_ctx(routes)

    assert [j.source_id for j in ExpressoEmprego().fetch(ctx)] == [ADECCO, "2466292"]


def test_expressoemprego_raises_when_the_search_endpoint_breaks(make_ctx):
    from jobscraper.http import SourceHTTPError

    ctx = make_ctx({SEARCH: lambda req: httpx.Response(503, text="down")})
    with pytest.raises(SourceHTTPError):
        list(ExpressoEmprego().fetch(ctx))


# ------------------------------------------------------------ Portuguese wording


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Trabalho 100% remoto", "remote"),
        ("Regime de teletrabalho", "remote"),
        ("Possibilidade de trabalho à distância", "remote"),
        ("Regime híbrido de 2 dias de teletrabalho", "hybrid"),
        ("Modelo hibrido (3 dias no escritório)", "hybrid"),
        ("Hybrid working model", "hybrid"),
        ("Fully remote team", "remote"),
        ("Presença diária no escritório", "unknown"),
        (None, "unknown"),
        # The same words about kit rather than people: seen live on the board's sysadmin ads.
        ("Conhecimentos de Microsoft 365 e ambientes híbridos", "unknown"),
        ("Suporte remoto e presencial aos utilizadores", "unknown"),
        ("Configuração de acessos remotos e VPN", "unknown"),
        ("Formação à distância incluída", "unknown"),
        # …but a real work-mode line in the same ad still wins.
        ("Ambientes híbridos de cloud; regime híbrido de 2 dias", "hybrid"),
    ],
)
def test_remote_kind_reads_the_portuguese_labels(text, expected):
    assert remote_kind(text) == expected


def test_parse_detail_falls_back_to_the_card(make_ctx):
    card = {
        "url": "https://www.expressoemprego.pt/emprego/x/lisboa/1",
        "source_id": "1",
        "title": "Programador Júnior",
        "company": "Acme",
        "location": "Porto",
        "posted": datetime(2026, 1, 2, tzinfo=UTC),
        "teaser": "Trabalho em regime de teletrabalho.",
    }
    job = parse_detail("<html><body>nothing</body></html>", card["url"], card)

    assert job.title == "Programador Júnior" and job.company == "Acme"
    assert job.city == "Porto" and job.country == "PT"
    assert job.remote == "remote"
    assert job.posted_at == datetime(2026, 1, 2, tzinfo=UTC)


def test_parsers_tolerate_junk():
    assert parse_search_html(None) == []
    assert parse_search_html("<html><body>nothing</body></html>") == []
    assert parse_search_html(fixture_text("expressoemprego_search_empty.html")) == []
    # No detail page and no card title: nothing to normalize, but no exception either.
    assert parse_detail(None, "https://www.expressoemprego.pt/emprego/x/1") is None
    assert parse_detail("", "https://www.expressoemprego.pt/emprego/x/1", {"url": "u"}) is None
