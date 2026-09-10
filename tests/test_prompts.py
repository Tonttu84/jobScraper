"""The AI prompts are generated from the profile: no candidate is hard-coded in the code.

The owner's wording still has to come out of ``config/profile.yaml`` unchanged in substance, and
a completely different candidate (senior, Portuguese) must not inherit any of it.
"""

from __future__ import annotations

from conftest import fixture_json

from jobscraper.ai.prompts import system_prompt
from jobscraper.config import (
    LanguagePolicy,
    LocationPolicy,
    Profile,
    PromptPolicy,
    SeniorityPolicy,
)

# "internship" is deliberately absent: it survives in the generic employment-type bullet,
# which is about contract shape, not about seniority.
JUNIOR_WORDING = ["junior", "trainee", "graduate", "entry-level", "42/Hive",
                  "Finnish", "German", "Dubai", "US salaries"]


def senior_profile() -> Profile:
    """A second candidate with nothing in common with the owner."""
    return Profile(
        name="Ana Ribeiro",
        summary="Senior C++ developer in Lisbon, 12 years of systems and trading work.",
        cv_text="Senior C++ developer. Low-latency systems, Linux, CMake.",
        skills=["C++", "Rust", "Linux"],
        interests=["low latency", "compilers"],
        languages=LanguagePolicy(ok=["pt", "en"], weak=["es"], drop_if_written_in=["de"]),
        seniority=SeniorityPolicy(max_years_keep=15, max_years_review=20,
                                  drop_title_terms=["junior", "intern"],
                                  keep_title_terms=["senior", "principal", "staff"]),
        location=LocationPolicy(tier1=["PT"], tier2=["ES", "IE"], tier3=["BR"], keep_all_remote=True),
        prompt=PromptPolicy(
            role_label="senior C++ developer",
            seniority_rule="ONLY senior, staff, principal or lead roles; at least "
                           "{max_years_keep} years of experience is expected.",
            role_rule="systems, embedded or backend C++ work.",
            extra_rules=["Trading and telecom domains are a plus."],
        ),
    )


def test_a_senior_profile_gets_its_own_prompt():
    text = system_prompt("prefilter", senior_profile())
    assert "You are screening job postings for a specific senior C++ developer." in text
    assert "ONLY senior, staff, principal or lead roles; at least 15 years" in text
    assert "systems, embedded or backend C++ work." in text
    assert "Portuguese or English" in text  # languages.ok, English names, generated
    assert "Spanish" in text  # languages.weak
    assert "tier 1 = PT" in text and "tier 2 = ES, IE" in text and "tier 3 = BR" in text
    assert "Trading and telecom domains are a plus." in text
    for junk in JUNIOR_WORDING:
        assert junk not in text, junk


def test_the_rank_prompt_uses_the_role_label_too():
    text = system_prompt("rank", senior_profile())
    assert "ranking job postings for a specific senior C++ developer" in text
    assert "FULL CV" in text and "low-latency systems" in text.lower()
    for junk in JUNIOR_WORDING:
        assert junk not in text, junk


def test_a_profile_without_a_prompt_section_still_reads_sensibly():
    bare = Profile(name="Nobody", summary="Someone who writes software.")
    text = system_prompt("prefilter", bare)
    assert "for a specific software developer" in text
    assert "junior" not in text.lower()
    assert "Languages: the posting's working language must be English, Finnish or German" in text
    assert "tier 1 = FI, EE" in text  # LocationPolicy defaults
    assert "Employment type: anything" in text


def test_the_owners_wording_survives_the_move_into_yaml(settings):
    text = system_prompt("prefilter", settings.profile)
    assert "for a specific junior software developer" in text
    assert "ONLY internship, trainee, graduate, junior, entry-level" in text
    assert "at most 2 years of experience" in text  # {max_years_keep} interpolated
    assert "especially keen on Go and Rust" in text
    assert "42/Hive" in text and "US salaries" in text
    assert "must be English, Finnish or German" in text
    assert "tier 1 = FI, EE" in text
    assert "AE" in text and "(work rights via employer)" in text
    rank = system_prompt("rank", settings.profile)
    assert "FULL CV" in rank and "Hive" in rank


def test_no_weak_languages_means_no_weak_line():
    profile = senior_profile()
    assert "Weak: es" in system_prompt("prefilter", profile)
    profile.languages.weak = []
    text = system_prompt("prefilter", profile)
    assert "Weak:" not in text
    assert "Working languages: pt, en (fluent)." in text


# --- deal-breakers -------------------------------------------------------------------------

#: The exact prompts a profile without deal-breakers produced before the feature existed.
#: Regenerating this fixture invalidates every cached verdict, so it has to be a deliberate act.
BASELINE_PROFILE = dict(name="Nobody", summary="Someone who writes software.", cv_text="A CV.")

DEAL_BREAKERS = [
    "consultancy / staffing placements where I am hired out to clients",
    "on-call rotations",
    "B2B / self-employed contract only",
    "more than 25% travel",
]


def test_deal_breakers_default_to_an_empty_list():
    assert Profile(name="Nobody", summary="Someone who writes software.").deal_breakers == []


def test_the_repo_profile_loads_with_no_deal_breakers(settings):
    assert settings.profile.deal_breakers == []


def test_an_empty_deal_breaker_list_leaves_both_prompts_byte_for_byte_unchanged():
    baseline = fixture_json("prompts_baseline.json")
    profile = Profile(**BASELINE_PROFILE)
    assert profile.deal_breakers == []
    assert system_prompt("prefilter", profile) == baseline["prefilter"]
    assert system_prompt("rank", profile) == baseline["rank"]
    for stage in ("prefilter", "rank"):
        assert "DEAL-BREAKER" not in system_prompt(stage, profile).upper()


def test_deal_breakers_reach_both_stages_with_stage_specific_instructions():
    profile = Profile(**BASELINE_PROFILE, deal_breakers=DEAL_BREAKERS)
    pre = system_prompt("prefilter", profile)
    rank = system_prompt("rank", profile)
    for text in (pre, rank):
        assert "DEAL-BREAKERS (automatic reject)" in text
        for item in DEAL_BREAKERS:
            assert item in text, item
    assert "set relevant=false and score 0 and name the deal-breaker in concerns" in pre
    assert "keep it and name the suspicion in concerns so the ranker checks it" in pre
    assert "cap the score" not in pre
    assert "score 0-10 regardless of other fit" in rank
    assert "cap the score at 40" in rank
    assert "relevant=false" not in rank


def test_blank_deal_breakers_are_ignored():
    profile = Profile(**BASELINE_PROFILE, deal_breakers=["  ", "on-call rotations"])
    text = system_prompt("prefilter", profile)
    assert "- on-call rotations" in text
    assert "- \n" not in text and "-   \n" not in text
