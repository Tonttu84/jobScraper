"""Tests for :mod:`jobscraper.filters.rules` against the real ``config/profile.yaml``.

The filter is deliberately permissive: it may only ``drop`` on strong signals, everything
softer must become ``review`` so the AI stage still sees it.
"""

from __future__ import annotations

import itertools
from datetime import date

import pytest

from jobscraper.config import LocationPolicy, Profile, SeniorityPolicy
from jobscraper.filters.deadline import find_deadline
from jobscraper.filters.evergreen import CUES, is_evergreen
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
    assert res.signals["seniority"] == "kept_by_title"


def test_bare_junior_developer_title_passes_role_gate(settings):
    job = make_job(title="Junior Developer", location_raw="Helsinki, Finland", country="FI")
    res = evaluate(job, settings.profile)
    assert res.status == "keep"
    assert res.location_tier == 1
    assert res.signals["seniority"] == "kept_by_title"


def test_senior_title_is_dropped(settings):
    job = make_job(title="Senior Software Engineer", location_raw="Berlin, Germany", country="DE")
    res = evaluate(job, settings.profile)
    assert res.status == "drop"
    assert any("senior" in r.lower() for r in res.reasons)
    assert res.signals["seniority"] == "excluded_by_title"


def test_three_years_experience_is_dropped(settings):
    """Owner (2026-09-07): only trainee/intern/junior roles; an unlabelled title asking 3+ years is out."""
    job = make_job(
        title="Software Developer",
        description="We are looking for someone with 3+ years of experience in web development. "
        + ENGLISH_DESC,
        location_raw="Tallinn, Estonia",
        country="EE",
    )
    res = evaluate(job, settings.profile)
    assert res.status == "drop"
    assert res.signals["years_required"] == 3


def test_junior_title_asking_three_years_is_only_review(settings):
    job = make_job(title="Junior Software Developer",
                   description="3+ years of experience with Python. " + ENGLISH_DESC)
    assert evaluate(job, settings.profile).status == "review"


@pytest.mark.parametrize("title", ["Mid-level Backend Developer", "Medior Java Developer",
                                   "Experienced Software Developer", "Mid Software Engineer",
                                   "Kokenut ohjelmistokehittäjä", "Intermediate Frontend Developer"])
def test_mid_level_titles_are_dropped(settings, title):
    res = evaluate(make_job(title=title), settings.profile)
    assert res.status == "drop", title
    assert any("title" in r for r in res.reasons)


def test_middleware_is_not_mid_level(settings):
    res = evaluate(make_job(title="Software Engineer - Platform & Middleware (Early Career)"), settings.profile)
    assert res.status != "drop"


def test_mid_seniority_label_drops_unlabelled_title(settings):
    """Boards label seniority separately (justjoin 'mid', nofluffjobs 'Mid', devitjobs 'Regular')."""
    res = evaluate(make_job(title="Backend Developer", seniority_raw="Mid"), settings.profile)
    assert res.status == "drop"
    assert any("label" in r for r in res.reasons)
    assert evaluate(make_job(title="Backend Developer", seniority_raw="Trainee, Junior"), settings.profile).status != "drop"
    assert evaluate(make_job(title="Junior Backend Developer", seniority_raw="Mid"), settings.profile).status != "drop"


def test_a_senior_profile_inverts_the_seniority_terms(settings):
    """Nothing in the code assumes a junior: the terms come from the profile (config.py)."""
    senior = settings.profile.model_copy(update={
        "seniority": SeniorityPolicy(max_years_keep=15, max_years_review=20,
                                     drop_title_terms=["junior", "intern", "trainee", "graduate"],
                                     keep_title_terms=["senior", "principal", "staff"],
                                     drop_label_terms=["junior", "trainee"]),
    })
    junior = evaluate(make_job(title="Junior Developer", country="FI"), senior)
    assert junior.status == "drop"
    assert junior.signals["seniority"] == "excluded_by_title"
    assert any("excluded by profile" in r for r in junior.reasons)

    kept = evaluate(make_job(title="Senior C++ Engineer", country="FI"), senior)
    assert kept.status == "keep"
    assert kept.signals["seniority"] == "kept_by_title"

    labelled = evaluate(make_job(title="C++ Engineer", seniority_raw="Junior", country="FI"), senior)
    assert labelled.status == "drop"
    assert any("label" in r and "excluded by profile" in r for r in labelled.reasons)
    # keep_title_terms still win over an excluded label, as they do for the owner's profile
    assert evaluate(make_job(title="Senior C++ Engineer", seniority_raw="Junior", country="FI"),
                    senior).status != "drop"


