"""The CV renderer's HTML (no browser) and the CLI paths that print a PDF (renderer faked).

Everything runs against the generic fixture ``tests/fixtures/cv-parts.yaml`` and the repository's
``config/cv-parts.example.yaml``; the owner's own library is never read. ``render_pdf`` is
monkeypatched in the CLI tests, so no Chromium is started here — the real print is covered by
``test_cv_render_browser.py``.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from jobscraper import cli as cli_mod
from jobscraper.config import ROOT
from jobscraper.cv import render as render_mod
from jobscraper.cv.parts import (
    EntryPick,
    Selection,
    default_selection,
    load_cv_parts,
    parts_fingerprint,
    resolve,
)
from jobscraper.cv.render import RenderResult, RenderUnavailable, count_pdf_pages, to_html
from jobscraper.cv.tailor import TAILOR_PROMPT_VERSION, Tailor, tailoring_key_prefix
from jobscraper.models import Job
from jobscraper.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "cv-parts.yaml"
EXAMPLE = ROOT / "config" / "cv-parts.example.yaml"
runner = CliRunner()


def _load(src: Path, tmp_path: Path):
    shutil.copyfile(src, tmp_path / "cv-parts.yaml")
    return load_cv_parts(tmp_path)


@pytest.fixture
def parts(tmp_path):
    return _load(FIXTURE, tmp_path)


@pytest.fixture
def cv(parts):
    return resolve(parts, default_selection(parts))


def _main(html: str) -> str:
    """The main column only (everything after the sidebar)."""
    return html.split('<main class="main">', 1)[1]


def _sidebar(html: str) -> str:
    return html.split('<aside class="sidebar">', 1)[1].split("</aside>", 1)[0]


# ------------------------------------------------------------------------- HTML


def test_html_is_one_a4_page_with_the_header(cv):
    html = to_html(cv, fonts=False)
    assert html.startswith("<!doctype html>")
    assert "@page { size: A4; margin: 0 }" in html
    assert "width: 210mm" in html and "297mm" in html
    side = _sidebar(html)
    # The name is split over two lines, then the title, then the contact lines.
    assert '<h1 class="name">Test<br>Person</h1>' in side
    assert '<div class="title">Junior developer</div>' in side
    assert side.index("Junior developer") < side.index("test@example.com") < side.index("github.com/test")
    assert 'href="mailto:test@example.com"' in side
    assert 'href="https://github.com/test"' in side
    # Contact lines with a known key get an icon; name and title are not contact lines.
    assert side.count('class="contact"') == 2
    assert side.count("<svg") >= 3  # mail, globe, gear


def test_unknown_header_keys_are_plain_contact_lines_without_an_icon(parts):
    parts = parts.model_copy(update={"header": {"name": "Solo", "title": "Dev", "city": "Helsinki",
                                                "phone": "+358 1", "website": "https://x.test"}})
    side = _sidebar(to_html(resolve(parts, default_selection(parts)), fonts=False))
    assert '<h1 class="name">Solo</h1>' in side  # a one-word name stays one line
    city = side.split("Helsinki")[0].rsplit('<div class="contact">', 1)[1]
    assert "<svg" not in city and '<span class="icon"></span>' in city
    phone = side.split("+358 1")[0].rsplit('<div class="contact">', 1)[1]
    assert "<svg" in phone
    assert 'href="tel:+3581"' in side
    assert 'href="https://x.test"' in side  # a value that is already a URL keeps its scheme


def test_main_column_has_profile_projects_and_work_in_order(cv):
    main = _main(to_html(cv, fonts=False))
    assert "A general summary about the test person, folded over two lines." in main
    assert 'SAMPLE PROJECTS <span class="light">ON GITHUB</span>' in main
    # Default selection: webserver and game, in that order; webserver shows its default bullets only.
    assert main.index(">Webserver<") < main.index(">Raycaster<")
    assert "Shell" not in main
    assert "An HTTP server in C++." in main
    assert "Wrote an incremental request parser." in main and "Served many clients with poll()." in main
    assert "Ran CGI scripts." not in main
    assert main.index("PROFILE") < main.index("SAMPLE PROJECTS") < main.index("WORK EXPERIENCE")
    work = main.split("WORK EXPERIENCE", 1)[1]
    assert work.index("Poker professional") < work.index("Shop assistant")
    assert '<span class="period">2010-2024</span>' in work and '<span class="period">2008-2010</span>' in work
    assert "Self-employed." in work and "Kept detailed records." in work
    assert "Used probability daily." not in work


def test_projects_follow_the_selection_order_and_bullets(parts):
    selection = Selection(summary="systems",
                          projects=[EntryPick(id="shell", bullets=["signals", "pipes"]),
                                    EntryPick(id="webserver", bullets=["cgi"])],
                          work={"poker": ["risk"], "shop": []})
    main = _main(to_html(resolve(parts, selection), fonts=False))
    assert "A systems-flavoured summary." in main
    assert main.index(">Shell<") < main.index(">Webserver<")
    assert main.index("Handled signals.") < main.index("Implemented pipes.")
    assert "Ran CGI scripts." in main and "Served many clients" not in main
    assert "Managed bankroll risk." in main and "Kept detailed records." not in main
    # A work entry with no bullets and no lead still shows its title and period, and no empty list.
    shop = main.split("Shop assistant", 1)[1]
    assert "2008-2010" in shop and "<ul" not in shop


def test_fixture_sidebar_shapes(cv):
    side = _sidebar(to_html(cv, fonts=False))
    # `technical` sits in the contact area under the gear, as "Technical skills:".
    assert "Technical skills:" in side
    technical = side.split("Technical skills:", 1)[1].split('<h2 class="side-heading"', 1)[0]
    assert "<li>C</li>" in technical and "<li>Python</li>" in technical
    # Every other key is a big heading; a string is a paragraph, a mapping "key (value)" bullets.
    assert '<h2 class="side-heading">EDUCATION</h2>' in side
    assert "<p>Hive Helsinki</p>" in side
    assert '<h2 class="side-heading">LANGUAGES</h2>' in side
    assert "<li>Finnish (native)</li>" in side and "<li>English (fluent)</li>" in side
    assert "TECHNICAL" not in side


def test_example_sidebar_shapes(tmp_path):
    parts = _load(EXAMPLE, tmp_path)
    side = _sidebar(to_html(resolve(parts, default_selection(parts)), fonts=False))
    assert '<h2 class="side-heading">SOFT SKILLS</h2>' in side
    assert "<li>Calm under pressure</li>" in side
    assert "<p>Hive Helsinki (coding school), 2024-2026</p>" in side
    assert "<li>Finnish (basic)</li>" in side


def test_labelled_and_education_sidebar_shapes(parts):
    sidebar = {
        "technical": [{"label": "Languages:", "text": "C, C++"}, {"label": "Tools", "text": "Git"}],
        "education": [{"period": "2024 - 2026", "title": "Hive Helsinki",
                       "bullets": ["Peer learning", "C and C++"]},
                      {"title": "Night school"}],
        "skills": ["Teamwork", 42, {"odd": "shape"}],
        "hobby": 7,
    }
    side = _sidebar(to_html(resolve(parts.model_copy(update={"sidebar": sidebar}),
                                    default_selection(parts)), fonts=False))
    technical = side.split("Technical skills:", 1)[1].split('<h2 class="side-heading"', 1)[0]
    assert '<div class="label">Languages:</div>' in technical and "<p>C, C++</p>" in technical
    assert '<div class="label">Tools</div>' in technical
    assert '<div class="edu-period">2024 - 2026</div>' in side
    assert '<div class="edu-title">Hive Helsinki</div>' in side
    assert "<li>Peer learning</li>" in side and "<li>C and C++</li>" in side
    night = side.split("Night school", 1)[1].split('<h2 class="side-heading"', 1)[0]
    assert "edu-period" not in night and "<ul" not in night
    assert "<li>Teamwork</li>" in side and "<li>42</li>" in side and "<li>odd: shape</li>" in side
    assert '<h2 class="side-heading">HOBBY</h2>' in side and "<p>7</p>" in side


def test_a_cv_without_title_or_technical_skills(parts):
    parts = parts.model_copy(update={"header": {"name": "Solo"},
                                     "sidebar": {"education": [{"period": "2020"}]}})
    side = _sidebar(to_html(resolve(parts, default_selection(parts)), fonts=False))
    assert 'class="title"' not in side and "Technical skills:" not in side
    assert '<div class="edu"><div class="edu-period">2020</div></div>' in side


def test_text_is_html_escaped(parts):
    raw = parts.model_dump()
    raw["projects"][0]["bullets"][0]["text"] = "Used <vector> & maps"
    raw["header"]["name"] = "A&B <Co>"
    raw["sidebar"]["education"] = "R&D <lab>"
    escaped = type(parts).model_validate(raw)
    html = to_html(resolve(escaped, default_selection(escaped)), fonts=False)
    assert "Used &lt;vector&gt; &amp; maps" in html and "<vector>" not in html
    assert "A&amp;B<br>&lt;Co&gt;" in html
    assert "R&amp;D &lt;lab&gt;" in html


def test_fonts_switch(cv):
    assert "fonts.googleapis" not in to_html(cv, fonts=False)
    with_fonts = to_html(cv)
    assert 'href="https://fonts.googleapis.com/css2?' in with_fonts
    for family in (render_mod.FONT_DISPLAY, render_mod.FONT_TEXT, render_mod.FONT_BODY):
        assert family.replace(" ", "+") in with_fonts


def test_count_pdf_pages():
    assert count_pdf_pages(b"%PDF-1.4 << /Type /Pages /Count 2 >> << /Type /Page >> << /Type/Page>>") == 2
    assert count_pdf_pages(b"%PDF-1.4 nothing") == 0


def test_render_pdf_without_playwright(monkeypatch, tmp_path):
    import sys

    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)  # import fails
    with pytest.raises(RenderUnavailable, match="playwright install chromium"):
        render_mod.render_pdf("<html></html>", tmp_path / "x.pdf")


class _PlaywrightError(Exception):
    pass


class FakePlaywright:
    """A stand-in ``playwright.sync_api``: the real browser run is test_cv_render_browser.py, but
    coverage cannot trace past Playwright's greenlet switch, so the body is covered here."""

    def __init__(self, height_px: float, launch_error: str | None = None):
        self.height_px, self.launch_error = height_px, launch_error
        self.closed = False
        self.calls: list[tuple] = []

    # module attributes
    Error = _PlaywrightError

    def sync_playwright(self):
        from contextlib import nullcontext

        return nullcontext(SimpleNamespace(chromium=SimpleNamespace(launch=self._launch)))

    def _launch(self):
        if self.launch_error:
            raise _PlaywrightError(self.launch_error + "\nmore detail")
        return SimpleNamespace(new_page=self._new_page, close=self._close)

    def _close(self):
        self.closed = True

    def _new_page(self, **kw):
        self.calls.append(("new_page", kw))

        def evaluate(expr):
            self.calls.append(("evaluate", expr))
            return True if "fonts.ready" in expr else self.height_px

        def pdf(path, **kw):
            self.calls.append(("pdf", kw))
            Path(path).write_bytes(b"%PDF << /Type /Pages >> << /Type /Page >>")

        def locator(selector):
            return SimpleNamespace(screenshot=lambda path: Path(path).write_bytes(b"png bytes"))

        return SimpleNamespace(set_content=lambda html, wait_until: self.calls.append(("html", html)),
                               evaluate=evaluate, pdf=pdf, locator=locator)


