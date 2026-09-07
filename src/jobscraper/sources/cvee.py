"""cv.ee — one of Estonia's two big private boards; keyless Elasticsearch-backed search API.

    GET https://cv.ee/api/v1/vacancy-search-service/search
        ?categories[]=INFORMATION_TECHNOLOGY&limit=250&offset=0&sorting=LATEST
    -> {"total": N, "vacancies": [{id, positionTitle, positionContent, employerName, townId,
        countyId, countryId, salaryFrom, salaryTo, remoteWork, remoteWorkType, publishDate,
        keywords, ...}], <facet counts>, ...}

Four things are worth knowing, all measured against the live API on 2026-09-07:

* Only the bracketed ``categories[]`` filters. ``categories=INFORMATION_TECHNOLOGY`` returns
  the whole board (1332 vacancies) while ``categories[]=INFORMATION_TECHNOLOGY`` returns 175.
* ``keywords`` does nothing at all. Within the IT category, ``keywords=developer`` and
  ``keywords=kokk`` ("cook") both return exactly the same 175 ids as no keyword; without a
  category it likewise returns the whole board. So we make **one request per category**, and
  the ``keywords`` option (``config/sources.yaml`` still sets it) is accepted and ignored.
* ``positionContent`` in the search response is the complete ad body, already flattened to
  plain text — including the benefits blurb. It is present for ~60% of the rows; the other
  ~40% are picture ads (``details.fileDetails``: a PDF or JPEG) that carry no text anywhere.
  This is where the description comes from.
* Locations are numeric ids. ``GET /api/v1/locations-service/list`` returns
  ``{"towns": [{id, countyId, countryId, name}], "counties": {id: {...}}, "countries": {id: {iso, name}}}``;
  it is fetched once per run and gives ``city``, ``country`` and ``location_raw``. Rows with a
  null ``townId`` (21 of 175) still have a ``countyId``, so they get a county-level location.

There is no JSON detail endpoint (``/vacancy-service/vacancy/{id}`` and friends all 404), but
the public page ``https://cv.ee/en/vacancy/{id}`` is Next.js SSR and embeds
``<script id="__NEXT_DATA__">`` whose ``props.pageProps.vacancy["<id>"].details.standardDetails``
holds the ad body as titled HTML sections. Comparing that against the search response for the
same vacancies, the *only* thing it adds is the section headings ("Tööülesanded", "Mida me
pakume?") — the words are otherwise identical — and the page is ~700 KB. That is a poor trade
for a whole run, so ``fetch_details`` defaults to **false**; turn it on in
``config/sources.yaml`` if the ranking stage ever wants the structure, and ``max_details``
(default 150) caps the damage. A detail fetch that fails or comes back empty only costs the
headings: the job is still emitted with the list body.

Recruitment agencies are said to post under the employer name "Vahendatud pakkumised"
("mediated offers"); such rows carry no real employer and are dropped. None were on the board
when this was written (0 of 1332), so the rule is a cheap guard rather than a hot path.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from typing import Any

from jobscraper.http import strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

API = "https://cv.ee/api/v1/vacancy-search-service/search"
LOCATIONS_API = "https://www.cv.ee/api/v1/locations-service/list"
JOB_URL = "https://cv.ee/en/vacancy/{id}"
PAGE_LIMIT = 250
MAX_DETAILS = 150

DEFAULT_CATEGORIES = ["INFORMATION_TECHNOLOGY"]
EE_COUNTRY_ID = 1  # cv.ee's own id for Estonia

_MEDIATED_RE = re.compile(r"vahendatud\s+pakkumised", re.IGNORECASE)
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
_REMOTE_TYPES = {"FULLY_REMOTE": "remote", "REMOTE": "remote", "HYBRID": "hybrid", "ON_SITE": "onsite"}


class Locations:
    """townId/countyId/countryId → names, from ``/api/v1/locations-service/list``.

    Every lookup misses when the endpoint is unavailable, so a broken locations service costs
    us the city but never a job.
    """

    def __init__(self, payload: Any = None) -> None:
        self.towns: dict[int, str] = {}
        self.counties: dict[int, str] = {}
        self.countries: dict[int, tuple[str | None, str | None]] = {}
        if not isinstance(payload, dict):
            return
        self.towns = _names(payload.get("towns"))
        self.counties = _names(payload.get("counties"))
        for ident, entry in _entries(payload.get("countries")):
            iso = entry.get("iso")
            name = entry.get("name")
            self.countries[ident] = (
                iso.strip() if isinstance(iso, str) else None,
                name.strip() if isinstance(name, str) else None,
            )

    def resolve(
        self, town_id: Any, county_id: Any, country_id: Any
    ) -> tuple[str | None, str | None, str | None]:
        """→ (city, ISO2 country, human-readable location string)."""
        town = self.towns.get(town_id) if isinstance(town_id, int) else None
        county = self.counties.get(county_id) if isinstance(county_id, int) else None
        iso, country_name = self.countries.get(country_id, (None, None)) if isinstance(country_id, int) else (None, None)
        if iso is None and country_id in (None, EE_COUNTRY_ID):
            iso = "EE"  # the board is Estonian: id 1 is Estonia, and a missing id means Estonia too
        parts = [p for p in (town, county, country_name) if p]
        return town, iso, ", ".join(parts) or None


def _entries(value: Any) -> Iterable[tuple[int, dict[str, Any]]]:
    """The payload uses a list for towns and id→object maps for counties/countries."""
    items = value if isinstance(value, list) else list(value.values()) if isinstance(value, dict) else []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            yield item["id"], item


def _names(value: Any) -> dict[int, str]:
    return {
        ident: entry["name"].strip()
        for ident, entry in _entries(value)
        if isinstance(entry.get("name"), str) and entry["name"].strip()
    }


def _salary_text(rec: dict[str, Any]) -> str | None:
    low, high = rec.get("salaryFrom"), rec.get("salaryTo")
    bounds = [f"{float(v):.0f}" for v in (low, high) if isinstance(v, (int, float)) and v > 0]
    if not bounds:
        return None
    unit = "EUR/h" if rec.get("hourlySalary") else "EUR"
    return f"{' - '.join(bounds)} {unit}"


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def parse_detail(html: str, vacancy_id: Any) -> str | None:
    """The ad body from a vacancy page's ``__NEXT_DATA__`` blob, section headings included.

    Returns ``None`` for anything unexpected — a maintenance page, a markup change, a picture
    ad with no text — and the caller falls back to the search response's own body.
    """
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        return None
    try:
        vacancy = json.loads(match.group(1))["props"]["pageProps"]["vacancy"][str(vacancy_id)]
        sections = (vacancy.get("details") or {}).get("standardDetails") or []
        benefits = _text((vacancy.get("highlights") or {}).get("additionalBenefits"))
    except (AttributeError, KeyError, TypeError, ValueError):
        return None

    chunks: list[str] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        body = _text(strip_html(section.get("content")))
        if not body:
            continue
        title = _text(section.get("title"))
        chunks.append(f"{title}\n{body}" if title else body)
    if benefits:
        chunks.append(benefits)
    return "\n\n".join(chunks) or None


def parse_record(
    rec: dict[str, Any],
    locations: Locations | None = None,
    description: str | None = None,
) -> Job | None:
    """One search-result row → a Job. ``description`` overrides the row's own body."""
    vacancy_id = rec.get("id")
    title = _text(rec.get("positionTitle"))
    if not vacancy_id or not title:
        return None
    employer = _text(rec.get("employerName"))
    if employer and _MEDIATED_RE.search(employer):
        return None

    places = locations or Locations()
    city, country, location_raw = places.resolve(
        rec.get("townId"), rec.get("countyId"), rec.get("countryId")
    )

    remote_type = rec.get("remoteWorkType")
    remote = _REMOTE_TYPES.get(str(remote_type).upper()) if remote_type else None
    if not remote:
        flag = rec.get("remoteWork")
        remote = guess_remote(title, flag=flag if isinstance(flag, bool) else None)

    return Job(
        source="cvee",
        source_id=str(vacancy_id),
        url=JOB_URL.format(id=vacancy_id),
        title=title,
        company=employer,
        # Already plain text in the search response; the detail page only adds headings.
        description=description or _text(rec.get("positionContent")),
        location_raw=location_raw,
        country=country,
        city=city,
        remote=remote,
        salary_text=_salary_text(rec),
        tags=[k for k in (rec.get("keywords") or []) if isinstance(k, str) and k.strip()],
        posted_at=parse_date(rec.get("publishDate") or rec.get("renewedDate")),
        raw=rec,
    )


