"""Company career boards behind a known ATS, via the ``ats-scrapers`` library.

Give it any public careers URL (Greenhouse, Lever, Ashby, Workable, SmartRecruiters,
Recruitee, Personio, Breezy, Teamtailor, …); ``get_scraper_for_url`` recognizes the ATS
from the URL shape and returns a scraper whose ``.fetch()`` yields ``ats_scrapers`` Job
rows, which this adapter normalizes into our ``Job``.

Custom-domain careers sites (jobs.siemens.com fronting Phenom, careers.example.com
fronting Avature/Eightfold) cannot be resolved from the URL alone. Name the ATS instead —
a board entry may be a mapping ``{ats, slug, company?, options?, include?, exclude?}``,
which goes straight to ``get_scraper(ats, slug, **options)``.

Enterprise boards list thousands of postings, so each board can carry a case-insensitive
``include`` / ``exclude`` regex tested against the posting *title* before anything is
converted (source-level ``default_include`` / ``default_exclude`` cover the boards that
don't set their own). When a board is filtered and its scraper supports per-job detail
requests (it overrides ``BaseScraper.get_description``), the listing is fetched without
descriptions and only the surviving postings are detailed — that is what turns a
half-hour Workday crawl into a short one. ``lazy_descriptions: true|false|auto``
overrides the rule.

A description that is not one — a page shell of CSS declarations, ``"..."``, an empty
paragraph — is stored as ``None`` (``clean_description``) so the AI stage is told there is no
description instead of being handed junk. A board that can detail-fetch gets a second chance
at the real text even when its listing "had" a description. Cornerstone career sites are the
reason both exist: their listing endpoint strips the HTML tags for us, which turns a tenant's
pasted-in ``<style>`` blocks into prose, so their descriptions come from the career site's
requisition service instead (``_cornerstone``).

One board failing (dead company, ATS change, unknown ATS name, 403) never stops the rest;
only an *all boards failed* run raises. A malformed board entry, on the other hand, is a
config bug and raises before any fetching starts.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from typing import Any

from ats_scrapers import Job as ATSJob
from ats_scrapers import get_scraper_for_url

# Import from the package, not from ``.base``: importing ``ats_scrapers.scrapers`` runs
# every ``@ScraperRegistry.register`` decorator, so ``get_scraper`` can find the class.
from ats_scrapers.scrapers import get_scraper
from ats_scrapers.scrapers.base import BaseScraper

from jobscraper.models import Job
from jobscraper.sources._common import clean_description, guess_country, guess_remote
from jobscraper.sources._cornerstone import CornerstoneDescriptions, is_cornerstone
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

NAME = "ats_boards"

_REMOTEISH_RE = re.compile(r"\b(remote|anywhere|telecommute|distributed)\b", re.IGNORECASE)

# Keys a mapping board entry may carry.
_ENTRY_KEYS = frozenset(
    {"url", "ats", "slug", "company", "options", "include", "exclude", "lazy_descriptions"}
)
_MAX_DESCRIPTION = 25_000  # what ats-scrapers' own enrich_descriptions truncates to


def _city(location: str | None) -> str | None:
    if not location:
        return None
    first = location.split(",")[0].strip()
    if not first or _REMOTEISH_RE.search(first):
        return None
    return first


def _salary_text(job: ATSJob) -> str | None:
    if job.salary_summary:
        return job.salary_summary
    if job.salary_min is None and job.salary_max is None:
        return None
    amounts = [f"{int(a):,}".replace(",", " ") for a in (job.salary_min, job.salary_max) if a]
    parts = ["–".join(amounts)]
    if job.salary_currency:
        parts.append(job.salary_currency)
    if job.salary_period:
        parts.append(f"/ {job.salary_period.lower()}")
    return " ".join(parts)


def convert(job: ATSJob, company: str | None = None) -> Job | None:
    """One ats-scrapers Job → our Job. ``company`` overrides the ATS display name."""
    url = str(job.url)
    ats_type = getattr(job.ats_type, "value", job.ats_type)
    location = job.location or None
    tags = [t for t in (job.department, job.team) if t]
    raw: dict[str, Any] = job.model_dump(mode="json", exclude={"description"})
    return Job(
        source=NAME,
        source_id=f"{ats_type}:{job.ats_id or url}",
        url=url,
        title=job.title,
        company=company or job.company,
        description=clean_description(job.description),
        location_raw=location,
        country=job.country_iso or guess_country(location),
        city=_city(location),
        remote=guess_remote(location, job.title, flag=job.is_remote),
        employment_type=job.employment_type or job.commitment,
        salary_text=_salary_text(job),
        tags=tags,
        posted_at=job.posted_at,
        raw=raw,
    )


@dataclass(frozen=True)
class Board:
    """One configured career board, normalized before any fetching happens."""

    label: str  # what the logs and the all-failed message name
    make_scraper: Callable[..., Any]  # (**ctor kwargs) -> ats-scrapers scraper
    include: re.Pattern[str] | None = None
    exclude: re.Pattern[str] | None = None
    company: str | None = None
    lazy: bool | None = None  # None = decide from the filter + scraper capability

    @property
    def filtered(self) -> bool:
        return self.include is not None or self.exclude is not None

    def keep(self, title: Any) -> bool:
        text = title if isinstance(title, str) else ""
        if self.include is not None and not self.include.search(text):
            return False
        return not (self.exclude is not None and self.exclude.search(text))


def _compile(pattern: Any, field: str, entry: Any) -> re.Pattern[str] | None:
    if pattern is None:
        return None
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError(f"{NAME}: {field!r} must be a non-empty regex in board entry {entry!r}")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"{NAME}: bad {field!r} regex in board entry {entry!r}: {exc}") from exc


def _lazy_flag(value: Any, where: str) -> bool | None:
    """``true`` / ``false`` / ``auto`` (or unset) → True / False / None."""
    if value is None or (isinstance(value, str) and value.strip().lower() == "auto"):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError(f"{NAME}: 'lazy_descriptions' must be true, false or auto in {where}")


def _url_factory(url: str) -> Callable[..., Any]:
    def make(**kwargs: Any) -> Any:
        return get_scraper_for_url(url, **kwargs)

    return make


def _ats_factory(ats: str, slug: str, options: dict[str, Any]) -> Callable[..., Any]:
    def make(**kwargs: Any) -> Any:
        return get_scraper(ats, slug, **{**options, **kwargs})

    return make


def make_board(
    entry: Any,
    *,
    default_include: re.Pattern[str] | None = None,
    default_exclude: re.Pattern[str] | None = None,
    default_lazy: bool | None = None,
) -> Board:
    """Normalize one ``urls:`` entry (a careers URL or a mapping) into a ``Board``."""
    if isinstance(entry, str):
        entry = {"url": entry.strip()}
    if not isinstance(entry, dict):
        raise ValueError(
            f"{NAME}: board entry must be a careers URL or a mapping, got {entry!r}"
        )
    unknown = sorted(set(entry) - _ENTRY_KEYS)
    if unknown:
        raise ValueError(
            f"{NAME}: unknown key(s) {unknown} in board entry {entry!r}; "
            f"known keys: {sorted(_ENTRY_KEYS)}"
        )

    url = str(entry.get("url") or "").strip()
    ats = str(entry.get("ats") or "").strip()
    slug = str(entry.get("slug") or "").strip()
    options = entry.get("options") or {}
    if not isinstance(options, dict):
        raise ValueError(f"{NAME}: 'options' must be a mapping in board entry {entry!r}")
    if url and (ats or slug):
        raise ValueError(
            f"{NAME}: board entry {entry!r} sets both 'url' and 'ats'/'slug' — pick one"
        )
    if url:
        label, make_scraper = url, _url_factory(url)
    elif ats and slug:
        label, make_scraper = f"{ats}:{slug}", _ats_factory(ats, slug, options)
    else:
        raise ValueError(
            f"{NAME}: board entry {entry!r} needs either 'url' or both 'ats' and 'slug'"
        )

    company = entry.get("company")
    return Board(
        label=label,
        make_scraper=make_scraper,
        include=_compile(entry["include"], "include", entry)
        if "include" in entry
        else default_include,
        exclude=_compile(entry["exclude"], "exclude", entry)
        if "exclude" in entry
        else default_exclude,
        company=str(company).strip() or None if company else None,
        lazy=_lazy_flag(entry["lazy_descriptions"], f"board entry {entry!r}")
        if "lazy_descriptions" in entry
        else default_lazy,
    )


def _supports_detail_fetch(scraper: Any) -> bool:
    """Whether the scraper can fetch one posting's description on demand."""
    get_description = getattr(type(scraper), "get_description", BaseScraper.get_description)
    return get_description is not BaseScraper.get_description


