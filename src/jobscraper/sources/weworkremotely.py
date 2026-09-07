"""weworkremotely.com — remote-only board, read through its per-category RSS feeds.

GET https://weworkremotely.com/categories/remote-programming-jobs.rss (and siblings)

The all-jobs feed caps at 100 items and is not paginated, so we read the programming category
feeds and dedupe on ``<link>``. Items carry WWR's custom elements next to the RSS ones:
``<region>`` ("Anywhere in the World", "Europe Only"), ``<country>``, ``<category>``,
``<type>`` and ``<skills>``. Titles read "Company: Job title".

Every posting is remote; ``<region>`` goes to ``Job.remote_region`` for the location filter.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from xml.etree import ElementTree as ET

from jobscraper.http import SourceHTTPError, strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_country, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

FEEDS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
]

_ANYWHERE_RE = re.compile(
    r"\b(anywhere|worldwide|world ?wide|global|any location)\b", re.IGNORECASE
)
_REGION_SPLIT_RE = re.compile(r"\s*(?:,|/|;|\||\bor\b|\band\b)\s*", re.IGNORECASE)
# "Acme Inc: Senior Backend Engineer" → ("Acme Inc", "Senior Backend Engineer").
_TITLE_RE = re.compile(r"^(?P<company>[^:]{1,80}):\s+(?P<title>.{2,})$", re.DOTALL)


def region_country(region: str | None) -> str | None:
    """ISO2 only when the "who can apply" text names exactly one country (see remotive)."""
    if not region or _ANYWHERE_RE.search(region):
        return None
    codes = {guess_country(part) for part in _REGION_SPLIT_RE.split(region) if part.strip()}
    codes.discard(None)
    return codes.pop() if len(codes) == 1 else None


def split_company_title(raw: str) -> tuple[str | None, str]:
    match = _TITLE_RE.match(raw.strip())
    if not match:
        return None, raw.strip()
    company = match.group("company").strip()
    title = match.group("title").strip()
    return (company or None, title) if company and title else (None, raw.strip())


def parse_feed(xml_text: str) -> list[ET.Element]:
    """RSS text → ``<item>`` elements. A broken feed is an endpoint failure, so it raises."""
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as exc:
        raise SourceHTTPError(f"weworkremotely: malformed RSS: {exc}") from exc
    return list(root.findall(".//item"))


def _text(item: ET.Element, tag: str) -> str | None:
    value = item.findtext(tag)
    return value.strip() if value and value.strip() else None


def parse_record(item: ET.Element) -> Job | None:
    link = _text(item, "link") or _text(item, "guid")
    raw_title = _text(item, "title")
    if not link or not raw_title:
        return None
    company, title = split_company_title(raw_title)
    region = _text(item, "region") or _text(item, "country")
    tags = [t for t in (_text(item, "category"),) if t]
    skills = _text(item, "skills")
    if skills:
        tags += [s.strip() for s in skills.split(",") if s.strip()]
    return Job(
        source="weworkremotely",
        source_id=_text(item, "guid") or link,
        url=link,
        title=title,
        company=company or _text(item, "company"),
        description=strip_html(item.findtext("description")),
        location_raw=region,
        country=region_country(region),
        remote="remote",
        remote_region=region,
        employment_type=_text(item, "type"),
        tags=tags,
        posted_at=parse_date(_text(item, "pubDate")),
        raw={
            "link": link,
            "title": raw_title,
            "region": region,
            "category": _text(item, "category"),
            "type": _text(item, "type"),
            "skills": skills,
            "pubDate": _text(item, "pubDate"),
        },
    )


class WeWorkRemotely:
    name = "weworkremotely"
    description = "weworkremotely.com category RSS feeds (remote-only, worldwide)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        feeds = [str(f) for f in (ctx.opt("feeds", FEEDS) or FEEDS) if f]
        seen: set[str] = set()
        emitted = 0
        for feed in feeds:
            items = parse_feed(ctx.http.get_text(feed))
            for job in safe_records(items, parse_record, self.name):
                if job.url in seen:
                    continue
                seen.add(job.url)
                yield job
                emitted += 1
                if ctx.limit and emitted >= ctx.limit:
                    return


register(WeWorkRemotely())
