"""Tests for the shared HTTP client: cache replay must not re-decode already-decoded bodies."""

from __future__ import annotations

import gzip
import threading

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


# --------------------------------------------------------------- thread safety


def test_two_threads_on_the_same_host_wait_the_delay_out(monkeypatch):
    """Sources fetch their boards from a thread pool: the polite delay must survive that."""
    clock = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds  # serialised by the host's lock, so this stays consistent

    monkeypatch.setattr(http_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(http_mod.time, "sleep", sleep)

    http = Http(min_delay=2.0, retries=0)
    threads = [
        threading.Thread(target=http._throttle, args=("https://example.test/a",))
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert slept == [2.0, 2.0]  # the first call waits for nothing, the two behind it do
    assert http._last_call == {"example.test": 104.0}


def test_the_same_host_is_throttled_one_thread_at_a_time(monkeypatch):
    """Two threads may not sit out the same host's delay at once — that halves it."""
    entered: list[str] = []
    sleeping = threading.Event()
    release = threading.Event()

    def sleep(_seconds: float) -> None:
        entered.append(threading.current_thread().name)
        sleeping.set()
        assert release.wait(10), "the throttled host was never released"

    monkeypatch.setattr(http_mod.time, "sleep", sleep)

    http = Http(min_delay=5.0, retries=0)
    http._throttle("https://example.test/a")  # primes the host: the next call has to wait

    first = threading.Thread(target=http._throttle, args=("https://example.test/b",))
    first.start()
    assert sleeping.wait(10)

    second = threading.Thread(target=http._throttle, args=("https://example.test/c",))
    second.start()
    second.join(0.3)
    assert entered == [first.name]  # the second thread queues behind, it does not overlap

    release.set()
    first.join(10)
    second.join(10)
    assert entered == [first.name, second.name]  # and then waits out its own delay


def test_a_sleeping_host_does_not_hold_up_another_one(monkeypatch):
    """One lock per host: waiting out example.test must not stall a call to other.test."""
    sleeping = threading.Event()
    release = threading.Event()

    def sleep(_seconds: float) -> None:
        sleeping.set()
        assert release.wait(10), "the throttled host was never released"

    monkeypatch.setattr(http_mod.time, "sleep", sleep)

    http = Http(min_delay=5.0, retries=0)
    http._throttle("https://example.test/a")  # primes the host: the next call has to wait

    slow = threading.Thread(target=http._throttle, args=("https://example.test/b",))
    slow.start()
    assert sleeping.wait(10)

    fast = threading.Thread(target=http._throttle, args=("https://other.test/a",))
    fast.start()
    fast.join(10)
    assert not fast.is_alive()  # a different host waits for nothing

    release.set()
    slow.join(10)
    assert set(http._last_call) == {"example.test", "other.test"}


def test_the_delay_map_stays_consistent_under_many_threads():
    hosts = [f"h{i}.test" for i in range(8)]
    http = Http(min_delay=0, retries=0)
    errors: list[Exception] = []

    def hammer(host: str) -> None:
        try:
            for _ in range(20):
                http._throttle(f"https://{host}/x")
        except Exception as exc:  # pragma: no cover - only reached if the map is unsafe
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(host,)) for host in hosts * 2]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert not errors
    assert set(http._last_call) == set(hosts)
