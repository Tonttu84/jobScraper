"""arbeitnow.com — free public job-board API (DACH-heavy, some wider EU; DE/EN mixed).

GET https://www.arbeitnow.com/api/job-board-api?page=N → {"data": [...], "links": {...}, "meta": {...}}
100 jobs per page, newest first. Don't follow ``links.next`` (carries a rotating search param).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from jobscraper.http import strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

API = "https://www.arbeitnow.com/api/job-board-api"


def parse_record(rec: dict[str, Any]) -> Job | None:
    slug = rec.get("slug") or rec.get("url")
    if not slug or not rec.get("title"):
        return None
    location = rec.get("location") or None
    remote_flag = rec.get("remote")
    tags = [t for t in (rec.get("tags") or []) if isinstance(t, str)]
    job_types = [t for t in (rec.get("job_types") or []) if isinstance(t, str)]
    return Job(
        source="arbeitnow",
        source_id=str(slug),
        url=rec.get("url") or f"https://www.arbeitnow.com/jobs/companies/{slug}",
        title=rec["title"],
        company=rec.get("company_name"),
        description=strip_html(rec.get("description")),
        location_raw=location,
        country=guess_country(location) or ("DE" if location else None),
        city=location.split(",")[0].strip() if location else None,
        remote=guess_remote(location, rec.get("title"), flag=remote_flag if isinstance(remote_flag, bool) else None),
        employment_type=", ".join(job_types) or None,
        tags=tags,
        posted_at=parse_date(rec.get("created_at")),
        raw=rec,
    )


class Arbeitnow:
    name = "arbeitnow"
    description = "arbeitnow.com public API (Germany/DACH + some EU)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        max_pages = int(ctx.opt("max_pages", 5))
        seen = 0
        for page in range(1, max_pages + 1):
            payload = ctx.http.get_json(API, params={"page": page})
            data = payload.get("data") or []
            if not data:
                break
            for job in safe_records(data, parse_record, self.name):
                yield job
                seen += 1
                if ctx.limit and seen >= ctx.limit:
                    return


register(Arbeitnow())
