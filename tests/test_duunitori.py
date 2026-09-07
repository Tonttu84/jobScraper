"""duunitori tests.

``tests/fixtures/duunitori.json`` is a real ``/api/v1/jobentries`` response captured on
2026-09-07 (search ``ohjelmistokehittäjä``), trimmed to three of its twenty results with
``descr`` cut to a few lines, plus one deliberately broken result. The envelope
(``count``/``next``/``previous``) and every record key are verbatim, so the tests below
pin the adapter to the shape the API really returns.
"""

import httpx
import pytest
from conftest import fixture_json, fixture_text

from jobscraper.http import SourceHTTPError
from jobscraper.sources.duunitori import Duunitori, parse_record, parse_search_html


def test_duunitori_parses_api_fixture(make_ctx):
    ctx = make_ctx({"jobentries": "duunitori.json"}, options={"queries": ["python"], "max_pages": 1})
    jobs = list(Duunitori().fetch(ctx))

    assert len(jobs) == 3  # the record with no slug/heading is skipped, not raised
    j = jobs[0]
    assert j.source == "duunitori"
    assert j.source_id == "senior-qt-c-developer-sdsuu-20549989"
    assert j.title == "Senior Qt / C++ Developer"
    assert j.company == "Pareto Software Oy"
    assert j.url == "https://duunitori.fi/tyopaikat/tyo/senior-qt-c-developer-sdsuu-20549989"
    assert j.country == "FI" and j.city == "Tampere"
    assert j.location_raw == "Tampere"
    assert j.remote == "hybrid"  # "Remote status: Hybrid" footer in descr
    assert j.posted_at is not None
    assert j.posted_at.isoformat() == "2026-09-07T10:00:07.803729+03:00"
    assert "Pareto is a growing software company" in j.description
    assert "<" not in j.description  # descr is plain text; strip_html leaves it readable
    assert j.salary_text is None  # the endpoint carries no salary field


def test_duunitori_country_wide_posting_has_no_city(make_ctx):
    """``municipality_name`` is sometimes the country ("Finland"), which is not a city."""
    ctx = make_ctx({"jobentries": "duunitori.json"}, options={"max_pages": 1})
    jobs = list(Duunitori().fetch(ctx))

    finland = jobs[1]
    assert finland.title == "OpenShift-kehittäjä, YOSO"
    assert finland.company == "Biisoni"
    assert finland.location_raw == "Finland"
    assert finland.country == "FI"
    assert finland.city is None
    assert finland.remote == "unknown"  # no remote marker anywhere in this record


def test_duunitori_plain_city_posting(make_ctx):
    ctx = make_ctx({"jobentries": "duunitori.json"}, options={"max_pages": 1})
    job = list(Duunitori().fetch(ctx))[2]

    assert job.city == "Espoo" and job.country == "FI"
    assert job.company == "Remedy Entertainment Oyj"
    assert job.remote == "unknown"
    assert job.raw["export_image_url"].startswith("https://duunitori.imgix.net/")


@pytest.mark.parametrize(
    ("status", "expected"),
    [("Hybrid", "hybrid"), ("Fully Remote", "remote"), ("Ei tiedossa", "unknown")],
)
def test_remote_status_footer_wins_over_guessing(status, expected):
    rec = dict(fixture_json("duunitori.json")["results"][0])
    rec["descr"] = f"Tehtävän kuvaus.\nLocations: Tampere, Finland\nRemote status: {status}"
    job = parse_record(rec)
    assert job is not None
    assert job.remote == expected  # unrecognized values fall back to guess_remote


def test_duunitori_dedupes_across_queries(make_ctx):
    ctx = make_ctx(
        {"jobentries": "duunitori.json"},
        options={"queries": ["python", "django"], "max_pages": 1},
    )
    jobs = list(Duunitori().fetch(ctx))
    assert len(jobs) == 3  # same slugs returned twice, emitted once
    assert len(ctx.http.calls) == 2


def test_duunitori_follows_next_up_to_max_pages(make_ctx):
    ctx = make_ctx({"jobentries": "duunitori.json"}, options={"queries": ["x"], "max_pages": 2})
    jobs = list(Duunitori().fetch(ctx))
    assert len(jobs) == 3  # page 2 repeats the same slugs
    assert len(ctx.http.calls) == 2  # the envelope's `next` is not null, so page 2 is fetched


def test_duunitori_stops_when_next_is_null(make_ctx):
    payload = fixture_json("duunitori.json") | {"next": None}
    ctx = make_ctx({"jobentries": payload}, options={"queries": ["x"], "max_pages": 5})
    assert len(list(Duunitori().fetch(ctx))) == 3
    assert len(ctx.http.calls) == 1


def test_duunitori_stops_on_empty_results(make_ctx):
    payload = {"count": 0, "next": None, "previous": None, "results": []}
    ctx = make_ctx({"jobentries": payload}, options={"queries": ["x"], "max_pages": 5})
    assert list(Duunitori().fetch(ctx)) == []
    assert len(ctx.http.calls) == 1


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
    assert jobs[1].remote == "remote"  # no footer here, so guess_remote decides
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
