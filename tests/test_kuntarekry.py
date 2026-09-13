"""kuntarekry.fi — the municipal half of the shared Grade Solutions platform."""

from datetime import UTC, datetime

from jobscraper.sources.kuntarekry import Kuntarekry

SEARCH = "kuntarekry.fi/fi/tyopaikat/?desc="
# Every detail request is answered with the one saved posting page, whichever card asked for it:
# the card and the detail page are independent halves of the mapping, and a test that reads a
# Vaasa card's title next to an Oulu detail page's location is showing exactly that.
ROUTES = {
    SEARCH: "kuntarekry_search.html",
    "kuntarekry.fi/fi/tyopaikat/": "kuntarekry_detail.html",
}
LISTING_ONLY = {"queries": ["ohjelmisto"], "max_pages": 1, "fetch_details": False}


def urls(ctx) -> list[str]:
    return [str(c.url) for c in ctx.http.calls]


def test_kuntarekry_parses_the_listing(make_ctx):
    ctx = make_ctx(ROUTES, options=LISTING_ONLY)
    jobs = list(Kuntarekry().fetch(ctx))

    j = jobs[0]
    assert j.source == "kuntarekry"
    assert j.source_id == "1912865"
    assert j.title == "Suunnitteluinsinööri"
    assert j.company == "Vaasan kaupunki"
    assert j.url == "https://kuntarekry.fi/fi/tyopaikat/suunnitteluinsinoori-vsa905-04-19-26/"
    assert j.country == "FI"
    assert j.remote == "unknown"
    assert j.posted_at.astimezone(UTC) == datetime(2026, 9, 11, 5, 0, tzinfo=UTC)
    assert j.raw["card"]["ext-id"] == "VSA905-04-19-26"


def test_kuntarekry_ignores_the_paid_promotions_below_the_pagination(make_ctx):
    """The results grid holds 24 cards; the "Mainostetut työpaikat" list underneath repeats the
    same paid slots on every page and is marked ``is-promoted``."""
    ctx = make_ctx(ROUTES, options=LISTING_ONLY)
    jobs = list(Kuntarekry().fetch(ctx))

    assert [j.title for j in jobs] == ["Suunnitteluinsinööri", "Sovellusarkkitehti, Esko Systems Oy"]


def test_kuntarekry_stops_on_a_single_page_listing(make_ctx):
    """``total="1"`` in the pagination: no ``/sivu2/`` request, whatever ``max_pages`` says."""
    ctx = make_ctx(ROUTES, options={"queries": ["ohjelmisto"], "max_pages": 4,
                                    "fetch_details": False})
    list(Kuntarekry().fetch(ctx))

    assert urls(ctx) == ["https://kuntarekry.fi/fi/tyopaikat/?desc=ohjelmisto"]


def test_kuntarekry_detail_fills_in_location_salary_and_the_full_body(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["ohjelmisto"], "max_pages": 1}, limit=1)
    job = next(iter(Kuntarekry().fetch(ctx)))

    assert job.title == "Suunnitteluinsinööri"
    assert job.company == "Vaasan kaupunki"      # the card's employer, not the detail's unit
    assert job.url == "https://kuntarekry.fi/fi/tyopaikat/suunnitteluinsinoori-vsa905-04-19-26/"
    assert job.country == "FI"
    assert job.remote == "hybrid"       # the "Etätyö" facet says "Osittainen etätyömahdollisuus"
    assert job.posted_at.astimezone(UTC) == datetime(2026, 9, 10, 5, 0, tzinfo=UTC)
    assert job.location_raw == "Oulu"
    assert job.city == "Oulu"
    assert job.salary_text == "YTES"
    assert job.employment_type == "Kokoaikatyö, Työpaikka, Vakinainen"
    assert "Esko Systems on asiakkaidensa omistama inhouse-yhtiö" in job.description
    assert "Lisätietoja organisaatiosta" in job.description
    assert job.raw["jsonld"]["hiringOrganization"] == "Esko Systems Oy"


def test_kuntarekry_detail_leaves_out_the_contact_block(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["ohjelmisto"], "max_pages": 1}, limit=1)
    job = next(iter(Kuntarekry().fetch(ctx)))

    assert "Yhteystiedot" not in job.description
    assert "Työavain" not in job.description


def test_kuntarekry_is_registered_and_enabled(settings):
    source = Kuntarekry()
    assert source.name == "kuntarekry"
    assert "kuntarekry.fi" in source.description
    assert settings.sources["kuntarekry"].enabled
