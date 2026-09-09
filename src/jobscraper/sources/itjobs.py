"""itjobs.pt — Portugal's main IT board. Two-step HTML, observed live 2026-09-09.

Search — ``GET https://www.itjobs.pt/emprego`` with the parameters of the site's own facets:

* ``q=<query>`` free text, ``page=N`` (1-based; the pager links look exactly like this),
  ``sort=date|relevance`` (the site defaults to ``relevance``; this adapter asks for ``date``
  so ``max_pages`` buys the *newest* ads rather than the most on-topic ones).
* the sidebar facets are plain query parameters too and are exposed as options:
  ``location=<id>`` (14 Lisboa, 18 Porto, 8 Coimbra, … 29 "International"),
  ``work_model=0|1|2`` (Presencial | Remoto | Híbrido), ``type=1`` (Full-time), ``contract=N``.

Results are grouped into one "daily block" per posting date::

    <div class="block borderless">
      <div class="heading"><div class="date-box"><div class="d-d"> 9</div><div class="d-m">set</div></div></div>
      <ul class="list-unstyled listing">
        <li><div class="list-title"><a class="title" href="/oferta/<id>/<slug>">…</a></div>
            <div class="list-name"><a href="/empresa/<slug>">…</a></div>
            <div class="list-details"><i class="fa fa-map-marker"></i>&nbsp;Lisboa, Porto
                                      <i class="far fa-car-building"></i>&nbsp;Híbrido</div></li>
      </ul>
    </div>

Three things about that markup:

* the date the site prints is the **day plus an abbreviated Portuguese month** ("9 set",
  "28 ago") with no year — never "hoje"/"há 3 dias", whatever other Portuguese boards do.
  ``parse_pt_date`` still understands the relative wordings (they show up inside ads) but the
  listing always needs the year inferred: a month ahead of today belongs to last year.
* ``list-details`` has no elements around its values — the icon tells you what the following
  text node means — and the schedule line is usually wrapped in an HTML *comment*, which
  BeautifulSoup happily hands back as a string. Comments are skipped explicitly.
* the "Em destaque" sidebar carries ``/oferta/`` links too, so cards are only read from
  ``ul.listing`` (the sidebar uses ``ul.item-list`` and ``div.title``, not ``div.list-title``).

Detail — the posting page carries one clean ``application/ld+json`` ``JobPosting``
(``title``, ``description`` as HTML, ``datePosted`` as a real date, ``employmentType``,
``hiringOrganization.name``, ``jobLocation[].address``) plus a visible label/value list
``.item-details li`` → ``Localidade`` ("Portugal - Coimbra"), ``Horário``, ``Modelo de
trabalho``, ``Referência``, ``Salário``. The visible work model wins over the JSON-LD:
``jobLocationType: TELECOMMUTE`` is set on plain hybrid ads as well. Some ads have no JSON-LD
at all, so title/company/date/description also fall back to ``h1.title``, ``h4.thin a``,
``.over-title small`` ("28 de Agosto de 2026") and ``.content-block`` — the last one after
dropping the action pills, the login box and a hidden watermark paragraph that echoes *our*
IP and User-Agent back into the page.

Two things the JSON-LD gets wrong and this adapter overrides: its strings are HTML-escaped
("Java &amp; React"), and every ad is stamped ``addressCountry: "PT"`` — including the ones
the board itself files under *International* (``addressLocality: "International"``, the
``location=29`` facet), which are the only ads that are not in Portugal. Those keep the label
as ``location_raw`` and get no country at all rather than a wrong one.

Not available anywhere in the HTML: technology tags and a seniority label, so ``tags`` stays
empty and ``seniority_raw`` stays None. There *is* a JSON API (``https://api.itjobs.pt``,
key by email at https://www.itjobs.pt/api) but its payload can't be verified without a key,
so this adapter is HTML-only.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from html import unescape
from typing import Any
from urllib.parse import urljoin

from jobscraper.http import strip_html
from jobscraper.models import Job
from jobscraper.sources._common import COUNTRY_NAMES, guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

BASE = "https://www.itjobs.pt"
SEARCH_URL = f"{BASE}/emprego"
DEFAULT_QUERIES = ["software developer", "C++", "engenheiro de software"]
FILTER_OPTIONS = ("location", "work_model", "type", "contract")

MONTHS = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}
_REL_DAYS = {"minuto": 0, "hora": 0, "dia": 1, "semana": 7}

_ID_RE = re.compile(r"/oferta/(\d+)")
_ABROAD_RE = re.compile(r"internacional|international|estrangeiro", re.I)
_REMOTE_PT_RE = re.compile(r"remot[oa]|teletrabalho|full\s*remote", re.I)
_HYBRID_PT_RE = re.compile(r"h[íi]brid[oa]", re.I)
_ONSITE_PT_RE = re.compile(r"presencial", re.I)
_REL_RE = re.compile(r"h[áa]\s+(\d+)\s*(minuto|hora|dia|semana|m[êe]s|mes)", re.I)
_DMY_RE = re.compile(r"(\d{1,2})\s*(?:de\s+)?([a-zç]+)\.?(?:\s*(?:de\s+)?(\d{4}))?", re.I)


def _text(node: Any) -> str | None:
    if node is None:
        return None
    text = " ".join(node.get_text(" ", strip=True).replace("\xa0", " ").split())
    return text or None


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def parse_pt_date(value: Any, today: date | None = None) -> datetime | None:
    """Portuguese posting dates → UTC midnight.

    Understands the day box ("9 set"), the posting page's long form ("28 de Agosto de 2026")
    and the relative wordings ads use ("hoje", "ontem", "há 3 dias", "há 2 semanas"). When the
    year is missing it is inferred from ``today``: a date that would land in the future belongs
    to the previous year. Anything else falls through to the shared ISO/epoch parser.
    """
    if value in (None, ""):
        return None
    text = " ".join(str(value).split()).lower()
    if not text:
        return None
    today = today or datetime.now(UTC).date()

    if text.startswith("hoje"):
        return _midnight(today)
    if text.startswith("anteontem"):
        return _midnight(today - timedelta(days=2))
    if text.startswith("ontem"):
        return _midnight(today - timedelta(days=1))

    relative = _REL_RE.search(text)
    if relative:
        step = _REL_DAYS.get(relative.group(2).lower(), 30)  # mês/meses ≈ 30 days
        return _midnight(today - timedelta(days=int(relative.group(1)) * step))

    match = _DMY_RE.search(text)
    if match:
        month = MONTHS.get(match.group(2)[:3])
        if month:
            year = int(match.group(3)) if match.group(3) else today.year
            try:
                day = date(year, month, int(match.group(1)))
            except ValueError:
                return None
            if not match.group(3) and day > today + timedelta(days=1):
                day = day.replace(year=year - 1)
            return _midnight(day)
    return parse_date(value)


# ------------------------------------------------------------------------------ search page


def _card_details(li: Any) -> dict[str, str]:
    """``list-details`` → {location, work_model, schedule}, keyed by the icon in front."""
    from bs4.element import Comment

    box = li.select_one(".list-details")
    if box is None:
        return {}
    found: dict[str, str] = {}
    for icon in box.find_all("i"):
        classes = " ".join(icon.get("class") or [])
        chunks: list[str] = []
        for sibling in icon.next_siblings:
            if isinstance(sibling, Comment):  # the schedule line is commented out on most ads
                continue
            if getattr(sibling, "name", None) == "i":
                break
            chunks.append(sibling.get_text(" ") if getattr(sibling, "name", None) else str(sibling))
        text = " ".join("".join(chunks).replace("\xa0", " ").split())
        if not text:
            continue
        if "map-marker" in classes:
            found.setdefault("location", text)
        elif "car-building" in classes:
            found.setdefault("work_model", text)
        elif "clock" in classes:
            found.setdefault("schedule", text)
    return found


def parse_search_html(html: str | None) -> list[dict[str, Any]]:
    """Search page → one raw card dict per posting, in document order."""
    if not html:
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    cards: list[dict[str, Any]] = []
    for listing in soup.select("ul.listing"):
        block = listing.find_parent(class_="block")
        date_text = _text(block.select_one(".date-box")) if block is not None else None
        for li in listing.find_all("li", recursive=False):
            link = li.select_one(".list-title a[href]")
            if link is None:
                continue  # a pulled ad keeps its <li> but loses the anchor
            details = _card_details(li)
            cards.append(
                {
                    "url": urljoin(BASE, str(link.get("href"))),
                    "title": _text(link) or link.get("title"),
                    "company": _text(li.select_one(".list-name")),
                    "location": details.get("location"),
                    "work_model": details.get("work_model"),
                    "schedule": details.get("schedule"),
                    "date_text": date_text,
                }
            )
    return cards


# ------------------------------------------------------------------------------ posting page


def _job_posting(soup: Any) -> dict[str, Any]:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (ValueError, TypeError):
            continue
        for node in data if isinstance(data, list) else [data]:
            if isinstance(node, dict) and str(node.get("@type", "")).lower() == "jobposting":
                return node
    return {}


def _first(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return {}


def _item_details(soup: Any) -> dict[str, str]:
    """``.item-details li`` → {"localidade": "Portugal - Coimbra", "salário": "…", …}."""
    fields: dict[str, str] = {}
    for li in soup.select(".item-details li"):
        label, value = _text(li.select_one(".info .title")), _text(li.select_one(".info .field"))
        if label and value:
            fields.setdefault(label.rstrip(":").lower(), value)
    return fields


def _field(fields: dict[str, str], prefix: str) -> str | None:
    """Look a label up by prefix, so the accents in Horário/Salário/Referência don't matter."""
    for label, value in fields.items():
        if label.startswith(prefix):
            return value
    return None


