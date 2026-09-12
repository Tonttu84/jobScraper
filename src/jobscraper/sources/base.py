"""Source adapter interface.

A source is a callable object: ``fetch(ctx) -> Iterable[Job]``. Adapters must never raise on
a single bad record — log and skip — but should raise ``SourceHTTPError`` (or any exception)
when the *endpoint* is broken, so ``jobscraper probe`` can report "this site changed".

Besides ``ctx.http`` (the polite client) a context may carry a headless-browser factory; an
adapter that can meet Cloudflare asks for HTML with :meth:`SourceContext.page` instead of
``ctx.http.get_text``, and the fallback is decided there.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from jobscraper.config import Profile
from jobscraper.http import BROWSER_HINT, Http, SourceHTTPError
from jobscraper.models import Job
from jobscraper.sources._common import is_cloudflare_challenge

if TYPE_CHECKING:  # pragma: no cover - the import only exists for the annotation
    from jobscraper.browser import Browser

log = logging.getLogger(__name__)

#: Statuses a Cloudflare-protected site answers a plain client with.
BLOCKED_STATUSES = (403, 503)
PAGE_MODES = ("auto", "browser", "http")


@dataclass
class SourceContext:
    http: Http
    profile: Profile
    options: dict[str, Any] = field(default_factory=dict)
    limit: int | None = None  # cap results (used by `probe`)
    #: Factory (not an instance) so Chromium only starts if a source really needs it;
    #: ``None`` when the config says ``http.browser: never`` or Playwright is not wanted.
    browser: Callable[[], Browser] | None = None
    #: How many pages this source took through the browser (``probe`` reports it).
    browser_calls: int = 0

    def opt(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)

    # ------------------------------------------------------------------ HTML fetching
    def page(self, url: str, *, wait_for: str | None = None, mode: str = "auto") -> str:
        """HTML of ``url``, through the headless browser when the plain client is turned away.

        ``mode``:

        * ``auto`` — try ``ctx.http`` first and fall back to the browser on 403/503 or on a
          Cloudflare challenge page served with a 200.
        * ``browser`` — go straight to the browser (a site known to be behind Cloudflare;
          saves one wasted request per page).
        * ``http`` — never use the browser, whatever comes back.

        ``wait_for`` is a CSS selector passed on to the browser. Without a browser factory the
        blocked cases raise :class:`SourceHTTPError` carrying the "needs a headless browser"
        hint, which is what ``jobscraper probe`` prints.
        """
        if mode not in PAGE_MODES:
            raise ValueError(f"unknown page mode {mode!r}: use one of {', '.join(PAGE_MODES)}")
        if mode == "browser":
            return self._browser_page(url, wait_for, f"GET {url} -> blocked")

        try:
            html = self.http.get_text(url)
        except SourceHTTPError as exc:
            if mode == "http" or exc.status not in BLOCKED_STATUSES:
                raise
            log.info("%s: HTTP %s, retrying through the headless browser", url, exc.status)
            return self._browser_page(url, wait_for, str(exc), exc)
        if mode == "auto" and is_cloudflare_challenge(html):
            log.info("%s: Cloudflare challenge, retrying through the headless browser", url)
            return self._browser_page(url, wait_for, f"GET {url} -> Cloudflare challenge")
        return html

    def _browser_page(
        self,
        url: str,
        wait_for: str | None,
        message: str,
        cause: Exception | None = None,
    ) -> str:
        if self.browser is None:
            hint = "" if BROWSER_HINT.strip() in message else BROWSER_HINT
            status = getattr(cause, "status", None) or 403
            raise SourceHTTPError(f"{message}{hint}", status) from cause
        html = self.browser().page_html(url, wait_for=wait_for)
        self.browser_calls += 1
        return html


class Source(Protocol):
    name: str
    description: str
    #: This adapter enumerates the source's whole listing, so a posting that does not come back
    #: has been taken down. False for a keyword search (LinkedIn, Indeed), where a posting
    #: missing from today's results means nothing at all. Adapters are duck-typed, so read it
    #: through :func:`enumerates_listing` rather than as an attribute.
    complete_listing: bool = True

    def fetch(self, ctx: SourceContext) -> Iterable[Job]: ...


def enumerates_listing(source: Source) -> bool:
    """Whether a posting this source did not return can be called taken down (default: yes)."""
    return bool(getattr(source, "complete_listing", True))


_REGISTRY: dict[str, Source] = {}


def register(source: Source) -> Source:
    _REGISTRY[source.name] = source
    return source


def all_sources() -> dict[str, Source]:
    # Import adapters lazily so a broken optional dependency doesn't kill the CLI.
    from jobscraper.sources import _load_all

    _load_all()
    return dict(_REGISTRY)


def get_source(name: str) -> Source:
    sources = all_sources()
    if name not in sources:
        raise KeyError(f"unknown source {name!r}; known: {', '.join(sorted(sources))}")
    return sources[name]


def take(items: Iterable[Job], limit: int | None) -> Iterable[Job]:
    if limit is None:
        yield from items
        return
    for i, item in enumerate(items):
        if i >= limit:
            return
        yield item


def safe_records(records: Iterable[Any], convert: Callable[[Any], Job | None], source: str) -> Iterable[Job]:
    """Convert raw records, skipping (and logging) any that fail to normalize."""
    for rec in records:
        try:
            job = convert(rec)
        except Exception as exc:
            log.warning("%s: skipping record (%s): %r", source, exc, str(rec)[:200])
            continue
        if job is not None:
            yield job