@pytest.fixture
def fake_playwright(monkeypatch):
    import sys

    def _install(height_px: float = 1122.52, launch_error: str | None = None) -> FakePlaywright:
        fake = FakePlaywright(height_px, launch_error)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)
        return fake

    return _install


def test_render_pdf_measures_the_page_and_prints_it(fake_playwright, tmp_path):
    fake = fake_playwright()
    result = render_mod.render_pdf("<html>cv</html>", tmp_path / "out" / "cv.pdf")
    assert result == RenderResult(pages=1, overflow_mm=0.0)
    assert ("html", "<html>cv</html>") in fake.calls
    assert ("pdf", {"format": "A4", "print_background": True, "prefer_css_page_size": True}) in fake.calls
    assert fake.closed and not (tmp_path / "out" / "cv.png").exists()
    # Screenshots come out at the reference export's width.
    new_page = dict(fake.calls)["new_page"]
    assert round(new_page["device_scale_factor"] * 210 / 25.4 * 96) == 1414


def test_render_pdf_reports_overflow_and_writes_the_png(fake_playwright, tmp_path):
    fake_playwright(height_px=1200)  # 317.5 mm
    png = tmp_path / "png" / "cv.png"
    result = render_mod.render_pdf("<html></html>", tmp_path / "cv.pdf", png)
    assert result.overflow_mm == 20.5
    assert png.read_bytes() == b"png bytes"


