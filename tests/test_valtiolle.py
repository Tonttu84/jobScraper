"""valtiolle.fi, and with it the shared Grade Solutions parser both Finnish boards use."""

from datetime import UTC, datetime

import httpx
import pytest

from jobscraper.sources._grade import (
    job_posting,
    page_count,
    parse_cards,
    publication_datetime,
    remote_facet,
    rendered_description,
)
from jobscraper.sources.valtiolle import Valtiolle

SEARCH = "valtiolle.fi/fi/tyopaikat/?desc="
PAGE2 = "valtiolle.fi/fi/tyopaikat/sivu2/"

# Every detail request is answered with the one saved posting page (a different posting from any
# of the cards, deliberately): the card and the detail page are independent halves of the
# mapping, so a job whose title comes from the card and whose salary comes from the detail page
# shows which half produced what.
ROUTES = {
    PAGE2: "valtiolle_search_p2.html",           # must come first: page 2 also carries ?desc=
    SEARCH: "valtiolle_search.html",
    "valtiolle.fi/fi/tyopaikat/": "valtiolle_detail.html",
}
LISTING_ONLY = {"queries": ["ohjelmisto"], "max_pages": 1, "fetch_details": False}


def urls(ctx) -> list[str]:
    return [str(c.url) for c in ctx.http.calls]


def detail_urls(ctx) -> list[str]:
    return [u for u in urls(ctx) if "?desc=" not in u]


# --------------------------------------------------------------------------- listing


def test_valtiolle_parses_the_listing(make_ctx):
    ctx = make_ctx(ROUTES, options=LISTING_ONLY)
    jobs = list(Valtiolle().fetch(ctx))

    assert [j.source_id for j in jobs] == ["303404", "303390", "303365"]
    j = jobs[0]
    assert j.source == "valtiolle"
    assert j.title == "Harjoittelija, kestävä matkailu"
    assert j.company == "Luonnonvarakeskus"
    assert j.url == "https://valtiolle.fi/fi/tyopaikat/harjoittelija-kestava-matkailu-24878/"
    assert j.country == "FI"
    assert j.remote == "unknown"
    # "11.9.2026" + "11:00" on the card is Helsinki wall-clock time: 08:00 UTC in September.
    assert j.posted_at.astimezone(UTC) == datetime(2026, 9, 11, 8, 0, tzinfo=UTC)
    assert j.raw["card"]["ext-id"] == "24878"


def test_valtiolle_asks_for_the_query_and_then_the_numbered_page(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["ohjelmisto"], "max_pages": 5,
                                    "fetch_details": False})
    list(Valtiolle().fetch(ctx))

    assert urls(ctx) == [
        "https://valtiolle.fi/fi/tyopaikat/?desc=ohjelmisto",
        "https://valtiolle.fi/fi/tyopaikat/sivu2/?desc=ohjelmisto",
    ]


def test_valtiolle_stops_when_the_pagination_says_it_is_the_last_page(make_ctx):
    """``<ip-pagination total>`` counts *pages*, so page 2 of 2 is the end: page 3 is never
    asked for even though ``max_pages`` would allow eight more."""
    ctx = make_ctx(ROUTES, options={"queries": ["ohjelmisto"], "max_pages": 9,
                                    "fetch_details": False})
    jobs = list(Valtiolle().fetch(ctx))

    assert len(urls(ctx)) == 2
    assert [j.source_id for j in jobs] == ["303404", "303390", "303365", "303349"]


def test_valtiolle_skips_a_malformed_card_without_killing_the_run(make_ctx):
    """Page 2 of the fixture carries a card with neither title nor url next to a good one."""
    ctx = make_ctx({SEARCH: "valtiolle_search_p2.html"}, options=LISTING_ONLY)
    jobs = list(Valtiolle().fetch(ctx))

    assert [j.title for j in jobs] == ["Laboratorioanalyytikko"]


def test_valtiolle_stops_at_the_probe_limit(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["ohjelmisto"], "max_pages": 9,
                                    "fetch_details": False}, limit=2)
    jobs = list(Valtiolle().fetch(ctx))

    assert len(jobs) == 2
    assert len(urls(ctx)) == 1  # the second page is never fetched


