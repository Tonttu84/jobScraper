from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from jobscraper.filters.language import find_language_requirements
from jobscraper.http import SourceHTTPError
from jobscraper.sources.justjoin import (
    JustJoin,
    _names,
    hydrate,
    languages_line,
    parse_offer,
    salary_text,
)

# The detail route must be listed first: its key is a substring-of-URL match and the list URL
# (".../offers?...") does not contain the trailing slash, so the two never collide.
ROUTES = {
    ("GET", "/candidate-api/offers/"): "justjoin_detail.json",
    ("GET", "/candidate-api/offers"): "justjoin.json",
}


def test_justjoin_parses_fixture(make_ctx):
    ctx = make_ctx(ROUTES, options={"max_pages": 1})
    jobs = list(JustJoin().fetch(ctx))
    assert len(jobs) == 2  # the broken offer is skipped, not raised

    j = jobs[0]
    assert j.source == "justjoin"
    assert j.source_id == "ba853a14-f777-4071-9dd3-2c54960257ee"
    assert j.title == "Junior AI Engineer"
    assert j.company == "Craftware"
    assert j.url == "https://justjoin.it/job-offer/craftware-junior-ai-engineer-warszawa-ai"
    assert j.country == "PL" and j.city == "Warszawa"
    assert j.location_raw == "Warszawa, PL"
    assert j.remote == "remote"
    assert j.seniority_raw == "junior"
    assert j.employment_type == "b2b"
    # Only the original-currency row is reported, not the four converted duplicates.
    assert j.salary_text == "13,440 - 25,200 PLN (b2b)"
    assert j.tags == ["Python", "AI", "Cloud", "Agentic Frameworks", "ML frameworks"]
    assert j.posted_at == datetime(2026, 9, 6, 17, 0, 7, 238731, tzinfo=UTC)
    assert "Craftware is a technology company" in j.description and "<" not in j.description

    # A non-Polish city still resolves; no salary is published for this one.
    assert jobs[1].city == "Lisbon" and jobs[1].country == "PT"
    assert jobs[1].salary_text is None
    assert jobs[1].employment_type is None  # justjoin's "any" is not an employment type


def test_justjoin_list_request_shape(make_ctx):
    ctx = make_ctx(ROUTES, options={"experience_levels": ["junior", "mid"], "max_pages": 1,
                                    "fetch_details": False})
    list(JustJoin().fetch(ctx))
    url = str(ctx.http.calls[0].url)
    assert url.startswith("https://justjoin.it/api/candidate-api/offers?")
    # Repeated params — the comma-joined form returns zero results on the live API.
    assert "experienceLevels=junior" in url and "experienceLevels=mid" in url
    assert "from=0" in url and "itemsCount=" in url
    assert "isRemote" not in url
    assert ctx.http.calls[0].headers["accept"] == "application/json"
    assert all("/candidate-api/offers/" not in str(c.url) for c in ctx.http.calls)


def test_justjoin_remote_only_uses_is_remote(make_ctx):
    ctx = make_ctx(ROUTES, options={"workplace_types": ["remote"], "max_pages": 1, "fetch_details": False})
    jobs = list(JustJoin().fetch(ctx))
    assert "isRemote=true" in str(ctx.http.calls[0].url)
    assert len(jobs) == 2


def test_justjoin_workplace_types_filter_client_side(make_ctx):
    # workplaceTypes is not a supported query param, so a non-remote selection is filtered here.
    ctx = make_ctx(ROUTES, options={"workplace_types": ["hybrid"], "max_pages": 1, "fetch_details": False})
    assert list(JustJoin().fetch(ctx)) == []
    assert "isRemote" not in str(ctx.http.calls[0].url)


def _page(cursor, total, records):
    return {"data": records,
            "meta": {"from": 0, "totalItems": total,
                     "prev": {"cursor": None, "itemsCount": len(records)},
                     "next": {"cursor": cursor, "itemsCount": len(records)}}}


def _rec(guid, slug, city, workplace="office"):
    return {"guid": guid, "slug": slug, "title": "Junior Dev", "companyName": "A", "city": city,
            "workplaceType": workplace, "experienceLevel": "junior",
            "publishedAt": "2026-09-05T00:00:00.0000000Z"}


