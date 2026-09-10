"""Cornerstone (csod.com) description recovery — ``sources/_cornerstone.py``.

The listing API hands back ``externalDescription`` with the HTML tags already removed, so a
tenant who pasted a whole HTML page into the description field (GMV) leaks the text of its
``<style>`` blocks and nothing downstream can tell CSS from prose. The career site's own
requisition service still serves the original HTML; these tests drive that fetch over
:class:`FakeHttp` with payloads captured from gmv.csod.com and imec.csod.com on 2026-09-10.
"""

from __future__ import annotations

import logging

import pytest
from ats_scrapers import Job as ATSJob
from conftest import FakeHttp, fixture_text

from jobscraper.http import SourceHTTPError
from jobscraper.sources import _cornerstone

GMV_JOB = "https://gmv.csod.com/ux/ats/careersite/4/job/5045?c=gmv"
GMV_HOME = "https://gmv.csod.com/ux/ats/careersite/4/home?c=gmv"
GMV_DETAIL = "https://gmv.csod.com/services/x/job-requisition/v2/requisitions/5045"

ROUTES = {
    "/home?c=gmv": "cornerstone_home.html",
    "/job-requisition/v2/requisitions/5045": "cornerstone_requisition.json",
}


def cs_job(url: str = GMV_JOB, *, ats_id: str = "5045", description=None) -> ATSJob:
    return ATSJob(
        url=url,
        title="Aerospace engineer",
        company="gmv",
        ats_type="cornerstone",
        ats_id=ats_id,
        description=description,
    )


# ------------------------------------------------------------------ URL / token parsing


def test_parses_origin_site_and_requisition_from_a_job_url():
    req = _cornerstone.parse_job_url(GMV_JOB)
    assert req is not None
    assert req.origin == "https://gmv.csod.com"
    assert req.site_id == "4"
    assert req.requisition_id == "5045"
    assert req.slug == "gmv"
    assert req.home_url == GMV_HOME
    assert req.detail_url == GMV_DETAIL


def test_home_url_without_a_tenant_query_drops_it():
    req = _cornerstone.parse_job_url("https://imec.csod.com/ux/ats/careersite/29/job/10048")
    assert req is not None
    assert req.home_url == "https://imec.csod.com/ux/ats/careersite/29/home"


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/acme/jobs/1",
        "https://gmv.csod.com/ux/ats/careersite/4/home?c=gmv",  # not a job URL
        "",
        None,
    ],
)
def test_non_cornerstone_job_urls_are_not_parsed(url):
    assert _cornerstone.parse_job_url(url) is None


def test_token_comes_from_the_career_site_page():
    html = fixture_text("cornerstone_home.html")
    assert _cornerstone.parse_token(html).startswith("eyJhbGciOiJIUzUxMiJ9.")
    # The other shape Cornerstone ships the JWT in.
    assert _cornerstone.parse_token("csod.context.token = 'abc123';") == "abc123"
    assert _cornerstone.parse_token("<html>no token here</html>") is None
    assert _cornerstone.parse_token("") is None
    assert _cornerstone.parse_token(None) is None


def test_is_cornerstone_reads_the_scrapers_ats():
    class Fake:
        ats = "cornerstone"

    class Enumish:
        class ats:  # stands in for the ATSType.CORNERSTONE enum member
            value = "CORNERSTONE"

    assert _cornerstone.is_cornerstone(Fake())
    assert _cornerstone.is_cornerstone(Enumish())
    assert not _cornerstone.is_cornerstone(object())


# ------------------------------------------------------------------ payload parsing


def test_picks_the_english_culture_and_strips_the_stylesheet():
    payload = FakeHttp(ROUTES).get_json(GMV_DETAIL)
    text = _cornerstone.pick_description(payload)

    assert text is not None
    assert "We lead missions to outer planets" in text
    assert "Fluent in English" in text
    # The <style> blocks the listing API leaked as prose are gone.
    assert "wm-ab-launcher-spinner" not in text
    assert "@keyframes" not in text
    assert "{" not in text
    # Culture 1 is English even though this tenant's default culture is Spanish (15).
    assert "Lideramos misiones" not in text


def test_falls_back_to_the_default_culture_when_there_is_no_english():
    payload = FakeHttp(ROUTES).get_json(GMV_DETAIL)
    del payload["data"]["externalDescriptions"]["1"]
    text = _cornerstone.pick_description(payload)
    assert text is not None and "Lideramos misiones" in text


def test_a_placeholder_description_is_no_description():
    payload = FakeHttp(
        {"requisitions/10048": "cornerstone_requisition_empty.json"}
    ).get_json("https://imec.csod.com/services/x/job-requisition/v2/requisitions/10048")
    # imec publishes "..." for every posting — there is nothing to recover.
    assert payload["data"]["externalDescriptions"]["2"] == "..."
    assert _cornerstone.pick_description(payload) is None


@pytest.mark.parametrize("payload", [None, {}, {"data": None}, {"data": {}}, {"data": 7}])
def test_malformed_payloads_yield_no_description(payload):
    assert _cornerstone.pick_description(payload) is None


# ------------------------------------------------------------------ the fetch


def test_fetches_the_token_once_for_every_posting_of_a_tenant():
    http = FakeHttp({**ROUTES, "/job-requisition/v2/requisitions/5046": "cornerstone_requisition.json"})
    details = _cornerstone.CornerstoneDescriptions(http)

    first = details.get_description(cs_job())
    second = details.get_description(
        cs_job("https://gmv.csod.com/ux/ats/careersite/4/job/5046?c=gmv", ats_id="5046")
    )

    assert first and second and first == second
    urls = [str(c.url) for c in http.calls]
    assert urls.count(GMV_HOME) == 1  # the JWT is reused
    assert urls == [GMV_HOME, GMV_DETAIL, GMV_DETAIL.replace("5045", "5046")]
    assert http.calls[-1].headers["authorization"].startswith("Bearer eyJhbGciOiJIUzUxMiJ9.")


def test_a_job_that_is_not_a_cornerstone_posting_makes_no_request():
    http = FakeHttp(ROUTES)
    job = ATSJob(
        url="https://boards.greenhouse.io/acme/jobs/1",
        title="Dev",
        company="Acme",
        ats_type="greenhouse",
    )
    assert _cornerstone.CornerstoneDescriptions(http).get_description(job) is None
    assert http.calls == []


def test_a_career_site_without_a_token_is_only_asked_once(caplog):
    http = FakeHttp({**ROUTES, "/home?c=gmv": "<html>the page format changed</html>"})
    details = _cornerstone.CornerstoneDescriptions(http)
    with caplog.at_level(logging.WARNING, logger="jobscraper.sources._cornerstone"):
        assert details.get_description(cs_job()) is None
        assert details.get_description(cs_job()) is None

    assert [str(c.url) for c in http.calls] == [GMV_HOME]  # no detail request, no re-try
    assert "token" in caplog.text


def test_a_tenant_that_denies_the_requisition_service_raises_for_the_caller():
    import httpx

    http = FakeHttp(
        {
            "/home?c=gmv": "cornerstone_home.html",
            "/job-requisition/": lambda req: httpx.Response(403, text='{"status":2}'),
        }
    )
    # ats_boards logs and moves on; the error must reach it rather than be swallowed here.
    with pytest.raises(SourceHTTPError):
        _cornerstone.CornerstoneDescriptions(http).get_description(cs_job())
