"""remotive.com — remote-only job board with a free public API (worldwide, tech-heavy).

GET https://remotive.com/api/remote-jobs?category=software-dev → {"jobs": [...], "job-count": N}
The feed carries the full description inline, so there is no detail call.

The API is rate limited (the provider asks for ~4 requests a day and serves 24h-delayed data),
so this adapter makes **exactly one request per run** — no pagination, no per-query loops.
Every posting here is remote; ``candidate_required_location`` says who may apply and goes to
``Job.remote_region`` for the location filter to classify ("Europe", "USA Only", "Anywhere").
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from jobscraper.http import strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records, take

API = "https://remotive.com/api/remote-jobs"

_ANYWHERE_RE = re.compile(
    r"\b(anywhere|worldwide|world ?wide|global|any location)\b", re.IGNORECASE
)
_REGION_SPLIT_RE = re.compile(r"\s*(?:,|/|;|\||\bor\b|\band\b)\s*", re.IGNORECASE)


def region_country(region: str | None) -> str | None:
    """ISO2 only when the "who can apply" text names exactly one country.

    "Germany" → DE, but "Europe", "UK, USA" or "Anywhere" stay None so that the location
    filter treats them as regions rather than a concrete country.
    """
    if not region or _ANYWHERE_RE.search(region):
        return None
    codes = {guess_country(part) for part in _REGION_SPLIT_RE.split(region) if part.strip()}
    codes.discard(None)
    return codes.pop() if len(codes) == 1 else None


def parse_record(rec: dict[str, Any]) -> Job | None:
    if not isinstance(rec, dict):
        return None
    job_id = rec.get("id")
    url = rec.get("url")
    title = rec.get("title")
    if not title or not (job_id or url):
        return None
    region = rec.get("candidate_required_location") or None
    tags = [t for t in (rec.get("tags") or []) if isinstance(t, str)]
    category = rec.get("category")
    if isinstance(category, str) and category and category not in tags:
        tags.insert(0, category)
    return Job(
        source="remotive",
        source_id=str(job_id or url),
        url=url or f"https://remotive.com/remote-jobs/{job_id}",
        title=title,
        company=rec.get("company_name"),
        description=strip_html(rec.get("description")),
        location_raw=region,
        country=region_country(region),
        remote="remote",
        remote_region=region,
        employment_type=rec.get("job_type") or None,
        tags=tags,
        salary_text=rec.get("salary") or None,
        posted_at=parse_date(rec.get("publication_date")),
        raw=rec,
    )


class Remotive:
    name = "remotive"
    description = "remotive.com remote-jobs API (worldwide; rate limited, one request per run)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        params: dict[str, Any] = {}
        category = ctx.opt("category", "software-dev")
        if category:
            params["category"] = str(category)
        search = ctx.opt("search")
        if search:
            params["search"] = str(search)
        if ctx.limit:
            params["limit"] = int(ctx.limit)
        payload = ctx.http.get_json(API, params=params)
        records = (payload or {}).get("jobs") or []
        yield from take(safe_records(records, parse_record, self.name), ctx.limit)


register(Remotive())
