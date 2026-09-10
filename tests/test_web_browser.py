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
from contextlib import contextmanager

import pytest
import uvicorn
from test_web import _seed, make_verdict  # tests/ is on sys.path (no package)

from jobscraper.store import Store
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


@contextmanager
def _serving(app):
    """Run ``app`` under uvicorn on a free localhost port in a thread; yields the base URL."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            thread.join(0.05)
        assert server.started, "uvicorn did not start"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(5)


@pytest.fixture
def site(tmp_path):
    """Seeded database served by uvicorn on a free localhost port; yields (base_url, jobs)."""
    db = tmp_path / "jobs.db"
    jobs = _seed(db)
    # ``languages`` is what ``serve`` passes from the profile: "pt" is offered although no
    # posting in the seeded report is Portuguese. No ``preset`` = the student default.
    with _serving(create_app(db, languages=["en", "pt"])) as base_url:
        yield base_url, jobs


@pytest.fixture
def tailored_site(tmp_path):
    """The same database served for a one-person profile (``web.preset: tailored``)."""
    db = tmp_path / "jobs.db"
    jobs = _seed(db)
    with _serving(create_app(db, languages=["en", "pt"], preset="tailored")) as base_url:
        yield base_url, jobs


@contextmanager
def _visiting(browser, base_url):
    """A fresh browser context (own localStorage) on ``base_url``; fails on any JS error."""
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


@pytest.fixture
def page(browser, site):
    """A fresh browser context (own localStorage) on the seeded site; fails on any JS error."""
    with _visiting(browser, site[0]) as page:
        yield page


@pytest.fixture
def tailored_page(browser, tailored_site):
    with _visiting(browser, tailored_site[0]) as page:
        yield page


@pytest.fixture
def refined_site(tmp_path):
    """The seeded database plus a refine verdict on the ranked job (the second AI pass)."""
    db = tmp_path / "jobs.db"
    jobs = _seed(db)
    store = Store(db)
    store.save_verdict(make_verdict(
        jobs["web"].id, "refine", 85, model="claude-fable-5-1", position=1,
        summary="The best React and Node fit of the shortlist, ahead of the two backend roles.",
    ))
    store.close()
    with _serving(create_app(db, languages=["en", "pt"])) as base_url:
        yield base_url, jobs


@pytest.fixture
def refined_page(browser, refined_site):
    with _visiting(browser, refined_site[0]) as page:
        yield page


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


def test_ranked_cards_show_their_rank_next_to_the_score(page, site):
    """The score alone does not say where a job sits in the report; ranked cards carry #N."""
    _, jobs = site
    expect(card(page, jobs["web"]).locator(".rank")).to_have_text("#1")
    # a screen survivor that was never ranked, and a rule-review leftover, get no rank badge
    expect(card(page, jobs["py"]).locator(".rank")).to_have_count(0)
    expect(card(page, jobs["rev"]).locator(".rank")).to_have_count(0)


def test_cards_say_how_far_the_pipeline_took_the_job(page, site):
    """Scrolling past the ranked jobs reaches ones the AI stages never finished with."""
    _, jobs = site
    expect(card(page, jobs["web"]).locator(".stage")).to_have_text("prefiltered and ranked")
    expect(card(page, jobs["py"]).locator(".stage")).to_have_text("prefiltered, not ranked")
    expect(card(page, jobs["rev"]).locator(".stage")).to_have_text("rules only, needs review")


def test_section_filter_uses_the_same_stage_words_as_the_cards(page):
    labels = page.locator("#f-sections label").all_text_contents()
    assert [x.strip() for x in labels] == ["ranked", "prefiltered only", "rules review only"]


def test_card_meta_line_and_tags(page, site):
    _, jobs = site
    web = card(page, jobs["web"])
    expect(web.locator(".meta")).to_contain_text("Reactive Oy")
    expect(web.locator(".meta")).to_contain_text("posted 2026-09-01")
    tags = web.locator(".tag").all_text_contents()
    assert "web" in tags and "en" in tags
    assert "prefiltered and ranked" not in tags  # the pipeline stage is not a job tag


