"""Tests for the shared HTTP client: cache replay must not re-decode already-decoded bodies."""

from __future__ import annotations

import gzip

import httpx

from jobscraper.http import Http


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
