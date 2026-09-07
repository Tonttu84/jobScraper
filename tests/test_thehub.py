"""thehub.io adapter tests.

Fixtures are trimmed copies of real responses. The list envelope carries no dates, no
country code and no description, so every job has to be completed from ``/api/jobs/{id}``.
"""

from datetime import UTC, datetime

import httpx
from conftest import fixture_json

from jobscraper.sources.thehub import TheHub

LIST = "api/v2/jobs"
DETAIL = "api/jobs/"
VERDA = "6a96164ad9d4c7b3f6617c5a"  # Helsinki, onsite, detail fixtured
ICEYE = "6a5acbbbfb0d306cbef9712d"  # Espoo, onsite, detail fixtured
NOLOC = "6a96164cd9d4c7b3f6617cb7"  # isRemote, list doc has no `location` at all


def detail_doc(name: str = "thehub_detail.json", drop: tuple[str, ...] = (), **changes) -> dict:
    doc = fixture_json(name)["doc"]
    for key in drop:
        doc.pop(key, None)
    doc.update(changes)
    return {"doc": doc}


def routes(**detail_overrides) -> dict:
    """List fixture + one detail route per id; tests override single ids by name."""
    routed = {
        LIST: "thehub.json",
        f"{DETAIL}{VERDA}": "thehub_detail.json",
        f"{DETAIL}{ICEYE}": "thehub_detail_iceye.json",
        f"{DETAIL}{NOLOC}": lambda _req: httpx.Response(404, text="not found"),
    }
    for job_id, value in detail_overrides.items():
        routed[f"{DETAIL}{job_id}"] = value
    return routed


def fetch(make_ctx, routed=None, options=None, limit=None):
    ctx = make_ctx(
        routed or routes(),
        options={"countries": ["FI"], "max_pages": 1, **(options or {})},
        limit=limit,
    )
    return list(TheHub().fetch(ctx)), ctx


def test_thehub_parses_the_real_list_and_detail(make_ctx):
    jobs, _ctx = fetch(make_ctx)

    # 4 docs in, 3 out: the doc without an id/title is skipped.
    assert [j.source_id for j in jobs] == [VERDA, ICEYE, NOLOC]
    j = jobs[0]
    assert j.source == "thehub"
    assert j.url == f"https://thehub.io/jobs/{VERDA}"
    assert j.title == "AI Developer Advocate"
    assert j.company == "Verda"
    assert j.country == "FI"
    assert j.city == "Helsinki"
    assert j.location_raw == "Helsinki, Finland"
    assert j.remote == "onsite"
    assert j.salary_text == "competitive"
    # description exists only on the detail document
    assert "vertically-integrated European cloud" in j.description
    assert "<p>" not in j.description
    # publishedAt, also detail-only: the list envelope has no dates whatsoever
    assert j.posted_at == datetime(2026, 9, 1, 7, 13, 15, 36000, tzinfo=UTC)
    assert j.raw["apply_url"] == "https://jobs.verda.com/jobs/7903387"
    assert j.raw["detail"]["absoluteJobUrl"] == f"https://thehub.io/jobs/{VERDA}"


def test_thehub_completes_every_job_from_the_detail_endpoint(make_ctx):
    jobs, ctx = fetch(make_ctx)

    detail_calls = [c for c in ctx.http.calls if DETAIL in str(c.url)]
    assert [str(c.url).rsplit("/", 1)[-1] for c in detail_calls] == [VERDA, ICEYE, NOLOC]
    assert jobs[1].posted_at == datetime(2026, 7, 21, 11, 20, 2, 834000, tzinfo=UTC)
    assert jobs[1].company == "ICEYE" and jobs[1].city == "Espoo"
    assert "Developer Enablement" in jobs[1].description


def test_thehub_posted_at_falls_back_to_approved_then_created(make_ctx):
    jobs, _ctx = fetch(
        make_ctx,
        routes(
            **{
                VERDA: detail_doc(drop=("publishedAt",)),
                ICEYE: detail_doc("thehub_detail_iceye.json", drop=("publishedAt", "approvedAt")),
            }
        ),
    )
    assert jobs[0].posted_at == datetime(2026, 9, 1, 7, 13, 14, 934000, tzinfo=UTC)  # approvedAt
    assert jobs[1].posted_at == datetime(2026, 7, 18, 0, 41, 31, 8000, tzinfo=UTC)  # createdAt


