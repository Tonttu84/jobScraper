"""expressoemprego.pt — the Expresso newspaper's Portuguese job board. Two-step HTML, no API.

The ``pesquisa-de-empregos.asp?chaves=`` search this adapter was originally specified against is
gone (the board answers its 404 page: *"Oops, a página que procura não se encontra disponível"*).
The live board is an ASP.NET site whose search lives in the URL *path*, and there is neither
JSON-LD nor a per-ad microdata block anywhere on it — the only structured data are the RSS feeds
(``/rss/informatica``, ``/rss/engenharia``, one per district too), and those carry a truncated
description and an ALL-CAPS title, so the cards plus the ad page beat them.

Discovery — ``GET https://www.expressoemprego.pt/emprego/pesquisa/<slot>/<slot>/…`` (observed
live 2026-09-09). The route has twelve ordered slots::

    /emprego/pesquisa/{query}/{localizacao}/{reference}/{data}/{tipo}/{exp}
                     /{habilitacao}/{funcao}/{setor}/{cidade}/{onlybo}/{ofertasdasemana}

and the site's own ``getURLCriteriosPesquisa`` (``/js/main``) fills every *unused* slot with the
slot's own name, then strips the trailing run of untouched names. ``search_url`` reproduces that
exactly, so a query alone is ``/emprego/pesquisa/software-developer`` while the IT function —
slot 8 — needs the seven placeholders in front of it. Values are slugged the way the site's
``encodeURIExtended`` does it: accents folded, lower-cased, every non-alphanumeric run turned
into ``-`` (so ``C++`` really is searched as ``c--``, which matches nothing).

``funcao = "Tecnologias de Informação"`` is the IT filter and is what actually buys IT ads: 50
open ones against 1880 on the whole board. A free-text query is *not* a substitute — the board
matches each word separately, so ``software developer`` returns 134 hits including "Business
Developer — Stands, Eventos e Feiras" and ``engenheiro de software`` returns 176 civil and
electrical engineers. The queries therefore run *after* the function pass and only add
stragglers; deciding what is really relevant is the filter/prefilter stage's job.

Paging is ``?page=N``, 1-based, 15 cards per page, and ``?order=data`` sorts newest first (so a
``max_pages`` cut keeps the recent ads). Every results page states its own total ("50 empregos
para a sua pesquisa"), which is what ends the walk; a page that adds no new URL, or has no cards
at all — which is how the board renders "no results", with no count line either — ends it too.

Cards are ``div.resultadosBox`` with ``h3.px20 a`` (href + proper-case title, the ``title``
attribute is upper-cased), ``h4.px18`` (company), ``div.px13.colorGray8`` (a truncated teaser)
and a ``span.px13.colorBlack`` meta line, "09.09.2026 | Aveiro, Portugal". The date is
``dd.mm.yyyy``. The card's markup is unbalanced — the reference number sits *after* the meta
span's ``</span>`` — but the ad id is the last path segment of the URL anyway.

Detail — ``/emprego/<slug>/<location-slug>/<id>`` (the location segment is missing on some ads).
There is no JSON-LD; the page has ``meta[property="og:title"]`` (the clean title; ``h1.px30``
appends "(M/F)"), ``h2.px18`` (company), the same ``span.px13.colorBlack`` meta line with
``Referência: <id>`` appended, and ``div.wucAnuncioDet`` with the full description. That div is
rendered by a per-advertiser XSLT and some advertisers ship their own ``<style>`` block inside
it, so style/script have to be dropped before ``strip_html``.

Neither a contract type nor a seniority label is published per ad: the search form's ``tipo``
(Full-time/Part-time/Estágio/Temporário) and ``exp`` filters exist, but the values appear only in
the form's own dropdowns, never on the ad page. ``employment_type`` and ``seniority_raw``
therefore stay None.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from jobscraper.http import strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

BASE = "https://www.expressoemprego.pt"
SEARCH_PATH = "/emprego/pesquisa/"
LISTING_PATH = "/ofertas-emprego"

# Ordered exactly as the site's getURLCriteriosPesquisa() writes them.
SLOTS = (
    "query", "localizacao", "reference", "data", "tipo", "exp",
    "habilitacao", "funcao", "setor", "cidade", "onlybo", "ofertasdasemana",
)

IT_FUNCAO = "Tecnologias de Informação"
DEFAULT_QUERIES = ["software developer", "programador", "C++", "engenheiro de software"]

_ID_RE = re.compile(r"/(\d{4,})/?$")
_DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
_TOTAL_RE = re.compile(r"<b>([\d\s .]+)</b>.{0,80}?empregos para a sua pesquisa", re.S)
_REFERENCE_RE = re.compile(r"Refer\w*ncia\s*:?\s*\d*\s*$", re.I)
_MF_SUFFIX_RE = re.compile(r"\s*\(\s*[mf]\s*/\s*[fm]\s*\)\s*$", re.I)

# The board writes about working from home in Portuguese; guess_remote only knows the English
# (and Finnish) words, so the Portuguese ones are translated into a hint before it is called.
_PT_REMOTE_RE = re.compile(
    r"(?<![a-z])(teletrabalho|remot[oa]s?|[àa]\s+dist[âa]ncia)(?![a-z])",
    re.I,
)
_PT_HYBRID_RE = re.compile(r"(?<![a-z])h[íi]brid[oa]s?(?![a-z])", re.I)
# …but the same words describe infrastructure and training: "ambientes híbridos" (hybrid cloud),
# "acesso remoto", "formação à distância" say nothing about where the person works. A live probe
# had every sysadmin ad on the board coming back "hybrid" until this was here.
_NOT_ABOUT_THE_WORKER_RE = re.compile(
    r"(?:acessos?|servidor(?:es)?|ambientes?|clouds?|nuvens?|infra-?estruturas?|redes?|"
    r"sistemas?|arquit[eé]turas?|desktops?|suportes?|forma[çc][ãa]o|ensino|cursos?)\s+$",
    re.I,
)


def slugify(text: Any) -> str:
    """The site's ``encodeURIExtended``: fold accents, lower-case, non-alphanumerics → ``-``."""
    folded = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]", "-", folded.strip().lower())


