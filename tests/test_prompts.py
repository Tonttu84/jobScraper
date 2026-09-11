"""The AI prompts are generated from the profile: no candidate is hard-coded in the code.

The owner's wording still has to come out of ``config/profile.yaml`` unchanged in substance, and
a completely different candidate (senior, Portuguese) must not inherit any of it.
"""

from __future__ import annotations

from conftest import fixture_json

from jobscraper.ai.prompts import rank_anchor_block, system_prompt
from jobscraper.config import (
    LanguagePolicy,
    LocationPolicy,
    Profile,
    PromptPolicy,
    SeniorityPolicy,
)
from jobscraper.models import AIVerdict, Job

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

#: The exact prompts a profile without deal-breakers produces. Regenerating this fixture is a
#: deliberate act: it means the wording moved, which is at least a PROMPT_VERSION bump and — when
#: the scoring scale moved with it — a reset of COMPATIBLE_PROMPT_VERSIONS that re-scores
#: everything. Last regenerated 2026-09-11 for the rank stage's absolute-scale paragraph, which
#: pins the same bands down harder instead of changing them, so the old verdicts stayed in use.
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


# --- the absolute scale ---------------------------------------------------------------------
# Owner, 2026-09-11: a rank window is a slice of a queue, not a shortlist. Scoring it relatively
# corrupts the stop rule — a weak window inflates a false entrant, a strong one hides real ones.


def test_the_rank_prompt_scores_on_a_fixed_scale(settings):
    rank = system_prompt("rank", settings.profile)
    assert "Score on a fixed scale, not against the other postings you see." in rank
    assert "A slice of weak postings has no 80s; a slice of strong ones may have ten." in rank
    assert "When REFERENCE SCORES are given" in rank


def test_the_refine_prompt_keeps_the_fixed_scale_head_and_its_own_tail(settings):
    """Both stages share the head on purpose: refine *is* the "separate, later pass"."""
    refine = system_prompt("refine", settings.profile)
    assert refine.index("Score on a fixed scale") < refine.index("REFINEMENT PASS")
    assert refine.rstrip().endswith("not a description of the job.")


def _anchor_job(source_id: str, title: str, **kw) -> Job:
    base = {"source": "teamtailor", "source_id": source_id,
            "url": f"https://jobs.example.test/{source_id}", "title": title,
            "company": "Example Oy", "location_raw": "Helsinki, Finland", "country": "FI"}
    base.update(kw)
    return Job(**base)


def _anchor_verdict(job: Job, score: int, summary: str) -> AIVerdict:
    return AIVerdict(job_id=job.id, stage="rank", model="claude-opus-5", prompt_version="v1",
                     relevant=True, score=score, language_ok=True, seniority_ok=True,
                     location_ok=True, summary=summary)


def test_no_anchors_means_no_reference_block():
    assert rank_anchor_block([]) == ""


def test_the_reference_block_is_one_line_per_anchor():
    strong = _anchor_job("a", "Junior Go Developer")
    weak = _anchor_job("b", "Salesforce Consultant", company=None, location_raw=None, country=None)
    block = rank_anchor_block([(strong, _anchor_verdict(strong, 85, "Core stack, junior level.")),
                               (weak, _anchor_verdict(weak, 38, "Different job entirely."))])
    assert block == (
        "REFERENCE SCORES (fixed; not part of your task — do not return these)\n"
        "Earlier postings scored for this candidate, so every batch is graded on the same scale. "
        "Score\nthe new postings against this scale, not against each other.\n"
        "- Junior Go Developer · Example Oy · Helsinki, Finland · score 85 · "
        "Core stack, junior level.\n"
        "- Salesforce Consultant · unknown · unknown · score 38 · Different job entirely.\n"
        "\n"
    )
