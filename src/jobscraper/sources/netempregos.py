"""net-empregos.com — Portugal's largest general job board. Two-step HTML, no usable feed.

Why not the RSS feed
--------------------
The site links ``https://www.net-empregos.com/rssfeed.asp`` from every page and it does carry
title / ``dc:creator`` (company) / link / ``pubDate``. It is still the wrong door (measured
2026-09-09):

* it is **not filterable** — ``rssfeed.asp?categoria=5`` (and any other parameter) returns
  byte-for-byte the same document as the bare URL: the newest 250 ads of the *whole* board.
  Of those 250, six were in any Informática category and two in *Programação*.
* every ``<description>`` is the first ~250 characters of the ad, cut mid-word with an
  ellipsis, so a detail fetch would be needed anyway.

The HTML search below scopes to an IT category server-side (~55 pages × 14 ads for
*Informática ( Programação )* alone) and the posting pages carry full JSON-LD, so that is
what this adapter uses.

Search
------
``GET https://www.net-empregos.com/pesquisa-empregos.asp`` with the fields of the site's own
form (``#pesquisa``): ``chaves`` (free text), ``cidade`` (free text), ``categoria``, ``zona``,
``tipo``, and ``page`` for paging. Page 1 omits ``page`` — that is what the site's own pager
links do. ``0`` means "all" for the three select fields.

``categoria`` ids come from the form's own ``<select>`` (read live 2026-09-09). The IT ones
are :data:`IT_CATEGORIES`; **3 is not among them** — that id is not in the select at all and
searching with it returns "no hits" for everything. ``tipo`` is
1 = Tempo Inteiro, 2 = Part-Time, 3 = Estágio, 4 = Teletrabalho.

A result card is ``div.job-item.media`` (a paid one is ``div.job-item.job-item-destaque.media``
and carries a ``<label>Oferta em Destaque</label>``). Its ``h2 > a`` holds the title and the
posting URL — sometimes with an **unquoted** href attribute. The four ``<li>``s under
``.job-ad-item`` are identified by the icon inside them, not by position or class:
``i.flaticon-calendar`` = date, ``i.flaticon-pin`` = zone, ``i.fa-tags`` = category,
``i.flaticon-work`` = company.

A search with no hits does **not** render an empty list: it prints "Não encontrámos nenhuma
oferta de emprego para a sua pesquisa" and then a ``div.related-Jobs`` block whose cards are
ordinary ``.job-item`` divs advertising completely unrelated jobs (a page number past the last
one behaves the same way). :func:`parse_search_html` therefore stops at ``.related-Jobs``;
taking every ``.job-item`` on the page would import lorry drivers as C++ postings.

Posting page
------------
``https://www.net-empregos.com/<id>/<slug>/`` carries a schema.org ``JobPosting`` in a single
``application/ld+json`` block: ``title``, ``description`` (HTML, with the ad's own URL appended
at the end — stripped here), ``datePosted``, ``validThrough``, ``industry``, ``employmentType``
(FULL_TIME / PART_TIME / INTERN / …), ``jobLocationType`` (``TELECOMMUTE`` on Teletrabalho ads)
and ``hiringOrganization.name``. Older ads render without it, so the visible markup
(``h1.title``, ``.candidate-listing-footer``, ``.job-description``) is the fallback.

Dates come in two shapes, neither of them ISO: the cards and the footer write ``28-8-2026``
(D-M-YYYY, unpadded) and the JSON-LD writes ``2026-8-28 15:08 UTC`` (Y-M-D, unpadded, always
UTC). :func:`parse_pt_date` reads both; ``_common.parse_date`` reads neither.

Encoding: the server answers ``Content-Type: text/html`` with **no charset** while the bytes
are ISO-8859-1 (the document's own ``<meta>`` says so). httpx then decodes as UTF-8 and every
Portuguese accent turns into U+FFFD, which is why :func:`get_html` decodes the raw bytes
itself — UTF-8 first (so fixtures and the on-disk HTTP cache still work), Windows-1252 when that
fails.

Location: a posting open to the whole country writes the placeholder ``( Todas as Zonas )``
in both the card and ``addressLocality``; it is not a town and is dropped.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from jobscraper.http import Http, _raise_for_status, strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

BASE = "https://www.net-empregos.com/"
SEARCH_URL = "https://www.net-empregos.com/pesquisa-empregos.asp"
FALLBACK_ENCODING = "cp1252"  # superset of ISO-8859-1: 0x80-0x9F become dashes and quotes, not control chars

# The ``categoria`` select, Informática rows only (read from the live form 2026-09-09).
IT_CATEGORIES: dict[str, int] = {
    "Informática ( Programação )": 5,
    "Informática ( Formação )": 34,
    "Informática ( Internet )": 35,
    "Informática ( Multimedia )": 36,
    "Informática ( Gestão de Redes )": 37,
    "Informática ( Analise de Sistemas )": 38,
    "Informática ( Técnico de Hardware )": 49,
    "Informática (Comercial/Gestor de Conta)": 56,
}
DEFAULT_CATEGORY = IT_CATEGORIES["Informática ( Programação )"]
DEFAULT_QUERIES = ["programador", "software developer", "C++", "engenheiro de software"]

_ID_RE = re.compile(r"/(\d{4,})(?:/|$)")
# "9-9-2026" (card / footer) and "2026-8-28 15:08 UTC" (JSON-LD datePosted).
_DMY_RE = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")
_YMD_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?\s*(?:UTC)?$")
# "( Todas as Zonas )" / "( Todas as Categorias )": the select's own "no filter" label.
_PLACEHOLDER_RE = re.compile(r"^\(\s*tod[ao]s\s+a?s\s+.*\)$", re.I)
_SELF_URL_RE = re.compile(r"\s*https?://(?:www\.)?net-empregos\.com/\S*\s*$", re.I)
_EMPLOYMENT_RE = re.compile(r"tipo\s+de\s+oferta\s*:\s*([^\n]{1,60})", re.I)
# Portuguese words `_common.guess_remote` doesn't know. "remoto" never matches English "remote".
_PT_HYBRID_RE = re.compile(r"h[ií]brid[oa]s?", re.I)
_PT_REMOTE_RE = re.compile(r"teletrabalho|remot[oa]s?|remotamente", re.I)
_SENIORITY_RE = re.compile(
    r"\b(s[ée]nior|senior|j[úu]nior|junior|est[áa]gi[oa]ri[oa]|est[áa]gio|trainee|lead|principal)\b",
    re.I,
)

# Icon class inside a card's <li> → the field that <li> holds.
_ICONS = {
    "flaticon-calendar": "posted",
    "flaticon-pin": "location",
    "fa-tags": "category",
    "flaticon-work": "company",
}


# ------------------------------------------------------------------------------ helpers
def get_html(http: Http, url: str, **kw: Any) -> str:
    """Fetch a page and decode it. See the module docstring: the server declares no charset."""
    resp = http.get(url, **kw)
    _raise_for_status(resp)
    raw = resp.content
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(FALLBACK_ENCODING, errors="replace")


def parse_pt_date(value: Any) -> datetime | None:
    """``9-9-2026`` or ``2026-8-28 15:08 UTC`` → an aware datetime, else ``_common.parse_date``."""
    if not value:
        return None
    text = str(value).strip()
    match = _DMY_RE.match(text)
    if match:
        day, month, year = (int(g) for g in match.groups())
        try:
            return datetime(year, month, day, tzinfo=UTC)
        except ValueError:  # a well-formed but impossible date, e.g. "31-2-2026"
            return None
    match = _YMD_RE.match(text)
    if match:
        year, month, day = (int(g) for g in match.groups()[:3])
        hour, minute = (int(g) if g else 0 for g in match.groups()[3:])
        try:
            return datetime(year, month, day, hour, minute, tzinfo=UTC)
        except ValueError:
            return None
    return parse_date(text)


def _clean(text: str | None) -> str | None:
    """Collapse whitespace; drop the board's "( Todas as ... )" placeholder labels."""
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    if not text or _PLACEHOLDER_RE.match(text):
        return None
    return text