def search_url(page: int = 1, **criteria: Any) -> str:
    """Build a search URL, filling unused slots with their own names as the site's JS does."""
    parts = [slugify(criteria.get(slot)) or slot for slot in SLOTS]
    while parts and parts[-1] == SLOTS[len(parts) - 1]:
        parts.pop()
    path = SEARCH_PATH + "/".join(parts) if parts else LISTING_PATH
    query = "?order=data" + (f"&page={page}" if page > 1 else "")
    return BASE + path + query


def parse_meta(text: str | None) -> tuple[datetime | None, str | None]:
    """"09.09.2026 | Aveiro, Portugal | Referência: 2466295" → (posted_at, location)."""
    cleaned = _REFERENCE_RE.sub("", (text or "").strip()).strip(" |")
    posted, location = None, []
    for chunk in cleaned.split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = _DATE_RE.fullmatch(chunk)
        if match and posted is None:
            posted = parse_date(match.group(1))
        else:
            location.append(chunk)
    return posted, ", ".join(location) or None


def _text(node: Any) -> str | None:
    if node is None:
        return None
    return node.get_text(" ", strip=True) or None


def _source_id(url: str) -> str:
    match = _ID_RE.search(url.split("?")[0])
    return match.group(1) if match else url.rstrip("/").rsplit("/", 1)[-1] or url


def parse_search_html(html: str | None) -> list[dict[str, Any]]:
    """Results page → one raw card dict per ad, in page order."""
    if not html:
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    records: list[dict[str, Any]] = []
    for card in soup.select("div.resultadosBox"):
        link = card.select_one("h3.px20 a[href]") or card.select_one("h3 a[href]")
        if link is None:
            continue  # an ad whose anchor lost its href is not reachable at all
        url = urljoin(BASE, str(link.get("href")))
        posted, location = parse_meta(_text(card.select_one("span.px13.colorBlack")))
        records.append(
            {
                "url": url,
                "source_id": _source_id(url),
                "title": _text(link),
                "company": _text(card.select_one("h4.px18")),
                "location": location,
                "posted": posted,
                "teaser": _text(card.select_one("div.px13.colorGray8")),
            }
        )
    return records


def parse_total(html: str | None) -> int | None:
    """The page's own hit count ("<b>1 880</b> empregos para a sua pesquisa"), when it has one."""
    match = _TOTAL_RE.search(html or "")
    if match is None:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def _says(pattern: re.Pattern[str], text: str) -> bool:
    """True when ``pattern`` matches somewhere it is talking about the *worker*."""
    return any(
        not _NOT_ABOUT_THE_WORKER_RE.search(text[max(0, m.start() - 40) : m.start()])
        for m in pattern.finditer(text)
    )


def remote_kind(*texts: str | None) -> str:
    """``guess_remote`` plus the board's Portuguese wording (remoto/teletrabalho/híbrido)."""
    joined = " ".join(t for t in texts if t)
    hints = [
        kind
        for kind, pattern in (("hybrid", _PT_HYBRID_RE), ("remote", _PT_REMOTE_RE))
        if _says(pattern, joined)
    ]
    return guess_remote(joined, *hints)


