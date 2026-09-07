"""thehub.io — Nordic startup/tech job platform (DK/SE/NO/FI), direct postings, no auth.

    GET https://thehub.io/api/v2/jobs?page=N&countryCode=FI&search=developer
        -> {"docs": [...], "pages": N, "total": N, "suggestions": {...}}  (15 docs/page, ?limit ignored)
    GET https://thehub.io/api/jobs/{id}
        -> {"doc": {... full posting ...}}

The list envelope is only a teaser: each doc carries company, id, isFeatured, isRemote,
jobPositionTypes, key, location, saved, title and views — no dates, no country code, no
description, no status. Everything else the pipeline needs comes from the detail endpoint,
which is therefore called once per job (publishedAt/approvedAt/createdAt, countryCode,
description, jobRoles, link, status). A detail failure is logged and the teaser is emitted
as-is; a detail whose ``status`` is not ``ACTIVE`` drops the job.

``jobRoles`` and ``jobPositionTypes`` arrive as Mongo ObjectIds with no lookup table in either
payload (the list's ``suggestions`` block uses different, slug-style keys), so opaque ids are
filtered out rather than surfaced as tags. ``link`` is the employer's apply URL (often an ATS
handoff) — we keep ``https://thehub.io/jobs/{id}`` as the canonical URL and stash the apply
URL in ``raw``.
"""

from __future__ import annotations

import logging
import re
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

_OBJECT_ID_RE = re.compile(r"[0-9a-f]{24}")


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


def _labels(value: Any) -> list[str]:
    """Like ``_strings`` but drops ObjectIds — thehub sends taxonomy ids, not names."""
    return [s for s in _strings(value) if not _OBJECT_ID_RE.fullmatch(s.lower())]


def _is_active(rec: dict[str, Any]) -> bool:
    """Only the detail document has a ``status``; a missing one is not a reason to drop."""
    status = rec.get("status")
    return not (isinstance(status, str) and status.strip() and status.strip().upper() != "ACTIVE")


def parse_record(rec: dict[str, Any], country_hint: str | None = None) -> Job | None:
    job_id = _doc_id(rec)
    title = rec.get("title")
    if not job_id or not isinstance(title, str) or not title.strip():
        return None
    if not _is_active(rec):
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
        employment_type=", ".join(_labels(rec.get("jobPositionTypes"))) or None,
        salary_text=_salary_text(rec),
        tags=_labels(rec.get("jobRoles")) + _labels(rec.get("tags")),
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
                    if not self._hydrate(ctx, job):
                        continue
                    yield job
                    emitted += 1
                    if ctx.limit and emitted >= ctx.limit:
                        return
                pages = payload.get("pages") if isinstance(payload, dict) else None
                if isinstance(pages, int) and page >= pages:
                    break
                if len(docs) < PER_PAGE:
                    break

    def _hydrate(self, ctx: SourceContext, job: Job) -> bool:
        """Complete a list teaser from ``/api/jobs/{id}``.

        Returns ``False`` when the detail says the posting is no longer ACTIVE, so the caller
        drops it. A broken detail endpoint is logged and the teaser is kept as-is.
        """
        try:
            payload = ctx.http.get_json(DETAIL_URL.format(id=job.source_id))
        except Exception as exc:
            log.warning("%s: detail %s failed (%s)", self.name, job.source_id, exc)
            return True
        doc = payload.get("doc") if isinstance(payload, dict) else None
        if not isinstance(doc, dict):
            doc = payload if isinstance(payload, dict) else None
        if not isinstance(doc, dict):
            return True
        if not _is_active(doc):
            log.debug("%s: %s is %s, skipping", self.name, job.source_id, doc.get("status"))
            return False

        # The detail document is a superset of the list doc, so normalize it the same way and
        # let every non-empty field win over the teaser.
        detail = parse_record(doc, country_hint=job.country)
        if detail is None:
            job.raw = {**job.raw, "detail": doc}
            return True
        for field in (
            "description", "company", "location_raw", "country", "city",
            "employment_type", "salary_text", "posted_at",
        ):
            value = getattr(detail, field)
            if value:
                setattr(job, field, value)
        if detail.tags:
            job.tags = detail.tags
        if detail.remote != "unknown":
            job.remote = detail.remote
        job.raw = {**job.raw, "detail": doc}
        if "apply_url" in detail.raw:
            job.raw["apply_url"] = detail.raw["apply_url"]
        return True


register(TheHub())
