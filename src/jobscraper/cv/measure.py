"""Part heights: how many millimetres each CV part takes on the page, measured once per library.

The tailor prompt shows every part's height and the page budget, so the model can add up its
selection instead of guessing, and :mod:`jobscraper.cv.fit` trims an answer that still overflows
without asking again. The heights come from a headless Chromium laying out the real stylesheet
in the real main column (:func:`measure_heights`): one page holds every summary, project and work
entry of the library with all their bullets, each marked with a ``data-m`` attribute.

The model is additive: ``predicted = overhead + summary + Σ project (header + bullets) +
Σ work entry (header + bullets)``. A project's header is its title and lead with the spacing down
to its first bullet, plus the gap below the project (the overhead pays that back once, for the
last project, which has none); a work entry shown without bullets counts its ``bare`` height
instead. The overhead (paddings, headings, the gaps between sections and between work entries)
is what the untailored CV's real main column measures beyond the sum of its parts, so the model
is exact for the untailored CV and off only by sub-pixel rounding for any other selection
(test_cv_render_browser.py holds it to 1 mm).

Cached as JSON at ``data/cv/heights-<library fingerprint>.json`` (:func:`cached_heights`); the
file also records the layout (stylesheet, fonts), and a different layout is measured again.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from pydantic import BaseModel, ValidationError

from jobscraper.cv import render
from jobscraper.cv.parts import (
    CvParts,
    ResolvedEntry,
    Selection,
    TailoredCV,
    _default_bullets,
    default_selection,
    parts_fingerprint,
    resolve,
)

#: The A4 page the CV must fit, in mm.
A4_MM = 297.0
#: Bump when the measuring page or the model changes in a way the stylesheet hash cannot see.
MEASURE_VERSION = "1"

_BOXES_JS = """() => {
  const out = {};
  for (const el of document.querySelectorAll('[data-m]')) {
    const r = el.getBoundingClientRect();
    const ul = el.querySelector(':scope > ul');
    out[el.dataset.m] = {top: r.top, height: r.height,
                         ul: ul ? ul.getBoundingClientRect().top : null,
                         gap: parseFloat(getComputedStyle(el).marginBottom) || 0};
  }
  return out;
}"""

# A flex item is stretched to the page's 297 mm; aligned to the start it is as tall as its content.
_MAIN_JS = """() => {
  const main = document.querySelector('.main');
  main.style.alignSelf = 'flex-start';
  return main.getBoundingClientRect().height;
}"""


class EntryHeights(BaseModel):
    """A project's or work entry's heights in mm."""

    header: float  # title (and period) and lead, down to the first bullet; projects: + the gap
    bare: float | None = None  # work entries: the entry shown without any bullet
    bullets: dict[str, float]


class HeightModel(BaseModel):
    """Every part's height in mm, and the fixed overhead of the main column."""

    fingerprint: str
    layout: str
    page_mm: float = A4_MM
    overhead_mm: float
    summaries: dict[str, float]
    projects: dict[str, EntryHeights]
    work: dict[str, EntryHeights]

    @property
    def budget_mm(self) -> float:
        """What the summary, projects and work entries may take together."""
        return self.page_mm - self.overhead_mm

    def content_mm(self, parts: CvParts, selection: Selection) -> float:
        """The summary, projects and work entries of ``selection`` (a work entry left out of it
        shows its default bullets, as :func:`~jobscraper.cv.parts.resolve` does)."""
        total = self.summaries[selection.summary]
        for pick in selection.projects:
            entry = self.projects[pick.id]
            total += entry.header + sum(entry.bullets[b] for b in pick.bullets)
        for w in parts.work:
            ids = selection.work[w.id] if w.id in selection.work else _default_bullets(parts, w)
            entry = self.work[w.id]
            total += (entry.header if ids else entry.bare or 0.0) + sum(entry.bullets[b] for b in ids)
        return total

    def predict_mm(self, parts: CvParts, selection: Selection) -> float:
        """The main column's height for ``selection``: overhead + its parts."""
        return self.overhead_mm + self.content_mm(parts, selection)

    def untailored_mm(self, parts: CvParts) -> float:
        return self.content_mm(parts, default_selection(parts))


