"""Polite HTTP client shared by the source adapters.

- Browser-like User-Agent (many boards 403 the default python UA).
- Per-host minimum delay so a source can't hammer a site.
- Retries with backoff on 429/5xx and connection errors.
- Optional on-disk response cache (``JOBSCRAPER_HTTP_CACHE=1``) so repeated local runs
  and ``jobscraper probe`` don't re-hit the sites while you iterate on parsing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from jobscraper.config import DATA_DIR

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class SourceHTTPError(RuntimeError):
    """Raised when a source endpoint keeps failing; carries the last status code."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Http:
    def __init__(
        self,
        *,
        min_delay: float = 1.0,
        timeout: float = 30.0,
        retries: int = 3,
        cache_dir: Path | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.min_delay = min_delay
        self.retries = retries
        self._last_call: dict[str, float] = {}
        use_cache = os.environ.get("JOBSCRAPER_HTTP_CACHE") == "1"
        self.cache_dir = cache_dir or (DATA_DIR / "cache" if use_cache else None)
        base_headers = {
            "User-Agent": DEFAULT_UA,
            "Accept-Language": "en-US,en;q=0.9,fi;q=0.8,de;q=0.7",
        }
        if headers:
            base_headers.update(headers)
        self._client = httpx.Client(
            timeout=timeout, headers=base_headers, follow_redirects=True, http2=False
        )

    # ------------------------------------------------------------------ helpers
    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        last = self._last_call.get(host)
        if last is not None:
            wait = self.min_delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_call[host] = time.monotonic()

    def _cache_path(self, method: str, url: str, body: Any) -> Path | None:
        if not self.cache_dir:
            return None
        key = hashlib.sha1(f"{method} {url} {json.dumps(body, sort_keys=True, default=str)}".encode()).hexdigest()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir / f"{key}.json"

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        req = self._client.build_request(method, url, params=params, json=json_body, data=data, headers=headers)
        cache = self._cache_path(method, str(req.url), json_body or data)
        if cache and cache.exists():
            payload = json.loads(cache.read_text(encoding="utf-8"))
            headers = {k: v for k, v in payload["headers"].items() if k.lower() not in _TRANSFER_HEADERS}
            return httpx.Response(payload["status"], text=payload["text"], headers=headers, request=req)

        last_exc: Exception | None = None
        status: int | None = None
        for attempt in range(self.retries + 1):
            self._throttle(str(req.url))
            try:
                resp = self._client.send(req)
                status = resp.status_code
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    delay = float(resp.headers.get("Retry-After") or 2 ** attempt * 2)
                    log.warning("%s %s -> %s, retrying in %.0fs", method, req.url, resp.status_code, delay)
                    time.sleep(min(delay, 60))
                    continue
                if cache and resp.status_code < 400:
                    cache.write_text(
                        json.dumps({"status": resp.status_code, "text": resp.text, "headers": _cacheable_headers(resp)}),
                        encoding="utf-8",
                    )
                return resp
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < self.retries:
                    time.sleep(2**attempt * 2)
                    continue
        raise SourceHTTPError(f"{method} {url} failed: {last_exc}", status)

    def get(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("POST", url, **kw)

    def get_json(self, url: str, **kw: Any) -> Any:
        resp = self.get(url, **kw)
        _raise_for_status(resp)
        return resp.json()

    def post_json(self, url: str, **kw: Any) -> Any:
        resp = self.post(url, **kw)
        _raise_for_status(resp)
        return resp.json()

    def get_text(self, url: str, **kw: Any) -> str:
        resp = self.get(url, **kw)
        _raise_for_status(resp)
        return resp.text

    def close(self) -> None:
        self._client.close()


# The cache stores the *decoded* text, so the transfer-level headers must not be replayed
# (httpx would try to gunzip plain text and raise DecodingError).
_TRANSFER_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


def _cacheable_headers(resp: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in resp.headers.items() if k.lower() not in _TRANSFER_HEADERS}


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        hint = ""
        if resp.status_code == 403 and "cloudflare" in resp.text.lower():
            hint = " (Cloudflare challenge: this source needs a headless browser)"
        raise SourceHTTPError(f"{resp.request.method} {resp.request.url} -> HTTP {resp.status_code}{hint}", resp.status_code)


def strip_html(html: str | None) -> str | None:
    """HTML → readable plain text; keeps paragraph/list breaks."""
    if not html:
        return None
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(["p", "li", "div", "h1", "h2", "h3", "h4", "tr"]):
        tag.append("\n")
    return soup.get_text()
