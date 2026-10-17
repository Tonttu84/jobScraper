"""Part heights (``jobscraper.cv.measure``) without a browser: the height model's arithmetic, the
measuring page, the cache, and ``jobscraper cv-heights``. Playwright is faked here; the real
measurement (and the model's accuracy against a real render) is test_cv_render_browser.py.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from jobscraper import cli as cli_mod
from jobscraper.config import ROOT
from jobscraper.cv import measure as measure_mod
from jobscraper.cv.measure import (
    HeightModel,
    cached_heights,
    heights_from_boxes,
    heights_path,
    layout_signature,
    main_height_mm,
    measure_heights,
    measurement_html,
)
from jobscraper.cv.parts import (
    EntryPick,
    Selection,
    default_selection,
    load_cv_parts,
    parts_fingerprint,
    resolve,
)
from jobscraper.cv.render import _MM_PER_PX, RenderUnavailable

FIXTURE = Path(__file__).parent / "fixtures" / "cv-parts.yaml"
runner = CliRunner()


@pytest.fixture
def parts(tmp_path):
    shutil.copyfile(FIXTURE, tmp_path / "cv-parts.yaml")
    return load_cv_parts(tmp_path)


def _px(mm: float) -> float:
    return mm / _MM_PER_PX


# ----------------------------------------------------------------------- the model


def test_content_prediction_and_budget(parts, flat_heights):
    heights = flat_heights(parts, mm=2.0, overhead_mm=50.0)
    default = default_selection(parts)
    # summary 2 + webserver 2+4 + game 2+2 + poker 2+2 + shop 2+2
    assert heights.content_mm(parts, default) == 20.0 == heights.untailored_mm(parts)
    assert heights.predict_mm(parts, default) == 70.0
    assert heights.budget_mm == 247.0


def test_an_omitted_work_entry_counts_its_defaults_and_an_empty_one_its_bare_header(parts, flat_heights):
    heights = flat_heights(parts, mm=2.0)
    shop = heights.work["shop"].model_copy(update={"bare": 0.5})
    heights = heights.model_copy(update={"work": {**heights.work, "shop": shop}})
    selection = Selection(summary="systems", projects=[EntryPick(id="shell", bullets=["pipes"])],
                          work={"shop": []})
    # systems 2 + shell 2+2 + poker (defaults: discipline) 2+2 + shop bare 0.5
    assert heights.content_mm(parts, selection) == 10.5


def test_layout_signature_follows_the_fonts_and_the_css(monkeypatch):
    assert layout_signature(True) != layout_signature(False)
    before = layout_signature(True)
    monkeypatch.setattr(measure_mod.render, "_CSS", measure_mod.render._CSS + "\n.x { }")
    assert layout_signature(True) != before


# ------------------------------------------------------------------- the measuring page


def _marks(html: str) -> list[str]:
    return re.findall(r'data-m="([^"]+)"', html)


def test_the_measuring_page_marks_every_part(parts):
    html = measurement_html(parts, fonts=False)
    marks = _marks(html)
    assert "summary:default" in marks and "summary:systems" in marks
    for p in parts.projects:
        assert f"project:{p.id}" in marks
        assert all(f"pbullet:{p.id}/{b.id}" in marks for b in p.bullets)
    for w in parts.work:
        assert f"work:{w.id}" in marks and f"workbare:{w.id}" in marks
        assert all(f"wbullet:{w.id}/{b.id}" in marks for b in w.bullets)
    # Every bullet of every entry, in the real column: the CV's own classes and stylesheet.
    assert "Ran CGI scripts." in html and "Supported heredocs." in html
    assert '<main class="main">' in html and '<aside class="sidebar">' in html
    assert "fonts.googleapis" not in html and "fonts.googleapis" in measurement_html(parts)


def _boxes(parts) -> dict:
    """Measured boxes for every mark: summaries 20 mm, project headers 6 mm (+2.8 mm gap below
    all but the last), project bullets 5, work headers 7 (6 without bullets), work bullets 4.5."""
    boxes = {}
    last_project = parts.projects[-1].id
    for mark in _marks(measurement_html(parts, fonts=False)):
        kind, _, ident = mark.partition(":")
        top = 100.0
        if kind == "summary":
            boxes[mark] = {"top": top, "height": _px(20), "ul": None, "gap": 0}
        elif kind == "project":
            boxes[mark] = {"top": top, "height": _px(30), "ul": top + _px(6),
                           "gap": 0 if ident == last_project else _px(2.8)}
        elif kind == "work":
            boxes[mark] = {"top": top, "height": _px(30), "ul": top + _px(7), "gap": _px(2.6)}
        elif kind == "workbare":
            boxes[mark] = {"top": top, "height": _px(6), "ul": None, "gap": _px(2.6)}
        else:
            boxes[mark] = {"top": top, "height": _px(5 if kind == "pbullet" else 4.5), "ul": None,
                           "gap": 0}
    return boxes


def test_heights_from_the_measured_boxes(parts):
    model = heights_from_boxes(parts, _boxes(parts), _px(250), layout="L")
    assert model.fingerprint == parts_fingerprint(parts) and model.layout == "L"
    assert model.summaries["systems"] == pytest.approx(20)
    # A project's header carries the gap below it; the overhead pays it back for the last one.
    assert model.projects["webserver"].header == pytest.approx(8.8)
    assert model.projects["shell"].bullets["heredoc"] == pytest.approx(5)
    assert model.work["poker"].header == pytest.approx(7)
    assert model.work["poker"].bare == pytest.approx(6)
    assert model.work["shop"].bullets["service"] == pytest.approx(4.5)
    # default: 20 + (8.8 + 10) + (8.8 + 5) + (7 + 4.5) + (7 + 4.5) = 75.6 of the 250 mm column
    assert model.overhead_mm == pytest.approx(250 - 75.6)
    assert model.predict_mm(parts, default_selection(parts)) == pytest.approx(250)


class FakePlaywright:
    """``playwright.sync_api`` for the measuring session: the boxes and the column height."""

    Error = RuntimeError

    def __init__(self, boxes: dict, main_px: float):
        self.boxes, self.main_px = boxes, main_px
        self.launches, self.closed = 0, False
        self.html: list[str] = []

    def sync_playwright(self):
        return nullcontext(SimpleNamespace(chromium=SimpleNamespace(launch=self._launch)))

    def _launch(self):
        self.launches += 1
        return SimpleNamespace(new_page=self._new_page, close=self._close)

    def _close(self):
        self.closed = True

    def _new_page(self, **kw):
        def evaluate(expr):
            if "fonts.ready" in expr:
                return True
            if "data-m" in expr:
                return self.boxes
            assert "alignSelf" in expr
            return self.main_px

        return SimpleNamespace(set_content=lambda html, wait_until: self.html.append(html),
                               evaluate=evaluate)


@pytest.fixture
def fake_playwright(monkeypatch, parts):
    fake = FakePlaywright(_boxes(parts), _px(250))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)
    return fake


def test_measure_heights_uses_one_browser_for_every_part(parts, fake_playwright):
    model = measure_heights(parts)
    assert fake_playwright.launches == 1 and fake_playwright.closed
    measuring, cv = fake_playwright.html
    assert 'data-m="summary:default"' in measuring and "fonts.googleapis" in measuring
    assert "data-m" not in cv and "A general summary" in cv  # the untailored CV, as printed
    assert model.layout == layout_signature(True)
    assert model.overhead_mm == pytest.approx(250 - 75.6)


def test_measure_heights_without_web_fonts(parts, fake_playwright):
    assert measure_heights(parts, fonts=False).layout == layout_signature(False)
    assert all("fonts.googleapis" not in html for html in fake_playwright.html)


def test_main_height_mm(parts, fake_playwright):
    assert main_height_mm(resolve(parts, default_selection(parts)), fonts=False) == pytest.approx(250)


def test_measure_heights_without_playwright(parts, monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(RenderUnavailable):
        measure_heights(parts)


# --------------------------------------------------------------------------- the cache


@pytest.fixture
def counted(monkeypatch, flat_heights):
    calls: list[bool] = []

    def measure(parts, *, fonts=True):
        calls.append(fonts)
        return flat_heights(parts, fonts=fonts)

    monkeypatch.setattr(measure_mod, "measure_heights", measure)
    return calls


def test_heights_path_is_a_windows_file_name(parts, tmp_path):
    path = heights_path(tmp_path, parts)
    assert path.parent == tmp_path and path.name.startswith("heights-test-1")
    assert path.suffix == ".json" and ":" not in path.name


def test_the_cache_is_written_once_and_reused(parts, tmp_path, counted):
    cache = tmp_path / "cv"
    first = cached_heights(parts, cache)
    assert counted == [True]
    stored = json.loads(heights_path(cache, parts).read_text(encoding="utf-8"))
    assert stored["fingerprint"] == parts_fingerprint(parts)
    assert cached_heights(parts, cache) == first and counted == [True]
    assert cached_heights(parts, cache, force=True) == first and counted == [True, True]


def test_the_cache_is_remeasured_for_another_layout_or_a_broken_file(parts, tmp_path, counted):
    cache = tmp_path / "cv"
    cached_heights(parts, cache)
    path = heights_path(cache, parts)
    stale = json.loads(path.read_text(encoding="utf-8")) | {"layout": "old"}
    path.write_text(json.dumps(stale), encoding="utf-8")
    assert cached_heights(parts, cache).layout == layout_signature(True)
    assert len(counted) == 2
    path.write_text("{not json", encoding="utf-8")
    cached_heights(parts, cache)
    assert len(counted) == 3
    cached_heights(parts, cache, fonts=False)  # the fallback fonts are another layout
    assert counted[-1] is False


def test_the_cache_is_only_read_when_building_is_not_allowed(parts, tmp_path, counted):
    cache = tmp_path / "cv"
    assert cached_heights(parts, cache, build=False) is None and counted == []
    cached_heights(parts, cache)
    assert isinstance(cached_heights(parts, cache, build=False), HeightModel)
    assert len(counted) == 1


# ------------------------------------------------------------------------- cv-heights


@pytest.fixture
def cli_env(tmp_path, monkeypatch, counted):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copyfile(ROOT / "config" / "profile.example.yaml", config_dir / "profile.yaml")
    shutil.copyfile(ROOT / "config" / "sources.yaml", config_dir / "sources.yaml")
    shutil.copyfile(FIXTURE, config_dir / "cv-parts.yaml")
    monkeypatch.setenv("JOBSCRAPER_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("JOBSCRAPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("JOBSCRAPER_PROFILE", raising=False)
    return SimpleNamespace(config=config_dir, data=tmp_path / "data", counted=counted)


def test_cli_cv_heights_measures_and_prints(cli_env):
    result = runner.invoke(cli_mod.app, ["cv-heights"])
    assert result.exit_code == 0, result.output
    assert cli_env.counted == [True]  # the real cache is measured with the web fonts
    files = list((cli_env.data / "cv").glob("heights-*.json"))
    assert len(files) == 1 and str(files[0]) in result.output
    assert "measured" in result.output
    # flat_heights: 1 mm per part, 100 mm overhead; the default CV has 10 parts.
    assert "overhead 100.0 mm" in result.output
    assert "budget for summary + projects + work: 197.0 mm" in result.output
    assert "the untailored CV uses 10.0 mm" in result.output
    lines = result.output.splitlines()
    assert any(re.fullmatch(r"\s+\[webserver\] Webserver\s+1\.0", line) for line in lines)
    assert any(re.fullmatch(r"\s+- \[cgi\]\s+1\.0", line) for line in lines)
    assert any(re.fullmatch(r"\s+\[poker\] Poker professional\s+1\.0 \(no bullets: 1\.0\)", line)
               for line in lines)
    assert any(re.fullmatch(r"\s+\[systems\]\s+1\.0", line) for line in lines)

    again = runner.invoke(cli_mod.app, ["cv-heights"])
    assert again.exit_code == 0 and "cached" in again.output and cli_env.counted == [True]
    forced = runner.invoke(cli_mod.app, ["cv-heights", "--force"])
    assert forced.exit_code == 0 and "measured" in forced.output and len(cli_env.counted) == 2


def test_cli_cv_heights_without_a_browser(cli_env, monkeypatch):
    def missing(parts, *, fonts=True):
        raise RenderUnavailable("no chromium; run playwright install chromium")

    monkeypatch.setattr(measure_mod, "measure_heights", missing)
    result = runner.invoke(cli_mod.app, ["cv-heights"])
    assert result.exit_code == 1 and "no chromium" in result.output


def test_cli_cv_heights_without_a_library(cli_env):
    (cli_env.config / "cv-parts.yaml").unlink()
    result = runner.invoke(cli_mod.app, ["cv-heights"])
    assert result.exit_code == 1 and "cv-parts.yaml" in result.output
