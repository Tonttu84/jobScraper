"""The recruitment platform behind valtiolle.fi and kuntarekry.fi.

Both boards are built and hosted by **Grade Solutions Oy** (part of Talentech) — every page of
either site says so in ``<meta name="author" content="Grade Solutions Oy" />``, and the
kuntarekry footer carries the vendor's logo. Beyond the branding the two sites are the same
application: identical ``<job-card>`` / ``<job-list>`` / ``<ip-pagination>`` custom elements,
identical ``/dist/{css,js}/app.<hash>.{css,js}`` bundles, identical URL scheme, identical
schema.org markup. So the parser lives here once and each board is a nine-line module.

**Listing** — ``GET https://<host>/fi/tyopaikat/?desc=<term>``, further pages at
``/fi/tyopaikat/sivu<N>/?desc=<term>``. 24 results per page, newest first. Each result is a
``<job-card>`` whose *attributes* are the whole record — there is no text content to parse::

    <job-card profit-center="Tulli" title="Lead Developer, Helsinki"
              publication-date="10.9.2026" publication-time="15:15"
              publication-end="30.9.2026" publication-end-time="16:15"
              ext-id="24357" url="/fi/tyopaikat/lead-developer-helsinki-24357/"
              job-id="303390" job-key="24357"></job-card>

``<ip-pagination current="1" total="2" next="…">`` closes the results list. **``total`` is the
number of pages, not the number of hits** (the reconnaissance note had this the other way
round): a search with 27 hits says ``total="2"``. Asking for a page past the end is not an
error — the site answers 200 with an empty results list — but reading ``total`` saves the
request. Kuntarekry follows the pagination with a second ``<job-list>`` of paid promotions
that repeats on every page; those cards carry ``is-promoted="true"`` and are skipped.

**Detail** — the posting page carries an ``application/ld+json`` ``JobPosting`` with
``title``, ``hiringOrganization`` (a plain string, not an Organization node),
``jobLocation.address.{addressLocality,addressRegion}``, ``datePosted`` (with the +03:00
offset, so it beats the card's wall-clock date), ``validThrough``, ``baseSalary.value`` (free
text: a sum, or a collective-agreement code like "YTES") and ``employmentType`` (the site's own
Finnish facet labels, passed through as they are).

Its ``description`` is **not** the advert, though: it is only the lead paragraph plus the first
section. Everything a candidate is actually screened on — "Hakijalta odotamme" (requirements),
"Tarjoamme sinulle" (the offer), the qualification rules — sits in the rendered
``<ip-details>`` sections and nowhere else. So the description is assembled from those, with
the JSON-LD text as the fallback for a page whose markup we no longer recognize. Two sections
are dropped by their language-independent ``headerIcon``: the contact block (a named
recruiter's e-mail and phone — personal data this project does not store, as with
Työmarkkinatori's ``recruiter``) and the mobile-only repeat of metadata we already have.

The one structured field worth reading out of that markup is **"Etätyö"** (remote work), which
:func:`remote_facet` takes instead of guessing — its negative value contains the same word the
text guess keys on, so guessing marks a strictly on-site job remote.

Both boards are Finland-only, so ``country`` is the constant ``"FI"`` rather than a guess.

Neither site blocks automated reading: kuntarekry.fi/robots.txt is ``Allow: /`` and valtiolle.fi
serves no robots.txt at all. Everything still goes through ``ctx.http``'s per-host delay.

Verified live 2026-09-13 against both hosts.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jobscraper.http import SourceHTTPError, strip_html
from jobscraper.models import Job
from jobscraper.sources._common import clean_description, guess_remote, parse_date
from jobscraper.sources.base import SourceContext, safe_records

log = logging.getLogger(__name__)

LIST_PATH = "/fi/tyopaikat/"
PAGE_PATH = "/fi/tyopaikat/sivu{page}/"
COUNTRY = "FI"

#: Cards per results page, as the platform serves them. Documented, never relied on.
PAGE_SIZE = 24

DEFAULT_MAX_PAGES = 5
DEFAULT_MAX_DETAILS = 200

#: Card attributes are Helsinki wall-clock time with no offset. Falling back to UTC on a host
#: without a tz database costs two or three hours on ``posted_at``, never a wrong day.
try:
    HELSINKI: tzinfo = ZoneInfo("Europe/Helsinki")
except ZoneInfoNotFoundError:  # pragma: no cover - only on a host with no tz database
    HELSINKI = UTC

#: ``<ip-details>`` sections left out of the description, by the icon in their header — that is
#: the one attribute of theirs that does not change with the site's language. ``envelop2`` is the
#: contact block (a named recruiter's e-mail and phone); ``file-text`` is the mobile-only
#: ("hide-on-desktop") repeat of the employer, the requisition id and the application window,
#: all of which the card and the JSON-LD already give us in structured form.
DROP_SECTION_ICONS = frozenset({"envelop2", "file-text"})


def _soup(html: str) -> Any:
    from bs4 import BeautifulSoup

    return BeautifulSoup(html or "", "lxml")


# ------------------------------------------------------------------------------- listing


def parse_cards(html: str) -> list[dict[str, str]]:
    """Every result ``<job-card>`` on a listing page, as its attribute dict.

    Deliberately not scoped to ``<job-list variant="grid">``: the wrapper is the vendor's
    layout, not a contract, and a card that ends up somewhere else should still be read. What
    *is* skipped is the paid promotion — ``is-promoted`` — because it repeats on every page and
    is not part of the search result.
    """
    cards = []
    for card in _soup(html).find_all("job-card"):
        if card.has_attr("is-promoted"):
            continue
        attrs = {k: v for k, v in card.attrs.items() if isinstance(v, str)}
        attrs.pop("data-translations", None)
        cards.append(attrs)
    return cards


def page_count(html: str) -> int | None:
    """``<ip-pagination total>`` — the number of pages, or ``None`` when there is no pagination."""
    pagination = _soup(html).find("ip-pagination")
    if pagination is None:
        return None
    try:
        return int(str(pagination.get("total")))
    except (TypeError, ValueError):
        return None


def publication_datetime(date: str | None, time: str | None = None) -> datetime | None:
    """``"11.9.2026"`` + ``"11:00"`` → an aware datetime in Finnish time.

    The card gives no offset, so the wall clock is read in ``Europe/Helsinki``. A missing or
    unreadable time means midnight; a date we cannot read at all means ``None``.
    """
    day = parse_date(date)
    if day is None:
        return None
    hour, minute = 0, 0
    with suppress(AttributeError, TypeError, ValueError):
        hour, minute = (int(part) for part in str(time).split(":", 1))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=HELSINKI)


# -------------------------------------------------------------------------- detail page


def job_posting(html: str) -> dict[str, Any]:
    """The page's schema.org ``JobPosting`` node, or ``{}`` when it is missing or malformed.

    The pages also carry a ``BreadcrumbList`` block, so the type is checked rather than the
    first block taken.
    """
    for script in _soup(html).find_all("script", attrs={"type": "application/ld+json"}):
        try:
            node = json.loads(script.string or "")
        except (TypeError, ValueError):
            continue
        if isinstance(node, dict) and "JobPosting" in str(node.get("@type", "")):
            return node
    return {}


def rendered_description(html: str) -> str | None:
    """The advert as the page shows it: the lead paragraph plus the ``<ip-details>`` sections.

    Each section keeps its heading, because the headings are what tell the AI stage apart the
    requirements from the offer. ``None`` when the page has none of this markup.
    """
    soup = _soup(html)
    parts: list[str] = []
    lead = soup.select_one("p.lead")
    if lead is not None:
        parts.append(lead.get_text(" ", strip=True))
    for section in soup.find_all("ip-details"):
        if section.get("headericon") in DROP_SECTION_ICONS:
            continue
        body = strip_html(section.decode_contents())
        if not body or not body.strip():
            continue
        heading = str(section.get("title") or "").strip()
        parts.append(f"{heading}\n{body.strip()}" if heading else body.strip())
    return clean_description("\n\n".join(parts)) if parts else None


def remote_facet(html: str) -> str | None:
    """The posting's structured "Etätyö" (remote work) field, or ``None`` when it has none.

    This one is worth reading rather than guessing, because guessing gets it backwards: the
    field's negative value, "Ei mahdollisuutta työskennellä etänä" ("no possibility of working
    remotely"), contains the very word ``guess_remote`` looks for, and every posting that
    carried the field came out of the text guess as ``remote`` (seen on the first live probe).

    The values observed across both boards on 2026-09-13 are "Mahdollisuus työskennellä etänä",
    "Ei mahdollisuutta työskennellä etänä" (valtiolle) and "Osittainen etätyömahdollisuus"
    (kuntarekry). None of them offers a *remote* post — these are office jobs in a named city
    that may be done partly from home — so the positive values map to ``hybrid``, and only a
    wording we have not seen falls back to the text guess.

    The site prints the field twice: as a ``<li>`` in "Tehtävän muut tiedot" and as an
    ``<ip-aside-item>`` in the mobile-only "Työpaikan tiedot" block. Either will do.
    """
    soup = _soup(html)
    values = [item.get_text(" ", strip=True)
              for item in soup.select('ip-aside-item[title="Etätyö"]')]
    values += [label.parent.get_text(" ", strip=True)
               for label in soup.find_all("strong", string=lambda s: s and s.strip() == "Etätyö:")]
    for value in values:
        low = value.lower().removeprefix("etätyö:").strip()
        if "etä" not in low:
            continue
        return "onsite" if low.startswith("ei ") else "hybrid"
    return None


def parse_detail(html: str) -> dict[str, Any]:
    """→ ``{"jsonld": …, "description": …, "remote": <kind or None>}``."""
    posting = job_posting(html)
    description = rendered_description(html) or clean_description(posting.get("description"))
    return {"jsonld": posting, "description": description, "remote": remote_facet(html)}


def _address(posting: dict[str, Any]) -> dict[str, Any]:
    location = posting.get("jobLocation")
    address = location.get("address") if isinstance(location, dict) else None
    return address if isinstance(address, dict) else {}


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def apply_detail(job: Job, detail: dict[str, Any]) -> Job:
    """A listing job plus what its detail page adds. Never overwrites the card's own facts."""
    posting = detail.get("jsonld") or {}
    address = _address(posting)
    locality = _text(address.get("addressLocality"))
    salary = posting.get("baseSalary")
    description = detail.get("description")
    return job.model_copy(
        update={
            "description": description,
            "location_raw": locality,
            "city": locality.split(",")[0].strip() if locality else None,
            "salary_text": _text(salary.get("value")) if isinstance(salary, dict) else None,
            "employment_type": _text(posting.get("employmentType")),
            "posted_at": parse_date(posting.get("datePosted")) or job.posted_at,
            "remote": detail.get("remote") or guess_remote(job.title, locality, description),
            "raw": {**job.raw, "jsonld": posting or None},
        }
    )


