"""nofluffjobs.com — salary-transparent IT board (PL + CEE + a few EN/NL/HU regions).

Search: POST https://nofluffjobs.com/api/search/posting
        ?pageTo=N&pageSize=20&salaryCurrency=EUR&salaryPeriod=month&region=<region>
        &language=en-GB&sort=newest
        header ``Content-Type: application/infiniteSearch+json`` (accepted as-is on
        2026-09-07; the ``application/json`` retry below is a guard, not a needed step),
        body ``{"criteriaSearch": {"seniority": [...]}, "pageSize": 20, "withSalaryMatch": true}``
        → ``{"postings": [...], "totalCount": N, "totalPages": N, "exactMatchesPages": N}``
Detail: GET https://nofluffjobs.com/api/posting/{id} → requirements + description blocks.

Posting (observed 2026-09-07; ``tests/fixtures/nofluffjobs.json`` is a trimmed real page):
    ``id`` (slug, may carry the diacritic city: ``…-Kraków``), ``url`` (lower-case slug used
    for the public link), ``title``, ``name`` (company), ``reference`` (short posting code,
    always present), ``category`` (always) and ``technology`` (only ~1 posting in 4),
    ``seniority`` as ``["Junior"]``, ``salary`` ``{from, to, currency,
    type: b2b|permanent|zlecenie|intern, period: "Month", disclosedAt}``,
    ``posted`` (always) and ``renewed`` (often absent) as epoch **milliseconds**, and
    ``location`` ``{"places": [...], "fullyRemote": bool}``. A place is
    ``{"country": {"code", "name"}, "city", …}`` where ``code`` is 2-letter (``"pl"``) or
    3-letter (``"POL"``); a fully-remote posting prepends a country-less pseudo-place
    ``{"city": "Remote"}``, and province-wide entries (``provinceOnly``) carry no city.
    The top-level ``fullyRemote`` is always false — ``location.fullyRemote`` is the real flag.

``pageSize`` counts postings, not records: one posting is repeated once **per place**, each
copy carrying its own ``id`` (``…-Remote``, ``…-Warszawa``, ``…-silesian``, …), the same
``reference`` and the same full ``places`` list rotated so its own place comes first. The
2026-09-07 ``pageSize=20`` page held 133 records for 20 postings, so ``fetch`` dedupes on
``reference`` — which also collapses the per-region views of one catalogue.

Detail (``tests/fixtures/nofluffjobs_detail.json``): the prose lives in
    ``details.description`` and ``requirements.description`` (HTML), the bullet list in
    ``specs.dailyTasks`` (plain strings), and the skills in ``requirements.musts``/``nices``
    as ``[{"value": …, "type": …}]``. There is no ``specifics`` block. The endpoint takes the
    posting ``id`` as-is, diacritics and all.

Language requirements are *structured*, not prose: ``requirements.languages`` holds
    ``[{"type": "MUST"|"NICE", "code": "pl", "level": "C2"|"NATIVE"|null}]``. 36 of the 124 live
    postings on 2026-09-07 never wrote "Polish" anywhere in their text, so
    ``build_description`` renders the field into a leading "Required languages: …" /
    "Nice-to-have languages: …" block. The wording is load-bearing: it is what
    ``jobscraper.filters.language.find_language_requirements`` keys on, and the block goes
    first so it survives truncation downstream.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from functools import partial
from typing import Any

from jobscraper.http import SourceHTTPError, _raise_for_status, strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

SEARCH_API = "https://nofluffjobs.com/api/search/posting"
POSTING_API = "https://nofluffjobs.com/api/posting/{id}"
JOB_URL = "https://nofluffjobs.com/{region}/job/{slug}"
INFINITE_SEARCH_CT = "application/infiniteSearch+json"
PAGE_SIZE = 20

# ISO-639-1 codes seen in ``requirements.languages`` → the English name the rule filter knows.
ISO_LANGUAGE_NAMES = {
    "pl": "Polish", "en": "English", "de": "German", "fr": "French", "es": "Spanish",
    "it": "Italian", "nl": "Dutch", "cs": "Czech", "sk": "Slovak", "uk": "Ukrainian",
    "ru": "Russian", "pt": "Portuguese", "sv": "Swedish", "no": "Norwegian",
    "da": "Danish", "fi": "Finnish", "hu": "Hungarian", "ro": "Romanian",
}


def _texts(value: Any) -> list[str]:
    """Flatten NFJ's mixed shapes (str / {"value": ...} / nested lists) into strings."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        for key in ("value", "name", "description", "label"):
            if value.get(key):
                return _texts(value[key])
        return []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(_texts(item))
        return out
    return []


