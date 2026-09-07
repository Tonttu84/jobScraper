"""remotive.com — remote-only job board with a free public API (worldwide, tech-heavy).

GET https://remotive.com/api/remote-jobs?category=software-dev → {"jobs": [...], "job-count": N}
plus two prose keys ("00-warning", "0-legal-notice"). The feed carries the full description
inline, so there is no detail call.

The API is rate limited (the provider asks for ~4 requests a day and serves 24h-delayed data),
so this adapter makes **exactly one request per run** — no pagination, no per-query loops.

Checked against the live response on 2026-09-07: the free API **ignores both ``category`` and
``limit``** and answers every call with the same ~18-posting teaser feed, categories mixed
(Sales, Writing, "All others"). The documented parameters are still sent in case that changes,
but the same choice is applied client-side — see ``DEFAULT_CATEGORIES`` and the ``categories``
option; ``ctx.limit`` is likewise honoured on our side.

Every posting here is remote; ``candidate_required_location`` says who may apply and goes to
``Job.remote_region`` for the location filter to classify ("Europe", "USA", "Worldwide").
``category`` is prepended to ``Job.tags``, ``job_type`` ("full_time", "contract", "freelance",
"part_time") is the employment type, ``salary`` is free text ("$170k - $200k", "$14/hour",
"" when unpublished) and ``publication_date`` is a naive ISO timestamp read as UTC.
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
DEFAULT_CATEGORY = "software-dev"
# Category *names* as the payload spells them (the query parameter takes slugs, the records
# carry display names). Substring match, so "Data" also keeps "Data Science".
DEFAULT_CATEGORIES = [
    "software development",
    "devops",
    "quality assurance",
    "information technology",
    "data",
]

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


def matches_category(rec: dict[str, Any], wanted: list[str]) -> bool:
    """Permissive: an unstated category is kept, the AI stages get the last word."""
    if not wanted:
        return True
    category = str(rec.get("category") or "").lower()
    if not category:
        return True
    return any(w.lower() in category for w in wanted if w)


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
        category = ctx.opt("category", DEFAULT_CATEGORY)
        if category:
            params["category"] = str(category)
        search = ctx.opt("search")
        if search:
            params["search"] = str(search)
        if ctx.limit:
            params["limit"] = int(ctx.limit)
        payload = ctx.http.get_json(API, params=params)
        records = (payload or {}).get("jobs") or []
        # The API ignores ?category=, so keep only the categories we asked for.
        wanted = [str(c) for c in (ctx.opt("categories", DEFAULT_CATEGORIES) or []) if c]
        records = [r for r in records if isinstance(r, dict) and matches_category(r, wanted)]
        yield from take(safe_records(records, parse_record, self.name), ctx.limit)


register(Remotive())
