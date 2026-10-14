"""The CV parts library: every sentence the CV may contain, written once by the owner.

A tailored CV is never *written* by the pipeline, only *selected*: a summary, an ordered handful
of projects with some of their bullets, and some bullets of each work entry, all by id from
``config/cv-parts.yaml``. Whatever picks them (the default selection here, an AI call later) can
therefore not put a single new claim on the page — an unknown id is a :class:`SelectionError`,
not a sentence. The library is validated up front so a typo in an id or a page that cannot hold
the defaults fails at load time instead of when a CV is rendered.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, ClassVar

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from jobscraper.config import _load_yaml, paths

CV_PARTS_FILE = "cv-parts.yaml"
_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")


def _scalar_to_str(value: Any) -> Any:
    """``period: 2024`` or ``version: 2026.1`` arrive from YAML as numbers; keep them as text."""
    return str(value) if isinstance(value, int | float) and not isinstance(value, bool) else value


#: Free text from the library: numbers accepted as text, surrounding whitespace (and the trailing
#: newline of a folded ``>`` block) dropped.
Text = Annotated[str, BeforeValidator(_scalar_to_str), AfterValidator(str.strip)]


class _Strict(BaseModel):
    # A misspelt key (``defualt: true``) would otherwise be dropped silently.
    model_config = ConfigDict(extra="forbid")


class _Part(_Strict):
    """Anything with an id and tags; checks both, plus the subclass's required text fields."""

    kind: ClassVar[str] = "part"
    required: ClassVar[tuple[str, ...]] = ()

    id: str
    tags: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_part(self):
        if not _ID.fullmatch(self.id):
            raise ValueError(f"{self.kind} id {self.id!r}: use lowercase letters, digits, '-' and '_', "
                             "starting with a letter or digit")
        for name in self.required:
            if not getattr(self, name):
                raise ValueError(f"{self.kind} {self.id!r}: {name} is blank")
        for tag in self.tags:
            if not tag or tag != tag.lower():
                raise ValueError(f"{self.kind} {self.id!r}: tag {tag!r} must be non-blank lowercase")
        return self


class Bullet(_Part):
    kind: ClassVar[str] = "bullet"
    required: ClassVar[tuple[str, ...]] = ("text",)

    text: Text
    default: bool = False


class Summary(_Part):
    kind: ClassVar[str] = "summary"
    required: ClassVar[tuple[str, ...]] = ("text",)

    text: Text


class _WithBullets(_Part):
    """An entry whose bullets are selected; ``max_bullets`` overrides the slot default."""

    title: Text
    lead: Text = ""
    max_bullets: int | None = None
    bullets: list[Bullet] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_bullets(self):
        if not self.bullets:
            raise ValueError(f"{self.kind} {self.id!r}: needs at least one bullet")
        if self.max_bullets is not None and self.max_bullets < 1:
            raise ValueError(f"{self.kind} {self.id!r}: max_bullets must be at least 1")
        dup = _first_duplicate(b.id for b in self.bullets)
        if dup is not None:
            raise ValueError(f"{self.kind} {self.id!r}: duplicate bullet id {dup!r}")
        return self

    def bullet(self, bullet_id: str) -> Bullet | None:
        return next((b for b in self.bullets if b.id == bullet_id), None)


class Entry(_WithBullets):
    """A project: a selectable entry, shown with its title, lead and selected bullets."""

    kind: ClassVar[str] = "project"
    required: ClassVar[tuple[str, ...]] = ("title", "lead")

    default: bool = False


class WorkEntry(_WithBullets):
    """A job: always shown, in file order; only its bullets are selected."""

    kind: ClassVar[str] = "work entry"
    required: ClassVar[tuple[str, ...]] = ("title", "period")

    period: Text


class Slots(_Strict):
    """How much the CV page holds."""

    projects: int = Field(3, ge=1)
    project_bullets: int = Field(2, ge=1)
    work_bullets: int = Field(5, ge=1)


