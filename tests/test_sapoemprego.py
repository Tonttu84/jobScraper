from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from conftest import fixture_json, fixture_text

from jobscraper.http import SourceHTTPError
from jobscraper.sources.sapoemprego import (
    SEARCH_API,
    SapoEmprego,
    build_job,
    page_total,
    parse_detail_html,
    parse_search,
)

SEARCH = "offers/search"
DETAIL = "software-developer?id="
DETAIL_4GL = "software-developer-4gl-068"
DETAIL_HYBRID = "software-developer-net-vba-hybrid-porto"

NO_JSONLD = "<html><body><h1>Oferta</h1></body></html>"


def _plain(_req):
    return httpx.Response(200, text=NO_JSONLD)


ROUTES = {
    SEARCH: "sapoemprego_search.json",
    DETAIL_4GL: _plain,
    DETAIL_HYBRID: _plain,
    DETAIL: "sapoemprego_detail.html",
}


def search_calls(ctx):
    return [c for c in ctx.http.calls if SEARCH in str(c.url)]


def query(request):
    return dict(parse_qsl(urlsplit(str(request.url)).query))


def test_sapoemprego_search_and_detail(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["software developer"]})
    jobs = list(SapoEmprego().fetch(ctx))

    assert len(jobs) == 3  # the image-highlight banner in the payload is not a posting
    j = jobs[0]
    assert j.source == "sapoemprego"
    assert j.source_id == "c88530f3-6fe8-488e-a901-41e95521df36"
    assert j.url == "https://emprego.sapo.pt/offers/software-developer?id=c88530f3-6fe8-488e-a901-41e95521df36"
    assert j.title == "Software Developer"
    assert j.company == "Olisipo"
    assert j.country == "PT" and j.city == "Lisboa"
    assert j.location_raw == "Lisboa"
    assert j.remote == "onsite"  # li.workhome says "Presencial"
    assert j.employment_type == "full_time"
    assert j.posted_at == datetime(2026, 8, 27, tzinfo=UTC)
    assert j.tags == ["Informática e Tecnologias"]
    assert j.salary_text is None  # the site prints "A definir" when there is no range
    assert j.seniority_raw is None  # SAPO never labels seniority
    # The full description comes off the detail page, not the 250-char listing teaser.
    assert "Conhecimentos sólidos de T-SQL" in j.description
    assert "<br>" not in j.description and len(j.description) > 500
    assert j.raw["jsonld"]["validThrough"] == "2026-09-17T23:59:59+00:00"


def test_sapoemprego_calls_the_xhr_search_endpoint(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["software developer"]})
    list(SapoEmprego().fetch(ctx))

    call = search_calls(ctx)[0]
    assert str(call.url).startswith(SEARCH_API)
    assert query(call) == {"pesquisa": "software developer", "pagina": "1"}
    # Without this header the endpoint answers HTTP 400 instead of JSON.
    assert call.headers["X-Requested-With"] == "XMLHttpRequest"


def test_sapoemprego_stops_paging_when_the_page_is_covered(make_ctx):
    """``pagination.total`` (3) is below one page (9), so page 2 is never requested."""
    ctx = make_ctx(ROUTES, options={"queries": ["software developer"], "max_pages": 5})
    list(SapoEmprego().fetch(ctx))
    assert len(search_calls(ctx)) == 1


def test_sapoemprego_waits_out_a_419(make_ctx, monkeypatch):
    """The board answers 419 (not 429) when the 15-a-minute search budget is spent."""
    slept: list[float] = []
    monkeypatch.setattr("jobscraper.sources.sapoemprego.time.sleep", slept.append)
    seen = {"n": 0}

    def throttled_then_ok(req):
        seen["n"] += 1
        if seen["n"] == 1:
            return httpx.Response(419, json=[])
        return httpx.Response(200, text=fixture_text("sapoemprego_search.json"))

    routes = {**ROUTES, SEARCH: throttled_then_ok}
    ctx = make_ctx(routes, options={"queries": ["software developer"], "throttle_pause": 30})
    jobs = list(SapoEmprego().fetch(ctx))

    assert len(jobs) == 3  # the retry after the pause carries the run
    assert slept == [30]
    assert len(search_calls(ctx)) == 2


