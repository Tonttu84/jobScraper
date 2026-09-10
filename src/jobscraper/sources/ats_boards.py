"""Company career boards behind a known ATS, via the ``ats-scrapers`` library.

Give it any public careers URL (Greenhouse, Lever, Ashby, Workable, SmartRecruiters,
Recruitee, Personio, Breezy, Teamtailor, …); ``get_scraper_for_url`` recognizes the ATS
from the URL shape and returns a scraper whose ``.fetch()`` yields ``ats_scrapers`` Job
rows, which this adapter normalizes into our ``Job``.

Custom-domain careers sites (jobs.siemens.com fronting Phenom, careers.example.com
fronting Avature/Eightfold) cannot be resolved from the URL alone. Name the ATS instead —
a board entry may be a mapping ``{ats, slug, company?, options?, include?, exclude?}``,
which goes straight to ``get_scraper(ats, slug, **options)``.

A board that hires in one country only may declare it as ``country: FI``. It is a fallback,
not an override: it fills in the rows whose location string we could not parse (Workday's
"2 Locations", a bare city), because an on-site job with no country reaches the AI screen
and is paid for there.

A board that lists worldwide may instead declare the countries it is worth fetching from —
``countries: [FI, SE, …]``, or the source-level ``default_countries`` for the boards that
don't name their own. It runs after the title filter and *before* any per-posting description
is fetched, which is the whole point: a JPMorgan or Hitachi listing leaves hundreds of title
survivors that are in the US or India, and the rule filter only drops them after we have paid
for their descriptions. It is deliberately permissive — a posting is dropped only when we
could read a country, that country is not in the list, and the posting is not remote.

Reading that country is the other half. Workday's search rows usually carry no locations list
and no country, only the rollup "2 Locations" — but their ``externalPath`` spells the place
out (``/job/Espoo-Finland/Software-Engineer_R12345``), so the slug is parsed back into a
location string (``slug_location``) that ``guess_country`` can read.

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

The boards are independent hosts, so ``workers`` of them are fetched at once (a thread pool;
``workers: 1`` keeps the old sequential path) and each board is emitted as it finishes. The
polite per-host delay still applies — it is enforced per host, not per client — so this
parallelism costs no site anything; what it buys is a wall time near the slowest board
instead of the sum of all of them.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
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
from jobscraper.sources._common import ISO2_CODES, clean_description, guess_country, guess_remote
from jobscraper.sources._cornerstone import CornerstoneDescriptions, is_cornerstone
from jobscraper.sources.base import SourceContext, register, safe_records

log = logging.getLogger(__name__)

NAME = "ats_boards"

_REMOTEISH_RE = re.compile(r"\b(remote|anywhere|telecommute|distributed)\b", re.IGNORECASE)

# Keys a mapping board entry may carry.
_ENTRY_KEYS = frozenset(
    {
        "url", "ats", "slug", "company", "country", "countries", "options", "include", "exclude",
        "lazy_descriptions",
    }
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


def _raw_locations(job: ATSJob) -> list[str]:
    """Location strings from the scraper's raw payload, best-effort.

    Workday's search rows carry a rollup ("2 Locations") in ``location`` and the real list
    in ``raw["locations"]`` — when the tenant sends one, which most do not. Entries are
    either plain strings or the usual ``{"descriptor": …}`` wrappers.
    """
    raw = job.raw if isinstance(job.raw, dict) else {}
    values = raw.get("locations")
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    found: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
        elif isinstance(value, dict):
            for key in ("descriptor", "name", "label", "location", "text", "country"):
                text = value.get(key)
                if isinstance(text, str) and text.strip():
                    found.append(text.strip())
                    break
    return found


#: Workday slug segments that name no place: a rollup, or a posting with no location at all.
_SLUG_PLACEHOLDERS = frozenset(
    {"multiple locations", "various locations", "remote", "virtual", "anywhere", "flexible"}
)


def slug_location(external_path: Any) -> str | None:
    """The location a Workday ``externalPath`` spells out, as a plain location string.

    ``/job/Shanghai-Shanghai-China/Software-Engineer_R12345`` → ``"Shanghai, Shanghai, China"``
    and ``/job/US-CA-San-Jose/Developer-Intern_R1`` → ``"US, CA, San, Jose"``: either shape is
    something ``guess_country`` reads, which matters because most Workday tenants send no
    locations list and no country at all. A rollup placeholder ("Multiple-Locations", "Remote")
    names no place, and neither does a path that is not a job path.
    """
    if not isinstance(external_path, str):
        return None
    parts = [p for p in external_path.split("/") if p.strip()]
    if len(parts) < 2 or parts[0].lower() != "job":
        return None
    tokens = [t.strip() for t in parts[1].split("-") if t.strip()]
    if not tokens or " ".join(tokens).lower() in _SLUG_PLACEHOLDERS:
        return None
    return ", ".join(tokens)


def _job_slug_location(job: ATSJob) -> str | None:
    raw = job.raw if isinstance(job.raw, dict) else {}
    return slug_location(raw.get("externalPath"))


def _location_text(job: ATSJob) -> str | None:
    """The row's own location string, or the one its Workday slug spells out."""
    return job.location or _job_slug_location(job)


