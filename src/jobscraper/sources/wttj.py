"""Welcome to the Jungle — the public Algolia index behind welcometothejungle.com/jobs.

Step 1: GET https://www.welcometothejungle.com/api/env → a ``window.env = {...};`` blob with
        PUBLIC_ALGOLIA_APPLICATION_ID + PUBLIC_ALGOLIA_API_KEY_CLIENT (they rotate; the
        constants below are the fallback).
Step 2: POST https://{APP}-dsn.algolia.net/1/indexes/wttj_jobs_production_en/query
        headers x-algolia-application-id / x-algolia-api-key (+ a WTTJ Referer — the client
        key is referer-locked), body {"query", "hitsPerPage", "page", "filters"}
        → ``{"hits": [...], "nbPages": N}``
Step 3 (optional, ``fetch_details``): GET
        https://api.welcometothejungle.com/api/v1/organizations/{org_slug}/jobs/{job_slug}
        → ``{"job": {...}}``, the document the site's own job page renders.

The board is global, so ``countries``/``include_remote`` narrow it server-side with an Algolia
filter expression; anything past the per-query hit cap is invisible otherwise.

A hit carries no full description: only ``summary`` (a generated synopsis), ``key_missions`` and
a ``profile`` snippet, which together run 850–2400 characters. The detail document adds the real
``description`` and the untruncated ``profile`` (plus ``recruitment_process``, usually null), so
each job costs one extra request — capped by ``max_details`` (default 150) so a wide query can't
turn into thousands of calls. A detail that fails is logged and the job is kept with the index
text. Verified live 2026-09: the endpoint answers JSON without auth.

Field notes: ``remote`` is one of no/punctual/partial/fulltime/unknown; ``offices[0]`` has the
ISO country_code; ``experience_level_minimum`` is a float count of years; tags come from
``sectors``, ``new_profession`` (job family) and ``language`` (as ``lang:xx``, so the rule stage
can see a French-only posting without reading the text).
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
DETAIL_URL = "https://api.welcometothejungle.com/api/v1/organizations/{org}/jobs/{slug}"
JOB_URL = SITE + "/en/companies/{org}/jobs/{slug}"
HITS_PER_PAGE = 100
MAX_DETAILS = 150

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
    except Exception as exc:
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


def _texts(*values: Any) -> list[str]:
    """The non-empty strings among ``values`` (anything else is dropped)."""
    return [v.strip() for v in values if isinstance(v, str) and v.strip()]


def build_tags(hit: dict[str, Any]) -> list[str]:
    """``sectors`` names + the ``new_profession`` job family + ``lang:<language>``."""
    tags: list[str] = []
    for sector in hit.get("sectors") or []:
        if isinstance(sector, dict):
            tags.extend(_texts(sector.get("name")))
    profession = hit.get("new_profession")
    if isinstance(profession, dict):
        tags.extend(_texts(profession.get("sub_category_name"), profession.get("pivot_name")))
    language = hit.get("language")
    if isinstance(language, str) and language.strip():
        tags.append(f"lang:{language.strip().lower()}")
    return list(dict.fromkeys(tags))


def build_description(hit: dict[str, Any], detail: dict[str, Any] | None = None) -> str | None:
    """Index synopsis + key missions, with the detail document's real body when we have it."""
    parts = _texts(hit.get("summary"))
    if detail:
        parts += _texts(detail.get("description"), detail.get("profile"), detail.get("recruitment_process"))
    else:
        parts += _texts(hit.get("profile"))
    missions = _texts(*(hit.get("key_missions") or []))
    if missions:
        parts.append("\n".join(f"- {m}" for m in missions))
    return strip_html("\n\n".join(dict.fromkeys(parts)))


def detail_url(hit: dict[str, Any]) -> str | None:
    """The public JSON document for a hit, or ``None`` when the slugs are missing."""
    org = hit.get("organization") if isinstance(hit.get("organization"), dict) else {}
    org_slug, slug = org.get("slug"), hit.get("slug")
    if not isinstance(org_slug, str) or not isinstance(slug, str) or not org_slug or not slug:
        return None
    return DETAIL_URL.format(org=org_slug, slug=slug)


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
        tags=build_tags(hit),
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
        fetch_details = bool(ctx.opt("fetch_details", True))
        max_details = int(ctx.opt("max_details", MAX_DETAILS))
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
        details = 0
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
                if fetch_details and details < max_details and self._hydrate(ctx, job):
                    details += 1
                yield job
                yielded += 1
                if ctx.limit and yielded >= ctx.limit:
                    return
            pages = payload.get("nbPages")
            if isinstance(pages, int) and page + 1 >= pages:
                break

    def _hydrate(self, ctx: SourceContext, job: Job) -> bool:
        """Replace the index synopsis with the detail document's full text.

        Returns whether a request was made (so the caller counts it against ``max_details``).
        A failing detail is logged and the job keeps the index text.
        """
        url = detail_url(job.raw)
        if not url:
            return False
        try:
            payload = ctx.http.get_json(url)
        except Exception as exc:
            log.warning("%s: detail %s failed (%s)", self.name, job.source_id, exc)
            return True
        detail = payload.get("job") if isinstance(payload, dict) else None
        if not isinstance(detail, dict):
            log.warning("%s: detail %s had no 'job' object", self.name, job.source_id)
            return True
        text = build_description(job.raw, detail)
        if text:
            job.description = text
        apply_url = detail.get("apply_url")
        if isinstance(apply_url, str) and apply_url.startswith(("http://", "https://")):
            job.raw = {**job.raw, "apply_url": apply_url.strip()}
        return True


register(WTTJ())
