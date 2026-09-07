"""justjoin.it — Polish/CEE IT marketplace, same-origin candidate API.

The old public host is gone: ``api.justjoin.it`` (``v2/user-panel/offers/by-cursor`` and
``v1/offers/{slug}``) answers HTTP 503 from nginx for every path. The site now calls a
same-origin API instead.

List:   GET https://justjoin.it/api/candidate-api/offers
        ?experienceLevels=junior&experienceLevels=mid   — repeat the param per level;
          the comma-joined form (``junior,mid``) is accepted but matches nothing.
        &isRemote=true                                  — the only workplace filter the API
          honours; ``workplaceTypes``/``workplaceType``/``remote`` are silently ignored, so
          anything other than "remote only" is filtered here after parsing.
        &sortBy=publishedAt&orderBy=descending&from=<cursor>&itemsCount=<n>
        → ``{"data": [...], "meta": {"from": N, "totalItems": N,
             "prev": {...}, "next": {"cursor": N, "itemsCount": N}}}``
        ``from`` + ``itemsCount`` page it (``page``/``perPage`` are ignored); itemsCount up to
        500 is honoured. The final page comes back with ``next.cursor == totalItems``, and
        reading past the end returns an empty ``data`` with ``next.cursor: null``.
Detail: GET https://justjoin.it/api/candidate-api/offers/{slug}
        → ``{"body": "<html>", "countryCode": "PL", "experienceLevel": "junior", ...}``
        (404 with an RFC-7231 problem document for an unknown slug).

The list records carry no country and no description: the country is taken from the city
(default PL) and corrected from the detail's ``countryCode``, the body comes from the detail
call. Neither response has an apply link worth linking to, so the canonical URL is built from
the slug: ``https://justjoin.it/job-offer/{slug}``.
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

LIST_API = "https://justjoin.it/api/candidate-api/offers"
DETAIL_API = "https://justjoin.it/api/candidate-api/offers/{slug}"
JOB_URL = "https://justjoin.it/job-offer/{slug}"
DEFAULT_COUNTRY = "PL"
DEFAULT_ITEMS_COUNT = 100
JSON_HEADERS = {"Accept": "application/json"}

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


def _contract(value: Any) -> str | None:
    """``b2b``/``permanent``/… ; justjoin's ``any`` means "unspecified", not a contract type."""
    if not isinstance(value, str):
        return None
    kind = value.strip().lower()
    return kind if kind and kind != "any" else None


def _country_code(rec: dict[str, Any]) -> str | None:
    candidates: list[Any] = [rec.get("countryCode")]
    locations = rec.get("locations")
    if isinstance(locations, list):
        candidates += [loc.get("countryCode") for loc in locations if isinstance(loc, dict)]
    for code in candidates:
        if isinstance(code, str) and len(code.strip()) == 2 and code.strip().isalpha():
            return code.strip().upper()
    return None


def _original_rows(employment_types: Any) -> list[dict[str, Any]]:
    """Salaries are repeated once per currency; keep only the one the employer entered."""
    rows = [e for e in (employment_types or []) if isinstance(e, dict)]
    original = [e for e in rows if str(e.get("currencySource") or "").strip().lower() == "original"]
    return original or rows


def salary_text(employment_types: Any) -> str | None:
    """``[{"from": 13440, "to": 25200, "currency": "PLN", "type": "b2b"}]`` → readable text.

    ``from``/``to`` are the monthly-normalized figures even when ``unit`` is ``Hour``
    (``fromPerUnit``/``toPerUnit`` hold the hourly rate), so the text is always per month.
    """
    parts: list[str] = []
    for entry in _original_rows(employment_types):
        low, high = _amount(entry.get("from")), _amount(entry.get("to"))
        if not low and not high:
            continue
        span = f"{low} - {high}" if low and high and low != high else (low or high)
        currency = entry.get("currency")
        if isinstance(currency, str) and currency.strip():
            span = f"{span} {currency.strip().upper()}"
        kind = _contract(entry.get("type"))
        if kind:
            span = f"{span} ({kind})"
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
    country = _country_code(rec) or guess_country(city) or DEFAULT_COUNTRY
    workplace = str(rec.get("workplaceType") or "").strip().lower()
    remote = WORKPLACE_REMOTE.get(workplace) or guess_remote(city, str(title))
    employment_types = rec.get("employmentTypes")
    kinds = [
        k
        for k in (_contract(e.get("type")) for e in (employment_types or []) if isinstance(e, dict))
        if k
    ]
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
        detail = ctx.http.get_json(DETAIL_API.format(slug=slug), headers=JSON_HEADERS)
    except Exception as exc:
        log.warning("justjoin: detail %s failed: %s", slug, exc)
        return
    if not isinstance(detail, dict):
        return
    job.description = strip_html(detail.get("body"))
    skills = [*_names(detail.get("requiredSkills")), *_names(detail.get("niceToHaveSkills"))]
    if skills:
        job.tags = list(dict.fromkeys([*job.tags, *skills]))
    level = detail.get("experienceLevel")
    if isinstance(level, dict):
        level = level.get("value")
    if isinstance(level, str) and level.strip():
        job.seniority_raw = level.strip()
    # Only trust the location of a payload that is actually about this offer.
    if detail.get("slug") == slug:
        job.country = _country_code(detail) or job.country
    job.raw = {**job.raw, "detail": detail}


class JustJoin:
    name = "justjoin"
    description = "justjoin.it candidate API (Poland/CEE IT marketplace)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        levels = [str(v).strip() for v in (ctx.opt("experience_levels", ["junior"]) or []) if str(v).strip()]
        workplaces = {str(v).strip().lower() for v in (ctx.opt("workplace_types", []) or []) if str(v).strip()}
        max_pages = int(ctx.opt("max_pages", 10))
        items_count = int(ctx.opt("items_count", DEFAULT_ITEMS_COUNT))
        fetch_details = bool(ctx.opt("fetch_details", True))
        max_details = int(ctx.opt("max_details", 200))

        base_params: dict[str, Any] = {"sortBy": "publishedAt", "orderBy": "descending"}
        if levels:
            base_params["experienceLevels"] = levels
        if workplaces == {"remote"}:
            # The API can narrow this one server-side; every other selection is filtered below.
            base_params["isRemote"] = "true"

        cursor = 0
        yielded = 0
        details = 0
        seen: set[str] = set()
        for _page in range(max_pages):
            params = {**base_params, "from": cursor, "itemsCount": items_count}
            payload = ctx.http.get_json(LIST_API, params=params, headers=JSON_HEADERS)
            if not isinstance(payload, dict):
                raise SourceHTTPError(f"justjoin: expected an object, got {type(payload).__name__}")
            data = payload.get("data") or []
            if not data:
                break
            for job in safe_records(data, parse_offer, self.name):
                if job.source_id in seen:
                    continue
                seen.add(job.source_id)
                if not _wanted(job, workplaces):
                    continue
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
            total = meta.get("totalItems")
            # A missing or non-advancing cursor means there is no next page (or the param was
            # ignored); on the last page the cursor equals the total item count.
            if not isinstance(next_cursor, int) or next_cursor <= cursor:
                break
            if isinstance(total, int) and next_cursor >= total:
                break
            cursor = next_cursor


def _wanted(job: Job, workplaces: set[str]) -> bool:
    """``workplace_types`` is not a server-side filter, so honour it against the parsed record."""
    if not workplaces:
        return True
    raw = str(job.raw.get("workplaceType") or "").strip().lower()
    return raw in workplaces or job.remote in workplaces


register(JustJoin())
