"""cvkeskus.ee — Estonia's largest private job board. Two-step HTML, no API.

Discovery, either of:

* search — ``GET https://www.cvkeskus.ee/toopakkumised`` with the parameters of the site's own
  search form (``#top_search_form``, observed live 2026-09-07). Three things matter:

  - ``op=search`` (a hidden input) switches the page from "newest ads on the whole board" to
    real search results. Without it every other parameter is silently ignored and you get the
    newest ~25 ads of any category — that is what made this adapter return kindergarten
    teachers for ``keyword=developer``.
  - ``search[categories][]=8`` is *Infotehnoloogia*, and the category is what actually buys IT
    ads: 472 open ones vs 3721 on the whole board. A keyword alone is not enough even with
    ``op=search``: the board matches Estonian stems, so ``developer`` also hits the "lapse
    *arengut*" (child's development) boilerplate of every kindergarten ad — 168 hits, sorted
    by date, teachers on top. Keywords are therefore optional and only sharpen an
    already-categorised search (``8`` + ``developer`` → 15 hits, all real IT ads).
  - ``start=<n>`` pages, 30 cards per page (the site's own pager links look the same).

  An ad can be filed under several categories at once (``occupationalCategory`` in its JSON-LD
  lists them), so a shop assistant's ad whose employer also ticked *Infotehnoloogia* does come
  through; deciding what is really relevant is the filter/prefilter stage's job, not this one's.

  Cards are ``article[data-component="jobad"]`` with ``a.jobad-url``, an ``h2`` title,
  ``.job-company``, ``span.location`` (a hybrid ad reads "Tallinn / Kaugtöö") and
  ``.salary-block``. Every card carries a ``data-event`` blob describing a *different* ad —
  the site's own bug; nothing here reads it.

* sitemap — ``option use_sitemap: true`` reads
  ``https://www.cvkeskus.ee/sitemap-listings-information-technology-en.xml`` and takes the
  ``<loc>`` entries (the whole English IT category, ~240 URLs — cap with ``max_details``).

Detail — each posting page carries a schema.org ``@graph``. Almost everything on the
``JobPosting`` node is an ``@id`` reference into that same graph, and the references are not
always exact: ``jobLocation`` points at a ``Place`` whose ``address`` points at
``#/schema/PostalAddress/listing-N`` while the node itself is published as
``#/schema/Address/listing-N``. References are therefore resolved by id first and by the
trailing path segment plus expected ``@type`` second — without that the city was always None.
The address carries the town in ``addressRegion`` (``addressLocality`` is normally absent)
plus ``addressCountry``.

``hiringOrganization`` references an ``Organization`` node that is *not* in the graph, so the
company name comes from the search card, or, in sitemap mode, from the employer link
(``a[href*="-toopakkumised-<org id>"]``) on the posting page.
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
from jobscraper.sources._common import COUNTRY_NAMES, guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

BASE = "https://www.cvkeskus.ee/"
SEARCH_URL = "https://www.cvkeskus.ee/toopakkumised"
SITEMAP_URL = "https://www.cvkeskus.ee/sitemap-listings-information-technology-en.xml"

IT_CATEGORY = 8  # "Infotehnoloogia" in the site's own category checkboxes
DEFAULT_CATEGORIES = [IT_CATEGORY]
PAGE_SIZE = 30  # cards per search page; the "start" parameter is an offset, not a page number

_ID_RE = re.compile(r"(\d{4,})\s*$")
# The board's own location labels for a posting you can do from home.
_REMOTE_LABEL_RE = re.compile(r"kaugt[öo]{2}|kodukontor|remote", re.I)


def _text(node: Any) -> str | None:
    if node is None:
        return None
    text = node.get_text(" ", strip=True)
    return text or None


def _pick(card: Any, *selectors: str) -> str | None:
    for sel in selectors:
        for found in card.select(sel):
            text = _text(found)
            if text:
                return text
    return None


def _card_location(card: Any) -> str | None:
    """Join the card's location spans ("Tallinn" + "Kaugtöö" → "Tallinn / Kaugtöö")."""
    parts: list[str] = []
    for span in card.select("span.location"):
        text = _text(span)
        if text and text not in parts:
            parts.append(text)
    return " / ".join(parts) or _pick(card, ".job-location")


