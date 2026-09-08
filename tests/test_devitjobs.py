from datetime import UTC, datetime

import httpx
import pytest

from jobscraper.http import SourceHTTPError
from jobscraper.sources.devitjobs import (
    DevITJobs,
    contract_types,
    description_text,
    detail_url,
    is_entry_level,
    job_url,
    record_country,
    record_remote,
    remote_region,
    salary_text,
)

NETKNIGHTS = "646f0c1214cdd61c4543f935"  # expLevel Junior, workplace hybrid — has a detail fixture
TELUS = "6a67219f51c64c1ac8183ddd"  # expLevel Junior, workplace remote — its detail 500s
EMBEDDED = "634564b2fb3fac004c580774"  # expLevel Senior, workplace office — listed first in the feed

FEEDS = {
    "germantechjobs.de/api/jobsLight": "devitjobs_de.json",
    "swissdevjobs.ch/api/jobsLight": "devitjobs_ch.json",
}


def _boom(req: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text="upstream exploded")


DETAILS = {
    f"germantechjobs.de/api/job/{NETKNIGHTS}": "devitjobs_detail.json",
    f"germantechjobs.de/api/job/{TELUS}": _boom,
}

NO_DETAILS = {"fetch_details": False}


def detail_ids(ctx) -> list[str]:
    """The job ids whose detail endpoint was actually requested, in call order."""
    return [str(c.url).rsplit("/", 1)[-1] for c in ctx.http.calls if "/api/job/" in str(c.url)]


def test_devitjobs_parses_light_feed(make_ctx):
    ctx = make_ctx(FEEDS, options={"sites": ["germantechjobs.de", "swissdevjobs.ch"], **NO_DETAILS})
    jobs = list(DevITJobs().fetch(ctx))
    assert len(jobs) == 5  # the broken row is skipped, not raised
    assert detail_ids(ctx) == []

    j = jobs[0]
    assert j.source == "devitjobs"
    assert j.source_id == NETKNIGHTS
    assert j.title == "Open Source System Engineer für MFA (m/w/d)"
    assert j.company == "NetKnights GmbH"
    assert j.url == (
        "https://germantechjobs.de/jobs/NetKnights-GmbH-Open-Source-System-Engineer-fr-MFA-mwd"
    )
    assert j.country == "DE" and j.city == "Kassel"  # the feed has no country field
    assert j.location_raw == "Kassel, DE"
    assert j.remote == "hybrid" and j.remote_region is None  # workplace: "hybrid"
    assert j.seniority_raw == "Junior" and j.employment_type == "Full-Time"
    assert j.salary_text == "40,000 - 65,000 EUR/year"
    assert j.tags == ["Linux", "System Engineer", "Security", "lang:German"]
    assert j.posted_at == datetime(2026, 9, 4, 22, 0, tzinfo=UTC)  # activeFrom, +02:00
    assert j.description is None  # the light feed carries no description

    remote = jobs[1]
    assert remote.source_id == TELUS
    assert remote.remote == "remote" and remote.remote_region == "DE only"  # remoteType
    assert remote.employment_type == "Contract" and remote.seniority_raw == "Junior"
    assert remote.tags == ["AI", "Support", "Web", "lang:German"]  # technologies is empty here

    onsite = jobs[2]
    assert onsite.source_id == EMBEDDED
    assert onsite.remote == "onsite" and onsite.seniority_raw == "Senior"
    assert onsite.location_raw == "Lustenau, Munich, DE"

    ch = jobs[3]
    assert ch.source_id == "68ee5a0c39bdedf9c20fcb9a"
    assert ch.country == "CH" and ch.city == "Zug"
    assert ch.url == "https://swissdevjobs.ch/jobs/Dialectic-Internship---DataMLAI"
    assert ch.salary_text == "60,000 - 85,000 CHF/year"
    assert jobs[4].source_id == "6313076be3f58101d34f7316" and jobs[4].remote == "hybrid"


