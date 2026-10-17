"""Is a posting still open? Ask its own page.

A search source like LinkedIn cannot say a posting was taken down: a job missing from today's
search may simply not have matched today's queries, so :meth:`Store.mark_missing` never counts
it. Verdicts are carried forward between runs, so a closed vacancy kept its place in the report
until somebody clicked it (report #14's #2, 2026-10-17). ``jobscraper check-live`` fetches the
pages of the jobs the owner is about to read, and this module reads them:

``gone``
    HTTP 404 or 410, on any source;
``closed``
    the page is up and carries a closed marker — one of the source's own
    (:data:`DETECTORS`) or a generic one (:data:`CLOSED_MARKERS`) — matched by CSS class, never
    by its wording, since the page may be localised;
``unknown``
    the fetch failed, was throttled (429 after the client's retries), refused, or walled off:
    that says nothing about the posting, so it never drops one;
``open``
    anything else.

:func:`record` stores the checks and re-runs the rule filter for the jobs whose answer changed
what it should say, so a closed or gone posting drops out of the queue, the shortlist, the report
and the web UI like any rule drop (``posting closed (checked …)``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from jobscraper.config import Profile
from jobscraper.filters.rules import RULES_VERSION, evaluate
from jobscraper.http import Fetched, Http, try_get
from jobscraper.models import FilterResult, Job, LivenessCheck
from jobscraper.store import Store

#: How many of the report's ranked jobs ``check-live`` (and ``run``) checks by default.
CHECK_LIVE_TOP = 40

#: Statuses that drop a posting in the rule filter.
DEAD = frozenset({"closed", "gone"})

#: ``(css selector, reason)`` checked on every page whatever its source: markers of the career
#: platforms many boards sit on. SuccessFactors serves a withdrawn requisition as 200 with
#: ``<p class="jobErrMsg">`` ("You can't view this job because it's not available at this time").
CLOSED_MARKERS: tuple[tuple[str, str], ...] = (
    (".jobErrMsg", "SuccessFactors: job not available"),
)

def _linkedin(page: Fetched, soup: Any) -> tuple[str, str] | None:
    """LinkedIn's guest page: the "No longer accepting applications" caption, or a login wall."""
    from jobscraper.sources.linkedin import is_walled

    if is_walled(page.url):
        return "unknown", f"login wall ({page.url})"
    if soup.select_one(".closed-job__flavor--closed") is not None:
        return "closed", "LinkedIn: no longer accepting applications"
    return None


#: Indeed's page data flag. The visible "This job has expired on Indeed" sentence is no marker:
#: it sits in the translation dictionary of every Indeed page, open or not.
_INDEED_EXPIRED = re.compile(r'"isJobExpired"\s*:\s*(true|false)')


def _indeed(page: Fetched, soup: Any) -> tuple[str, str] | None:
    """Indeed serves an expired posting as 200; its embedded page data says ``isJobExpired``."""
    found = _INDEED_EXPIRED.search(page.text or "")
    if found is None:
        return None
    return ("closed", "Indeed: job expired") if found.group(1) == "true" else ("open", f"HTTP {page.status}")


#: Source name → its own detector. A detector reads a 2xx page (the fetch and its parsed soup)
#: and returns ``(status, reason)``, or None for no opinion. Add a source here when its pages
#: show a closed state the generic :data:`CLOSED_MARKERS` miss.
DETECTORS: dict[str, Callable[[Fetched, Any], tuple[str, str] | None]] = {
    "linkedin": _linkedin,
    "indeed": _indeed,
}


def classify(job: Job, page: Fetched) -> LivenessCheck:
    """What ``page`` (a fetch of ``job``'s own page) says about the posting."""
    def result(status: str, reason: str) -> LivenessCheck:
        return LivenessCheck(job_id=job.id, status=status, reason=reason, http_status=page.status)

    if page.status is None:
        return result("unknown", f"fetch failed: {page.error or 'no response'}")
    if page.status in (404, 410):
        return result("gone", f"HTTP {page.status}")
    if page.status >= 400:
        return result("unknown", f"HTTP {page.status}")
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page.text or "", "lxml")
    detector = DETECTORS.get(job.source)
    found = detector(page, soup) if detector else None
    if found is not None:
        return result(*found)
    for selector, reason in CLOSED_MARKERS:
        if soup.select_one(selector) is not None:
            return result("closed", reason)
    return result("open", f"HTTP {page.status}")


def fetch(http: Http, job: Job) -> Fetched:
    """``job``'s own page: LinkedIn's guest page (the fetch hydration uses), else its URL."""
    from jobscraper.sources import linkedin

    page = linkedin.fetch_page(http, job.url) if job.source == linkedin.NAME else None
    return page if page is not None else try_get(http, job.url)


def check(http: Http, job: Job) -> LivenessCheck:
    """Fetch ``job``'s page and classify it. Never raises."""
    return classify(job, fetch(http, job))


def record(store: Store, profile: Profile, checks: Sequence[LivenessCheck]) -> list[FilterResult]:
    """Store ``checks`` and re-filter the jobs whose rule verdict they change; returns those.

    ``report`` (and every stage after ``filter``) reads the stored filter results, so a check
    only takes effect once the job is re-filtered. That is done here, for a job whose latest
    conclusive check is closed/gone (it drops) or whose stored result still carries a liveness
    drop (it reopened). A duplicate stays a duplicate, and a job the filter never saw (out of
    the day window) is left for the next ``filter`` — which reads the checks itself.
    """
    if not checks:
        return []
    store.save_liveness(checks)
    ids = list(dict.fromkeys(c.job_id for c in checks))
    latest = store.liveness(ids)
    stored = store.filter_results()
    updated: list[FilterResult] = []
    for job in store.jobs(ids=ids):
        fr = stored.get(job.id)
        if fr is None or "duplicate_of" in fr.signals:
            continue
        live = latest.get(job.id)
        if (live is not None and live.status in DEAD) or fr.signals.get("liveness") in DEAD:
            updated.append(evaluate(job, profile, liveness=live))
    updated.sort(key=lambda r: ids.index(r.job_id))
    store.save_filter_results(updated, RULES_VERSION)
    return updated