class CvParts(_Strict):
    version: Text
    slots: Slots = Field(default_factory=Slots)
    header: dict[str, Text] = Field(default_factory=dict)
    summaries: list[Summary] = Field(default_factory=list)
    projects: list[Entry] = Field(default_factory=list)
    work: list[WorkEntry] = Field(default_factory=list)
    sidebar: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_library(self):
        if not self.version:
            raise ValueError("version is blank")
        for key, value in self.header.items():
            if not value:
                raise ValueError(f"header {key!r} is blank")
        if not self.summaries:
            raise ValueError("the library needs at least one summary")
        if not self.projects:
            raise ValueError("the library needs at least one project")
        for label, items in (("summary", self.summaries), ("project", self.projects),
                             ("work entry", self.work)):
            dup = _first_duplicate(item.id for item in items)
            if dup is not None:
                raise ValueError(f"duplicate {label} id {dup!r}")
        defaults = sum(p.default for p in self.projects)
        if defaults > self.slots.projects:
            raise ValueError(f"{defaults} projects are marked default, but slots.projects is "
                             f"{self.slots.projects}")
        for entry in [*self.projects, *self.work]:
            n, most = sum(b.default for b in entry.bullets), effective_max_bullets(self, entry)
            if n > most:
                raise ValueError(f"{entry.kind} {entry.id!r}: {n} default bullets, but at most {most} fit")
        return self

    def summary(self, summary_id: str) -> Summary | None:
        return next((s for s in self.summaries if s.id == summary_id), None)

    def project(self, project_id: str) -> Entry | None:
        return next((p for p in self.projects if p.id == project_id), None)

    def work_entry(self, work_id: str) -> WorkEntry | None:
        return next((w for w in self.work if w.id == work_id), None)


def _first_duplicate(ids: Iterable[str]) -> str | None:
    seen: set[str] = set()
    for ident in ids:
        if ident in seen:
            return ident
        seen.add(ident)
    return None


def effective_max_bullets(parts: CvParts, entry: Entry | WorkEntry) -> int:
    """The entry's own ``max_bullets``, else the slot default for its kind."""
    if entry.max_bullets is not None:
        return entry.max_bullets
    return parts.slots.work_bullets if isinstance(entry, WorkEntry) else parts.slots.project_bullets


def load_cv_parts(config_dir: Path | None = None) -> CvParts:
    """Read and validate ``cv-parts.yaml`` from the (profile-aware) config directory."""
    cfg = Path(config_dir) if config_dir else paths().config
    return CvParts.model_validate(_load_yaml(cfg / CV_PARTS_FILE))