def test_devitjobs_fills_description_from_the_detail_endpoint(make_ctx):
    ctx = make_ctx({**FEEDS, **DETAILS}, options={"sites": ["germantechjobs.de"]})
    jobs = list(DevITJobs().fetch(ctx))

    assert str(ctx.http.calls[1].url) == f"https://germantechjobs.de/api/job/{NETKNIGHTS}"
    text = jobs[0].description
    assert text is not None
    assert text.startswith("Wir bei NetKnights glauben")
    assert "Responsibilities:\nDu liebst Open Source" in text
    assert "Requirements:\nDiese Position ist sowohl für Einsteiger" in text
    assert "Nice to have:\nDu bist kontaktfreudig" in text
    assert jobs[0].raw["tier"] == "business"  # the detail payload is merged into raw


def test_devitjobs_fetches_entry_level_details_first_and_caps_them(make_ctx):
    ctx = make_ctx({**FEEDS, **DETAILS}, options={"sites": ["germantechjobs.de"], "max_details": 2})
    jobs = list(DevITJobs().fetch(ctx))

    # The senior row is first in the feed but last in line for a detail call, and the cap of 2
    # is spent on the two entry-level rows.
    assert detail_ids(ctx) == [NETKNIGHTS, TELUS]
    assert [j.source_id for j in jobs] == [NETKNIGHTS, TELUS, EMBEDDED]


def test_devitjobs_keeps_the_job_when_its_detail_fails(make_ctx):
    ctx = make_ctx({**FEEDS, **DETAILS}, options={"sites": ["germantechjobs.de"], "max_details": 2})
    telus = next(j for j in DevITJobs().fetch(ctx) if j.source_id == TELUS)
    assert telus.title == "Quality Assurance Rater - German (Germany)"
    assert telus.description is None  # the 500 cost us the description, not the job


def test_devitjobs_hits_one_endpoint_per_site(make_ctx):
    ctx = make_ctx(FEEDS, options={"sites": ["swissdevjobs.ch"], **NO_DETAILS})
    jobs = list(DevITJobs().fetch(ctx))
    assert len(jobs) == 2 and len(ctx.http.calls) == 1
    assert str(ctx.http.calls[0].url) == "https://swissdevjobs.ch/api/jobsLight"