def test_valtiolle_does_not_yield_the_same_posting_twice(make_ctx):
    """Two overlapping queries must not produce two copies of one posting, nor two detail hits."""
    ctx = make_ctx(ROUTES, options={"queries": ["ohjelmisto", "kehittäjä"], "max_pages": 1})
    jobs = list(Valtiolle().fetch(ctx))

    assert [j.source_id for j in jobs] == ["303404", "303390", "303365"]
    assert len(detail_urls(ctx)) == 3


# --------------------------------------------------------------------------- detail page


def test_valtiolle_detail_adds_what_the_json_ld_leaves_out(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["ohjelmisto"], "max_pages": 1}, limit=1)
    job = next(iter(Valtiolle().fetch(ctx)))

    assert job.location_raw == "Joensuu, Mikkeli, Pori, Helsinki, Hämeenlinna"
    assert job.city == "Joensuu"
    assert job.country == "FI"
    assert job.salary_text == "Aloittavan henkilön palkka on 3 270,19 euroa kuukaudessa."
    assert job.employment_type == (
        "Kaikki työpaikat, Vakinainen, Virkasuhde, Virastotyöaika, Toimeenpaneva taso"
    )
    # datePosted carries the offset, so it wins over the card's wall-clock date.
    assert job.posted_at.astimezone(UTC) == datetime(2026, 9, 11, 10, 35, tzinfo=UTC)
    assert job.raw["jsonld"]["validThrough"] == "2026-09-28T14:00:00+03:00"

    # The rendered page carries sections the JSON-LD `description` does not — the requirements
    # and the offer. That is the whole reason this adapter fetches the detail page at all.
    assert "Haemme tilinpäätöstaitoista pääkirjanpitäjää" in job.description   # the lead
    assert "Tehtävässä vastaat kirjanpidon" in job.description                 # Työtehtävän kuvaus
    assert "Hakijalta odotamme" in job.description                             # section heading
    assert "Kokemusta kirjanpidosta ja tilinpäätöksistä" in job.description    # the requirements
    assert "Joustavan hybridityön mallin" in job.description                   # Tarjoamme sinulle
    assert "Kokemusta kirjanpidosta" not in job.raw["jsonld"]["description"]


def test_valtiolle_detail_leaves_out_the_contact_block_and_the_metadata_repeat(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["ohjelmisto"], "max_pages": 1}, limit=1)
    job = next(iter(Valtiolle().fetch(ctx)))

    assert "Yhteystietomme" not in job.description
    assert "etunimi.sukunimi@palkeet.fi" not in job.description   # recruiter contact details
    assert "0295 562 229" not in job.description
    assert "Rekrytoinnin ID" not in job.description               # the mobile metadata repeat


def test_valtiolle_reads_the_structured_remote_field(make_ctx):
    """The fixture's posting says "Mahdollisuus työskennellä etänä" in the "Etätyö" facet."""
    ctx = make_ctx(ROUTES, options={"queries": ["ohjelmisto"], "max_pages": 1}, limit=1)
    job = next(iter(Valtiolle().fetch(ctx)))

    assert job.remote == "hybrid"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Mahdollisuus työskennellä etänä", "hybrid"),          # valtiolle
        ("Ei mahdollisuutta työskennellä etänä", "onsite"),     # valtiolle — the trap
        ("Osittainen etätyömahdollisuus", "hybrid"),            # kuntarekry
        ("Sovitaan erikseen", None),                            # a wording we have not seen
    ],
)
def test_remote_facet_reads_the_field_instead_of_guessing(value, expected):
    aside = f'<ip-aside-item title="Etätyö">{value}</ip-aside-item>'
    item = f"<ul><li><strong>Etätyö:</strong> {value}</li></ul>"

    assert remote_facet(aside) == expected
    assert remote_facet(item) == expected
    assert remote_facet("<html><body>no such field</body></html>") is None


