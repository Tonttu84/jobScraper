"""Shared test helpers: a fake HTTP client that serves fixture files, and a SourceContext factory."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

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


@pytest.fixture(scope="session")
def settings():
    return load_settings(CONFIG_DIR)


@pytest.fixture
def make_ctx(settings) -> Callable[..., SourceContext]:
    def _make(routes: dict[Any, Any] | None = None, options: dict[str, Any] | None = None, limit: int | None = None) -> SourceContext:
        return SourceContext(http=FakeHttp(routes or {}), profile=settings.profile, options=options or {}, limit=limit)

    return _make


__all__ = ["FakeHttp", "_raise_for_status", "fixture_json", "fixture_text"]