def test_seniority_signal_names_are_policy_neutral(settings):
    """The signal reaches the AI prompt verbatim, so it must not name a seniority level.

    A senior profile keeps "Senior C++ Engineer" via ``keep_title_terms``; calling that
    ``entry_by_title`` told the AI stage the opposite of the truth.
    """
    senior = settings.profile.model_copy(update={
        "seniority": SeniorityPolicy(max_years_keep=15, max_years_review=20,
                                     drop_title_terms=["junior", "intern", "trainee"],
                                     keep_title_terms=["senior"],
                                     drop_label_terms=["junior"]),
    })
    assert evaluate(make_job(title="Senior C++ Engineer", country="FI"),
                    senior).signals["seniority"] == "kept_by_title"
    assert evaluate(make_job(title="Junior C++ Engineer", country="FI"),
                    senior).signals["seniority"] == "excluded_by_title"
    assert evaluate(make_job(title="C++ Engineer", seniority_raw="Junior", country="FI"),
                    senior).signals["seniority"] == "excluded_by_label"

    # ... and the owner's junior policy reports the same names for the mirror-image cases.
    junior = settings.profile
    assert evaluate(make_job(title="Junior C++ Engineer", country="FI"),
                    junior).signals["seniority"] == "kept_by_title"
    assert evaluate(make_job(title="Senior C++ Engineer", country="FI"),
                    junior).signals["seniority"] == "excluded_by_title"
    assert evaluate(make_job(title="C++ Engineer", seniority_raw="Mid", country="FI"),
                    junior).signals["seniority"] == "excluded_by_label"
    assert evaluate(make_job(title="C++ Engineer", country="FI"),
                    junior).signals["seniority"] == "unlabeled"


def test_empty_drop_label_terms_never_drop_on_a_label():
    """The default policy has no label terms at all: no board label can drop a job by itself."""
    profile = Profile(name="X", summary="s")
    res = evaluate(make_job(title="Backend Developer", seniority_raw="Mid", country="FI"), profile)
    assert res.status != "drop"


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
    assert res.signals["seniority"] == "kept_by_title"


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


# --------------------------------------------------- dedupe: location preference

DEDUPE_LOCATION = LocationPolicy(tier1=["FI"], tier2=["SE"], tier3=["DE"])


@pytest.mark.parametrize("better,worse", [("FI", "SE"), ("SE", "DE"), ("DE", "US"), (None, "US")])
def test_dedupe_prefers_the_better_located_copy(better, worse):
    # The better-located copy is on the *worse* source, so only the location can decide it.
    good = make_job(source="linkedin", source_id=f"li-{better}", company="Acme Oy", country=better)
    bad = make_job(source="teamtailor", source_id=f"tt-{worse}", company="Acme Oy", country=worse)
    unique, dups = dedupe([bad, good], DEDUPE_LOCATION)
    assert [j.id for j in unique] == [good.id]
    assert dups == {good.id: [bad.id]}


def test_dedupe_within_the_same_tier_still_follows_source_priority():
    linkedin = make_job(source="linkedin", source_id="li-fi", company="Acme Oy", country="FI")
    teamtailor = make_job(source="teamtailor", source_id="tt-fi", company="Acme Oy", country="FI")
    unique, dups = dedupe([linkedin, teamtailor], DEDUPE_LOCATION)
    assert [j.id for j in unique] == [teamtailor.id]
    assert dups == {teamtailor.id: [linkedin.id]}


def test_dedupe_matches_tier_countries_case_insensitively():
    lowercase = LocationPolicy(tier1=["fi"], tier2=[], tier3=[])
    fi = make_job(source="linkedin", source_id="li-lc", company="Acme Oy", country="FI")
    us = make_job(source="teamtailor", source_id="tt-lc", company="Acme Oy", country="US")
    unique, _ = dedupe([us, fi], lowercase)
    assert [j.id for j in unique] == [fi.id]


