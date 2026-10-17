"""The deterministic page fitter (``jobscraper.cv.fit``): no browser, no AI.

Every part of the fixture library is 10 mm high here (``flat_heights``), so an overflow of N mm
needs ceil(N / 10) bullets dropped; the overhead is chosen per test to set the overflow.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from jobscraper.cv.fit import MAX_VERIFY_RENDERS, Trim, fit_selection, parse_drop_order
from jobscraper.cv.parts import EntryPick, Selection, load_cv_parts

FIXTURE = Path(__file__).parent / "fixtures" / "cv-parts.yaml"
PAGE = 297.0


@pytest.fixture
def parts(tmp_path):
    shutil.copyfile(FIXTURE, tmp_path / "cv-parts.yaml")
    return load_cv_parts(tmp_path)


def _sel(projects=None, work=None) -> Selection:
    """Selection A: 130 mm of content at 10 mm per part."""
    return Selection(
        summary="systems",
        projects=projects or [EntryPick(id="webserver", bullets=["parse", "poll"]),
                              EntryPick(id="shell", bullets=["pipes", "signals", "builtins"])],
        work={"poker": ["discipline", "math"], "shop": ["service"]} if work is None else work)


@pytest.fixture
def model(parts, flat_heights):
    """``model(overflow)``: the height model under which selection A overflows by ``overflow``."""
    def _make(overflow: float):
        heights = flat_heights(parts, mm=10.0)
        content = heights.content_mm(parts, _sel())
        assert content == 130.0
        return heights.model_copy(update={"overhead_mm": PAGE - content + overflow})

    return _make


def _trims(*labels: str) -> list[Trim]:
    out = []
    for label in labels:
        entry, _, bullet = label.partition("/")
        kind = "work" if entry in ("poker", "shop") else "project"
        out.append(Trim(kind=kind, entry=entry, bullet=bullet or None))
    return out


def _labels(trims: list[Trim]) -> list[str]:
    return [t.label for t in trims]


class OffBy:
    """A real-render stand-in: the page is ``error`` mm taller than the model says."""

    def __init__(self, parts, heights, error: float):
        self.parts, self.heights, self.error = parts, heights, error
        self.calls: list = []

    def __call__(self, cv) -> float:
        self.calls.append(cv)
        return max(0.0, round(self.heights.predict_mm(self.parts, cv.selection) + self.error - PAGE, 1))


# ------------------------------------------------------------------------- the fitter


def test_a_selection_that_fits_is_unchanged(parts, model):
    heights = model(0.0)
    measure = OffBy(parts, heights, 0.0)
    fitted = fit_selection(parts, _sel(), _trims("poker/math"), heights, measure)
    assert fitted.selection == _sel() and fitted.dropped == []
    assert fitted.renders == 1 and fitted.overflow_mm == 0.0
    assert fitted.predicted_mm == PAGE


def test_without_a_measure_nothing_is_rendered(parts, model):
    fitted = fit_selection(parts, _sel(), [], model(0.0))
    assert fitted.renders == 0 and fitted.overflow_mm is None and fitted.dropped == []


def test_the_drop_order_is_followed_until_the_page_fits(parts, model):
    fitted = fit_selection(parts, _sel(), _trims("poker/math", "webserver/poll", "shell/pipes"),
                           model(15.0))
    assert _labels(fitted.dropped) == ["poker/math", "webserver/poll"]
    assert fitted.selection.projects[0].bullets == ["parse"]
    assert fitted.selection.work == {"poker": ["discipline"], "shop": ["service"]}
    assert fitted.predicted_mm == PAGE - 5.0


def test_a_tiny_predicted_overflow_is_tolerated(parts, model):
    assert fit_selection(parts, _sel(), _trims("poker/math"), model(0.5)).dropped == []


def test_a_project_and_a_work_entry_keep_one_bullet(parts, model, flat_heights):
    selection = _sel(projects=[EntryPick(id="webserver", bullets=["parse", "poll"]),
                               EntryPick(id="game", bullets=["dda"])])
    heights = flat_heights(parts, mm=10.0)
    heights = heights.model_copy(
        update={"overhead_mm": PAGE - heights.content_mm(parts, selection) + 15.0})
    order = _trims("game/dda", "shop/service", "webserver/parse", "webserver/poll",
                   "poker/discipline")
    fitted = fit_selection(parts, selection, order, heights)
    assert _labels(fitted.dropped) == ["webserver/parse", "poker/discipline"]
    assert [(p.id, p.bullets) for p in fitted.selection.projects] == [("webserver", ["poll"]),
                                                                      ("game", ["dda"])]


def test_the_fallback_trims_projects_bottom_up_then_the_longest_work_entry(parts, model):
    fitted = fit_selection(parts, _sel(), [], model(35.0))
    assert _labels(fitted.dropped) == ["shell/builtins", "shell/signals", "webserver/poll",
                                       "poker/math"]


def test_the_fallback_follows_a_short_drop_order(parts, model):
    fitted = fit_selection(parts, _sel(), _trims("webserver/parse"), model(25.0))
    assert _labels(fitted.dropped) == ["webserver/parse", "shell/builtins", "shell/signals"]


def test_the_last_resort_drops_the_lowest_project(parts, model):
    fitted = fit_selection(parts, _sel(), [], model(45.0))
    assert _labels(fitted.dropped)[-1] == "shell"
    assert fitted.dropped[-1] == Trim(kind="project", entry="shell")
    assert [p.id for p in fitted.selection.projects] == ["webserver"]


def test_when_nothing_is_left_to_drop_the_fitter_stops(parts, model):
    heights = model(500.0)
    measure = OffBy(parts, heights, 0.0)
    fitted = fit_selection(parts, _sel(), [], heights, measure)
    assert [(p.id, p.bullets) for p in fitted.selection.projects] == [("webserver", ["parse"])]
    assert fitted.selection.work == {"poker": ["discipline"], "shop": ["service"]}
    assert fitted.predicted_mm > PAGE
    assert fitted.renders == 1 and fitted.overflow_mm > 0  # nothing changed: no second render


def test_a_model_that_is_off_is_corrected_by_one_more_render(parts, model):
    heights = model(18.0)
    measure = OffBy(parts, heights, 3.0)
    fitted = fit_selection(parts, _sel(), _trims("poker/math", "webserver/poll", "shell/pipes",
                                                 "shell/signals"), heights, measure)
    # The model alone stops after two drops (295 mm); the render says 298 mm, 3 mm more.
    assert _labels(fitted.dropped) == ["poker/math", "webserver/poll", "shell/pipes"]
    assert fitted.renders == 2 and len(measure.calls) == 2
    assert fitted.overflow_mm == 0.0
    # The renders saw the CV as fitted so far.
    assert measure.calls[0].work[0].bullets == ["Kept detailed records."]


def test_verify_renders_are_capped(parts, model):
    heights = model(15.0)
    calls = []

    def always_over(cv):
        calls.append(cv)
        return 5.0

    fitted = fit_selection(parts, _sel(), [], heights, always_over)
    assert len(calls) == MAX_VERIFY_RENDERS == fitted.renders == 3
    assert fitted.overflow_mm == 5.0
    assert len(fitted.dropped) >= 4  # each overflowing render bought more drops


# ------------------------------------------------------------------------ drop_order


def test_parse_drop_order_keeps_selected_bullets_and_notes_the_rest(parts):
    trims, notes = parse_drop_order(parts, _sel(), [
        "poker/math", "shell/heredoc", "ghost/x", "webserver", "poker/math", "shop/service",
        "webserver/poll"])
    assert _labels(trims) == ["poker/math", "shop/service", "webserver/poll"]
    assert [t.kind for t in trims] == ["work", "work", "project"]
    assert notes == [
        'drop_order "shell/heredoc" is not a selected bullet; ignored',
        'drop_order "ghost/x" is not a selected bullet; ignored',
        'drop_order "webserver" is not a selected bullet; ignored',
        'drop_order "poker/math" is listed twice; ignored',
    ]


def test_parse_drop_order_uses_the_default_bullets_of_an_omitted_work_entry(parts):
    trims, notes = parse_drop_order(parts, _sel(work={}), ["poker/discipline", "poker/math"])
    assert _labels(trims) == ["poker/discipline"]
    assert notes == ['drop_order "poker/math" is not a selected bullet; ignored']


def test_trim_labels():
    assert Trim(kind="project", entry="shell").label == "shell"
    assert Trim(kind="work", entry="poker", bullet="math").label == "poker/math"
