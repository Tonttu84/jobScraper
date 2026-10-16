"""Tailoring: one Claude call per job that picks CV parts by id.

The model sees the whole parts library (every text with its id) and the posting, and answers
with ids only — a summary, an ordered handful of projects with their bullets, the bullets of each
work entry — plus notes for the owner (what the posting is after, why these picks, what nothing
in the library covers). The ids go through :func:`jobscraper.cv.parts.validate_selection`; a bad
answer is sent back once with its problems listed, and a second bad answer is an error, never a
guess. The CV text itself is only ever looked up, so no sentence the owner did not write can
reach the page.

Results are cached per job in the store, keyed on the library fingerprint, this prompt's version
and the model: editing a single bullet re-tailors, asking twice does not pay twice.

Two ways to get the answer, as with the other AI stages: :meth:`Tailor.tailor` calls the API;
without an API key, :func:`export_prompt` writes the whole request to a file, a Claude Code
subagent writes the answer JSON, and :meth:`Tailor.import_answer` validates and caches it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from jobscraper.ai.client import _THINKING_IS_IMPLICIT, AIStage, _client
from jobscraper.ai.prompts import job_prompt
from jobscraper.config import Profile
from jobscraper.cv.parts import (
    CvParts,
    EntryPick,
    Selection,
    SelectionError,
    TailoredCV,
    effective_max_bullets,
    parts_fingerprint,
    resolve,
    to_markdown,
    validate_selection,
)
from jobscraper.models import AIVerdict, FilterResult, Job
from jobscraper.store import Store

log = logging.getLogger(__name__)

#: Bump on any wording change below; it is part of the cache key.
TAILOR_PROMPT_VERSION = "2026-10-16.1"


class TailorError(RuntimeError):
    """The model gave no usable selection (refused, or twice an answer that does not fit)."""


# --------------------------------------------------------------------------- schema


class PickedEntry(BaseModel):
    id: str
    bullets: list[str] = Field(description="bullet ids of this entry, in the order to show them")


class TailorAnswer(BaseModel):
    """What the model returns. Property order is generation order: read the posting first."""

    focus: str = Field(description="One or two sentences: what this posting most wants from the hire")
    summary: str = Field(description="id of the one PROFILE summary")
    projects: list[PickedEntry] = Field(description="the projects to show, lead project first")
    work: list[PickedEntry] = Field(description="each work entry once, with the bullets to show")
    rationale: str = Field(description="Two or three sentences for the candidate: why these picks "
                                       "and what strong part was left out, and why")
    gaps: list[str] = Field(default_factory=list,
                            description="Requirements of the posting nothing in the library covers; "
                                        "short, most important first. For the cover letter.")


def to_selection(answer: TailorAnswer) -> Selection:
    return Selection(summary=answer.summary,
                     projects=[EntryPick(id=p.id, bullets=list(p.bullets)) for p in answer.projects],
                     work={w.id: list(w.bullets) for w in answer.work})


def answer_problems(parts: CvParts, answer: TailorAnswer) -> list[str]:
    """Every way ``answer`` does not fit ``parts``; empty when it is usable."""
    problems: list[str] = []
    seen: set[str] = set()
    for w in answer.work:
        if w.id in seen:
            problems.append(f"work entry {w.id!r} listed twice")
        seen.add(w.id)
    try:
        validate_selection(parts, to_selection(answer))
    except SelectionError as exc:
        problems += exc.problems
    return problems


# --------------------------------------------------------------------------- prompt


def _marker(default: bool) -> str:
    return " (on the untailored CV)" if default else ""


def library_block(parts: CvParts) -> str:
    """The whole library as the model reads it: every text with its id, the page's limits."""
    s = parts.slots
    lines = [
        f"CV PARTS LIBRARY (version {parts.version})",
        f"The page holds {s.projects} projects; each project shows up to {s.project_bullets} "
        f"bullets and each work entry up to {s.work_bullets}, unless the entry says otherwise.",
        "",
        "SUMMARIES (choose exactly one; the first is the untailored CV's)",
    ]
    for summary in parts.summaries:
        lines.append(f"[{summary.id}] tags: {', '.join(summary.tags) or '-'}")
        lines.append(f"  {summary.text}")
    lines += ["", f"PROJECTS (choose an ordered list of up to {s.projects}, lead project first)"]
    for entry in parts.projects:
        lines.append(f"[{entry.id}] {entry.title}{_marker(entry.default)} — tags: "
                     f"{', '.join(entry.tags) or '-'}; up to {effective_max_bullets(parts, entry)} bullets")
        lines.append(f"  lead: {entry.lead}")
        lines += [f"  - [{b.id}] {b.text}{_marker(b.default)}" for b in entry.bullets]
    lines += ["", "WORK (every entry is always shown, in this order; choose its bullets)"]
    for entry in parts.work:
        lines.append(f"[{entry.id}] {entry.title} ({entry.period}); up to "
                     f"{effective_max_bullets(parts, entry)} bullets")
        if entry.lead:
            lines.append(f"  lead: {entry.lead}")
        lines += [f"  - [{b.id}] {b.text}{_marker(b.default)}" for b in entry.bullets]
    return "\n".join(lines) + "\n"


