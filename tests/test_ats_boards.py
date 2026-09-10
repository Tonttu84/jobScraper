"""ats_boards adapter.

The library does the HTTP, so the tests monkeypatch ``get_scraper_for_url`` (and
``get_scraper``, for explicit-ATS entries) with fake scrapers whose ``.fetch()`` returns
``ats_scrapers`` Job objects built from the fixture.
"""

import logging
from datetime import UTC, datetime

import pytest
from ats_scrapers import Job as ATSJob
from ats_scrapers.exceptions import ScraperError
from conftest import fixture_json, fixture_text

from jobscraper.sources import ats_boards
from jobscraper.sources.ats_boards import ATSBoards, convert, slug_location

GREENHOUSE = "https://boards.greenhouse.io/supercell"
LEVER = "https://jobs.lever.co/wolt"
SIEMENS = "https://jobs.siemens.com"
CORNERSTONE_JOB = "https://gmv.csod.com/ux/ats/careersite/4/job/5045?c=gmv"

# Stand-in posting bodies. They are wordy on purpose: a description with almost no prose in
# it is treated as no description at all, so a two-word sentinel would test the wrong thing.
LISTING_BODY = (
    "The whole body of this posting, exactly as the listing payload handed it over: what "
    "the team builds, what you would work on and how to apply."
)
DETAIL_BODY = "The {title} posting in full, as served by the per-posting detail request."


def ats_jobs() -> list[ATSJob]:
    return [ATSJob(**rec) for rec in fixture_json("ats_boards.json")]


def make_job(title: str, *, ats_id: str = "1", company: str = "Acme", description=None) -> ATSJob:
    """A minimal ats-scrapers Job; only the title matters for the filter tests."""
    return ATSJob(
        url=f"https://boards.greenhouse.io/acme/jobs/{ats_id}",
        title=title,
        company=company,
        ats_type="greenhouse",
        ats_id=ats_id,
        description=description,
    )


class Calls(list):
    """Board keys in construction order; ``.made`` holds the fake scrapers built."""

    def __init__(self) -> None:
        super().__init__()
        self.made: list[FakeScraper] = []


class FakeScraper:
    """A scraper whose listing payload already carries descriptions (no lazy path)."""

    def __init__(self, jobs, **kwargs):
        self.jobs = jobs
        self.kwargs = kwargs

    def fetch(self):
        return list(self.jobs)


class LazyScraper(FakeScraper):
    """A scraper that supports per-job detail fetches (like Workday/Oracle)."""

    def __init__(self, jobs, **kwargs):
        super().__init__(jobs, **kwargs)
        self.described: list[str] = []

    def get_description(self, job):
        self.described.append(job.title)
        return DETAIL_BODY.format(title=job.title)


class BrokenLazyScraper(LazyScraper):
    def get_description(self, job):
        self.described.append(job.title)
        raise ScraperError("detail page 403")


class EmptyLazyScraper(LazyScraper):
    """Detail support, but the detail page has no body to give."""

    def get_description(self, job):
        self.described.append(job.title)
        return None


def patch_boards(monkeypatch, mapping):
    """mapping: board key → list of ats Jobs, an exception to raise, or (cls, jobs).

    Board keys are the careers URL for URL-resolved boards and ``(ats, slug)`` for
    explicit-ATS entries.
    """
    calls = Calls()

    def build(key, kwargs):
        calls.append(key)
        result = mapping[key]
        if isinstance(result, Exception):
            raise result
        cls, jobs = result if isinstance(result, tuple) else (FakeScraper, result)
        scraper = cls(jobs, **kwargs)
        calls.made.append(scraper)
        return scraper

    def fake_get_scraper_for_url(url, **kwargs):
        return build(url, kwargs)

    def fake_get_scraper(ats, slug, **kwargs):
        return build((ats, slug), kwargs)

    monkeypatch.setattr(ats_boards, "get_scraper_for_url", fake_get_scraper_for_url)
    monkeypatch.setattr(ats_boards, "get_scraper", fake_get_scraper)
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


# --------------------------------------------------------------- explicit ATS entries