def test_a_closing_posting_says_so_in_the_meta_line_and_wears_an_urgent_tag(page, site):
    """The seeded review leftover closes in three days; the ranked one states no deadline."""
    _, jobs = site
    rev = card(page, jobs["rev"])
    expect(rev.locator(".meta")).to_contain_text("closes in 3 days")
    expect(rev.locator(".tag.urgent")).to_have_text("closes in 3 days")
    web = card(page, jobs["web"])
    expect(web.locator(".meta")).not_to_contain_text("closes")
    expect(web.locator(".tag.urgent")).to_have_count(0)


def test_an_evergreen_advert_wears_a_tag_and_can_be_hidden(page, site):
    """The seeded review leftover is a talent pool; the box that hides it starts unticked."""
    _, jobs = site
    expect(card(page, jobs["rev"]).locator(".tag.evergreen")).to_have_text("evergreen advert")
    expect(card(page, jobs["web"]).locator(".tag.evergreen")).to_have_count(0)

    box = page.locator("#f-evergreen")
    expect(box).not_to_be_checked()
    expect(page.locator("label:has(#f-evergreen)")).to_contain_text("hide evergreen adverts (1)")
    box.check()
    expect(count(page)).to_have_text("2 of 2")
    expect(card(page, jobs["rev"])).to_have_count(0)
    page.locator("#reset").click()
    expect(box).not_to_be_checked()
    expect(count(page)).to_have_text("3 of 3")


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


def test_the_card_main_area_looks_clickable(page, site):
    _, jobs = site
    cursor = card(page, jobs["web"]).locator(".card-main").evaluate(
        "el => getComputedStyle(el).cursor")
    assert cursor == "pointer"


# ------------------------------------------------------------------ refine


def test_a_refined_card_explains_the_score_box_with_both_component_scores(refined_page, refined_site):
    """The box holds the mean of the two AI passes, so the card says what went into it."""
    base_url, jobs = refined_site
    web = card(refined_page, jobs["web"])
    expect(web.locator(".rank")).to_have_text("#1")
    expect(web.locator(".parts")).to_have_text("rank 91 · refine 85")
    payload = refined_page.request.get(f"{base_url}/api/jobs/{jobs['web'].id}").json()
    assert payload["score"] == 88  # round((91 + 85) / 2)
    expect(web.locator(".score")).to_have_text(str(payload["score"]))
    # a job the refine pass never saw has one score only, and nothing to explain
    expect(card(refined_page, jobs["py"]).locator(".parts")).to_have_count(0)


def test_a_card_without_a_refine_verdict_has_no_parts_line(page, site):
    _, jobs = site
    expect(card(page, jobs["web"]).locator(".score")).to_have_text("91")
    expect(card(page, jobs["web"]).locator(".parts")).to_have_count(0)


def test_the_detail_puts_the_refine_verdict_before_the_rank_summary(refined_page, refined_site):
    """The shortlist-wide judgement is the more specific one, so it is read first."""
    _, jobs = refined_site
    web = card(refined_page, jobs["web"])
    web.locator("button.toggle").click()
    detail = web.locator(".detail")
    expect(detail).to_be_visible()
    expect(detail).to_contain_text("Against the rest of the shortlist")
    expect(detail).to_contain_text("ahead of the two backend roles")
    expect(detail).to_contain_text("position 1 of the shortlist")
    # the rank block, and everything after it, is still there and still in its old order
    expect(detail).to_contain_text("Strong React and Node fit")
    expect(detail).to_contain_text("Why apply")
    headings = detail.locator("h4").all_text_contents()
    assert headings.index("Against the rest of the shortlist") < headings.index("Rank")
    assert headings[-1] == "Description"


# ------------------------------------------------------------------ details button