TAILOR_SYSTEM = """You are tailoring the one-page CV of a specific {role_label} to one job posting.

You never write CV text. Every sentence on the page comes from the CV PARTS LIBRARY below, written
by the candidate, and you answer with ids only. An id that is not in the library is an error.

How to choose:
- Summary: the one whose angle matches what the posting is mostly about; the first one when no
  other fits clearly better.
- Projects: fill every project slot. Order them by what a recruiter for THIS posting should read
  first: evidence of the posting's core stack and of the work the role actually does beats an
  impressive but unrelated project. A project that only matches on a soft skill goes last.
- Bullets: within each chosen entry, pick the bullets that speak to the posting, up to the entry's
  limit, in the order they should be read. A bullet with concrete technology or numbers beats a
  generic one when both fit. Fewer than the limit is fine when the rest would be noise.
- Work: list every work entry once and choose its bullets the same way; these jobs are not in
  software, so pick the bullets that transfer to this role (analysis, teaching, ownership, ...).
- Projects tagged ai-directed were built by directing coding agents (their own text says so).
  They show specification, testing, product and operations skill. For a posting that is about
  writing code by hand in a given language, lead with hand-written projects in that language and
  use ai-directed ones as support; for product, AI, automation or generalist roles they can lead.
- Mirror the posting: when a part names a technology the posting asks for, prefer it.

Also return, for the candidate (none of this goes on the CV): `focus`, what the posting most wants;
`rationale`, why these picks and which strong part you left out and why; `gaps`, requirements the
library does not cover at all, so the cover letter can address them.

"""


def tailor_system_prompt(profile: Profile, parts: CvParts) -> str:
    """Instructions, the library and the dossier: fixed per candidate, so cached across jobs."""
    prompt = TAILOR_SYSTEM.replace("{role_label}", profile.prompt.role_label.strip())
    prompt += library_block(parts)
    dossier = profile.dossier.strip()
    if dossier:
        prompt += ("\nDOSSIER (background on the candidate, to judge which parts are strongest; "
                   "never a source of CV text)\n" + dossier + "\n")
    return prompt


def tailor_user_prompt(job: Job, fr: FilterResult | None, max_chars: int,
                       verdict: AIVerdict | None) -> str:
    """The posting as the AI stages read it, plus the ranker's angle on it when there is one."""
    prompt = job_prompt(job, fr, max_chars)
    if verdict is not None and (verdict.why_apply or verdict.concerns):
        notes = ["", "", "RANKER'S NOTES (an earlier pass judged this posting against the CV)"]
        if verdict.why_apply:
            notes.append("Why apply / what to emphasise:")
            notes += [f"- {item}" for item in verdict.why_apply]
        if verdict.concerns:
            notes.append("Concerns:")
            notes += [f"- {item}" for item in verdict.concerns]
        prompt += "\n".join(notes)
    return prompt


# ----------------------------------------------------------------------------- call


@dataclass
class Tailoring:
    """A validated selection, its text, and the model's notes for the owner."""

    selection: Selection
    cv: TailoredCV
    focus: str
    rationale: str
    gaps: list[str] = field(default_factory=list)
    model: str = ""
    cached: bool = False


