"""Tests for :mod:`jobscraper.prior` — the free lexical prior the rank queue is ordered by.

The screen score does not sort (Spearman -0.06 against the rank score on the default profile),
so the queue's order was close to arbitrary. These tests pin the properties the prior has to
have to be worth reading: curated skills outweigh CV prose, a rare term outweighs a common one,
a long posting does not win by being long, and the whole thing is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobscraper.config import Profile
from jobscraper.models import Job
from jobscraper.prior import bm25, query_terms, queue_prior, rank_prior, tokens


def make_job(source_id: str, title: str, description: str) -> Job:
    return Job(
        source="teamtailor",
        source_id=source_id,
        url=f"https://jobs.example.test/{source_id}",
        title=title,
        company="Example Oy",
        description=description,
        location_raw="Helsinki, Finland",
        country="FI",
        posted_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


def make_profile(**kw) -> Profile:
    base = {"name": "Test Candidate", "summary": "", "cv_text": "", "skills": [], "interests": []}
    base.update(kw)
    return Profile(**base)


# ------------------------------------------------------------------------ tokenizing


def test_tokens_lower_cases_splits_and_drops_one_character_tokens():
    assert tokens("Junior Backend Developer (Python 3)") == [
        "junior", "backend", "developer", "python"]  # the bare "3" is one character


def test_tokens_keeps_tech_names_the_substitution_map_protects():
    got = tokens("C++, C#, F#, .NET, Node.js and CI/CD")
    assert got == ["cplusplus", "csharp", "fsharp", "dotnet", "nodejs", "and", "cicd"]


def test_tokens_of_empty_text_is_empty():
    assert tokens("") == [] and tokens("   ") == []


def test_a_posting_saying_c_plus_plus_matches_a_c_plus_plus_skill():
    """Without the substitution map "C++" tokenizes to "c" and is dropped as one character."""
    profile = make_profile(skills=["C++"])
    hit = make_job("hit", "Junior C++ Engineer", "Embedded work in C++ on real hardware.")
    miss = make_job("miss", "Junior Sales Engineer", "Talk to customers about our hardware.")

    scores = rank_prior([hit, miss], profile)

    assert scores[hit.id] > 0 and scores[miss.id] == 0.0


# --------------------------------------------------------------------------- the query


def test_query_terms_weight_the_sources_and_take_the_maximum_never_the_sum():
    profile = make_profile(skills=["Python"], interests=["Robotics"],
                           summary="I like python.", cv_text="python python python python")

    terms = query_terms(profile)

    assert terms["python"] == 3.0   # skills win; four repeats in the CV add nothing
    assert terms["robotics"] == 2.0
    assert terms["like"] == 1.0


def test_a_multi_word_skill_contributes_each_of_its_tokens():
    terms = query_terms(make_profile(skills=["Full Stack Open"]))
    assert terms == {"full": 3.0, "stack": 3.0, "open": 3.0}


def test_an_empty_profile_has_no_query_and_scores_every_job_zero():
    jobs = [make_job("a", "Junior Backend Developer", "Python and Go."),
            make_job("b", "Junior Frontend Developer", "React and TypeScript.")]

    assert query_terms(make_profile()) == {}
    assert rank_prior(jobs, make_profile()) == {jobs[0].id: 0.0, jobs[1].id: 0.0}


# ----------------------------------------------------------------------------- scoring


def test_a_curated_skill_outranks_a_word_that_only_appears_in_the_cv_prose():
    """Same rarity, same length: only the source weight (3.0 vs 1.0) separates the two."""
    profile = make_profile(skills=["Kubernetes"], cv_text="I enjoy photography on weekends")
    skill = make_job("skill", "Engineer", "kubernetes clusters at scale")
    prose = make_job("prose", "Engineer", "photography clusters at scale")

    scores = rank_prior([skill, prose], profile)

    assert scores[skill.id] > scores[prose.id] > 0


def test_a_rare_term_beats_a_common_one_through_the_idf():
    profile = make_profile(skills=["Rust", "Python"])
    docs = {
        "rare": "rust systems programming",
        "common-1": "python web services",
        "common-2": "python data pipelines",
        "common-3": "python api services",
    }

    scores = bm25(docs, query_terms(profile))

    assert scores["rare"] > scores["common-1"]


def test_a_longer_document_does_not_win_on_length_alone():
    query = {"python": 1.0}
    docs = {"short": "python developer", "long": "python developer " + "filler word " * 40}

    scores = bm25(docs, query)

    assert scores["short"] > scores["long"] > 0


def test_the_title_counts_twice_so_a_title_match_beats_a_body_match():
    profile = make_profile(skills=["Rust"])
    in_title = make_job("title", "Junior Rust Developer", "We write software for money.")
    in_body = make_job("body", "Junior Developer Software", "Rust is one of the tools we use.")

    scores = rank_prior([in_title, in_body], profile)

    assert scores[in_title.id] > scores[in_body.id] > 0


def test_no_term_can_push_a_score_below_zero_however_common_it_is():
    """The IDF is log(1 + ...), so a term in every document is worth little but never negative."""
    docs = {f"j{n}": "python developer" for n in range(5)}
    scores = bm25(docs, {"python": 3.0})
    assert all(score > 0 for score in scores.values())


def test_bm25_over_an_empty_document_set_or_an_empty_query_is_empty_or_zero():
    assert bm25({}, {"python": 1.0}) == {}
    assert bm25({"a": "python developer"}, {}) == {"a": 0.0}
    assert bm25({"a": "", "b": ""}, {"python": 1.0}) == {"a": 0.0, "b": 0.0}


def test_the_prior_is_deterministic_and_does_not_depend_on_the_input_order():
    profile = make_profile(skills=["Python", "Kubernetes"], interests=["Embedded"])
    jobs = [make_job("a", "Junior Python Developer", "Kubernetes, Django, PostgreSQL."),
            make_job("b", "Embedded Engineer", "C and assembly on microcontrollers."),
            make_job("c", "Junior Data Engineer", "Python pipelines and dbt.")]

    first = rank_prior(jobs, profile)

    assert first == rank_prior(jobs, profile)
    assert first == rank_prior(list(reversed(jobs)), profile)


def test_a_job_with_no_description_still_scores_on_its_title():
    profile = make_profile(skills=["Rust"])
    bare = Job(source="x", source_id="bare", url="https://e.test/1", title="Junior Rust Developer")
    other = make_job("other", "Junior Sales Lead", "Selling things to people.")

    scores = rank_prior([bare, other], profile)

    assert scores[bare.id] > 0 and scores[other.id] == 0.0


# ------------------------------------------------------------------- the config switch


def test_queue_prior_returns_none_when_the_profile_orders_by_the_screen_score():
    profile = make_profile(skills=["Python"])
    profile.ai.rank_order = "screen"
    jobs = [make_job("a", "Junior Python Developer", "Python.")]

    assert queue_prior(jobs, profile) is None


def test_queue_prior_returns_the_scores_under_the_default_setting():
    profile = make_profile(skills=["Python"])
    jobs = [make_job("a", "Junior Python Developer", "Python."),
            make_job("b", "Junior Sales Lead", "Selling.")]

    prior = queue_prior(jobs, profile)

    assert prior is not None and prior[jobs[0].id] > prior[jobs[1].id]


def test_the_module_says_why_it_exists():
    import jobscraper.prior as prior_mod

    assert prior_mod.__doc__ and "-0.06" in prior_mod.__doc__


@pytest.mark.parametrize("bad", ["Prior", "screen ", "sonnet"])
def test_the_config_field_only_accepts_the_two_orderings(bad: str):
    from pydantic import ValidationError

    from jobscraper.config import AIPolicy

    with pytest.raises(ValidationError):
        AIPolicy(rank_order=bad)