def test_details_button_is_first_in_the_actions_row_and_toggles_the_detail(page, site):
    _, jobs = site
    web = card(page, jobs["web"])
    detail, button = web.locator(".detail"), web.locator(".actions button.toggle")
    expect(web.locator(".actions > *").first).to_have_class("toggle")
    expect(button).to_have_text("Details ▾")
    expect(button).to_have_attribute("aria-expanded", "false")
    expect(detail).to_be_hidden()

    button.click()
    expect(detail).to_be_visible()
    expect(detail).to_contain_text("Why apply")
    expect(detail).to_contain_text("We build single page apps with React, Node.js and TypeScript.")
    expect(button).to_have_text("Hide ▴")
    expect(button).to_have_attribute("aria-expanded", "true")

    button.click()
    expect(detail).to_be_hidden()
    expect(button).to_have_text("Details ▾")
    expect(button).to_have_attribute("aria-expanded", "false")


def test_the_details_button_follows_a_detail_opened_by_clicking_the_card(page, site):
    _, jobs = site
    web = card(page, jobs["web"])
    web.locator(".meta").click()
    expect(web.locator(".detail")).to_be_visible()
    expect(web.locator("button.toggle")).to_have_text("Hide ▴")
    web.locator("button.toggle").click()
    expect(web.locator(".detail")).to_be_hidden()


def test_a_decision_saved_with_the_detail_open_keeps_it_open_and_the_label_right(page, site):
    _, jobs = site
    page.locator("#user").fill("tonttu")
    page.locator("#user").press("Enter")
    web = card(page, jobs["web"])
    web.locator("button.toggle").click()
    expect(web.locator(".detail")).to_be_visible()

    web.locator('button.act[data-status="applied"]').click()
    expect(web.locator("button.act.on")).to_have_attribute("data-status", "applied")
    expect(web.locator(".detail")).to_be_visible()
    expect(web.locator(".detail")).to_contain_text("Why apply")
    expect(web.locator("button.toggle")).to_have_text("Hide ▴")
    expect(web.locator("button.toggle")).to_have_attribute("aria-expanded", "true")
    # the re-rendered button still works
    web.locator("button.toggle").click()
    expect(web.locator(".detail")).to_be_hidden()
    expect(web.locator("button.toggle")).to_have_text("Details ▾")


# ------------------------------------------------------------------ teaser


def test_teaser_shows_the_first_sentence_of_the_ai_verdict(page, site):
    _, jobs = site
    web = card(page, jobs["web"])
    # the rank summary wins, cut at its first sentence
    expect(web.locator(".meta + .teaser")).to_have_text(
        "Strong React and Node fit for a junior developer…")
    # a prefilter-only job falls back to the prefilter summary, uncut when it is one sentence
    expect(card(page, jobs["py"]).locator(".teaser")).to_have_text(
        f"Summary for {jobs['py'].id}.")
    # nothing to say, no empty element
    expect(card(page, jobs["rev"]).locator(".teaser")).to_have_count(0)


def test_teaser_cuts_at_160_characters_when_the_first_sentence_is_longer(page):
    long_text = "word " * 60  # 300 characters with no sentence end
    cut = page.evaluate("text => teaserText(text)", long_text)
    assert cut.endswith("…") and len(cut) <= 161
    assert cut[:-1] in long_text
    # a sentence end past the limit does not save it
    late = ("x" * 200) + ". tail"
    assert page.evaluate("text => teaserText(text)", late) == ("x" * 160) + "…"
    # short single sentences are shown as they are
    assert page.evaluate("text => teaserText(text)", "All good.") == "All good."
    assert page.evaluate("text => teaserText(text)", "") == ""


# ------------------------------------------------------------------ languages


def test_the_profiles_languages_are_offered_even_when_unobserved(page, site):
    expect(lang(page, "pt")).to_have_count(1)
    expect(lang(page, "pt")).not_to_be_checked()
    expect(count(page)).to_have_text("3 of 3")  # an unticked box changes nothing


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


# ------------------------------------------------------------------ tailored preset


def test_tailored_preset_offers_only_the_profiles_languages_all_ticked(tailored_page):
    """One person, known languages: every box the profile lists, all ticked, nothing else."""
    expect(tailored_page.locator("#f-langs input")).to_have_count(2)
    expect(lang(tailored_page, "en")).to_be_checked()
    expect(lang(tailored_page, "pt")).to_be_checked()
    # "de" is observed in the data but not spoken, so it is not on offer here
    expect(lang(tailored_page, "de")).to_have_count(0)