def test_justjoin_follows_cursor_then_stops_at_total(make_ctx):
    pages = [
        _page(1, 2, [_rec("a", "a-slug", "Kraków")]),
        # last page: next.cursor == totalItems, so there is nothing after it
        _page(2, 2, [_rec("b", "b-slug", "Gdańsk", "remote")]),
    ]
    calls: list[httpx.Request] = []

    def route(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json=pages[min(len(calls) - 1, len(pages) - 1)])

    ctx = make_ctx({("GET", "/candidate-api/offers"): route},
                   options={"max_pages": 5, "fetch_details": False})
    jobs = list(JustJoin().fetch(ctx))
    assert [j.source_id for j in jobs] == ["a", "b"]
    assert len(calls) == 2 and "from=1" in str(calls[1].url)
    assert jobs[0].remote == "onsite" and jobs[0].country == "PL"
    assert jobs[1].remote == "remote"


def test_justjoin_stops_on_empty_page(make_ctx):
    pages = [_page(1, 99, [_rec("a", "a-slug", "Poznań")]), _page(None, 99, [])]
    calls: list[httpx.Request] = []

    def route(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json=pages[min(len(calls) - 1, len(pages) - 1)])

    ctx = make_ctx({("GET", "/candidate-api/offers"): route},
                   options={"max_pages": 5, "fetch_details": False})
    assert len(list(JustJoin().fetch(ctx))) == 1
    assert len(calls) == 2