def test_explicit_ats_entry_passes_options_and_company(monkeypatch, make_ctx):
    calls = patch_boards(monkeypatch, {("phenom", SIEMENS): [make_job("Software Engineer")]})
    entry = {
        "ats": "phenom",
        "slug": SIEMENS,
        "company": "Siemens",
        "options": {"locale": "en_global", "country": "global"},
    }
    jobs = list(ATSBoards().fetch(make_ctx({}, options={"urls": [entry]})))

    assert calls == [("phenom", SIEMENS)]
    assert calls.made[0].kwargs == {
        "timeout": 30.0,
        "include_descriptions": True,
        "locale": "en_global",
        "country": "global",
    }
    assert [j.company for j in jobs] == ["Siemens"]  # display-name override


def test_mapping_entry_with_url_is_resolved_like_a_string(monkeypatch, make_ctx):
    calls = patch_boards(monkeypatch, {GREENHOUSE: ats_jobs()})
    entry = {"url": GREENHOUSE, "company": "Supercell Oy", "include": "backend"}
    jobs = list(ATSBoards().fetch(make_ctx({}, options={"urls": [entry]})))

    assert calls == [GREENHOUSE]
    assert [(j.title, j.company) for j in jobs] == [("Junior Backend Engineer", "Supercell Oy")]


@pytest.mark.parametrize(
    "entry",
    [
        {"company": "Nokia"},  # neither url nor ats/slug
        {"ats": "phenom"},  # ats without slug
        {"slug": SIEMENS},  # slug without ats
        {"url": GREENHOUSE, "ats": "phenom", "slug": SIEMENS},  # ambiguous
        {"url": GREENHOUSE, "options": ["locale=en"]},  # options must be a mapping
        {"url": GREENHOUSE, "include": "(("},  # unusable regex
        {"url": GREENHOUSE, "exclude": 5},  # not a regex at all
        {"url": GREENHOUSE, "typo": 1},  # unknown key
        42,  # neither a URL nor a mapping
    ],
)
def test_malformed_entry_raises_before_fetching(monkeypatch, make_ctx, entry):
    calls = patch_boards(monkeypatch, {GREENHOUSE: ats_jobs()})
    ctx = make_ctx({}, options={"urls": [GREENHOUSE, entry]})
    with pytest.raises(ValueError, match="ats_boards"):
        list(ATSBoards().fetch(ctx))
    assert calls == []  # normalization fails fast: no board was fetched


def test_unknown_ats_name_fails_that_board_only(monkeypatch, make_ctx):
    calls = patch_boards(
        monkeypatch,
        {
            ("nosuchats", "acme"): ValueError("'nosuchats' is not a valid ATSType"),
            GREENHOUSE: ats_jobs(),
        },
    )
    entries = [{"ats": "nosuchats", "slug": "acme"}, GREENHOUSE]
    jobs = list(ATSBoards().fetch(make_ctx({}, options={"urls": entries})))
    assert calls == [("nosuchats", "acme"), GREENHOUSE]
    assert len(jobs) == 3


def test_unknown_ats_name_alone_raises_all_failed(monkeypatch, make_ctx):
    patch_boards(monkeypatch, {("nosuchats", "acme"): ValueError("not a valid ATSType")})
    ctx = make_ctx({}, options={"urls": [{"ats": "nosuchats", "slug": "acme"}]})
    with pytest.raises(RuntimeError, match="all 1 boards failed"):
        list(ATSBoards().fetch(ctx))


# --------------------------------------------------------------- title filters


def test_include_and_exclude_filter_titles_before_conversion(monkeypatch, make_ctx, caplog):
    listing = [
        make_job("Junior Software Engineer", ats_id="1"),
        make_job("Senior Software Engineer", ats_id="2"),
        make_job("Warehouse Operative", ats_id="3"),
        make_job("SOFTWARE developer intern", ats_id="4"),
    ]
    patch_boards(monkeypatch, {GREENHOUSE: listing})
    entry = {"url": GREENHOUSE, "include": "engineer|developer", "exclude": "senior|lead"}
    with caplog.at_level(logging.INFO, logger="jobscraper.sources.ats_boards"):
        jobs = list(ATSBoards().fetch(make_ctx({}, options={"urls": [entry]})))

    assert [j.title for j in jobs] == ["Junior Software Engineer", "SOFTWARE developer intern"]
    assert "4 jobs, 2 after title filter" in caplog.text