def test_render_pdf_without_chromium(fake_playwright, tmp_path):
    fake = fake_playwright(launch_error="Executable doesn't exist")
    with pytest.raises(RenderUnavailable, match=r"playwright install chromium.*Executable doesn't exist\)"):
        render_mod.render_pdf("<html></html>", tmp_path / "x.pdf")
    assert not fake.closed  # never launched


# --------------------------------------------------------------------- store / tailor helpers


def test_latest_tailoring_is_the_newest_under_the_prefix(tmp_path):
    store = Store(tmp_path / "t.db")
    assert store.latest_tailoring("j1", "lib:1|v|") is None
    store.save_tailoring("j1", "lib:1|v|model-a", "model-a", '{"n": 1}')
    store.save_tailoring("j1", "lib:2|v|model-a", "model-a", '{"n": "other library"}')
    store.save_tailoring("j2", "lib:1|v|model-a", "model-a", '{"n": "other job"}')
    assert store.latest_tailoring("j1", "lib:1|v|") == '{"n": 1}'
    store.conn.execute("UPDATE cv_tailorings SET created_at='2000-01-01' WHERE data='{\"n\": 1}'")
    store.save_tailoring("j1", "lib:1|v|subagent", "subagent", '{"n": 2}')
    assert store.latest_tailoring("j1", "lib:1|v|") == '{"n": 2}'
    # The prefix is matched literally, not as a LIKE pattern.
    store.save_tailoring("j3", "a_b|v|m", "m", '{"n": 3}')
    assert store.latest_tailoring("j3", "a%|") is None
    store.close()