def test_dedupe_prefers_a_remote_copy_when_both_are_out_of_tier():
    remote = make_job(source="linkedin", source_id="li-rem", company="Acme Oy", country="US",
                      remote="remote")
    onsite = make_job(source="teamtailor", source_id="tt-ons", company="Acme Oy", country="US")
    unique, dups = dedupe([onsite, remote], DEDUPE_LOCATION)
    assert [j.id for j in unique] == [remote.id]
    assert dups == {remote.id: [onsite.id]}


def test_dedupe_ignores_remote_when_the_profile_does_not_keep_all_remote():
    strict = LocationPolicy(tier1=["FI"], tier2=[], tier3=[], keep_all_remote=False)
    remote = make_job(source="linkedin", source_id="li-rem2", company="Acme Oy", country="US",
                      remote="remote")
    onsite = make_job(source="teamtailor", source_id="tt-ons2", company="Acme Oy", country="US")
    unique, _ = dedupe([remote, onsite], strict)
    assert [j.id for j in unique] == [onsite.id]


def test_dedupe_without_a_location_keeps_the_source_ordering():
    us = make_job(source="teamtailor", source_id="tt-us", company="Acme Oy", country="US")
    fi = make_job(source="linkedin", source_id="li-fi2", company="Acme Oy", country="FI")
    unique, dups = dedupe([fi, us])
    assert [j.id for j in unique] == [us.id]
    assert dups == {us.id: [fi.id]}


def test_dedupe_lists_every_loser_under_the_winner():
    fi = make_job(source="linkedin", source_id="li-multi", company="Acme Oy", country="FI")
    us = make_job(source="teamtailor", source_id="tt-multi", company="Acme Oy", country="US")
    ie = make_job(source="duunitori", source_id="du-multi", company="Acme Oy", country="IE")
    unique, dups = dedupe([us, ie, fi], DEDUPE_LOCATION)
    assert [j.id for j in unique] == [fi.id]
    # Losers keep the same ordering: teamtailor outranks duunitori once both are out of tier.
    assert dups == {fi.id: [us.id, ie.id]}


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


def test_remote_restricted_to_a_non_target_country_is_review(settings):
    """devitjobs.uk marks many jobs 'remote' with remote_region 'GB only': remote in name, but
    closed to applicants outside the UK. Permissive filter → review, not keep, not drop."""
    job = Job(source="devitjobs", source_id=str(next(_ids)), url="https://x/1", title="Junior Software Developer",
              company="Acme", description=ENGLISH_DESC, location_raw="London, GB", country="GB", remote="remote",
              remote_region="GB only")
    res = evaluate(job, settings.profile)
    assert res.status == "review"
    assert any("GB" in r for r in res.reasons)
    assert res.signals["remote_region"] == "country_only:GB"


def test_remote_restricted_to_a_target_country_is_kept(settings):
    job = Job(source="devitjobs", source_id=str(next(_ids)), url="https://x/2", title="Junior Software Developer",
              company="Acme", description=ENGLISH_DESC, location_raw="Helsinki, FI", country="FI", remote="remote",
              remote_region="FI only")
    res = evaluate(job, settings.profile)
    assert res.status == "keep"


def test_stale_postings_are_dropped(settings):
    """Owner: kill anything too old before the AI passes. Unknown dates stay (permissive)."""
    from datetime import UTC, datetime, timedelta

    old = make_job(posted_at=datetime.now(UTC) - timedelta(days=settings.profile.max_age_days + 5))
    res = evaluate(old, settings.profile)
    assert res.status == "drop"
    assert any("days old" in r for r in res.reasons)

    fresh = make_job(posted_at=datetime.now(UTC) - timedelta(days=3))
    assert evaluate(fresh, settings.profile).status != "drop"

    undated = make_job(posted_at=None)
    assert evaluate(undated, settings.profile).status != "drop"


def test_stale_check_accepts_naive_datetimes(settings):
    """SQLite hands back naive datetimes; the age check must not raise on them."""
    from datetime import datetime, timedelta

    old = make_job(posted_at=datetime.now() - timedelta(days=200))  # naive on purpose
    assert evaluate(old, settings.profile).status == "drop"