def _amount(value: Any) -> str | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return f"{int(value):,}"
    return str(value).strip() or None


def salary_text(salary: Any) -> str | None:
    """``{"from": 2084, "to": 3242, "currency": "EUR", "type": "permanent"}`` → readable text."""
    if not isinstance(salary, dict):
        return None
    low, high = _amount(salary.get("from")), _amount(salary.get("to"))
    if not low and not high:
        return None
    span = f"{low} - {high}" if low and high and low != high else (low or high)
    currency = salary.get("currency")
    if isinstance(currency, str) and currency.strip():
        span = f"{span} {currency.strip().upper()}"
    kind = salary.get("type")
    if isinstance(kind, str) and kind.strip():
        span = f"{span} ({kind.strip()})"
    return span


def _place_country(place: dict[str, Any]) -> str | None:
    country = place.get("country") if isinstance(place.get("country"), dict) else {}
    code = str(country.get("code") or "").strip()
    if len(code) == 2 and code.isalpha():
        return code.upper()
    # Some regions answer with 3-letter codes or none at all — fall back to text.
    return guess_country(country.get("name"), place.get("city"), code or None)


def parse_posting(rec: dict[str, Any], *, region: str) -> Job | None:
    if not isinstance(rec, dict):
        return None
    posting_id = rec.get("id")
    title = rec.get("title")
    if not posting_id or not title:
        return None
    slug = str(rec.get("url") or posting_id).strip("/")
    location = rec.get("location") if isinstance(rec.get("location"), dict) else {}
    places = [p for p in (location.get("places") or []) if isinstance(p, dict)]
    # A fully-remote posting gets a pseudo-place ``{"city": "Remote"}`` in front of its real
    # offices, and province-wide entries carry no city at all.
    named = [str(p.get("city")).strip() for p in places if p.get("city")]
    cities = [c for c in named if c.casefold() != "remote"]
    fully_remote = bool(location.get("fullyRemote") or rec.get("fullyRemote") or len(cities) < len(named))
    parts = (["Remote"] if fully_remote else []) + cities
    location_raw = ", ".join(dict.fromkeys(parts)) or None
    country = next((c for c in (_place_country(p) for p in places) if c), None)
    seniority = [s for s in _texts(rec.get("seniority")) if s]
    tags = [t for t in (rec.get("technology"), rec.get("category")) if isinstance(t, str) and t.strip()]
    return Job(
        source="nofluffjobs",
        source_id=str(posting_id),
        url=JOB_URL.format(region=region, slug=slug),
        title=str(title),
        company=rec.get("name") or None,
        location_raw=location_raw,
        country=country,
        city=cities[0] if cities else None,
        remote="remote" if fully_remote else guess_remote(location_raw, str(title)),
        seniority_raw=", ".join(seniority) or None,
        salary_text=salary_text(rec.get("salary")),
        tags=tags,
        posted_at=parse_date(rec.get("posted") or rec.get("renewed")),
        raw=rec,
    )


def _language_name(entry: dict[str, Any]) -> str | None:
    """``{"code": "pl", "level": "C2"}`` → ``"Polish (C2)"``; unknown codes stay as ``"ZZ"``."""
    code = str(entry.get("code") or "").strip()
    if not code:
        return None
    name = ISO_LANGUAGE_NAMES.get(code.lower(), code.upper())
    level = entry.get("level")
    level = str(level).strip() if isinstance(level, str) else ""
    if not level:
        return name
    return f"{name} ({'native' if level.upper() == 'NATIVE' else level})"


def language_lines(holder: Any) -> list[str]:
    """Render ``requirements.languages`` as sentences the language rule filter can read.

    The "Required" / "Nice-to-have" wording is what ``filters.language`` classifies on, so it
    is deliberate — do not reword it without a matching change there.
    """
    entries = holder.get("languages") if isinstance(holder, dict) else None
    if not isinstance(entries, list):
        return []
    musts: list[str] = []
    nices: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = _language_name(entry)
        if not name:
            continue
        bucket = nices if str(entry.get("type") or "").strip().upper() == "NICE" else musts
        bucket.append(name)
    lines = []
    if musts:
        lines.append("Required languages: " + ", ".join(dict.fromkeys(musts)) + ".")
    if nices:
        lines.append("Nice-to-have languages: " + ", ".join(dict.fromkeys(nices)) + ".")
    return lines


