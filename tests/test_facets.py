"""Tests for :mod:`jobscraper.facets`.

Facets are deterministic: no AI, no network. They answer two questions a web UI needs to
filter on — which natural languages a posting needs, and which tech stacks it touches
(in particular whether it is Full Stack Open style web work).
"""

from __future__ import annotations

import pytest

from jobscraper.facets import (
    FACETS_VERSION,
    STACKS,
    WEB_STACKS,
    compute_all,
    compute_facets,
    detect_stacks,
)
from jobscraper.models import FilterResult, Job, JobFacets

# An English paragraph comfortably over the 80-character floor detect_language() needs.
ENGLISH_PARAGRAPH = (
    "We are looking for a junior software developer to join our platform team in Helsinki. "
    "You will work with TypeScript, React and Node.js and help us build reliable services "
    "for our customers. We offer mentoring, a flexible hybrid setup and a friendly team."
)


def make_job(
    title: str = "Junior Developer",
    description: str | None = None,
    *,
    source_id: str = "1",
    tags: list[str] | None = None,
    posting_language: str | None = None,
) -> Job:
    return Job(
        source="test",
        source_id=source_id,
        url=f"https://x/{source_id}",
        title=title,
        description=description,
        tags=tags or [],
        posting_language=posting_language,
    )


# --------------------------------------------------------------------------- stacks

# One representative phrase per stack tag. The keys must cover STACKS exactly.
STACK_PHRASES: dict[str, str] = {
    "web": "You will build a React frontend against a Node.js and GraphQL backend.",
    "c_cpp": "Strong C++ skills and experience with embedded C are required.",
    "python": "We write Python and Django services for our customers.",
    "go": "We are hiring a Go developer for our backend team.",
    "rust": "Our new services are written in Rust.",
    "java": "Java and Spring Boot experience, Kotlin is a plus.",
    "dotnet": "Our platform runs on C# and .NET, mostly ASP.NET web APIs.",
    "mobile": "You will ship Android and iOS apps built with Flutter.",
    "embedded": "You will write firmware for microcontrollers, RTOS experience helps.",
    "devops": "Kubernetes, Docker, Terraform and AWS are our daily tools.",
    "data": "You will work with SQL, pandas and machine learning pipelines.",
    "game": "Unity gameplay programmer for an unannounced title.",
    "qa": "Test automation with Playwright and Cypress, plus manual QA.",
    "php": "We maintain a large Laravel and PHP codebase.",
    "ruby": "Our monolith is Ruby on Rails.",
}


def test_stack_phrases_cover_every_tag() -> None:
    assert set(STACK_PHRASES) == set(STACKS)


@pytest.mark.parametrize("tag", sorted(STACK_PHRASES))
def test_each_stack_tag_is_matched_by_its_phrase(tag: str) -> None:
    assert tag in detect_stacks(STACK_PHRASES[tag])


def test_detect_stacks_is_sorted_and_deduplicated() -> None:
    text = "React and Python and React again, plus Django and TypeScript."
    stacks = detect_stacks(text)
    assert stacks == sorted(stacks)
    assert len(stacks) == len(set(stacks))
    assert stacks == ["python", "web"]


def test_detect_stacks_empty_text() -> None:
    assert detect_stacks("") == []


def test_detect_stacks_can_return_several_tags() -> None:
    text = "Full-stack engineer: React, Node.js, PostgreSQL, Docker and Kubernetes."
    assert set(detect_stacks(text)) >= {"web", "data", "devops"}


# ------------------------------------------------------------- stack negative cases


@pytest.mark.parametrize(
    "text",
    [
        "Benefits include free fruit and Vitamin C supplements.",
        "This is a C-level executive assistant position.",
        "If that fails we always have a plan C ready.",
        "Great communication skills and a can-do attitude.",
    ],
)
def test_bare_c_is_not_the_c_language(text: str) -> None:
    assert "c_cpp" not in detect_stacks(text)


@pytest.mark.parametrize(
    "text",
    [
        "You will write code in C every day.",
        "Solid C programming experience is expected.",
        "We look for a C developer with an interest in low level work.",
        "Our stack is C, Python and a bit of Rust.",
        "Comfortable with Python or C.",
        "Experience with C/C++ toolchains.",
        "Embedded C on automotive targets.",
    ],
)
def test_bare_c_is_detected_when_it_is_the_language(text: str) -> None:
    assert "c_cpp" in detect_stacks(text)