def test_sapoemprego_reraises_a_second_419(make_ctx, monkeypatch):
    monkeypatch.setattr("jobscraper.sources.sapoemprego.time.sleep", lambda _s: None)
    routes = {**ROUTES, SEARCH: lambda req: httpx.Response(419, json=[])}
    ctx = make_ctx(routes, options={"queries": ["software developer"]})
    with pytest.raises(SourceHTTPError) as excinfo:
        list(SapoEmprego().fetch(ctx))
    assert excinfo.value.status == 419


def test_sapoemprego_paces_its_search_calls(make_ctx, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("jobscraper.sources.sapoemprego.time.sleep", slept.append)
    ctx = make_ctx(ROUTES, options={"queries": ["a", "b", "c"]})
    list(SapoEmprego().fetch(ctx))
    assert slept == [4.5, 4.5]  # between the calls, never before the first


def test_sapoemprego_dedupes_by_url_across_queries(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["software developer", "programador"], "search_delay": 0})
    jobs = list(SapoEmprego().fetch(ctx))

    assert len(search_calls(ctx)) == 2  # both queries are searched
    assert len(jobs) == 3  # ... but the same postings are emitted once
    assert [q["pesquisa"] for q in map(query, search_calls(ctx))] == ["software developer", "programador"]


def test_sapoemprego_falls_back_to_the_listing_record(make_ctx):
    """A posting page without JSON-LD still yields the card's title/company/date."""
    ctx = make_ctx(ROUTES, options={"queries": ["software developer"]})
    j = list(SapoEmprego().fetch(ctx))[1]

    assert j.title == "Software Developer 4GL 068"
    assert j.company == "MILESTONE II TECHNOLOGY, S.A."
    assert j.country == "PT" and j.location_raw == "Lisboa"
    assert j.employment_type == "full_time"  # from the card's job_work_hours
    assert j.posted_at == datetime(2026, 8, 27, tzinfo=UTC)
    assert "A Milestone está à procura" in j.description  # the 250-char listing teaser
    assert len(j.description) <= 250


def test_sapoemprego_survives_a_broken_detail_page(make_ctx):
    routes = dict(ROUTES)
    routes[DETAIL] = lambda req: httpx.Response(500, text="boom")
    ctx = make_ctx(routes, options={"queries": ["software developer"]})
    jobs = list(SapoEmprego().fetch(ctx))

    assert len(jobs) == 3  # nothing is dropped; the card carries the posting
    assert jobs[0].title == "Software Developer" and jobs[0].company == "Olisipo"


def test_sapoemprego_can_skip_detail_pages(make_ctx):
    ctx = make_ctx({SEARCH: "sapoemprego_search.json"},
                   options={"fetch_details": False, "search_delay": 0})
    jobs = list(SapoEmprego().fetch(ctx))

    assert len(jobs) == 3
    assert not [c for c in ctx.http.calls if SEARCH not in str(c.url)]
    # "Hybrid (Porto)" is only in the title; the listing has no work-model field at all.
    assert jobs[2].remote == "hybrid"


def test_sapoemprego_caps_detail_requests(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["software developer"], "max_details": 1})
    jobs = list(SapoEmprego().fetch(ctx))

    assert len(jobs) == 3
    assert len([c for c in ctx.http.calls if SEARCH not in str(c.url)]) == 1


def test_sapoemprego_respects_limit(make_ctx):
    ctx = make_ctx(ROUTES, limit=1)
    assert len(list(SapoEmprego().fetch(ctx))) == 1


