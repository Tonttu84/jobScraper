"""tyomarkkinatori.fi — Job Market Finland, the national portal run by KEHA-keskus.

Two same-origin JSON endpoints, both keyless (the SPA's own XHR)::

    POST /api/jobpostingfulltext/search/v2/search
         {"query": q, "filters": {}, "paging": {"pageNumber": n, "pageSize": 90},
          "sorting": "LATEST"}
         -> {"content": [{id, title, employer, location, publishDate}], "lastPage": ...}
    GET  /api/jobposting-new/v1/public/jobpostings/{id}
         -> {"position": {"title", "jobDescription"}, "owner": {"company"},
             "application": {"published"}}

The list item has no posting body, so descriptions need the per-id detail call. That endpoint
rate-limits: after a few hundred requests it answers 403. We therefore cap detail fetches with
``max_details`` and latch off on the first 403 — the remaining jobs are still yielded, just
without a description, and a later run picks them up.

Texts are Finnish public-sector multilingual values: ``{"fi": ..., "sv": ..., "en": ...}`` with
whichever languages the employer filled in. They may also arrive as a plain string, or be
missing entirely.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from jobscraper.http import SourceHTTPError, strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

SEARCH_URL = "https://tyomarkkinatori.fi/api/jobpostingfulltext/search/v2/search"
DETAIL_URL = "https://tyomarkkinatori.fi/api/jobposting-new/v1/public/jobpostings/{id}"
PUBLIC_URL = "https://tyomarkkinatori.fi/henkiloasiakkaat/avoimet-tyopaikat/{id}/{lang}"

PAGE_SIZE = 90
DEFAULT_QUERIES = ["software developer"]
# English first (we read English), then Finland's two official languages, then German.
LANG_ORDER = ("en", "fi", "sv", "de")


def pick_lang(value: Any) -> tuple[str | None, str]:
    """Best available text from a multilingual map → ``(text, language code)``.

    Tolerates a plain string (assumed Finnish), a map missing every preferred key, and None.
    """
    if isinstance(value, str):
        return (value.strip() or None), "fi"
    if isinstance(value, dict):
        for code in LANG_ORDER:
            text = value.get(code)
            if isinstance(text, str) and text.strip():
                return text.strip(), code
        for code, text in value.items():
            if isinstance(text, str) and text.strip():
                return text.strip(), str(code) or "fi"
    return None, "fi"


def _location_text(location: dict[str, Any]) -> tuple[str | None, list[str], bool]:
    """→ (free-text location, municipality names, is-foreign)."""
    foreign = bool(location.get("foreignCountry"))
    names: list[str] = []
    for muni in location.get("municipalities") or []:
        if not isinstance(muni, dict):
            continue
        label, _ = pick_lang(muni.get("label"))
        if label:
            names.append(label)
    parts = list(names)
    if not foreign:
        # Append the country so the geo helpers resolve a domestic posting with no municipality.
        parts.append("Finland")
    return (", ".join(parts) or None), names, foreign


def parse_record(rec: dict[str, Any]) -> Job | None:
    job_id = rec.get("id")
    title, lang = pick_lang(rec.get("title"))
    if not job_id or not title:
        return None
    employer = rec.get("employer") or {}
    company, _ = pick_lang(employer.get("ownerName") if isinstance(employer, dict) else None)
    if not company and isinstance(employer, dict):
        office = employer.get("ownerOfficeName")
        company = office.strip() if isinstance(office, str) and office.strip() else None
    location = rec.get("location") if isinstance(rec.get("location"), dict) else {}
    location_raw, municipalities, foreign = _location_text(location or {})
    return Job(
        source="tyomarkkinatori",
        source_id=str(job_id),
        url=PUBLIC_URL.format(id=job_id, lang=lang),
        title=title,
        company=company,
        location_raw=location_raw,
        country=(guess_country(location_raw) if foreign else "FI"),
        city=municipalities[0] if municipalities else None,
        remote=guess_remote(location_raw, title),
        posted_at=parse_date(rec.get("publishDate")),
        raw=rec,
    )


def apply_detail(job: Job, detail: dict[str, Any]) -> Job:
    """Enrich a list-derived job with its detail payload (sparse fields never blank a good one)."""
    position = detail.get("position") if isinstance(detail.get("position"), dict) else {}
    description, _ = pick_lang((position or {}).get("jobDescription"))
    if description:
        job.description = strip_html(description)
    title, _ = pick_lang((position or {}).get("title"))
    if title:
        job.title = title
    owner = detail.get("owner") if isinstance(detail.get("owner"), dict) else {}
    company, _ = pick_lang((owner or {}).get("company"))
    if company:
        job.company = company
    application = detail.get("application") if isinstance(detail.get("application"), dict) else {}
    published = parse_date((application or {}).get("published"))
    if published:
        job.posted_at = published
    job.raw = {**job.raw, "detail": detail}
    return job


class Tyomarkkinatori:
    name = "tyomarkkinatori"
    description = "tyomarkkinatori.fi — Job Market Finland, the national public portal"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        queries = ctx.opt("queries", DEFAULT_QUERIES) or DEFAULT_QUERIES
        if isinstance(queries, str):
            queries = [queries]
        max_pages = int(ctx.opt("max_pages", 5))
        fetch_details = bool(ctx.opt("fetch_details", True))
        max_details = int(ctx.opt("max_details", 150))

        seen: set[str] = set()
        emitted = 0
        details_done = 0
        rate_limited = False

        for query in queries:
            for page in range(max_pages):
                payload = ctx.http.post_json(
                    SEARCH_URL,
                    json_body={
                        "query": str(query),
                        "filters": {},
                        "paging": {"pageNumber": page, "pageSize": PAGE_SIZE},
                        "sorting": "LATEST",
                    },
                )
                content = payload.get("content") if isinstance(payload, dict) else None
                if not isinstance(content, list) or not content:
                    break
                for job in safe_records(content, parse_record, self.name):
                    if job.source_id in seen:
                        continue
                    seen.add(job.source_id)
                    if fetch_details and not rate_limited and details_done < max_details:
                        detail, rate_limited = self._detail(ctx, job.source_id)
                        if detail is not None:
                            details_done += 1
                            job = apply_detail(job, detail)
                    yield job
                    emitted += 1
                    if ctx.limit and emitted >= ctx.limit:
                        return
                if self._is_last_page(payload, page):
                    break

    # ------------------------------------------------------------------------- helpers
    @staticmethod
    def _is_last_page(payload: Any, page: int) -> bool:
        last = payload.get("lastPage") if isinstance(payload, dict) else None
        if isinstance(last, bool):
            return last
        if isinstance(last, int):
            return page >= last
        return False

    def _detail(self, ctx: SourceContext, job_id: str) -> tuple[dict[str, Any] | None, bool]:
        """→ (detail payload or None, rate-limited flag). A 403 latches detail fetching off."""
        try:
            payload = ctx.http.get_json(DETAIL_URL.format(id=job_id))
        except SourceHTTPError as exc:
            if exc.status == 403:
                log.warning("%s: detail endpoint rate-limited; continuing without descriptions", self.name)
                return None, True
            log.warning("%s: detail %s failed (%s); ingesting list-only", self.name, job_id, exc)
            return None, False
        except Exception as exc:  # noqa: BLE001 - one bad detail must not abort the fetch
            log.warning("%s: detail %s failed (%s); ingesting list-only", self.name, job_id, exc)
            return None, False
        return (payload if isinstance(payload, dict) else None), False


register(Tyomarkkinatori())
