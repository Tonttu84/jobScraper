"""Company career boards behind a known ATS, via the ``ats-scrapers`` library.

Give it any public careers URL (Greenhouse, Lever, Ashby, Workable, SmartRecruiters,
Recruitee, Personio, Breezy, Teamtailor, …); ``get_scraper_for_url`` recognizes the ATS
from the URL shape and returns a scraper whose ``.fetch()`` yields ``ats_scrapers`` Job
rows, which this adapter normalizes into our ``Job``.

Custom-domain careers sites (careers.example.com fronting Phenom/Avature/Eightfold)
cannot be resolved from the URL alone — ``ats-scrapers`` raises for those, and the board
is logged and skipped like any other failure. One board failing (dead company, ATS
change, 403) never stops the rest; only an *all boards failed* run raises.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from ats_scrapers import Job as ATSJob
from ats_scrapers import get_scraper_for_url

from jobscraper.http import strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, guess_remote
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

NAME = "ats_boards"

# ats-scrapers normalizes most descriptions to text, but some providers (Lever, Greenhouse)
# hand back assembled HTML — strip only when the text actually contains tags.
_HTML_RE = re.compile(
    r"<(?:br|p|div|ul|ol|li|h[1-6]|strong|em|a|span|table)\b|</[a-z]+>", re.IGNORECASE
)
_REMOTEISH_RE = re.compile(r"\b(remote|anywhere|telecommute|distributed)\b", re.IGNORECASE)


def _describe(text: str | None) -> str | None:
    if not text:
        return None
    return strip_html(text) if _HTML_RE.search(text) else text


def _city(location: str | None) -> str | None:
    if not location:
        return None
    first = location.split(",")[0].strip()
    if not first or _REMOTEISH_RE.search(first):
        return None
    return first


def _salary_text(job: ATSJob) -> str | None:
    if job.salary_summary:
        return job.salary_summary
    if job.salary_min is None and job.salary_max is None:
        return None
    amounts = [f"{int(a):,}".replace(",", " ") for a in (job.salary_min, job.salary_max) if a]
    parts = ["–".join(amounts)]
    if job.salary_currency:
        parts.append(job.salary_currency)
    if job.salary_period:
        parts.append(f"/ {job.salary_period.lower()}")
    return " ".join(parts)


def convert(job: ATSJob) -> Job | None:
    """One ats-scrapers Job → our Job."""
    url = str(job.url)
    ats_type = getattr(job.ats_type, "value", job.ats_type)
    location = job.location or None
    tags = [t for t in (job.department, job.team) if t]
    raw: dict[str, Any] = job.model_dump(mode="json", exclude={"description"})
    return Job(
        source=NAME,
        source_id=f"{ats_type}:{job.ats_id or url}",
        url=url,
        title=job.title,
        company=job.company,
        description=_describe(job.description),
        location_raw=location,
        country=job.country_iso or guess_country(location),
        city=_city(location),
        remote=guess_remote(location, job.title, flag=job.is_remote),
        employment_type=job.employment_type or job.commitment,
        salary_text=_salary_text(job),
        tags=tags,
        posted_at=job.posted_at,
        raw=raw,
    )


class ATSBoards:
    name = NAME
    description = "Company career boards (Greenhouse/Lever/Ashby/… via ats-scrapers)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        urls = [str(u).strip() for u in (ctx.opt("urls") or []) if str(u).strip()]
        if not urls:
            raise ValueError("ats_boards: no careers URLs configured (option 'urls')")
        timeout = float(ctx.opt("timeout", 30.0))
        include_descriptions = bool(ctx.opt("include_descriptions", True))

        seen = 0
        failures: list[str] = []
        for url in urls:
            try:
                scraper = get_scraper_for_url(
                    url, timeout=timeout, include_descriptions=include_descriptions
                )
                ats_jobs = scraper.fetch()
            except Exception as exc:
                failures.append(url)
                log.warning("%s: board %s failed: %s", self.name, url, exc)
                continue
            log.info("%s: %s -> %d jobs", self.name, url, len(ats_jobs))
            for job in safe_records(ats_jobs, convert, self.name):
                yield job
                seen += 1
                if ctx.limit and seen >= ctx.limit:
                    return
        if len(failures) == len(urls):
            raise RuntimeError(
                f"ats_boards: all {len(urls)} boards failed "
                f"({', '.join(failures[:3])}{'…' if len(failures) > 3 else ''})"
            )


register(ATSBoards())
