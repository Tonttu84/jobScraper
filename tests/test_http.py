"""Tests for the shared HTTP client: cache replay must not re-decode already-decoded bodies."""

from __future__ import annotations

import gzip

import httpx
import pytest

from jobscraper import http as http_mod
from jobscraper.http import Http, SourceHTTPError


def _gzip_json_response(req: httpx.Request) -> httpx.Response:
    body = gzip.compress(b'{"hello": "world"}')
    return httpx.Response(
        200,
        content=body,
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip", "Content-Length": str(len(body))},
        request=req,
    )


def test_cache_replays_gzip_responses(tmp_path, monkeypatch):
    first = Http(min_delay=0, retries=0, cache_dir=tmp_path)
    monkeypatch.setattr(first._client, "send", _gzip_json_response)
    assert first.get_json("https://example.test/api") == {"hello": "world"}
    assert len(list(tmp_path.glob("*.json"))) == 1

    second = Http(min_delay=0, retries=0, cache_dir=tmp_path)

    def boom(req: httpx.Request) -> httpx.Response:
        raise AssertionError("cache miss: the network was touched")

    monkeypatch.setattr(second._client, "send", boom)
    resp = second.get("https://example.test/api")
    assert resp.status_code == 200
    assert resp.json() == {"hello": "world"}
    assert resp.headers["content-type"] == "application/json"
    assert "content-encoding" not in resp.headers


def test_extra_headers_are_merged_over_the_polite_defaults():
    http = Http(min_delay=0, retries=0, headers={"X-Api-Key": "secret", "User-Agent": "probe/1"})
    assert http._client.headers["x-api-key"] == "secret"
    assert http._client.headers["user-agent"] == "probe/1"  # the default UA is overridable
    assert "accept-language" in http._client.headers


def test_no_cache_dir_means_no_cache_lookup():
    http = Http(min_delay=0, retries=0)
    assert http.cache_dir is None
    assert http._cache_path("GET", "https://example.test/a", None) is None


def test_the_per_host_delay_is_waited_out(monkeypatch):
    """The throttle only sleeps for a host that was just called, and only for the remainder."""
    ticks = iter([100.0, 100.5, 100.5, 100.5])
    slept: list[float] = []
    monkeypatch.setattr(http_mod.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(http_mod.time, "sleep", slept.append)

    http = Http(min_delay=2.0, retries=0)
    http._throttle("https://example.test/a")
    http._throttle("https://example.test/b")  # same host, 0.5s later
    assert slept == [1.5]
    http._throttle("https://other.test/a")  # a different host waits for nothing
    assert slept == [1.5]


def test_a_server_error_is_retried_before_giving_up(monkeypatch):
    http = Http(min_delay=0, retries=2)
    seen: list[httpx.Request] = []

    def send(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(503, headers={"Retry-After": "1"}, text="down", request=req)
        return httpx.Response(200, json={"ok": True}, request=req)

    monkeypatch.setattr(http._client, "send", send)
    monkeypatch.setattr(http_mod.time, "sleep", lambda _s: None)
    assert http.get_json("https://example.test/api") == {"ok": True}
    assert len(seen) == 2


def test_an_error_response_is_never_cached(tmp_path, monkeypatch):
    http = Http(min_delay=0, retries=0, cache_dir=tmp_path)
    monkeypatch.setattr(
        http._client, "send", lambda req: httpx.Response(404, text="nope", request=req)
    )
    assert http.get("https://example.test/missing").status_code == 404
    assert list(tmp_path.glob("*.json")) == []


def test_a_dead_host_is_retried_and_then_reported(monkeypatch):
    http = Http(min_delay=0, retries=2)
    attempts: list[httpx.Request] = []
    slept: list[float] = []

    def send(req: httpx.Request) -> httpx.Response:
        attempts.append(req)
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(http._client, "send", send)
    monkeypatch.setattr(http_mod.time, "sleep", slept.append)
    with pytest.raises(SourceHTTPError) as err:
        http.get("https://example.test/api")

    assert len(attempts) == 3 and slept == [2, 4]
    assert err.value.status is None
    assert "no route to host" in str(err.value)
