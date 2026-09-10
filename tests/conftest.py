"""Shared test helpers: a fake HTTP client that serves fixture files, and a SourceContext factory."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from jobscraper import config
from jobscraper.config import load_settings
from jobscraper.http import Http, _raise_for_status
from jobscraper.sources.base import SourceContext

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


class FakeHttp(Http):
    """Routes (substring of URL, optional method) → fixture text / JSON / callable.

    Usage:
        http = FakeHttp({"job-board-api": "arbeitnow.json"})            # any request whose URL contains the key
        http = FakeHttp({("POST", "/api/search/posting"): "nfj.json"})
        http = FakeHttp({"x": lambda req: httpx.Response(403, text="cloudflare")})
    Unmatched URLs raise AssertionError so tests notice unexpected requests.
    """

    def __init__(self, routes: dict[Any, Any]) -> None:
        super().__init__(min_delay=0, retries=0)
        self.routes = routes
        self.calls: list[httpx.Request] = []

    def request(self, method: str, url: str, *, params=None, json_body=None, data=None, headers=None) -> httpx.Response:
        req = self._client.build_request(method, url, params=params, json=json_body, data=data, headers=headers)
        self.calls.append(req)
        full = str(req.url)
        for key, value in self.routes.items():
            k_method, k_sub = (key if isinstance(key, tuple) else (None, key))
            if k_method and k_method.upper() != method.upper():
                continue
            if k_sub in full:
                if callable(value):
                    resp = value(req)
                elif isinstance(value, str) and (FIXTURES / value).exists():
                    resp = httpx.Response(200, text=fixture_text(value))
                elif isinstance(value, str):
                    resp = httpx.Response(200, text=value)
                else:
                    resp = httpx.Response(200, json=value)
                resp.request = req
                return resp
        raise AssertionError(f"FakeHttp: no route for {method} {full}")


class FakeBrowser:
    """Stand-in for :class:`jobscraper.browser.Browser`, routed like :class:`FakeHttp`.

    Routes (substring of URL) → fixture file name / literal text / callable taking the URL.
    Every call is recorded (``calls``, ``waited``) so a test can assert that the headless
    fallback was — or was not — used. Unmatched URLs raise AssertionError.
    """

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []
        self.waited: list[str | None] = []
        self.closed = False

    def page_html(self, url: str, *, wait_for: str | None = None, challenge_timeout: float = 30.0) -> str:
        self.calls.append(url)
        self.waited.append(wait_for)
        for key, value in self.routes.items():
            if key in url:
                if callable(value):
                    return value(url)
                if isinstance(value, str) and (FIXTURES / value).exists():
                    return fixture_text(value)
                return str(value)
        raise AssertionError(f"FakeBrowser: no route for {url}")

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _no_profile_leaks(monkeypatch):
    """The active profile is process state; no test may inherit one from another."""
    monkeypatch.delenv("JOBSCRAPER_PROFILE", raising=False)
    config.use_profile(None)
    yield
    config.use_profile(None)


@pytest.fixture(scope="session")
def settings():
    return load_settings(CONFIG_DIR)


@pytest.fixture
def make_ctx(settings) -> Callable[..., SourceContext]:
    def _make(
        routes: dict[Any, Any] | None = None,
        options: dict[str, Any] | None = None,
        limit: int | None = None,
        browser_routes: dict[str, Any] | None = None,
    ) -> SourceContext:
        """A SourceContext over :class:`FakeHttp`; ``browser_routes`` adds a :class:`FakeBrowser`.

        The browser factory hands out the same fake every time, so a test can read it back
        with ``ctx.browser()``.
        """
        browser = None
        if browser_routes is not None:
            fake = FakeBrowser(browser_routes)
            browser = lambda: fake  # noqa: E731 - the factory is one expression
        return SourceContext(
            http=FakeHttp(routes or {}),
            profile=settings.profile,
            options=options or {},
            limit=limit,
            browser=browser,
        )

    return _make


__all__ = ["FakeBrowser", "FakeHttp", "_raise_for_status", "fixture_json", "fixture_text"]
