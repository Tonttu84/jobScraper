"""Tests for :mod:`jobscraper.filters.rules` against the real ``config/profile.yaml``.

The filter is deliberately permissive: it may only ``drop`` on strong signals, everything
softer must become ``review`` so the AI stage still sees it.
"""

from __future__ import annotations

import itertools

import pytest

from jobscraper.filters.rules import classify_remote_region, dedupe, evaluate
from jobscraper.models import Job

_ids = itertools.count(1)

ENGLISH_DESC = (
    "We are looking for a junior software developer to join our platform team. You will work "
    "with TypeScript, React and Node.js and learn from experienced colleagues. We offer "
    "mentoring, flexible hours and a friendly team that likes to teach."
)

POLISH_DESC = (
    "Poszukujemy młodszego programisty do naszego zespołu w Warszawie, który pomoże nam "
    "rozwijać nasze aplikacje internetowe. Będziesz pracować w zespole doświadczonych "
    "inżynierów nad nowymi funkcjami naszego produktu. Oferujemy elastyczne godziny pracy, "
    "prywatną opiekę medyczną, dofinansowanie karty sportowej oraz przyjazną atmosferę w "
    "zespole. Zapraszamy do aplikowania osoby, które chcą się rozwijać i uczyć nowych "
    "technologii razem z nami."
)


def make_job(**kw) -> Job:
    """Build a Job with sensible defaults; every call gets a distinct id."""
    n = next(_ids)
    base = {
        "source": "test",
        "source_id": f"job-{n}",
        "url": f"https://jobs.example.test/{n}",
        "title": "Junior Software Developer",
        "company": "Example Oy",
        "description": ENGLISH_DESC,
        "remote": "onsite",
    }
    base.update(kw)
    return Job(**base)


# ------------------------------------------------------------------ seniority / role


def test_junior_in_helsinki_is_kept(settings):
    job = make_job(title="Junior Software Developer", location_raw="Helsinki, Finland",
                   country="FI", city="Helsinki")
    res = evaluate(job, settings.profile)
    assert res.status == "keep"
    assert res.reasons == []
    assert res.location_tier == 1
    assert res.signals["seniority"] == "entry_by_title"


def test_bare_junior_developer_title_passes_role_gate(settings):
    job = make_job(title="Junior Developer", location_raw="Helsinki, Finland", country="FI")
    res = evaluate(job, settings.profile)
    assert res.status == "keep"
    assert res.location_tier == 1
    assert res.signals["seniority"] == "entry_by_title"


def test_senior_title_is_dropped(settings):
    job = make_job(title="Senior Software Engineer", location_raw="Berlin, Germany", country="DE")
    res = evaluate(job, settings.profile)
    assert res.status == "drop"
    assert any("senior" in r.lower() for r in res.reasons)
    assert res.signals["seniority"] == "senior_by_title"


def test_three_years_experience_is_review_not_drop(settings):
    job = make_job(
        title="Software Developer",
        description="We are looking for someone with 3+ years of experience in web development. "
        + ENGLISH_DESC,
        location_raw="Tallinn, Estonia",
        country="EE",
    )
    res = evaluate(job, settings.profile)
    assert res.status == "review"
    assert res.signals["years_required"] == 3


def test_six_years_experience_is_dropped(settings):
    job = make_job(
        title="Software Developer",
        description="We expect a minimum 6 years of experience with backend development. "
        + ENGLISH_DESC,
        location_raw="Tallinn, Estonia",
        country="EE",
    )
    res = evaluate(job, settings.profile)
    assert res.status == "drop"
    assert res.signals["years_required"] == 6


# ------------------------------------------------------------------ language


def test_posting_written_in_polish_is_dropped(settings):
    assert len(POLISH_DESC) >= 300
    job = make_job(title="Junior Software Developer", description=POLISH_DESC,
                   location_raw="Warszawa", country="PL")
    res = evaluate(job, settings.profile)
    assert res.status == "drop"
    assert any("posting written in pl" in r for r in res.reasons)


def test_polish_as_a_plus_in_warsaw_is_not_dropped(settings):
    job = make_job(
        title="Junior Software Developer",
        description="Polish is a plus, English is our working language. " + ENGLISH_DESC,
        location_raw="Warsaw, Poland",
        country="PL",
    )
    res = evaluate(job, settings.profile)
    assert res.status in ("keep", "review")
    assert res.location_tier == 2


def test_required_dutch_is_dropped(settings):
    job = make_job(
        title="Junior Software Developer",
        description="Fluent Dutch is required for this role. " + ENGLISH_DESC,
        location_raw="Amsterdam, Netherlands",
        country="NL",
    )
    res = evaluate(job, settings.profile)
    assert res.status == "drop"
    assert res.signals["languages_required"] == ["nl"]
    assert any(r.startswith("requires nl") for r in res.reasons)


def test_required_swedish_is_only_review(settings):
    job = make_job(
        title="Junior Software Developer",
        description="Fluent Swedish is required for this role. " + ENGLISH_DESC,
        location_raw="Helsinki, Finland",
        country="FI",
    )
    res = evaluate(job, settings.profile)
    assert res.status == "review"
    assert res.signals["languages_required"] == ["sv"]


