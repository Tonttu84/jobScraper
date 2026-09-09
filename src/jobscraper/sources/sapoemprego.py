"""emprego.sapo.pt — SAPO Emprego, one of Portugal's biggest job boards. JSON search + JSON-LD detail.

Observed live 2026-09-09.

Search — the ``/pesquisa?q=`` page everybody links to is a Vue shell: its cards are rendered
client-side and the ``application/ld+json`` on it is only ``WebSite``/``Organization``, never a
JobPosting list, so there is nothing to scrape there. The page's ``<search-results-component>``
does carry the first page of results as a JSON prop, and its ``:search-link`` prop names the
endpoint the component itself calls for every page after that::

    GET https://emprego.sapo.pt/offers/search?pesquisa=<term>&pagina=<n>
        X-Requested-With: XMLHttpRequest        # without it the route answers HTTP 400

→ ``{"offers": [...], "pagination": {"total": 47, "page": 1, "size": 9, "offers_total": 47},
     "filters": {...}, "amounts": {...}}``

Two traps in that response:

* ``page`` is spelled ``pagina`` in the query string. A ``page=`` parameter is silently
  ignored and every request comes back as page 1 — which looks like a working scraper that
  re-scrapes the same nine ads until ``max_pages`` runs out.
* ``offers`` mixes real postings with paid image banners, which have no ``offer_name`` and an
  empty ``link``. They are skipped.
* ``/pesquisa?q=<term>`` puts the term in ``side_filters.qualification`` instead of ``search``
  and returns an unfiltered list; ``/offers?pesquisa=`` and this endpoint are the real search.

The route is throttled at **15 requests per minute** (measured 2026-09-09: request 16 inside
the same minute fails, and it recovers about a minute later). It answers **HTTP 419** — Laravel's
"page expired", complete with a fresh session cookie and an empty JSON body — where every other
board would say 429, so the shared client's 429/5xx backoff never sees it. Hence ``search_delay``
(4.5 s between search calls, i.e. ~13/min) and a single pause-and-retry on a 419. Posting pages
are a different route and are not part of that budget.

An offer record::

    {"id": "<uuid>", "publication_date": "2026-08-27", "job_country": "Portugal",
     "job_district": "Lisboa", "job_district_all": false, "job_municipality": "",
     "location": "Lisboa", "job_work_hours": "Full-Time", "offer_name": "Software Developer",
     "offer_pitch": null, "job_description": "<250 chars of plain text, truncated>",
     "company_name": "Olisipo", "anonymous": false, "remote_work": false,
     "canonical": "software-developer", "company_canonical": "olisipo",
     "link": "https://emprego.sapo.pt/offers/software-developer?id=<uuid>"}

``remote_work`` is dead: it is ``false`` on every record, including ads whose own location
reads "Remoto", so it is ignored. Dates are already ISO (``publication_date``); the
``publication_date_string`` next to it is a bucket label ("last_day"), not a date, and no
Portuguese date string ever has to be parsed.

Detail — ``link`` is a normal page carrying a proper schema.org ``JobPosting``::

    {"@type": "JobPosting", "datePosted": "2026-08-27", "title", "description" (HTML),
     "validThrough", "industry", "url", "employmentType": "FULL_TIME",
     "jobLocation": {"@type": "Place", "address": {"addressLocality", "addressRegion",
                     "addressCountry": "PT"}},
     "hiringOrganization": {"name": ...},
     "jobLocationType": "TELECOMMUTE",                       # remote ads only
     "applicantLocationRequirements": {"name": "Portugal"}}  # remote ads only

It is fetched because the listing's ``job_description`` is cut off at 250 characters.
The page also renders a "Resumo da Oferta" block (``div.metadata li.<field>``) with the
site's own labels: ``workhome`` (Presencial / Teletrabalho / Híbrido), ``salary``
("A definir" when there is none), ``time``, ``contract`` ("Sem termo"), ``category``,
``positions``. Nothing anywhere labels seniority, so ``seniority_raw`` stays None.

Work model: ``li.workhome`` and ``jobLocationType`` are the structured signals; SAPO also
appends its own sentence to the JSON-LD description ("Vaga em regime 100% remoto." /
"Regime de trabalho híbrido."). The *description* is only scanned for Portuguese work-model
words when none of those exist — a perk line like "Formação Presencial e Remota no Learning
Center" otherwise turns an on-site ad remote. Title and location are always scanned: employers
routinely write "– Hybrid (Porto)" in the title while ticking "Presencial" in the form.

Countries come back as Portuguese names ("Portugal", "Colômbia", "Angola"), which
``guess_country`` does not know; a small local table maps the ones the board actually uses.
A record with no country at all is Portuguese — that is what the board is — but an unmapped
foreign name stays None rather than being filed under PT.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterable, Iterator
from typing import Any

from jobscraper.http import SourceHTTPError, strip_html
from jobscraper.models import Job
from jobscraper.sources._common import COUNTRY_NAMES, guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

SEARCH_API = "https://emprego.sapo.pt/offers/search"
PAGE_SIZE = 9  # the endpoint's own page size; `pagination.size` says the same
XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}

THROTTLED = 419  # what this board answers instead of 429 once the search budget is spent
SEARCH_DELAY = 4.5  # seconds between search calls: the route allows 15 a minute
THROTTLE_PAUSE = 60.0  # how long to wait out a 419 before the one retry

DEFAULT_QUERIES = ["software developer", "programador", "C++", "engenheiro de software"]

# The board's own work-model labels (li.workhome on the posting page).
WORK_MODELS = {"presencial": "onsite", "teletrabalho": "remote", "remoto": "remote",
               "híbrido": "hybrid", "hibrido": "hybrid"}

_PT_REMOTE_RE = re.compile(r"\b(teletrabalho|remot[oa]s?)\b", re.I)
_PT_HYBRID_RE = re.compile(r"\bh[íi]brid[oa]s?\b", re.I)
# SAPO's own sentence, appended to the JSON-LD description of a remote/hybrid ad.
_LD_NOTE_RE = re.compile(r"regime (?:de trabalho h[íi]brido|100% remoto)", re.I)
_PLACEHOLDER_SALARY_RE = re.compile(r"^(a definir|n/?d|-)$", re.I)

# Portuguese country names the board uses; guess_country only knows the English/native ones.
PT_COUNTRY_NAMES: dict[str, str] = {
    "portugal": "PT", "espanha": "ES", "frança": "FR", "alemanha": "DE", "reino unido": "GB",
    "países baixos": "NL", "holanda": "NL", "bélgica": "BE", "luxemburgo": "LU", "suíça": "CH",
    "irlanda": "IE", "itália": "IT", "áustria": "AT", "dinamarca": "DK", "suécia": "SE",
    "noruega": "NO", "finlândia": "FI", "islândia": "IS", "polónia": "PL", "polonia": "PL",
    "chéquia": "CZ", "república checa": "CZ", "eslováquia": "SK", "hungria": "HU",
    "roménia": "RO", "bulgária": "BG", "grécia": "GR", "croácia": "HR", "eslovénia": "SI",
    "lituânia": "LT", "letónia": "LV", "estónia": "EE", "malta": "MT", "chipre": "CY",
    "brasil": "BR", "angola": "AO", "moçambique": "MZ", "cabo verde": "CV",
    "são tomé e príncipe": "ST", "guiné-bissau": "GW", "timor-leste": "TL",
    "colômbia": "CO", "colombia": "CO", "méxico": "MX", "estados unidos": "US", "canadá": "CA",
    "emirados árabes unidos": "AE", "arábia saudita": "SA", "catar": "QA", "marrocos": "MA",
    "áfrica do sul": "ZA", "china": "CN", "índia": "IN", "japão": "JP", "austrália": "AU",
    "suiça": "CH", "belgica": "BE",
}


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _first_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return {}


def _has_type(node: Any, wanted: str) -> bool:
    types = node.get("@type") if isinstance(node, dict) else None
    if isinstance(types, str):
        return types.lower() == wanted.lower()
    if isinstance(types, list):
        return any(isinstance(t, str) and t.lower() == wanted.lower() for t in types)
    return False


def parse_search(payload: Any) -> list[dict[str, Any]]:
    """Search response → the real postings in it (paid image banners have no ``offer_name``)."""
    offers = payload.get("offers") if isinstance(payload, dict) else payload
    if not isinstance(offers, list):
        raise SourceHTTPError(f"sapoemprego: search response carries no offer list ({str(payload)[:120]!r})")
    return [o for o in offers if isinstance(o, dict) and _text(o.get("offer_name")) and _text(o.get("link"))]


def page_total(payload: Any) -> int | None:
    """``pagination.total`` — how many postings the query has in all, when the endpoint says."""
    pagination = payload.get("pagination") if isinstance(payload, dict) else None
    total = pagination.get("total") if isinstance(pagination, dict) else None
    try:
        return int(total)
    except (TypeError, ValueError):
        return None


def parse_detail_html(html: str | None) -> dict[str, Any]:
    """Posting page → ``{"jsonld": <JobPosting>, "meta": {"workhome": "Presencial", ...}}``."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    posting: dict[str, Any] = {}
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (ValueError, TypeError):
            continue
        for node in data if isinstance(data, list) else [data]:
            if _has_type(node, "JobPosting"):
                posting = node
    meta: dict[str, str] = {}
    for item in soup.select("div.metadata li"):
        classes = item.get("class") or []
        text = item.get_text(" ", strip=True)
        if classes and text:
            meta.setdefault(str(classes[0]), text)
    return {"jsonld": posting, "meta": meta}


