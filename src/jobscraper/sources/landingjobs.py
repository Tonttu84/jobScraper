"""landing.jobs — Portugal-based, Europe-wide tech board with a public v1 feed.

GET https://landing.jobs/api/v1/jobs?limit=50&offset=N → a JSON ARRAY of
``{id, title, url, locations: [{city, country_code}], remote, created_at, updated_at,
   published_at, expires_at, type, tags, currency_code, gross_salary_low,
   gross_salary_high, role_description, main_requirements, nice_to_have, perks,
   relocation_paid}``

The whole feed is small (tens of jobs) and NOT ordered by date: it keeps long-lived postings,
so plenty of rows are a year old. ``published_at`` is the posting date; ``updated_at`` is a
bulk re-stamp (identical across most rows) and must not be used for it.

There is no company field at all — the employer is humanized from the posting URL
(``https://landing.jobs/at/<company>/<job>``). Salary is ``gross_salary_low``/``_high`` in
``currency_code`` (EUR, sometimes BRL); only a fifth of the rows carry one.
Paging stops on the first empty (or short) page.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from jobscraper.http import SourceHTTPError, strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

API = "https://landing.jobs/api/v1/jobs"
PAGE_SIZE = 50


def company_from_url(url: str | None) -> str | None:
    """``https://landing.jobs/at/acme-labs/junior-dev`` → ``Acme Labs``."""
    if not isinstance(url, str) or not url.strip():
        return None
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if len(segments) < 2 or segments[0] != "at":
        return None
    return " ".join(word.capitalize() for word in segments[1].replace("_", "-").split("-") if word) or None


def _amount(value: Any) -> str | None:
    if isinstance(value, bool) or value in (None, "", 0):
        return None
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return str(value).strip() or None


def salary_text(rec: dict[str, Any]) -> str | None:
    low, high = _amount(rec.get("gross_salary_low")), _amount(rec.get("gross_salary_high"))
    if not low and not high:
        return None
    span = f"{low} - {high}" if low and high and low != high else (low or high)
    currency = rec.get("currency_code") or rec.get("gross_salary_currency") or rec.get("currency")
    if isinstance(currency, str) and currency.strip():
        span = f"{span} {currency.strip().upper()}"
    return span


def _locations(rec: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """→ (location_raw, city, country) from ``locations: [...]`` or a plain ``location`` string.

    A posting can list several offices in several countries; each city keeps its own code
    ("Munich (DE), Lisbon (PT), Cologne (DE)") and the first country wins, because dropping it
    for being ambiguous would make the rule filter throw the posting away as "on-site with
    unknown country".
    """
    entries = [loc for loc in (rec.get("locations") or []) if isinstance(loc, dict)]
    labels, cities, countries = [], [], []
    for loc in entries:
        city = loc.get("city")
        city = city.strip() if isinstance(city, str) and city.strip() else None
        code = str(loc.get("country_code") or "").strip()
        code = code.upper() if len(code) == 2 and code.isalpha() else None
        if city:
            cities.append(city)
        if code:
            countries.append(code)
        if city and code:
            labels.append(f"{city} ({code})")
        elif city or code:
            labels.append(city or code)
    if labels:
        return ", ".join(dict.fromkeys(labels)), (cities[0] if cities else None), (countries[0] if countries else None)
    text = rec.get("location") if isinstance(rec.get("location"), str) else None
    text = text.strip() if text else None
    if not text:
        return None, None, None
    return text, text.split(",")[0].strip() or None, guess_country(text)


def build_description(rec: dict[str, Any]) -> str | None:
    parts = []
    for key in ("role_description", "main_requirements", "nice_to_have"):
        value = rec.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return strip_html("\n\n".join(parts)) if parts else None


def parse_record(rec: dict[str, Any]) -> Job | None:
    if not isinstance(rec, dict):
        return None
    title = rec.get("title")
    url = rec.get("url") if isinstance(rec.get("url"), str) else None
    if not title or not url:
        return None
    location_raw, city, country = _locations(rec)
    remote_flag = rec.get("remote")
    remote_flag = remote_flag if isinstance(remote_flag, bool) else None
    tags = [t for t in (rec.get("tags") or []) if isinstance(t, str)]
    return Job(
        source="landingjobs",
        source_id=str(rec.get("id") or url),
        url=url,
        title=str(title),
        company=rec.get("company_name") or company_from_url(url),
        description=build_description(rec),
        location_raw=", ".join(p for p in (location_raw, "Remote" if remote_flag else None) if p) or None,
        country=country,
        city=city,
        remote=guess_remote(location_raw, str(title), flag=remote_flag),
        employment_type=rec.get("type") if isinstance(rec.get("type"), str) else None,
        salary_text=salary_text(rec),
        tags=tags,
        posted_at=parse_date(rec.get("published_at") or rec.get("created_at")),
        raw=rec,
    )


class LandingJobs:
    name = "landingjobs"
    description = "landing.jobs public v1 feed (Portugal + Europe-wide tech)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        max_pages = int(ctx.opt("max_pages", 10))
        yielded = 0
        seen: set[str] = set()
        for page in range(max_pages):
            payload = ctx.http.get_json(API, params={"limit": PAGE_SIZE, "offset": page * PAGE_SIZE})
            if isinstance(payload, dict):  # tolerate a wrapped array
                payload = payload.get("jobs") or payload.get("data") or []
            if not isinstance(payload, list):
                raise SourceHTTPError(f"landingjobs: expected a list, got {type(payload).__name__}")
            if not payload:
                break
            for job in safe_records(payload, parse_record, self.name):
                if job.source_id in seen:
                    continue
                seen.add(job.source_id)
                yield job
                yielded += 1
                if ctx.limit and yielded >= ctx.limit:
                    return
            if len(payload) < PAGE_SIZE:  # last page
                break


register(LandingJobs())