Describe = Callable[[ATSJob], str | None]


def _detail_capable(scraper: Any) -> bool:
    """Whether one posting's description can be fetched on demand for this board."""
    return is_cornerstone(scraper) or _supports_detail_fetch(scraper)


def _describer(scraper: Any, http: Any) -> Describe | None:
    """How this board fetches one posting's description, or ``None`` if it cannot.

    Most providers answer for themselves. Cornerstone is the exception: its scraper has no
    per-posting request, and the descriptions its listing carries can be a page shell (see
    ``_cornerstone``), so we go to the career site's requisition service over ``ctx.http``.
    """
    if is_cornerstone(scraper):
        return CornerstoneDescriptions(http).get_description if http is not None else None
    if _supports_detail_fetch(scraper):
        return scraper.get_description
    return None


def _open_board(
    board: Board, *, timeout: float, include_descriptions: bool, http: Any = None
) -> tuple[Any, Describe | None]:
    """Build the scraper for ``board`` and decide how descriptions are fetched.

    Returns the scraper and, when the board goes lazy, the per-posting description call for
    the title survivors. The capability check needs an instance, so a lazy board is built
    twice — both calls are plain attribute assignment in ats-scrapers, no I/O.
    """
    scraper = board.make_scraper(timeout=timeout, include_descriptions=include_descriptions)
    wanted = board.lazy if board.lazy is not None else board.filtered
    if wanted and include_descriptions and _detail_capable(scraper):
        scraper = board.make_scraper(timeout=timeout, include_descriptions=False)
    return scraper, (_describer(scraper, http) if wanted else None)