def test_tailoring_key_prefix_matches_the_cache_key(settings, parts, tmp_path):
    t = Tailor(settings.profile, Store(tmp_path / "k.db"), parts, model="m")
    assert tailoring_key_prefix(parts) == f"{parts_fingerprint(parts)}|{TAILOR_PROMPT_VERSION}|"
    assert t.cache_key == tailoring_key_prefix(parts) + "m"


# --------------------------------------------------------------------------- CLI


ANSWER = ('{"focus": "f", "summary": "systems", "projects": [{"id": "shell", "bullets": ["pipes"]}],'
          ' "work": [{"id": "poker", "bullets": ["math"]}], "rationale": "r", "gaps": []}')


class FakeRender:
    """Stands in for ``render_pdf``: records the calls, writes the files, returns ``result``."""

    def __init__(self, result=RenderResult(pages=1, overflow_mm=0.0), error=None):
        self.result, self.error = result, error
        self.calls: list[dict] = []

    def __call__(self, html, pdf_path, png_path=None):
        self.calls.append({"html": html, "pdf": Path(pdf_path), "png": png_path})
        if self.error is not None:
            raise self.error
        Path(pdf_path).parent.mkdir(parents=True, exist_ok=True)
        Path(pdf_path).write_bytes(b"%PDF")
        return self.result


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copyfile(ROOT / "config" / "profile.example.yaml", config_dir / "profile.yaml")
    shutil.copyfile(ROOT / "config" / "sources.yaml", config_dir / "sources.yaml")
    shutil.copyfile(FIXTURE, config_dir / "cv-parts.yaml")
    monkeypatch.setenv("JOBSCRAPER_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("JOBSCRAPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("JOBSCRAPER_PROFILE", raising=False)
    db = tmp_path / "jobs.db"
    monkeypatch.setenv("JOBSCRAPER_DB", str(db))
    store = Store(db)
    job = Job(source="test", source_id="1", url="https://example.test/1", title="C++ Developer",
              company="Acme", country="FI", description="C++.")
    store.upsert_jobs([job])
    store.close()
    fake = FakeRender()
    monkeypatch.setattr(render_mod, "render_pdf", fake)
    answers = tmp_path / "data" / "cv" / "tailored" / "answers"
    answers.mkdir(parents=True)
    (answers / f"{job.id}.json").write_text(ANSWER, encoding="utf-8")
    return SimpleNamespace(job=job, fake=fake, data=tmp_path / "data", config=config_dir, db=db)


def _today() -> str:
    return f"{datetime.now(UTC):%Y-%m-%d}"


def test_cv_render_default(cli_env):
    result = runner.invoke(cli_mod.app, ["cv-render"])
    assert result.exit_code == 0, result.output
    pdf = cli_env.data / "cv" / "cv-default.pdf"
    assert pdf.is_file() and str(pdf) in result.output
    call = cli_env.fake.calls[0]
    assert call["pdf"] == pdf and call["png"] is None
    assert "Webserver" in call["html"] and "fonts.googleapis" in call["html"]
    assert "WARNING" not in result.output


def test_cv_render_out_and_png(cli_env, tmp_path):
    out = tmp_path / "elsewhere" / "cv.pdf"
    result = runner.invoke(cli_mod.app, ["cv-render", "--out", str(out), "--png"])
    assert result.exit_code == 0, result.output
    assert cli_env.fake.calls[0]["pdf"] == out
    assert cli_env.fake.calls[0]["png"] == out.with_suffix(".png")
    assert str(out.with_suffix(".png")) in result.output


def test_cv_render_overflow_warns_but_succeeds(cli_env):
    cli_env.fake.result = RenderResult(pages=2, overflow_mm=12.4)
    result = runner.invoke(cli_mod.app, ["cv-render"])
    assert result.exit_code == 0, result.output
    assert "WARNING: the CV overflows one page by 12.4 mm; choose fewer bullets" in result.output
    assert "2 pages" in result.output


def test_cv_render_without_a_browser_fails(cli_env):
    cli_env.fake.error = RenderUnavailable("no chromium; run playwright install chromium")
    result = runner.invoke(cli_mod.app, ["cv-render"])
    assert result.exit_code == 1
    assert "no chromium" in result.output


def test_cv_render_without_a_library(cli_env):
    (cli_env.config / "cv-parts.yaml").unlink()
    result = runner.invoke(cli_mod.app, ["cv-render"])
    assert result.exit_code == 1 and "cv-parts.yaml" in result.output


def test_cv_render_a_job_needs_a_cached_tailoring(cli_env):
    job_id = cli_env.job.id
    missing = runner.invoke(cli_mod.app, ["cv-render", job_id])
    assert missing.exit_code == 1
    assert f"jobscraper tailor {job_id}" in missing.output
    assert cli_env.fake.calls == []

    unknown = runner.invoke(cli_mod.app, ["cv-render", "doesnotexist"])
    assert unknown.exit_code == 1 and "no job" in unknown.output

    imported = runner.invoke(cli_mod.app, ["tailor", job_id, "--import", "--no-pdf"])
    assert imported.exit_code == 0, imported.output
    assert cli_env.fake.calls == []  # --no-pdf

    result = runner.invoke(cli_mod.app, ["cv-render", job_id[:8]])
    assert result.exit_code == 0, result.output
    pdf = cli_env.data / "cv" / "tailored" / f"{_today()}-{job_id}.pdf"
    assert cli_env.fake.calls[0]["pdf"] == pdf
    html = cli_env.fake.calls[0]["html"]
    assert "A systems-flavoured summary." in html and ">Shell<" in html and "Webserver" not in html
    assert "Used probability daily." in html


def test_tailor_import_renders_the_pdf(cli_env):
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id, "--import"])
    assert result.exit_code == 0, result.output
    pdf = cli_env.data / "cv" / "tailored" / f"{_today()}-{cli_env.job.id}.pdf"
    assert cli_env.fake.calls[0]["pdf"] == pdf and cli_env.fake.calls[0]["png"] is None
    assert str(pdf) in result.output
    assert (pdf.with_suffix(".md")).is_file()


def test_tailor_export_renders_nothing(cli_env):
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id, "--export"])
    assert result.exit_code == 0, result.output
    assert cli_env.fake.calls == []


def test_tailor_without_a_browser_only_warns(cli_env):
    cli_env.fake.error = RenderUnavailable("no chromium")
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id, "--import"])
    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output and "no chromium" in result.output
    assert "# Tailored CV" in result.output


def test_tailor_overflow_warns(cli_env):
    cli_env.fake.result = RenderResult(pages=2, overflow_mm=3.0)
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id, "--import"])
    assert result.exit_code == 0, result.output
    assert "WARNING: the CV overflows one page by 3.0 mm; choose fewer bullets" in result.output
