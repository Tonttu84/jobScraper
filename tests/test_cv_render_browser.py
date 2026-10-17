"""The CV renderer printed by a real headless Chromium (``jobscraper.cv.render.render_pdf``).

Also the part heights (``jobscraper.cv.measure``): the additive height model against the real
main column, and the fitter against a real overflowing page.

Runs with ``fonts=False`` so nothing leaves the machine (``set_content``, no server, no web
fonts); the fallback fonts are close enough in size for the fit checks. Skips when Playwright's
browser is not installed (``python -m playwright install chromium``). Listed in scripts/check.sh's
``BROWSER`` suites, which run on one worker after the parallel part.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from jobscraper.cv import measure as measure_mod
from jobscraper.cv.fit import fit_selection, parse_drop_order
from jobscraper.cv.measure import cached_heights, heights_path, main_height_mm, measure_heights
from jobscraper.cv.parts import EntryPick, Selection, default_selection, load_cv_parts, resolve
from jobscraper.cv.render import RenderUnavailable, measure_overflow, render_pdf, to_html

playwright_api = pytest.importorskip("playwright.sync_api")

FIXTURE = Path(__file__).parent / "fixtures" / "cv-parts.yaml"


@pytest.fixture(scope="module", autouse=True)
def _chromium():
    with playwright_api.sync_playwright() as pw:
        try:
            pw.chromium.launch().close()
        except playwright_api.Error as exc:  # browser binary missing
            pytest.skip(f"chromium not installed for playwright: {exc}")


@pytest.fixture
def parts(tmp_path):
    shutil.copyfile(FIXTURE, tmp_path / "cv-parts.yaml")
    return load_cv_parts(tmp_path)


def _stuffed(parts):
    """The fixture library with every bullet far too long for the page."""
    raw = parts.model_dump()
    long = " ".join(["A deliberately long bullet that wraps over many lines of the page."] * 12)
    for entry in [*raw["projects"], *raw["work"]]:
        for bullet in entry["bullets"]:
            bullet["text"] = long
    return type(parts).model_validate(raw)


def test_the_fixture_cv_is_one_page(parts, tmp_path):
    pdf = tmp_path / "out" / "cv.pdf"
    result = render_pdf(to_html(resolve(parts, default_selection(parts)), fonts=False), pdf)
    assert result.pages == 1 and result.overflow_mm == 0.0
    assert pdf.read_bytes().startswith(b"%PDF")
    assert not pdf.with_suffix(".png").exists()


def test_too_much_text_overflows(parts, tmp_path):
    stuffed = _stuffed(parts)
    result = render_pdf(to_html(resolve(stuffed, default_selection(stuffed)), fonts=False),
                        tmp_path / "long.pdf")
    assert result.overflow_mm > 0
    assert result.pages >= 2


def test_measure_overflow_agrees_with_the_print(parts, tmp_path):
    cv = resolve(parts, default_selection(parts))
    assert measure_overflow(cv, fonts=False) == 0.0
    stuffed = _stuffed(parts)
    stuffed_cv = resolve(stuffed, default_selection(stuffed))
    printed = render_pdf(to_html(stuffed_cv, fonts=False), tmp_path / "long.pdf").overflow_mm
    assert printed > 0
    assert measure_overflow(stuffed_cv, fonts=False) == printed


def test_png_is_written_at_the_reference_width(parts, tmp_path):
    png = tmp_path / "cv.png"
    render_pdf(to_html(resolve(parts, default_selection(parts)), fonts=False), tmp_path / "cv.pdf", png)
    data = png.read_bytes()
    assert data.startswith(b"\x89PNG")
    width = int.from_bytes(data[16:20], "big")  # IHDR
    assert abs(width - 1414) <= 2


def test_a_missing_chromium_is_render_unavailable(parts, tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "no-browsers"))
    with pytest.raises(RenderUnavailable, match="playwright install chromium"):
        render_pdf("<html></html>", tmp_path / "x.pdf")


# ------------------------------------------------------------------- part heights


def _varied(parts):
    """The fixture library with bullets and leads of one to four lines, so wrapping counts."""
    raw = parts.model_dump()
    filler = "Built and measured a thing with care, then wrote it up for the team. "
    n = 0
    for entry in [*raw["projects"], *raw["work"]]:
        if entry.get("lead"):
            entry["lead"] = entry["lead"] + " " + filler * (n % 2)
        for bullet in entry["bullets"]:
            bullet["text"] = f"{bullet['text']} " + filler * (n % 5)
            n += 1
    return type(parts).model_validate(raw)


def _selections():
    return [
        Selection(summary="systems",
                  projects=[EntryPick(id="shell", bullets=["signals", "pipes", "heredoc"]),
                            EntryPick(id="webserver", bullets=["cgi"])],
                  work={"poker": ["risk", "math"], "shop": []}),
        Selection(summary="default", projects=[EntryPick(id="game", bullets=["dda"])],
                  work={"shop": ["service"]}),
    ]


@pytest.mark.parametrize("library", [lambda p: p, _varied], ids=["fixture", "varied"])
def test_the_height_model_predicts_the_real_main_column(parts, library):
    parts = library(parts)
    heights = measure_heights(parts, fonts=False)
    for selection in [default_selection(parts), *_selections()]:
        real = main_height_mm(resolve(parts, selection), fonts=False)
        predicted = heights.predict_mm(parts, selection)
        assert abs(predicted - real) <= 1.0, (selection, predicted, real)


def test_the_predicted_overflow_matches_the_print(parts):
    long = _varied(_stuffed(parts))
    heights = measure_heights(long, fonts=False)
    selection = default_selection(long)
    overflow = measure_overflow(resolve(long, selection), fonts=False)
    assert overflow > 0
    assert abs(heights.predict_mm(long, selection) - heights.page_mm - overflow) <= 1.0


def test_the_fitter_fits_a_real_overflowing_page(parts):
    long = _varied(parts)
    raw = long.model_dump()
    raw["summaries"][1]["text"] = " ".join(["A much longer summary sentence for the test."] * 30)
    long = type(parts).model_validate(raw)
    heights = measure_heights(long, fonts=False)
    selection = _selections()[0]
    assert heights.predict_mm(long, selection) > heights.page_mm
    trims, _ = parse_drop_order(long, selection, ["poker/math", "shell/heredoc"])
    fitted = fit_selection(long, selection, trims, heights,
                           lambda cv: measure_overflow(cv, fonts=False))
    assert fitted.dropped[:2] == trims and fitted.overflow_mm <= 0.5
    assert fitted.renders == 1  # the model was right the first time


def test_the_heights_cache_is_written_once_and_reused(parts, tmp_path, monkeypatch):
    calls = []
    real = measure_mod.measure_heights

    def counting(p, *, fonts=True):
        calls.append(fonts)
        return real(p, fonts=fonts)

    monkeypatch.setattr(measure_mod, "measure_heights", counting)
    cache = tmp_path / "cv"
    first = cached_heights(parts, cache, fonts=False)
    assert heights_path(cache, parts).is_file()
    assert cached_heights(parts, cache, fonts=False) == first
    assert calls == [False]