def _country(name: str | None) -> str | None:
    name = _text(name)
    if not name:
        return None
    if name.lower() in PT_COUNTRY_NAMES:
        return PT_COUNTRY_NAMES[name.lower()]
    return guess_country(name)


def _work_model(meta_label: str | None, jsonld: dict[str, Any], *, title: str | None,
                location: str | None, description: str | None) -> str:
    """Remote/hybrid/onsite, structured signals first, ad text second (see the module docstring)."""
    label = WORK_MODELS.get((_text(meta_label) or "").lower())
    if label is None and str(jsonld.get("jobLocationType") or "").strip().upper() == "TELECOMMUTE":
        label = "remote"
    if label is None:
        note = _LD_NOTE_RE.search(jsonld.get("description") or "")
        if note:
            label = "hybrid" if _PT_HYBRID_RE.search(note.group(0)) else "remote"

    texts = [title, location] if label else [title, location, description]
    joined = " ".join(t for t in texts if t)
    if _PT_HYBRID_RE.search(joined):
        return "hybrid"
    if _PT_REMOTE_RE.search(joined):
        return "remote"
    guessed = guess_remote(joined)  # the English words guess_remote already knows
    if guessed != "unknown":
        return guessed
    return label or "unknown"


def _employment_type(*values: Any) -> str | None:
    for value in values:
        text = _text(value)
        if text:
            return re.sub(r"[\s-]+", "_", text).lower()
    return None


