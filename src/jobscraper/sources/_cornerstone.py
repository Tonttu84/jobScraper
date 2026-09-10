"""Real posting text for Cornerstone (``*.csod.com``) career sites.

``ats-scrapers`` takes Cornerstone descriptions from the listing endpoint
(``rec-job-search/external/jobs``), and that endpoint returns ``externalDescription`` with
the HTML tags **already removed**. For most tenants that is fine. For a tenant that pasted a
whole HTML page into the description field — GMV does, template and all — the tags are gone
but the text inside its ``<style>`` blocks is not, so what arrives is 5 000 characters of
CSS declarations followed by the actual advert, and nothing downstream can separate them
(observed live on gmv.csod.com, 2026-09-10).

The career site's own requisition service still serves the original HTML, which strips
properly. Two plain GET requests, no browser:

1. ``GET https://<host>/ux/ats/careersite/<site>/home?c=<slug>`` — the career-site page
   carries a short-lived JWT (``csod.context.token``, ~1 h) used as a bearer token.
2. ``GET https://<host>/services/x/job-requisition/v2/requisitions/<requisition id>``
   with ``Authorization: Bearer <jwt>`` → ``{"data": {"externalDescriptions": {"<culture
   id>": "<html>"}, "defaultCultureId": n, …}}``.

Culture id 1 is English; a tenant whose default culture is another language still lists
English when it has one, so English wins, then the default culture, then whatever is left.

Not every tenant allows the service: Henkel answers 403 (its listing descriptions are clean
prose anyway, so nothing asks), and imec publishes ``"..."`` as the description of every
posting in both the listing and the requisition service — there is nothing to recover there,
which is why that board is configured eager in ``config/sources.yaml``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from jobscraper.http import Http
from jobscraper.sources._common import clean_description

log = logging.getLogger(__name__)

#: ats-scrapers builds Cornerstone job URLs as ``<origin>/ux/ats/careersite/<site>/job/<id>``.
_JOB_URL_RE = re.compile(
    r"^(?P<origin>https?://[^/\s]+)/ux/ats/careersite/(?P<site>\d+)/job/(?P<req>[\w.-]+)",
    re.IGNORECASE,
)
_SLUG_RE = re.compile(r"[?&]c=([^&#]+)")
#: The JWT, in either shape the career-site page ships it.
_TOKEN_RE = re.compile(r"csod\.context\.token\s*=\s*['\"]([^'\"]+)['\"]")
_TOKEN_FALLBACK_RE = re.compile(r'"token"\s*:\s*"([^"]+)"')

#: English. Cornerstone culture ids are global, not per tenant.
CULTURE_ENGLISH = "1"


def is_cornerstone(scraper: Any) -> bool:
    """Whether ``scraper`` is the library's Cornerstone scraper (or a stand-in for one)."""
    ats = getattr(scraper, "ats", None)
    return str(getattr(ats, "value", ats)).lower() == "cornerstone"


@dataclass(frozen=True)
class Requisition:
    """The pieces of a Cornerstone job URL the two requests need."""

    origin: str
    site_id: str
    requisition_id: str
    slug: str | None = None

    @property
    def home_url(self) -> str:
        """The career-site page that carries the JWT."""
        query = f"?c={self.slug}" if self.slug else ""
        return f"{self.origin}/ux/ats/careersite/{self.site_id}/home{query}"

    @property
    def detail_url(self) -> str:
        return f"{self.origin}/services/x/job-requisition/v2/requisitions/{self.requisition_id}"


def parse_job_url(url: str | None) -> Requisition | None:
    """A Cornerstone posting URL → its :class:`Requisition`, or ``None`` if it isn't one."""
    if not url:
        return None
    match = _JOB_URL_RE.match(str(url))
    if not match:
        return None
    slug = _SLUG_RE.search(str(url))
    return Requisition(
        origin=match.group("origin"),
        site_id=match.group("site"),
        requisition_id=match.group("req"),
        slug=slug.group(1) if slug else None,
    )


def parse_token(html: str | None) -> str | None:
    """The career site's JWT, from either shape the page writes it in."""
    if not html:
        return None
    match = _TOKEN_RE.search(html) or _TOKEN_FALLBACK_RE.search(html)
    return match.group(1) if match else None


def pick_description(payload: Any) -> str | None:
    """The requisition payload → the best posting body it carries, English first."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    bodies = data.get("externalDescriptions")
    if not isinstance(bodies, dict):
        return None
    default = str(data.get("defaultCultureId") or "")
    for culture in [CULTURE_ENGLISH, default, *sorted(bodies)]:
        text = clean_description(bodies.get(culture))
        if text:
            return text
    return None


class CornerstoneDescriptions:
    """Per-posting description fetches for the Cornerstone boards of one run.

    One instance per board: it holds the JWT for that tenant so a 130-posting board pays for
    the career-site page once. A tenant whose page carries no JWT is remembered as hopeless
    and never asked again; a failing requisition request is left to raise, so ``ats_boards``
    logs it against that posting and moves on.
    """

    def __init__(self, http: Http) -> None:
        self.http = http
        self._tokens: dict[str, str | None] = {}

    def _token(self, req: Requisition) -> str | None:
        if req.origin in self._tokens:
            return self._tokens[req.origin]
        token = parse_token(self.http.get_text(req.home_url))
        if not token:
            log.warning(
                "cornerstone: no JWT token in %s; descriptions stay as the listing gave them",
                req.home_url,
            )
        self._tokens[req.origin] = token
        return token

    def get_description(self, job: Any) -> str | None:
        """One posting's description, or ``None`` when this tenant cannot serve it."""
        req = parse_job_url(getattr(job, "url", None))
        if req is None:
            return None
        token = self._token(req)
        if not token:
            return None
        payload = self.http.get_json(
            req.detail_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        return pick_description(payload)