def _slug_country(job: ATSJob) -> str | None:
    """The country of a Workday slug. Both spellings are tried: "Espoo, Finland" is read by
    the comma rules, "London United Kingdom" by the multi-word country names."""
    text = _job_slug_location(job)
    if not text:
        return None
    return guess_country(text, text.replace(",", ""))


def job_country(job: ATSJob) -> str | None:
    """The country this listing row states, before any board-level fallback."""
    return (
        job.country_iso
        or guess_country(job.location or None)
        or guess_country(*_raw_locations(job))
        or _slug_country(job)
    )


def convert(
    job: ATSJob, company: str | None = None, country: str | None = None
) -> Job | None:
    """One ats-scrapers Job → our Job.

    ``company`` overrides the ATS display name; ``country`` is the board's declared country,
    used only for the rows whose own location string says nothing.
    """
    url = str(job.url)
    ats_type = getattr(job.ats_type, "value", job.ats_type)
    location = _location_text(job)
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
        country=job_country(job) or country,
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
    country: str | None = None  # ISO-2 fallback for rows with no parsable location
    countries: frozenset[str] | None = None  # worth fetching from; None = every country
    lazy: bool | None = None  # None = decide from the filter + scraper capability

    @property
    def filtered(self) -> bool:
        return self.include is not None or self.exclude is not None

    def keep(self, title: Any) -> bool:
        text = title if isinstance(title, str) else ""
        if self.include is not None and not self.include.search(text):
            return False
        return not (self.exclude is not None and self.exclude.search(text))

    def in_region(self, job: ATSJob) -> bool:
        """Whether this row is worth the per-posting description it is about to cost.

        Permissive on purpose: only a posting whose country we could actually read, that is
        not in the list and is not remote, is dropped. An unknown country stays — that call
        belongs to the rule filter, which sees the description this saves us from fetching.
        """
        if not self.countries:
            return True
        country = job_country(job) or self.country
        if country is None or country in self.countries:
            return True
        return guess_remote(_location_text(job), job.title, flag=job.is_remote) == "remote"


def _compile(pattern: Any, field: str, entry: Any) -> re.Pattern[str] | None:
    if pattern is None:
        return None
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError(f"{NAME}: {field!r} must be a non-empty regex in board entry {entry!r}")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"{NAME}: bad {field!r} regex in board entry {entry!r}: {exc}") from exc


def _iso2_code(value: Any, field: str, where: str) -> str:
    """One ISO-2 country code. A name, a typo or YAML's unquoted ``NO`` (a bool) is a bug."""
    code = str(value if value is not None else "").strip()
    if code.lower() not in ISO2_CODES:
        raise ValueError(
            f"{NAME}: {field!r} must be an ISO-2 country code (e.g. FI), got {value!r} in {where}"
        )
    return code.upper()


def _country_code(value: Any, entry: Any) -> str | None:
    """``country: fi`` → ``"FI"``. Anything that is not a real ISO-2 code is a config bug."""
    if value is None:
        return None
    return _iso2_code(value, "country", f"board entry {entry!r}")


