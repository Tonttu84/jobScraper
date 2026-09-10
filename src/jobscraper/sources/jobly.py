"""jobly.fi — Finnish job board (Drupal), behind Cloudflare.

**Confidence: low.** jobly.fi could not be reached from the sandbox this adapter was written
in (no egress to job boards), so nothing here was observed live: the URLs, the markup and the
JSON-LD shape are what the site's public pages are *documented and commonly built* like, not
what was measured. Verify with ``jobscraper probe jobly`` before enabling it — expect the
selectors to need a nudge, not a rewrite. Everything is therefore written defensively:

* **Discovery** is nothing but "collect the links that point at ``/tyopaikka/``" on
  ``https://www.jobly.fi/tyopaikat?search=<query>&page=<n>`` (Drupal's pager is 0-based).
  No card structure is assumed, because that is the part that changes; the pages are walked
  until one yields no link that was not already known.
* **Detail** prefers a ``<script type="application/ld+json">`` ``JobPosting`` block, which is
  what Google for Jobs requires and therefore the most stable thing on a board's page. Every
  field of it is optional here, and a page without JSON-LD still yields a job from the ``<h1>``,
  the first element whose class mentions a company, and the page text.

The site answers a plain HTTP client with a Cloudflare interstitial, so the default option is
``mode: browser`` — pages go straight through :meth:`SourceContext.page`'s headless browser
(``uv run playwright install chromium``). ``mode: auto`` tries plain HTTP first and falls back;
``mode: http`` never starts a browser (useful to see what the plain client really gets).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

from jobscraper.http import strip_html
from jobscraper.models import Job
from jobscraper.sources._common import COUNTRY_NAMES, guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

BASE = "https://www.jobly.fi/"
LISTING_URL = "https://www.jobly.fi/tyopaikat"
JOB_PATH = "/tyopaikka/"
HOST_SUFFIX = "jobly.fi"

DEFAULT_QUERIES = ["software developer", "ohjelmistokehittäjä", "junior developer"]
DEFAULT_MODE = "browser"


# ------------------------------------------------------------------------------ listing


def listing_url(query: str, page: int) -> str:
    return f"{LISTING_URL}?{urlencode({'search': query, 'page': page})}"


def parse_listing_html(html: str | None) -> list[str]:
    """Search page → the posting URLs on it, absolute and deduped, in document order."""
    if not html:
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        if JOB_PATH not in href:
            continue
        url = urljoin(BASE, href).split("#")[0].split("?")[0]
        parts = urlsplit(url)
        if not parts.netloc.endswith(HOST_SUFFIX) or JOB_PATH not in parts.path:
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


# ------------------------------------------------------------------------------ detail


def _text(node: Any) -> str | None:
    if node is None:
        return None
    return node.get_text(" ", strip=True) or None


def _iter_ld_nodes(soup: Any) -> Iterable[dict[str, Any]]:
    """Every JSON-LD object on the page, flattening ``@graph`` and top-level lists."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        stack: list[Any] = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                graph = node.get("@graph")
                if graph is not None:
                    stack.extend(graph if isinstance(graph, list) else [graph])
                yield node


def _has_type(node: dict[str, Any], wanted: str) -> bool:
    types = node.get("@type")
    if isinstance(types, str):
        return types.lower() == wanted.lower()
    if isinstance(types, list):
        return any(isinstance(t, str) and t.lower() == wanted.lower() for t in types)
    return False


def _first_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return {}


def _clean_str(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) and value.strip() else None


