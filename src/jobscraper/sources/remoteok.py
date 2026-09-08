"""remoteok.com — remote board with a single free JSON endpoint.

GET https://remoteok.com/api → a JSON *array* whose first element is an API/legal notice
object (``{"legal": ..., "last_updated": ...}``); the job objects follow. Anything without
both ``id`` and ``position`` is skipped. No pagination, no auth: one request per run.

What the feed actually serves (checked 2026-09-07 against the live response):

* it is **not** a tech-only board any more — the ~100 live postings are mostly scraped
  hotel/retail/trades ads (Kitchen Technician, Carpenter, Gardener);
* ``tags`` are auto-generated and unreliable: that Carpenter arrives tagged
  ``["sys admin", "infosec", …, "engineer"]``. They are still exported on ``Job.tags`` for
  the AI stages, but the client-side ``tags`` option matches the **position title** only —
  matching the tags too kept 37 of 100 postings, nearly all of them non-software;
* ``location`` is the posting's own city with an empty country half ("Budapest, "), not the
  "who may apply" region the field used to hold, so it goes to ``Job.location_raw`` /
  ``Job.country`` and only worldwide/anywhere wording is kept as ``Job.remote_region``;
* ``position`` and ``company`` are HTML-escaped ("St. Regis Hotels &amp; Resorts");
* ``salary_min``/``salary_max`` are USD integers and are 0/0 on ~95% of postings;
* ``date`` (ISO 8601) and ``epoch`` carry the same timestamp.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from html import unescape
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
_TRAILING_SEP_RE = re.compile(r"^[\s,;/|]+|[\s,;/|]+$")
# RemoteOK appends an anti-bot line to most descriptions; it is noise for the AI stages.
_ANTIBOT_RE = re.compile(r"Please mention the word \*\*[A-Z]+\*\*.*", re.IGNORECASE | re.DOTALL)


def clean_location(value: Any) -> str | None:
    """Drop the dangling separator RemoteOK leaves behind: "Budapest, " → "Budapest"."""
    if not isinstance(value, str):
        return None
    return _TRAILING_SEP_RE.sub("", value) or None


def text_field(value: Any) -> str | None:
    """Feed strings are HTML-escaped; unescape before they reach the report and the prompts."""
    return unescape(value) if isinstance(value, str) and value.strip() else None


def salary_text(low: Any, high: Any) -> str | None:
    """RemoteOK salaries are USD/year integers; 0 means "not published"."""

    def num(value: Any) -> str | None:
        try:
            amount = float(value)
        except (TypeError, ValueError):
            return None
        return f"{int(amount):,}" if amount > 0 else None

    low_s, high_s = num(low), num(high)
    if not low_s and not high_s:
        return None
    if low_s and high_s and low_s != high_s:
        return f"{low_s} - {high_s} USD"
    return f"{low_s or high_s} USD"


def matches_tags(rec: dict[str, Any], terms: list[str]) -> bool:
    """Permissive OR filter over the *title*; the feed's own tags are too noisy to match on."""
    if not terms:
        return True
    title = str(rec.get("position") or rec.get("title") or "").lower()
    return any(term.lower() in title for term in terms if term)


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
    location = clean_location(rec.get("location"))
    # Only "Anywhere"/"Worldwide" wording is a statement about who may apply; a city is not.
    region = location if location and _ANYWHERE_RE.search(location) else None
    description = strip_html(rec.get("description"))
    if description:
        description = _ANTIBOT_RE.sub("", description).strip() or None
    return Job(
        source="remoteok",
        source_id=str(job_id),
        url=url,
        title=text_field(str(title)) or str(title),
        company=text_field(rec.get("company")),
        description=description,
        location_raw=location,
        country=None if region else guess_country(location),
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
