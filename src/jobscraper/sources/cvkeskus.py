"""cvkeskus.ee — Estonia's largest private job board. Two-step HTML, no API.

Discovery, either of:

* search  — ``GET https://www.cvkeskus.ee/toopakkumised?keyword=<kw>``; cards are
  ``article[data-component="jobad"]`` with ``a.jobad-url`` and an ``h2`` title.
* sitemap — ``option use_sitemap: true`` reads
  ``https://www.cvkeskus.ee/sitemap-listings-information-technology-en.xml`` and takes the
  ``<loc>`` entries (the whole English IT category, a few hundred URLs — cap with
  ``max_details``).

Detail — each posting page carries a schema.org ``@graph`` with a ``JobPosting`` node
(``title``, ``datePosted``, ``validThrough``, ``description`` as HTML, ``hiringOrganization``
either inline or as an ``@id`` reference to an ``Organization`` node in the same graph, and
``jobLocation.address.addressLocality``). When the JSON-LD is missing we fall back to whatever
the search card gave us, so a markup change degrades instead of failing.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urljoin

from jobscraper.http import strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

BASE = "https://www.cvkeskus.ee/"
SEARCH_URL = "https://www.cvkeskus.ee/toopakkumised"
SITEMAP_URL = "https://www.cvkeskus.ee/sitemap-listings-information-technology-en.xml"

DEFAULT_KEYWORDS = ["developer"]
_ID_RE = re.compile(r"(\d{4,})\s*$")


def _text(node: Any) -> str | None:
    if node is None:
        return None
    text = node.get_text(" ", strip=True)
    return text or None


def _pick(card: Any, *selectors: str) -> str | None:
    for sel in selectors:
        found = _text(card.select_one(sel))
        if found:
            return found
    return None


def parse_search_html(html: str | None) -> list[dict[str, Any]]:
    """Search page → one raw card dict per posting (``url`` plus whatever else is on the card)."""
    if not html:
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    cards = (
        soup.select('article[data-component="jobad"]')
        or soup.select(".job-offer-container")
        or soup.select(".jobad-container")
    )
    records: list[dict[str, Any]] = []
    for card in cards:
        link = card.select_one("a.jobad-url") or card.select_one('a[href*="-"]')
        href = link.get("href") if link is not None else None
        if not href:
            continue
        url = urljoin(BASE, str(href))
        title = _pick(card, "h2", "h3", "a.jobad-url") or (_text(link) if link is not None else None)
        records.append(
            {
                "url": url,
                "title": title,
                "company": _pick(card, ".job-company", '[class*="company"]'),
                "location": _pick(card, '[class*="location"]', ".job-location"),
                "salary": _pick(card, '[class*="salary"]', ".job-salary"),
            }
        )
    return records


def parse_sitemap(xml: str | None) -> list[str]:
    """Sitemap XML → the ``<loc>`` URLs, in document order."""
    if not xml:
        return []
    return [
        loc.strip()
        for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml, flags=re.DOTALL | re.IGNORECASE)
        if loc.strip().startswith("http")
    ]


def _iter_ld_nodes(html: str) -> Iterable[dict[str, Any]]:
    """Every JSON-LD object on the page, flattening ``@graph`` and top-level lists."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
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


def _source_id(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    match = _ID_RE.search(slug)
    return match.group(1) if match else (slug or url)


def parse_detail(html: str | None, url: str, card: dict[str, Any] | None = None) -> Job | None:
    """Posting page → Job, from JSON-LD when present, else from the search card."""
    card = card or {}
    posting: dict[str, Any] = {}
    organizations: dict[str, dict[str, Any]] = {}
    for node in _iter_ld_nodes(html or ""):
        if not posting and _has_type(node, "JobPosting"):
            posting = node
        elif _has_type(node, "Organization"):
            node_id = node.get("@id")
            if isinstance(node_id, str):
                organizations[node_id] = node

    title = posting.get("title") or card.get("title")
    if not isinstance(title, str) or not title.strip():
        return None

    hiring = _first_dict(posting.get("hiringOrganization"))
    company = hiring.get("name")
    if not isinstance(company, str) or not company.strip():
        referenced = organizations.get(hiring.get("@id")) if isinstance(hiring.get("@id"), str) else None
        company = (referenced or {}).get("name")
    if not isinstance(company, str) or not company.strip():
        company = card.get("company")

    address = _first_dict(_first_dict(posting.get("jobLocation")).get("address"))
    locality = address.get("addressLocality")
    country_name = address.get("addressCountry")
    if isinstance(country_name, dict):
        country_name = country_name.get("name")
    location_raw = locality if isinstance(locality, str) and locality.strip() else card.get("location")

    loc_type = posting.get("jobLocationType")
    # Only a positive TELECOMMUTE is trusted; a missing type says nothing about on-site work.
    remote_flag = True if isinstance(loc_type, str) and loc_type.strip().upper() == "TELECOMMUTE" else None

    employment = posting.get("employmentType")
    if isinstance(employment, list):
        employment = ", ".join(e for e in employment if isinstance(e, str)) or None

    return Job(
        source="cvkeskus",
        source_id=_source_id(url),
        url=url,
        title=title.strip(),
        company=company.strip() if isinstance(company, str) and company.strip() else None,
        description=strip_html(posting.get("description")),
        location_raw=location_raw if isinstance(location_raw, str) else None,
        country=guess_country(country_name if isinstance(country_name, str) else None, location_raw)
        or "EE",
        city=location_raw.split(",")[0].strip() if isinstance(location_raw, str) else None,
        remote=guess_remote(location_raw if isinstance(location_raw, str) else None, title,
                            flag=remote_flag),
        employment_type=employment if isinstance(employment, str) else None,
        salary_text=card.get("salary"),
        posted_at=parse_date(posting.get("datePosted")),
        raw={"card": card, "jsonld": posting or None, "valid_through": posting.get("validThrough")},
    )


class CvKeskus:
    name = "cvkeskus"
    description = "cvkeskus.ee — Estonian job board (search or sitemap discovery + JSON-LD detail)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        max_details = int(ctx.opt("max_details", 60))
        cards = self._discover(ctx, max_details)

        def convert(card: dict[str, Any]) -> Job | None:
            html = ctx.http.get_text(card["url"])
            return parse_detail(html, card["url"], card)

        for emitted, job in enumerate(safe_records(cards, convert, self.name), start=1):
            yield job
            if ctx.limit and emitted >= ctx.limit:
                return

    def _discover(self, ctx: SourceContext, max_details: int) -> list[dict[str, Any]]:
        """Collect candidate postings (deduped by URL) from the sitemap or the search pages."""
        cards: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(card: dict[str, Any]) -> None:
            url = card.get("url")
            if not url or url in seen or len(cards) >= max_details:
                return
            seen.add(url)
            cards.append(card)

        if bool(ctx.opt("use_sitemap", False)):
            sitemap = str(ctx.opt("sitemap_url", SITEMAP_URL))
            for url in parse_sitemap(ctx.http.get_text(sitemap)):
                add({"url": url})
            return cards

        keywords = ctx.opt("keywords", DEFAULT_KEYWORDS) or DEFAULT_KEYWORDS
        if isinstance(keywords, str):
            keywords = [keywords]
        for keyword in keywords:
            html = ctx.http.get_text(SEARCH_URL, params={"keyword": str(keyword)})
            found = parse_search_html(html)
            if not found:
                log.warning("%s: no job cards found for %r (markup change?)", self.name, keyword)
            for card in found:
                add(card)
            if len(cards) >= max_details:
                break
        return cards


register(CvKeskus())