def test_javascript_is_not_java() -> None:
    stacks = detect_stacks("We write JavaScript and TypeScript every day.")
    assert "web" in stacks
    assert "java" not in stacks


def test_java_is_detected_on_its_own() -> None:
    assert "java" in detect_stacks("Backend Java engineer wanted.")


@pytest.mark.parametrize(
    "text",
    [
        "The codebase is rusty and needs love.",
        "We trust our engineers to make decisions.",
    ],
)
def test_rust_negatives(text: str) -> None:
    assert "rust" not in detect_stacks(text)


def test_ios_is_not_bios() -> None:
    assert "mobile" not in detect_stacks("You will be flashing the biOS of our devices.")
    assert "mobile" in detect_stacks("You will ship our iOS application.")


def test_go_needs_a_programming_context() -> None:
    assert "go" not in detect_stacks("You are ready to go the extra mile for the team.")
    assert "go" in detect_stacks("Backend services written in Go.")
    assert "go" in detect_stacks("Golang is our main language.")


# ----------------------------------------------------------------------- web_dev


def test_web_dev_true_for_react_node_posting() -> None:
    job = make_job(
        "Junior Frontend Developer",
        "You will build user interfaces with React and a Node.js backend in TypeScript.",
    )
    facets = compute_facets(job)
    assert "web" in facets.stacks
    assert facets.web_dev is True


def test_web_dev_false_for_embedded_cpp_posting() -> None:
    job = make_job(
        "Embedded Software Engineer",
        "You will write C++ and firmware for automotive microcontrollers using an RTOS.",
    )
    facets = compute_facets(job)
    assert set(facets.stacks) == {"c_cpp", "embedded"}
    assert facets.web_dev is False


def test_web_stacks_constant() -> None:
    assert {"web"} == WEB_STACKS
    assert set(STACKS) >= WEB_STACKS


# ------------------------------------------------------------------ compute_facets


def test_compute_facets_uses_tags_and_title_for_stacks() -> None:
    job = make_job("Developer", "A nice place to work.", tags=["React", "Kubernetes"])
    facets = compute_facets(job)
    assert set(facets.stacks) == {"web", "devops"}


def test_compute_facets_sets_ids_and_version() -> None:
    job = make_job()
    facets = compute_facets(job)
    assert facets.job_id == job.id
    assert facets.facets_version == FACETS_VERSION


def test_compute_facets_prefers_filter_signals() -> None:
    job = make_job("Kehittäjä", ENGLISH_PARAGRAPH)
    fr = FilterResult(
        job_id=job.id,
        status="keep",
        signals={
            "posting_language": "fi",
            "languages_required": ["fi"],
            "languages_optional": ["sv"],
        },
    )
    facets = compute_facets(job, fr)
    assert facets.posting_language == "fi"
    assert facets.languages_required == ["fi"]
    assert facets.languages_optional == ["sv"]


def test_compute_facets_falls_back_to_detection_without_filter_result() -> None:
    job = make_job(
        "Junior Developer",
        ENGLISH_PARAGRAPH + " Fluent English is required, Finnish is considered a plus.",
    )
    facets = compute_facets(job)
    assert facets.posting_language == "en"
    assert facets.languages_required == ["en"]
    assert facets.languages_optional == ["fi"]


def test_compute_facets_falls_back_when_signals_are_empty() -> None:
    job = make_job(
        "Junior Developer",
        ENGLISH_PARAGRAPH + " Fluent English is required.",
    )
    fr = FilterResult(job_id=job.id, status="keep", signals={})
    facets = compute_facets(job, fr)
    assert facets.posting_language == "en"
    assert facets.languages_required == ["en"]


def test_compute_facets_uses_job_posting_language_when_set() -> None:
    job = make_job("Developer", "Short.", posting_language="de")
    facets = compute_facets(job)
    assert facets.posting_language == "de"


