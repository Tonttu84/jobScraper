"""Teamtailor career sites (multi-tenant; heavily used by Nordic tech companies).

Every public Teamtailor career site serves two anonymous feeds:

* ``GET https://{slug}.teamtailor.com/jobs.json`` — JSON Feed 1.1. Preferred: the
  ``_jobposting`` extension carries schema.org fields the RSS feed lacks — a structured
  postal address (city + ISO country) and the hiring organization's real name.
  Observed ``_jobposting`` keys, and nothing else: ``@context``, ``@type``, ``title``,
  ``description``, ``identifier``, ``datePosted``, ``hiringOrganization``, ``jobLocation``.
  In particular there is **no** ``employmentType`` and **no** ``jobLocationType``, so a
  fully remote role is indistinguishable from an on-site one here.
* ``GET https://{slug}.teamtailor.com/jobs.rss?per_page=200`` — RSS with the
  ``https://teamtailor.com/locations`` namespace (``tt:city``, ``tt:country``,
  ``tt:name``, ``tt:department``, ``tt:role``) plus ``<remoteStatus>`` (``none`` /
  ``hybrid`` / ``fully``), the only remote marker Teamtailor publishes. Used as a
  fallback when jobs.json is missing (404) or serves something that isn't JSON (some
  tenants only expose the RSS feed).

Both feeds list every open job in one request — no pagination. The item id differs
between them: jobs.json uses the job's UUID (also the RSS ``<guid>``), the RSS parser
uses the numeric id from the job URL.

One tenant is one endpoint: a dead/renamed slug (404, DNS failure, 403) is logged and
skipped so the rest of the tenants still produce jobs; only an *all tenants failed* run
raises, because that means the feed shape or our access changed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any
from xml.etree import ElementTree as ET

from jobscraper.http import strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

NAME = "teamtailor"
JSON_URL = "https://{slug}.teamtailor.com/jobs.json"
RSS_URL = "https://{slug}.teamtailor.com/jobs.rss"
RSS_PER_PAGE = 200

TT_NS = {"tt": "https://teamtailor.com/locations"}
# <remoteStatus> values seen in the wild; "hybrid" is handled separately in parse_rss_item.
_REMOTE_FLAG = {"fully": True, "none": False}
# Public job URLs look like https://{slug}.teamtailor.com/jobs/1234567-some-title
_URL_ID_RE = re.compile(r"/jobs/(\d+)")
# Teamtailor tenant slugs are DNS labels; refuse anything else so a config typo can't
# make us request an arbitrary host.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)


class TeamtailorTenantError(RuntimeError):
    """A single tenant's feeds could not be read (bad slug, 404, non-feed body)."""


# --------------------------------------------------------------------------- JSON Feed


def _address_parts(posting: dict[str, Any]) -> tuple[str | None, str | None]:
    """First ``jobLocation`` entry → (city, ISO2 country)."""
    locations = posting.get("jobLocation")
    if isinstance(locations, dict):
        locations = [locations]
    for loc in locations or []:
        if not isinstance(loc, dict):
            continue
        address = loc.get("address")
        if not isinstance(address, dict):
            continue
        city = address.get("addressLocality") or None
        country = address.get("addressCountry") or None
        if isinstance(country, dict):  # schema.org allows a nested Country object
            country = country.get("name") or country.get("identifier")
        if city or country:
            return (str(city) if city else None, str(country) if country else None)
    return None, None


def parse_json_item(item: dict[str, Any], slug: str) -> Job | None:
    """One JSON Feed item → Job. Returns None for items without an id/title/url."""
    item_id = item.get("id") or item.get("url")
    title = item.get("title")
    url = item.get("url") or (str(item_id) if str(item_id or "").startswith("http") else None)
    if not item_id or not title or not url:
        return None

    posting = item.get("_jobposting")
    posting = posting if isinstance(posting, dict) else {}
    city, country = _address_parts(posting)
    org = posting.get("hiringOrganization")
    company = (org or {}).get("name") if isinstance(org, dict) else None

    location_raw = ", ".join(p for p in (city, country) if p) or None
    # jobs.json has no remote marker at all (see the module docstring): treat a posting
    # with a real address as on-site and let ``guess_remote`` override that when the
    # title or location says remote/hybrid.
    remote_flag = False if (city or country) else None

    return Job(
        source=NAME,
        source_id=f"{slug}:{item_id}",
        url=str(url),
        title=str(title),
        company=company or slug,
        description=strip_html(item.get("content_html") or item.get("content_text")),
        location_raw=location_raw,
        country=guess_country(country, city),
        city=city,
        remote=guess_remote(location_raw, str(title), flag=remote_flag),
        posted_at=parse_date(item.get("date_published") or posting.get("datePosted")),
        raw=item,
    )


