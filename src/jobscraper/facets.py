"""Deterministic, structured facets per job, for filtering the final results in a UI.

Two axes:

1. **Natural languages** — which language the posting is written in, which languages it
   explicitly requires, and which it merely appreciates. These come straight from the rule
   filter's signals when a :class:`~jobscraper.models.FilterResult` is available, and are
   re-derived from the text otherwise.
2. **Tech stack** — a small set of coarse tags (see :data:`STACKS`) matched with regexes over
   title + description + tags, plus the ``web_dev`` flag that says whether the posting overlaps
   what the Full Stack Open course teaches (React/Node/TypeScript web development).

No AI, no network: the same input always yields the same facets, so the UI can filter cheaply
and results can be recomputed for old rows by bumping :data:`FACETS_VERSION`.
"""

from __future__ import annotations

import re
from typing import Any

from jobscraper.filters.language import detect_language, find_language_requirements
from jobscraper.models import FilterResult, Job, JobFacets

FACETS_VERSION = "2026-09-08.1"

# Programming-language names that make a neighbouring bare "C" credible ("C, Python and Rust",
# "Python or C"). Ordered longest-first where one is a prefix of another.
_NEIGHBOUR = (
    r"(?:c\+\+|cpp|c#|python|javascript|java|typescript|rust|golang|go|\.net|dotnet|php|ruby|"
    r"kotlin|swift|matlab|assembly|perl|scala|haskell|fortran|ada|vhdl|verilog|qt|embedded|linux)"
)
# Separators that read as "these are items in a list of languages".
_SEP = r"(?:\s*/\s*|\s*,\s*|\s*&\s*|\s+and\s+|\s+or\s+)"
# A standalone "C" that is not the start of "C++", "C#" or "C-level".
_BARE_C = r"\bc\b(?![+#\-])"

STACKS: dict[str, re.Pattern[str]] = {
    "web": re.compile(
        r"\b(?:react|angular|vue|svelte|next\.?js|nuxt|node\.?js|node|express|typescript|"
        r"javascript|html5?|css3?|sass|scss|tailwind|redux|jquery|webpack|vite|"
        r"front[\s-]?end|full[\s-]?stack|mongodb|graphql|rest apis?|web develop\w*)\b",
        re.I,
    ),
    "c_cpp": re.compile(
        # "C++"/"cpp" and "embedded C" are unambiguous; a bare "C" needs a language context so
        # that "Vitamin C", "C-level" and "plan C" stay out.
        r"\bc\+\+|\bcpp\b|\bembedded\s+c\b|"
        rf"{_BARE_C}{_SEP}{_NEIGHBOUR}\b|"
        rf"\b{_NEIGHBOUR}{_SEP}{_BARE_C}|"
        rf"\bin\s+{_BARE_C}|"
        rf"{_BARE_C}\s+(?:programming|program|language|developer|development|dev|engineer|"
        r"programmer|coder|coding|skills)\b",
        re.I,
    ),
    "python": re.compile(r"\b(?:python[23]?|django|flask|fastapi)\b", re.I),
    "go": re.compile(
        r"\bgolang\b|\bgo\s+(?:developer|engineer|programming|language|lang)\b|\bin\s+go\b",
        re.I,
    ),
    "rust": re.compile(r"\brust\b", re.I),
    "java": re.compile(r"\bjava\b|\bspring\s*boot\b|\bspring\b|\bkotlin\b", re.I),
    "dotnet": re.compile(r"\bc#|\basp\.net\b|\bdotnet\b|(?<!\w)\.net\b", re.I),
    "mobile": re.compile(
        r"\b(?:android|ios|swift|kotlin|flutter|react native|swiftui|jetpack compose)\b", re.I
    ),
    "embedded": re.compile(
        r"\b(?:embedded|firmware|rtos|microcontrollers?|mcu|fpga|bare[\s-]metal)\b", re.I
    ),
    "devops": re.compile(
        r"\b(?:devops|kubernetes|k8s|docker|terraform|ansible|aws|azure|gcp|google cloud|"
        r"cloud engineer|sre|site reliability)\b|\bci\s*/\s*cd\b",
        re.I,
    ),
    "data": re.compile(
        r"\b(?:sql|postgres(?:ql)?|data engineer\w*|pandas|spark|etl|machine learning|"
        r"data scien\w*|pytorch|tensorflow)\b",
        re.I,
    ),
    "game": re.compile(r"\b(?:unity|unreal|gameplay)\b|\bgame\s+dev\w*", re.I),
    "qa": re.compile(
        r"\b(?:qa|playwright|cypress|selenium|testers?)\b|\btest automation\b|"
        r"\bquality assurance\b",
        re.I,
    ),
    "php": re.compile(r"\b(?:php|laravel|symfony)\b", re.I),
    "ruby": re.compile(r"\bruby\b|\brails\b", re.I),
}

#: Stack tags that mean "this is the kind of web work Full Stack Open prepares you for".
WEB_STACKS = {"web"}


def detect_stacks(text: str) -> list[str]:
    """Return the sorted stack tags whose pattern occurs in ``text``."""
    if not text:
        return []
    return sorted(tag for tag, pattern in STACKS.items() if pattern.search(text))


def _codes(value: Any) -> list[str]:
    """Normalize a signals value into sorted, lower-case, de-duplicated ISO-639-1 codes."""
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    return sorted({str(code).strip().lower() for code in value if str(code).strip()})


def compute_facets(job: Job, fr: FilterResult | None = None) -> JobFacets:
    """Derive facets for one job, reusing the rule filter's signals when they are available."""
    signals: dict[str, Any] = fr.signals if fr is not None else {}

    if "posting_language" in signals:
        posting_language = signals["posting_language"] or None
    elif job.posting_language:
        posting_language = job.posting_language
    else:
        posting_language, _confidence = detect_language(job.description or job.title)
    if posting_language:
        posting_language = posting_language.strip().lower()

    if "languages_required" in signals or "languages_optional" in signals:
        required = _codes(signals.get("languages_required"))
        optional = _codes(signals.get("languages_optional"))
    else:
        req = find_language_requirements(job.text)
        required = _codes(req.required)
        optional = _codes(req.optional)
    optional = [code for code in optional if code not in required]

    stacks = detect_stacks("\n".join([job.title, job.description or "", " ".join(job.tags)]))
    return JobFacets(
        job_id=job.id,
        facets_version=FACETS_VERSION,
        posting_language=posting_language,
        languages_required=required,
        languages_optional=optional,
        stacks=stacks,
        web_dev=bool(set(stacks) & WEB_STACKS),
    )


def compute_all(jobs: list[Job], filters: dict[str, FilterResult]) -> list[JobFacets]:
    """Facets for every job, in input order, using ``filters[job.id]`` when present."""
    return [compute_facets(job, filters.get(job.id)) for job in jobs]
