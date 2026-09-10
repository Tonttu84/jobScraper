"""Unit tests for the headless-browser fallback (:mod:`jobscraper.browser`).

Chromium is never launched here: a fake ``playwright.sync_api`` module is injected into
``sys.modules`` (Playwright is imported lazily *inside* the methods precisely so this works),
and ``jobscraper.browser.time`` is replaced by a fake clock so the challenge polling and the
per-host throttle can be tested without waiting. The one test that starts a real browser is
opt-in and skips when the Chromium build is missing, like ``tests/test_web_browser.py``.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from jobscraper import browser as browser_mod
from jobscraper.browser import Browser, BrowserBlocked, BrowserFactory, BrowserUnavailable
from jobscraper.http import SourceHTTPError

CHALLENGE = (
    "<html><head><title>Just a moment...</title></head>"
    "<body><div id='cf-chl-widget'></div></body></html>"
)
PAGE = "<html><head><title>Jobs</title></head><body><h1>Developer</h1></body></html>"

CHALLENGE_STEP = ("Just a moment...", CHALLENGE)
PAGE_STEP = ("Jobs", PAGE)


class FakeClock:
    """Stand-in for the ``time`` module: sleeping moves the clock instead of blocking."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []
        self.on_sleep = None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += max(seconds, 0.001)
        if self.on_sleep is not None:
            self.on_sleep()


class FakePage:
    def __init__(self, ctx: FakeContext) -> None:
        self.ctx = ctx
        self.closed = False

    def goto(self, url: str, **kw) -> None:
        self.ctx.goto_calls.append((url, kw))

    def title(self) -> str:
        return self.ctx.step()[0]

    def content(self) -> str:
        return self.ctx.step()[1]

    def wait_for_selector(self, selector: str, **kw) -> None:
        self.ctx.waited.append(selector)

    def close(self) -> None:
        self.closed = True


class FakeContext:
    """One persistent context; ``steps`` are (title, html) snapshots, the last one repeats."""

    def __init__(self, steps, **kwargs) -> None:
        self.steps = list(steps)
        self.kwargs = kwargs
        self.index = 0
        self.pages: list[FakePage] = []
        self.goto_calls: list[tuple[str, dict]] = []
        self.waited: list[str] = []
        self.default_timeout: float | None = None
        self.closed = False

    def step(self) -> tuple[str, str]:
        return self.steps[min(self.index, len(self.steps) - 1)]

    def advance(self) -> None:
        self.index += 1

    def new_page(self) -> FakePage:
        page = FakePage(self)
        self.pages.append(page)
        return page

    def set_default_timeout(self, ms: float) -> None:
        self.default_timeout = ms

    def close(self) -> None:
        self.closed = True


class FakePlaywright:
    def __init__(self, steps) -> None:
        self.steps = steps
        self.contexts: list[FakeContext] = []
        self.stopped = False
        self.chromium = SimpleNamespace(launch_persistent_context=self._launch)

    def _launch(self, user_data_dir, **kwargs) -> FakeContext:
        ctx = FakeContext(self.steps, user_data_dir=user_data_dir, **kwargs)
        self.contexts.append(ctx)
        return ctx

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(browser_mod, "time", fake)
    return fake


@pytest.fixture
def fake_pw(monkeypatch):
    """install(steps) → the FakePlaywright that :mod:`jobscraper.browser` will import."""

    def install(steps) -> FakePlaywright:
        pw = FakePlaywright(steps)
        module = SimpleNamespace(
            sync_playwright=lambda: SimpleNamespace(start=lambda: pw), Error=RuntimeError
        )
        monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
        return pw

    return install


# ------------------------------------------------------------------ normal fetch


def test_page_html_opens_a_persistent_context_and_returns_the_html(fake_pw, clock, tmp_path):
    pw = fake_pw([PAGE_STEP])
    with Browser(user_data_dir=tmp_path / "profile", min_delay=0) as browser:
        assert browser.page_html("https://www.jobly.fi/tyopaikat") == PAGE

    ctx = pw.contexts[0]
    assert ctx.kwargs["user_data_dir"] == str(tmp_path / "profile")
    assert (tmp_path / "profile").is_dir()  # the profile survives between runs
    assert ctx.kwargs["headless"] is True
    assert "Mozilla/5.0" in ctx.kwargs["user_agent"]
    assert ctx.kwargs["viewport"] == {"width": 1280, "height": 900}
    assert ctx.default_timeout == 45_000
    url, kw = ctx.goto_calls[0]
    assert url == "https://www.jobly.fi/tyopaikat"
    assert kw["wait_until"] == "domcontentloaded"
    assert ctx.pages[0].closed is True  # the tab is not leaked
    assert ctx.waited == []


def test_wait_for_selector_is_awaited_before_reading_the_html(fake_pw, clock, tmp_path):
    pw = fake_pw([PAGE_STEP])
    with Browser(user_data_dir=tmp_path, min_delay=0) as browser:
        assert browser.page_html("https://x.test/a", wait_for=".job-list") == PAGE
    assert pw.contexts[0].waited == [".job-list"]


def test_headless_false_is_passed_through(fake_pw, clock, tmp_path):
    pw = fake_pw([PAGE_STEP])
    with Browser(user_data_dir=tmp_path, headless=False, min_delay=0) as browser:
        browser.page_html("https://x.test/a")
    assert pw.contexts[0].kwargs["headless"] is False