def test_valtiolle_guesses_remote_when_the_field_is_missing(make_ctx):
    """Without the facet the body decides, as on every other board."""
    remote_page = (
        '<html><body><p class="lead">Teemme työtä etätyönä koko Suomessa, tule mukaan '
        "rakentamaan julkishallinnon palveluita kanssamme.</p></body></html>"
    )
    routes = dict(ROUTES)
    routes["valtiolle.fi/fi/tyopaikat/"] = lambda req: httpx.Response(200, text=remote_page)
    ctx = make_ctx(routes, options={"queries": ["ohjelmisto"], "max_pages": 1}, limit=1)
    job = next(iter(Valtiolle().fetch(ctx)))

    assert job.remote == "remote"


def test_valtiolle_leaves_remote_unknown_when_nothing_says_anything(make_ctx):
    """Listing-only: the card carries neither a location nor a remote flag."""
    ctx = make_ctx(ROUTES, options=LISTING_ONLY)
    jobs = list(Valtiolle().fetch(ctx))

    assert {j.remote for j in jobs} == {"unknown"}


def test_valtiolle_falls_back_to_the_json_ld_description(make_ctx):
    """A detail page with the JSON-LD but no rendered sections still yields a description."""
    minimal = (
        '<html><body><script type="application/ld+json">'
        '{"@context":"http://schema.org","@type":"JobPosting","title":"X",'
        '"hiringOrganization":"Y","description":"Kuvaus ilman osioita, mutta riittavan pitka '
        'jotta se kelpaa tyopaikkailmoituksen kuvaukseksi."}'
        "</script></body></html>"
    )
    routes = dict(ROUTES)
    routes["valtiolle.fi/fi/tyopaikat/"] = lambda req: httpx.Response(200, text=minimal)
    ctx = make_ctx(routes, options={"queries": ["ohjelmisto"], "max_pages": 1}, limit=1)
    job = next(iter(Valtiolle().fetch(ctx)))

    assert job.description.startswith("Kuvaus ilman osioita")
    assert job.location_raw is None and job.city is None
    assert job.salary_text is None


def test_valtiolle_a_detail_page_without_json_ld_is_not_fatal(make_ctx):
    routes = dict(ROUTES)
    routes["valtiolle.fi/fi/tyopaikat/"] = lambda req: httpx.Response(200, text="<html></html>")
    ctx = make_ctx(routes, options={"queries": ["ohjelmisto"], "max_pages": 1}, limit=1)
    job = next(iter(Valtiolle().fetch(ctx)))

    assert job.description is None
    assert job.title == "Harjoittelija, kestävä matkailu"   # the card still decides


def test_valtiolle_a_broken_detail_page_still_yields_the_listing_job(make_ctx):
    routes = dict(ROUTES)
    routes["valtiolle.fi/fi/tyopaikat/"] = lambda req: httpx.Response(500, text="boom")
    ctx = make_ctx(routes, options={"queries": ["ohjelmisto"], "max_pages": 1}, limit=1)
    jobs = list(Valtiolle().fetch(ctx))

    assert [j.title for j in jobs] == ["Harjoittelija, kestävä matkailu"]
    assert jobs[0].description is None


