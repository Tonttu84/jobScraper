"""Headless-browser fallback for sites that will not talk to a plain HTTP client.

A few boards (jobly.fi, bayt.com, duunitori.fi on a bad day) sit behind Cloudflare's
"Just a moment…" interstitial: the first request gets a challenge page or a 403, and only a
real browser that runs the challenge JavaScript is let through. This module wraps Playwright's
sync API in the smallest thing the adapters need — "give me the HTML of this URL" — with the
same manners as :class:`jobscraper.http.Http`: one browser-like User-Agent and a per-host
minimum delay.

Three details make it worth having as a class instead of a helper function:

* **The profile is persistent.** ``chromium.launch_persistent_context`` keeps the Cloudflare
  clearance cookie in ``data/browser/``, so the challenge is normally solved once per machine
  and not once per run.
* **The context is reused.** Starting Chromium costs a second or two; one instance serves
  every page a run needs and is closed by the caller (or by ``with``).
* **Playwright is optional.** It lives in the ``dev`` extra, so it is imported *inside* the
  methods: importing this module never fails, and a missing install raises
  :class:`BrowserUnavailable` with the two commands that fix it.

Adapters do not normally construct this themselves — they call
:meth:`jobscraper.sources.base.SourceContext.page`, which decides between plain HTTP and the
browser (see the ``http.browser`` switch in ``config/sources.yaml``).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

from jobscraper.config import paths
from jobscraper.http import DEFAULT_UA, SourceHTTPError
from jobscraper.sources._common import is_cloudflare_challenge

log = logging.getLogger(__name__)

#: How long to wait between two looks at a page that is still showing a challenge.
POLL_INTERVAL = 1.0

VIEWPORT = {"width": 1280, "height": 900}

INSTALL_HINT = (
    "Playwright is not installed. Run `uv sync --extra dev` and then "
    "`uv run playwright install chromium`."
)


class BrowserUnavailable(RuntimeError):
    """Playwright itself is missing (the ``dev`` extra was not installed)."""


class BrowserBlocked(SourceHTTPError):
    """The Cloudflare challenge never cleared; reported as HTTP 403 like the plain client."""

    def __init__(self, message: str) -> None:
        super().__init__(message, 403)


class Browser:
    """A persistent headless Chromium, used as ``browser.page_html(url)``."""

    def __init__(
        self,
        user_data_dir: Path | None = None,
        *,
        headless: bool = True,
        timeout: float = 45.0,
        min_delay: float = 1.0,
    ) -> None:
        self.user_data_dir = Path(user_data_dir) if user_data_dir is not None else paths().data / "browser"
        self.headless = headless
        self.timeout = timeout
        self.min_delay = min_delay
        self._playwright: Any = None
        self._context: Any = None
        self._last_call: dict[str, float] = {}

    # ------------------------------------------------------------------ lifecycle
    def _sync_playwright(self) -> Any:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # the dev extra is not installed
            raise BrowserUnavailable(INSTALL_HINT) from exc
        return sync_playwright

    def _ensure_context(self) -> Any:
        if self._context is not None:
            return self._context
        sync_playwright = self._sync_playwright()
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        log.info("starting headless Chromium (profile: %s)", self.user_data_dir)
        self._context = self._playwright.chromium.launch_persistent_context(
            str(self.user_data_dir),
            headless=self.headless,
            user_agent=DEFAULT_UA,
            viewport=dict(VIEWPORT),
            locale="en-US",
        )
        self._context.set_default_timeout(self.timeout * 1000)
        return self._context

    def close(self) -> None:
        """Close the browser and stop Playwright. Safe to call twice, or before first use."""
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def __enter__(self) -> Browser:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------ fetching
    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        last = self._last_call.get(host)
        if last is not None:
            wait = self.min_delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_call[host] = time.monotonic()

    @staticmethod
    def _is_challenge(page: Any) -> bool:
        try:
            title = page.title() or ""
        except Exception:  # a page mid-navigation can refuse to answer; look at the body only
            title = ""
        if "just a moment" in title.lower():
            return True
        return is_cloudflare_challenge(page.content())

    def page_html(
        self, url: str, *, wait_for: str | None = None, challenge_timeout: float = 30.0
    ) -> str:
        """Render ``url`` and return its HTML, sitting out a Cloudflare challenge if there is one.

        ``wait_for`` is a CSS selector the page must show before the HTML is read (useful for
        client-rendered listings). :class:`BrowserBlocked` is raised when the challenge is still
        up after ``challenge_timeout`` seconds.
        """
        self._throttle(url)
        context = self._ensure_context()
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
            deadline = time.monotonic() + challenge_timeout
            while self._is_challenge(page):
                if time.monotonic() >= deadline:
                    raise BrowserBlocked(
                        f"{url}: Cloudflare challenge did not clear in {challenge_timeout:.0f}s"
                    )
                log.debug("%s: waiting out the Cloudflare challenge", url)
                time.sleep(POLL_INTERVAL)
            if wait_for:
                page.wait_for_selector(wait_for, timeout=self.timeout * 1000)
            return page.content()
        finally:
            page.close()


class BrowserFactory:
    """Creates one :class:`Browser` on first call and hands out the same one afterwards.

    This is what the CLI passes to :class:`~jobscraper.sources.base.SourceContext`, so Chromium
    only starts if a source actually asks for a page, and every source in a run shares it.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self._browser: Browser | None = None

    def __call__(self) -> Browser:
        if self._browser is None:
            self._browser = Browser(**self.kwargs)
        return self._browser

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