def test_source_level_defaults_apply_only_to_boards_without_their_own(monkeypatch, make_ctx):
    listing = [make_job("Senior Data Engineer", ats_id="1"), make_job("Data Engineer", ats_id="2")]
    patch_boards(monkeypatch, {GREENHOUSE: listing, LEVER: listing})
    options = {
        "urls": [GREENHOUSE, {"url": LEVER, "exclude": "nothing-here"}],
        "default_include": "engineer",
        "default_exclude": "senior",
    }
    jobs = list(ATSBoards().fetch(make_ctx({}, options=options)))
    # first board takes both defaults, the second keeps default_include but overrides exclude
    assert [j.title for j in jobs] == ["Data Engineer", "Senior Data Engineer", "Data Engineer"]


def test_title_filter_applies_before_the_limit(monkeypatch, make_ctx):
    listing = [make_job("Senior Engineer", ats_id="1"), make_job("Junior Engineer", ats_id="2")]
    patch_boards(monkeypatch, {GREENHOUSE: listing})
    options = {"urls": [GREENHOUSE], "default_exclude": "senior"}
    jobs = list(ATSBoards().fetch(make_ctx({}, options=options, limit=1)))
    assert [j.title for j in jobs] == ["Junior Engineer"]


# --------------------------------------------------------------- lazy descriptions


def test_filtered_board_with_detail_support_fetches_descriptions_lazily(monkeypatch, make_ctx):
    listing = [make_job("Junior Engineer", ats_id="1"), make_job("Senior Engineer", ats_id="2")]
    calls = patch_boards(monkeypatch, {GREENHOUSE: (LazyScraper, listing)})
    options = {"urls": [GREENHOUSE], "default_exclude": "senior"}
    jobs = list(ATSBoards().fetch(make_ctx({}, options=options)))

    scraper = calls.made[-1]
    assert scraper.kwargs["include_descriptions"] is False  # listing skips descriptions
    assert scraper.described == ["Junior Engineer"]  # only the survivor is detailed
    assert [j.description for j in jobs] == [DETAIL_BODY.format(title="Junior Engineer")]


def test_lazy_description_failure_leaves_the_job_without_one(monkeypatch, make_ctx):
    patch_boards(monkeypatch, {GREENHOUSE: (BrokenLazyScraper, [make_job("Junior Engineer")])})
    options = {"urls": [GREENHOUSE], "default_include": "engineer"}
    jobs = list(ATSBoards().fetch(make_ctx({}, options=options)))
    assert len(jobs) == 1 and jobs[0].description is None


def test_scraper_without_detail_support_keeps_full_listing_descriptions(monkeypatch, make_ctx):
    listing = [make_job("Junior Engineer", description=LISTING_BODY)]
    calls = patch_boards(monkeypatch, {GREENHOUSE: listing})
    options = {"urls": [GREENHOUSE], "default_include": "engineer"}
    jobs = list(ATSBoards().fetch(make_ctx({}, options=options)))

    assert len(calls.made) == 1
    assert calls.made[0].kwargs["include_descriptions"] is True
    assert jobs[0].description == LISTING_BODY


def test_lazy_descriptions_false_forces_the_eager_path(monkeypatch, make_ctx):
    listing = [make_job("Junior Engineer", description=LISTING_BODY)]
    calls = patch_boards(monkeypatch, {GREENHOUSE: (LazyScraper, listing)})
    options = {"urls": [GREENHOUSE], "default_include": "engineer", "lazy_descriptions": False}
    jobs = list(ATSBoards().fetch(make_ctx({}, options=options)))

    assert calls.made[-1].kwargs["include_descriptions"] is True
    assert calls.made[-1].described == []
    assert jobs[0].description == LISTING_BODY


def test_lazy_descriptions_true_applies_without_a_title_filter(monkeypatch, make_ctx):
    calls = patch_boards(monkeypatch, {GREENHOUSE: (LazyScraper, [make_job("Anything")])})
    options = {"urls": [GREENHOUSE], "lazy_descriptions": True}
    jobs = list(ATSBoards().fetch(make_ctx({}, options=options)))

    assert calls.made[-1].kwargs["include_descriptions"] is False
    assert jobs[0].description == DETAIL_BODY.format(title="Anything")