def test_compute_facets_leaves_posting_language_none_when_undetectable() -> None:
    job = make_job("Developer", "Too short to detect.")
    facets = compute_facets(job)
    assert facets.posting_language is None


def test_compute_facets_language_lists_are_sorted_and_lowercase() -> None:
    job = make_job("Developer", "Short.")
    fr = FilterResult(
        job_id=job.id,
        status="keep",
        signals={"languages_required": ["SV", "de"], "languages_optional": ["FI"]},
    )
    facets = compute_facets(job, fr)
    assert facets.languages_required == ["de", "sv"]
    assert facets.languages_optional == ["fi"]


def test_compute_facets_accepts_a_scalar_language_signal() -> None:
    job = make_job("Developer", "Short.")
    fr = FilterResult(
        job_id=job.id,
        status="keep",
        signals={"languages_required": "FI", "languages_optional": ["fi", "en"]},
    )
    facets = compute_facets(job, fr)
    assert facets.languages_required == ["fi"]
    # A language that is both required and optional stays only in the required list.
    assert facets.languages_optional == ["en"]


# ------------------------------------------------------------------- language_ok


def test_language_ok_unknown_posting_language_only_required_matter() -> None:
    facets = JobFacets(
        job_id="x", facets_version=FACETS_VERSION, languages_required=["en"]
    )
    assert facets.language_ok({"en"}) is True
    assert facets.language_ok({"fi"}) is False


def test_language_ok_required_subset_of_spoken() -> None:
    facets = JobFacets(
        job_id="x",
        facets_version=FACETS_VERSION,
        posting_language="en",
        languages_required=["en", "fi"],
    )
    assert facets.language_ok({"en", "fi", "sv"}) is True
    assert facets.language_ok({"en"}) is False


def test_language_ok_posting_language_not_spoken() -> None:
    facets = JobFacets(
        job_id="x", facets_version=FACETS_VERSION, posting_language="pl", languages_required=[]
    )
    assert facets.language_ok({"en", "fi"}) is False
    assert facets.language_ok({"en", "pl"}) is True


def test_language_ok_optional_languages_are_ignored() -> None:
    facets = JobFacets(
        job_id="x",
        facets_version=FACETS_VERSION,
        posting_language="en",
        languages_required=["en"],
        languages_optional=["de", "sv"],
    )
    assert facets.language_ok({"en"}) is True


def test_language_ok_accepts_a_list_and_is_case_insensitive() -> None:
    facets = JobFacets(
        job_id="x", facets_version=FACETS_VERSION, posting_language="en", languages_required=["fi"]
    )
    assert facets.language_ok(["EN", "FI"]) is True
    assert facets.language_ok([]) is False


def test_language_ok_no_requirements_at_all() -> None:
    facets = JobFacets(job_id="x", facets_version=FACETS_VERSION)
    assert facets.language_ok(set()) is True


# -------------------------------------------------------------------- compute_all


def test_compute_all_returns_one_facets_per_job() -> None:
    jobs = [
        make_job("React Developer", "We use React and Node.js.", source_id="1"),
        make_job("C++ Engineer", "Embedded C++ and firmware work.", source_id="2"),
        make_job("Data Engineer", "SQL, Spark and ETL pipelines.", source_id="3"),
    ]
    fr = FilterResult(job_id=jobs[0].id, status="keep", signals={"posting_language": "en"})
    facets = compute_all(jobs, {fr.job_id: fr})
    assert len(facets) == len(jobs)
    assert [f.job_id for f in facets] == [j.id for j in jobs]
    assert facets[0].posting_language == "en"
    assert facets[0].web_dev is True
    assert facets[1].web_dev is False
    assert "data" in facets[2].stacks


def test_compute_all_with_no_filter_results() -> None:
    jobs = [make_job("Rust Engineer", "We write Rust.", source_id="9")]
    facets = compute_all(jobs, {})
    assert len(facets) == 1
    assert facets[0].stacks == ["rust"]


# ------------------------------------------------------------------- round-trip


def test_job_facets_json_round_trip() -> None:
    job = make_job("Fullstack Developer", ENGLISH_PARAGRAPH)
    facets = compute_facets(job)
    again = JobFacets.model_validate_json(facets.model_dump_json())
    assert again == facets
