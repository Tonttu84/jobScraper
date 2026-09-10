"""The source context's HTTP → headless-browser fallback (``SourceContext.page``)."""

from __future__ import annotations

import httpx
import pytest

from jobscraper.http import SourceHTTPError
from jobscraper.sources._common import is_cloudflare_challenge

URL = "https://www.jobly.fi/tyopaikka/1"
PAGE = "<html><body><h1>Developer</h1></body></html>"
CHALLENGE = (
    "<html><head><title>Just a moment...</title></head>"
    "<body><script src='/cdn-cgi/challenge-platform/x.js'></script></body></html>"
)
HINT = "(Cloudflare challenge: this source needs a headless browser)"


def status(code: int, text: str = "cloudflare"):
    return lambda req: httpx.Response(code, text=text)


# ------------------------------------------------------------------ the detector


def test_is_cloudflare_challenge_looks_for_the_interstitial_not_the_word():
    assert is_cloudflare_challenge(CHALLENGE)
    assert is_cloudflare_challenge("<title>Just a moment…</title>")
    assert is_cloudflare_challenge("<div id='cf-chl-widget'></div>")
    assert not is_cloudflare_challenge(PAGE)
    assert not is_cloudflare_challenge("<p>You will work with Cloudflare Workers.</p>")
    assert not is_cloudflare_challenge("")
    assert not is_cloudflare_challenge(None)


# ------------------------------------------------------------------ auto mode


def test_auto_uses_plain_http_when_the_page_comes_back(make_ctx):
    ctx = make_ctx({"jobly.fi": PAGE}, browser_routes={"jobly.fi": "<html>browser</html>"})
    assert ctx.page(URL) == PAGE
    assert ctx.browser().calls == []
    assert ctx.browser_calls == 0


@pytest.mark.parametrize("code", [403, 503])
def test_auto_falls_back_to_the_browser_on_a_blocked_status(make_ctx, code, caplog):
    ctx = make_ctx({"jobly.fi": status(code)}, browser_routes={"jobly.fi": PAGE})
    with caplog.at_level("INFO"):
        assert ctx.page(URL) == PAGE
    assert ctx.browser().calls == [URL]
    assert ctx.browser_calls == 1
    assert any("browser" in r.message.lower() for r in caplog.records)


def test_auto_falls_back_when_a_200_is_really_a_challenge_page(make_ctx):
    ctx = make_ctx({"jobly.fi": CHALLENGE}, browser_routes={"jobly.fi": PAGE})
    assert ctx.page(URL) == PAGE
    assert ctx.browser().calls == [URL]
    assert ctx.browser_calls == 1


def test_auto_does_not_fall_back_on_other_errors(make_ctx):
    ctx = make_ctx({"jobly.fi": status(404, "gone")}, browser_routes={"jobly.fi": PAGE})
    with pytest.raises(SourceHTTPError) as excinfo:
        ctx.page(URL)
    assert excinfo.value.status == 404
    assert ctx.browser().calls == []


def test_wait_for_is_handed_to_the_browser(make_ctx):
    ctx = make_ctx({"jobly.fi": status(403)}, browser_routes={"jobly.fi": PAGE})
    ctx.page(URL, wait_for=".job")
    assert ctx.browser().waited == [".job"]


# ------------------------------------------------------------------ explicit modes


def test_browser_mode_never_makes_a_plain_request(make_ctx):
    # FakeHttp with no routes raises on any call, so this fails loudly if HTTP is touched.
    ctx = make_ctx({}, browser_routes={"jobly.fi": PAGE})
    assert ctx.page(URL, mode="browser") == PAGE
    assert ctx.http.calls == []
    assert ctx.browser_calls == 1


def test_http_mode_never_falls_back(make_ctx):
    ctx = make_ctx({"jobly.fi": status(403)}, browser_routes={"jobly.fi": PAGE})
    with pytest.raises(SourceHTTPError):
        ctx.page(URL, mode="http")
    assert ctx.browser().calls == []
    assert ctx.browser_calls == 0


def test_http_mode_returns_a_challenge_page_as_it_is(make_ctx):
    """``http`` means "no browser, ever" — the caller decides what to do with the page."""
    ctx = make_ctx({"jobly.fi": CHALLENGE}, browser_routes={"jobly.fi": PAGE})
    assert ctx.page(URL, mode="http") == CHALLENGE
    assert ctx.browser().calls == []


def test_an_unknown_mode_is_a_programming_error(make_ctx):
    ctx = make_ctx({"jobly.fi": PAGE})
    with pytest.raises(ValueError, match="mode"):
        ctx.page(URL, mode="magic")


# ------------------------------------------------------------------ no browser configured


def test_without_a_browser_factory_the_status_error_carries_the_hint(make_ctx):
    ctx = make_ctx({"jobly.fi": status(403, "denied")})
    with pytest.raises(SourceHTTPError) as excinfo:
        ctx.page(URL)
    assert HINT in str(excinfo.value)
    assert excinfo.value.status == 403


def test_the_hint_is_not_repeated_when_the_client_already_added_it(make_ctx):
    ctx = make_ctx({"jobly.fi": status(403, "cloudflare")})
    with pytest.raises(SourceHTTPError) as excinfo:
        ctx.page(URL)
    assert str(excinfo.value).count(HINT) == 1


def test_without_a_browser_factory_a_challenge_page_raises_the_same_hint(make_ctx):
    ctx = make_ctx({"jobly.fi": CHALLENGE})
    with pytest.raises(SourceHTTPError) as excinfo:
        ctx.page(URL)
    assert HINT in str(excinfo.value)
    assert excinfo.value.status == 403


def test_browser_mode_without_a_factory_says_so(make_ctx):
    ctx = make_ctx({})
    with pytest.raises(SourceHTTPError) as excinfo:
        ctx.page(URL, mode="browser")
    assert HINT in str(excinfo.value)
