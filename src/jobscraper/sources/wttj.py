"""Welcome to the Jungle — the public Algolia index behind welcometothejungle.com/jobs.

Step 1: GET https://www.welcometothejungle.com/api/env → PUBLIC_ALGOLIA_APPLICATION_ID +
        PUBLIC_ALGOLIA_API_KEY_CLIENT (they rotate; the constants below are the fallback).
Step 2: POST https://{APP}-dsn.algolia.net/1/indexes/wttj_jobs_production_en/query
        headers x-algolia-application-id / x-algolia-api-key (+ a WTTJ Referer — the client
        key is referer-locked), body {"query", "hitsPerPage", "page", "filters"}
        → ``{"hits": [...], "nbPages": N}``

The board is global, so ``countries``/``include_remote`` narrow it server-side with an Algolia
filter expression; anything past the per-query hit cap is invisible otherwise.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from jobscraper.http import SourceHTTPError, strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

ENV_URL = "https://www.welcometothejungle.com/api/env"
SITE = "https://www.welcometothejungle.com"
INDEX = "wttj_jobs_production_en"
QUERY_URL = "https://{app}-dsn.algolia.net/1/indexes/{index}/query"
JOB_URL = SITE + "/en/companies/{org}/jobs/{slug}"
HITS_PER_PAGE = 100

# Public in-page credentials; used when /api/env can't be read.
APP_ID = "CSEKHVMS53"
API_KEY = "4bd8f6215d0cc52b26430765769e65a0"

REMOTE_KINDS = {
    "fulltime": "remote",
    "full": "remote",
    "full_remote": "remote",
    "partial": "hybrid",
    "punctual": "hybrid",
    "occasional": "hybrid",
    "no": "onsite",
    "none": "onsite",
}


def credentials(ctx: SourceContext) -> tuple[str, str]:
    """Fresh Algolia app id + client key, falling back to the in-page constants."""
    try:
        text = ctx.http.get_text(ENV_URL)
        start, end = text.find("{"), text.rfind("}")
        env = json.loads(text[start : end + 1]) if start != -1 and end > start else {}
        app = str(env.get("PUBLIC_ALGOLIA_APPLICATION_ID") or "").strip()
        key = str(env.get("PUBLIC_ALGOLIA_API_KEY_CLIENT") or "").strip()
        if app.isalnum() and 6 <= len(app) <= 16 and 16 <= len(key) <= 500:
            return app, key
        log.warning("wttj: /api/env gave an unexpected credential shape, using the built-in keys")
    except Exception as exc:  # noqa: BLE001
        log.warning("wttj: /api/env failed (%s), using the built-in keys", exc)
    return APP_ID, API_KEY


def build_filters(countries: list[str], include_remote: bool) -> str | None:
    """``["FI", "EE"]`` → ``(offices.country_code:FI OR offices.country_code:EE) OR remote:fulltime``."""
    clauses = [f"offices.country_code:{c.strip().upper()}" for c in countries if str(c).strip()]
    joined = " OR ".join(clauses)
    if joined and len(clauses) > 1:
        joined = f"({joined})"
    if include_remote:
        joined = f"{joined} OR remote:fulltime" if joined else "remote:fulltime"
    return joined or None


def _amount(value: Any) -> str | None:
    if isinstance(value, bool) or value in (None, "", 0):
        return None
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return str(value).strip() or None


def salary_text(hit: dict[str, Any]) -> str | None:
    low = _amount(hit.get("salary_minimum")) or _amount(hit.get("salary_yearly_minimum"))
    high = _amount(hit.get("salary_maximum"))
    if not low and not high:
        return None
    span = f"{low} - {high}" if low and high and low != high else (low or high)
    currency = hit.get("salary_currency")
    if isinstance(currency, str) and currency.strip():
        span = f"{span} {currency.strip().upper()}"
    period = hit.get("salary_period")
    if isinstance(period, str) and period.strip():
        span = f"{span}/{period.strip()}"
    return span


def _paragraphs(value: Any) -> list[str]:
    """Flatten WTTJ's description shapes (str / list / {'description'|'content'|'text': ...})."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        out: list[str] = []
        for key in ("name", "title"):
            if isinstance(value.get(key), str) and value[key].strip():
                out.append(value[key].strip())
                break
        for key in ("description", "content", "text", "body"):
            out.extend(_paragraphs(value.get(key)))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_paragraphs(item))
        return out
    return []