def source_id(url: str) -> str:
    """``https://www.net-empregos.com/15867575/senior-c/`` → ``15867575``."""
    match = _ID_RE.search(urlsplit(url).path)
    return match.group(1) if match else url


def _seniority(title: str | None) -> str | None:
    match = _SENIORITY_RE.search(title or "")
    return match.group(1) if match else None


def _remote(title: str | None, location: str | None, text: str | None, telecommute: bool) -> str:
    """``guess_remote`` plus the Portuguese wording (teletrabalho / remoto / híbrido)."""
    if telecommute:
        return "remote"
    joined = " ".join(t for t in (title, location, text) if t)
    if _PT_HYBRID_RE.search(joined):
        return "hybrid"
    if _PT_REMOTE_RE.search(joined):
        return "remote"
    return guess_remote(title, location, text)


def _description(html: str | None) -> str | None:
    """Strip the markup and the site's own URL, which it appends to every ad."""
    text = strip_html(html)
    return _SELF_URL_RE.sub("", text).strip() or None if text else None


# ------------------------------------------------------------------------------ search page
def _card_fields(card: Any) -> dict[str, str | None]:
    """The ``<li>``s under ``.job-ad-item``, keyed by the icon each one carries."""
    fields: dict[str, str | None] = {}
    for li in card.select("li"):
        icon = li.find("i")
        classes = icon.get("class") or [] if icon is not None else []
        key = next((_ICONS[c] for c in classes if c in _ICONS), None)
        if key and key not in fields:
            fields[key] = _clean(li.get_text(" ", strip=True))
    return fields