def test_a_year_count_is_only_read_next_to_experience_words(settings):
    """"founded 5 years ago" is company history, and "25 years" is a typo, not a requirement."""
    history = make_job(
        title="Junior Software Developer",
        country="FI",
        description="The company was founded 5 years ago in Helsinki. " + ENGLISH_DESC,
    )
    res = evaluate(history, settings.profile)
    assert "years_required" not in res.signals
    assert res.status == "keep"

    absurd = make_job(
        title="Junior Software Developer",
        country="FI",
        description="We ask for 25 years of experience with our stack. " + ENGLISH_DESC,
    )
    assert "years_required" not in evaluate(absurd, settings.profile).signals

    within = make_job(
        title="Junior Software Developer",
        country="FI",
        description="You have up to 2 years of experience with web development. " + ENGLISH_DESC,
    )
    res = evaluate(within, settings.profile)
    assert res.signals["years_required"] == 2 and res.status == "keep"


def test_a_year_count_between_the_two_thresholds_is_review_not_drop(settings):
    """A profile that keeps up to 2 years but reviews up to 5: a 4-year ad is worth a look."""
    lenient = settings.profile.model_copy(update={
        "seniority": SeniorityPolicy(
            max_years_keep=2,
            max_years_review=5,
            drop_title_terms=settings.profile.seniority.drop_title_terms,
            keep_title_terms=settings.profile.seniority.keep_title_terms,
            drop_label_terms=settings.profile.seniority.drop_label_terms,
        ),
    })
    job = make_job(
        title="Software Developer",
        country="FI",
        description="We expect 4 years of experience with backend development. " + ENGLISH_DESC,
    )
    res = evaluate(job, lenient)
    assert res.status == "review"
    assert res.signals["years_required"] == 4
    assert any("4 years of experience" in r for r in res.reasons)


def test_a_title_the_role_gate_does_not_recognize(settings):
    """The description gets one chance; without it the posting is not a software job at all."""
    rescued = make_job(title="Junior Analyst", country="FI", description=ENGLISH_DESC)
    res = evaluate(rescued, settings.profile)
    assert res.status == "review"
    assert any("description mentions it" in r for r in res.reasons)

    dropped = make_job(
        title="Junior Analyst",
        country="FI",
        description="You will read spreadsheets and write summaries for the management team.",
    )
    res = evaluate(dropped, settings.profile)
    assert res.status == "drop"
    assert any("not a software/IT role" in r for r in res.reasons)


def test_a_posting_too_short_to_have_a_language(settings):
    job = make_job(title="Junior Developer", country="FI", description=None)
    res = evaluate(job, settings.profile)
    assert "posting_language" not in res.signals
    assert res.status == "keep"


def test_a_posting_in_a_weak_language_is_reviewed_not_dropped(settings):
    """Swedish is on the profile's ``weak`` list: readable enough to let a human decide."""
    swedish = (
        "Vi söker en junior systemutvecklare till vårt team i Stockholm som vill lära sig mer "
        "om moderna webbtjänster. Du kommer att arbeta tillsammans med erfarna kollegor och "
        "utveckla nya funktioner i våra produkter. Vi erbjuder flexibla arbetstider och en "
        "trevlig arbetsmiljö."
    )
    job = make_job(title="Junior Developer", country="SE", description=swedish)
    res = evaluate(job, settings.profile)
    assert res.signals["posting_language"] == "sv"
    assert res.status == "review"
    assert any("written in sv" in r for r in res.reasons)


def test_an_only_clause_that_names_no_country_says_nothing():
    assert classify_remote_region("Full time only") == "unknown"


def test_a_job_with_neither_a_country_nor_a_remote_flag_is_reviewed(settings):
    job = make_job(title="Junior Developer", country=None, location_raw=None, remote="unknown")
    res = evaluate(job, settings.profile)
    assert res.status == "review"
    assert "location unknown" in res.reasons


# ------------------------------------------------------------------ application deadlines

TODAY = date(2026, 9, 10)