def test_devitjobs_respects_limit(make_ctx):
    ctx = make_ctx(
        FEEDS, options={"sites": ["germantechjobs.de", "swissdevjobs.ch"], **NO_DETAILS}, limit=1
    )
    assert len(list(DevITJobs().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1  # the second site is never touched


def test_devitjobs_entry_level_detection():
    assert is_entry_level({"expLevel": "Junior"})
    assert is_entry_level({"name": "Werkstudent (m/w/d) Softwareentwicklung"})
    assert is_entry_level({"name": "Pflichtpraktikum Robotik"})
    assert is_entry_level({"jobType": "Internship"})
    assert is_entry_level({"name": "Graduate Software Engineer"})
    assert is_entry_level({"name": "Absolvent Data Engineering (m/w/d)"})
    assert is_entry_level({"name": "Young Professional Cloud"})
    assert not is_entry_level({"name": "International Sales Engineer", "expLevel": "Regular"})
    assert not is_entry_level({"name": "Senior Internal Auditor"})
    assert not is_entry_level("not a dict")


def test_devitjobs_helpers_tolerate_junk():
    assert job_url("devitjobs.uk", {}) is None
    assert job_url("devitjobs.uk", {"jobUrl": "https://x.example/j"}) == "https://x.example/j"
    assert job_url("devitjobs.uk", {"redirectJobUrl": "https://x.example/job"}) == "https://x.example/job"
    assert contract_types(7) == [] and contract_types({"permanent": False}) == []
    assert salary_text({}, "EUR") is None
    assert salary_text({"annualSalaryFrom": 50000}, None) == "50,000/year"
    assert detail_url("devitjobs.uk", "abc123") == "https://devitjobs.uk/api/job/abc123"
    assert description_text({}) is None
    assert description_text({"description": "   "}) is None
    assert description_text({"description": "<p>Hi</p><br/>there"}).startswith("Hi")


def test_devitjobs_country_and_remote_helpers():
    assert record_country({"country": "de"}, "devitjobs.uk") == "DE"  # a row that carries ISO2
    assert record_country({"country": "Germany"}, "devitjobs.uk") == "DE"  # ... or a country name
    assert record_country({"country": "   "}, "devitjobs.uk") == "GB"  # blank falls back to the board
    assert record_country({}, "unknown.example") is None

    assert record_remote({"workplace": 7}, "Remote (Berlin)") == "remote"  # junk workplace → sniff
    assert record_remote({"workplace": "Hybrid "}, "Berlin") == "hybrid"  # cased/padded still maps
    assert record_remote({}, "Berlin") == "unknown"

    assert remote_region({"remoteType": "anywhere"}, "DE") == "Anywhere"
    assert remote_region({"remoteType": "countryandeu"}, "DE") == "DE + EU"
    assert remote_region({"remoteType": "onlycountry"}, None) == "onlycountry"  # no country to fill in
    assert remote_region({"remoteType": "emea"}, "DE") == "emea"  # unknown value passes through
    assert remote_region({"remoteType": "  "}, "DE") is None


def test_devitjobs_accepts_a_wrapped_feed_and_rejects_a_scalar_one(make_ctx):
    row = {"_id": "abc", "name": "Platform Engineer", "jobUrl": "/jobs/platform-engineer",
           "technologies": [{"name": "Go"}, {"value": "Rust"}, 7, "  "],
           "annualSalaryFrom": "40k"}
    ctx = make_ctx({"devitjobs.uk/api/jobsLight": {"jobs": [row]}},
                   options={"sites": ["devitjobs.uk"], **NO_DETAILS})
    job = next(iter(DevITJobs().fetch(ctx)))
    assert job.url == "https://devitjobs.uk/jobs/platform-engineer"  # jobUrl was already a path
    assert job.country == "GB" and job.tags == ["Go", "Rust"]
    assert job.salary_text == "40k GBP/year"

    ctx = make_ctx({"devitjobs.nl/api/jobsLight": '"maintenance"'},
                   options={"sites": ["devitjobs.nl"], **NO_DETAILS})
    with pytest.raises(SourceHTTPError, match="expected a list"):
        list(DevITJobs().fetch(ctx))


def test_devitjobs_keeps_the_job_when_the_detail_is_not_an_object(make_ctx):
    # Asking for a job the backend does not recognise answers with an array, not the job object.
    routes = {**FEEDS, f"germantechjobs.de/api/job/{NETKNIGHTS}": []}
    ctx = make_ctx(routes, options={"sites": ["germantechjobs.de"], "max_details": 1})
    job = next(iter(DevITJobs().fetch(ctx)))
    assert job.source_id == NETKNIGHTS and job.description is None


def test_devitjobs_remote_only_sites_skips_non_remote_rows_before_the_detail_call(make_ctx):
    routes = {**FEEDS, **DETAILS}
    options = {"sites": ["germantechjobs.de"], "remote_only_sites": ["germantechjobs.de"]}
    ctx = make_ctx(routes, options=options)
    jobs = list(DevITJobs().fetch(ctx))

    # Only the workplace: "remote" row survives; the office/hybrid ones never cost a detail call.
    assert [j.source_id for j in jobs] == [TELUS]
    assert detail_ids(ctx) == [TELUS]

    # Without the option the same feed yields the hybrid and office rows too.
    all_jobs = list(DevITJobs().fetch(make_ctx(routes, options={"sites": ["germantechjobs.de"]})))
    assert [j.source_id for j in all_jobs] == [NETKNIGHTS, TELUS, EMBEDDED]


def test_devitjobs_remote_only_leaves_other_sites_alone(make_ctx):
    ctx = make_ctx(FEEDS, options={"sites": ["germantechjobs.de", "swissdevjobs.ch"],
                                   "remote_only_sites": ["germantechjobs.de"], **NO_DETAILS})
    # The unfiltered board keeps its hybrid/office rows; the remote-only board keeps one.
    assert [j.source_id for j in DevITJobs().fetch(ctx)] == [
        TELUS, "68ee5a0c39bdedf9c20fcb9a", "6313076be3f58101d34f7316",
    ]


def test_devitjobs_deduplicates_ids_across_sites(make_ctx):
    ctx = make_ctx(FEEDS, options={"sites": ["germantechjobs.de", "germantechjobs.de"], **NO_DETAILS})
    jobs = list(DevITJobs().fetch(ctx))
    assert len(ctx.http.calls) == 2  # the board is fetched twice ...
    assert [j.source_id for j in jobs] == [NETKNIGHTS, TELUS, EMBEDDED]  # ... and yielded once