def _location_parts(text: str | None) -> tuple[str | None, str | None]:
    """"Portugal - Coimbra" → ("Coimbra", "Portugal"); "Lisboa, Porto" → ("Lisboa, Porto", None)."""
    if not text:
        return None, None
    parts = [p.strip() for p in re.split(r"\s+-\s+", text) if p.strip()]
    country = parts.pop(0) if len(parts) > 1 and guess_country(parts[0]) else None
    return " - ".join(parts) or None, country


def _city(location_raw: Any) -> str | None:
    """First real town in "Lisboa, Porto"; None for a bare country or the abroad bucket."""
    if not isinstance(location_raw, str):
        return None
    for part in location_raw.split(","):
        part = part.strip()
        if not part or part.lower() in COUNTRY_NAMES or _ABROAD_RE.search(part):
            continue
        return part
    return None


def _abroad(*texts: Any) -> bool:
    """True when the board filed the ad under its "International" bucket (location id 29)."""
    return any(isinstance(t, str) and _ABROAD_RE.search(t) for t in texts)


def _ld_text(value: Any) -> str | None:
    """A JSON-LD string, with its HTML entities decoded ("Java &amp; React")."""
    if not isinstance(value, str) or not value.strip():
        return None
    return unescape(value).strip() or None


def _remote_kind(work_model: str | None, *texts: str | None) -> str:
    """The board's own work-model label first, then the Portuguese words, then the shared guess."""
    for pattern, kind in ((_REMOTE_PT_RE, "remote"), (_HYBRID_PT_RE, "hybrid"), (_ONSITE_PT_RE, "onsite")):
        if work_model and pattern.search(work_model):
            return kind
    joined = " ".join(t for t in texts if t)
    if _HYBRID_PT_RE.search(joined):
        return "hybrid"
    if _REMOTE_PT_RE.search(joined):
        return "remote"
    return guess_remote(*texts)