# ------------------------------------------------------------------------------- source


@dataclass(frozen=True)
class GradeBoard:
    """One board on the platform: everything that differs between valtiolle and kuntarekry."""

    name: str
    description: str
    base: str
    default_queries: tuple[str, ...]

    def list_url(self, page: int) -> str:
        return urljoin(self.base, LIST_PATH if page == 1 else PAGE_PATH.format(page=page))


class GradeSource:
    """``fetch`` for a Grade board. Subclasses set :attr:`board` and nothing else."""

    board: GradeBoard

    @property
    def name(self) -> str:
        return self.board.name

    @property
    def description(self) -> str:
        return self.board.description

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        board = self.board
        queries = ctx.opt("queries", list(board.default_queries)) or list(board.default_queries)
        if isinstance(queries, str):
            queries = [queries]
        max_pages = int(ctx.opt("max_pages", DEFAULT_MAX_PAGES))
        fetch_details = bool(ctx.opt("fetch_details", True))
        max_details = int(ctx.opt("max_details", DEFAULT_MAX_DETAILS))

        seen: set[str] = set()
        emitted = 0
        details_done = 0

        for query in queries:
            for page in range(1, max_pages + 1):
                html = ctx.http.get_text(board.list_url(page), params={"desc": str(query)})
                cards = parse_cards(html)
                if not cards:
                    break
                for job in safe_records(cards, self._convert, board.name):
                    if job.source_id in seen:
                        continue
                    seen.add(job.source_id)
                    if fetch_details and details_done < max_details:
                        details_done += 1
                        detail = self._detail(ctx, job.url)
                        if detail is not None:
                            job = apply_detail(job, detail)
                    yield job
                    emitted += 1
                    if ctx.limit and emitted >= ctx.limit:
                        return
                total = page_count(html)
                if total is not None and page >= total:
                    break

    # ---------------------------------------------------------------------------- helpers
    def _convert(self, card: dict[str, str]) -> Job | None:
        board = self.board
        title, url, job_id = card.get("title"), card.get("url"), card.get("job-id")
        if not (title and url and job_id):
            return None
        return Job(
            source=board.name,
            source_id=str(job_id),
            url=urljoin(board.base, url),
            title=title,
            company=_text(card.get("profit-center")),
            country=COUNTRY,
            remote=guess_remote(title),
            posted_at=publication_datetime(card.get("publication-date"),
                                           card.get("publication-time")),
            raw={"card": card},
        )

    def _detail(self, ctx: SourceContext, url: str) -> dict[str, Any] | None:
        """The detail page's contribution, or ``None`` when it cannot be had.

        A single unreachable posting page is a per-record failure: log it and keep the
        listing-only job, which still has a title, an employer and a URL.
        """
        try:
            return parse_detail(ctx.http.get_text(url))
        except (SourceHTTPError, ValueError) as exc:
            log.warning("%s: detail %s failed (%s); ingesting list-only", self.board.name, url, exc)
            return None