def parts_fingerprint(parts: CvParts) -> str:
    """``version:hash`` of the whole library — a cache key that also moves on unbumped edits."""
    canonical = json.dumps(parts.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return f"{parts.version}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]}"


# --- selections ------------------------------------------------------------------------------


class EntryPick(_Strict):
    id: str
    bullets: list[str]  # bullet ids, in the order they are shown


class Selection(_Strict):
    """What a tailored CV shows, by id only."""

    summary: str
    projects: list[EntryPick]  # ordered, lead project first
    #: work entry id → bullet ids; an entry left out shows its default bullets, ``[]`` shows none.
    work: dict[str, list[str]] = Field(default_factory=dict)


class SelectionError(ValueError):
    """A selection that does not fit the library; the message has one line per problem."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


def _bullet_problems(parts: CvParts, entry: Entry | WorkEntry, ids: list[str]) -> list[str]:
    where = f"{entry.kind} {entry.id!r}"
    problems = [f"{where}: unknown bullet {b!r}" for b in ids if entry.bullet(b) is None]
    seen: set[str] = set()
    for b in ids:
        if b in seen:
            problems.append(f"{where}: bullet {b!r} selected twice")
        seen.add(b)
    most = effective_max_bullets(parts, entry)
    if len(ids) > most:
        problems.append(f"{where}: {len(ids)} bullets selected, but at most {most} fit")
    return problems


def _project_problems(parts: CvParts, picks: list[EntryPick]) -> list[str]:
    problems: list[str] = []
    if not picks:
        problems.append("no projects selected")
    elif len(picks) > parts.slots.projects:
        problems.append(f"{len(picks)} projects selected, but slots.projects is {parts.slots.projects}")
    seen: set[str] = set()
    for pick in picks:
        entry = parts.project(pick.id)
        if entry is None:
            problems.append(f"unknown project {pick.id!r}")
        elif pick.id in seen:
            problems.append(f"project {pick.id!r} selected twice")
        elif not pick.bullets:
            problems.append(f"project {pick.id!r}: no bullets selected")
        else:
            problems += _bullet_problems(parts, entry, pick.bullets)
        seen.add(pick.id)
    return problems


def validate_selection(parts: CvParts, selection: Selection) -> None:
    """Raise :class:`SelectionError` listing every way ``selection`` does not fit ``parts``."""
    problems: list[str] = []
    if parts.summary(selection.summary) is None:
        problems.append(f"unknown summary {selection.summary!r}")
    problems += _project_problems(parts, selection.projects)
    for work_id, ids in selection.work.items():
        entry = parts.work_entry(work_id)
        if entry is None:
            problems.append(f"unknown work entry {work_id!r}")
        else:
            problems += _bullet_problems(parts, entry, ids)
    if problems:
        raise SelectionError(problems)


def _default_bullets(parts: CvParts, entry: Entry | WorkEntry) -> list[str]:
    """The entry's ``default: true`` bullets, else its first ones up to its max."""
    marked = [b.id for b in entry.bullets if b.default]
    return marked or [b.id for b in entry.bullets[:effective_max_bullets(parts, entry)]]


def default_selection(parts: CvParts) -> Selection:
    """The CV as the owner would send it untailored: first summary, the default projects
    (else the first ``slots.projects``) and every entry's default bullets."""
    projects = [p for p in parts.projects if p.default] or parts.projects[:parts.slots.projects]
    return Selection(
        summary=parts.summaries[0].id,
        projects=[EntryPick(id=p.id, bullets=_default_bullets(parts, p)) for p in projects],
        work={w.id: _default_bullets(parts, w) for w in parts.work},
    )


# --- resolving and rendering -----------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedEntry:
    title: str
    lead: str
    bullets: list[str]
    period: str | None = None


@dataclass(frozen=True)
class TailoredCV:
    """A selection turned into text: what a renderer sees, with no ids left in it."""

    header: dict[str, str]
    summary: str
    projects: list[ResolvedEntry]
    work: list[ResolvedEntry]
    sidebar: dict[str, Any]
    version: str
    selection: Selection


def _texts(entry: Entry | WorkEntry, ids: list[str]) -> list[str]:
    return [entry.bullet(b).text for b in ids]  # type: ignore[union-attr]  # validated


def resolve(parts: CvParts, selection: Selection) -> TailoredCV:
    """Validate ``selection`` and look up every text it names."""
    validate_selection(parts, selection)
    projects = []
    for pick in selection.projects:
        entry = parts.project(pick.id)
        assert entry is not None  # validated
        projects.append(ResolvedEntry(entry.title, entry.lead, _texts(entry, pick.bullets)))
    work = []
    for w in parts.work:
        ids = selection.work[w.id] if w.id in selection.work else _default_bullets(parts, w)
        work.append(ResolvedEntry(w.title, w.lead, _texts(w, ids), w.period))
    summary = parts.summary(selection.summary)
    assert summary is not None  # validated
    return TailoredCV(header=dict(parts.header), summary=summary.text, projects=projects, work=work,
                      sidebar=parts.sidebar, version=parts.version, selection=selection)


def _flat(value: Any) -> str:
    """One line for a sidebar value: lists comma-joined, mappings as ``key: value; ...``."""
    if isinstance(value, list):
        return ", ".join(_flat(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_flat(v)}" for k, v in value.items())
    return str(value)


def _entry_blocks(entry: ResolvedEntry) -> list[str]:
    heading = f"### {entry.title}" + (f" ({entry.period})" if entry.period else "")
    blocks = [heading]
    if entry.lead:
        blocks.append(entry.lead)
    if entry.bullets:
        blocks.append("\n".join(f"- {b}" for b in entry.bullets))
    return blocks


def to_markdown(cv: TailoredCV) -> str:
    """A readable markdown CV — what the owner copies into the Canva layout by hand."""
    blocks = [f"# {cv.header.get('name', 'CV')}"]
    if cv.header.get("title"):
        blocks.append(cv.header["title"])
    contacts = [v for k, v in cv.header.items() if k not in ("name", "title")]
    if contacts:
        blocks.append(" · ".join(contacts))
    blocks += ["## PROFILE", cv.summary, "## SAMPLE PROJECTS"]
    for entry in cv.projects:
        blocks += _entry_blocks(entry)
    blocks.append("## WORK EXPERIENCE")
    for entry in cv.work:
        blocks += _entry_blocks(entry)
    if cv.sidebar:
        blocks.append("## SIDEBAR")
        blocks.append("\n".join(f"- {k.replace('_', ' ').capitalize()}: {_flat(v)}"
                                for k, v in cv.sidebar.items()))
    return "\n\n".join(blocks) + "\n"
