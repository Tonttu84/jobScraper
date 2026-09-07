"""devitjobs family — one backend behind germantechjobs.de, swissdevjobs.ch, devitjobs.{nl,uk,com}.

GET https://<site>/api/jobsLight → a JSON ARRAY of light rows:
``{_id, name, company, jobUrl, redirectJobUrl, actualCity, cityCategory, country, jobType,
   contractTypes, language, hasLangCheck, postedAt, postedAtUnix, annualSalaryFrom,
   annualSalaryTo, source, technologies|techs}``

The light feed is the whole active inventory in one call (no pagination) and carries no
description — that only exists on the per-job detail endpoint, which this adapter skips.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from jobscraper.http import SourceHTTPError
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

API = "https://{site}/api/jobsLight"

# Board → the country its postings are in, and the currency its annual salaries are quoted in.
SITE_COUNTRY = {
    "germantechjobs.de": "DE",
    "swissdevjobs.ch": "CH",
    "devitjobs.nl": "NL",
    "devitjobs.uk": "GB",
    "devitjobs.com": "US",
    "devitjobs.fr": "FR",
}
SITE_CURRENCY = {"DE": "EUR", "CH": "CHF", "NL": "EUR", "GB": "GBP", "US": "USD", "FR": "EUR"}


def _strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            name = item.get("name") or item.get("value")
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
    return out


def contract_types(value: Any) -> list[str]:
    """``{"permanent": true, "freelance": false}`` or ``["permanent"]`` → ``["permanent"]``."""
    if isinstance(value, dict):
        return [str(k) for k, flag in value.items() if flag]
    return _strings(value)


def job_url(site: str, rec: dict[str, Any]) -> str | None:
    raw = str(rec.get("jobUrl") or "").strip()
    if raw.startswith(("http://", "https://")):
        return raw
    if raw.startswith("/"):
        return f"https://{site}{raw}"
    if raw:
        return f"https://{site}/jobs/{raw}"
    redirect = rec.get("redirectJobUrl")
    if isinstance(redirect, str) and redirect.startswith("http"):
        return redirect
    return None


def _amount(value: Any) -> str | None:
    if isinstance(value, bool) or value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        return f"{int(value):,}"
    return str(value).strip() or None


def salary_text(rec: dict[str, Any], currency: str | None) -> str | None:
    low, high = _amount(rec.get("annualSalaryFrom")), _amount(rec.get("annualSalaryTo"))
    if not low and not high:
        return None
    span = f"{low} - {high}" if low and high and low != high else (low or high)
    return f"{span} {currency}/year" if currency else f"{span}/year"


def record_country(rec: dict[str, Any], site: str) -> str | None:
    """The row's own country wins when it is usable; otherwise the board's country."""
    value = rec.get("country")
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if len(text) == 2 and text.isalpha():
            return text.upper()
        guessed = guess_country(text)
        if guessed:
            return guessed
    return SITE_COUNTRY.get(site)


def parse_record(rec: dict[str, Any], *, site: str) -> Job | None:
    if not isinstance(rec, dict):
        return None
    title = rec.get("name")
    url = job_url(site, rec)
    if not title or not url:
        return None
    city = rec.get("actualCity") or rec.get("cityCategory") or None
    city = city.strip() if isinstance(city, str) else None
    category = rec.get("cityCategory") if isinstance(rec.get("cityCategory"), str) else None
    country = record_country(rec, site)
    tags = _strings(rec.get("technologies") or rec.get("techs"))
    language = rec.get("language")
    if isinstance(language, str) and language.strip():
        tags.append(f"lang:{language.strip()}")
    contracts = contract_types(rec.get("contractTypes"))
    job_type = rec.get("jobType") if isinstance(rec.get("jobType"), str) else None
    return Job(
        source="devitjobs",
        source_id=str(rec.get("_id") or url),
        url=url,
        title=str(title),
        company=rec.get("company") or None,
        location_raw=", ".join(dict.fromkeys(p for p in (city, category, country) if p)) or None,
        country=country,
        city=city,
        remote=guess_remote(city, category, str(title)),
        seniority_raw=job_type,
        employment_type=", ".join(dict.fromkeys(contracts)) or job_type,
        salary_text=salary_text(rec, SITE_CURRENCY.get(country or "")),
        tags=list(dict.fromkeys(tags)),
        posted_at=parse_date(rec.get("postedAt") or rec.get("postedAtUnix") or rec.get("activeFrom")),
        raw={**rec, "site": site},
    )


class DevITJobs:
    name = "devitjobs"
    description = "devitjobs boards (germantechjobs.de, swissdevjobs.ch, devitjobs.nl/uk/com)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        sites = [str(s).strip().lower() for s in (ctx.opt("sites", ["germantechjobs.de", "swissdevjobs.ch"]) or [])
                 if str(s).strip()]
        yielded = 0
        seen: set[str] = set()
        for site in sites:
            payload = ctx.http.get_json(API.format(site=site))
            if isinstance(payload, dict):  # some deploys wrap the array
                payload = payload.get("jobs") or payload.get("data") or []
            if not isinstance(payload, list):
                raise SourceHTTPError(f"devitjobs: {site} returned {type(payload).__name__}, expected a list")
            for job in safe_records(payload, lambda rec, s=site: parse_record(rec, site=s), self.name):
                if job.source_id in seen:
                    continue
                seen.add(job.source_id)
                yield job
                yielded += 1
                if ctx.limit and yielded >= ctx.limit:
                    return


register(DevITJobs())
