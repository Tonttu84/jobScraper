"""Browser tests for the web UI (``src/jobscraper/web/static/index.html``).

The API tests in :mod:`test_web` cannot see the page's JavaScript, which is where the filter
wiring and the card rendering live, so these drive the real page in headless Chromium through
Playwright against a uvicorn server running in a thread over the seeded database. They skip when
Playwright's browser is not installed (``python -m playwright install chromium``).
No network beyond 127.0.0.1, no AI calls.
"""

from __future__ import annotations

import socket
import threading

import pytest
import uvicorn
from test_web import _seed  # tests/ is on sys.path (no package)

from jobscraper.web.app import create_app

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except playwright_api.Error as exc:  # browser binary missing
            pytest.skip(f"chromium not installed for playwright: {exc}")
        yield browser
        browser.close()


@pytest.fixture
def site(tmp_path):
    """Seeded database served by uvicorn on a free localhost port; yields (base_url, jobs)."""
    db = tmp_path / "jobs.db"
    jobs = _seed(db)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(db), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        thread.join(0.05)
    assert server.started, "uvicorn did not start"
    yield f"http://127.0.0.1:{port}", jobs
    server.should_exit = True
    thread.join(5)


@pytest.fixture
def page(browser, site):
    """A fresh browser context (own localStorage) on the seeded site; fails on any JS error."""
    base_url, _ = site
    context = browser.new_context(base_url=base_url)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.goto("/")
    expect(page.locator("#count")).to_contain_text(" of ")
    yield page
    context.close()
    assert errors == [], f"browser errors: {errors}"


def card(page, job):
    return page.locator(f'.card[data-id="{job.id}"]')


def lang(page, code):
    return page.locator(f'#f-langs input[value="{code}"]')


def count(page):
    return page.locator("#count")


# ------------------------------------------------------------------ rendering


def test_page_shows_the_report_and_the_jobs_the_viewer_can_read(page, site):
    _, jobs = site
    expect(page.locator("#report-info")).to_contain_text("report #")
    expect(lang(page, "en")).to_be_checked()
    expect(lang(page, "de")).not_to_be_checked()
    # the German-only C++ job is hidden because only English is ticked
    expect(count(page)).to_have_text("3 of 3")
    web = card(page, jobs["web"])
    expect(web.locator("h3 a")).to_have_text(jobs["web"].title)
    expect(web.locator("h3 a")).to_have_attribute("href", jobs["web"].url)
    expect(web.locator(".score")).to_have_text("91")
    expect(card(page, jobs["cpp"])).to_have_count(0)


def test_card_meta_line_and_tags(page, site):
    _, jobs = site
    web = card(page, jobs["web"])
    expect(web.locator(".meta")).to_contain_text("Reactive Oy")
    expect(web.locator(".meta")).to_contain_text("posted 2026-09-01")
    tags = web.locator(".tag").all_text_contents()
    assert "web" in tags and "en" in tags and "ranked" in tags


def test_detail_opens_on_click_with_rank_summary_and_description(page, site):
    _, jobs = site
    web = card(page, jobs["web"])
    detail = web.locator(".detail")
    expect(detail).to_be_hidden()
    web.locator(".meta").click()
    expect(detail).to_be_visible()
    expect(detail).to_contain_text("Why apply")
    expect(detail).to_contain_text("React and Node work")
    expect(detail).to_contain_text("Work rights")
    expect(detail).to_contain_text("We build single page apps with React, Node.js and TypeScript.")
    web.locator(".meta").click()
    expect(detail).to_be_hidden()


# ------------------------------------------------------------------ languages


def test_ticking_a_language_reloads_the_list_without_a_page_reload(page, site):
    _, jobs = site
    lang(page, "de").check()
    expect(count(page)).to_have_text("4 of 4")
    expect(card(page, jobs["cpp"])).to_have_count(1)
    lang(page, "de").uncheck()
    expect(count(page)).to_have_text("3 of 3")
    expect(card(page, jobs["cpp"])).to_have_count(0)


def test_required_language_is_red_only_when_the_viewer_does_not_speak_it(page, site):
    _, jobs = site
    lang(page, "de").check()
    badge = card(page, jobs["cpp"]).locator(".tag", has_text="de required")
    expect(badge).to_have_count(1)
    expect(badge).to_have_class("tag")
    # nothing ticked: no language filter, and the unmet requirement is highlighted
    lang(page, "de").uncheck()
    lang(page, "en").uncheck()
    expect(count(page)).to_have_text("4 of 4")
    badge = card(page, jobs["cpp"]).locator(".tag", has_text="de required")
    expect(badge).to_have_class("tag req")
    # postings without an unmet requirement never carry a red tag
    expect(card(page, jobs["web"]).locator(".tag.req")).to_have_count(0)


def test_language_choice_survives_a_reload(page, site):
    _, jobs = site
    lang(page, "de").check()
    expect(count(page)).to_have_text("4 of 4")
    page.reload()
    expect(lang(page, "de")).to_be_checked()
    expect(count(page)).to_have_text("4 of 4")
    expect(card(page, jobs["cpp"])).to_have_count(1)


# ------------------------------------------------------------------ Full Stack Open


