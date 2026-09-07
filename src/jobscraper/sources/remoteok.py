"""remoteok.com — remote-only tech board with a single free JSON endpoint.

GET https://remoteok.com/api → a JSON *array* whose first element is an API/legal notice
object (``{"legal": ..., "last_updated": ...}``); the job objects follow. Anything without
both ``id`` and ``position`` is skipped. No pagination, no auth: one request per run.

The board is small (~100 live postings), so the ``tags`` option is applied client-side as a
permissive OR filter over the posting's tags *and* title. Every posting is remote;
``location`` ("Worldwide", "Europe", "United States") goes to ``Job.remote_region``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from jobscraper.http import strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

API = "https://remoteok.com/api"
SITE = "https://remoteok.com"
DEFAULT_TAGS = ["dev", "engineer", "junior"]

_ANYWHERE_RE = re.compile(
    r"\b(anywhere|worldwide|world ?wide|global|any location)\b", re.IGNORECASE
)
_REGION_SPLIT_RE = re.compile(r"\s*(?:,|/|;|\||\bor\b|\band\b)\s*", re.IGNORECASE)
# RemoteOK appends an anti-bot line to most descriptions; it is noise for the AI stages.
_ANTIBOT_RE = re.compile(r"Please mention the word \*\*[A-Z]+\*\*.*", re.IGNORECASE | re.DOTALL)


def region_country(region: str | None) -> str | None:
    """ISO2 only when the "who can apply" text names exactly one country (see remotive)."""
    if not region or _ANYWHERE_RE.search(region):
        return None
    codes = {guess_country(part) for part in _REGION_SPLIT_RE.split(region) if part.strip()}
    codes.discard(None)
    return codes.pop() if len(codes) == 1 else None


def salary_text(low: Any, high: Any) -> str | None:
    """RemoteOK salaries are USD/year integers."""

    def num(value: Any) -> str | None:
        try:
            amount = float(value)
        except (TypeError, ValueError):
            return None
        return f"{int(amount):,}" if amount > 0 else None

    low_s, high_s = num(low), num(high)
    if not low_s and not high_s:
        return None
    span = f"{low_s} - {high_s}" if low_s and high_s else (low_s or high_s)
    return f"{span} USD"


def matches_tags(rec: dict[str, Any], terms: list[str]) -> bool:
    """Permissive OR filter: any term appearing in a tag or in the title keeps the posting."""
    if not terms:
        return True
    haystack = " ".join(
        [str(rec.get("position") or "")]
        + [t for t in (rec.get("tags") or []) if isinstance(t, str)]
    ).lower()
    return any(term.lower() in haystack for term in terms if term)


def parse_record(rec: dict[str, Any]) -> Job | None:
    if not isinstance(rec, dict):
        return None
    job_id = rec.get("id")
    title = rec.get("position") or rec.get("title")
    if not job_id or not title:
        return None  # element 0 is the legal notice, not a job
    url = str(rec.get("url") or rec.get("apply_url") or "")
    if url.startswith("/"):
        url = SITE + url
    if not url:
        slug = rec.get("slug") or job_id
        url = f"{SITE}/remote-jobs/{slug}"
    region = rec.get("location") or None
    description = strip_html(rec.get("description"))
    if description:
        description = _ANTIBOT_RE.sub("", description).strip() or None
    return Job(
        source="remoteok",
        source_id=str(job_id),
        url=url,
        title=str(title),
        company=rec.get("company"),
        description=description,
        location_raw=region,
        country=region_country(region),
        remote="remote",
        remote_region=region,
        tags=[t for t in (rec.get("tags") or []) if isinstance(t, str)],
        salary_text=salary_text(rec.get("salary_min"), rec.get("salary_max")),
        posted_at=parse_date(rec.get("date")) or parse_date(rec.get("epoch")),
        raw=rec,
    )


class RemoteOK:
    name = "remoteok"
    description = "remoteok.com public JSON API (remote-only tech jobs, worldwide)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        terms = [str(t) for t in (ctx.opt("tags", DEFAULT_TAGS) or []) if t]
        payload = ctx.http.get_json(API)
        records = payload if isinstance(payload, list) else (payload or {}).get("jobs") or []
        wanted = [r for r in records if isinstance(r, dict) and matches_tags(r, terms)]
        seen: set[str] = set()
        emitted = 0
        for job in safe_records(wanted, parse_record, self.name):
            if job.source_id in seen:
                continue
            seen.add(job.source_id)
            yield job
            emitted += 1
            if ctx.limit and emitted >= ctx.limit:
                return


register(RemoteOK())