# ------------------------------------------------------------------ location / remote


def test_remote_europe_only_is_tier_zero(settings):
    job = make_job(title="Junior Backend Developer", remote="remote",
                   remote_region="Europe Only", country=None, location_raw=None)
    res = evaluate(job, settings.profile)
    assert res.status == "keep"
    assert res.location_tier == 0
    assert res.signals["remote_region"] == "europe"


def test_remote_us_only_is_review_not_drop(settings):
    job = make_job(
        title="Junior Backend Developer",
        description="You must be located in the United States for this role. " + ENGLISH_DESC,
        remote="remote",
        country=None,
        location_raw=None,
    )
    res = evaluate(job, settings.profile)
    assert res.status == "review"
    assert res.signals["remote_region"] == "us_only"
    assert any("US-only" in r for r in res.reasons)


def test_onsite_outside_target_countries_is_dropped(settings):
    job = make_job(location_raw="Bangalore, India", country="IN", city="Bangalore")
    res = evaluate(job, settings.profile)
    assert res.status == "drop"
    assert any("outside target countries" in r for r in res.reasons)
    assert res.location_tier is None


def test_tier3_country_is_kept_with_work_rights_note(settings):
    job = make_job(location_raw="Tbilisi, Georgia", country="GE", city="Tbilisi")
    res = evaluate(job, settings.profile)
    assert res.status == "keep"
    assert res.location_tier == 3
    assert "Georgia" in res.signals["work_rights_note"]


# ------------------------------------------------------------------ role gate


@pytest.mark.parametrize("title", ["Sales Manager", "Nurse"])
def test_non_software_titles_are_dropped(settings, title):
    job = make_job(title=title, location_raw="Helsinki, Finland", country="FI")
    res = evaluate(job, settings.profile)
    assert res.status == "drop"
    assert any("non-software role" in r for r in res.reasons)


def test_data_engineer_passes_the_role_gate(settings):
    job = make_job(title="Data Engineer", location_raw="Helsinki, Finland", country="FI")
    res = evaluate(job, settings.profile)
    assert res.status != "drop"
    assert not any("software/IT role" in r for r in res.reasons)


@pytest.mark.parametrize(
    "title,country",
    [("Werkstudent Softwareentwicklung", "DE"), ("Harjoittelija, ohjelmistokehitys", "FI")],
)
def test_entry_level_compound_titles_are_kept(settings, title, country):
    job = make_job(title=title, country=country)
    res = evaluate(job, settings.profile)
    assert res.status == "keep"
    assert res.signals["seniority"] == "entry_by_title"


# ------------------------------------------------------------------ dedupe


def test_dedupe_prefers_direct_board_over_aggregator():
    linkedin = make_job(source="linkedin", source_id="li-1", title="Junior Software Developer",
                        company="Acme Oy", url="https://linkedin.test/1")
    teamtailor = make_job(source="teamtailor", source_id="tt-1",
                          title="junior software developer", company="ACME Ltd.",
                          url="https://acme.teamtailor.com/1")
    unique, dups = dedupe([linkedin, teamtailor])
    assert [j.source for j in unique] == ["teamtailor"]
    assert dups == {teamtailor.id: [linkedin.id]}


def test_dedupe_never_merges_jobs_without_a_company():
    a = make_job(source="linkedin", source_id="li-2", title="Junior Software Developer",
                 company=None)
    b = make_job(source="teamtailor", source_id="tt-2", title="Junior Software Developer",
                 company=None)
    unique, dups = dedupe([a, b])
    assert {j.id for j in unique} == {a.id, b.id}
    assert dups == {}


def test_dedupe_keeps_unrelated_jobs_apart():
    a = make_job(source="linkedin", source_id="li-3", title="Junior Software Developer",
                 company="Acme Oy")
    b = make_job(source="linkedin", source_id="li-4", title="Junior Backend Developer",
                 company="Acme Oy")
    unique, dups = dedupe([a, b])
    assert len(unique) == 2
    assert dups == {}


# ------------------------------------------------------------------ remote region


@pytest.mark.parametrize(
    "text,expected",
    [
        ("US only", "us_only"),
        ("Anywhere in the world", "worldwide"),
        ("EMEA", "europe"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_classify_remote_region(text, expected):
    assert classify_remote_region(text) == expected


def test_onsite_with_unknown_country_is_review_not_drop(settings):
    """LinkedIn rows queried as 'European Union' often carry no location at all; a junior job
    with an unknown country must reach the AI stage, not be thrown away (permissive filter)."""
    job = Job(source="linkedin", source_id=str(next(_ids)), url="https://x/1", title="Junior Full Stack Developer",
              company="Acme", description=ENGLISH_DESC, location_raw=None, country=None, remote="onsite")
    res = evaluate(job, settings.profile)
    assert res.status == "review"
    assert any("unknown" in r for r in res.reasons)
