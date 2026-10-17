"""Tests for :mod:`jobscraper.liveness`: is a posting's own page still open?

LinkedIn is a search source, so a posting missing from today's search says nothing; the page
itself does. Every fetch goes through :class:`tests.conftest.FakeHttp` — nothing here touches
the network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from jobscraper import liveness
from jobscraper.filters.rules import RULES_VERSION, evaluate
from jobscraper.http import Fetched, try_get
from jobscraper.models import FilterResult, Job, LivenessCheck
from jobscraper.store import Store
from tests.conftest import FakeHttp, fixture_text

LINKEDIN_URL = "https://fi.linkedin.com/jobs/view/junior-software-engineer-at-example-4477802497?refId=x"
GUEST_URL = "https://www.linkedin.com/jobs/view/4477802497"
SF_URL = "https://career5.successfactors.eu/sfcareer/jobreqcareer?jobId=173675&company=atlascopcoP"


def _linkedin_job(**kw) -> Job:
    base = {"source": "linkedin", "source_id": "4477802497", "url": LINKEDIN_URL,
            "title": "Junior Software Engineer", "company": "Example Oy", "country": "FI",
            "description": "We build backend services in Go. English working language.",
            "remote": "onsite"}
    base.update(kw)
    return Job(**base)


def _board_job(**kw) -> Job:
    base = {"source": "ats_boards", "source_id": "173675", "url": SF_URL,
            "title": "Junior Software Engineer", "company": "Atlas Copco", "country": "FI",
            "description": "We build backend services in Go. English working language.",
            "remote": "onsite"}
    base.update(kw)
    return Job(**base)


def _page(status: int | None, text: str = "", url: str = GUEST_URL, error: str | None = None) -> Fetched:
    return Fetched(url=url, status=status, text=text, error=error)


# ------------------------------------------------------------------ the detector


def test_a_closed_linkedin_page_is_closed() -> None:
    """The fixture's caption is Finnish: the class decides, not the English wording."""
    check = liveness.classify(_linkedin_job(), _page(200, fixture_text("liveness_linkedin_closed.html")))
    assert check.status == "closed"
    assert "no longer accepting applications" in check.reason.lower()
    assert check.http_status == 200
    assert check.job_id == _linkedin_job().id


def test_an_open_linkedin_page_is_open() -> None:
    check = liveness.classify(_linkedin_job(), _page(200, fixture_text("liveness_linkedin_open.html")))
    assert check.status == "open"


def test_the_linkedin_marker_counts_only_on_linkedin() -> None:
    """A source's own markers stay with that source: another board may reuse a class name."""
    check = liveness.classify(_board_job(), _page(200, fixture_text("liveness_linkedin_closed.html"),
                                                  url=SF_URL))
    assert check.status == "open"


def test_a_linkedin_login_wall_is_unknown() -> None:
    """LinkedIn answers a walled-off request with 200 and a login page: that says nothing."""
    check = liveness.classify(_linkedin_job(),
                              _page(200, "<html>Sign in</html>",
                                    url="https://www.linkedin.com/authwall?trk=x"))
    assert check.status == "unknown"
    assert "login wall" in check.reason


def test_a_successfactors_not_available_page_is_closed_on_any_source() -> None:
    """The SuccessFactors marker is generic: any board may sit on a SuccessFactors site."""
    for job in (_board_job(), _board_job(source="teamtailor"), _linkedin_job()):
        check = liveness.classify(job, _page(200, fixture_text("liveness_successfactors_closed.html"),
                                             url="https://careers.example.test/job/173675"))
        assert check.status == "closed", job.source
        assert check.reason == "SuccessFactors: job not available"


@pytest.mark.parametrize("status", [404, 410])
def test_404_and_410_are_gone_on_any_source(status: int) -> None:
    for job in (_linkedin_job(), _board_job()):
        check = liveness.classify(job, _page(status, "Not found"))
        assert check.status == "gone"
        assert check.reason == f"HTTP {status}"
        assert check.http_status == status


@pytest.mark.parametrize("status", [429, 403, 500, 503])
def test_other_error_statuses_are_unknown(status: int) -> None:
    check = liveness.classify(_linkedin_job(), _page(status, "slow down"))
    assert check.status == "unknown"
    assert check.reason == f"HTTP {status}"


def test_a_failed_fetch_is_unknown() -> None:
    check = liveness.classify(_board_job(), _page(None, error="ConnectTimeout: timed out"))
    assert check.status == "unknown"
    assert check.reason == "fetch failed: ConnectTimeout: timed out"
    assert check.http_status is None


def test_any_other_200_page_is_open() -> None:
    check = liveness.classify(_board_job(), _page(200, "<html><h1>Junior Software Engineer</h1></html>",
                                                  url=SF_URL))
    assert check.status == "open"
    assert check.reason == "HTTP 200"


def test_an_empty_page_is_open() -> None:
    assert liveness.classify(_board_job(), _page(200, "", url=SF_URL)).status == "open"


def test_detectors_are_a_per_source_table() -> None:
    assert "linkedin" in liveness.DETECTORS
    assert callable(liveness.DETECTORS["linkedin"])