@pytest.mark.parametrize("text", [
    # English
    "Apply by 13 September 2026 to be considered.",
    "Application deadline: 13 September 2026.",
    "The deadline is 13 September 2026.",
    "Closing date 13 September 2026.",
    "Applications close on 13 September 2026.",
    "The vacancy closes on 13 September 2026.",
    "The last day to apply is 13 September 2026.",
    # Finnish
    "Hakuaika päättyy 13.9.2026 klo 23.59.",
    "Viimeinen hakupäivä 13.9.2026.",
    "Hae viimeistään 13.9.2026.",
    "Hakemukset viimeistään 13.9.2026.",
    # German
    "Bewerbungsfrist: 13. September 2026.",
    "Bitte bewerben Sie sich bis 13. September 2026.",
    "Bewerbungsschluss 13.09.2026.",
    "Bitte bewerben Sie sich bis zum 13. September 2026.",
    # Portuguese
    "Candidaturas até 13 de setembro de 2026.",
    "Prazo de candidatura: 13 de setembro de 2026.",
    # Swedish
    "Sista ansökningsdag 13 september 2026.",
    "Ansök senast 13 september 2026.",
])
def test_every_deadline_cue_language_is_read(text: str) -> None:
    assert find_deadline(text, TODAY) == date(2026, 9, 13)


@pytest.mark.parametrize("stamp", [
    "13 September 2026", "September 13, 2026", "13.9.2026", "13.09.2026",
    "13/09/2026", "2026-09-13", "13 Sep 2026", "13 Sept 2026",
    "13 syyskuuta 2026", "13 syyskuu 2026", "13. September 2026",
    "13 de setembro de 2026", "13 september 2026",
])
def test_every_deadline_date_format_is_parsed(stamp: str) -> None:
    assert find_deadline(f"Application deadline: {stamp}.", TODAY) == date(2026, 9, 13)


def test_a_deadline_without_a_year_takes_the_current_one() -> None:
    assert find_deadline("Apply by 13 September.", TODAY) == date(2026, 9, 13)


def test_a_deadline_without_a_year_just_behind_us_stays_in_this_year() -> None:
    """Nine days past is a closed vacancy, not next year's round: the drop rule should see it."""
    assert find_deadline("Apply by 1 September.", TODAY) == date(2026, 9, 1)


def test_a_deadline_without_a_year_long_past_rolls_over_to_next_year() -> None:
    assert find_deadline("Apply by 5 January.", TODAY) == date(2027, 1, 5)


@pytest.mark.parametrize("text", [
    "Application deadline: 13 September 2035.",  # too far ahead to be this posting's
    "Application deadline: 13 September 2020.",  # more than a year behind us
])
def test_an_absurd_deadline_is_ignored(text: str) -> None:
    assert find_deadline(text, TODAY) is None


def test_an_impossible_date_is_ignored() -> None:
    assert find_deadline("Application deadline: 31 February 2027.", TODAY) is None
    assert find_deadline("Application deadline: 31 February.", TODAY) is None


def test_a_leap_day_that_next_year_does_not_have_is_ignored() -> None:
    """29 February, long past, and rolling it over one year would invent a day that never comes."""
    assert find_deadline("Apply by 29 February.", date(2028, 12, 1)) is None


@pytest.mark.parametrize("text", [
    "We work in a fast-paced, deadline-driven environment.",
    "You will own the deadline for the release train.",
    "Deadlines matter here.",
    "",
])
def test_a_deadline_word_without_a_date_matches_nothing(text: str) -> None:
    assert find_deadline(text, TODAY) is None


def test_a_date_without_a_cue_is_not_a_deadline() -> None:
    assert find_deadline("The team offsite is on 13 September 2026.", TODAY) is None


def test_a_date_too_far_after_the_cue_is_not_read() -> None:
    filler = "x" * 120
    assert find_deadline(f"Application deadline{filler}13 September 2026", TODAY) is None


def test_the_first_readable_deadline_wins() -> None:
    text = "Deadline for the demo is Friday. Apply by 13 September 2026, no exceptions."
    assert find_deadline(text, TODAY) == date(2026, 9, 13)


def test_a_month_and_year_without_a_day_is_not_a_date() -> None:
    assert find_deadline("Application deadline: September 2026.", TODAY) is None


def test_a_passed_deadline_is_dropped(settings):
    job = make_job(location_raw="Helsinki, Finland", country="FI",
                   description=ENGLISH_DESC + " Application deadline: 1 September 2026.")
    res = evaluate(job, settings.profile, today=TODAY)
    assert res.status == "drop"
    assert "application deadline passed on 2026-09-01" in res.reasons
    assert res.signals["deadline"] == "2026-09-01"
    assert "closes_in_days" not in res.signals


