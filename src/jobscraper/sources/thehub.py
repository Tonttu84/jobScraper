"""thehub.io — Nordic startup/tech job platform (DK/SE/NO/FI), direct postings, no auth.

    GET https://thehub.io/api/v2/jobs?page=N&countryCode=FI&search=developer
        -> {"docs": [...], "pages": N, "total": N, ...}   (15 docs per page, ?limit is ignored)
    GET https://thehub.io/api/jobs/{id}
        -> {"doc": {... full posting incl. description ...}}

The list envelope usually carries everything we need; when a doc has no ``description`` we
hydrate it from the detail endpoint. Non-``ACTIVE`` docs (DRAFT/EXPIRED show up depending on
cache state) are dropped. ``link`` is the employer's apply URL (often an ATS handoff) — we keep
``https://thehub.io/jobs/{id}`` as the canonical URL and stash the apply URL in ``raw``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from jobscraper.http import strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

LIST_URL = "https://thehub.io/api/v2/jobs"
DETAIL_URL = "https://thehub.io/api/jobs/{id}"
JOB_URL = "https://thehub.io/jobs/{id}"
PER_PAGE = 15  # hard-coded by the API

DEFAULT_COUNTRIES = ["FI", "SE", "NO", "DK"]
DEFAULT_SEARCH = "developer"


def _doc_id(rec: dict[str, Any]) -> str | None:
    for key in ("id", "_id"):
        value = rec.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int):
            return str(value)
    return None


def _format_location(rec: dict[str, Any]) -> str | None:
    """``location`` ships as {address, locality, country}; ``geoLocation`` sometimes repeats it."""
    for key in ("location", "geoLocation"):
        loc = rec.get(key)
        if isinstance(loc, str) and loc.strip():
            return loc.strip()
        if not isinstance(loc, dict):
            continue
        address = loc.get("address")
        if isinstance(address, str) and address.strip():
            return address.strip()
        parts = [
            loc[k].strip()
            for k in ("locality", "city", "region", "country")
            if isinstance(loc.get(k), str) and loc[k].strip()
        ]
        if parts:
            return ", ".join(parts)
    return None


def _salary_text(rec: dict[str, Any]) -> str | None:
    salary = rec.get("salary")
    if isinstance(salary, str) and salary.strip():
        return salary.strip()
    for key in ("salaryRange", "salary"):
        rng = rec.get(key)
        if not isinstance(rng, dict):
            continue
        low = rng.get("from") or rng.get("min") or rng.get("low")
        high = rng.get("to") or rng.get("max") or rng.get("high")
        currency = rng.get("currency") if isinstance(rng.get("currency"), str) else ""
        bounds = [str(v) for v in (low, high) if isinstance(v, (int, float)) and v > 0]
        if bounds:
            return " - ".join(bounds) + (f" {currency}" if currency else "")
    return None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]
    return []


def parse_record(rec: dict[str, Any], country_hint: str | None = None) -> Job | None:
    job_id = _doc_id(rec)
    title = rec.get("title")
    if not job_id or not isinstance(title, str) or not title.strip():
        return None
    status = rec.get("status")
    if isinstance(status, str) and status.strip() and status.strip().upper() != "ACTIVE":
        return None

    company_obj = rec.get("company") if isinstance(rec.get("company"), dict) else {}
    company = (company_obj or {}).get("name")
    if not isinstance(company, str) or not company.strip():
        company = rec.get("companyName") if isinstance(rec.get("companyName"), str) else None

    location_raw = _format_location(rec)
    country_code = rec.get("countryCode")
    country = country_code if isinstance(country_code, str) and country_code.strip() else None
    country = country or guess_country(location_raw) or country_hint

    is_remote = rec.get("isRemote")
    apply_url = rec.get("link")
    raw = dict(rec)
    if isinstance(apply_url, str) and apply_url.startswith(("http://", "https://")):
        raw["apply_url"] = apply_url.strip()

    return Job(
        source="thehub",
        source_id=job_id,
        url=JOB_URL.format(id=job_id),
        title=title.strip(),
        company=company.strip() if isinstance(company, str) and company.strip() else None,
        description=strip_html(rec.get("description")),
        location_raw=location_raw,
        country=country,
        city=location_raw.split(",")[0].strip() if location_raw else None,
        remote=guess_remote(
            location_raw, title, flag=is_remote if isinstance(is_remote, bool) else None
        ),
        employment_type=", ".join(_strings(rec.get("jobPositionTypes"))) or None,
        salary_text=_salary_text(rec),
        tags=_strings(rec.get("jobRoles")) + _strings(rec.get("tags")),
        posted_at=parse_date(
            rec.get("publishedAt") or rec.get("approvedAt") or rec.get("createdAt")
        ),
        raw=raw,
    )


class TheHub:
    name = "thehub"
    description = "thehub.io — Nordic startup jobs (FI/SE/NO/DK), public REST API"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        countries = ctx.opt("countries", DEFAULT_COUNTRIES) or DEFAULT_COUNTRIES
        if isinstance(countries, str):
            countries = [countries]
        search = ctx.opt("search", DEFAULT_SEARCH)
        max_pages = int(ctx.opt("max_pages", 10))
        seen: set[str] = set()
        emitted = 0

        for country in countries:
            for page in range(1, max_pages + 1):
                params: dict[str, Any] = {"page": page, "countryCode": str(country)}
                if search:
                    params["search"] = str(search)
                payload = ctx.http.get_json(LIST_URL, params=params)
                docs = payload.get("docs") if isinstance(payload, dict) else None
                if not isinstance(docs, list) or not docs:
                    break

                def convert(rec: dict[str, Any], _c: str = str(country)) -> Job | None:
                    return parse_record(rec, country_hint=_c)

                for job in safe_records(docs, convert, self.name):
                    if job.source_id in seen:
                        continue
                    seen.add(job.source_id)
                    if not job.description:
                        self._hydrate(ctx, job)
                    yield job
                    emitted += 1
                    if ctx.limit and emitted >= ctx.limit:
                        return
                pages = payload.get("pages") if isinstance(payload, dict) else None
                if isinstance(pages, int) and page >= pages:
                    break
                if len(docs) < PER_PAGE:
                    break

    def _hydrate(self, ctx: SourceContext, job: Job) -> None:
        """Fill in the description from the detail endpoint; a failure is logged, never raised."""
        try:
            payload = ctx.http.get_json(DETAIL_URL.format(id=job.source_id))
        except Exception as exc:
            log.warning("%s: detail %s failed (%s)", self.name, job.source_id, exc)
            return
        doc = payload.get("doc") if isinstance(payload, dict) else None
        if not isinstance(doc, dict):
            doc = payload if isinstance(payload, dict) else None
        if not isinstance(doc, dict):
            return
        job.description = strip_html(doc.get("description"))
        if not job.company:
            company = (doc.get("company") or {}).get("name") if isinstance(doc.get("company"), dict) else None
            job.company = company.strip() if isinstance(company, str) and company.strip() else None
        if not job.salary_text:
            job.salary_text = _salary_text(doc)
        job.raw = {**job.raw, "detail": doc}


register(TheHub())