def _is_country_name(text: str) -> bool:
    """"Portugal" is a country, "Lisboa" is a city — ``guess_country`` answers PT to both."""
    low = text.strip().lower()
    return low in PT_COUNTRY_NAMES or low in COUNTRY_NAMES


def _first_town(text: str) -> str | None:
    """The town in a location label. Employers paste whole postal addresses into it
    ("Av. El Dorado #92 - 32, Bogotá,"), so the first part with no house number wins."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    for part in parts:
        if not any(ch.isdigit() for ch in part) and not _is_country_name(part):
            return part
    return parts[0] if parts else None


def _city(locality: str | None, card: dict[str, Any], location_raw: str | None) -> str | None:
    """A real town, never the board's "Remoto" placeholder or a whole-country ad."""
    if card.get("job_district_all"):
        return None
    for candidate in (locality, card.get("job_municipality"), card.get("job_district"), location_raw):
        text = _text(candidate)
        if not text or _PT_REMOTE_RE.search(text) or _is_country_name(text):
            continue
        return _first_town(text)
    return None


def build_job(card: dict[str, Any] | None, detail: dict[str, Any] | None = None) -> Job | None:
    """One search record (plus the posting page, when it was fetched) → a normalized Job."""
    card = card or {}
    detail = detail or {}
    jsonld = detail.get("jsonld") or {}
    meta = detail.get("meta") or {}

    url = _text(card.get("link")) or _text(jsonld.get("url"))
    title = _text(jsonld.get("title")) or _text(card.get("offer_name"))
    if not url or not title:
        return None

    address = _first_dict(_first_dict(jsonld.get("jobLocation")).get("address"))
    location_raw = _text(card.get("location")) or _text(meta.get("location")) or _text(
        address.get("addressLocality")) or _text(address.get("addressRegion"))

    raw_country = _text(address.get("addressCountry")) or _text(card.get("job_country"))
    country = _country(raw_country)
    if country is None and not raw_country:
        country = guess_country(location_raw) or "PT"  # a Portuguese board's default

    description = strip_html(jsonld.get("description")) or strip_html(card.get("job_description"))
    salary = _text(meta.get("salary"))
    industry = _text(jsonld.get("industry")) or _text(meta.get("category"))
    company = (_text(_first_dict(jsonld.get("hiringOrganization")).get("name"))
               or _text(card.get("company_name")) or _text(meta.get("company")))

    return Job(
        source="sapoemprego",
        source_id=_text(card.get("id")) or _text(meta.get("id")) or url,
        url=url,
        title=title,
        company=company,
        description=description,
        location_raw=location_raw,
        country=country,
        city=_city(address.get("addressLocality"), card, location_raw),
        remote=_work_model(meta.get("workhome"), jsonld, title=title, location=location_raw,
                           description=description),
        remote_region=_text(_first_dict(jsonld.get("applicantLocationRequirements")).get("name")),
        employment_type=_employment_type(jsonld.get("employmentType"), meta.get("time"),
                                         card.get("job_work_hours")),
        salary_text=None if salary and _PLACEHOLDER_SALARY_RE.match(salary) else salary,
        tags=[industry] if industry else [],
        posted_at=parse_date(jsonld.get("datePosted") or card.get("publication_date") or meta.get("date")),
        raw={"card": card, "jsonld": jsonld or None, "meta": meta or None},
    )


