"""cv.ee — one of Estonia's two big private boards; keyless Elasticsearch-backed search API.

    GET https://cv.ee/api/v1/vacancy-search-service/search
        ?keywords=<kw>&categories[]=INFORMATION_TECHNOLOGY&limit=250&offset=0&sorting=LATEST
    -> {"total": N, "vacancies": [{id, positionTitle, employerName, townId, salaryFrom,
        salaryTo, remoteWork, remoteWorkType, publishDate, categories, keywords, ...}], ...}

The list carries no posting body and no readable town name (only ``townId``; the id→name map
lives behind ``/api/v1/locations-service/list``, which we don't fetch), and the research notes
document no per-vacancy detail endpoint — so these are title-only jobs with ``city=None`` and
the numeric town id kept in ``raw``. Pagination is offset-based until ``total`` is reached.

Recruitment agencies post under the employer name "Vahendatud pakkumised" ("mediated offers");
those rows are dropped because they carry no real employer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from jobscraper.models import Job
from jobscraper.sources._common import guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

API = "https://cv.ee/api/v1/vacancy-search-service/search"
JOB_URL = "https://cv.ee/en/vacancy/{id}"
PAGE_LIMIT = 250

DEFAULT_KEYWORDS = ["developer"]
DEFAULT_CATEGORIES = ["INFORMATION_TECHNOLOGY"]

_MEDIATED_RE = re.compile(r"vahendatud\s+pakkumised", re.IGNORECASE)
_REMOTE_TYPES = {"REMOTE": "remote", "HYBRID": "hybrid", "ON_SITE": "onsite"}


def _salary_text(rec: dict[str, Any]) -> str | None:
    low, high = rec.get("salaryFrom"), rec.get("salaryTo")
    bounds = [f"{float(v):.0f}" for v in (low, high) if isinstance(v, (int, float)) and v > 0]
    if not bounds:
        return None
    unit = "EUR/h" if rec.get("hourlySalary") else "EUR"
    return f"{' - '.join(bounds)} {unit}"


def parse_record(rec: dict[str, Any]) -> Job | None:
    vacancy_id = rec.get("id")
    title = rec.get("positionTitle")
    if not vacancy_id or not isinstance(title, str) or not title.strip():
        return None
    employer = rec.get("employerName")
    employer = employer.strip() if isinstance(employer, str) and employer.strip() else None
    if employer and _MEDIATED_RE.search(employer):
        return None

    remote_type = rec.get("remoteWorkType")
    remote = _REMOTE_TYPES.get(str(remote_type).upper()) if remote_type else None
    if not remote:
        flag = rec.get("remoteWork")
        remote = guess_remote(title, flag=flag if isinstance(flag, bool) else None)

    tags = [k for k in (rec.get("keywords") or []) if isinstance(k, str) and k.strip()]
    return Job(
        source="cvee",
        source_id=str(vacancy_id),
        url=JOB_URL.format(id=vacancy_id),
        title=title.strip(),
        company=employer,
        description=None,  # the search API returns no body and no detail endpoint is documented
        location_raw=None,
        country="EE",
        city=None,  # only a numeric townId is exposed; kept in `raw`
        remote=remote,
        salary_text=_salary_text(rec),
        tags=tags,
        posted_at=parse_date(rec.get("publishDate") or rec.get("renewedDate")),
        raw=rec,
    )


class CvEe:
    name = "cvee"
    description = "cv.ee vacancy search API (Estonia, IT category)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        keywords = ctx.opt("keywords", DEFAULT_KEYWORDS) or DEFAULT_KEYWORDS
        if isinstance(keywords, str):
            keywords = [keywords]
        categories = ctx.opt("categories", DEFAULT_CATEGORIES) or DEFAULT_CATEGORIES
        if isinstance(categories, str):
            categories = [categories]
        limit = int(ctx.opt("page_size", PAGE_LIMIT))
        max_pages = int(ctx.opt("max_pages", 5))

        seen: set[str] = set()
        emitted = 0
        for keyword in keywords:
            offset = 0
            for _page in range(max_pages):
                payload = ctx.http.get_json(
                    API,
                    params={
                        "keywords": str(keyword),
                        "categories[]": [str(c) for c in categories],
                        "limit": limit,
                        "offset": offset,
                        "sorting": "LATEST",
                    },
                )
                vacancies = payload.get("vacancies") if isinstance(payload, dict) else None
                if not isinstance(vacancies, list) or not vacancies:
                    break
                for job in safe_records(vacancies, parse_record, self.name):
                    if job.source_id in seen:
                        continue
                    seen.add(job.source_id)
                    yield job
                    emitted += 1
                    if ctx.limit and emitted >= ctx.limit:
                        return
                offset += len(vacancies)
                total = payload.get("total")
                if isinstance(total, int) and offset >= total:
                    break
                if len(vacancies) < limit:
                    break


register(CvEe())