def _fill_descriptions(
    jobs: list[ATSJob], describe: Describe | None, label: str
) -> None:
    """Give the title survivors a usable description.

    A listing description that is only page shell or a placeholder is dropped first, so a
    board whose listing "has" descriptions still gets the real text detail-fetched. Failures
    leave the posting without one — better than shipping the shell to the AI stage.

    The surviving bodies are stored back as the cleaned text, so ``convert`` does not parse
    the same HTML a second time.
    """
    for job in jobs:
        if job.description:
            job.description = clean_description(job.description)
        if job.description or describe is None:
            continue
        try:
            description = describe(job)
        except Exception as exc:
            log.warning("%s: %s: description fetch failed for %s: %s", NAME, label, job.url, exc)
            continue
        if description:
            job.description = description[:_MAX_DESCRIPTION]


class ATSBoards:
    name = NAME
    description = "Company career boards (Greenhouse/Lever/Ashby/Workday/… via ats-scrapers)"

    def fetch(self, ctx: SourceContext) -> Iterable[Job]:
        entries = [
            e
            for e in (ctx.opt("urls") or [])
            if e is not None and not (isinstance(e, str) and not e.strip())
        ]
        if not entries:
            raise ValueError(f"{NAME}: no careers URLs configured (option 'urls')")
        timeout = float(ctx.opt("timeout", 30.0))
        include_descriptions = bool(ctx.opt("include_descriptions", True))
        defaults = {
            "default_include": _compile(ctx.opt("default_include"), "default_include", "options"),
            "default_exclude": _compile(ctx.opt("default_exclude"), "default_exclude", "options"),
            "default_lazy": _lazy_flag(ctx.opt("lazy_descriptions"), "option 'lazy_descriptions'"),
        }
        # Malformed config is a bug, not a flaky board: fail before any HTTP happens.
        boards = [make_board(entry, **defaults) for entry in entries]

        seen = 0
        failures: list[str] = []
        for board in boards:
            try:
                scraper, describe = _open_board(
                    board,
                    timeout=timeout,
                    include_descriptions=include_descriptions,
                    http=ctx.http,
                )
                ats_jobs = list(scraper.fetch())
            except Exception as exc:
                failures.append(board.label)
                log.warning("%s: board %s failed: %s", self.name, board.label, exc)
                continue
            kept = [job for job in ats_jobs if board.keep(job.title)]
            log.info(
                "%s: %s -> %d jobs, %d after title filter",
                self.name,
                board.label,
                len(ats_jobs),
                len(kept),
            )
            _fill_descriptions(kept, describe, board.label)
            for job in safe_records(kept, partial(convert, company=board.company), self.name):
                yield job
                seen += 1
                if ctx.limit and seen >= ctx.limit:
                    return
        if len(failures) == len(boards):
            raise RuntimeError(
                f"ats_boards: all {len(boards)} boards failed "
                f"({', '.join(failures[:3])}{'…' if len(failures) > 3 else ''})"
            )


register(ATSBoards())