def test_thehub_country_comes_from_the_detail_country_code(make_ctx):
    """The list doc for NOLOC has no location; only the detail knows where the job is."""
    jobs, _ctx = fetch(
        make_ctx,
        routes(**{NOLOC: detail_doc(id=NOLOC, countryCode="DK", location={}, isRemote=True)}),
    )
    job = jobs[2]
    assert job.country == "DK"  # not the FI query hint
    assert job.remote == "remote"


def test_thehub_falls_back_to_the_query_country_when_the_detail_is_silent(make_ctx):
    jobs, _ctx = fetch(make_ctx)  # NOLOC's detail route 404s
    assert jobs[2].country == "FI"
    assert jobs[2].posted_at is None and jobs[2].description is None


def test_thehub_keeps_opaque_ids_out_of_tags_and_employment_type(make_ctx):
    """jobRoles/jobPositionTypes ship as Mongo ObjectIds; only readable labels are kept."""
    jobs, _ctx = fetch(
        make_ctx,
        routes(
            **{
                ICEYE: detail_doc(
                    "thehub_detail_iceye.json",
                    jobRoles=["backenddeveloper", "engineer"],
                    jobPositionTypes=["Full-time"],
                )
            }
        ),
    )
    assert jobs[0].tags == []  # ["69b0f3be9990650fc9aa1c86", ...] is not a tag
    assert jobs[0].employment_type is None
    assert jobs[1].tags == ["backenddeveloper", "engineer"]
    assert jobs[1].employment_type == "Full-time"


def test_thehub_drops_a_posting_the_detail_reports_as_expired(make_ctx):
    jobs, _ctx = fetch(
        make_ctx,
        routes(**{ICEYE: detail_doc("thehub_detail_iceye.json", status="EXPIRED")}),
    )
    assert [j.source_id for j in jobs] == [VERDA, NOLOC]


def test_thehub_survives_a_broken_detail_endpoint(make_ctx):
    jobs, _ctx = fetch(
        make_ctx,
        {LIST: "thehub.json", DETAIL: lambda _req: httpx.Response(500, text="boom")},
    )
    assert [j.source_id for j in jobs] == [VERDA, ICEYE, NOLOC]
    assert all(j.posted_at is None and j.description is None for j in jobs)
    assert jobs[0].title == "AI Developer Advocate"  # list data survives on its own


def test_thehub_tolerates_an_unwrapped_or_unusable_detail(make_ctx):
    unwrapped = fixture_json("thehub_detail.json")["doc"]  # served without the {"doc": ...} envelope
    jobs, _ctx = fetch(
        make_ctx,
        routes(**{VERDA: unwrapped, ICEYE: [], NOLOC: {"doc": {"title": "no id here"}}}),
    )
    assert jobs[0].posted_at == datetime(2026, 9, 1, 7, 13, 15, 36000, tzinfo=UTC)
    assert jobs[1].posted_at is None and "detail" not in jobs[1].raw  # not a document at all
    assert jobs[2].raw["detail"] == {"title": "no id here"}  # unparsable, but kept for later


def test_thehub_iterates_countries_and_dedupes(make_ctx):
    jobs, ctx = fetch(make_ctx, options={"countries": ["FI", "SE"], "search": "developer"})

    assert len(jobs) == 3  # same ids on both country pages, emitted once
    list_calls = [c for c in ctx.http.calls if LIST in str(c.url)]
    assert len(list_calls) == 2
    assert "countryCode=FI" in str(list_calls[0].url)
    assert "search=developer" in str(list_calls[0].url)
    assert "countryCode=SE" in str(list_calls[1].url)
    # the second country page hits no detail endpoint again
    assert len([c for c in ctx.http.calls if DETAIL in str(c.url)]) == 3


def test_thehub_respects_limit(make_ctx):
    jobs, ctx = fetch(make_ctx, options={"countries": ["FI", "SE"], "max_pages": 5}, limit=1)
    assert len(jobs) == 1
    assert len([c for c in ctx.http.calls if DETAIL in str(c.url)]) == 1  # no wasted detail calls
