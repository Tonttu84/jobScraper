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

With a height model (:mod:`jobscraper.cv.measure`) the prompt shows every part's height in mm
and the page budget, and the answer also ranks the bullets it would give up first
(``drop_order``). The page fit is then automation, not a second request: :mod:`jobscraper.cv.fit`
drops those bullets until the model says the page fits, checked by one real render (``fit``, the
CLI passes :func:`jobscraper.cv.render.measure_overflow`). The stored answer is the fitted one,
with the model's own picks and what was trimmed kept beside it for the hand-off.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from jobscraper.ai.client import _THINKING_IS_IMPLICIT, AIStage, _client
from jobscraper.ai.prompts import job_prompt
from jobscraper.config import Profile
from jobscraper.cv.fit import Trim, fit_selection, parse_drop_order
from jobscraper.cv.measure import EntryHeights, HeightModel
from jobscraper.cv.parts import (
    CvParts,
    EntryPick,
    Selection,
    SelectionError,
    TailoredCV,
    default_selection,
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
TAILOR_PROMPT_VERSION = "2026-10-17.1"  # measured page budget, drop_order


def tailoring_key_prefix(parts: CvParts) -> str:
    """The cache key of a tailoring under ``parts`` and this prompt, up to the model."""
    return f"{parts_fingerprint(parts)}|{TAILOR_PROMPT_VERSION}|"


class TailorError(RuntimeError):
    """The model gave no usable selection (refused, or twice an answer with ids not in the library).

    ``problems`` lists what was wrong, one line each; without a list, the message's first line.
    """

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or [message.splitlines()[0]]


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
    drop_order: list[str] = Field(
        default_factory=list,
        description='the selected bullets to give up first if the page still overflows, most '
                    'expendable first, as "<entry id>/<bullet id>" (projects and work entries)')
    rationale: str = Field(description="Two or three sentences for the candidate: why these picks "
                                       "and what strong part was left out, and why")
    gaps: list[str] = Field(default_factory=list,
                            description="Requirements of the posting nothing in the library covers; "
                                        "short, most important first. For the cover letter.")


def to_selection(answer: TailorAnswer) -> Selection:
    return Selection(summary=answer.summary,
                     projects=[EntryPick(id=p.id, bullets=list(p.bullets)) for p in answer.projects],
                     work={w.id: list(w.bullets) for w in answer.work})


class StoredAnswer(TailorAnswer):
    """What the store keeps: the answer as fitted to the page, plus what the fitter took out
    and the model's own picks (``original_*``, only when something was trimmed). Read back as a
    plain :class:`TailorAnswer` (``cv-render``), the extra fields are ignored."""

    trimmed: list[Trim] = Field(default_factory=list)
    original_projects: list[PickedEntry] | None = None
    original_work: list[PickedEntry] | None = None
    notes: list[str] = Field(default_factory=list)


def _picked(selection: Selection) -> tuple[list[PickedEntry], list[PickedEntry]]:
    return ([PickedEntry(id=p.id, bullets=list(p.bullets)) for p in selection.projects],
            [PickedEntry(id=i, bullets=list(b)) for i, b in selection.work.items()])


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


def _mm(value: float | None, fmt: str) -> str:
    """``fmt`` with ``value`` in mm (one decimal); nothing without a height."""
    return "" if value is None else fmt.format(f"{value:.1f}")


def library_block(parts: CvParts, heights: HeightModel | None = None) -> str:
    """The whole library as the model reads it: every text with its id, the page's limits, and
    with ``heights`` each part's height on the page and the budget they share."""
    s = parts.slots
    lines = [
        f"CV PARTS LIBRARY (version {parts.version})",
        f"The page holds {s.projects} projects; each project shows up to {s.project_bullets} "
        f"bullets and each work entry up to {s.work_bullets}, unless the entry says otherwise.",
    ]
    if heights is not None:
        lines += [
            f"Page budget for summary + projects + work: {heights.budget_mm:.1f} mm; the untailored "
            f"CV uses {heights.untailored_mm(parts):.1f} mm.",
            "Heights are as printed: an entry's own height is its title and lead, and each bullet "
            "adds its own.",
        ]

    def own(kind: str, ident: str) -> EntryHeights | None:
        return None if heights is None else getattr(heights, kind)[ident]

    def bullets(entry, mine: EntryHeights | None) -> list[str]:
        return [f"  - [{b.id}] {b.text}{_marker(b.default)}"
                + _mm(None if mine is None else mine.bullets[b.id], " ({} mm)")
                for b in entry.bullets]

    lines += ["", "SUMMARIES (choose exactly one; the first is the untailored CV's)"]
    for summary in parts.summaries:
        mine_mm = None if heights is None else heights.summaries[summary.id]
        lines.append(f"[{summary.id}] tags: {', '.join(summary.tags) or '-'}" + _mm(mine_mm, " — {} mm"))
        lines.append(f"  {summary.text}")
    lines += ["", f"PROJECTS (choose an ordered list of up to {s.projects}, lead project first)"]
    for entry in parts.projects:
        mine = own("projects", entry.id)
        lines.append(f"[{entry.id}] {entry.title}{_marker(entry.default)}"
                     + _mm(None if mine is None else mine.header, " — {} mm") + " — tags: "
                     f"{', '.join(entry.tags) or '-'}; up to {effective_max_bullets(parts, entry)} bullets")
        lines.append(f"  lead: {entry.lead}")
        lines += bullets(entry, mine)
    lines += ["", "WORK (every entry is always shown, in this order; choose its bullets)"]
    for entry in parts.work:
        mine = own("work", entry.id)
        lines.append(f"[{entry.id}] {entry.title} ({entry.period})"
                     + _mm(None if mine is None else mine.header, " — {} mm")
                     + f"; up to {effective_max_bullets(parts, entry)} bullets")
        if entry.lead:
            lines.append(f"  lead: {entry.lead}")
        lines += bullets(entry, mine)
    return "\n".join(lines) + "\n"


TAILOR_SYSTEM = """You are tailoring the one-page CV of a specific {role_label} to one job posting.

You never write CV text. Every sentence on the page comes from the CV PARTS LIBRARY below, written
by the candidate, and you answer with ids only. An id that is not in the library is an error.

Start from the untailored CV (the parts marked "on the untailored CV") and change what this
posting gives a reason to change. The projects are the main lever; the summary and the work
entries change only a little.
- Projects: fill every project slot. Order them by what a recruiter for THIS posting should read
  first: evidence of the posting's core stack and of the work the role actually does beats an
  impressive but unrelated project. A project that only matches on a soft skill goes last.
- Project bullets: pick the ones that speak to the posting, in the order they should be read. A
  bullet with concrete technology or numbers beats a generic one when both fit. Fewer than the
  limit is fine when the rest would be noise.
- Summary: keep the first one unless another one's angle fits the posting clearly better.
- Work: list every work entry once. Keep its untailored bullets, swapping at most two of them per
  entry for a bullet that transfers clearly better to this role (analysis, teaching, ownership,
  ...). Do not add bullets beyond the untailored count.
- Projects tagged ai-directed were built by directing coding agents (their own text says so).
  They show specification, testing, product and operations skill. For a posting that is about
  writing code by hand in a given language, lead with hand-written projects in that language and
  use ai-directed ones as support; for product, AI, automation or generalist roles they can lead.
- Mirror the posting: when a part names a technology the posting asks for, prefer it.

PAGE BUDGET
Every part in the library shows its height on the page in millimetres, and the budget line says
how much the summary, projects and work entries may take together. Add up your selection and
stay within the budget: a longer part has to be paid for with a shorter or dropped one. Also
return `drop_order`: the bullets you would give up first if the page still overflows, most
expendable first, as "<entry id>/<bullet id>" (projects and work entries alike). The pipeline
drops them in that order until the page fits; it never asks you again.

Also return, for the candidate (none of this goes on the CV): `focus`, what the posting most wants;
`rationale`, why these picks and which strong part you left out and why; `gaps`, requirements the
library does not cover at all, so the cover letter can address them.

"""


def tailor_system_prompt(profile: Profile, parts: CvParts,
                         heights: HeightModel | None = None) -> str:
    """Instructions, the library (with its heights) and the dossier: fixed per candidate, so
    cached across jobs."""
    prompt = TAILOR_SYSTEM.replace("{role_label}", profile.prompt.role_label.strip())
    prompt += library_block(parts, heights)
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
    overflow_mm: float | None = None  # measured by the fit check; None when there was none
    trimmed: list[Trim] = field(default_factory=list)  # what the fitter dropped, in order
    notes: list[str] = field(default_factory=list)  # drop_order entries that were ignored


class Tailor:
    """Picks CV parts for one job at a time; see the module docstring."""

    def __init__(self, profile: Profile, store: Store, parts: CvParts, model: str | None = None,
                 fit: Callable[[TailoredCV], float] | None = None,
                 heights: HeightModel | None = None) -> None:
        """``heights``: the part heights; they go into the prompt and drive the fitter. ``fit``
        measures how many mm a CV overflows its page: the fitter's check (without heights the
        page is only measured and reported)."""
        ai = profile.ai
        self.profile = profile
        self.store = store
        self.parts = parts
        self.model = model or ai.rank_model
        self.effort = ai.rank_effort
        self.max_chars = ai.max_description_chars
        self.heights = heights
        self.system = tailor_system_prompt(profile, parts, heights)
        self.cache_key = tailoring_key_prefix(parts) + self.model
        self.fit = fit
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

    def _result(self, answer: StoredAnswer, *, cached: bool,
                overflow_mm: float | None = None) -> Tailoring:
        selection = to_selection(answer)
        return Tailoring(selection=selection, cv=resolve(self.parts, selection), focus=answer.focus,
                         rationale=answer.rationale, gaps=list(answer.gaps), model=self.model,
                         cached=cached, overflow_mm=overflow_mm, trimmed=list(answer.trimmed),
                         notes=list(answer.notes))

    def _fit_and_store(self, job: Job, answer: TailorAnswer) -> Tailoring:
        """Fit a valid answer to the page (with heights; without them only measure it, with
        ``fit``), store it and return it."""
        selection = to_selection(answer)
        trims, notes = parse_drop_order(self.parts, selection, answer.drop_order)
        stored = StoredAnswer(**answer.model_dump(), notes=notes)
        overflow = None
        if self.heights is not None:
            fitted = fit_selection(self.parts, selection, trims, self.heights, self.fit)
            overflow = fitted.overflow_mm
            if fitted.dropped:
                log.info("tailor: %s trimmed to fit the page: %s", job.id,
                         ", ".join(t.label for t in fitted.dropped))
                projects, work = _picked(fitted.selection)
                stored = stored.model_copy(update={
                    "projects": projects, "work": work, "trimmed": fitted.dropped,
                    "original_projects": answer.projects, "original_work": answer.work})
        elif self.fit is not None:
            overflow = self.fit(resolve(self.parts, selection))
        self.store.save_tailoring(job.id, self.cache_key, self.model, stored.model_dump_json())
        return self._result(stored, cached=False, overflow_mm=overflow)

    def tailor(self, job: Job, fr: FilterResult | None, verdict: AIVerdict | None = None, *,
               force: bool = False) -> Tailoring:
        """The cached tailoring for ``job`` under this library, else a fresh one (stored)."""
        if not force:
            stored = self.store.tailoring(job.id, self.cache_key)
            if stored is not None:  # validated when stored, under this very library
                return self._result(StoredAnswer.model_validate_json(stored), cached=True)
        messages = [{"role": "user", "content": self.user_prompt(job, fr, verdict)}]
        answer = self._ask(messages)
        problems = answer_problems(self.parts, answer)
        if problems:
            log.warning("tailor: answer for %s did not fit the library, asking again: %s",
                        job.id, "; ".join(problems))
            messages += [
                {"role": "assistant", "content": answer.model_dump_json()},
                {"role": "user", "content": "That answer does not fit the library:\n"
                 + "\n".join(f"- {p}" for p in problems) + "\nAnswer again, using only ids from "
                 "the CV PARTS LIBRARY and staying within each entry's limit."},
            ]
            answer = self._ask(messages)
            problems = answer_problems(self.parts, answer)
            if problems:
                raise TailorError("the answer still does not fit the library:\n"
                                  + "\n".join(problems), problems)
        return self._fit_and_store(job, answer)

    def user_prompt(self, job: Job, fr: FilterResult | None, verdict: AIVerdict | None) -> str:
        return tailor_user_prompt(job, fr, self.max_chars, verdict)

    def import_answer(self, job: Job, text: str) -> Tailoring:
        """A subagent's answer (JSON text): validated against the library, then cached."""
        try:
            answer = TailorAnswer.model_validate_json(text)
        except ValidationError as exc:
            raise TailorError(f"not a valid answer: {exc}") from exc
        problems = answer_problems(self.parts, answer)
        if problems:  # nothing is cached: the subagent fixes the JSON and imports again
            raise TailorError("the answer does not fit the library:\n" + "\n".join(problems),
                              problems)
        # An overflowing page is not an error any more: the fitter trims it by drop_order.
        return self._fit_and_store(job, answer)


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


def _short(text: str, most: int = 60) -> str:
    return text if len(text) <= most else text[:most - 1].rstrip() + "…"


def _trimmed_text(parts: CvParts, trim: Trim) -> str:
    if trim.bullet is None:
        return f"project **{parts.project(trim.entry).title}**"  # type: ignore[union-attr]
    entry = parts.project(trim.entry) if trim.kind == "project" else parts.work_entry(trim.entry)
    return f'"{_short(entry.bullet(trim.bullet).text)}"'  # type: ignore[union-attr]  # validated


def trim_lines(parts: CvParts, result: Tailoring) -> list[str]:
    """Markdown bullets: what the fitter dropped to fit the page, and ignored drop_order entries."""
    lines = []
    if result.trimmed:
        lines.append("- Trimmed automatically to fit the page: "
                     + "; ".join(_trimmed_text(parts, t) for t in result.trimmed))
    return lines + [f"- Note: {note}" for note in result.notes]


def changes_from_standard(parts: CvParts, selection: Selection) -> list[str]:
    """Markdown bullets: how ``selection`` differs from the untailored CV (summary, projects in
    and out, work bullets added and removed, by text)."""
    std = default_selection(parts)
    lines = [f"- Summary: unchanged (`{std.summary}`)" if selection.summary == std.summary
             else f"- Summary: `{std.summary}` → `{selection.summary}`"]
    old, new = [p.id for p in std.projects], [p.id for p in selection.projects]
    added, dropped = [i for i in new if i not in old], [i for i in old if i not in new]

    def named(ids: list[str]) -> str:
        return ", ".join(f"**{parts.project(i).title}** (`{i}`)" for i in ids)  # type: ignore[union-attr]

    if added:
        lines.append(f"- Projects in: {named(added)}")
    if dropped:
        lines.append(f"- Projects out: {named(dropped)}")
    if not added and not dropped:
        lines.append("- Projects: the standard ones" + (", reordered" if new != old else ""))
    work = []
    for entry in parts.work:
        before = std.work[entry.id]
        after = selection.work.get(entry.id, before)

        def text(b: str, entry=entry) -> str:
            return _short(entry.bullet(b).text)  # type: ignore[union-attr]  # validated ids

        diff = [f'+ "{text(b)}"' for b in after if b not in before]
        diff += [f'− "{text(b)}"' for b in before if b not in after]
        if diff:
            work.append(f"- **{entry.title}**: " + "; ".join(diff))
    return lines + (work or ["- Work bullets: unchanged"])


def to_handoff_markdown(job: Job, result: Tailoring, parts: CvParts,
                        pdf_path: Path | None = None) -> str:
    """What the owner works from: the job, the model's notes, what changed from the standard CV,
    what goes where, then the CV text. ``pdf_path``: the printed CV, when one was written."""
    sel, cv = result.selection, result.cv
    lines = [
        f"# Tailored CV: {job.title} — {job.company or 'unknown company'}",
        "",
        f"- Job: {job.url}",
        f"- Job id: `{job.id}`",
        f"- Library {cv.version}, {result.model}" + (" (cached)" if result.cached else ""),
    ]
    if pdf_path is not None:
        lines.append(f"- PDF: {pdf_path}")
    lines += [
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
    lines += ["", "## Changes from your standard CV", "", *changes_from_standard(parts, sel),
              *trim_lines(parts, result)]
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