def test_sapoemprego_reads_a_teletrabalho_posting():
    detail = parse_detail_html(fixture_text("sapoemprego_detail_remote.html"))
    card = {
        "id": "97c2f106-079e-46e6-a774-a5443f845227",
        "offer_name": "Senior .NET Full Stack Developer | Remoto",
        "company_name": "Integer Consulting",
        "location": "Remoto",
        "job_country": "Portugal",
        "publication_date": "2026-08-17",
        "link": "https://emprego.sapo.pt/offers/senior-net-full-stack-developer-remoto?id=97c2f106-079e-46e6-a774-a5443f845227",
    }
    job = build_job(card, detail)

    assert job.title == "Senior .NET Full Stack Developer | Remoto"
    assert job.company == "Integer Consulting"
    assert job.country == "PT"
    assert job.city is None  # "Remoto" is a work model, never a city
    assert job.location_raw == "Remoto"
    assert job.remote == "remote"
    assert job.remote_region == "Portugal"  # applicantLocationRequirements
    assert job.posted_at == datetime(2026, 8, 17, tzinfo=UTC)
    assert "100% remoto" in job.description


def test_parse_search_skips_banners_and_rejects_junk():
    records = parse_search(fixture_json("sapoemprego_search.json"))
    assert [r["offer_name"] for r in records] == [
        "Software Developer",
        "Software Developer 4GL 068",
        "Software Developer .NET, VBA  – Hybrid (Porto)",
    ]
    assert parse_search({"offers": []}) == []
    with pytest.raises(SourceHTTPError):
        parse_search({"message": "Unauthenticated."})
    with pytest.raises(SourceHTTPError):
        parse_search("<html>a captcha page</html>")


def test_sapoemprego_reraises_a_broken_search_endpoint(make_ctx):
    """A 419 is throttling and is waited out; anything else means the site changed — raise."""
    routes = {**ROUTES, SEARCH: lambda req: httpx.Response(503, text="maintenance")}
    ctx = make_ctx(routes, options={"queries": ["software developer"]})
    with pytest.raises(SourceHTTPError) as excinfo:
        list(SapoEmprego().fetch(ctx))
    assert excinfo.value.status == 503


def test_parsers_tolerate_junk():
    assert page_total({"pagination": {"total": 47}}) == 47
    assert page_total({"pagination": {"total": "muitos"}}) is None
    assert page_total({"pagination": None}) is None
    assert page_total([]) is None
    assert parse_detail_html(None) == {"jsonld": {}, "meta": {}}
    assert parse_detail_html("<html><script type='application/ld+json'>{oops</script></html>") == {
        "jsonld": {},
        "meta": {},
    }
    assert build_job({}) is None  # no url, no title
    assert build_job({"link": "https://emprego.sapo.pt/offers/x?id=1"}) is None  # no title


def test_sapoemprego_accepts_list_shaped_jsonld():
    """``@type`` as a list and several ``jobLocation`` entries are both legal schema.org."""
    html = """<html><head><script type="application/ld+json">
    {"@type": ["JobPosting"], "title": "Dev", "datePosted": "2026-09-01",
     "jobLocation": ["not a node", {"@type": "Place", "address": {"addressLocality": "Porto",
                     "addressCountry": "PT"}}],
     "hiringOrganization": {"name": "ACME"}}
    </script></head><body></body></html>"""
    job = build_job({"link": "https://emprego.sapo.pt/offers/x?id=1"}, parse_detail_html(html))
    assert job.title == "Dev" and job.company == "ACME"
    assert job.city == "Porto" and job.country == "PT"


def test_sapoemprego_reads_the_jsonld_remote_markers():
    """A remote ad carries ``jobLocationType`` and SAPO's own sentence in the description."""
    card = {"link": "https://emprego.sapo.pt/offers/x?id=1", "offer_name": "Engineer", "location": "Aveiro"}
    telecommute = {"jsonld": {"jobLocationType": "TELECOMMUTE",
                              "applicantLocationRequirements": {"name": "Portugal"}}, "meta": {}}
    assert build_job(card, telecommute).remote == "remote"

    note = {"jsonld": {"description": "<p>Regime de trabalho híbrido.</p>"}, "meta": {}}
    assert build_job(card, note).remote == "hybrid"


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Lisboa", "Lisboa"),
        ("Coimbra, Coimbra, Portugal", "Coimbra"),
        ("Av. El Dorado #92 - 32, Bogotá,", "Bogotá"),  # employers paste whole addresses in
        ("Rua 25 de Abril 12", "Rua 25 de Abril 12"),  # nothing better on offer: keep it as is
        ("Remoto", None),
        ("Portugal", None),
    ],
)
def test_sapoemprego_finds_the_town_in_a_location_label(location, expected):
    card = {"link": "https://emprego.sapo.pt/offers/x?id=1", "offer_name": "Dev", "location": location}
    assert build_job(card).city == expected