def _country_list(value: Any, field: str, where: str) -> frozenset[str] | None:
    """``countries: [FI, se]`` → ``{"FI", "SE"}``; unset or empty means no country filter."""
    if value is None:
        return None
    items = value if isinstance(value, list) else [value]
    return frozenset(_iso2_code(item, field, where) for item in items) or None


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
    default_countries: frozenset[str] | None = None,
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
        country=_country_code(entry.get("country"), entry),
        countries=_country_list(entry["countries"], "countries", f"board entry {entry!r}")
        if "countries" in entry
        else default_countries,
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


def _run_board(
    board: Board, *, timeout: float, include_descriptions: bool, http: Any
) -> tuple[list[Job], str]:
    """Everything one board costs: list → title filter → country filter → descriptions →
    convert. Returns the board's jobs and the summary line its caller logs.

    This is what a worker thread runs, so it touches no shared state of its own: the only
    thing it shares is ``ctx.http``, whose per-host delay is thread-safe (see ``http.py``).
    """
    scraper, describe = _open_board(
        board, timeout=timeout, include_descriptions=include_descriptions, http=http
    )
    ats_jobs = list(scraper.fetch())
    kept = [job for job in ats_jobs if board.keep(job.title)]
    # Country before descriptions: an out-of-region posting must not cost a request.
    in_region = [job for job in kept if board.in_region(job)]
    summary = f"{NAME}: {board.label} -> {len(ats_jobs)} jobs, {len(kept)} after title filter"
    if board.countries:
        summary += f", {len(in_region)} after country filter"
    _fill_descriptions(in_region, describe, board.label)
    convert_one = partial(convert, company=board.company, country=board.country)
    return list(safe_records(in_region, convert_one, NAME)), summary


#: A board's finished work: call it for ``(jobs, summary)``, or let it re-raise its failure.
BoardWork = Callable[[], tuple[list[Job], str]]


def _board_results(
    boards: list[Board], run: Callable[[Board], tuple[list[Job], str]], workers: int
) -> Iterator[tuple[Board, BoardWork]]:
    """Each board paired with its result, ready to be logged and yielded.

    With one worker the boards are fetched lazily, in configured order — the sequential path
    the ``ctx.limit`` short-circuit and the ordering tests rely on. With more, they run in a
    thread pool and arrive as they finish, so one slow enterprise board no longer holds up
    the 80 others (boards are independent hosts). Board order in the output then varies
    between runs; nothing downstream depends on it — the store upserts by job id.
    """
    if workers <= 1:
        for board in boards:
            yield board, partial(run, board)
        return

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ats-board")
    try:
        pending: dict[Future[tuple[list[Job], str]], Board] = {
            pool.submit(run, board): board for board in boards
        }
        for future in as_completed(pending):
            yield pending[future], future.result
    finally:
        # Reached early when ``ctx.limit`` was filled: drop the boards still queued and stop
        # waiting on the ones in flight, whose output nobody is going to read anyway.
        pool.shutdown(wait=False, cancel_futures=True)


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
        workers = int(ctx.opt("workers", 4) or 1)
        defaults = {
            "default_include": _compile(ctx.opt("default_include"), "default_include", "options"),
            "default_exclude": _compile(ctx.opt("default_exclude"), "default_exclude", "options"),
            "default_countries": _country_list(
                ctx.opt("default_countries"), "default_countries", "option 'default_countries'"
            ),
            "default_lazy": _lazy_flag(ctx.opt("lazy_descriptions"), "option 'lazy_descriptions'"),
        }
        # Malformed config is a bug, not a flaky board: fail before any HTTP happens.
        boards = [make_board(entry, **defaults) for entry in entries]

        run = partial(
            _run_board, timeout=timeout, include_descriptions=include_descriptions, http=ctx.http
        )
        seen = 0
        failures: list[str] = []
        for board, work in _board_results(boards, run, workers):
            try:
                jobs, summary = work()
            except Exception as exc:
                failures.append(board.label)
                log.warning("%s: board %s failed: %s", self.name, board.label, exc)
                continue
            log.info("%s", summary)
            for job in jobs:
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
