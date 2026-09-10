"""duunitori.fi — Finland's largest private job board (mostly Finnish-language postings).

Primary door is the undocumented but public DRF endpoint used by the site itself::

    GET https://duunitori.fi/api/v1/jobentries?search=<q>&search_also_descr=1&page=N
        Accept: application/json
    -> {"count": N, "next": url|null, "previous": url|null, "results": [...]}

A result has exactly nine keys (observed 2026-09-07, see ``tests/fixtures/duunitori.json``):
``slug``, ``heading``, ``company_name``, ``municipality_name``, ``descr``, ``date_posted``,
``export_image_url``, ``latitude``, ``longitude``. Notably absent: any id or URL (the posting
URL is built from the slug), and any salary, employment-type, tag or remote-work field.
Two consequences for the mapping:

* ``descr`` is **plain text with newlines**, not HTML — it still goes through
  :func:`~jobscraper.http.strip_html`, which is a no-op on it, so the HTML fallback below can
  share the same normalizer.
* ``municipality_name`` is usually a single city, but a country-wide posting says ``Finland``.
  That is not a city, so :func:`parse_record` leaves ``city`` empty for it.

Because there is no remote flag, ``remote`` is guessed from the location and title. The one
structured hint the payload does carry is the ``Remote status: ...`` footer that
Teamtailor-syndicated postings keep in ``descr``; :func:`_remote_from_descr` reads it and
``guess_remote`` stays the fallback.

Duunitori sits behind Cloudflare and the endpoint is not contractual: it may answer with
HTML (the DRF browsable API, or a challenge page) instead of JSON. When that happens we fall
back to parsing the server-rendered search page
``https://duunitori.fi/tyopaikat?haku=<q>&sivu=N`` with :func:`parse_search_html`, which
returns records in the same shape as the API so both paths share one normalizer.
A Cloudflare 403 raises (``jobscraper probe`` should report the source as broken).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from jobscraper.http import SourceHTTPError, _raise_for_status, strip_html
from jobscraper.models import Job
from jobscraper.sources._common import (
    COUNTRY_NAMES,
    guess_country,
    guess_remote,
    is_cloudflare_challenge,
    parse_date,
)
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

API = "https://duunitori.fi/api/v1/jobentries"
SEARCH_HTML = "https://duunitori.fi/tyopaikat"
JOB_URL = "https://duunitori.fi/tyopaikat/tyo/{slug}"
JSON_HEADERS = {"Accept": "application/json"}
HTML_HEADERS = {"Accept": "text/html,application/xhtml+xml"}

DEFAULT_QUERIES = ["software developer", "ohjelmistokehittäjä", "junior developer"]

# Text that means "this is a relative date, not a company name" in a search card.
_DATE_HINTS = ("sitten", "tänään", "eilen", "ago", "today", "yesterday", "päivä", "tunti")

# Footer that Teamtailor-syndicated postings keep in `descr`, e.g. "Remote status: Hybrid".
_REMOTE_STATUS_RE = re.compile(r"^[ \t]*remote status:[ \t]*(.+)$", re.I | re.M)


def _slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlsplit(str(url)).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or None


def _remote_from_descr(description: str | None) -> str | None:
    """Remote kind from a ``Remote status: ...`` footer, or ``None`` to keep guessing.

    The value is classified with :func:`guess_remote` rather than a hard-coded vocabulary,
    so a wording we haven't seen simply falls through to the location/title guess.
    """
    if not description:
        return None
    match = _REMOTE_STATUS_RE.search(description)
    if not match:
        return None
    kind = guess_remote(match.group(1).strip())
    return kind if kind != "unknown" else None


def _city(location: str | None) -> str | None:
    """First component of the location, unless it names a country ("Finland")."""
    if not location:
        return None
    city = str(location).split(",")[0].strip()
    if not city or city.lower() in COUNTRY_NAMES:
        return None
    return city


def parse_record(rec: dict[str, Any]) -> Job | None:
    """Normalize one ``results`` entry (API) or one search card (HTML fallback)."""
    slug = rec.get("slug") or _slug_from_url(rec.get("url"))
    heading = rec.get("heading") or rec.get("title")
    if not slug or not heading:
        return None
    location = rec.get("municipality_name") or rec.get("location") or None
    company = rec.get("company_name") or rec.get("company") or None
    description = strip_html(rec.get("descr") or rec.get("description"))
    return Job(
        source="duunitori",
        source_id=str(slug),
        url=rec.get("url") or JOB_URL.format(slug=slug),
        title=str(heading),
        company=str(company) if company else None,
        description=description,
        location_raw=str(location) if location else None,
        country=guess_country(location) or "FI",
        city=_city(location),
        remote=_remote_from_descr(description) or guess_remote(location, str(heading)),
        posted_at=parse_date(rec.get("date_posted") or rec.get("posted_date")),
        raw=rec,
    )


def _card_text(card: Any, *selectors: str) -> str | None:
    for sel in selectors:
        node = card.select_one(sel)
        if node is not None:
            text = node.get_text(" ", strip=True)
            if text:
                return text
    return None


def parse_search_html(html: str | None) -> list[dict[str, Any]]:
    """Fallback parser for the server-rendered search page.

    Returns raw records shaped like the API's ``results`` entries so they can go through
    :func:`parse_record` unchanged. The markup changes often, so every lookup is optional and
    several selector spellings are tried.
    """
    if not html:
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    cards = soup.select(".job-box") or soup.select('[class*="job-box"]')
    records: list[dict[str, Any]] = []
    for card in cards:
        link = (
            card.select_one("a.job-box__hover-title")
            or card.select_one('a[class*="hover-title"]')
            or card.select_one('a[href*="/tyopaikat/tyo/"]')
            or card.select_one("a[href]")
        )
        href = link.get("href") if link is not None else None
        slug = _slug_from_url(href)
        if not slug:
            continue
        title = _card_text(
            card,
            "a.job-box__hover-title",
            '[class*="hover-title"]',
            ".job-box__title",
            "h3",
            "h2",
        )
        if not title and link is not None:
            title = link.get_text(" ", strip=True) or None
        if not title:
            continue
        company = _card_text(card, ".job-box__company", '[class*="job-box__company"]')
        posted = _card_text(card, ".job-box__job-posted", '[class*="date"]')
        # `.job-box__job-posted` carries either the employer or a relative date, depending on
        # the card variant — take it as the company only when it doesn't read like a date.
        if not company and posted and not any(h in posted.lower() for h in _DATE_HINTS):
            company, posted = posted, None
        location = _card_text(
            card, ".job-box__job-location", '[class*="location"]', ".job-box__municipality"
        )
        url = str(href)
        if not url.startswith("http"):
            url = JOB_URL.format(slug=slug)
        records.append(
            {
                "slug": slug,
                "url": url,
                "heading": title,
                "company_name": company,
                "municipality_name": location,
                "date_posted": posted,
            }
        )
    return records


class Duunitori:
    name = "duunitori"
    description = "duunitori.fi search API (Finland), with a server-rendered HTML fallback"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        queries = ctx.opt("queries", DEFAULT_QUERIES) or DEFAULT_QUERIES
        if isinstance(queries, str):
            queries = [queries]
        max_pages = int(ctx.opt("max_pages", 5))
        seen_slugs: set[str] = set()
        emitted = 0

        for query in queries:
            for page in range(1, max_pages + 1):
                records, has_next = self._page(ctx, str(query), page)
                if not records:
                    break
                for job in safe_records(records, parse_record, self.name):
                    if job.source_id in seen_slugs:
                        continue
                    seen_slugs.add(job.source_id)
                    yield job
                    emitted += 1
                    if ctx.limit and emitted >= ctx.limit:
                        return
                if not has_next:
                    break

    # -------------------------------------------------------------------------- one page
    def _page(self, ctx: SourceContext, query: str, page: int) -> tuple[list[dict[str, Any]], bool]:
        params = {"search": query, "search_also_descr": 1, "page": page}
        resp = ctx.http.get(API, params=params, headers=JSON_HEADERS)
        _raise_for_status(resp)
        if is_cloudflare_challenge(resp.text):
            raise SourceHTTPError(
                "duunitori: bot challenge instead of JSON (this source needs a headless browser)",
                resp.status_code,
            )
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            results = payload.get("results")
            if not isinstance(results, list):
                results = []
            return results, bool(payload.get("next"))

        # The API answered with HTML — parse what came back, else ask the search page.
        log.info("duunitori: API returned non-JSON for %r page %s; using HTML fallback", query, page)
        records = parse_search_html(resp.text)
        if not records:
            html = ctx.http.get_text(
                SEARCH_HTML, params={"haku": query, "sivu": page}, headers=HTML_HEADERS
            )
            if is_cloudflare_challenge(html):
                raise SourceHTTPError("duunitori: bot challenge on the search page", 403)
            records = parse_search_html(html)
        return records, bool(records)


register(Duunitori())
