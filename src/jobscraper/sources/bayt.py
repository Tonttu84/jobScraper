"""bayt.com — the big Gulf job board (used here for Dubai / UAE listings).

No public API: we scrape the search listing HTML.

GET https://www.bayt.com/                                    (cookie handshake)
GET https://www.bayt.com/en/uae/jobs/q/{query-with-dashes}/?page=N

Bayt hands out session cookies on the home page and 403s / "Access Denied" the search pages
without them; the shared ``ctx.http`` client keeps the cookie jar, so the handshake happens
once per run. If the listing still comes back blocked (Cloudflare ``cf-chl``, "Access
Denied", HTTP 403) we raise ``SourceHTTPError`` — that state needs a headless browser, not a
parser fix. The card markup changes often, so every field is looked up by *substring* of the
class name and every one of them is optional.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from bs4 import BeautifulSoup
from bs4.element import Tag

from jobscraper.http import SourceHTTPError, strip_html
from jobscraper.models import Job
from jobscraper.sources._common import guess_remote, parse_date
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

SITE = "https://www.bayt.com"
HOME = SITE + "/"
SEARCH = SITE + "/en/uae/jobs/q"
DEFAULT_QUERIES = ["junior-developer", "software-developer"]

_BLOCKED_RE = re.compile(r"access denied|cf-chl|captcha-delivery|attention required", re.IGNORECASE)
_RELATIVE_RE = re.compile(r"(\d+)\+?\s*(day|week|month|hour|minute)s?\s*ago", re.IGNORECASE)
_RELATIVE_UNITS = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1.0, "week": 7.0, "month": 30.0}


def slugify(query: str) -> str:
    """"junior developer" → "junior-developer" (Bayt's search path segment)."""
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", str(query).lower())).strip("-")


def parse_posted(text: str | None) -> datetime | None:
    """Bayt shows either a date or "3 days ago" / "Today" / "30+ days ago"."""
    if not text:
        return None
    lowered = text.strip().lower()
    now = datetime.now(UTC)
    if lowered.startswith(("today", "just now")):
        return now
    if lowered.startswith("yesterday"):
        return now - timedelta(days=1)
    match = _RELATIVE_RE.search(lowered)
    if match:
        days = int(match.group(1)) * _RELATIVE_UNITS[match.group(2).lower()]
        return now - timedelta(days=days)
    return parse_date(text)


def _class_text(
    card: Tag, *needles: str, name: str | None = None, skip: Iterable[str | None] = ()
) -> str | None:
    """Text of the first descendant whose class contains a needle, needles in priority order.

    ``skip`` drops candidates whose text was already claimed by another field — Bayt reuses
    the generic ``t-mute`` class for the company, the location *and* the date.
    """
    unwanted = {s for s in skip if s}
    elements = [el for el in card.find_all(name or True) if isinstance(el, Tag)]
    for needle in needles:
        for el in elements:
            if needle in " ".join(el.get("class") or []):
                text = el.get_text(" ", strip=True)
                if text and text not in unwanted:
                    return text
    return None


def job_cards(html: str) -> list[Tag]:
    """Every listing card on a search page, tolerant of Bayt's two markup variants."""
    soup = BeautifulSoup(html, "lxml")
    cards: list[Tag] = []
    for li in soup.find_all("li"):
        if not isinstance(li, Tag):
            continue
        if li.has_attr("data-js-job") or "has-pointer-d" in " ".join(li.get("class") or []):
            cards.append(li)
    return cards


def parse_card(card: Tag) -> Job | None:
    link = card.select_one("h2 a[href]") or card.find("a", href=True)
    if link is None:
        return None
    title = link.get_text(" ", strip=True)
    href = str(link.get("href") or "")
    if not title or not href:
        return None
    url = href if href.startswith("http") else SITE + ("" if href.startswith("/") else "/") + href
    company = _class_text(card, "jb-company", "t-nowrap", skip=[title])
    posted = _class_text(card, "jb-date", name="span")
    if not posted:
        date_el = card.find(attrs={"data-automation-id": "job-active-date"})
        posted = date_el.get_text(" ", strip=True) if isinstance(date_el, Tag) else None
    location = _class_text(card, "jb-loc", "t-mute", skip=[title, company, posted])
    return Job(
        source="bayt",
        source_id=url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or url,
        url=url,
        title=title,
        company=company,
        location_raw=location,
        country="AE",
        city=location.split(",")[0].strip() if location else None,
        remote=guess_remote(location, title),
        posted_at=parse_posted(posted),
        raw={"title": title, "url": url, "company": company, "location": location, "date": posted},
    )


def _check_blocked(status: int, text: str) -> None:
    if status == 403 or _BLOCKED_RE.search(text[:5000]):
        raise SourceHTTPError(
            "bayt.com blocked the request (bot challenge): this source needs a headless "
            "browser (or fresh cookies) to scrape",
            status,
        )


def _listing_html(ctx: SourceContext, query: str, page: int) -> str:
    resp = ctx.http.get(f"{SEARCH}/{query}/", params={"page": page})
    _check_blocked(resp.status_code, resp.text)
    if resp.status_code >= 400:
        raise SourceHTTPError(
            f"GET {resp.request.url} -> HTTP {resp.status_code}", resp.status_code
        )
    return resp.text


def description_of(html: str) -> str | None:
    """Job-page description: Bayt's ``card-content is-spaced`` block (older: ``jb-descr``)."""
    soup = BeautifulSoup(html, "lxml")
    divs = [div for div in soup.find_all("div") if isinstance(div, Tag)]
    for needle in ("card-content is-spaced", "jb-descr"):
        for div in divs:
            if needle in " ".join(div.get("class") or []):
                text = strip_html(str(div))
                if not text:
                    return None
                return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip() or None
    return None


class Bayt:
    name = "bayt"
    description = "bayt.com UAE/Dubai job search (HTML scrape, cookie handshake)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        queries = [slugify(q) for q in (ctx.opt("queries", DEFAULT_QUERIES) or []) if q]
        max_pages = int(ctx.opt("max_pages", 2))
        fetch_details = bool(ctx.opt("fetch_details", True))
        max_details = int(ctx.opt("max_details", 40))

        try:  # cookie handshake; the shared client keeps the jar for the search pages
            ctx.http.get(HOME)
        except Exception as exc:
            log.warning("bayt: cookie handshake failed (%s), trying the search pages anyway", exc)

        seen: set[str] = set()
        emitted = details = 0
        for query in queries:
            for page in range(1, max(max_pages, 1) + 1):
                cards = job_cards(_listing_html(ctx, query, page))
                if not cards:
                    break
                for job in safe_records(cards, parse_card, self.name):
                    if job.source_id in seen:
                        continue
                    seen.add(job.source_id)
                    if fetch_details and details < max_details:
                        details += 1
                        self._add_description(ctx, job)
                    yield job
                    emitted += 1
                    if ctx.limit and emitted >= ctx.limit:
                        return

    def _add_description(self, ctx: SourceContext, job: Job) -> None:
        """Detail pages are best effort: a failure must not lose the listing row."""
        try:
            resp = ctx.http.get(job.url)
            _check_blocked(resp.status_code, resp.text)
            if resp.status_code < 400:
                job.description = description_of(resp.text)
        except SourceHTTPError:
            raise
        except Exception as exc:
            log.warning("bayt: detail fetch failed for %s (%s)", job.url, exc)


register(Bayt())


__all__ = ["Bayt", "description_of", "job_cards", "parse_card", "parse_posted", "slugify"]