# --------------------------------------------------------------------------------- RSS


def _text(el: ET.Element, path: str) -> str | None:
    value = (el.findtext(path, namespaces=TT_NS) or "").strip()
    return value or None


def parse_rss_item(item: ET.Element, slug: str) -> Job | None:
    """One RSS ``<item>`` → Job (mirrors ats_scrapers.scrapers.teamtailor)."""
    link = _text(item, "link")
    if not link:
        return None
    guid = _text(item, "guid")
    match = _URL_ID_RE.search(link)
    item_id = match.group(1) if match else guid
    title = _text(item, "title")
    if not item_id or not title:
        return None

    location = item.find("tt:locations/tt:location", TT_NS)
    city = _text(location, "tt:city") if location is not None else None
    country = _text(location, "tt:country") if location is not None else None
    if location is not None and not (city or country):
        city = _text(location, "tt:name")
    location_raw = ", ".join(p for p in (city, country) if p) or None

    status = (_text(item, "remoteStatus") or "").lower()
    if status == "hybrid":
        remote = "hybrid"
    else:
        # "fully"/"none" are the other values Teamtailor emits; anything else (or a
        # missing element) leaves the decision to the title/location text.
        remote = guess_remote(location_raw, title, flag=_REMOTE_FLAG.get(status))
    tags = [t for t in (_text(item, "tt:department"), _text(item, "tt:role")) if t]

    return Job(
        source=NAME,
        source_id=f"{slug}:{item_id}",
        url=link,
        title=title,
        company=slug,
        description=strip_html(_text(item, "description")),
        location_raw=location_raw,
        country=guess_country(country, city),
        city=city,
        remote=remote,
        tags=tags,
        posted_at=parse_date(_text(item, "pubDate")),
        raw={"link": link, "guid": guid, "title": title},
    )


def parse_rss(xml_text: str, slug: str) -> list[Job]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise TeamtailorTenantError(f"{slug}: jobs.rss is not XML ({exc})") from exc
    if root.tag.lower() != "rss" and root.find(".//channel") is None:
        raise TeamtailorTenantError(f"{slug}: jobs.rss root <{root.tag}> is not <rss>")
    return list(safe_records(root.iter("item"), lambda it: parse_rss_item(it, slug), NAME))


# ------------------------------------------------------------------------------ tenant


def fetch_tenant(ctx: SourceContext, slug: str) -> list[Job]:
    """Jobs for one tenant. JSON Feed first, RSS as fallback. Raises on a dead tenant."""
    if not _SLUG_RE.match(slug):
        raise TeamtailorTenantError(f"{slug!r} is not a valid Teamtailor tenant slug")

    resp = ctx.http.get(JSON_URL.format(slug=slug))
    if resp.status_code < 400:
        payload: Any = None
        try:
            payload = resp.json()
        except ValueError:
            log.info("%s: %s/jobs.json is not JSON, falling back to jobs.rss", NAME, slug)
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return list(
                safe_records(payload["items"], lambda it: parse_json_item(it, slug), NAME)
            )
        if payload is not None:
            log.info("%s: %s/jobs.json has no items[], falling back to jobs.rss", NAME, slug)
    else:
        log.info(
            "%s: %s/jobs.json -> HTTP %s, falling back to jobs.rss",
            NAME, slug, resp.status_code,
        )

    xml_text = ctx.http.get_text(RSS_URL.format(slug=slug), params={"per_page": RSS_PER_PAGE})
    return parse_rss(xml_text, slug)


class Teamtailor:
    name = NAME
    description = "Teamtailor career sites (jobs.json JSON Feed, jobs.rss fallback)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        tenants = [str(t).strip() for t in (ctx.opt("tenants") or []) if str(t).strip()]
        if not tenants:
            raise ValueError("teamtailor: no tenant slugs configured (option 'tenants')")

        seen = 0
        failures: list[str] = []
        for slug in tenants:
            try:
                jobs = fetch_tenant(ctx, slug)
            except Exception as exc:
                failures.append(slug)
                log.warning("%s: tenant %s failed: %s", self.name, slug, exc)
                continue
            for job in jobs:
                yield job
                seen += 1
                if ctx.limit and seen >= ctx.limit:
                    return
        if len(failures) == len(tenants):
            raise RuntimeError(
                f"teamtailor: all {len(tenants)} tenants failed "
                f"({', '.join(failures[:5])}{'…' if len(failures) > 5 else ''})"
            )


register(Teamtailor())