def build_description(detail: Any) -> str | None:
    """Assemble a description out of whichever detail blocks this posting carries."""
    if not isinstance(detail, dict):
        return None

    def block(key: str) -> dict[str, Any]:
        value = detail.get(key)
        return value if isinstance(value, dict) else {}

    specs, details, requirements = block("specs"), block("details"), block("requirements")
    # First: the structured language requirements are the one block that must survive truncation.
    parts: list[str] = language_lines(requirements) or language_lines(detail)
    for holder in (details, requirements, specs, detail):
        parts.extend(_texts(holder.get("description")))
    tasks = _texts(specs.get("dailyTasks")) or _texts(detail.get("dailyTasks"))
    if tasks:
        parts.append("Daily tasks:\n" + "\n".join(f"- {t}" for t in tasks))
    musts = _texts(requirements.get("musts")) or _texts(detail.get("musts"))
    if musts:
        parts.append("Must have: " + ", ".join(musts))
    nices = _texts(requirements.get("nices")) or _texts(detail.get("nices"))
    if nices:
        parts.append("Nice to have: " + ", ".join(nices))
    text = "\n\n".join(dict.fromkeys(p for p in parts if p))
    return strip_html(text)


def hydrate(ctx: SourceContext, job: Job) -> None:
    """Add the description from the posting endpoint; a failed detail never drops a job."""
    try:
        detail = ctx.http.get_json(POSTING_API.format(id=job.source_id))
    except Exception as exc:
        log.warning("nofluffjobs: detail %s failed: %s", job.source_id, exc)
        return
    if not isinstance(detail, dict):
        return
    job.description = build_description(detail)
    job.raw = {**job.raw, "detail": detail}


class NoFluffJobs:
    name = "nofluffjobs"
    description = "nofluffjobs.com search API (Poland/CEE IT board, salaries published)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        regions = [str(r).strip() for r in (ctx.opt("regions", ["pl", "en"]) or []) if str(r).strip()]
        seniority = [str(s).strip() for s in (ctx.opt("seniority", ["trainee", "junior"]) or []) if str(s).strip()]
        max_pages = int(ctx.opt("max_pages", 5))
        fetch_details = bool(ctx.opt("fetch_details", True))
        max_details = int(ctx.opt("max_details", 200))
        body = {
            "criteriaSearch": {"seniority": seniority},
            "pageSize": PAGE_SIZE,
            "withSalaryMatch": True,
        }
        content_type = INFINITE_SEARCH_CT
        seen: set[str] = set()
        yielded = 0
        details = 0

        for region in regions:
            for page in range(1, max_pages + 1):
                params = {
                    "pageTo": page,
                    "pageSize": PAGE_SIZE,
                    "salaryCurrency": "EUR",
                    "salaryPeriod": "month",
                    "region": region,
                    "language": "en-GB",
                    "sort": "newest",
                }
                resp = ctx.http.post(
                    SEARCH_API,
                    params=params,
                    json_body=body,
                    headers={"Content-Type": content_type, "Accept": "application/json"},
                )
                if resp.status_code in (400, 415) and content_type != "application/json":
                    log.info(
                        "nofluffjobs: %s rejected (HTTP %s), retrying as application/json",
                        content_type,
                        resp.status_code,
                    )
                    content_type = "application/json"
                    resp = ctx.http.post(
                        SEARCH_API,
                        params=params,
                        json_body=body,
                        headers={"Content-Type": content_type, "Accept": "application/json"},
                    )
                _raise_for_status(resp)
                payload = resp.json()
                if not isinstance(payload, dict):
                    raise SourceHTTPError(f"nofluffjobs: expected an object, got {type(payload).__name__}")
                postings = payload.get("postings") or []
                if not postings:
                    break
                for job in safe_records(postings, partial(parse_posting, region=region), self.name):
                    # One posting comes back once per place, each copy with its own ``id``;
                    # ``reference`` is the same for every copy (and across regions).
                    key = str(job.raw.get("reference") or job.source_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    if fetch_details and details < max_details:
                        details += 1
                        hydrate(ctx, job)
                    yield job
                    yielded += 1
                    if ctx.limit and yielded >= ctx.limit:
                        return
                total_pages = payload.get("totalPages")
                if isinstance(total_pages, int) and page >= total_pages:
                    break


register(NoFluffJobs())