def test_an_open_deadline_is_kept_and_counted(settings):
    job = make_job(location_raw="Helsinki, Finland", country="FI",
                   description=ENGLISH_DESC + " Application deadline: 13 September 2026.")
    res = evaluate(job, settings.profile, today=TODAY)
    assert res.status == "keep"
    assert res.signals["deadline"] == "2026-09-13"
    assert res.signals["closes_in_days"] == 3


def test_a_deadline_today_closes_in_zero_days(settings):
    job = make_job(location_raw="Helsinki, Finland", country="FI",
                   description=ENGLISH_DESC + " Application deadline: 10 September 2026.")
    res = evaluate(job, settings.profile, today=TODAY)
    assert res.status == "keep"
    assert res.signals["closes_in_days"] == 0


def test_a_posting_without_a_deadline_carries_no_deadline_signal(settings):
    job = make_job(location_raw="Helsinki, Finland", country="FI")
    res = evaluate(job, settings.profile, today=TODAY)
    assert "deadline" not in res.signals
    assert "closes_in_days" not in res.signals


def test_evaluate_defaults_to_the_real_today(settings):
    """Without an explicit date the rule uses the clock, so the CLI needs no extra argument."""
    ahead = date.today().replace(year=date.today().year + 1)
    job = make_job(location_raw="Helsinki, Finland", country="FI",
                   description=ENGLISH_DESC + f" Application deadline: {ahead.isoformat()}.")
    res = evaluate(job, settings.profile)
    assert res.signals["deadline"] == ahead.isoformat()
    assert res.signals["closes_in_days"] > 0


# ------------------------------------------------------------------ evergreen adverts


@pytest.mark.parametrize("cue", CUES)
def test_every_evergreen_cue_is_recognised(cue: str) -> None:
    """Each cue, dropped into a sentence, reports itself back as the evidence."""
    assert is_evergreen("Software Engineer", f"About the role. {cue} in our team.") == cue


def test_the_cue_is_reported_in_its_canonical_spelling() -> None:
    """Boards shout and wrap lines; the signal has to stay a small, stable vocabulary."""
    assert is_evergreen("x", "We keep a TALENT\n  POOL of graduates.") == "talent pool"
    assert is_evergreen("x", "Bitte senden Sie eine initiativbewerbung.") == "Initiativbewerbung"


def test_a_cue_in_the_title_is_enough() -> None:
    title = "Register Your Interest – Graduates"
    assert is_evergreen(title, f"{title}\nWe hire graduates every autumn.") \
        == "register your interest"


def test_the_title_decides_when_the_body_names_another_cue() -> None:
    """A long description would otherwise pick the cue; the heading is the whole story."""
    assert is_evergreen("Register Your Interest", "We are building a talent pool.") \
        == "register your interest"


@pytest.mark.parametrize("text", [
    "You will join a talented team of engineers.",
    "Great career opportunities and a clear growth path.",
    "We offer real opportunities to grow your talent.",
    "The talent acquisition team will contact you.",
    "",
])
def test_ordinary_recruiting_words_are_not_evergreen(text: str) -> None:
    assert is_evergreen("Junior Software Developer", text) is None


def test_an_evergreen_advert_is_signalled_but_never_dropped(settings):
    """Not a live vacancy is a concern for the AI stages, not a verdict the rules may pass."""
    job = make_job(location_raw="Helsinki, Finland", country="FI",
                   description=ENGLISH_DESC + " This posting is to advertise potential job "
                                              "opportunities at our company.")
    res = evaluate(job, settings.profile, today=TODAY)
    assert res.status == "keep"
    assert res.signals["evergreen"] == "advertise potential job opportunities"


def test_an_evergreen_title_signals_on_a_posting_the_rules_send_to_review(settings):
    """The signal rides along whatever else the rules made of the posting."""
    job = make_job(title="Register Your Interest – Graduate Software Engineer",
                   location_raw="Helsinki, Finland", country="FI",
                   description=ENGLISH_DESC + " We ask for at least 5 years of experience.")
    res = evaluate(job, settings.profile, today=TODAY)
    assert res.status == "review"
    assert res.signals["evergreen"] == "register your interest"


def test_an_ordinary_posting_carries_no_evergreen_signal(settings):
    job = make_job(location_raw="Helsinki, Finland", country="FI")
    assert "evergreen" not in evaluate(job, settings.profile, today=TODAY).signals