def parse_search_html(html: str | None) -> list[dict[str, Any]]:
    """Search page → one raw card dict per posting, stopping at the "similar jobs" block."""
    if not html:
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    records: list[dict[str, Any]] = []
    for node in soup.find_all("div"):
        classes = node.get("class") or []
        if "related-Jobs" in classes:
            break  # everything below is an unrelated "Ofertas de Emprego Semelhantes" ad
        if "job-item" not in classes:
            continue
        link = node.select_one("h2 a[href]") or node.select_one("a.apply-button[href]")
        if link is None:
            continue
        url = urljoin(BASE, str(link.get("href")))
        fields = _card_fields(node)
        records.append(
            {
                "url": url,
                "source_id": source_id(url),
                "title": _clean(link.get_text(" ", strip=True)),
                "company": fields.get("company"),
                "location": fields.get("location"),
                "category": fields.get("category"),
                "posted": fields.get("posted"),
                "featured": "job-item-destaque" in classes,
            }
        )
    return records


# ------------------------------------------------------------------------------ posting page
def _json_ld(soup: Any) -> dict[str, Any]:
    """The page's ``JobPosting`` node, or ``{}`` when it is missing or malformed.

    ``strict=False`` is not optional: the ad text is pasted into the JSON with its raw CR
    characters intact (``...</b>\\r<BR>\\r<BR>Somos a Log...``), which strict JSON rejects as
    an "invalid control character". Every posting page observed 2026-09-09 failed that way.
    """
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json")}):
        try:
            data = json.loads(script.string or script.get_text() or "", strict=False)
        except (ValueError, TypeError):
            continue
        for node in data if isinstance(data, list) else [data]:
            if isinstance(node, dict) and "JobPosting" in str(node.get("@type", "")):
                return node
    return {}


def _visible(soup: Any) -> dict[str, str | None]:
    """Title / description / footer fields read off the rendered page (no-JSON-LD fallback)."""
    title = soup.select_one("h1.title") or soup.select_one("h1")
    body = soup.select_one(".job-description")
    if body is not None:
        for tag in body.select("h2, script, style"):
            tag.decompose()
    footer = soup.select_one(".candidate-listing-footer")
    fields = _card_fields(footer) if footer is not None else {}
    return {
        "title": _clean(title.get_text(" ", strip=True)) if title is not None else None,
        "description": _description(body.decode_contents()) if body is not None else None,
        "company": fields.get("company"),
        "location": fields.get("location"),
        "category": fields.get("category"),
        "posted": fields.get("posted"),
    }