def test_unfiltered_board_is_not_lazy_by_default(monkeypatch, make_ctx):
    calls = patch_boards(monkeypatch, {GREENHOUSE: (LazyScraper, [make_job("Anything")])})
    jobs = list(ATSBoards().fetch(make_ctx({}, options={"urls": [GREENHOUSE]})))

    assert len(calls.made) == 1
    assert calls.made[0].kwargs["include_descriptions"] is True
    assert calls.made[0].described == []
    assert jobs[0].description is None


def test_lazy_descriptions_accepts_yaml_strings(monkeypatch, make_ctx):
    calls = patch_boards(monkeypatch, {GREENHOUSE: (LazyScraper, [make_job("Junior Engineer")])})
    options = {"urls": [GREENHOUSE], "default_include": "engineer", "lazy_descriptions": "false"}
    list(ATSBoards().fetch(make_ctx({}, options=options)))
    assert calls.made[-1].kwargs["include_descriptions"] is True

    calls = patch_boards(monkeypatch, {GREENHOUSE: (LazyScraper, [make_job("Junior Engineer")])})
    options = {"urls": [GREENHOUSE], "default_include": "engineer", "lazy_descriptions": "auto"}
    list(ATSBoards().fetch(make_ctx({}, options=options)))
    assert calls.made[-1].kwargs["include_descriptions"] is False


@pytest.mark.parametrize(
    "options",
    [
        {"urls": [GREENHOUSE], "lazy_descriptions": "sometimes"},
        {"urls": [{"url": GREENHOUSE, "lazy_descriptions": 7}]},
    ],
)
def test_bad_lazy_descriptions_value_raises(monkeypatch, make_ctx, options):
    calls = patch_boards(monkeypatch, {GREENHOUSE: ats_jobs()})
    with pytest.raises(ValueError, match="true, false or auto"):
        list(ATSBoards().fetch(make_ctx({}, options=options)))
    assert calls == []


def test_lazy_path_leaves_descriptions_the_listing_already_carried(monkeypatch, make_ctx):
    listing = [
        make_job("Junior Engineer", ats_id="1", description=LISTING_BODY),
        make_job("Graduate Engineer", ats_id="2"),
    ]
    calls = patch_boards(monkeypatch, {GREENHOUSE: (EmptyLazyScraper, listing)})
    options = {"urls": [GREENHOUSE], "default_include": "engineer"}
    jobs = list(ATSBoards().fetch(make_ctx({}, options=options)))

    assert calls.made[-1].described == ["Graduate Engineer"]  # the other one was already done
    assert [j.description for j in jobs] == [LISTING_BODY, None]  # no body found, no crash


# ------------------------------------------------- page-shell / empty descriptions


def test_page_shell_description_is_stored_as_missing():
    """GMV pastes a whole HTML page into the description field, and Cornerstone's API
    strips the tags, so the text of its <style> blocks arrives as prose. Better nothing."""
    job = make_job("Engineer", description=fixture_text("cornerstone_listing_shell.txt"))
    assert convert(job).description is None


@pytest.mark.parametrize("body", ["...", "   ", "n/a", "TBD - see our website", "<p>&nbsp;</p>"])
def test_a_description_that_says_nothing_is_missing(body):
    assert convert(make_job("Engineer", description=body)).description is None


def test_a_real_description_with_a_code_snippet_survives():
    body = (
        "We are looking for a backend engineer to grow our Go services. You will write "
        'code like `func main() { fmt.Println("hi") }`, ship it weekly, review pull '
        "requests and mentor our interns."
    )
    assert convert(make_job("Engineer", description=body)).description == body


# --------------------------------------------------------------- Cornerstone boards


def cornerstone_job(*, ats_id: str = "5045", description=None) -> ATSJob:
    return ATSJob(
        url=CORNERSTONE_JOB.replace("5045", ats_id),
        title="Aerospace engineer",
        company="gmv",
        ats_type="cornerstone",
        ats_id=ats_id,
        description=description,
    )


class CornerstoneFake(FakeScraper):
    """Like the library's CornerstoneScraper: no ``get_description`` of its own, an ``ats``
    that says which provider it is, and a listing that always carries a description."""

    ats = "cornerstone"


