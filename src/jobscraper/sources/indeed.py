"""Indeed job search via ``python-jobspy``.

``jobspy.scrape_jobs`` talks to Indeed's mobile GraphQL API, which is per-country: the
``country_indeed`` string picks the domain (``fi.indeed.com``, ``ee.indeed.com``, …), so
this adapter loops queries × countries instead of queries × free-text locations.

Options (``config/sources.yaml``)::

    queries: ["junior software developer", ...]
    countries: [finland, estonia, germany, "united arab emirates"]  # jobspy country names
    results_per_query: 40      # jobspy `results_wanted`
    hours_old: 168
    delay: 3                   # seconds between scrape_jobs calls
    use_country_as_location: false   # pass the country name as `location` too

Rows carry no ISO country, so the country is guessed from the row's location text and
falls back to the queried country. One failing (query, country) pair is logged and
skipped; if *every* call fails the adapter raises.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from functools import partial
from typing import Any

import pandas as pd
from jobspy import scrape_jobs

from jobscraper.models import Job
from jobscraper.sources._common import guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

NAME = "indeed"
# Indirection so tests (and a future async runner) can replace the pacing.
sleep = time.sleep

# jobspy country string → ISO2, for the names whose ISO code isn't obvious from the text
# (everything else falls through to ``guess_country``, which knows the country names).
COUNTRY_ISO: dict[str, str] = {
    "finland": "FI", "estonia": "EE", "sweden": "SE", "norway": "NO", "denmark": "DK",
    "germany": "DE", "netherlands": "NL", "belgium": "BE", "austria": "AT",
    "switzerland": "CH", "france": "FR", "spain": "ES", "portugal": "PT", "italy": "IT",
    "ireland": "IE", "poland": "PL", "czech republic": "CZ", "czechia": "CZ",
    "slovakia": "SK", "hungary": "HU", "romania": "RO", "bulgaria": "BG", "greece": "GR",
    "croatia": "HR", "slovenia": "SI", "lithuania": "LT", "latvia": "LV", "malta": "MT",
    "cyprus": "CY", "luxembourg": "LU", "ukraine": "UA", "türkiye": "TR", "turkey": "TR",
    "united arab emirates": "AE", "uk": "GB", "united kingdom": "GB", "usa": "US",
    "us": "US", "united states": "US", "canada": "CA", "india": "IN",
}

RAW_KEYS = (
    "id", "site", "job_url", "job_url_direct", "company", "company_url", "location",
    "date_posted", "job_type", "is_remote", "min_amount", "max_amount", "currency",
    "interval", "emails",
)


def country_iso(country: str | None) -> str | None:
    if not country:
        return None
    return COUNTRY_ISO.get(country.strip().lower()) or guess_country(country)


def cell(rec: dict[str, Any], key: str) -> Any:
    """DataFrame cell → value or None (pandas uses NaN/NaT for 'missing')."""
    value = rec.get(key)
    if value is None:
        return None
    if isinstance(value, (list, tuple, set, dict)):
        return value or None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return value
    if isinstance(value, str):
        return value.strip() or None
    return value


def text(rec: dict[str, Any], key: str) -> str | None:
    value = cell(rec, key)
    return str(value).strip() or None if value is not None else None


def salary_text(rec: dict[str, Any]) -> str | None:
    low, high = cell(rec, "min_amount"), cell(rec, "max_amount")
    if low is None and high is None:
        return None
    amounts = [f"{float(a):,.0f}".replace(",", " ") for a in (low, high) if a is not None]
    parts = ["–".join(amounts)]
    currency = text(rec, "currency")
    interval = text(rec, "interval")
    if currency:
        parts.append(currency)
    if interval:
        parts.append(interval)
    return " ".join(parts)


def raw_of(rec: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """JSON-safe subset of the row (Timestamps/NaN would not survive the SQLite store)."""
    raw: dict[str, Any] = {}
    for key in RAW_KEYS:
        value = cell(rec, key)
        keep = value is None or isinstance(value, (str, int, float, bool))
        raw[key] = value if keep else str(value)
    raw.update(extra)
    return raw


def parse_row(rec: dict[str, Any], queried_country: str | None = None) -> Job | None:
    url = text(rec, "job_url_direct") or text(rec, "job_url")
    title = text(rec, "title")
    if not url or not title:
        return None
    location = text(rec, "location")
    remote_flag = cell(rec, "is_remote")
    city = location.split(",")[0].strip() if location else None
    return Job(
        source=NAME,
        source_id=text(rec, "id") or text(rec, "job_url") or url,
        url=url,
        title=title,
        company=text(rec, "company"),
        description=text(rec, "description"),
        location_raw=location,
        country=guess_country(location) or country_iso(queried_country),
        city=city,
        remote=guess_remote(
            location, title, flag=remote_flag if isinstance(remote_flag, bool) else None
        ),
        employment_type=text(rec, "job_type"),
        salary_text=salary_text(rec),
        posted_at=parse_date(cell(rec, "date_posted")),
        raw=raw_of(rec, query_country=queried_country),
    )


class Indeed:
    name = NAME
    description = "Indeed per-country job search (via python-jobspy)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        queries = [str(q).strip() for q in (ctx.opt("queries") or []) if str(q).strip()]
        countries = [str(c).strip() for c in (ctx.opt("countries") or []) if str(c).strip()]
        if not queries or not countries:
            raise ValueError("indeed: options 'queries' and 'countries' must both be set")
        results_wanted = int(ctx.opt("results_per_query", 40))
        hours_old = int(ctx.opt("hours_old", 168))
        delay = float(ctx.opt("delay", 3))
        as_location = bool(ctx.opt("use_country_as_location", False))

        seen_urls: set[str] = set()
        attempts = failures = seen = 0
        for query in queries:
            for country in countries:
                if attempts:
                    sleep(delay)
                attempts += 1
                try:
                    df = scrape_jobs(
                        site_name=["indeed"],
                        search_term=query,
                        location=country if as_location else None,
                        country_indeed=country,
                        results_wanted=results_wanted,
                        hours_old=hours_old,
                        description_format="markdown",
                        verbose=0,
                    )
                except Exception as exc:
                    failures += 1
                    log.warning("%s: %r in %r failed: %s", self.name, query, country, exc)
                    continue
                if df is None or df.empty:
                    log.info("%s: %r in %r -> 0 rows", self.name, query, country)
                    continue
                records = df.to_dict(orient="records")
                log.info("%s: %r in %r -> %d rows", self.name, query, country, len(records))
                convert = partial(parse_row, queried_country=country)
                for job in safe_records(records, convert, self.name):
                    if job.url in seen_urls:
                        continue
                    seen_urls.add(job.url)
                    yield job
                    seen += 1
                    if ctx.limit and seen >= ctx.limit:
                        return
        if attempts and failures == attempts:
            raise RuntimeError(f"indeed: all {attempts} jobspy calls failed (blocked?)")


register(Indeed())