def parse_search_html(html: str | None) -> list[dict[str, Any]]:
    """Search page → one raw card dict per posting (``url`` plus whatever else is on the card)."""
    if not html:
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    cards = (
        soup.select('article[data-component="jobad"]')
        or soup.select('[data-component="jobad"]')
        or soup.select(".job-offer-container")
    )
    records: list[dict[str, Any]] = []
    for card in cards:
        link = card.select_one("a.jobad-url[href]") or card.select_one('a[href*="-"]')
        href = link.get("href") if link is not None else None
        if not href:
            continue
        url = urljoin(BASE, str(href))
        title = _pick(card, "h2", "h3") or (_text(link) if link is not None else None)
        records.append(
            {
                "url": url,
                "title": title,
                "company": _pick(card, ".job-company", '[class*="company"]'),
                "location": _card_location(card),
                "salary": _pick(card, ".salary-block", '[class*="salary"]'),
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


class _Graph:
    """The page's JSON-LD nodes, indexed so ``@id`` references can be followed."""

    def __init__(self, nodes: Iterable[dict[str, Any]]) -> None:
        self.by_id: dict[str, dict[str, Any]] = {}
        self.by_tail: dict[str, list[dict[str, Any]]] = {}
        for node in nodes:
            node_id = node.get("@id")
            if not isinstance(node_id, str):
                continue
            self.by_id.setdefault(node_id, node)
            self.by_tail.setdefault(node_id.rstrip("/").rsplit("/", 1)[-1], []).append(node)

    def lookup(self, ref: str, wanted: str) -> dict[str, Any] | None:
        exact = self.by_id.get(ref)
        if exact is not None:
            return exact
        # cvkeskus publishes e.g. Place.address -> ".../PostalAddress/listing-N" while the node
        # itself is ".../Address/listing-N"; the trailing segment is the reliable part.
        for candidate in self.by_tail.get(ref.rstrip("/").rsplit("/", 1)[-1], []):
            if _has_type(candidate, wanted):
                return candidate
        return None

    def resolve(self, value: Any, wanted: str) -> dict[str, Any]:
        """An inline object, or the node its ``@id`` points at, or ``{}``."""
        node = _first_dict(value)
        ref = node.get("@id")
        if isinstance(ref, str):
            target = self.lookup(ref, wanted)
            if target is not None and target is not node:
                merged = dict(target)
                merged.update({k: v for k, v in node.items() if k != "@id"})
                return merged
        return node


def _source_id(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    match = _ID_RE.search(slug)
    return match.group(1) if match else (slug or url)


def _employer_from_page(soup: Any, org_ref: Any) -> str | None:
    """The employer link (``/wise-toopakkumised-179285``) the referenced Organization is missing."""
    if not isinstance(org_ref, str):
        return None
    tail = org_ref.rstrip("/").rsplit("/", 1)[-1]
    if not tail.isdigit():
        return None
    for link in soup.select(f'a[href*="-toopakkumised-{tail}"]'):
        text = _text(link)
        if text:
            return text
    return None


def _city(location_raw: str | None) -> str | None:
    """First real town in "Tallinn / Kaugtöö" or "Tartu, Estonia".

    None when the label says nothing about a town: a work-from-home tag ("Kaugtöö",
    "Kodukontori võimalusega") or the country itself, which the board writes as "Eesti".
    """
    if not location_raw:
        return None
    for part in re.split(r"[/,]", location_raw):
        part = part.strip()
        if not part or _REMOTE_LABEL_RE.search(part) or part.lower() in COUNTRY_NAMES:
            continue
        return part
    return None


def parse_detail(html: str | None, url: str, card: dict[str, Any] | None = None) -> Job | None:
    """Posting page → Job, from JSON-LD when present, else from the search card."""
    from bs4 import BeautifulSoup

    card = card or {}
    soup = BeautifulSoup(html or "", "lxml")
    nodes = list(_iter_ld_nodes(soup))
    graph = _Graph(nodes)
    posting = next((n for n in nodes if _has_type(n, "JobPosting")), {})

    title = posting.get("title") or card.get("title")
    if not isinstance(title, str) or not title.strip():
        return None

    hiring = _first_dict(posting.get("hiringOrganization"))
    company = graph.resolve(hiring, "Organization").get("name")
    if not isinstance(company, str) or not company.strip():
        company = card.get("company") or _employer_from_page(soup, hiring.get("@id"))

    place = graph.resolve(posting.get("jobLocation"), "Place")
    address = graph.resolve(place.get("address"), "PostalAddress")
    locality = address.get("addressLocality") or address.get("addressRegion")
    country_name = address.get("addressCountry")
    if isinstance(country_name, dict):
        country_name = country_name.get("name")
    location_raw = locality if isinstance(locality, str) and locality.strip() else card.get("location")
    location_raw = location_raw.strip() if isinstance(location_raw, str) else None

    loc_type = posting.get("jobLocationType")
    telecommute = isinstance(loc_type, str) and loc_type.strip().upper() == "TELECOMMUTE"
    labelled_remote = bool(location_raw and _REMOTE_LABEL_RE.search(location_raw))
    # Only a positive signal is trusted; silence says nothing about on-site work.
    remote_flag = True if telecommute or labelled_remote else None

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
        location_raw=location_raw,
        country=guess_country(country_name if isinstance(country_name, str) else None, location_raw)
        or "EE",
        city=_city(location_raw),
        remote=guess_remote(location_raw, title, flag=remote_flag),
        employment_type=employment if isinstance(employment, str) else None,
        salary_text=card.get("salary"),
        posted_at=parse_date(posting.get("datePosted")),
        raw={"card": card, "jsonld": posting or None, "valid_through": posting.get("validThrough")},
    )


class CvKeskus:
    name = "cvkeskus"
    description = "cvkeskus.ee — Estonian job board (IT category search or sitemap + JSON-LD detail)"

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

    def _search_params(self, categories: list[str], keyword: str | None, start: int) -> list[tuple[str, str]]:
        params: list[tuple[str, str]] = [("op", "search")]
        params += [("search[categories][]", cat) for cat in categories]
        if keyword:
            params.append(("search[keyword]", keyword))
        if start:
            params.append(("start", str(start)))
        return params

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

        categories = ctx.opt("categories", DEFAULT_CATEGORIES)
        if isinstance(categories, (str, int)):
            categories = [categories]
        categories = [str(c) for c in (categories or [])]
        keywords: Any = ctx.opt("keywords", []) or [None]
        if isinstance(keywords, str):
            keywords = [keywords]
        pages = max(1, -(-max_details // PAGE_SIZE))

        for keyword in keywords:
            for page in range(pages):
                params = self._search_params(categories, keyword, page * PAGE_SIZE)
                found = parse_search_html(ctx.http.get_text(SEARCH_URL, params=params))
                if not found:
                    if page == 0:
                        log.warning("%s: no job cards found for %r (markup change?)", self.name, keyword)
                    break
                before = len(cards)
                for card in found:
                    add(card)
                if len(cards) == before or len(cards) >= max_details:
                    break  # the page repeated what we already had, or we have enough
            if len(cards) >= max_details:
                break
        return cards


register(CvKeskus())