CORNERSTONE_ROUTES = {
    "/home?c=gmv": "cornerstone_home.html",
    "/job-requisition/v2/requisitions/5045": "cornerstone_requisition.json",
}
CORNERSTONE_ENTRY = {"ats": "cornerstone", "slug": "gmv", "company": "GMV"}


def test_cornerstone_shell_description_is_refetched_from_the_requisition_service(
    monkeypatch, make_ctx
):
    listing = [cornerstone_job(description=fixture_text("cornerstone_listing_shell.txt"))]
    patch_boards(monkeypatch, {("cornerstone", "gmv"): (CornerstoneFake, listing)})
    ctx = make_ctx(
        CORNERSTONE_ROUTES,
        options={"urls": [CORNERSTONE_ENTRY], "default_include": "engineer"},
    )
    jobs = list(ATSBoards().fetch(ctx))

    assert len(jobs) == 1
    assert "We lead missions to outer planets" in jobs[0].description
    assert "wm-ab-launcher-spinner" not in jobs[0].description
    assert jobs[0].company == "GMV"


def test_cornerstone_board_leaves_a_usable_listing_description_alone(monkeypatch, make_ctx):
    body = (
        "Conduct tests at unit, subsystem and satellite level, write and maintain test "
        "procedures and hardware work instructions, and prepare the test reports."
    )
    patch_boards(
        monkeypatch,
        {("cornerstone", "gmv"): (CornerstoneFake, [cornerstone_job(description=body)])},
    )
    # No routes: any HTTP request would raise. OHB and Henkel look like this.
    ctx = make_ctx({}, options={"urls": [CORNERSTONE_ENTRY], "default_include": "engineer"})
    jobs = list(ATSBoards().fetch(ctx))

    assert [j.description for j in jobs] == [body]
    assert ctx.http.calls == []


def test_lazy_descriptions_false_skips_the_cornerstone_detail_fetch(monkeypatch, make_ctx):
    """imec publishes "..." for every posting and the requisition service repeats it, so
    that board is configured eager: no description, and no request wasted asking for one."""
    patch_boards(
        monkeypatch,
        {("cornerstone", "gmv"): (CornerstoneFake, [cornerstone_job(description="...")])},
    )
    options = {
        "urls": [{**CORNERSTONE_ENTRY, "lazy_descriptions": False}],
        "default_include": "engineer",
    }
    ctx = make_ctx({}, options=options)
    jobs = list(ATSBoards().fetch(ctx))

    assert [j.description for j in jobs] == [None]
    assert ctx.http.calls == []


def test_a_failed_cornerstone_detail_fetch_leaves_the_job_without_a_description(
    monkeypatch, make_ctx, caplog
):
    import httpx

    listing = [cornerstone_job(description=fixture_text("cornerstone_listing_shell.txt"))]
    patch_boards(monkeypatch, {("cornerstone", "gmv"): (CornerstoneFake, listing)})
    ctx = make_ctx(
        {
            "/home?c=gmv": "cornerstone_home.html",
            "/job-requisition/": lambda req: httpx.Response(403, text='{"status":2}'),
        },
        options={"urls": [CORNERSTONE_ENTRY], "default_include": "engineer"},
    )
    with caplog.at_level(logging.WARNING, logger="jobscraper.sources.ats_boards"):
        jobs = list(ATSBoards().fetch(ctx))

    assert [j.description for j in jobs] == [None]
    assert "description fetch failed" in caplog.text


# --------------------------------------------------------------- per-board default country


def located(location, *, ats_id="1", raw=None, title="Software Engineer") -> ATSJob:
    """A listing row whose location string is all we know."""
    job = make_job(title, ats_id=ats_id)
    object.__setattr__(job, "location", location)
    if raw is not None:
        object.__setattr__(job, "raw", raw)
    return job


def test_board_country_fills_only_the_rows_left_unknown(monkeypatch, make_ctx):
    """Single-country boards (OP-Palvelut, imec, …) may declare where they hire."""
    listing = [located("2 Locations", ats_id="1"), located("Redmond, WA, US", ats_id="2")]
    patch_boards(monkeypatch, {GREENHOUSE: listing})
    entry = {"url": GREENHOUSE, "country": "fi"}
    jobs = list(ATSBoards().fetch(make_ctx({}, options={"urls": [entry]})))

    assert [j.country for j in jobs] == ["FI", "US"]  # parsed country is never overwritten