def test_check_stamps_the_time() -> None:
    before = datetime.now(UTC)
    check = liveness.classify(_board_job(), _page(200, "", url=SF_URL))
    assert before <= check.checked_at <= datetime.now(UTC)


# ------------------------------------------------------------------ fetching


def test_try_get_never_raises_and_reports_where_it_landed() -> None:
    http = FakeHttp({"example.test": lambda req: httpx.Response(200, text="ok")})
    got = try_get(http, "https://example.test/a")
    assert got == Fetched(url="https://example.test/a", status=200, text="ok", error=None)

    def explode(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    broken = FakeHttp({"example.test": explode})
    failed = try_get(broken, "https://example.test/b")
    assert failed.status is None and failed.text == ""
    assert "refused" in (failed.error or "")
    assert failed.url == "https://example.test/b"


def test_check_fetches_the_linkedin_guest_page() -> None:
    http = FakeHttp({GUEST_URL: "liveness_linkedin_closed.html"})
    check = liveness.check(http, _linkedin_job())
    assert check.status == "closed"
    assert [str(r.url) for r in http.calls] == [GUEST_URL]


def test_check_fetches_any_other_job_at_its_own_url() -> None:
    http = FakeHttp({"successfactors.eu": "liveness_successfactors_closed.html"})
    check = liveness.check(http, _board_job())
    assert check.status == "closed"
    assert str(http.calls[0].url) == SF_URL


def test_check_falls_back_to_the_url_for_a_linkedin_row_without_a_job_view_link() -> None:
    http = FakeHttp({"linkedin.com/company": lambda req: httpx.Response(404)})
    check = liveness.check(http, _linkedin_job(url="https://www.linkedin.com/company/example"))
    assert check.status == "gone"


def test_check_turns_an_unrouted_fetch_into_unknown() -> None:
    check = liveness.check(FakeHttp({}), _board_job())
    assert check.status == "unknown"
    assert check.reason.startswith("fetch failed:")


def test_the_real_client_follows_redirects() -> None:
    """SuccessFactors 302s twice before the page that says the job is gone."""
    from jobscraper.http import Http

    http = Http(min_delay=0, retries=0)
    try:
        assert http._client.follow_redirects is True
    finally:
        http.close()


# ------------------------------------------------------------------ recording + re-filtering


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "jobs.db")
    yield s
    s.close()


def _closed(job: Job, when: datetime | None = None) -> LivenessCheck:
    return LivenessCheck(job_id=job.id, status="closed", reason="LinkedIn: no longer accepting applications",
                         checked_at=when or datetime(2026, 10, 17, 9, 0, tzinfo=UTC), http_status=200)


def test_record_saves_the_checks_and_drops_a_closed_job(store, settings, monkeypatch) -> None:
    from jobscraper.filters import rules

    monkeypatch.setattr(rules, "today_utc", lambda: date(2026, 10, 17))
    job, other = _linkedin_job(), _board_job()
    store.upsert_jobs([job, other])
    store.save_filter_results([evaluate(job, settings.profile), evaluate(other, settings.profile)],
                              RULES_VERSION)
    assert store.filter_results()[job.id].status != "drop"
    open_check = LivenessCheck(job_id=other.id, status="open", reason="HTTP 200")

    refiltered = liveness.record(store, settings.profile, [_closed(job), open_check])

    assert [r.job_id for r in refiltered] == [job.id]  # the open one had nothing to undo
    fr = store.filter_results()[job.id]
    assert fr.status == "drop"
    assert fr.reasons[0] == "posting closed (checked 2026-10-17)"
    assert fr.signals["liveness"] == "closed"
    assert store.filter_results()[other.id].status != "drop"
    assert set(store.liveness()) == {job.id, other.id}


def test_record_reopens_a_job_whose_page_is_open_again(store, settings, monkeypatch) -> None:
    from jobscraper.filters import rules

    monkeypatch.setattr(rules, "today_utc", lambda: date(2026, 10, 17))
    job = _linkedin_job()
    store.upsert_jobs([job])
    store.save_filter_results([evaluate(job, settings.profile)], RULES_VERSION)
    liveness.record(store, settings.profile, [_closed(job)])
    assert store.filter_results()[job.id].status == "drop"

    later = datetime(2026, 10, 18, tzinfo=UTC)
    liveness.record(store, settings.profile,
                    [LivenessCheck(job_id=job.id, status="open", reason="HTTP 200", checked_at=later)])
    assert store.filter_results()[job.id].status != "drop"


def test_record_leaves_duplicates_and_unfiltered_jobs_alone(store, settings) -> None:
    dup, unfiltered = _linkedin_job(), _board_job()
    store.upsert_jobs([dup, unfiltered])
    store.save_filter_results([FilterResult(job_id=dup.id, status="drop", reasons=["duplicate of abc"],
                                            signals={"duplicate_of": "abc"})], RULES_VERSION)

    assert liveness.record(store, settings.profile, [_closed(dup), _closed(unfiltered)]) == []
    assert store.filter_results()[dup.id].reasons == ["duplicate of abc"]
    assert unfiltered.id not in store.filter_results()
    assert set(store.liveness()) == {dup.id, unfiltered.id}  # the checks are kept all the same


def test_record_with_nothing_is_a_no_op(store, settings) -> None:
    assert liveness.record(store, settings.profile, []) == []