def test_tailored_preset_hides_the_full_stack_open_box(tailored_page):
    expect(tailored_page.locator("label:has(#f-webdev)")).to_be_hidden()


def test_tailored_preset_never_sends_web_dev(tailored_page):
    # An "I have not done Full Stack Open" left in this browser by a student page must not leak
    # into a profile that never asks the question.
    tailored_page.evaluate("localStorage.setItem('jobscraper.fso', '0')")
    tailored_page.reload()
    expect(count(tailored_page)).to_contain_text(" of ")
    with tailored_page.expect_request(lambda r: "/api/jobs?" in r.url) as info:
        tailored_page.locator("#f-q").fill("dev")
    assert "web_dev" not in info.value.url
    expect(count(tailored_page)).to_contain_text(" of ")


def test_tailored_preset_reset_keeps_the_languages_ticked(tailored_page):
    tailored_page.locator("#f-q").fill("dev")
    tailored_page.locator("#reset").click()
    expect(tailored_page.locator("#f-q")).to_have_value("")
    expect(lang(tailored_page, "en")).to_be_checked()
    expect(lang(tailored_page, "pt")).to_be_checked()
    expect(tailored_page.locator("label:has(#f-webdev)")).to_be_hidden()


def test_tailored_preset_language_choice_still_survives_a_reload(tailored_page):
    """The stored choice wins over the preset's defaults, exactly as on the student page."""
    lang(tailored_page, "pt").uncheck()
    tailored_page.reload()
    expect(tailored_page.locator("#count")).to_contain_text(" of ")
    expect(lang(tailored_page, "pt")).not_to_be_checked()
    expect(lang(tailored_page, "en")).to_be_checked()


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
    # the same complaint next to the click, where the eye already is
    expect(web.locator(".actions .card-hint")).to_have_text("pick a handle first (top of the page)")
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


def test_inline_hint_is_per_card_never_doubled_and_goes_away_while_typing(page, site):
    _, jobs = site
    web = card(page, jobs["web"])
    py = card(page, jobs["py"])
    web.locator('button.act[data-status="applied"]').click()
    expect(web.locator(".card-hint")).to_have_count(1)
    expect(page.locator("#user")).to_be_focused()
    # only the card that was clicked complains
    expect(py.locator(".card-hint")).to_have_count(0)

    # clicking again on the same card replaces the hint instead of stacking a second one
    web.locator('button.act[data-status="skipped"]').click()
    expect(web.locator(".card-hint")).to_have_count(1)
    # a note left without a handle complains on its own card too
    py.locator("input.note").fill("maybe later")
    py.locator("input.note").press("Tab")
    expect(py.locator(".card-hint")).to_have_count(1)
    expect(page.locator(".card-hint")).to_have_count(2)

    # the handle arriving clears every inline hint while it is still being typed
    page.locator("#user").fill("tonttu")
    expect(page.locator(".card-hint")).to_have_count(0)
    expect(page.locator("#hint")).to_have_text("")


def test_a_handle_means_no_inline_hint_and_the_decision_saves(page, site):
    base_url, jobs = site
    page.locator("#user").fill("tonttu")
    page.locator("#user").press("Enter")
    web = card(page, jobs["web"])
    web.locator('button.act[data-status="applied"]').click()
    expect(web.locator("button.act.on")).to_have_attribute("data-status", "applied")
    expect(page.locator(".card-hint")).to_have_count(0)
    expect(page.locator("#hint")).to_have_text("")
    stored = page.request.get(f"{base_url}/api/jobs/{jobs['web'].id}?user=tonttu").json()
    assert stored["decision"]["status"] == "applied"


def test_a_saved_decision_clears_a_hint_left_on_that_card(page, site):
    _, jobs = site
    web = card(page, jobs["web"])
    web.locator('button.act[data-status="applied"]').click()
    expect(web.locator(".card-hint")).to_have_count(1)
    # a handle that arrives without an input event (restored from storage, pasted by the browser)
    page.evaluate("() => { document.getElementById('user').value = 'tonttu'; }")
    web.locator('button.act[data-status="applied"]').click()
    expect(web.locator("button.act.on")).to_have_attribute("data-status", "applied")
    expect(web.locator(".card-hint")).to_have_count(0)


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


