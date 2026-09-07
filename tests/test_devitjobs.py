from datetime import UTC, datetime

from jobscraper.sources.devitjobs import DevITJobs, contract_types, job_url, salary_text

ROUTES = {
    "germantechjobs.de/api/jobsLight": "devitjobs_de.json",
    "swissdevjobs.ch/api/jobsLight": "devitjobs_ch.json",
}


def test_devitjobs_parses_fixture(make_ctx):
    ctx = make_ctx(ROUTES, options={"sites": ["germantechjobs.de", "swissdevjobs.ch"]})
    jobs = list(DevITJobs().fetch(ctx))
    assert len(jobs) == 3  # the broken row is skipped, not raised
    j = jobs[0]
    assert j.source == "devitjobs"
    assert j.source_id == "66d7f1a2c3b4e5f600112233"
    assert j.title == "Junior Backend Developer (m/f/d)"
    assert j.company == "Beispiel Tech GmbH"
    assert j.url == "https://germantechjobs.de/jobs/junior-backend-developer-beispiel-tech-berlin"
    assert j.country == "DE" and j.city == "Berlin" and j.remote == "unknown"
    assert j.seniority_raw == "Full-Time" and j.employment_type == "permanent"
    assert j.salary_text == "48,000 - 60,000 EUR/year"
    assert j.tags == ["Python", "Django", "PostgreSQL", "lang:English"]
    assert j.posted_at == datetime(2025, 9, 2, 9, 0, tzinfo=UTC)
    assert j.description is None  # the light feed carries no description

    remote = jobs[1]
    assert remote.remote == "remote" and remote.country == "DE"  # falls back to the board's country
    assert remote.url == "https://germantechjobs.de/jobs/working-student-frontend-remote-werk"
    assert remote.tags == ["React", "TypeScript", "lang:German"]
    assert remote.posted_at == datetime.fromtimestamp(1756700000, tz=UTC)

    ch = jobs[2]
    assert ch.country == "CH" and ch.city == "Zurich"
    assert ch.url == "https://swissdevjobs.ch/jobs/junior-software-engineer-alpine-labs-zurich"
    assert ch.salary_text == "90,000 - 110,000 CHF/year"


def test_devitjobs_hits_one_endpoint_per_site(make_ctx):
    ctx = make_ctx(ROUTES, options={"sites": ["swissdevjobs.ch"]})
    jobs = list(DevITJobs().fetch(ctx))
    assert len(jobs) == 1 and len(ctx.http.calls) == 1
    assert str(ctx.http.calls[0].url) == "https://swissdevjobs.ch/api/jobsLight"


def test_devitjobs_respects_limit(make_ctx):
    ctx = make_ctx(ROUTES, options={"sites": ["germantechjobs.de", "swissdevjobs.ch"]}, limit=1)
    assert len(list(DevITJobs().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1


def test_devitjobs_helpers_tolerate_junk():
    assert job_url("devitjobs.uk", {}) is None
    assert job_url("devitjobs.uk", {"redirectJobUrl": "https://x.example/job"}) == "https://x.example/job"
    assert contract_types(7) == [] and contract_types({"permanent": False}) == []
    assert salary_text({}, "EUR") is None
    assert salary_text({"annualSalaryFrom": 50000}, None) == "50,000/year"