def _description(posting: dict[str, Any], soup: Any) -> str | None:
    text = strip_html(posting.get("description")) if isinstance(posting.get("description"), str) else None
    if text:
        return text
    block = soup.select_one(".content-block")
    if block is None:
        return None
    for junk in block.select(".action-pills, .signup-box, script, style, [style*='visibility: hidden']"):
        junk.decompose()
    return strip_html(str(block))


def parse_detail(
    html: str | None,
    url: str,
    card: dict[str, Any] | None = None,
    today: date | None = None,
) -> Job | None:
    """Posting page (or nothing, when details are switched off) + search card → Job."""
    from bs4 import BeautifulSoup

    card = card or {}
    soup = BeautifulSoup(html or "", "lxml")
    posting = _job_posting(soup)
    fields = _item_details(soup)

    title = _ld_text(posting.get("title")) or card.get("title") or _text(soup.select_one("h1.title"))
    if not isinstance(title, str) or not title.strip():
        return None

    company = (
        _ld_text(_first(posting.get("hiringOrganization")).get("name"))
        or card.get("company")
        or _text(soup.select_one(".job-header h4"))
    )

    address = _first(_first(posting.get("jobLocation")).get("address"))
    locality = address.get("addressLocality") or address.get("addressRegion")
    detail_location, country_label = _location_parts(_field(fields, "localidade"))
    location_raw = card.get("location") or detail_location
    description = _description(posting, soup)
    work_model = card.get("work_model") or _field(fields, "modelo de trabalho")

    country = guess_country(_ld_text(address.get("addressCountry"))) or guess_country(country_label, location_raw)
    if _abroad(location_raw, locality):
        # The board stamps "PT" on its International ads too; the visible bucket is the truth,
        # and which country the job is really in is then simply unknown.
        country = None if country == "PT" else country
    elif not country:
        country = "PT"  # a Portuguese board: anything not flagged as abroad is domestic

    employment = _field(fields, "hor") or card.get("schedule") or posting.get("employmentType")

    return Job(
        source="itjobs",
        source_id=(_ID_RE.search(url).group(1) if _ID_RE.search(url) else url),
        url=url,
        title=title.strip(),
        company=company.strip() if isinstance(company, str) and company.strip() else None,
        description=description,
        location_raw=location_raw,
        country=country,
        city=_city(locality) or _city(location_raw),
        remote=_remote_kind(work_model, title, location_raw, description),
        employment_type=employment if isinstance(employment, str) else None,
        salary_text=_field(fields, "sal"),
        posted_at=(
            parse_date(posting.get("datePosted"))
            or parse_pt_date(_text(soup.select_one(".over-title small")), today)
            or parse_pt_date(card.get("date_text"), today)
        ),
        raw={"card": card, "jsonld": posting or None, "fields": fields},
    )