def test_justjoin_respects_limit(make_ctx):
    ctx = make_ctx(ROUTES, options={"max_pages": 3, "fetch_details": False}, limit=1)
    assert len(list(JustJoin().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1


def test_justjoin_max_details_caps_detail_requests(make_ctx):
    ctx = make_ctx(ROUTES, options={"max_pages": 1, "max_details": 1})
    jobs = list(JustJoin().fetch(ctx))
    assert sum("/candidate-api/offers/" in str(c.url) for c in ctx.http.calls) == 1
    # The un-hydrated job keeps the language line built from the list record, but no body.
    assert "Craftware is a technology company" in jobs[0].description
    assert jobs[1].description == "Required languages: English (B1), Spanish (B1)."


def test_justjoin_detail_failure_keeps_the_job(make_ctx):
    routes = {
        ("GET", "/candidate-api/offers/"): lambda req: httpx.Response(404, text="Entity not found"),
        ("GET", "/candidate-api/offers"): "justjoin.json",
    }
    ctx = make_ctx(routes, options={"max_pages": 1})
    jobs = list(JustJoin().fetch(ctx))
    assert len(jobs) == 2 and jobs[0].description is None


def test_justjoin_detail_hydrates_country_and_skills(make_ctx):
    ctx = make_ctx(ROUTES, options={"max_pages": 1})
    job = next(iter(JustJoin().fetch(ctx)))
    assert job.raw["detail"]["slug"] == "craftware-junior-ai-engineer-warszawa-ai"
    assert job.country == "PL"
    assert "Python" in job.tags


def test_justjoin_dead_endpoint_raises(make_ctx):
    routes = {("GET", "/candidate-api/offers"): lambda req: httpx.Response(503, text="<html>nginx</html>")}
    ctx = make_ctx(routes, options={"max_pages": 1, "fetch_details": False})
    with pytest.raises(SourceHTTPError):
        list(JustJoin().fetch(ctx))


def test_justjoin_non_object_payload_raises(make_ctx):
    ctx = make_ctx({("GET", "/candidate-api/offers"): []}, options={"max_pages": 1, "fetch_details": False})
    with pytest.raises(SourceHTTPError):
        list(JustJoin().fetch(ctx))


def test_justjoin_parse_offer_skips_incomplete_records():
    assert parse_offer("nope") is None
    assert parse_offer({"slug": "x"}) is None
    assert parse_offer({"title": "x"}) is None


def test_justjoin_salary_text_tolerates_junk():
    assert salary_text(None) is None
    assert salary_text("broken") is None
    assert salary_text([{"from": None, "to": None}, "x"]) is None
    assert salary_text([{"from": 2500, "currency": "eur", "type": "permanent",
                         "currencySource": "original"}]) == "2,500 EUR (permanent)"
    # equal bounds collapse; "any" is dropped as a contract type
    assert salary_text([{"from": 9000, "to": 9000, "currency": "pln", "type": "any",
                         "currencySource": "original"}]) == "9,000 PLN"
    # nothing marked original (old payload shape) → fall back to every row
    assert salary_text([{"from": 100, "currency": "usd"}]) == "100 USD"
    # figures the API wrote as text, and a currency field that is not a string
    assert salary_text([{"from": "13 440", "to": "25 200", "currency": 7}]) == "13 440 - 25 200"


def test_justjoin_skill_lists_come_in_several_shapes():
    assert _names(["Python", 7, {"label": "unnamed"}, {"value": " SQL "}]) == ["Python", "SQL"]
    assert _names("Python") == []  # a bare string is not a list of skills


def test_justjoin_hydrate_survives_a_detail_that_is_not_an_object():
    job = parse_offer(_rec("g1", "s1", "Kraków"))

    class _Http:
        def get_json(self, url, **kwargs):
            return ["unexpected", "array"]

    hydrate(SimpleNamespace(http=_Http()), job, "s1")
    assert job.description is None and "detail" not in job.raw


def test_justjoin_hydrate_reads_an_experience_level_object():
    job = parse_offer(_rec("g1", "s1", "Kraków"))

    class _Http:
        def get_json(self, url, **kwargs):
            return {"slug": "s1", "body": "<p>Body</p>", "experienceLevel": {"value": "mid"}}

    hydrate(SimpleNamespace(http=_Http()), job, "s1")
    assert job.seniority_raw == "mid"
    assert job.description.strip() == "Body"


def test_justjoin_without_experience_levels_sends_none(make_ctx):
    ctx = make_ctx(ROUTES, options={"experience_levels": [], "max_pages": 1, "fetch_details": False})
    list(JustJoin().fetch(ctx))
    assert "experienceLevels" not in str(ctx.http.calls[0].url)


def test_justjoin_stops_when_the_cursor_stops_advancing(make_ctx):
    """A page whose ``next.cursor`` does not move is the last page, whatever totalItems says."""
    ctx = make_ctx(
        {("GET", "/candidate-api/offers"): _page(0, 999, [_rec("a", "a-slug", "Kraków")])},
        options={"max_pages": 5, "fetch_details": False},
    )
    assert len(list(JustJoin().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1


def test_justjoin_dedupes_across_pages_and_stops_at_max_pages(make_ctx):
    """Advancing cursor, repeated records: the page budget ends the loop, the guid dedupes."""
    calls: list[httpx.Request] = []

    def route(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json=_page(len(calls), 999, [_rec("a", "a-slug", "Kraków")]))

    ctx = make_ctx({("GET", "/candidate-api/offers"): route},
                   options={"max_pages": 2, "fetch_details": False})
    assert [j.source_id for j in JustJoin().fetch(ctx)] == ["a"]
    assert len(calls) == 2


def _lang_rec(languages):
    rec = _rec("lang", "lang-slug", "Warszawa", "remote")
    if languages is not None:
        rec["languages"] = languages
    return rec


def test_justjoin_prepends_required_languages_line():
    job = parse_offer(_lang_rec([{"code": "pl", "level": "C1"}, {"code": "en", "level": "B2"}]))
    assert job.description.startswith("Required languages: Polish (C1), English (B2).")
    assert job.raw["languages"][0] == {"code": "pl", "level": "C1"}


def test_justjoin_language_line_absent_without_the_field():
    assert parse_offer(_lang_rec(None)).description is None
    assert parse_offer(_lang_rec([])).description is None


def test_justjoin_language_line_is_seen_by_the_rule_filter():
    job = parse_offer(_lang_rec([{"code": "pl", "level": "C1"}, {"code": "en", "level": "C1"}]))
    found = find_language_requirements(job.description)
    assert "pl" in found.required and "en" in found.required


def test_justjoin_language_line_tolerates_junk_and_unknown_codes():
    assert languages_line(None) is None
    assert languages_line("broken") is None
    assert languages_line([{"level": "C1"}, "x", {"code": "  "}]) is None
    assert languages_line([{"code": "zz"}]) == "Required languages: ZZ."
    assert languages_line([{"code": "de"}]) == "Required languages: German."


def test_justjoin_hydrate_keeps_the_language_line_in_front_of_the_body(make_ctx):
    # The list record carries languages; the detail fixture's own field is empty, so the list
    # value survives hydration and stays ahead of the posting body.
    ctx = make_ctx(ROUTES, options={"max_pages": 1})
    job = list(JustJoin().fetch(ctx))[1]
    assert job.description.startswith("Required languages: English (B1), Spanish (B1).\n\n")
    assert "Craftware is a technology company" in job.description


def test_justjoin_detail_languages_win_over_the_list_record():
    job = parse_offer(_lang_rec([{"code": "en", "level": "B1"}]))
    detail = {"slug": "lang-slug", "body": "<p>Body text</p>",
              "languages": [{"code": "pl", "level": "C1"}]}

    class _Http:
        def get_json(self, url, **kwargs):
            return detail

    hydrate(SimpleNamespace(http=_Http()), job, "lang-slug")
    assert job.description.startswith("Required languages: Polish (C1).\n\n")
    assert "Body text" in job.description