class CvEe:
    name = "cvee"
    description = "cv.ee vacancy search API (Estonia, IT category)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        categories = ctx.opt("categories", DEFAULT_CATEGORIES) or DEFAULT_CATEGORIES
        if isinstance(categories, str):
            categories = [categories]
        page_size = int(ctx.opt("page_size", PAGE_LIMIT))
        max_pages = int(ctx.opt("max_pages", 5))
        details_left = int(ctx.opt("max_details", MAX_DETAILS)) if ctx.opt("fetch_details", False) else 0
        if ctx.opt("keywords"):
            log.debug("cvee: the search API ignores `keywords`; fetching whole categories instead")

        locations = self._locations(ctx)

        def convert(rec: dict[str, Any]) -> Job | None:
            nonlocal details_left
            job = parse_record(rec, locations)
            if job is None or details_left <= 0:
                return job  # don't spend a detail fetch on a row we are dropping anyway
            details_left -= 1
            detail = self._detail(ctx, rec["id"])
            return parse_record(rec, locations, detail) if detail else job

        seen: set[str] = set()
        emitted = 0
        for category in categories:
            offset = 0
            for _page in range(max_pages):
                payload = ctx.http.get_json(
                    API,
                    params={
                        "categories[]": [str(category)],
                        "limit": page_size,
                        "offset": offset,
                        "sorting": "LATEST",  # newest first, so `max_details` is spent on fresh ads
                    },
                )
                vacancies = payload.get("vacancies") if isinstance(payload, dict) else None
                if not isinstance(vacancies, list) or not vacancies:
                    break
                for job in safe_records(vacancies, convert, self.name):
                    if job.source_id in seen:
                        continue
                    seen.add(job.source_id)
                    yield job
                    emitted += 1
                    if ctx.limit and emitted >= ctx.limit:
                        return
                offset += len(vacancies)
                total = payload.get("total")
                if isinstance(total, int) and offset >= total:
                    break
                if len(vacancies) < page_size:
                    break

    # ---------------------------------------------------------------- helpers
    def _locations(self, ctx: SourceContext) -> Locations:
        """Fetched once per run; a failure costs the city, not the run."""
        try:
            return Locations(ctx.http.get_json(LOCATIONS_API))
        except Exception as exc:  # auxiliary endpoint: never fatal
            log.warning("cvee: locations list unavailable (%s); cities will be missing", exc)
            return Locations()

    def _detail(self, ctx: SourceContext, vacancy_id: Any) -> str | None:
        try:
            html = ctx.http.get_text(JOB_URL.format(id=vacancy_id))
        except Exception as exc:  # the search row is still a usable job
            log.warning("cvee: detail page for %s failed (%s)", vacancy_id, exc)
            return None
        body = parse_detail(html, vacancy_id)
        if body is None:
            log.debug("cvee: no readable __NEXT_DATA__ body for %s", vacancy_id)
        return body


register(CvEe())