def test_sapoemprego_ignores_a_whole_country_ad():
    card = {"link": "https://emprego.sapo.pt/offers/x?id=1", "offer_name": "Dev",
            "job_district": "Lisboa", "job_district_all": True, "location": "Todo o país"}
    assert build_job(card).city is None


def test_sapoemprego_accepts_a_single_query_string(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": "programador"})
    assert len(list(SapoEmprego().fetch(ctx))) == 3
    assert query(search_calls(ctx)[0])["pesquisa"] == "programador"


def test_sapoemprego_stops_on_an_empty_page(make_ctx):
    ctx = make_ctx({SEARCH: {"offers": [], "pagination": {"total": "many"}}},
                   options={"queries": ["nothing at all"]})
    assert list(SapoEmprego().fetch(ctx)) == []
    assert len(search_calls(ctx)) == 1  # an empty page ends the query, junk `total` and all


@pytest.mark.parametrize(
    ("work_model", "title", "location", "expected"),
    [
        ("Presencial", "Software Developer", "Lisboa", "onsite"),
        ("Teletrabalho", "Software Developer", "Remoto", "remote"),
        ("Híbrido", "Software Developer", "Lisboa", "hybrid"),
        # The employer's own label loses to a clearer one in the ad text.
        ("Presencial", "Software Developer – Hybrid (Porto)", "Lisboa", "hybrid"),
        ("Presencial", "Consultor DevOps (m/f) – Remoto", "Lisboa", "remote"),
        (None, "Operador de Call Center - Teletrabalho", "Faro", "remote"),
        (None, "Técnico de Suporte | Regime Híbrido", "Porto", "hybrid"),
        (None, "Software Developer", "Lisboa", "unknown"),
    ],
)
def test_sapoemprego_reads_the_portuguese_work_models(work_model, title, location, expected):
    card = {"link": "https://emprego.sapo.pt/offers/x?id=1", "offer_name": title, "location": location}
    detail = {"jsonld": {}, "meta": {"workhome": work_model} if work_model else {}}
    assert build_job(card, detail).remote == expected


@pytest.mark.parametrize(
    ("job_country", "location", "expected"),
    [
        ("Portugal", "Lisboa", "PT"),
        ("Colômbia", "Bogotá", "CO"),
        ("Angola", "Luanda", "AO"),
        ("Alemanha", "Berlim", "DE"),
        (None, "Porto", "PT"),  # SAPO is a Portuguese board: no country given means Portugal
        ("Tuvalu", "Funafuti", None),  # unknown name: better empty than wrongly filed as PT
    ],
)
def test_sapoemprego_maps_portuguese_country_names(job_country, location, expected):
    card = {
        "link": "https://emprego.sapo.pt/offers/x?id=1",
        "offer_name": "Dev",
        "location": location,
        "job_country": job_country,
    }
    assert build_job(card).country == expected


def test_sapoemprego_keeps_a_real_salary_range():
    detail = {"jsonld": {}, "meta": {"salary": "De 15.000€ a 25.000€", "contract": "Sem termo"}}
    card = {"link": "https://emprego.sapo.pt/offers/x?id=1", "offer_name": "Engenheiro MES"}
    job = build_job(card, detail)
    assert job.salary_text == "De 15.000€ a 25.000€"
    assert job.raw["meta"]["contract"] == "Sem termo"


def test_sapoemprego_drops_the_placeholder_salary():
    detail = {"jsonld": {}, "meta": {"salary": "A definir"}}
    card = {"link": "https://emprego.sapo.pt/offers/x?id=1", "offer_name": "Dev"}
    assert build_job(card, detail).salary_text is None


def test_sapoemprego_handles_an_anonymous_employer():
    card = {"link": "https://emprego.sapo.pt/offers/x?id=1", "offer_name": "Dev", "company_name": None,
            "anonymous": True}
    assert build_job(card).company is None
