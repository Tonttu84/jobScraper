"""ats_boards adapter.

The library does the HTTP, so the tests monkeypatch ``get_scraper_for_url`` with a fake
scraper whose ``.fetch()`` returns ``ats_scrapers`` Job objects built from the fixture.
"""

from datetime import UTC, datetime

import pytest
from ats_scrapers import Job as ATSJob
from ats_scrapers.exceptions import ScraperError
from conftest import fixture_json

from jobscraper.sources import ats_boards
from jobscraper.sources.ats_boards import ATSBoards

GREENHOUSE = "https://boards.greenhouse.io/supercell"
LEVER = "https://jobs.lever.co/wolt"


def ats_jobs() -> list[ATSJob]:
    return [ATSJob(**rec) for rec in fixture_json("ats_boards.json")]


class FakeScraper:
    def __init__(self, jobs):
        self.jobs = jobs

    def fetch(self):
        return list(self.jobs)


def patch_boards(monkeypatch, mapping):
    """mapping: careers URL → list of ats Jobs, or an exception instance to raise."""
    calls: list[str] = []

    def fake_get_scraper_for_url(url, **kwargs):
        calls.append(url)
        result = mapping[url]
        if isinstance(result, Exception):
            raise result
        return FakeScraper(result)

    monkeypatch.setattr(ats_boards, "get_scraper_for_url", fake_get_scraper_for_url)
    return calls


def test_converts_library_jobs(monkeypatch, make_ctx):
    patch_boards(monkeypatch, {GREENHOUSE: ats_jobs()})
    jobs = list(ATSBoards().fetch(make_ctx({}, options={"urls": [GREENHOUSE]})))

    assert len(jobs) == 3
    gh, lever, workable = jobs

    assert gh.source == "ats_boards"
    assert gh.source_id == "greenhouse:4567890"
    assert gh.url == "https://boards.greenhouse.io/supercell/jobs/4567890"
    assert gh.title == "Junior Backend Engineer" and gh.company == "Supercell"
    assert gh.country == "FI" and gh.city == "Helsinki"  # derived from the location text
    assert gh.remote == "unknown"
    assert gh.employment_type == "FULL_TIME"
    assert gh.salary_text == "45 000 € – 60 000 € / year"
    assert gh.tags == ["Engineering"]
    assert "backend" in gh.description and "<" not in gh.description  # HTML stripped
    assert gh.posted_at == datetime(2026, 9, 2, 10, 0, tzinfo=UTC)

    assert lever.remote == "remote"  # is_remote flag from the ATS
    assert lever.city is None  # "Remote - Europe" is not a city
    assert lever.salary_text == "55 000–70 000 EUR / year"  # composed from min/max
    assert lever.description.startswith("Plain text body")  # left alone, no tags

    # no ats_id → the posting URL keeps source_id stable
    assert workable.source_id == "workable:https://apply.workable.com/oura/j/ABC123"
    assert workable.country == "EE"  # country_iso wins over the location guess
    assert workable.description is None


def test_one_board_failing_does_not_stop_the_rest(monkeypatch, make_ctx):
    calls = patch_boards(
        monkeypatch,
        {LEVER: ScraperError("Could not recognize an ATS"), GREENHOUSE: ats_jobs()},
    )
    jobs = list(ATSBoards().fetch(make_ctx({}, options={"urls": [LEVER, GREENHOUSE]})))
    assert calls == [LEVER, GREENHOUSE]
    assert len(jobs) == 3


def test_all_boards_failing_raises(monkeypatch, make_ctx):
    patch_boards(monkeypatch, {LEVER: ScraperError("boom"), GREENHOUSE: TimeoutError("slow")})
    ctx = make_ctx({}, options={"urls": [LEVER, GREENHOUSE]})
    with pytest.raises(RuntimeError, match="all 2 boards failed"):
        list(ATSBoards().fetch(ctx))


def test_bad_record_is_skipped_not_raised(monkeypatch, make_ctx):
    good = ats_jobs()[0]
    broken = ats_jobs()[1]
    object.__setattr__(broken, "title", None)  # bypass validation to fake a bad row
    patch_boards(monkeypatch, {GREENHOUSE: [broken, good]})
    jobs = list(ATSBoards().fetch(make_ctx({}, options={"urls": [GREENHOUSE]})))
    assert [j.source_id for j in jobs] == ["greenhouse:4567890"]


def test_limit_stops_early(monkeypatch, make_ctx):
    calls = patch_boards(monkeypatch, {GREENHOUSE: ats_jobs(), LEVER: ats_jobs()})
    ctx = make_ctx({}, options={"urls": [GREENHOUSE, LEVER]}, limit=2)
    assert len(list(ATSBoards().fetch(ctx))) == 2
    assert calls == [GREENHOUSE]  # second board never touched


def test_missing_urls_option_raises(make_ctx):
    with pytest.raises(ValueError, match="no careers URLs"):
        list(ATSBoards().fetch(make_ctx({})))
