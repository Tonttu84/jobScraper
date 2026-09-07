"""tyomarkkinatori.fi — Job Market Finland, the national portal run by KEHA-keskus.

Two same-origin JSON endpoints, both keyless (the SPA's own XHR)::

    POST /api/jobpostingfulltext/search/v2/search
         {"query": q, "filters": {}, "paging": {"pageNumber": n, "pageSize": 90},
          "sorting": "LATEST"}
         -> {"pageSize", "totalElements", "lastPage", "content": [item]}
    GET  /api/jobposting-new/v1/public/jobpostings/{id}
         -> {"languages", "descriptionsContentType", "metadata", "client", "recruiter",
             "position", "owner", "location", "application", "externalLinks"}

``pageNumber`` is zero-based and ``lastPage`` is the number of pages, not an index: a query
with 165 hits at pageSize 90 answers ``lastPage: 2`` and serves pages 0 and 1 (page 2 comes
back empty). Verified live 2026-09-07.

A search item carries: applicationPeriodEndDate, applicationUrl {values: {lang: url}},
continuityOfWork [code], created, createdSource, employer {businessId, name, reference,
ownerName {lang: text}, ownerOfficeName, ownerOfficeCode}, employerType, employmentRelationships,
id, industryCode, lastModified, lastModifiedSource, location {foreignCountry, countries
[{value, label}], regions, municipalities [{value, label, region}], address}, officialMunicipality,
ownerOfficeCode, ownerOfficeName, publishDate, sort, tags (empty in practice), title {lang: text}
and workTime. There is no posting body, so descriptions need the per-id detail call, whose
``position`` adds jobDescription, marketingDescription, title, occupations/skills (ESCO entries
with multilingual prefLabel), workLanguages, workTime, employmentRelationship, continuityOfWork,
wagePrinciple(+Info), drivingLicenses, permitCards and workTimeDetails.

The detail endpoint rate-limits: after a few hundred requests it answers 403. We therefore cap
detail fetches with ``max_details`` and latch off on the first 403 — the remaining jobs are still
yielded, just without a description, and a later run picks them up. The detail ``recruiter`` block
holds the contact person's name, e-mail and phone; it is dropped and never stored.

Code sets are documented in Job Market Finland's retrieval interface guide (noutorajapintaohje):
tyoAika 01/02 = full-time/part-time and tyonJatkuvuus 01/02/0201/0202 = until further
notice/fixed term/seasonal/summer work. ``employmentRelationships`` (01, 0101, 02, 03, 04) is not
in that guide, so it is kept as a raw code instead of being given an invented label.

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
MAX_TAGS = 25

WORK_TIME_CODES = {"01": "full_time", "02": "part_time"}
CONTINUITY_CODES = {
    "01": "permanent",  # continues until further notice
    "02": "fixed_term",
    "0201": "seasonal",
    "0202": "summer_job",
}


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


def _codes(value: Any) -> list[str]:
    """A code field is either a single code or a list of them."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]
    return []


def employment_type(work_time: Any, continuity: Any) -> str | None:
    """``workTime`` + ``continuityOfWork`` codes → 'full_time, permanent' and friends."""
    labels: list[str] = []
    for code in _codes(work_time):
        label = WORK_TIME_CODES.get(code)
        if label and label not in labels:
            labels.append(label)
    for code in _codes(continuity):
        label = CONTINUITY_CODES.get(code)
        if label and label not in labels:
            labels.append(label)
    return ", ".join(labels) or None


def _location_text(location: dict[str, Any]) -> tuple[str | None, list[str], str | None]:
    """→ (free-text location, municipality names, ISO2 of the work country if given)."""
    foreign = bool(location.get("foreignCountry"))
    names: list[str] = []
    for muni in location.get("municipalities") or []:
        if not isinstance(muni, dict):
            continue
        label, _ = pick_lang(muni.get("label"))
        if label:
            names.append(label)
    country_code: str | None = None
    country_label: str | None = None
    for country in location.get("countries") or []:
        if not isinstance(country, dict):
            continue
        value = country.get("value")
        if isinstance(value, str) and value.strip():
            country_code = value.strip().upper()
            country_label, _ = pick_lang(country.get("label"))
            break
    parts = list(names)
    # Name the country so the geo helpers resolve a posting with no municipality at all.
    if country_label:
        parts.append(country_label)
    elif not foreign:
        parts.append("Finland")
    location_raw = ", ".join(parts) or None
    if not country_code:
        country_code = guess_country(location_raw) if foreign else "FI"
    return location_raw, names, country_code


