"""LinkedIn job search via ``python-jobspy``.

``jobspy.scrape_jobs`` drives LinkedIn's public *guest* endpoints (no login, no API key).
That endpoint is rate-limited and unstable: it 429s quickly, so this adapter runs one
query×location at a time with a delay between calls, and asks for descriptions only when
``fetch_descriptions`` is on (each description is an extra request per job).

Options (``config/sources.yaml``)::

    queries: ["junior software developer", ...]
    locations: ["Finland", "Estonia", "Berlin", ...]
    results_per_query: 40      # jobspy `results_wanted`
    hours_old: 168             # only postings newer than this
    fetch_descriptions: false  # LinkedIn description fetch is slow + rate-limited
    delay: 3                   # seconds between scrape_jobs calls

One failing query is logged and skipped (a 429 on "Berlin" shouldn't lose "Helsinki");
if *every* call fails the adapter raises, because that means we're blocked outright.

Because descriptions are off during the scrape, most LinkedIn rows are title-only. Later
stages that only look at a handful of jobs (the ranking stage) top them up one at a time with
``fetch_description``, which reads the same public guest page jobspy would have read.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from functools import partial
from typing import Any

import pandas as pd
from jobspy import scrape_jobs

from jobscraper.http import Http, strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

NAME = "linkedin"
# Indirection so tests (and a future async runner) can replace the pacing.
sleep = time.sleep

RAW_KEYS = (
    "id", "site", "job_url", "job_url_direct", "company", "company_url", "location",
    "date_posted", "job_type", "is_remote", "min_amount", "max_amount", "currency",
    "interval", "emails",
)


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


def parse_row(rec: dict[str, Any], queried_location: str | None = None) -> Job | None:
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
        country=guess_country(location) or guess_country(queried_location),
        city=city,
        remote=guess_remote(
            location, title, flag=remote_flag if isinstance(remote_flag, bool) else None
        ),
        employment_type=text(rec, "job_type"),
        salary_text=salary_text(rec),
        posted_at=parse_date(cell(rec, "date_posted")),
        raw=raw_of(rec, query_location=queried_location),
    )


class LinkedIn:
    name = NAME
    description = "LinkedIn public job search (via python-jobspy)"
    # Keyword searches, not a listing: a posting missing from today's results says nothing.
    complete_listing = False

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        queries = [str(q).strip() for q in (ctx.opt("queries") or []) if str(q).strip()]
        locations = [str(x).strip() for x in (ctx.opt("locations") or []) if str(x).strip()]
        if not queries or not locations:
            raise ValueError("linkedin: options 'queries' and 'locations' must both be set")
        results_wanted = int(ctx.opt("results_per_query", 40))
        hours_old = int(ctx.opt("hours_old", 168))
        fetch_descriptions = bool(ctx.opt("fetch_descriptions", False))
        delay = float(ctx.opt("delay", 3))

        seen_urls: set[str] = set()
        attempts = failures = seen = 0
        for query in queries:
            for location in locations:
                if attempts:
                    sleep(delay)
                attempts += 1
                try:
                    df = scrape_jobs(
                        site_name=["linkedin"],
                        search_term=query,
                        location=location,
                        results_wanted=results_wanted,
                        hours_old=hours_old,
                        linkedin_fetch_description=fetch_descriptions,
                        description_format="markdown",
                        verbose=0,
                    )
                except Exception as exc:
                    failures += 1
                    log.warning("%s: %r @ %r failed: %s", self.name, query, location, exc)
                    continue
                if df is None or df.empty:
                    log.info("%s: %r @ %r -> 0 rows", self.name, query, location)
                    continue
                records = df.to_dict(orient="records")
                log.info("%s: %r @ %r -> %d rows", self.name, query, location, len(records))
                convert = partial(parse_row, queried_location=location)
                for job in safe_records(records, convert, self.name):
                    if job.url in seen_urls:
                        continue
                    seen_urls.add(job.url)
                    yield job
                    seen += 1
                    if ctx.limit and seen >= ctx.limit:
                        return
        if attempts and failures == attempts:
            raise RuntimeError(f"linkedin: all {attempts} jobspy calls failed (rate-limited?)")


register(LinkedIn())


# ------------------------------------------------------------- description hydration
# The guest job page (no login) carries the posting body in `div.show-more-less-html__markup`;
# jobspy reads the same element. Everything here is best-effort: a rate-limited or walled-off
# page must cost the caller a log line, never an exception.

JOB_VIEW_RE = re.compile(r"linkedin\.com/jobs/view/(?:[\w%-]*-)?(\d+)", re.IGNORECASE)
GUEST_URL = "https://www.linkedin.com/jobs/view/{}"
DESCRIPTION_SELECTORS = (
    "div.show-more-less-html__markup",
    "div.description__text",
    "section.description",
)
# LinkedIn answers a walled-off request with a 200 + an authwall/login page rather than a 4xx.
WALLED_PATHS = ("/authwall", "/login", "/uas/login", "/checkpoint", "/signup")


def guest_url(url: str | None) -> str | None:
    """Any ``linkedin.com/jobs/view/<id>…`` URL → the canonical guest page, else None.

    Country subdomains (``nl.linkedin.com``), slugged paths and ``?refId=…`` tracking
    parameters all point at the same posting; the bare form is what the guest page wants.
    """
    match = JOB_VIEW_RE.search(url or "")
    return GUEST_URL.format(match.group(1)) if match else None


def parse_description(html: str | None) -> str | None:
    """Guest job page HTML → plain-text description, or None if the page has no posting body."""
    if not html:
        return None
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for selector in DESCRIPTION_SELECTORS:
        node = soup.select_one(selector)
        if node is None:
            continue
        for tag in node.find_all(["button", "script", "style", "icon", "form"]):
            tag.decompose()
        lines = [line.strip() for line in (strip_html(str(node)) or "").splitlines()]
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        if text:
            return text
    return None


def fetch_description(http: Http, url: str) -> str | None:
    """Read one posting's description from the public guest page. Never raises."""
    target = guest_url(url)
    if not target:
        return None
    try:
        resp = http.get(target)
    except Exception as exc:  # a missing description is never worth failing a run over
        log.warning("%s: description fetch for %s failed: %s", NAME, target, exc)
        return None
    if resp.status_code >= 400:
        log.warning("%s: description fetch for %s -> HTTP %s", NAME, target, resp.status_code)
        return None
    landed = str(resp.url).lower()
    if any(path in landed for path in WALLED_PATHS):
        log.warning("%s: description fetch for %s hit a login wall (%s)", NAME, target, resp.url)
        return None
    text = parse_description(resp.text)
    if text is None:
        log.info("%s: no description block on %s", NAME, target)
    return text