class SapoEmprego:
    name = "sapoemprego"
    description = "emprego.sapo.pt — SAPO Emprego (Portugal): search JSON + JSON-LD posting pages"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        queries = ctx.opt("queries", DEFAULT_QUERIES) or DEFAULT_QUERIES
        if isinstance(queries, str):
            queries = [queries]
        max_pages = max(1, int(ctx.opt("max_pages", 5)))
        budget = int(ctx.opt("max_details", 150)) if ctx.opt("fetch_details", True) else 0
        spent = 0

        def convert(card: dict[str, Any]) -> Job | None:
            nonlocal spent
            detail = None
            if spent < budget:
                spent += 1
                try:
                    detail = parse_detail_html(ctx.http.get_text(card["link"]))
                except Exception as exc:  # one dead posting page must not cost us the record
                    log.warning("%s: detail page %s failed (%s)", self.name, card["link"], exc)
            return build_job(card, detail)

        for emitted, job in enumerate(safe_records(self._cards(ctx, queries, max_pages), convert, self.name), 1):
            yield job
            if ctx.limit and emitted >= ctx.limit:
                return

    def _search(self, ctx: SourceContext, query: str, page: int, pause: float) -> Any:
        """One search page, waiting out a single 419 (the board's way of saying "slow down")."""
        params = {"pesquisa": query, "pagina": page}
        try:
            return ctx.http.get_json(SEARCH_API, params=params, headers=XHR_HEADERS)
        except SourceHTTPError as exc:
            if exc.status != THROTTLED:
                raise
            log.warning("%s: throttled on %r page %d, waiting %.0fs", self.name, query, page, pause)
            if pause > 0:
                time.sleep(pause)
            return ctx.http.get_json(SEARCH_API, params=params, headers=XHR_HEADERS)

    def _cards(self, ctx: SourceContext, queries: list[str], max_pages: int) -> Iterator[dict[str, Any]]:
        """Every posting the queries return, deduped by URL. The board sorts by its own
        relevance score and offers no date sort, so pages come back in whatever order it likes."""
        seen: set[str] = set()
        delay = float(ctx.opt("search_delay", SEARCH_DELAY))
        pause = float(ctx.opt("throttle_pause", THROTTLE_PAUSE))
        searched = False
        for query in queries:
            for page in range(1, max_pages + 1):
                if searched and delay > 0:
                    time.sleep(delay)
                searched = True
                payload = self._search(ctx, query, page, pause)
                records = parse_search(payload)
                if not records:
                    if page == 1:
                        log.info("%s: no postings for %r", self.name, query)
                    break
                for record in records:
                    url = str(record["link"])
                    if url in seen:
                        continue
                    seen.add(url)
                    yield record
                total = page_total(payload)
                if total is not None and page * PAGE_SIZE >= total:
                    break


register(SapoEmprego())
