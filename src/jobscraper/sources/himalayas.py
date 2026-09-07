"""himalayas.app — remote-only board with a free JSON API (worldwide, tech-heavy).

GET https://himalayas.app/jobs/api?limit=50&offset=0 → {"jobs": [...], "totalCount": N}
Paginated by ``offset``; ``totalCount`` bounds the walk.

Every posting is remote. ``locationRestrictions`` (and ``timezoneRestrictions`` as a fallback)
say who may apply and go to ``Job.remote_region`` for the location filter.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from jobscraper.http import strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

API = "https://himalayas.app/jobs/api"

_ANYWHERE_RE = re.compile(
    r"\b(anywhere|worldwide|world ?wide|global|any location)\b", re.IGNORECASE
)
_REGION_SPLIT_RE = re.compile(r"\s*(?:,|/|;|\||\bor\b|\band\b)\s*", re.IGNORECASE)


def region_country(region: str | None) -> str | None:
    """ISO2 only when the "who can apply" text names exactly one country (see remotive)."""
    if not region or _ANYWHERE_RE.search(region):
        return None
    codes = {guess_country(part) for part in _REGION_SPLIT_RE.split(region) if part.strip()}
    codes.discard(None)
    return codes.pop() if len(codes) == 1 else None


def salary_text(low: Any, high: Any, currency: Any) -> str | None:
    def num(value: Any) -> str | None:
        try:
            amount = float(value)
        except (TypeError, ValueError):
            return None
        return f"{int(amount):,}" if amount > 0 else None

    low_s, high_s = num(low), num(high)
    if not low_s and not high_s:
        return None
    span = f"{low_s} - {high_s}" if low_s and high_s else (low_s or high_s)
    unit = str(currency).strip().upper() if currency else ""
    return f"{span} {unit}".strip()


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]
    return []


def parse_record(rec: dict[str, Any]) -> Job | None:
    if not isinstance(rec, dict):
        return None
    title = rec.get("title")
    url = rec.get("applicationLink") or rec.get("guid")
    if not title or not url:
        return None
    locations = _strings(rec.get("locationRestrictions"))
    timezones = _strings(rec.get("timezoneRestrictions"))
    region = ", ".join(locations) or ", ".join(timezones) or None
    return Job(
        source="himalayas",
        source_id=str(rec.get("guid") or url),
        url=str(url),
        title=title,
        company=rec.get("companyName"),
        description=strip_html(rec.get("description")) or strip_html(rec.get("excerpt")),
        location_raw=region,
        country=region_country(", ".join(locations) or None),
        remote="remote",
        remote_region=region,
        seniority_raw=", ".join(_strings(rec.get("seniority"))) or None,
        employment_type=rec.get("employmentType") or None,
        tags=_strings(rec.get("categories")),
        salary_text=salary_text(rec.get("minSalary"), rec.get("maxSalary"), rec.get("currency")),
        posted_at=parse_date(rec.get("pubDate")),
        raw=rec,
    )


class Himalayas:
    name = "himalayas"
    description = "himalayas.app remote-jobs API (worldwide)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        max_pages = int(ctx.opt("max_pages", 5))
        page_size = int(ctx.opt("page_size", 50))
        emitted = 0
        offset = 0
        for _ in range(max(max_pages, 1)):
            payload = ctx.http.get_json(API, params={"limit": page_size, "offset": offset})
            payload = payload or {}
            records = payload.get("jobs") or []
            if not records:
                break
            for job in safe_records(records, parse_record, self.name):
                yield job
                emitted += 1
                if ctx.limit and emitted >= ctx.limit:
                    return
            offset += len(records)
            try:
                total = int(payload.get("totalCount") or 0)
            except (TypeError, ValueError):
                total = 0
            if total and offset >= total:
                break


register(Himalayas())
