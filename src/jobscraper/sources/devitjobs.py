"""devitjobs family — one backend behind germantechjobs.de, swissdevjobs.ch, devitjobs.{nl,uk,com}.

``GET https://<site>/api/jobsLight`` → a JSON ARRAY of light rows (the whole active inventory in
one call, no pagination). Observed keys::

    {_id, jobUrl, redirectJobUrl, name, company, actualCity, cityCategory, activeFrom, jobType,
     expLevel, workplace, remoteType, language, annualSalaryFrom, annualSalaryTo, technologies,
     filterTags, techCategory, metaCategory, companyType, companySize, hasVisaSponsorship, …}

Note there is no ``country`` (the board implies it), no ``postedAt`` (it is ``activeFrom``) and no
description. ``workplace`` is ``office|hybrid|remote`` and ``remoteType`` is
``onlycountry|countryandeu|anywhere``; ``expLevel`` is ``Junior|Regular|Senior|Lead``.

``GET https://<site>/api/job/<_id>`` → the same row plus the prose: ``description`` (company
blurb), ``responsibilitiesTextArea``, ``requirementsMustTextArea``, ``requirementsNiceTextArea``
(plain text in every payload observed, but run through ``strip_html`` if markup shows up), plus
``tier``/``createdAt``/``met*`` methodology flags. The endpoint wants the ObjectId, not the slug.

Because the light feed is the whole inventory (hundreds of rows per board), detail calls are
capped: ``fetch_details`` (default true) and ``max_details`` (default 150, split evenly over the
configured sites, with any unused share rolling over). Rows that look entry-level go first — both
in the detail queue and in the yielded order — so a small ``max_details`` (or ``probe``'s limit)
is spent on the jobs this pipeline cares about. A failed detail costs the description, never the
job.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from jobscraper.http import SourceHTTPError, strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

API = "https://{site}/api/jobsLight"
DETAIL_API = "https://{site}/api/job/{job_id}"

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

WORKPLACE_REMOTE = {"remote": "remote", "hybrid": "hybrid", "office": "onsite", "onsite": "onsite"}
REMOTE_REGION = {"anywhere": "Anywhere", "onlycountry": "{country} only", "countryandeu": "{country} + EU"}

# The prose lives in four fields; only the first is unlabelled (it is the company blurb).
DETAIL_FIELDS = (
    ("description", None),
    ("responsibilitiesTextArea", "Responsibilities"),
    ("requirementsMustTextArea", "Requirements"),
    ("requirementsNiceTextArea", "Nice to have"),
)

# The German nouns take a `\w*` on both sides: they show up as compounds ("Pflichtpraktikum",
# "Werkstudententaetigkeit", "Absolventin"), where a plain `\b` prefix would miss them.
_ENTRY_LEVEL_RE = re.compile(
    r"\b(?:junior|jr|intern|internship|working student|trainee|graduate|entry[- ]level|"
    r"young professional"
    r"|\w*(?:praktikum|praktikant|werkstudent|absolvent|berufseinsteiger|einsteiger)\w*)\b",
    re.I,
)
_MARKUP_RE = re.compile(r"<(?:br|p|div|ul|ol|li|strong|em|b|i|h[1-6]|span|a)\b[^>]*>", re.I)


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


def detail_url(site: str, job_id: str) -> str:
    return DETAIL_API.format(site=site, job_id=job_id)


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


def record_remote(rec: dict[str, Any], *texts: str | None) -> str:
    """``workplace`` is authoritative when present; fall back to sniffing the location/title."""
    workplace = rec.get("workplace")
    if isinstance(workplace, str):
        mapped = WORKPLACE_REMOTE.get(workplace.strip().lower())
        if mapped:
            return mapped
    return guess_remote(*texts)


def remote_region(rec: dict[str, Any], country: str | None) -> str | None:
    """``remoteType`` says who may apply remotely: 'DE only', 'DE + EU', 'Anywhere'."""
    value = rec.get("remoteType")
    if not isinstance(value, str) or not value.strip():
        return None
    key = value.strip().lower()
    template = REMOTE_REGION.get(key)
    if template is None:
        return value.strip()
    if "{country}" in template and not country:
        return value.strip()
    return template.format(country=country)


def is_entry_level(rec: Any) -> bool:
    """Does this row look like something a graduate could apply to? (title / jobType / expLevel)"""
    if not isinstance(rec, dict):
        return False
    text = " ".join(str(rec.get(key) or "") for key in ("name", "jobType", "expLevel"))
    return bool(_ENTRY_LEVEL_RE.search(text))


def _prose(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = strip_html(value) if _MARKUP_RE.search(value) else value
    return text.strip() if text and text.strip() else None


def description_text(detail: dict[str, Any]) -> str | None:
    """Stitch the detail payload's four prose fields into one labelled plain-text description."""
    if not isinstance(detail, dict):
        return None
    parts: list[str] = []
    for key, label in DETAIL_FIELDS:
        text = _prose(detail.get(key))
        if text:
            parts.append(f"{label}:\n{text}" if label else text)
    return "\n\n".join(parts) or None


