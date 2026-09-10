"""jobs.careers.microsoft.com — Microsoft's own careers site, backed by a public JSON service.

The endpoint is undocumented (it is what the careers SPA calls), so every field below is
best-effort and read with ``.get()``. Verify with ``jobscraper probe microsoft``.

Search: ``GET https://gcsservices.careers.microsoft.com/search/api/v1/search``
    ``q`` free text, ``lc`` location (**repeated once per location** — httpx expands a list
    value into repeated keys), ``exp`` experience bucket ("Students and graduates"),
    ``p`` profession ("Software Engineering"), ``l=en_us``, ``pg`` (1-based), ``pgSz`` (max 20),
    ``o=Recent``, ``flt=true`` →
    ``{"operationResult": {"result": {"totalJobs": N, "jobs": [record, …]}}}``

Search record: ``jobId`` (numeric string), ``title``, ``postingDate`` (ISO 8601) and a
    ``properties`` bag holding ``description`` (a *short* HTML teaser), ``locations`` (list of
    "City, Region, Country"), ``primaryLocation``, ``workSiteFlexibility``
    ("Up to 100% work from home" / "Up to 50% work from home" / "Microsoft on-site only"),
    ``profession``, ``discipline``, ``jobType``, ``roleType``, ``employmentType``,
    ``educationLevel``.

Detail: ``GET …/search/api/v1/job/{jobId}?lang=en_us`` → the same envelope around
    ``description`` / ``responsibilities`` / ``qualifications`` (HTML), which together are the
    full posting text. Fetched per job (capped by ``max_details``); a detail failure is a
    per-record failure — the job keeps the search teaser.

Public link: ``https://jobs.careers.microsoft.com/global/en/job/{jobId}/``

Pagination is driven by ``totalJobs``: the loop stops once it has *received* that many records
(rather than by ``pg * pgSz``), so a page size the service silently shrinks — or a filter that
returns fewer rows than ``pgSz`` — still walks to the last page. ``max_pages`` is the hard cap.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from functools import partial
from typing import Any

from jobscraper.http import SourceHTTPError, strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

SEARCH_API = "https://gcsservices.careers.microsoft.com/search/api/v1/search"
DETAIL_API = "https://gcsservices.careers.microsoft.com/search/api/v1/job/{job_id}"
JOB_URL = "https://jobs.careers.microsoft.com/global/en/job/{job_id}/"
PAGE_SIZE = 20  # the service caps ``pgSz`` at 20

DEFAULT_QUERIES = ["software engineer", "software developer"]
DEFAULT_LOCATIONS = [
    "Finland", "Estonia", "Sweden", "Denmark", "Norway", "Germany", "Netherlands", "Ireland",
    "Czech Republic", "Poland", "Spain", "Portugal", "United Arab Emirates",
]
DEFAULT_EXPERIENCE = ["Students and graduates"]
DEFAULT_PROFESSION = "Software Engineering"

_PERCENT_RE = re.compile(r"(\d{1,3})\s*%")


def work_site_remote(flexibility: str | None) -> str | None:
    """``workSiteFlexibility`` → remote kind, or ``None`` when the wording is unfamiliar.

    Microsoft states work-from-home as a percentage ceiling: 100% is remote, anything in
    between is hybrid, 0% (and "Microsoft on-site only") is onsite. ``guess_remote`` cannot read
    those — "work from home" is not in its vocabulary and a percentage means nothing to it.
    """
    if not flexibility:
        return None
    low = flexibility.lower()
    match = _PERCENT_RE.search(low)
    if match:
        percent = int(match.group(1))
        if percent >= 100:
            return "remote"
        return "hybrid" if percent > 0 else "onsite"
    if "hybrid" in low:
        return "hybrid"
    if "on-site" in low or "onsite" in low or "in office" in low:
        return "onsite"
    if "work from home" in low or "remote" in low:
        return "remote"
    return None


def unwrap(payload: Any) -> dict[str, Any] | None:
    """Peel ``{"operationResult": {"result": …}}``; tolerate the envelope being absent."""
    if not isinstance(payload, dict):
        return None
    operation = payload.get("operationResult")
    if isinstance(operation, dict):
        result = operation.get("result")
        return result if isinstance(result, dict) else operation
    return payload


def parse_record(rec: Any, *, seniority: str | None = None) -> Job | None:
    if not isinstance(rec, dict):
        return None
    job_id = rec.get("jobId") or rec.get("id")
    title = rec.get("title")
    if not job_id or not title:
        return None
    props = rec.get("properties") if isinstance(rec.get("properties"), dict) else {}
    locations = [loc.strip() for loc in (props.get("locations") or []) if isinstance(loc, str) and loc.strip()]
    location_raw = props.get("primaryLocation") or rec.get("primaryLocation") or (locations[0] if locations else None)
    location_raw = str(location_raw).strip() if location_raw else None
    flexibility = props.get("workSiteFlexibility") or rec.get("workSiteFlexibility")
    tags = [t.strip() for t in (props.get("profession"), props.get("discipline")) if isinstance(t, str) and t.strip()]
    return Job(
        source="microsoft",
        source_id=str(job_id),
        url=JOB_URL.format(job_id=job_id),
        title=str(title),
        company="Microsoft",
        # The search payload carries only a teaser; ``hydrate`` replaces it with the full text.
        description=strip_html(props.get("description")),
        location_raw=location_raw,
        # "Helsinki, Uusimaa, Finland" ends in the country name, which ``guess_country`` knows.
        country=guess_country(location_raw, *locations),
        city=(location_raw.split(",")[0].strip() or None) if location_raw else None,
        remote=work_site_remote(flexibility) or guess_remote(str(title), flexibility),
        employment_type=props.get("employmentType") or None,
        # The searched ``exp`` bucket is a better seniority signal than the org-chart ``jobType``.
        seniority_raw=seniority or props.get("jobType") or props.get("roleType") or None,
        tags=list(dict.fromkeys(tags)),
        posted_at=parse_date(rec.get("postingDate") or props.get("postingDate")),
        raw=rec,
    )


def detail_description(payload: Any) -> str | None:
    """Join the detail's description + responsibilities + qualifications into plain text."""
    result = unwrap(payload)
    if not isinstance(result, dict):
        return None
    parts = [result.get(key) for key in ("description", "responsibilities", "qualifications")]
    html = "\n\n".join(p for p in parts if isinstance(p, str) and p.strip())
    return strip_html(html)


