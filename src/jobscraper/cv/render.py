"""The tailored CV as a finished one-page PDF in the layout of the owner's Canva CV.

:func:`to_html` lays a :class:`~jobscraper.cv.parts.TailoredCV` out as one self-contained A4 page
(inline CSS and SVG icons, no template engine): a navy sidebar with the name, title, contact
lines and the fixed sidebar sections, and a white main column with the profile, the selected
projects and the work entries. :func:`render_pdf` prints it with a headless Chromium through
Playwright and says whether it fits: a page that holds too many bullets grows past 297 mm, and
the overflow in mm is what the caller warns about. :func:`measure_overflow` takes the same
measurement without printing, for the tailor step's page-fit check.

Measurements come from the Canva export (docs/PLAN.md, "Tailored CV"). Canva's fonts "Now" and
"Aileron" are not on Google Fonts; the closest substitutes found there sit in the ``FONT_*``
constants, so the exact fonts can be swapped in later (``@font-face`` files) without touching
the layout.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from jobscraper.cv.parts import ResolvedEntry, TailoredCV, _flat

#: Canva's "Now" (name, title, section headings): not on Google Fonts; the closest one there.
FONT_DISPLAY = "League Spartan"
#: Canva's "Aileron" (project and work entries): not on Google Fonts either.
FONT_TEXT = "Roboto"
#: Canva's "Lato" (profile paragraph, sidebar): on Google Fonts as itself.
FONT_BODY = "Lato"
#: Weights loaded for every family above.
FONT_WEIGHTS = "300;400;700"

NAVY = "#163853"  # sampled from the Canva export
GREY = "#545454"

INSTALL_HINT = ("Playwright or its Chromium is missing: run `uv sync --extra dev` and then "
                "`uv run playwright install chromium`.")

#: The reference PNG export is 1414 px wide (A4 at ~171 dpi); screenshots match it.
PNG_WIDTH = 1414
_MM_PER_PX = 25.4 / 96


class RenderUnavailable(RuntimeError):
    """Playwright or its Chromium is not installed."""


@dataclass(frozen=True)
class RenderResult:
    pages: int
    overflow_mm: float  # how far the content runs past one A4 page; 0.0 when it fits


# ----------------------------------------------------------------------------- icons

_SVG = ('<svg viewBox="0 0 24 24" width="100%" height="100%" aria-hidden="true" '
        'xmlns="http://www.w3.org/2000/svg">{}</svg>')
_ICONS = {
    "phone": _SVG.format(
        '<path fill="currentColor" d="M6.62 10.79c1.44 2.83 3.76 5.14 6.59 6.59l2.2-2.2c.27-.27.67-.36'
        ' 1.02-.24 1.12.37 2.33.57 3.57.57.55 0 1 .45 1 1V20c0 .55-.45 1-1 1-9.39 0-17-7.61-17-17'
        ' 0-.55.45-1 1-1h3.5c.55 0 1 .45 1 1 0 1.25.2 2.45.57 3.57.11.35.03.74-.25 1.02l-2.2 2.2z"/>'),
    "email": _SVG.format(
        '<path fill="currentColor" d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0'
        '-1.1-.9-2-2-2zm0 4-8 5-8-5V6l8 5 8-5v2z"/>'),
    "github": _SVG.format(
        '<g fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9.2"/>'
        '<ellipse cx="12" cy="12" rx="4.2" ry="9.2"/><path d="M2.8 12h18.4M4.2 7.4h15.6M4.2 16.6h15.6"/></g>'),
    "gear": _SVG.format(
        '<g fill="none" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round">'
        '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1'
        ' 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0'
        ' 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83'
        'l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0'
        ' 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33'
        'H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33'
        'l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2'
        ' 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></g>'),
}

# ------------------------------------------------------------------------------- CSS

_CSS = """
@page { size: A4; margin: 0 }
* { box-sizing: border-box; margin: 0; padding: 0 }
html, body { background: #fff; -webkit-print-color-adjust: exact; print-color-adjust: exact }
.page { width: 210mm; min-height: 297mm; display: flex; background: #fff; color: %(grey)s;
        font-family: %(body)s; font-kerning: normal }
ul { list-style: none }
ul > li { position: relative; padding-left: 4.4mm }
ul > li::before { content: ""; position: absolute; left: 1.15mm; top: 0.62em; width: 1mm;
                  height: 1mm; border-radius: 50%%; background: currentColor }

/* --- sidebar: Lato 10, the name in Now 23 (Canva units are pt) ---------------------- */
.sidebar { width: 64.5mm; flex: none; background: %(navy)s; color: #fff;
           padding: 5.2mm 3.6mm 5mm 4.1mm; font-family: %(body)s; font-size: 10pt;
           line-height: 1.35 }
.name { font-family: %(display)s; font-weight: 300; font-size: 27.4pt; line-height: 0.86;
        letter-spacing: 0.01em; margin: 0 0 0 1.5mm }
.title { font-family: %(display)s; font-weight: 300; font-size: 13.9pt; margin-top: 10.1mm;
         letter-spacing: 0.01em; white-space: nowrap }
.title-rule { border-top: 0.3mm solid #fff; margin: 1.8mm 2.4mm 1.6mm 4.3mm }
.contact { display: flex; align-items: center; min-height: 7.4mm; font-size: 10.5pt }
.contact a { color: inherit; text-decoration: none }
.icon { width: 3.4mm; height: 3.4mm; flex: none; margin: 0 3.6mm 0 0.6mm; display: inline-block }
.technical { font-weight: 700; font-size: 11.6pt }
.technical .icon { width: 4.6mm; height: 4.6mm; margin-right: 2.6mm; margin-left: 0 }
.side-block { padding-left: 0.3mm }
.side-block .label { font-weight: 700; font-size: 12pt; margin: 2.6mm 0 1.2mm }
.side-block p { padding-left: 0.5mm }
.side-heading { font-family: %(display)s; font-weight: 700; font-size: 15.2pt; letter-spacing: 0.15em;
                line-height: 1.2; margin: 4.4mm 0 2.4mm 1.4mm }
/* The rule under a sidebar heading is shorter than the text column, as in the Canva layout. */
.side-heading::after { content: ""; display: block; border-top: 0.3mm solid #fff;
                       margin: 1.4mm 5.5mm 0 0.1mm }
.side-block ul > li { margin-bottom: 0.9mm }
.edu { margin-bottom: 3mm; font-size: 11pt }
.edu-period { font-weight: 700; letter-spacing: 0.08em; margin-bottom: 0.3mm }
.edu-title { font-weight: 700; margin-bottom: 0.6mm }
.side-block .edu ul > li { margin-bottom: 0.3mm }

/* --- main column: Aileron 11.1 / 10.1, profile Lato 12, headings Now Bold ----------- */
.main { flex: 1; padding: 8mm 8.6mm 7mm 14.3mm; font-family: %(text)s; font-size: 10.1pt;
        line-height: 1.36 }
.heading { font-family: %(display)s; font-weight: 700; font-size: 15.4pt; letter-spacing: 0.15em;
           color: %(navy)s; line-height: 1.15; margin: 0 }
.heading .light { font-weight: 400 }
.summary { font-family: %(body)s; font-size: 12pt; line-height: 1.38; text-align: justify;
           margin-top: 0.6mm }
.projects { margin-top: 7mm; margin-left: -1.9mm }
.projects .heading { margin-bottom: 3.8mm }
.project { margin: 0 0 2.8mm 4.6mm }
.ptitle { font-weight: 700; font-size: 11.1pt; margin-bottom: 1.2mm }
.plead { font-size: 11.1pt; line-height: 1.37; padding-left: 3mm; margin-bottom: 0.8mm }
.project ul, .job ul { text-align: justify }
.work { margin-top: 4.2mm }
.work .heading { padding-bottom: 1.8mm; border-bottom: 0.35mm solid %(navy)s; margin-bottom: 2.2mm }
.job { margin: 0 0 2.6mm 2.4mm }
/* Trailing margins would count against the page; the padding is the bottom margin. */
.project:last-child, .job:last-child, .edu:last-child, ul > li:last-child { margin-bottom: 0 }
.jhead { display: flex; align-items: baseline; font-size: 11.1pt }
.dot { width: 1.6mm; height: 1.6mm; border-radius: 50%%; background: %(navy)s; flex: none;
       margin: 0 3.8mm 0 0; align-self: center }
.jtitle { font-weight: 700; flex: 1 }
.period { font-size: 10pt; white-space: nowrap; margin-left: 3mm }
.jlead { font-size: 12pt; padding-left: 5.4mm; line-height: 1.15; margin: 0.6mm 0 0.6mm }
.job ul { padding-left: 7.4mm; line-height: 1.33 }
"""


def _stack(family: str) -> str:
    return f"'{family}', 'Segoe UI', Arial, sans-serif"


def _font_links() -> str:
    families = list(dict.fromkeys((FONT_DISPLAY, FONT_TEXT, FONT_BODY)))
    query = "&".join(f"family={f.replace(' ', '+')}:wght@{FONT_WEIGHTS}" for f in families)
    return ('<link rel="preconnect" href="https://fonts.googleapis.com">'
            '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
            f'<link rel="stylesheet" href="https://fonts.googleapis.com/css2?{query}&display=swap">')


# --------------------------------------------------------------------------- sidebar


def _e(value: Any) -> str:
    return escape(str(value), quote=True)


def _ul(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{_e(item)}</li>" for item in items) + "</ul>" if items else ""


def _cap(key: str) -> str:
    return key[:1].upper() + key[1:]


def _education(item: dict) -> str:
    parts = []
    if item.get("period"):
        parts.append(f'<div class="edu-period">{_e(item["period"])}</div>')
    if item.get("title"):
        parts.append(f'<div class="edu-title">{_e(item["title"])}</div>')
    parts.append(_ul([_flat(b) for b in item.get("bullets") or []]))
    return '<div class="edu">' + "".join(parts) + "</div>"


def _side_value(value: Any) -> str:
    """One sidebar value by its shape: str → paragraph, list[str] → bullets, {label, text} →
    bold label + text, {period, title, bullets} → education block, mapping → "key (value)"."""
    if isinstance(value, dict):
        return _ul([f"{_cap(str(k))} ({_flat(v)})" for k, v in value.items()])
    if not isinstance(value, list):
        return f"<p>{_e(value)}</p>"
    html, bullets = [], []
    for item in value:
        if isinstance(item, dict) and ("label" in item or "text" in item):
            html.append(_ul(bullets))
            bullets = []
            html.append(f'<div class="label">{_e(item.get("label", ""))}</div>'
                        f'<p>{_e(_flat(item.get("text", "")))}</p>')
        elif isinstance(item, dict) and ("period" in item or "title" in item):
            html.append(_ul(bullets))
            bullets = []
            html.append(_education(item))
        else:
            bullets.append(_flat(item))
    html.append(_ul(bullets))
    return "".join(html)


def _href(key: str, value: str) -> str | None:
    if key == "email":
        return f"mailto:{value}"
    if key == "phone":
        return "tel:" + re.sub(r"\s+", "", value)
    if re.match(r"https?://", value):
        return value
    if key == "github":
        return f"https://{value}"
    return None


def _contact(key: str, value: str) -> str:
    icon = _ICONS.get(key, "")
    href = _href(key, value)
    text = f'<a href="{_e(href)}">{_e(value)}</a>' if href else _e(value)
    return f'<div class="contact"><span class="icon">{icon}</span><span>{text}</span></div>'


def _sidebar(cv: TailoredCV) -> str:
    name = cv.header.get("name", "")
    first, _, rest = name.partition(" ")
    html = [f'<h1 class="name">{_e(first)}' + (f"<br>{_e(rest)}" if rest else "") + "</h1>"]
    if cv.header.get("title"):
        html.append(f'<div class="title">{_e(cv.header["title"])}</div><div class="title-rule"></div>')
    html += [_contact(k, v) for k, v in cv.header.items() if k not in ("name", "title")]
    if "technical" in cv.sidebar:
        html.append(f'<div class="contact technical"><span class="icon">{_ICONS["gear"]}</span>'
                    "<span>Technical skills:</span></div>")
        html.append(f'<div class="side-block">{_side_value(cv.sidebar["technical"])}</div>')
    for key, value in cv.sidebar.items():
        if key == "technical":
            continue
        html.append(f'<h2 class="side-heading">{_e(key.replace("_", " ").upper())}</h2>')
        html.append(f'<div class="side-block">{_side_value(value)}</div>')
    return '<aside class="sidebar">' + "".join(html) + "</aside>"


# ------------------------------------------------------------------------------ main


def _project(entry: ResolvedEntry) -> str:
    return (f'<div class="project"><div class="ptitle">{_e(entry.title)}</div>'
            + (f'<div class="plead">{_e(entry.lead)}</div>' if entry.lead else "")
            + _ul(entry.bullets) + "</div>")


def _job(entry: ResolvedEntry) -> str:
    return ('<div class="job"><div class="jhead"><span class="dot"></span>'
            f'<span class="jtitle">{_e(entry.title)}</span>'
            f'<span class="period">{_e(entry.period or "")}</span></div>'
            + (f'<div class="jlead">{_e(entry.lead)}</div>' if entry.lead else "")
            + _ul(entry.bullets) + "</div>")


def _main(cv: TailoredCV) -> str:
    return ('<main class="main">'
            f'<section class="profile"><h2 class="heading">PROFILE</h2><p class="summary">{_e(cv.summary)}</p></section>'
            '<section class="projects"><h2 class="heading">SAMPLE PROJECTS <span class="light">ON GITHUB</span></h2>'
            + "".join(_project(p) for p in cv.projects) + "</section>"
            '<section class="work"><h2 class="heading">WORK EXPERIENCE</h2>'
            + "".join(_job(w) for w in cv.work) + "</section></main>")


def to_html(cv: TailoredCV, *, fonts: bool = True) -> str:
    """The CV as one self-contained A4 page; ``fonts=False`` leaves out the Google Fonts links
    (tests: nothing touches the network, the fallback fonts are used)."""
    css = _CSS % {"navy": NAVY, "grey": GREY, "display": _stack(FONT_DISPLAY),
                  "text": _stack(FONT_TEXT), "body": _stack(FONT_BODY)}
    title = _e(cv.header.get("name", "CV"))
    return ("<!doctype html>\n"
            f'<html lang="en"><head><meta charset="utf-8"><title>{title}</title>'
            + (_font_links() if fonts else "")
            + f"<style>{css}</style></head><body>"
            f'<div class="page">{_sidebar(cv)}{_main(cv)}</div></body></html>\n')


# ------------------------------------------------------------------------------- PDF


def count_pdf_pages(data: bytes) -> int:
    """Page objects in a PDF as Chromium writes it (no object streams): ``/Type /Page``."""
    return len(re.findall(rb"/Type\s*/Page(?![A-Za-z])", data))


_OVERFLOW_JS = """() => {
  const page = document.querySelector('.page');
  return page ? page.getBoundingClientRect().height : 0;
}"""


@contextmanager
def _chromium_page(html: str) -> Iterator[Any]:
    """A headless Chromium page with ``html`` loaded and its fonts ready; closed afterwards.

    Raises :class:`RenderUnavailable` when Playwright or its browser is not installed.
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # the dev extra is not installed
        raise RenderUnavailable(INSTALL_HINT) from exc
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except PlaywrightError as exc:  # the browser binary is missing
            raise RenderUnavailable(f"{INSTALL_HINT} ({str(exc).splitlines()[0]})") from exc
        try:
            page = browser.new_page(viewport={"width": 800, "height": 1200},
                                    device_scale_factor=PNG_WIDTH / (210 / _MM_PER_PX))
            page.set_content(html, wait_until="load")
            page.evaluate("document.fonts.ready.then(() => true)")
            yield page
        finally:
            browser.close()


def _overflow_mm(page: Any) -> float:
    """How far the loaded page runs past one A4 page, in mm (0.0 when it fits)."""
    height_mm = page.evaluate(_OVERFLOW_JS) * _MM_PER_PX
    return round(max(0.0, height_mm - 297.0), 1)


def measure_overflow(cv: TailoredCV, *, fonts: bool = True) -> float:
    """The overflow :func:`render_pdf` would report for ``cv``, without writing anything: the
    page-fit check of a tailoring. ``fonts=True`` loads the web fonts, as the real print does.

    Raises :class:`RenderUnavailable` when Playwright or its browser is not installed.
    """
    with _chromium_page(to_html(cv, fonts=fonts)) as page:
        return _overflow_mm(page)


def render_pdf(html: str, pdf_path: Path, png_path: Path | None = None) -> RenderResult:
    """Print ``html`` to ``pdf_path`` (A4) with headless Chromium; optionally a PNG of the page.

    Raises :class:`RenderUnavailable` when Playwright or its browser is not installed.
    """
    pdf_path = Path(pdf_path)
    with _chromium_page(html) as page:
        overflow = _overflow_mm(page)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        page.pdf(path=str(pdf_path), format="A4", print_background=True, prefer_css_page_size=True)
        if png_path is not None:
            Path(png_path).parent.mkdir(parents=True, exist_ok=True)
            page.locator(".page").screenshot(path=str(png_path))
    return RenderResult(pages=count_pdf_pages(pdf_path.read_bytes()), overflow_mm=overflow)