def test_the_default_profile_lives_under_the_data_directory(fake_pw, clock, tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSCRAPER_DATA_DIR", str(tmp_path / "data"))
    pw = fake_pw([PAGE_STEP])
    with Browser(min_delay=0) as browser:
        browser.page_html("https://x.test/a")
    assert pw.contexts[0].kwargs["user_data_dir"] == str(tmp_path / "data" / "browser")


# ------------------------------------------------------------------ Cloudflare challenge


def test_a_challenge_that_clears_is_waited_out(fake_pw, clock, tmp_path):
    pw = fake_pw([CHALLENGE_STEP, PAGE_STEP])
    browser = Browser(user_data_dir=tmp_path, min_delay=0)
    clock.on_sleep = lambda: pw.contexts[0].advance()  # the second poll sees the real page
    assert browser.page_html("https://x.test/a") == PAGE
    assert clock.slept, "the challenge was not polled"
    browser.close()


def test_a_challenge_that_never_clears_raises_browser_blocked(fake_pw, clock, tmp_path):
    pw = fake_pw([CHALLENGE_STEP])
    browser = Browser(user_data_dir=tmp_path, min_delay=0)
    with pytest.raises(BrowserBlocked) as excinfo:
        browser.page_html("https://x.test/a", challenge_timeout=5)
    assert excinfo.value.status == 403
    assert isinstance(excinfo.value, SourceHTTPError)
    assert "https://x.test/a" in str(excinfo.value)
    assert pw.contexts[0].pages[0].closed is True
    browser.close()


def test_a_page_that_only_carries_the_challenge_script_is_a_challenge(fake_pw, clock, tmp_path):
    script = "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1'></script>"
    fake_pw([("Loading", f"<html><body>{script}</body></html>")])
    browser = Browser(user_data_dir=tmp_path, min_delay=0)
    with pytest.raises(BrowserBlocked):
        browser.page_html("https://x.test/a", challenge_timeout=2)
    browser.close()


def test_a_page_that_refuses_to_report_its_title_is_judged_by_its_body(
    fake_pw, clock, tmp_path, monkeypatch
):
    """A page navigating under you raises on ``title()``; that must not fail the fetch."""
    fake_pw([PAGE_STEP])

    def boom(self) -> str:
        raise RuntimeError("Execution context was destroyed")

    monkeypatch.setattr(FakePage, "title", boom)
    with Browser(user_data_dir=tmp_path, min_delay=0) as browser:
        assert browser.page_html("https://x.test/a") == PAGE


# ------------------------------------------------------------------ availability


def test_missing_playwright_explains_how_to_install_it(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    browser = Browser(user_data_dir=tmp_path, min_delay=0)
    with pytest.raises(BrowserUnavailable) as excinfo:
        browser.page_html("https://x.test/a")
    message = str(excinfo.value)
    assert "uv sync --extra dev" in message
    assert "playwright install chromium" in message


# ------------------------------------------------------------------ throttle / reuse


def test_requests_to_the_same_host_are_throttled(fake_pw, clock, tmp_path):
    fake_pw([PAGE_STEP])
    with Browser(user_data_dir=tmp_path, min_delay=5.0) as browser:
        browser.page_html("https://x.test/a")
        assert clock.slept == []  # nothing to wait for on the first call
        browser.page_html("https://x.test/b")
        assert clock.slept and clock.slept[0] == pytest.approx(5.0, abs=0.01)
        browser.page_html("https://other.test/a")  # a different host is not throttled
        assert len(clock.slept) == 1


def test_one_context_is_reused_and_closed_once(fake_pw, clock, tmp_path):
    pw = fake_pw([PAGE_STEP])
    browser = Browser(user_data_dir=tmp_path, min_delay=0)
    browser.page_html("https://x.test/a")
    browser.page_html("https://x.test/b")
    assert len(pw.contexts) == 1
    assert len(pw.contexts[0].pages) == 2

    browser.close()
    assert pw.contexts[0].closed is True
    assert pw.stopped is True
    browser.close()  # idempotent
    assert len(pw.contexts) == 1


def test_closing_an_unused_browser_does_nothing(fake_pw, clock, tmp_path):
    pw = fake_pw([PAGE_STEP])
    Browser(user_data_dir=tmp_path, min_delay=0).close()
    assert pw.contexts == []


# ------------------------------------------------------------------ factory


def test_browser_factory_creates_one_browser_lazily(fake_pw, clock, tmp_path):
    pw = fake_pw([PAGE_STEP])
    factory = BrowserFactory(user_data_dir=tmp_path, min_delay=0)
    assert pw.contexts == []  # nothing started yet
    first = factory()
    first.page_html("https://x.test/a")
    assert factory() is first
    assert len(pw.contexts) == 1

    factory.close()
    assert pw.contexts[0].closed is True
    factory.close()  # idempotent, and a fresh browser can still be made afterwards
    assert factory() is not first


# ------------------------------------------------------------------ opt-in smoke test


def test_real_chromium_renders_a_data_url(tmp_path):
    """The only test that starts a browser; skipped when the Chromium build is missing."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    browser = Browser(user_data_dir=tmp_path / "profile", min_delay=0, timeout=15)
    try:
        html = browser.page_html("data:text/html,<h1 id=x>hei</h1>", wait_for="#x")
    except playwright_api.Error as exc:  # browser binary missing
        pytest.skip(f"chromium not installed for playwright: {exc}")
    finally:
        browser.close()
    assert "hei" in html