def hydrate(ctx: SourceContext, job: Job) -> None:
    """Replace the teaser with the full posting text; a failed detail never drops a job."""
    try:
        payload = ctx.http.get_json(DETAIL_API.format(job_id=job.source_id), params={"lang": "en_us"})
    except Exception as exc:
        log.warning("microsoft: detail %s failed: %s", job.source_id, exc)
        return
    text = detail_description(payload)
    if not text:
        return
    job.description = text
    job.raw = {**job.raw, "detail": unwrap(payload)}


class Microsoft:
    name = "microsoft"
    description = "Microsoft Careers search API (jobs.careers.microsoft.com, global)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        queries = _strings(ctx.opt("queries", DEFAULT_QUERIES))
        locations = _strings(ctx.opt("locations", DEFAULT_LOCATIONS))
        experience = _strings(ctx.opt("experience", DEFAULT_EXPERIENCE))
        profession = ctx.opt("profession", DEFAULT_PROFESSION)
        max_pages = int(ctx.opt("max_pages", 10))
        fetch_details = bool(ctx.opt("fetch_details", True))
        max_details = int(ctx.opt("max_details", 150))
        seen: set[str] = set()
        yielded = 0
        details = 0

        for query in queries or [""]:
            for exp in experience or [None]:
                received = 0
                for page in range(1, max_pages + 1):
                    result = self._search(ctx, query=query, locations=locations, exp=exp,
                                          profession=profession, page=page)
                    records = result.get("jobs") or []
                    if not records:
                        break
                    received += len(records)
                    for job in safe_records(records, partial(parse_record, seniority=exp), self.name):
                        if job.source_id in seen:
                            continue
                        seen.add(job.source_id)
                        if fetch_details and details < max_details:
                            details += 1
                            hydrate(ctx, job)
                        yield job
                        yielded += 1
                        if ctx.limit and yielded >= ctx.limit:
                            return
                    total = result.get("totalJobs")
                    if isinstance(total, int) and received >= total:
                        break

    def _search(self, ctx: SourceContext, *, query: str, locations: list[str], exp: str | None,
                profession: Any, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {"l": "en_us", "pg": page, "pgSz": PAGE_SIZE, "o": "Recent", "flt": "true"}
        if query:
            params["q"] = query
        if locations:
            params["lc"] = locations  # httpx repeats the key once per value
        if exp:
            params["exp"] = exp
        if profession:
            params["p"] = str(profession)
        payload = ctx.http.get_json(SEARCH_API, params=params)
        result = unwrap(payload)
        if result is None:
            raise SourceHTTPError(f"microsoft: unexpected search payload ({type(payload).__name__})")
        return result


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in (value or []) if str(v).strip()]


register(Microsoft())