def _description(soup: Any) -> str | None:
    body = soup.select_one("div.wucAnuncioDet")
    if body is None:
        return None
    for tag in body.select("style, script"):  # per-advertiser XSLT ships its own CSS
        tag.decompose()
    return strip_html(str(body))


def parse_detail(html: str | None, url: str, card: dict[str, Any] | None = None) -> Job | None:
    """Ad page → Job, falling back to the search card for anything the page doesn't carry."""
    from bs4 import BeautifulSoup

    card = card or {}
    soup = BeautifulSoup(html or "", "lxml")
    panel = soup.select_one("#ContentPlaceHolder_pnAnuncio") or soup

    og = soup.select_one('meta[property="og:title"]')
    title = og.get("content") if og is not None else None
    if not isinstance(title, str) or not title.strip():
        title = _MF_SUFFIX_RE.sub("", _text(panel.select_one("h1.px30")) or "")
    title = title.strip() or (card.get("title") or "").strip()
    if not title:
        return None

    posted, location = parse_meta(_text(panel.select_one("span.px13.colorBlack")))
    company = _text(panel.select_one("h2.px18")) or card.get("company")
    description = _description(soup) or card.get("teaser")
    location_raw = location or card.get("location")
    city = location_raw.split(",")[0].strip() if location_raw else None

    return Job(
        source="expressoemprego",
        source_id=card.get("source_id") or _source_id(url),
        url=url,
        title=title,
        company=company,
        description=description,
        location_raw=location_raw,
        country=guess_country(location_raw) or "PT",
        city=city or None,
        remote=remote_kind(title, location_raw, description),
        posted_at=posted or card.get("posted"),
        raw={"card": card},
    )


def card_to_job(card: dict[str, Any]) -> Job | None:
    """Normalize straight off the search card (``fetch_details: false``)."""
    return parse_detail(None, card["url"], card)


class ExpressoEmprego:
    name = "expressoemprego"
    description = "expressoemprego.pt — Portuguese job board (IT function search + ad pages)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        fetch_details = bool(ctx.opt("fetch_details", True))
        cards = self._discover(ctx)

        def convert(card: dict[str, Any]) -> Job | None:
            if not fetch_details:
                return card_to_job(card)
            return parse_detail(ctx.http.get_text(card["url"]), card["url"], card)

        for emitted, job in enumerate(safe_records(cards, convert, self.name), start=1):
            yield job
            if ctx.limit and emitted >= ctx.limit:
                return

    def _searches(self, ctx: SourceContext) -> list[dict[str, Any]]:
        """The IT function pass first, then one pass per free-text query."""
        funcao = ctx.opt("funcao", IT_FUNCAO)
        queries: Any = ctx.opt("queries", DEFAULT_QUERIES)
        if isinstance(queries, str):
            queries = [queries]
        searches = [{"funcao": funcao}] if funcao else []
        searches += [{"query": q} for q in (queries or []) if q]
        return searches

    def _discover(self, ctx: SourceContext) -> list[dict[str, Any]]:
        """Walk every search's pages, collecting cards deduped by URL."""
        max_pages = max(1, int(ctx.opt("max_pages", 5)))
        max_details = int(ctx.opt("max_details", 150))
        if ctx.limit:
            # `probe` only wants a handful: don't walk 150 ad pages to show five. Twice the
            # limit leaves room for the odd card whose ad page fails to normalize.
            max_details = min(max_details, ctx.limit * 2)
        cards: list[dict[str, Any]] = []
        seen: set[str] = set()

        for criteria in self._searches(ctx):
            harvested = 0
            for page in range(1, max_pages + 1):
                html = ctx.http.get_text(search_url(page=page, **criteria))
                found = parse_search_html(html)
                total = parse_total(html)
                if not found:
                    if page == 1 and total:
                        log.warning("%s: %s hits but no cards for %r (markup change?)",
                                    self.name, total, criteria)
                    elif page == 1:
                        # The board drops the count line too when a search matches nothing —
                        # which is what the default "C++" query does, since it slugs to "c--".
                        log.info("%s: no hits for %r", self.name, criteria)
                    break
                harvested += len(found)
                before = len(cards)
                for card in found:
                    if card["url"] in seen or len(cards) >= max_details:
                        continue
                    seen.add(card["url"])
                    cards.append(card)
                if len(cards) == before or len(cards) >= max_details:
                    break  # the page repeated what we already had, or we have enough
                if total is not None and harvested >= total:
                    break  # the board says that was the last of them
            if len(cards) >= max_details:
                break
        return cards


register(ExpressoEmprego())