def test_board_country_works_on_an_explicit_ats_entry(monkeypatch, make_ctx):
    patch_boards(monkeypatch, {("cornerstone", "imec"): [located("Leuven")]})
    entry = {"ats": "cornerstone", "slug": "imec", "company": "imec", "country": "BE"}
    jobs = list(ATSBoards().fetch(make_ctx({}, options={"urls": [entry]})))

    assert jobs[0].country == "BE"


@pytest.mark.parametrize("value", ["Finland", "FIN", "", "ZZ"])
def test_bad_board_country_raises(monkeypatch, make_ctx, value):
    calls = patch_boards(monkeypatch, {GREENHOUSE: ats_jobs()})
    entry = {"url": GREENHOUSE, "country": value}
    with pytest.raises(ValueError, match="ISO-2"):
        list(ATSBoards().fetch(make_ctx({}, options={"urls": [entry]})))
    assert calls == []  # config bugs fail before any board is opened


def test_workday_locations_list_resolves_a_rollup_location(monkeypatch, make_ctx):
    """Workday's search rows say "2 Locations"; the raw payload sometimes lists them."""
    listing = [
        located("2 Locations", ats_id="1", raw={"locations": ["Espoo, Finland", "Berlin, DE"]}),
        located("3 Locations", ats_id="2", raw={"locations": [{"descriptor": "Hyderabad, TS, IN"}]}),
        located("2 Locations", ats_id="3", raw={"locations": "Bengaluru, KA, IN"}),
        located("2 Locations", ats_id="4", raw={"locations": [{"id": 7}, "Tokyo"]}),
        located("2 Locations", ats_id="5", raw={"locations": ["Anywhere"]}),
        located("2 Locations", ats_id="6", raw={"locations": {"city": "Espoo"}}),
        located("2 Locations", ats_id="7", raw={"bullet_fields": ["JR001"]}),
    ]
    patch_boards(monkeypatch, {GREENHOUSE: listing})
    jobs = list(ATSBoards().fetch(make_ctx({}, options={"urls": [GREENHOUSE]})))

    assert [j.country for j in jobs] == ["FI", "IN", "IN", "JP", None, None, None]


# --------------------------------------------------------------- Workday externalPath slugs


def workday_jobs() -> list[ATSJob]:
    """Workday search rows as the library hands them over: no ``locations`` list, a slug."""
    return [ATSJob(**rec) for rec in fixture_json("ats_boards_workday.json")]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/job/Shanghai-Shanghai-China/Software-Engineer_R12345", "Shanghai, Shanghai, China"),
        ("/job/Espoo-Finland/Software-Engineer_R12345", "Espoo, Finland"),
        ("/job/US-CA-San-Jose/Developer-Intern_R1", "US, CA, San, Jose"),
        ("/job/Multiple-Locations/Graduate-Developer_R1", None),  # a rollup names no place
        ("/job/Remote/Graduate-Developer_R1", None),
        ("/job/--/Graduate-Developer_R1", None),  # nothing but separators
        ("/job/", None),  # no location segment at all
        ("/details/Espoo-Finland/x", None),  # not a job path
        ("", None),
        (None, None),
        (17, None),
    ],
)
def test_slug_location_reads_the_workday_path(path, expected):
    assert slug_location(path) == expected


def test_workday_external_path_names_the_country(monkeypatch, make_ctx):
    """Most Workday tenants send neither a locations list nor a country — only the slug."""
    patch_boards(monkeypatch, {GREENHOUSE: workday_jobs()})
    jobs = list(ATSBoards().fetch(make_ctx({}, options={"urls": [GREENHOUSE]})))

    assert [j.country for j in jobs] == ["FI", "CN", None, "US"]
    assert jobs[0].location_raw == "Espoo, Finland"  # no location string: the slug becomes one
    assert jobs[0].city == "Espoo"
    assert jobs[1].location_raw == "2 Locations"  # the rollup the tenant did publish stands


# --------------------------------------------------------------- per-board country filter