def _apply_url(value: Any) -> str | None:
    """``applicationUrl.values`` / ``application.url`` — a multilingual map of employer links."""
    if isinstance(value, dict) and isinstance(value.get("values"), dict):
        value = value["values"]
    url, _ = pick_lang(value)
    return url


def _label_tags(entries: Any) -> list[str]:
    """ESCO ``occupations``/``skills`` entries → their preferred labels."""
    tags: list[str] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        label, _ = pick_lang(entry.get("prefLabel"))
        if label:
            tags.append(label)
    return tags


def _merge_tags(*groups: Iterable[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for tag in group:
            if tag and tag not in merged:
                merged.append(tag)
    return merged[:MAX_TAGS]


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
    location_raw, municipalities, country = _location_text(location or {})
    deadline = rec.get("applicationPeriodEndDate")
    return Job(
        source="tyomarkkinatori",
        source_id=str(job_id),
        url=PUBLIC_URL.format(id=job_id, lang=lang),
        title=title,
        company=company,
        location_raw=location_raw,
        country=country,
        city=municipalities[0] if municipalities else None,
        remote=guess_remote(location_raw, title),
        employment_type=employment_type(rec.get("workTime"), rec.get("continuityOfWork")),
        tags=_merge_tags(t for t in rec.get("tags") or [] if isinstance(t, str)),
        posted_at=parse_date(rec.get("publishDate")),
        raw={
            **rec,
            "apply_url": _apply_url(rec.get("applicationUrl")),
            "application_deadline": deadline if isinstance(deadline, str) else None,
        },
    )


def apply_detail(job: Job, detail: dict[str, Any]) -> Job:
    """Enrich a list-derived job with its detail payload (sparse fields never blank a good one)."""
    detail = {k: v for k, v in detail.items() if k != "recruiter"}  # names/phones stay out
    position = detail.get("position") if isinstance(detail.get("position"), dict) else {}
    position = position or {}
    description, _ = pick_lang(position.get("jobDescription"))
    if not description:
        description, _ = pick_lang(position.get("marketingDescription"))
    if description:
        # Assignment skips the model's own cleaner, so trim here.
        job.description = (strip_html(description) or "").strip() or None
    title, _ = pick_lang(position.get("title"))
    if title:
        job.title = title
    owner = detail.get("owner") if isinstance(detail.get("owner"), dict) else {}
    company, _ = pick_lang((owner or {}).get("company"))
    if company:
        job.company = company
    kind = employment_type(position.get("workTime"), position.get("continuityOfWork"))
    if kind:
        job.employment_type = kind
    job.tags = _merge_tags(
        job.tags,
        _label_tags(position.get("occupations")),
        _label_tags(position.get("skills")),
        (f"lang:{code}" for code in _codes(position.get("workLanguages"))),
    )
    salary, _ = pick_lang(position.get("wagePrincipleInfo"))
    if salary:
        job.salary_text = salary
    application = detail.get("application") if isinstance(detail.get("application"), dict) else {}
    application = application or {}
    published = parse_date(application.get("published"))
    if published:
        job.posted_at = published
    expires = application.get("expires")
    job.raw = {
        **job.raw,
        "apply_url": job.raw.get("apply_url") or _apply_url(application.get("url")),
        "application_deadline": job.raw.get("application_deadline")
        or (expires if isinstance(expires, str) else None),
        "detail": detail,
    }
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
        """``lastPage`` counts the pages, and ``page`` is zero-based — so page 1 of 2 is the last."""
        last = payload.get("lastPage") if isinstance(payload, dict) else None
        if isinstance(last, bool):
            return last
        if isinstance(last, int):
            return page + 1 >= last
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
        except Exception as exc:
            log.warning("%s: detail %s failed (%s); ingesting list-only", self.name, job_id, exc)
            return None, False
        return (payload if isinstance(payload, dict) else None), False


register(Tyomarkkinatori())