def layout_signature(fonts: bool) -> str:
    """What the heights depend on besides the library: the stylesheet, the fonts, this module."""
    key = "\n".join((MEASURE_VERSION, render._CSS, render.FONT_DISPLAY, render.FONT_TEXT,
                     render.FONT_BODY, render.FONT_WEIGHTS, f"fonts={fonts}"))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def measurement_html(parts: CvParts, *, fonts: bool = True) -> str:
    """One page with every part of the library in the main column, each marked ``data-m``:
    ``summary:<id>``, ``project:<id>`` with ``pbullet:<id>/<bullet>``, ``work:<id>`` with
    ``wbullet:<id>/<bullet>``, and ``workbare:<id>`` (the work entry without bullets)."""
    summaries = "".join(render._summary(s.text, f"summary:{s.id}") for s in parts.summaries)
    projects = "".join(
        render._project(ResolvedEntry(p.title, p.lead, [b.text for b in p.bullets]), f"project:{p.id}",
                        [f"pbullet:{p.id}/{b.id}" for b in p.bullets])
        for p in parts.projects)
    jobs = "".join(
        render._job(ResolvedEntry(w.title, w.lead, [b.text for b in w.bullets], w.period), f"work:{w.id}",
                    [f"wbullet:{w.id}/{b.id}" for b in w.bullets])
        for w in parts.work)
    jobs += "".join(render._job(ResolvedEntry(w.title, w.lead, [], w.period), f"workbare:{w.id}")
                    for w in parts.work)
    # The sidebar has a fixed width, so an empty one leaves the main column as wide as on the CV.
    body = ('<div class="page"><aside class="sidebar"></aside>'
            + render._main_column(summaries, projects, jobs) + "</div>")
    return render._document(body, fonts=fonts, title="CV part heights")


def heights_from_boxes(parts: CvParts, boxes: dict[str, dict], main_px: float, *,
                       layout: str) -> HeightModel:
    """The model from the measuring page's boxes (px, by ``data-m``) and the untailored CV's
    main-column height (px)."""
    def mm(px: float) -> float:
        return px * render._MM_PER_PX

    def height(mark: str) -> float:
        return mm(boxes[mark]["height"])

    def header(mark: str) -> float:
        return mm(boxes[mark]["ul"] - boxes[mark]["top"])

    gap = max(mm(box["gap"]) for mark, box in boxes.items() if mark.startswith("project:"))
    model = HeightModel(
        fingerprint=parts_fingerprint(parts), layout=layout, overhead_mm=0.0,
        summaries={s.id: height(f"summary:{s.id}") for s in parts.summaries},
        projects={p.id: EntryHeights(header=header(f"project:{p.id}") + gap,
                                     bullets={b.id: height(f"pbullet:{p.id}/{b.id}") for b in p.bullets})
                  for p in parts.projects},
        work={w.id: EntryHeights(header=header(f"work:{w.id}"), bare=height(f"workbare:{w.id}"),
                                 bullets={b.id: height(f"wbullet:{w.id}/{b.id}") for b in w.bullets})
              for w in parts.work},
    )
    return model.model_copy(update={"overhead_mm": mm(main_px) - model.untailored_mm(parts)})


def measure_heights(parts: CvParts, *, fonts: bool = True) -> HeightModel:
    """Measure every part in one headless Chromium session (two documents: the measuring page,
    then the untailored CV for the overhead). ``fonts=True`` loads the web fonts, as the print
    does. Raises :class:`~jobscraper.cv.render.RenderUnavailable` without a browser."""
    with render._chromium_page(measurement_html(parts, fonts=fonts)) as page:
        boxes = page.evaluate(_BOXES_JS)
        render.load(page, render.to_html(resolve(parts, default_selection(parts)), fonts=fonts))
        main_px = page.evaluate(_MAIN_JS)
    return heights_from_boxes(parts, boxes, main_px, layout=layout_signature(fonts))


def main_height_mm(cv: TailoredCV, *, fonts: bool = True) -> float:
    """The real main column's height of ``cv`` in mm, as laid out on the printed page."""
    with render._chromium_page(render.to_html(cv, fonts=fonts)) as page:
        return page.evaluate(_MAIN_JS) * render._MM_PER_PX


# ------------------------------------------------------------------------------ cache


def heights_path(cache_dir: Path, parts: CvParts) -> Path:
    """``<cache_dir>/heights-<fingerprint>.json``; the fingerprint's ``:`` is not allowed in a
    Windows file name, so it becomes ``_``."""
    return Path(cache_dir) / f"heights-{parts_fingerprint(parts).replace(':', '_')}.json"


def _read(path: Path, parts: CvParts, fonts: bool) -> HeightModel | None:
    if not path.is_file():
        return None
    try:
        model = HeightModel.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError:
        return None
    current = model.fingerprint == parts_fingerprint(parts) and model.layout == layout_signature(fonts)
    return model if current else None


def cached_heights(parts: CvParts, cache_dir: Path, *, fonts: bool = True, build: bool = True,
                   force: bool = False) -> HeightModel | None:
    """The heights of ``parts`` from the cache, else measured and cached (``build``), else None.
    ``force`` measures again. Raises :class:`~jobscraper.cv.render.RenderUnavailable` when it
    has to measure without a browser."""
    path = heights_path(cache_dir, parts)
    model = None if force else _read(path, parts, fonts)
    if model is not None or not build:
        return model
    model = measure_heights(parts, fonts=fonts)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return model