def test_country_filter_drops_out_of_region_before_the_detail_fetch(monkeypatch, make_ctx, caplog):
    """The point of the filter: an Indian posting never costs a description request."""
    listing = [
        located("Helsinki, Finland", ats_id="1", title="Junior Engineer Helsinki"),
        located("Bengaluru, KA, IN", ats_id="2", title="Junior Engineer Bengaluru"),
        located("2 Locations", ats_id="3", title="Junior Engineer Somewhere"),
        located("Remote - United States", ats_id="4", title="Junior Engineer Remote"),
    ]
    calls = patch_boards(monkeypatch, {GREENHOUSE: (LazyScraper, listing)})
    entry = {"url": GREENHOUSE, "countries": ["FI", "de"], "include": "engineer"}
    with caplog.at_level(logging.INFO, logger="jobscraper.sources.ats_boards"):
        jobs = list(ATSBoards().fetch(make_ctx({}, options={"urls": [entry]})))

    # kept: in-region, unknown country, and remote whatever its country
    assert calls.made[-1].described == [
        "Junior Engineer Helsinki",
        "Junior Engineer Somewhere",
        "Junior Engineer Remote",
    ]
    assert [j.country for j in jobs] == ["FI", None, "US"]
    assert "4 jobs, 4 after title filter, 3 after country filter" in caplog.text


def test_default_countries_apply_only_to_boards_without_their_own(monkeypatch, make_ctx):
    listing = [located("Helsinki, Finland", ats_id="1"), located("Bengaluru, KA, IN", ats_id="2")]
    patch_boards(monkeypatch, {GREENHOUSE: listing, LEVER: listing})
    options = {
        "urls": [GREENHOUSE, {"url": LEVER, "countries": ["IN"]}],
        "default_countries": ["FI", "SE"],
    }
    jobs = list(ATSBoards().fetch(make_ctx({}, options=options)))
    assert [j.country for j in jobs] == ["FI", "IN"]


def test_countries_accepts_a_single_code(monkeypatch, make_ctx):
    listing = [located("Helsinki, Finland", ats_id="1"), located("Bengaluru, KA, IN", ats_id="2")]
    patch_boards(monkeypatch, {GREENHOUSE: listing})
    entry = {"url": GREENHOUSE, "countries": "fi"}
    jobs = list(ATSBoards().fetch(make_ctx({}, options={"urls": [entry]})))
    assert [j.country for j in jobs] == ["FI"]


def test_an_empty_countries_list_filters_nothing(monkeypatch, make_ctx):
    patch_boards(monkeypatch, {GREENHOUSE: [located("Bengaluru, KA, IN")]})
    entry = {"url": GREENHOUSE, "countries": []}
    jobs = list(ATSBoards().fetch(make_ctx({}, options={"urls": [entry]})))
    assert [j.country for j in jobs] == ["IN"]


def test_board_country_decides_the_rows_the_filter_cannot_place(monkeypatch, make_ctx):
    """``country:`` is the board's own fallback, so the country filter reads it too."""
    listing = [located("2 Locations", ats_id="1"), located("Helsinki", ats_id="2")]
    patch_boards(monkeypatch, {GREENHOUSE: listing})
    entry = {"url": GREENHOUSE, "country": "US", "countries": ["FI"]}
    jobs = list(ATSBoards().fetch(make_ctx({}, options={"urls": [entry]})))
    assert [j.country for j in jobs] == ["FI"]  # the unplaceable row was dropped as US


@pytest.mark.parametrize(
    "options",
    [
        {"urls": [{"url": GREENHOUSE, "countries": ["Finland"]}]},  # a name, not a code
        {"urls": [{"url": GREENHOUSE, "countries": [False]}]},  # unquoted NO in YAML
        {"urls": [{"url": GREENHOUSE, "countries": "nowhere"}]},
        {"urls": [{"url": GREENHOUSE, "countries": {"fi": True}}]},
        {"urls": [GREENHOUSE], "default_countries": ["ZZ"]},
    ],
)
def test_bad_countries_value_raises(monkeypatch, make_ctx, options):
    calls = patch_boards(monkeypatch, {GREENHOUSE: ats_jobs()})
    with pytest.raises(ValueError, match="ISO-2"):
        list(ATSBoards().fetch(make_ctx({}, options=options)))
    assert calls == []  # config bugs fail before any board is opened
