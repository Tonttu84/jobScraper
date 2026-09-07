"""justjoin.it — Polish/CEE IT marketplace with a public cursor-paginated API.

List:   GET https://api.justjoin.it/v2/user-panel/offers/by-cursor
        ?experienceLevels[]=junior&workplaceTypes[]=remote  (filters are optional)
        → ``{"data": [...], "meta": {"next": {"cursor": N}}}``
        The next page is ``?from=<cursor>`` (``?cursor=`` is silently ignored).
Detail: GET https://api.justjoin.it/v1/offers/{slug} → ``{"body": "<html>", ...}``

The list response has no apply link, so the canonical URL is built from the slug.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from jobscraper.http import SourceHTTPError, strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

LIST_API = "https://api.justjoin.it/v2/user-panel/offers/by-cursor"
DETAIL_API = "https://api.justjoin.it/v1/offers/{slug}"
JOB_URL = "https://justjoin.it/job-offer/{slug}"
DEFAULT_COUNTRY = "PL"

WORKPLACE_REMOTE = {
    "remote": "remote",
    "fully_remote": "remote",
    "hybrid": "hybrid",
    "partly_remote": "hybrid",
    "office": "onsite",
    "on_site": "onsite",
    "stationary": "onsite",
}


def _names(items: Any) -> list[str]:
    if not isinstance(items, (list, tuple, set)):
        return []
    out: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            name = item.get("name") or item.get("value")
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
    return out


def _amount(value: Any) -> str | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return f"{int(value):,}"
    return str(value).strip() or None


def salary_text(employment_types: Any) -> str | None:
    """``[{"from": 8000, "to": 12000, "currency": "pln", "type": "b2b"}]`` → readable text."""
    parts: list[str] = []
    for entry in employment_types or []:
        if not isinstance(entry, dict):
            continue
        low, high = _amount(entry.get("from")), _amount(entry.get("to"))
        if not low and not high:
            continue
        span = f"{low} - {high}" if low and high and low != high else (low or high)
        currency = entry.get("currency")
        if isinstance(currency, str) and currency.strip():
            span = f"{span} {currency.strip().upper()}"
        kind = entry.get("type")
        if isinstance(kind, str) and kind.strip():
            span = f"{span} ({kind.strip()})"
        parts.append(span)
    return "; ".join(dict.fromkeys(parts)) or None


def parse_offer(rec: dict[str, Any]) -> Job | None:
    if not isinstance(rec, dict):
        return None
    slug = rec.get("slug")
    title = rec.get("title")
    if not slug or not title:
        return None
    city = rec.get("city") if isinstance(rec.get("city"), str) else None
    code = str(rec.get("countryCode") or "").strip()
    country = code.upper() if len(code) == 2 and code.isalpha() else None
    country = country or guess_country(city) or DEFAULT_COUNTRY
    workplace = str(rec.get("workplaceType") or "").strip().lower()
    remote = WORKPLACE_REMOTE.get(workplace) or guess_remote(city, str(title))
    employment_types = rec.get("employmentTypes") or []
    kinds = [k for k in (e.get("type") for e in employment_types if isinstance(e, dict)) if isinstance(k, str)]
    return Job(
        source="justjoin",
        source_id=str(rec.get("guid") or slug),
        url=JOB_URL.format(slug=slug),
        title=str(title),
        company=rec.get("companyName") or None,
        location_raw=", ".join(p for p in (city, country) if p) or None,
        country=country,
        city=city,
        remote=remote,
        seniority_raw=rec.get("experienceLevel") or None,
        employment_type=", ".join(dict.fromkeys(kinds)) or None,
        salary_text=salary_text(employment_types),
        tags=_names(rec.get("requiredSkills")),
        posted_at=parse_date(rec.get("publishedAt")),
        raw=rec,
    )


def hydrate(ctx: SourceContext, job: Job, slug: str) -> None:
    """Add the posting body the list endpoint omits; a failed detail never drops a job."""
    try:
        detail = ctx.http.get_json(DETAIL_API.format(slug=slug))
    except Exception as exc:
        log.warning("justjoin: detail %s failed: %s", slug, exc)
        return
    if not isinstance(detail, dict):
        return
    job.description = strip_html(detail.get("body"))
    skills = _names(detail.get("requiredSkills"))
    if skills:
        job.tags = list(dict.fromkeys([*job.tags, *skills]))
    level = detail.get("experienceLevel")
    if isinstance(level, dict) and isinstance(level.get("value"), str):
        job.seniority_raw = level["value"]
    job.raw = {**job.raw, "detail": detail}


class JustJoin:
    name = "justjoin"
    description = "justjoin.it public API (Poland/CEE IT marketplace)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        levels = [str(v).strip() for v in (ctx.opt("experience_levels", ["junior"]) or []) if str(v).strip()]
        workplaces = [str(v).strip() for v in (ctx.opt("workplace_types", []) or []) if str(v).strip()]
        max_pages = int(ctx.opt("max_pages", 10))
        fetch_details = bool(ctx.opt("fetch_details", True))
        max_details = int(ctx.opt("max_details", 200))

        base_params: dict[str, Any] = {}
        if levels:
            base_params["experienceLevels[]"] = levels
        if workplaces:
            base_params["workplaceTypes[]"] = workplaces

        cursor = 0
        yielded = 0
        details = 0
        seen: set[str] = set()
        for _page in range(max_pages):
            params = dict(base_params)
            if cursor:
                params["from"] = cursor
            payload = ctx.http.get_json(LIST_API, params=params)
            if not isinstance(payload, dict):
                raise SourceHTTPError(f"justjoin: expected an object, got {type(payload).__name__}")
            data = payload.get("data") or []
            if not data:
                break
            for job in safe_records(data, parse_offer, self.name):
                if job.source_id in seen:
                    continue
                seen.add(job.source_id)
                if fetch_details and details < max_details:
                    details += 1
                    hydrate(ctx, job, str(job.raw.get("slug")))
                yield job
                yielded += 1
                if ctx.limit and yielded >= ctx.limit:
                    return
            meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            nxt = meta.get("next") if isinstance(meta.get("next"), dict) else None
            next_cursor = nxt.get("cursor") if nxt else None
            # A non-increasing cursor means the page param was ignored — stop instead of looping.
            if not isinstance(next_cursor, int) or next_cursor <= cursor:
                break
            cursor = next_cursor


register(JustJoin())