def test_full_stack_open_is_on_by_default_and_unticking_hides_web_jobs(page, site):
    _, jobs = site
    box = page.locator("#f-webdev")
    expect(page.locator("label:has(#f-webdev)")).to_contain_text("I have done Full Stack Open")
    expect(box).to_be_checked()
    expect(card(page, jobs["web"])).to_have_count(1)
    box.uncheck()
    expect(count(page)).to_have_text("2 of 2")
    expect(card(page, jobs["web"])).to_have_count(0)
    expect(card(page, jobs["py"])).to_have_count(1)
    box.check()
    expect(count(page)).to_have_text("3 of 3")


def test_full_stack_open_choice_survives_a_reload_and_reset(page, site):
    _, jobs = site
    page.locator("#f-webdev").uncheck()
    expect(count(page)).to_have_text("2 of 2")
    page.reload()
    expect(page.locator("#f-webdev")).not_to_be_checked()
    expect(count(page)).to_have_text("2 of 2")
    page.locator("#reset").click()
    expect(page.locator("#f-webdev")).not_to_be_checked()
    expect(count(page)).to_have_text("2 of 2")
    expect(card(page, jobs["web"])).to_have_count(0)


# ------------------------------------------------------------------ other filters


def test_stack_chip_search_section_score_and_reset(page, site):
    _, jobs = site
    chip = page.locator('#f-stacks .chip[data-value="web"]')
    chip.click()
    expect(chip).to_have_class("chip on")
    expect(count(page)).to_have_text("1 of 1")
    expect(card(page, jobs["web"])).to_have_count(1)
    chip.click()
    expect(count(page)).to_have_text("3 of 3")

    page.locator("#f-q").fill("python")
    expect(count(page)).to_have_text("1 of 1")
    expect(card(page, jobs["py"])).to_have_count(1)
    page.locator("#f-q").fill("")
    expect(count(page)).to_have_text("3 of 3")

    page.locator('#f-sections input[value="review"]').uncheck()
    expect(count(page)).to_have_text("2 of 2")
    expect(card(page, jobs["rev"])).to_have_count(0)

    page.locator("#f-score").fill("90")
    expect(count(page)).to_have_text("1 of 1")

    page.locator("#reset").click()
    expect(count(page)).to_have_text("3 of 3")
    expect(page.locator('#f-sections input[value="review"]')).to_be_checked()
    expect(page.locator("#f-score")).to_have_value("")
    # profile facts are not filters: languages stay as they were
    expect(lang(page, "en")).to_be_checked()


def test_no_match_message(page):
    page.locator("#f-q").fill("zzz-no-such-job")
    expect(count(page)).to_have_text("0 of 0")
    expect(page.locator("#list")).to_contain_text("Nothing matches these filters.")


# ------------------------------------------------------------------ decisions


def test_decision_needs_a_handle_then_round_trips_through_the_ui(page, site):
    base_url, jobs = site
    web = card(page, jobs["web"])
    web.locator('button.act[data-status="applied"]').click()
    expect(page.locator("#hint")).to_have_text("pick a handle first")
    expect(web.locator("button.act.on")).to_have_count(0)

    page.locator("#user").fill("tonttu")
    page.locator("#user").press("Enter")
    expect(page.locator("#hint")).to_have_text("")
    web.locator('button.act[data-status="applied"]').click()
    expect(web.locator("button.act.on")).to_have_attribute("data-status", "applied")
    expect(web.locator(".badge")).to_have_text("applied")

    stored = page.request.get(f"{base_url}/api/jobs/{jobs['web'].id}?user=tonttu").json()
    assert stored["decision"]["status"] == "applied"

    # a note saved on blur keeps the current status
    web.locator("input.note").fill("sent CV on Monday")
    web.locator("input.note").press("Tab")
    expect(web.locator("input.note")).to_have_value("sent CV on Monday")
    expect(web.locator("button.act.on")).to_have_attribute("data-status", "applied")
    stored = page.request.get(f"{base_url}/api/jobs/{jobs['web'].id}?user=tonttu").json()
    assert (stored["decision"]["status"], stored["decision"]["note"]) == ("applied", "sent CV on Monday")

    # the handle and the decision are still there after a reload
    page.reload()
    expect(page.locator("#user")).to_have_value("tonttu")
    expect(card(page, jobs["web"]).locator("button.act.on")).to_have_attribute("data-status", "applied")

    # clear removes it
    card(page, jobs["web"]).locator('button.act[data-status=""]').click()
    expect(card(page, jobs["web"]).locator("button.act.on")).to_have_count(0)
    expect(card(page, jobs["web"]).locator(".badge")).to_have_count(0)


def test_decision_filter_shows_only_the_users_marked_jobs(page, site):
    _, jobs = site
    page.locator("#user").fill("tonttu")
    page.locator("#user").press("Enter")
    card(page, jobs["py"]).locator('button.act[data-status="interested"]').click()
    expect(card(page, jobs["py"]).locator("button.act.on")).to_have_count(1)
    page.locator("#f-decision").select_option("interested")
    expect(count(page)).to_have_text("1 of 1")
    expect(card(page, jobs["py"])).to_have_count(1)
    page.locator("#f-decision").select_option("none")
    expect(count(page)).to_have_text("2 of 2")
    expect(card(page, jobs["py"])).to_have_count(0)