def parse_record(rec: dict[str, Any], *, site: str, detail: dict[str, Any] | None = None) -> Job | None:
    if not isinstance(rec, dict):
        return None
    if isinstance(detail, dict):
        rec = {**rec, **detail}  # the detail is a superset, minus a few light-only keys
    title = rec.get("name")
    url = job_url(site, rec)
    if not title or not url:
        return None
    city = rec.get("actualCity") or rec.get("cityCategory") or None
    city = city.strip() if isinstance(city, str) else None
    category = rec.get("cityCategory") if isinstance(rec.get("cityCategory"), str) else None
    country = record_country(rec, site)
    tags = _strings(rec.get("technologies") or rec.get("techs") or rec.get("filterTags"))
    language = rec.get("language")
    if isinstance(language, str) and language.strip():
        tags.append(f"lang:{language.strip()}")
    contracts = contract_types(rec.get("contractTypes"))
    job_type = rec.get("jobType") if isinstance(rec.get("jobType"), str) else None
    exp_level = rec.get("expLevel") if isinstance(rec.get("expLevel"), str) else None
    return Job(
        source="devitjobs",
        source_id=str(rec.get("_id") or url),
        url=url,
        title=str(title),
        company=rec.get("company") or None,
        description=description_text(rec),
        location_raw=", ".join(dict.fromkeys(p for p in (city, category, country) if p)) or None,
        country=country,
        city=city,
        remote=record_remote(rec, city, category, str(title)),
        remote_region=remote_region(rec, country),
        seniority_raw=exp_level or job_type,
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
        want_details = bool(ctx.opt("fetch_details", True))
        budget = max(int(ctx.opt("max_details", 150) or 0), 0) if want_details else 0
        yielded = 0
        seen: set[str] = set()
        for index, site in enumerate(sites):
            rows = self._light_feed(ctx, site)
            # Entry-level rows first: they get the detail calls, and `ctx.limit` keeps them.
            rows.sort(key=lambda rec: not is_entry_level(rec))
            share = budget // (len(sites) - index)  # unused share rolls over to the next board
            for job in safe_records(rows, lambda rec, s=site: parse_record(rec, site=s), self.name):
                if job.source_id in seen:
                    continue
                seen.add(job.source_id)
                if share > 0 and job.raw.get("_id"):
                    share -= 1
                    budget -= 1
                    job = self._with_detail(ctx, site, job)
                yield job
                yielded += 1
                if ctx.limit and yielded >= ctx.limit:
                    return

    def _light_feed(self, ctx: SourceContext, site: str) -> list[Any]:
        payload = ctx.http.get_json(API.format(site=site))
        if isinstance(payload, dict):  # some deploys wrap the array
            payload = payload.get("jobs") or payload.get("data") or []
        if not isinstance(payload, list):
            raise SourceHTTPError(f"devitjobs: {site} returned {type(payload).__name__}, expected a list")
        return list(payload)

    def _with_detail(self, ctx: SourceContext, site: str, job: Job) -> Job:
        """Re-parse the row with its detail payload; on any failure keep the light job."""
        try:
            detail = ctx.http.get_json(detail_url(site, str(job.raw["_id"])))
            if not isinstance(detail, dict):
                raise TypeError(f"expected an object, got {type(detail).__name__}")
            return parse_record(job.raw, site=site, detail=detail) or job
        except Exception as exc:
            log.warning("%s: no detail for %s (%s)", self.name, job.url, exc)
            return job


register(DevITJobs())