class Tailor:
    """Picks CV parts for one job at a time; see the module docstring."""

    def __init__(self, profile: Profile, store: Store, parts: CvParts, model: str | None = None) -> None:
        ai = profile.ai
        self.profile = profile
        self.store = store
        self.parts = parts
        self.model = model or ai.rank_model
        self.effort = ai.rank_effort
        self.max_chars = ai.max_description_chars
        self.system = tailor_system_prompt(profile, parts)
        self.cache_key = f"{parts_fingerprint(parts)}|{TAILOR_PROMPT_VERSION}|{self.model}"
        self._client = None

    @property
    def client(self):  # type: ignore[no-untyped-def]
        """Built on first use: the subagent path never needs credentials."""
        if self._client is None:
            self._client = _client()
        return self._client

    @client.setter
    def client(self, value) -> None:  # type: ignore[no-untyped-def]
        self._client = value

    def _thinking(self) -> dict[str, dict]:
        if any(family in self.model.lower() for family in _THINKING_IS_IMPLICIT):
            return {}
        return {"thinking": {"type": "adaptive"}}

    def _ask(self, messages: list[dict]) -> TailorAnswer:
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=8000,
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            output_format=TailorAnswer,
            output_config={"effort": self.effort},
            **self._thinking(),
        )
        log.info("tailor: %s usage %s", self.model, AIStage._usage(response.usage))
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise TailorError(f"{self.model} gave no structured answer (stop_reason={response.stop_reason})")
        return response.parsed_output

    def _result(self, answer: TailorAnswer, *, cached: bool) -> Tailoring:
        selection = to_selection(answer)
        return Tailoring(selection=selection, cv=resolve(self.parts, selection), focus=answer.focus,
                         rationale=answer.rationale, gaps=list(answer.gaps), model=self.model,
                         cached=cached)

    def tailor(self, job: Job, fr: FilterResult | None, verdict: AIVerdict | None = None, *,
               force: bool = False) -> Tailoring:
        """The cached tailoring for ``job`` under this library, else a fresh one (stored)."""
        if not force:
            stored = self.store.tailoring(job.id, self.cache_key)
            if stored is not None:  # validated when stored, under this very library
                return self._result(TailorAnswer.model_validate_json(stored), cached=True)
        messages = [{"role": "user", "content": self.user_prompt(job, fr, verdict)}]
        answer = self._ask(messages)
        problems = answer_problems(self.parts, answer)
        if problems:
            log.warning("tailor: answer for %s did not fit the library, asking again: %s",
                        job.id, "; ".join(problems))
            messages += [
                {"role": "assistant", "content": answer.model_dump_json()},
                {"role": "user", "content": "That answer does not fit the library:\n"
                 + "\n".join(f"- {p}" for p in problems)
                 + "\nAnswer again, using only ids from the CV PARTS LIBRARY and staying within "
                   "each entry's limit."},
            ]
            answer = self._ask(messages)
            problems = answer_problems(self.parts, answer)
            if problems:
                raise TailorError("the answer still does not fit the library:\n" + "\n".join(problems))
        self.store.save_tailoring(job.id, self.cache_key, self.model, answer.model_dump_json())
        return self._result(answer, cached=False)

    def user_prompt(self, job: Job, fr: FilterResult | None, verdict: AIVerdict | None) -> str:
        return tailor_user_prompt(job, fr, self.max_chars, verdict)

    def import_answer(self, job: Job, text: str) -> Tailoring:
        """A subagent's answer (JSON text): validated against the library, then cached."""
        try:
            answer = TailorAnswer.model_validate_json(text)
        except ValidationError as exc:
            raise TailorError(f"not a valid answer: {exc}") from exc
        problems = answer_problems(self.parts, answer)
        if problems:
            raise TailorError("the answer does not fit the library:\n" + "\n".join(problems))
        self.store.save_tailoring(job.id, self.cache_key, self.model, answer.model_dump_json())
        return self._result(answer, cached=False)


_EXPORT_TAIL = """
ANSWER FORMAT
Write one JSON object matching this schema, and nothing else, to:
  {answer_path}
Every id must come from the CV PARTS LIBRARY above. Check with
  jobscraper tailor {job_id} --import
which lists every problem; fix them and import again.

{schema}
"""


def export_prompt(tailor: Tailor, job: Job, fr: FilterResult | None, verdict: AIVerdict | None,
                  answer_path: Path) -> str:
    """The whole request as one file for a subagent: system prompt, posting, answer format."""
    schema = json.dumps(TailorAnswer.model_json_schema(), indent=2)
    tail = (_EXPORT_TAIL.replace("{answer_path}", str(answer_path)).replace("{job_id}", job.id)
            .replace("{schema}", schema))
    return ("# SYSTEM\n\n" + tailor.system + "\n# USER\n\n"
            + tailor.user_prompt(job, fr, verdict) + "\n" + tail)


# -------------------------------------------------------------------------- hand-off


def _bullet_lines(texts: list[str]) -> list[str]:
    return [f"   - {text}" for text in texts]


def to_handoff_markdown(job: Job, result: Tailoring) -> str:
    """What the owner works from: the job, the model's notes, what goes where, then the CV text."""
    sel, cv = result.selection, result.cv
    lines = [
        f"# Tailored CV: {job.title} — {job.company or 'unknown company'}",
        "",
        f"- Job: {job.url}",
        f"- Job id: `{job.id}`",
        f"- Library {cv.version}, {result.model}" + (" (cached)" if result.cached else ""),
        "",
        "## What the posting wants",
        "",
        result.focus,
        "",
        "## Why these parts",
        "",
        result.rationale,
        "",
        "## Not covered by the library (cover letter)",
        "",
    ]
    lines += [f"- {gap}" for gap in result.gaps] or ["- nothing"]
    lines += ["", "## Into the Canva layout", "", f"Profile: **{sel.summary}**", "",
              "Sample projects, in this order:"]
    for n, (pick, entry) in enumerate(zip(sel.projects, cv.projects, strict=True), start=1):
        lines.append(f"{n}. **{entry.title}** (`{pick.id}`: {', '.join(pick.bullets)})")
        lines += _bullet_lines(entry.bullets)
    lines += ["", "Work experience bullets:"]
    for entry in cv.work:
        lines.append(f"- **{entry.title}**")
        lines += _bullet_lines(entry.bullets)
    lines += ["", "---", ""]
    return "\n".join(lines) + "\n" + to_markdown(cv)