class ITJobs:
    name = "itjobs"
    description = "itjobs.pt — Portugal's main IT board (HTML search + JSON-LD posting pages)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        fetch_details = bool(ctx.opt("fetch_details", True))
        max_details = int(ctx.opt("max_details", 150))
        today = datetime.now(UTC).date()
        fetched = 0

        def convert(card: dict[str, Any]) -> Job | None:
            nonlocal fetched
            html = None
            if fetch_details and fetched < max_details:
                fetched += 1
                html = ctx.http.get_text(card["url"])
            return parse_detail(html, card["url"], card, today=today)

        for emitted, job in enumerate(safe_records(self._iter_cards(ctx), convert, self.name), start=1):
            yield job
            if ctx.limit and emitted >= ctx.limit:
                return

    def _params(self, ctx: SourceContext, query: str, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {"q": query, "sort": str(ctx.opt("sort", "date"))}
        if page > 1:
            params["page"] = page
        for key in FILTER_OPTIONS:
            value = ctx.opt(key)
            if value not in (None, ""):
                params[key] = value
        return params

    def _iter_cards(self, ctx: SourceContext) -> Iterator[dict[str, Any]]:
        """Search cards for every query, deduped by posting URL. Lazy, so ``limit`` stops early."""
        queries = ctx.opt("queries", DEFAULT_QUERIES) or DEFAULT_QUERIES
        if isinstance(queries, str):
            queries = [queries]
        max_pages = max(1, int(ctx.opt("max_pages", 5)))
        seen: set[str] = set()

        for query in queries:
            for page in range(1, max_pages + 1):
                cards = parse_search_html(ctx.http.get_text(SEARCH_URL, params=self._params(ctx, query, page)))
                if not cards:
                    if page == 1:
                        log.warning("%s: no job cards for %r (no results, or markup change)", self.name, query)
                    break
                fresh = [c for c in cards if c["url"] not in seen]
                if not fresh:
                    break  # the page only repeated what we already have
                seen.update(c["url"] for c in fresh)
                yield from fresh


register(ITJobs())
