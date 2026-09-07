from datetime import datetime, timedelta, timezone

import httpx

from jobscraper.sources.cvee import CvEe

SEARCH = "vacancy-search-service/search"


def test_cvee_parses_fixture(make_ctx):
    ctx = make_ctx({SEARCH: "cvee.json"}, options={"keywords": ["developer"], "max_pages": 1})
    jobs = list(CvEe().fetch(ctx))

    # 4 vacancies in, 2 out: the mediated-offer row and the id-less row are dropped.
    assert len(jobs) == 2
    j = jobs[0]
    assert j.source == "cvee"
    assert j.source_id == "1476412"
    assert j.url == "https://cv.ee/en/vacancy/1476412"
    assert j.title == "Full Stack Developer"
    assert j.company == "Bolt Technology OÜ"
    assert j.country == "EE"
    assert j.city is None and j.location_raw is None  # only a numeric townId is exposed
    assert j.raw["townId"] == 312
    assert j.remote == "hybrid"
    assert j.salary_text == "3000 - 4500 EUR"
    assert j.tags == ["react", "node.js"]
    assert j.description is None  # the search API returns no body
    assert j.posted_at == datetime(2026, 9, 1, 9, 12, tzinfo=timezone(timedelta(hours=3)))

    assert jobs[1].remote == "remote"
    assert jobs[1].salary_text is None


def test_cvee_paginates_until_total(make_ctx):
    pages = {"n": 0}

    def search(req: httpx.Request) -> httpx.Response:
        pages["n"] += 1
        offset = int(dict(req.url.params).get("offset", 0))
        return httpx.Response(
            200,
            json={
                "total": 4,
                "vacancies": [
                    {"id": 1000 + offset + i, "positionTitle": f"Dev {offset + i}",
                     "employerName": "Example OÜ"}
                    for i in range(2)
                ],
            },
        )

    ctx = make_ctx({SEARCH: search}, options={"keywords": ["dev"], "page_size": 2, "max_pages": 5})
    jobs = list(CvEe().fetch(ctx))
    assert [j.source_id for j in jobs] == ["1000", "1001", "1002", "1003"]
    assert pages["n"] == 2  # stops once offset reaches `total`


def test_cvee_respects_limit(make_ctx):
    ctx = make_ctx({SEARCH: "cvee.json"}, options={"keywords": ["a", "b"]}, limit=1)
    assert len(list(CvEe().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1


def test_cvee_dedupes_across_keywords(make_ctx):
    ctx = make_ctx({SEARCH: "cvee.json"}, options={"keywords": ["developer", "engineer"]})
    assert len(list(CvEe().fetch(ctx))) == 2
    assert len(ctx.http.calls) == 2
    assert "categories%5B%5D=INFORMATION_TECHNOLOGY" in str(ctx.http.calls[0].url)
