"""The page fitter: trims a selection until it fits one page, without asking the model again.

The tailor model picks parts knowing their heights (:mod:`jobscraper.cv.measure`) and also ranks
the bullets it would give up first (``drop_order``). :func:`fit_selection` drops them in that
order while the height model predicts an overflow; when the order runs out, a fixed rule takes
over: the last bullet of the lowest-placed project that has more than one, then the last bullet
of the work entry with the most bullets (never its first), and as the very last resort the
lowest-placed project itself. A project always keeps one bullet and one project always stays.

The prediction is then checked by one real render (``measure``, the overflow in mm); if the page
still overflows, the model's error is added to every later prediction and dropping goes on in
the same order, for at most :data:`MAX_VERIFY_RENDERS` renders. Pure apart from ``measure``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict

from jobscraper.cv.measure import HeightModel
from jobscraper.cv.parts import (
    CvParts,
    EntryPick,
    Selection,
    TailoredCV,
    WorkEntry,
    _default_bullets,
    resolve,
)

#: Overflow up to this many mm counts as fitting: rounding, not a line of text.
FIT_TOLERANCE_MM = 0.5
#: Real renders that check the model's prediction, per tailoring.
MAX_VERIFY_RENDERS = 3


class Trim(BaseModel):
    """One thing the fitter took out: a bullet of an entry, or (``bullet`` None) a whole project."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["project", "work"]
    entry: str
    bullet: str | None = None

    @property
    def label(self) -> str:
        """``<entry id>/<bullet id>``, as ``drop_order`` names it; a project's id alone."""
        return f"{self.entry}/{self.bullet}" if self.bullet else self.entry


@dataclass
class Fitted:
    selection: Selection
    dropped: list[Trim] = field(default_factory=list)  # in the order they were dropped
    predicted_mm: float = 0.0  # the model's main-column height for the fitted selection
    overflow_mm: float | None = None  # the last real render's overflow; None without one
    renders: int = 0


def _work_bullets(parts: CvParts, selection: Selection) -> dict[str, list[str]]:
    """Every work entry's shown bullets (an entry left out of the selection: its defaults)."""
    return {w.id: list(selection.work[w.id]) if w.id in selection.work else _default_bullets(parts, w)
            for w in parts.work}


def _work_minimum(parts: CvParts, entry: WorkEntry) -> int:
    """A work entry keeps one bullet, unless the untailored CV shows it with none."""
    return min(1, len(_default_bullets(parts, entry)))


def parse_drop_order(parts: CvParts, selection: Selection,
                     items: Iterable[str]) -> tuple[list[Trim], list[str]]:
    """The ``drop_order`` entries that name a selected bullet of a selected entry, as trims;
    and one note per entry that does not (ignored: the fitter has its own fallback)."""
    projects = {p.id: p.bullets for p in selection.projects}
    work = _work_bullets(parts, selection)
    trims: list[Trim] = []
    notes: list[str] = []
    for item in items:
        entry, sep, bullet = item.partition("/")
        if sep and bullet in projects.get(entry, []):
            trim = Trim(kind="project", entry=entry, bullet=bullet)
        elif sep and bullet in work.get(entry, []):
            trim = Trim(kind="work", entry=entry, bullet=bullet)
        else:
            notes.append(f'drop_order "{item}" is not a selected bullet; ignored')
            continue
        if trim in trims:
            notes.append(f'drop_order "{item}" is listed twice; ignored')
            continue
        trims.append(trim)
    return trims, notes


class _State:
    """The selection being trimmed: projects in order, every work entry's bullets."""

    def __init__(self, parts: CvParts, selection: Selection) -> None:
        self.parts = parts
        self.summary = selection.summary
        self.projects = [(p.id, list(p.bullets)) for p in selection.projects]
        self.work = _work_bullets(parts, selection)

    def selection(self) -> Selection:
        return Selection(summary=self.summary,
                         projects=[EntryPick(id=i, bullets=list(b)) for i, b in self.projects],
                         work={i: list(b) for i, b in self.work.items()})

    def _bullets(self, trim: Trim) -> list[str]:
        if trim.kind == "work":
            return self.work[trim.entry]
        return dict(self.projects)[trim.entry]

    def can_drop(self, trim: Trim) -> bool:
        bullets = self._bullets(trim)
        least = 1 if trim.kind == "project" else _work_minimum(self.parts, self.parts.work_entry(trim.entry))
        return trim.bullet in bullets and len(bullets) > least

    def drop(self, trim: Trim) -> None:
        if trim.bullet is None:
            self.projects = [(i, b) for i, b in self.projects if i != trim.entry]
        else:
            self._bullets(trim).remove(trim.bullet)

    def fallback(self) -> Trim | None:
        """The fixed rule once ``drop_order`` is spent (see the module docstring)."""
        for project_id, bullets in reversed(self.projects):
            if len(bullets) > 1:
                return Trim(kind="project", entry=project_id, bullet=bullets[-1])
        longest = max(reversed(self.parts.work), key=lambda w: len(self.work[w.id]), default=None)
        if longest is not None and len(self.work[longest.id]) > 1:
            return Trim(kind="work", entry=longest.id, bullet=self.work[longest.id][-1])
        if len(self.projects) > 1:
            return Trim(kind="project", entry=self.projects[-1][0])
        return None


def fit_selection(parts: CvParts, selection: Selection, drop_order: list[Trim],
                  heights: HeightModel, measure: Callable[[TailoredCV], float] | None = None, *,
                  max_renders: int = MAX_VERIFY_RENDERS,
                  tolerance: float = FIT_TOLERANCE_MM) -> Fitted:
    """``selection`` trimmed to fit the page under ``heights``, checked by ``measure`` (the real
    overflow in mm of a CV; None: the model alone decides). ``drop_order``: from
    :func:`parse_drop_order`, most expendable first."""
    state = _State(parts, selection)
    pending = list(drop_order)
    fitted = Fitted(selection=selection)
    error = 0.0  # how much taller the real page was than the model said, at the last render

    def predicted() -> float:
        return heights.predict_mm(parts, state.selection())

    def trim_to_fit() -> bool:
        """Drop until the corrected prediction fits or nothing is left; whether anything went."""
        changed = False
        while predicted() + error - heights.page_mm > tolerance:
            while pending and not state.can_drop(pending[0]):
                pending.pop(0)
            trim = pending.pop(0) if pending else state.fallback()
            if trim is None:
                break
            state.drop(trim)
            fitted.dropped.append(trim)
            changed = True
        return changed

    trim_to_fit()
    while measure is not None and fitted.renders < max_renders:
        fitted.overflow_mm = measure(resolve(parts, state.selection()))
        fitted.renders += 1
        if fitted.overflow_mm <= tolerance:
            break
        error = heights.page_mm + fitted.overflow_mm - predicted()
        if not trim_to_fit():
            break
    if fitted.dropped:
        fitted.selection = state.selection()
    fitted.predicted_mm = predicted()
    return fitted
