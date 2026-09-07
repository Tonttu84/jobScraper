"""jobicy.com — remote job board with a free public API, filterable by geo and industry.

GET https://jobicy.com/api/v2/remote-jobs?count=50&geo=europe&industry=dev
    → {"apiVersion": "2.2.16", "jobCount": N, "lastUpdate": …, "appliedFilters": {…},
       "jobs": [...], "statusCode": 200, "success": true}

One request per geo (default ``europe`` + ``anywhere``), results deduped by ``id``. The API
canonicalizes the industry itself: ``industry=dev`` comes back as ``engineering`` in
``appliedFilters``.

Every posting is remote. ``jobGeo`` says who may apply and goes to ``Job.remote_region``; it is
either one place ("Poland", "EMEA", "Anywhere") or a padded list ("Europe,  USA") — a list of
countries stays ``country=None`` rather than guessing one of them. ``jobLevel`` is a *string*
("Senior", "Entry-Level, Junior"), ``jobType`` and ``jobIndustry`` are lists.

Salary (API v2.2: only present on a minority of records, and the keys are no longer the
``annualSalary*`` ones of v2.0): ``salaryMin``/``salaryMax``/``salaryCurrency`` plus
``salaryPeriod`` — "yearly", "monthly" or "hourly", so the period has to be kept.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from jobscraper.http import strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

API = "https://jobicy.com/api/v2/remote-jobs"
DEFAULT_GEOS = ["europe", "anywhere"]

_ANYWHERE_RE = re.compile(
    r"\b(anywhere|worldwide|world ?wide|global|any location)\b", re.IGNORECASE
)
_REGION_SPLIT_RE = re.compile(r"\s*(?:,|/|;|\||\bor\b|\band\b)\s*", re.IGNORECASE)


def region_country(region: str | None) -> str | None:
    """ISO2 only when the "who can apply" text names exactly one country.

    Every part has to resolve to the same country: "Poland" is PL, but "Europe, USA" is a
    two-region posting, not a US one, and "EMEA" is no country at all.
    """
    if not region or _ANYWHERE_RE.search(region):
        return None
    codes = {guess_country(part) for part in _REGION_SPLIT_RE.split(region) if part.strip()}
    return codes.pop() if len(codes) == 1 and None not in codes else None


_PERIODS = {"yearly": "year", "annual": "year", "monthly": "month", "weekly": "week", "daily": "day", "hourly": "hour"}


def salary_text(low: Any, high: Any, currency: Any, period: Any = None) -> str | None:
    """"5,300 - 6,600 USD/month" from the salaryMin/Max/Currency/Period quartet."""

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
    text = f"{span} {unit}".strip()
    key = str(period).strip().lower() if period else ""
    per = _PERIODS.get(key, key)
    return f"{text}/{per}" if per else text


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]
    return []


def parse_record(rec: dict[str, Any]) -> Job | None:
    if not isinstance(rec, dict):
        return None
    job_id = rec.get("id")
    url = rec.get("url")
    title = rec.get("jobTitle")
    if not title or not (job_id or url):
        return None
    # jobGeo pads its separators ("Europe,  USA"); collapse so the region reads like a list.
    raw_geo = rec.get("jobGeo")
    region = re.sub(r"\s+", " ", raw_geo).strip() if isinstance(raw_geo, str) else None
    region = region or None
    levels = _strings(rec.get("jobLevel"))
    return Job(
        source="jobicy",
        source_id=str(job_id or url),
        url=url or f"https://jobicy.com/jobs/{job_id}",
        title=title,
        company=rec.get("companyName"),
        description=strip_html(rec.get("jobDescription")) or strip_html(rec.get("jobExcerpt")),
        location_raw=region,
        country=region_country(region),
        remote="remote",
        remote_region=region,
        seniority_raw=", ".join(levels) or None,
        employment_type=", ".join(_strings(rec.get("jobType"))) or None,
        tags=_strings(rec.get("jobIndustry")),
        salary_text=salary_text(
            rec.get("salaryMin"),
            rec.get("salaryMax"),
            rec.get("salaryCurrency"),
            rec.get("salaryPeriod"),
        ),
        posted_at=parse_date(rec.get("pubDate")),
        raw=rec,
    )


class Jobicy:
    name = "jobicy"
    description = "jobicy.com remote-jobs API (europe + anywhere feeds)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        geo = ctx.opt("geo")
        geos = [str(g) for g in ([geo] if geo else ctx.opt("geos", DEFAULT_GEOS)) if g]
        count = int(ctx.opt("count", 50))
        seen: set[str] = set()
        emitted = 0
        for geo_value in geos or [""]:
            params: dict[str, Any] = {"count": count}
            if geo_value:
                params["geo"] = geo_value
            industry = ctx.opt("industry", "dev")
            if industry:
                params["industry"] = str(industry)
            tag = ctx.opt("tag")
            if tag:
                params["tag"] = str(tag)
            payload = ctx.http.get_json(API, params=params)
            records = (payload or {}).get("jobs") or []
            for job in safe_records(records, parse_record, self.name):
                if job.source_id in seen:
                    continue
                seen.add(job.source_id)
                yield job
                emitted += 1
                if ctx.limit and emitted >= ctx.limit:
                    return


register(Jobicy())