def _address(posting: dict[str, Any]) -> dict[str, Any]:
    place = posting.get("jobLocation")
    if isinstance(place, list):
        place = next((p for p in place if isinstance(p, dict)), {})
    address = place.get("address") if isinstance(place, dict) else None
    return address if isinstance(address, dict) else {}


def _str(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def parse_detail(html: str | None, url: str, card: dict[str, Any] | None = None) -> Job | None:
    """Posting page → Job, from the JSON-LD when present, else the markup, else the card."""
    from bs4 import BeautifulSoup

    card = card or {}
    soup = BeautifulSoup(html or "", "lxml")
    posting = _json_ld(soup)
    page = _visible(soup)

    title = _str(posting.get("title")) or card.get("title") or page["title"]
    if not title:
        return None

    org = posting.get("hiringOrganization")
    company = _str(org.get("name")) if isinstance(org, dict) else None
    address = _address(posting)
    location = (
        _clean(_str(address.get("addressLocality")) or _str(address.get("addressRegion")))
        or _clean(card.get("location"))
        or page["location"]
    )
    description = _description(_str(posting.get("description"))) or page["description"]
    category = _str(posting.get("industry")) or card.get("category") or page["category"]
    employment = _str(posting.get("employmentType"))
    if not employment and description:
        match = _EMPLOYMENT_RE.search(description)
        employment = _clean(match.group(1)) if match else None
    telecommute = str(posting.get("jobLocationType") or "").strip().upper() == "TELECOMMUTE"

    return Job(
        source="netempregos",
        source_id=source_id(url),
        url=url,
        title=title,
        company=company or card.get("company") or page["company"],
        description=description,
        location_raw=location,
        country=guess_country(_str(address.get("addressCountry")), location) or "PT",
        city=location,
        remote=_remote(title, location, description, telecommute),
        seniority_raw=_seniority(title),
        employment_type=employment,
        tags=[category] if category else [],
        posted_at=parse_pt_date(posting.get("datePosted"))
        or parse_pt_date(card.get("posted"))
        or parse_pt_date(page["posted"]),
        raw={
            "card": card or None,
            "jsonld": posting or None,
            "valid_through": posting.get("validThrough"),
        },
    )


class NetEmpregos:
    name = "netempregos"
    description = "net-empregos.com — Portuguese general job board (IT category search + JSON-LD)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        queries = ctx.opt("queries", DEFAULT_QUERIES) or DEFAULT_QUERIES
        if isinstance(queries, str):
            queries = [queries]
        categories = ctx.opt("category", DEFAULT_CATEGORY)
        if isinstance(categories, (str, int)):
            categories = [categories]
        categories = [str(c) for c in (categories or [DEFAULT_CATEGORY])]
        max_pages = max(1, int(ctx.opt("max_pages", 5)))
        fetch_details = bool(ctx.opt("fetch_details", True))
        max_details = int(ctx.opt("max_details", 150))
        base_params = {
            "cidade": str(ctx.opt("city", "")),
            "zona": str(ctx.opt("zone", 0)),
            "tipo": str(ctx.opt("kind", 0)),
        }

        seen: set[str] = set()
        emitted = 0
        details = 0

        def convert(card: dict[str, Any]) -> Job | None:
            nonlocal details
            html = None
            if fetch_details and details < max_details:
                details += 1
                html = get_html(ctx.http, card["url"])
            return parse_detail(html, card["url"], card)

        for query in queries:
            for category in categories:
                for page in range(1, max_pages + 1):
                    params = {"chaves": str(query), "categoria": category, **base_params}
                    if page > 1:
                        params["page"] = str(page)
                    cards = parse_search_html(get_html(ctx.http, SEARCH_URL, params=params))
                    if not cards:
                        if page == 1:
                            log.info("%s: no hits for %r in category %s", self.name, query, category)
                        break
                    fresh = [c for c in cards if c["url"] not in seen]
                    seen.update(c["url"] for c in fresh)
                    if not fresh:
                        break  # the page only repeated postings we already have
                    for job in safe_records(fresh, convert, self.name):
                        yield job
                        emitted += 1
                        if ctx.limit and emitted >= ctx.limit:
                            return


register(NetEmpregos())