def build_description(hit: dict[str, Any]) -> str | None:
    parts: list[str] = []
    for key in ("summary", "description", "profile", "experience", "sections"):
        parts.extend(_paragraphs(hit.get(key)))
    missions = _paragraphs(hit.get("key_missions"))
    if missions:
        parts.append("\n".join(f"- {m}" for m in missions))
    text = "\n\n".join(dict.fromkeys(p for p in parts if p))
    return strip_html(text)


def parse_hit(hit: dict[str, Any]) -> Job | None:
    if not isinstance(hit, dict):
        return None
    title = hit.get("name")
    slug = hit.get("slug") or hit.get("objectID")
    org = hit.get("organization") if isinstance(hit.get("organization"), dict) else {}
    org_slug = org.get("slug") or org.get("reference")
    if not title or not slug or not org_slug:
        return None
    offices = [o for o in (hit.get("offices") or []) if isinstance(o, dict)]
    office = offices[0] if offices else {}
    city = office.get("city") if isinstance(office.get("city"), str) else None
    code = str(office.get("country_code") or "").strip()
    country = code.upper() if len(code) == 2 and code.isalpha() else guess_country(office.get("country"), city)
    remote_raw = str(hit.get("remote") or "").strip().lower()
    location_raw = ", ".join(
        p for p in (city, office.get("country") if isinstance(office.get("country"), str) else None) if p
    ) or None
    experience = hit.get("experience_level_minimum")
    seniority = f"{int(experience)}+ years" if isinstance(experience, (int, float)) else None
    tags = [t for t in _paragraphs(hit.get("sectors")) if t]
    return Job(
        source="wttj",
        source_id=str(hit.get("objectID") or hit.get("reference") or slug),
        url=JOB_URL.format(org=org_slug, slug=slug),
        title=str(title),
        company=org.get("name") or str(org_slug),
        description=build_description(hit),
        location_raw=location_raw,
        country=country,
        city=city,
        remote=REMOTE_KINDS.get(remote_raw, "unknown"),
        seniority_raw=seniority,
        employment_type=hit.get("contract_type") if isinstance(hit.get("contract_type"), str) else None,
        salary_text=salary_text(hit),
        tags=tags,
        posted_at=parse_date(hit.get("published_at_timestamp") or hit.get("published_at")),
        raw=hit,
    )


class WTTJ:
    name = "wttj"
    description = "Welcome to the Jungle (public Algolia index, EU-wide + remote)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        query = str(ctx.opt("query", "") or "")
        countries = [str(c) for c in (ctx.opt("countries", []) or [])]
        include_remote = bool(ctx.opt("include_remote", True))
        max_pages = int(ctx.opt("max_pages", 5))
        filters = build_filters(countries, include_remote)

        app, key = credentials(ctx)
        url = QUERY_URL.format(app=app, index=INDEX)
        headers = {
            "x-algolia-application-id": app,
            "x-algolia-api-key": key,
            "Content-Type": "application/json",
            "Referer": f"{SITE}/",
            "Origin": SITE,
        }
        seen: set[str] = set()
        yielded = 0
        for page in range(max_pages):
            body: dict[str, Any] = {"query": query, "hitsPerPage": HITS_PER_PAGE, "page": page}
            if filters:
                body["filters"] = filters
            payload = ctx.http.post_json(url, json_body=body, headers=headers)
            if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
                raise SourceHTTPError("wttj: unexpected Algolia response — expected {'hits': [...]}")
            hits = payload["hits"]
            if not hits:
                break
            for job in safe_records(hits, parse_hit, self.name):
                if job.url in seen:
                    continue
                seen.add(job.url)
                yield job
                yielded += 1
                if ctx.limit and yielded >= ctx.limit:
                    return
            pages = payload.get("nbPages")
            if isinstance(pages, int) and page + 1 >= pages:
                break


register(WTTJ())