def test_valtiolle_caps_how_many_detail_pages_it_fetches(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": ["ohjelmisto"], "max_pages": 1, "max_details": 1})
    jobs = list(Valtiolle().fetch(ctx))

    assert len(jobs) == 3
    assert [j.description is not None for j in jobs] == [True, False, False]
    assert len(detail_urls(ctx)) == 1


def test_valtiolle_a_broken_listing_page_raises(make_ctx):
    """An endpoint failure is not a per-record failure: ``probe`` must see the source is down."""
    from jobscraper.http import SourceHTTPError

    ctx = make_ctx({SEARCH: lambda req: httpx.Response(503, text="down")}, options=LISTING_ONLY)
    with pytest.raises(SourceHTTPError):
        list(Valtiolle().fetch(ctx))


# --------------------------------------------------------------------------- parser units


def test_parse_cards_takes_every_card_when_the_grid_list_is_gone():
    """The markup is a vendor's, not a contract: a card outside a ``<job-list>`` still counts."""
    html = '<html><body><job-card title="T" url="/fi/tyopaikat/t-1/" job-id="1"></job-card></body></html>'
    assert parse_cards(html) == [{"title": "T", "url": "/fi/tyopaikat/t-1/", "job-id": "1"}]


def test_parse_cards_ignores_the_paid_promotions():
    html = (
        '<job-list variant="grid"><job-card title="A" url="/a/" job-id="1"></job-card></job-list>'
        '<job-list variant="col-2">'
        '<job-card is-promoted="true" title="B" url="/b/" job-id="2"></job-card></job-list>'
    )
    assert [c["title"] for c in parse_cards(html)] == ["A"]


@pytest.mark.parametrize(
    "html,expected",
    [
        ('<ip-pagination current="1" total="3"></ip-pagination>', 3),
        ('<ip-pagination current="1" total=""></ip-pagination>', None),   # emptied by the site
        ("<ip-pagination></ip-pagination>", None),                        # attribute gone
        ("<html><body>no pagination at all</body></html>", None),
    ],
)
def test_page_count_reads_the_number_of_pages_or_gives_up(html, expected):
    assert page_count(html) == expected


def test_a_listing_page_with_no_cards_ends_the_walk(make_ctx):
    """A page past the end answers 200 with an empty grid, which is the other stop condition."""
    empty = '<html><body><job-list variant="grid"></job-list></body></html>'
    ctx = make_ctx({SEARCH: lambda req: httpx.Response(200, text=empty)}, options=LISTING_ONLY)

    assert list(Valtiolle().fetch(ctx)) == []


def test_a_single_query_may_be_written_as_a_bare_string(make_ctx):
    ctx = make_ctx(ROUTES, options={"queries": "ohjelmisto", "max_pages": 1,
                                    "fetch_details": False})
    jobs = list(Valtiolle().fetch(ctx))

    assert len(jobs) == 3
    assert urls(ctx) == ["https://valtiolle.fi/fi/tyopaikat/?desc=ohjelmisto"]


def test_job_posting_walks_past_the_breadcrumbs_and_past_broken_json():
    """The pages carry a ``BreadcrumbList`` block too, and sometimes a block that will not parse."""
    html = (
        '<script type="application/ld+json">{"@type":"BreadcrumbList","name":"nav"}</script>'
        '<script type="application/ld+json">{oops</script>'
        '<script type="application/ld+json">{"@type":"JobPosting","title":"Kehittäjä"}</script>'
    )
    assert job_posting(html) == {"@type": "JobPosting", "title": "Kehittäjä"}
    assert job_posting("<html><body>nothing here</body></html>") == {}


def test_rendered_description_skips_an_empty_section():
    html = (
        '<p class="lead">Johdanto, joka kertoo lyhyesti mistä tässä tehtävässä on kysymys.</p>'
        '<ip-details headerIcon="star-empty" title="Hakijalta odotamme"><p></p></ip-details>'
        '<ip-details headerIcon="briefcase"><p>Osio ilman otsikkoa.</p></ip-details>'
    )
    body = rendered_description(html)

    assert "Hakijalta odotamme" not in body
    assert body.endswith("Osio ilman otsikkoa.")


@pytest.mark.parametrize(
    "date,time,expected",
    [
        ("11.9.2026", "11:00", datetime(2026, 9, 11, 8, 0, tzinfo=UTC)),    # EEST, +03:00
        ("2.1.2026", "09:30", datetime(2026, 1, 2, 7, 30, tzinfo=UTC)),     # EET, +02:00
        ("11.9.2026", None, datetime(2026, 9, 10, 21, 0, tzinfo=UTC)),      # midnight, +03:00
        ("11.9.2026", "kello kaksi", datetime(2026, 9, 10, 21, 0, tzinfo=UTC)),
    ],
)
def test_publication_datetime_reads_finnish_wall_clock_time(date, time, expected):
    assert publication_datetime(date, time).astimezone(UTC) == expected


@pytest.mark.parametrize("date,time", [(None, "11:00"), ("", None), ("eilen", "11:00")])
def test_publication_datetime_gives_up_quietly(date, time):
    assert publication_datetime(date, time) is None


def test_valtiolle_is_registered_and_enabled(settings):
    source = Valtiolle()
    assert source.name == "valtiolle"
    assert "valtiolle.fi" in source.description
    assert settings.sources["valtiolle"].enabled