# ------------------------------------------------------------------ country names


def options_of(page, select_id):
    return page.eval_on_selector(
        f"#{select_id}", "el => Array.from(el.options).map(o => o.value + '|' + o.textContent)")


def selected_of(page, select_id):
    return page.eval_on_selector(
        f"#{select_id}", "el => Array.from(el.selectedOptions).map(o => o.value)")


def test_country_options_show_names_with_counts_sorted_by_name(page):
    # the seed holds one job each in FI, DE, PT and SE — by name Finland comes before Germany
    assert options_of(page, "f-country") == [
        "FI|Finland (1)", "DE|Germany (1)", "PT|Portugal (1)", "SE|Sweden (1)"
    ]
    # the code is still the value the API gets
    assert page.evaluate("() => countryName('LU')") == "Luxembourg"
    assert page.evaluate("() => countryName('GB')") == "United Kingdom"
    assert page.evaluate("() => countryName('ZZ')") == "ZZ"  # unknown codes fall back


def test_choosing_a_country_by_its_name_filters_like_the_code_did(page, site):
    _, jobs = site
    page.locator("#f-country").select_option(label="Finland (1)")
    expect(count(page)).to_have_text("1 of 1")
    expect(card(page, jobs["web"])).to_have_count(1)
    assert selected_of(page, "f-country") == ["FI"]
    page.locator("#f-country").select_option([])
    expect(count(page)).to_have_text("3 of 3")


# ------------------------------------------------------------------ decision multi-select


DEFAULT_DECISIONS = ["none", "applied", "interested", "interview", "offer"]


def test_decision_multi_select_defaults_to_everything_but_skipped_and_rejected(page):
    expect(page.locator("#f-decision")).to_have_attribute("multiple", "")
    assert options_of(page, "f-decision") == [
        "none|no decision", "applied|applied", "skipped|skipped", "interested|interested",
        "interview|interview", "rejected|rejected", "offer|offer",
    ]
    assert selected_of(page, "f-decision") == DEFAULT_DECISIONS


def test_the_muted_line_explains_the_filter_needs_a_handle(page):
    note = page.locator("#decision-note")
    expect(note).to_have_text("applies once you pick a handle")
    page.locator("#user").fill("tonttu")
    expect(note).to_be_hidden()
    page.locator("#user").fill("")
    expect(note).to_be_visible()


def test_marking_a_job_skipped_hides_it_on_the_next_reload_but_not_under_the_cursor(page, site):
    _, jobs = site
    page.locator("#user").fill("tonttu")
    page.locator("#user").press("Enter")
    expect(count(page)).to_have_text("3 of 3")

    py = card(page, jobs["py"])
    py.locator('button.act[data-status="skipped"]').click()
    expect(py.locator("button.act.on")).to_have_attribute("data-status", "skipped")
    # the card is refreshed in place — it must not vanish while it is being clicked
    expect(py).to_have_count(1)
    expect(count(page)).to_have_text("3 of 3")

    page.reload()
    expect(count(page)).to_have_text("2 of 2")
    expect(card(page, jobs["py"])).to_have_count(0)

    # asking for only the skipped ones brings it back
    page.locator("#f-decision").select_option("skipped")
    expect(count(page)).to_have_text("1 of 1")
    expect(card(page, jobs["py"])).to_have_count(1)


def test_decision_selection_survives_a_reload_and_reset_restores_the_default(page):
    page.locator("#user").fill("tonttu")
    page.locator("#user").press("Enter")
    page.locator("#f-decision").select_option(["skipped", "rejected"])
    expect(count(page)).to_have_text("0 of 0")

    page.reload()
    assert selected_of(page, "f-decision") == ["skipped", "rejected"]
    expect(count(page)).to_have_text("0 of 0")

    page.locator("#reset").click()
    assert selected_of(page, "f-decision") == DEFAULT_DECISIONS
    expect(count(page)).to_have_text("3 of 3")
    # and the restored default is what a reload brings back
    page.reload()
    assert selected_of(page, "f-decision") == DEFAULT_DECISIONS