def _number(value: Any) -> str | None:
    """"3200.0" → "3200"; anything unparseable is dropped rather than guessed at."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    return _clean_str(value)


def salary_text(base_salary: Any) -> str | None:
    """schema.org ``baseSalary`` → "3200 - 4000 EUR / MONTH"-ish free text."""
    if isinstance(base_salary, str):
        return _clean_str(base_salary)
    node = _first_dict(base_salary)
    if not node:
        return None
    value = node.get("value")
    unit = None
    if isinstance(value, dict):
        low, high = _number(value.get("minValue")), _number(value.get("maxValue"))
        exact = _number(value.get("value"))
        unit = _clean_str(value.get("unitText"))
        amount = f"{low} - {high}" if low and high and low != high else (exact or low or high)
    else:
        amount = _number(value)
    if not amount:
        return None
    currency = _clean_str(node.get("currency")) or _clean_str(node.get("salaryCurrency"))
    return " ".join(p for p in (amount, currency) if p) + (f" / {unit}" if unit else "")


def _company_from_markup(soup: Any) -> str | None:
    """First element whose class mentions a company/organization (Drupal field names vary)."""
    for element in soup.find_all(attrs={"class": True}):
        classes = " ".join(element.get("class") or []).lower()
        if "company" in classes or "organization" in classes or "employer" in classes:
            text = _text(element)
            if text:
                return text
    return None


def _location_from_markup(soup: Any) -> str | None:
    for element in soup.find_all(attrs={"class": True}):
        classes = " ".join(element.get("class") or []).lower()
        if "location" in classes or "sijainti" in classes:
            text = _text(element)
            if text:
                return text
    return None


def _description_from_markup(soup: Any) -> str | None:
    for selector in (".field--name-body", "article", "main", "body"):
        node = soup.select_one(selector)
        text = strip_html(str(node)) if node is not None else None
        if text and text.strip():
            return text.strip()
    return None


def _city(location_raw: str | None) -> str | None:
    """First component that is a town rather than the country ("Espoo, Suomi" → "Espoo")."""
    if not location_raw:
        return None
    for part in location_raw.split(","):
        part = part.strip()
        if part and part.lower() not in COUNTRY_NAMES:
            return part
    return None


def parse_detail(html: str | None, url: str) -> Job | None:
    """Posting page → Job. JSON-LD when the page has it, the markup when it doesn't."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    posting = next((n for n in _iter_ld_nodes(soup) if _has_type(n, "JobPosting")), None)
    data = posting or {}

    title = _clean_str(data.get("title")) or _text(soup.select_one("h1"))
    if not title:
        return None

    organization = _first_dict(data.get("hiringOrganization"))
    company = _clean_str(organization.get("name")) or _company_from_markup(soup)

    place = _first_dict(data.get("jobLocation"))
    address = _first_dict(place.get("address"))
    country_name = address.get("addressCountry")
    if isinstance(country_name, dict):
        country_name = country_name.get("name")
    locality = _clean_str(address.get("addressLocality")) or _clean_str(address.get("addressRegion"))
    location_raw = locality or _location_from_markup(soup)

    description = strip_html(data.get("description")) or _description_from_markup(soup)

    employment = data.get("employmentType")
    if isinstance(employment, list):
        employment = ", ".join(e for e in employment if isinstance(e, str)) or None

    location_type = data.get("jobLocationType")
    telecommute = isinstance(location_type, str) and location_type.strip().upper() == "TELECOMMUTE"

    return Job(
        source="jobly",
        source_id=urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or url,
        url=url,
        title=title,
        company=company,
        description=description,
        location_raw=location_raw,
        country=guess_country(_clean_str(country_name), location_raw) or "FI",
        city=_city(location_raw),
        remote=guess_remote(title, location_raw, description, flag=True if telecommute else None),
        employment_type=_clean_str(employment),
        salary_text=salary_text(data.get("baseSalary")),
        posted_at=parse_date(data.get("datePosted")),
        raw={"url": url, "jsonld": posting},
    )


# ------------------------------------------------------------------------------ source


class Jobly:
    name = "jobly"
    description = "jobly.fi — Finnish job board behind Cloudflare (headless browser + JSON-LD)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        queries = ctx.opt("queries", DEFAULT_QUERIES) or DEFAULT_QUERIES
        if isinstance(queries, str):
            queries = [queries]
        mode = str(ctx.opt("mode", DEFAULT_MODE))
        urls = self._discover(ctx, [str(q) for q in queries], mode)

        def convert(url: str) -> Job | None:
            return parse_detail(ctx.page(url, mode=mode), url)

        for emitted, job in enumerate(safe_records(urls, convert, self.name), start=1):
            yield job
            if ctx.limit and emitted >= ctx.limit:
                return

    def _discover(self, ctx: SourceContext, queries: list[str], mode: str) -> list[str]:
        """Posting URLs from the search pages, deduped across queries and pages."""
        max_pages = int(ctx.opt("max_pages", 3))
        cap = int(ctx.opt("max_details", 100))
        if ctx.limit:
            cap = min(cap, ctx.limit)  # one page fetched per job; don't fetch what we'd drop
        wait_for = ctx.opt("wait_for") or None

        urls: list[str] = []
        seen: set[str] = set()
        for query in queries:
            for page in range(max_pages):
                html = ctx.page(listing_url(query, page), wait_for=wait_for, mode=mode)
                found = [u for u in parse_listing_html(html) if u not in seen]
                if not found:
                    if page == 0 and not urls:
                        log.warning("%s: no /tyopaikka/ links for %r (markup change?)", self.name, query)
                    break
                for url in found:
                    seen.add(url)
                    urls.append(url)
                if len(urls) >= cap:
                    return urls[:cap]
        return urls


register(Jobly())
