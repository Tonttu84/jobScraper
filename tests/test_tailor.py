"""The tailor step: one Claude call per job that picks CV parts by id, never writes CV text.

Runs against the generic fixture ``tests/fixtures/cv-parts.yaml``; the owner's own library is
never read. ``messages.parse`` is monkeypatched, as in test_refine.py — nothing touches the API.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from jobscraper import cli as cli_mod
from jobscraper.config import ROOT
from jobscraper.cv import measure as measure_mod
from jobscraper.cv import render as render_mod
from jobscraper.cv import tailor as tailor_mod
from jobscraper.cv.fit import Trim
from jobscraper.cv.parts import load_cv_parts, parts_fingerprint
from jobscraper.cv.tailor import (
    TAILOR_PROMPT_VERSION,
    PickedEntry,
    StoredAnswer,
    Tailor,
    TailorAnswer,
    TailorError,
    answer_problems,
    export_prompt,
    library_block,
    tailor_system_prompt,
    tailor_user_prompt,
    to_handoff_markdown,
    to_selection,
)
from jobscraper.models import AIVerdict, Decision, FilterResult, Job, ReportItem, ReportSnapshot
from jobscraper.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "cv-parts.yaml"
runner = CliRunner()


@pytest.fixture
def parts(tmp_path):
    shutil.copyfile(FIXTURE, tmp_path / "cv-parts.yaml")
    return load_cv_parts(tmp_path)


def _job(**kw) -> Job:
    fields = dict(source="test", source_id="1", url="https://example.test/1", title="C++ Developer",
                  company="Acme", country="FI", description="Low-latency C++ networking. English.")
    fields.update(kw)
    return Job(**fields)


def _answer(**kw) -> TailorAnswer:
    fields = dict(
        focus="Systems C++ work on networking code.",
        summary="systems",
        projects=[PickedEntry(id="webserver", bullets=["poll", "parse"]),
                  PickedEntry(id="shell", bullets=["pipes"])],
        work=[PickedEntry(id="poker", bullets=["math"])],
        rationale="Leads with the server because the role is networking.",
        gaps=["Kubernetes"],
    )
    fields.update(kw)
    return TailorAnswer(**fields)


def _usage() -> SimpleNamespace:
    return SimpleNamespace(input_tokens=5000, output_tokens=400, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0)


def _response(parsed, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason, usage=_usage())


class FakeMessages:
    """Hands out the queued answers in order and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


@pytest.fixture
def make_tailor(settings, parts, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    store = Store(tmp_path / "tailor.db")

    def _make(*responses, model: str | None = None, fit=None,
              heights=None) -> tuple[Tailor, FakeMessages]:
        t = Tailor(settings.profile, store, parts, model=model, fit=fit, heights=heights)
        fake = FakeMessages(*responses)
        t.client = SimpleNamespace(messages=fake)
        return t, fake

    return _make


# ------------------------------------------------------------------------ prompt


def test_library_block_lists_every_id_with_its_limits(parts):
    block = library_block(parts)
    assert "version test-1" in block
    assert "[default]" in block and "[systems]" in block
    assert "[webserver] Webserver" in block and "[shell] Shell" in block and "[game] Raycaster" in block
    assert "up to 3 bullets" in block  # shell's own max_bullets
    assert "- [parse] Wrote an incremental request parser." in block
    assert "[poker] Poker professional (2010-2024)" in block
    assert "- [service] Served customers." in block
    assert "2 projects" in block
    # The untailored CV is marked so the model knows what "no change" looks like.
    assert "on the untailored CV" in block


def test_library_block_without_heights_has_no_budget(parts):
    block = library_block(parts)
    assert "Page budget" not in block and " mm" not in block
    assert library_block(parts, None) == block


def test_library_block_shows_every_height_and_the_budget(parts, flat_heights):
    heights = flat_heights(parts, mm=2.0, overhead_mm=100.0)
    webserver = heights.projects["webserver"].model_copy(update={"header": 6.14})
    heights = heights.model_copy(update={"projects": {**heights.projects, "webserver": webserver}})
    block = library_block(parts, heights)
    budget = ("Page budget for summary + projects + work: 197.0 mm; the untailored CV uses "
              "24.1 mm.")
    assert budget in block and block.index(budget) < block.index("SUMMARIES")
    assert "[default] tags: general — 2.0 mm" in block
    assert "[webserver] Webserver (on the untailored CV) — 6.1 mm — tags: systems, cpp;" in block
    assert "  - [parse] Wrote an incremental request parser. (on the untailored CV) (2.0 mm)" in block
    assert "  - [cgi] Ran CGI scripts. (2.0 mm)" in block
    assert "[poker] Poker professional (2010-2024) — 2.0 mm; up to 2 bullets" in block
    assert "  - [service] Served customers. (2.0 mm)" in block
    # Everything else reads as without heights.
    assert library_block(parts).splitlines()[0] == block.splitlines()[0]


def test_system_prompt_asks_for_the_budget_and_a_drop_order(settings, parts, flat_heights):
    prompt = tailor_system_prompt(settings.profile, parts, flat_heights(parts))
    assert "PAGE BUDGET" in prompt and "`drop_order`" in prompt
    assert "it never asks you again" in prompt
    assert "The page is full." not in prompt and "Page budget for summary" in prompt
    assert TAILOR_PROMPT_VERSION == "2026-10-22.1"


def test_answer_schema_ranks_the_expendables_after_choosing():
    assert list(TailorAnswer.model_fields) == ["focus", "summary", "projects", "work", "drop_order",
                                               "rationale", "gaps"]
    assert _answer().drop_order == []


def test_system_prompt_carries_the_library_and_the_dossier(settings, parts):
    profile = settings.profile.model_copy(update={"dossier": "Wrote the webserver core himself."})
    prompt = tailor_system_prompt(profile, parts)
    assert library_block(parts) in prompt
    assert "Wrote the webserver core himself." in prompt
    assert "ids only" in prompt
    assert profile.prompt.role_label.strip() in prompt


def test_system_prompt_without_a_dossier_has_no_dossier_block(settings, parts):
    profile = settings.profile.model_copy(update={"dossier": ""})
    assert "DOSSIER" not in tailor_system_prompt(profile, parts)


def test_user_prompt_adds_the_rankers_notes_when_there_are_some():
    job = _job()
    fr = FilterResult(job_id=job.id, status="keep")
    verdict = AIVerdict(job_id=job.id, stage="rank", model="m", prompt_version="v", relevant=True,
                        score=80, language_ok=True, seniority_ok=True, location_ok=True,
                        summary="Good fit.", concerns=["No Kubernetes."],
                        why_apply=["Lead with the webserver."])
    prompt = tailor_user_prompt(job, fr, 6000, verdict)
    assert "Title: C++ Developer" in prompt
    assert "RANKER'S NOTES" in prompt
    assert "- Lead with the webserver." in prompt and "- No Kubernetes." in prompt
    assert "RANKER'S NOTES" not in tailor_user_prompt(job, fr, 6000, None)
    only_concerns = verdict.model_copy(update={"why_apply": []})
    prompt = tailor_user_prompt(job, fr, 6000, only_concerns)
    assert "Concerns:" in prompt and "Why apply" not in prompt
    only_why = verdict.model_copy(update={"concerns": []})
    prompt = tailor_user_prompt(job, fr, 6000, only_why)
    assert "Why apply" in prompt and "Concerns:" not in prompt


# --------------------------------------------------------------- answer → selection


def test_to_selection_maps_the_answer(parts):
    selection = to_selection(_answer())
    assert selection.summary == "systems"
    assert [(p.id, p.bullets) for p in selection.projects] == [("webserver", ["poll", "parse"]),
                                                               ("shell", ["pipes"])]
    assert selection.work == {"poker": ["math"]}
    assert answer_problems(parts, _answer()) == []


def test_answer_problems_lists_bad_ids_and_repeated_work_entries(parts):
    answer = _answer(summary="nope",
                     projects=[PickedEntry(id="webserver", bullets=["poll", "ghost"])],
                     work=[PickedEntry(id="poker", bullets=["math"]),
                           PickedEntry(id="poker", bullets=["risk"])])
    problems = answer_problems(parts, answer)
    assert "unknown summary 'nope'" in problems
    assert "project 'webserver': unknown bullet 'ghost'" in problems
    assert "work entry 'poker' listed twice" in problems


# --------------------------------------------------------------------- the call


def test_tailor_picks_parts_and_caches_them(make_tailor, parts):
    t, fake = make_tailor(_response(_answer()))
    job = _job()
    result = t.tailor(job, None)
    assert result.selection.summary == "systems"
    assert result.cv.projects[0].title == "Webserver"
    assert result.cv.projects[0].bullets == ["Served many clients with poll().",
                                             "Wrote an incremental request parser."]
    assert result.rationale.startswith("Leads with the server")
    assert result.gaps == ["Kubernetes"] and not result.cached
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["output_format"] is TailorAnswer
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Title: C++ Developer" in call["messages"][0]["content"]

    again, fake2 = make_tailor()  # no answers queued: a second API call would fail
    cached = again.tailor(job, None)
    assert cached.cached and cached.selection == result.selection and fake2.calls == []


def test_force_asks_again(make_tailor):
    t, _ = make_tailor(_response(_answer()))
    t.tailor(_job(), None)
    t2, fake = make_tailor(_response(_answer(summary="default")))
    assert t2.tailor(_job(), None, force=True).selection.summary == "default"
    assert len(fake.calls) == 1


def test_cache_is_keyed_on_the_library_fingerprint(make_tailor, parts, settings, tmp_path):
    t, _ = make_tailor(_response(_answer()))
    t.tailor(_job(), None)
    changed = parts.model_copy(update={"version": "test-2"})
    t2 = Tailor(settings.profile, t.store, changed)
    fake = FakeMessages(_response(_answer(summary="default")))
    t2.client = SimpleNamespace(messages=fake)
    assert t2.tailor(_job(), None).selection.summary == "default"
    assert t2.cache_key == f"{parts_fingerprint(changed)}|{TAILOR_PROMPT_VERSION}|{t2.model}"


def test_a_bad_answer_is_sent_back_once_with_its_problems(make_tailor):
    bad = _answer(projects=[PickedEntry(id="webserver", bullets=["ghost"])])
    t, fake = make_tailor(_response(bad), _response(_answer()))
    result = t.tailor(_job(), None)
    assert result.selection.projects[0].bullets == ["poll", "parse"]
    assert len(fake.calls) == 2
    retry = fake.calls[1]["messages"]
    assert [m["role"] for m in retry] == ["user", "assistant", "user"]
    assert "unknown bullet 'ghost'" in retry[2]["content"]
    assert '"ghost"' in retry[1]["content"]  # the model sees its own answer


def test_two_bad_answers_raise(make_tailor):
    bad = _answer(summary="nope")
    t, _ = make_tailor(_response(bad), _response(bad))
    with pytest.raises(TailorError, match="unknown summary 'nope'"):
        t.tailor(_job(), None)


def test_a_refusal_raises(make_tailor):
    t, _ = make_tailor(_response(None, stop_reason="refusal"))
    with pytest.raises(TailorError, match="refusal"):
        t.tailor(_job(), None)


def test_thinking_is_left_to_opus_and_set_for_other_models(make_tailor):
    t, fake = make_tailor(_response(_answer()), model="claude-opus-5")
    t.tailor(_job(), None)
    assert "thinking" not in fake.calls[0]
    t2, fake2 = make_tailor(_response(_answer()), model="claude-sonnet-5")
    t2.tailor(_job(source_id="2"), None)
    assert fake2.calls[0]["thinking"] == {"type": "adaptive"}


# ---------------------------------------------------------------------- page fit


class FakeFit:
    """Stands in for ``render.measure_overflow``: hands out the queued overflows in mm."""

    def __init__(self, *overflows: float):
        self.overflows = list(overflows)
        self.calls: list = []

    def __call__(self, cv) -> float:
        self.calls.append(cv)
        return self.overflows.pop(0)


def _overflowing(parts, flat_heights, overflow: float):
    """Heights (10 mm per part) under which ``_answer()`` overflows the page by ``overflow``."""
    heights = flat_heights(parts, mm=10.0)
    content = heights.content_mm(parts, to_selection(_answer()))
    return heights.model_copy(update={"overhead_mm": 297.0 - content + overflow})


def test_without_a_fit_check_nothing_is_measured(make_tailor):
    t, _ = make_tailor(_response(_answer()))
    assert t.tailor(_job(), None).overflow_mm is None


def test_without_heights_the_fit_check_only_measures(make_tailor):
    fit = FakeFit(12.3)
    t, fake = make_tailor(_response(_answer()), fit=fit)
    result = t.tailor(_job(), None)
    assert result.overflow_mm == 12.3 and result.trimmed == []
    assert len(fake.calls) == 1 and len(fit.calls) == 1
    assert fit.calls[0].summary == "A systems-flavoured summary."  # the answer's own CV


def test_an_overflowing_answer_is_trimmed_without_asking_again(make_tailor, parts, flat_heights):
    fit = FakeFit(0.0)
    answer = _answer(drop_order=["poker/math", "webserver/parse"])
    t, fake = make_tailor(_response(answer), fit=fit,
                          heights=_overflowing(parts, flat_heights, 8.0))
    result = t.tailor(_job(), None)
    assert len(fake.calls) == 1  # one AI answer is enough
    assert result.selection.projects[0].bullets == ["poll"]  # poker keeps its only bullet
    assert result.trimmed == [Trim(kind="project", entry="webserver", bullet="parse")]
    assert result.overflow_mm == 0.0 and len(fit.calls) == 1
    assert result.cv.projects[0].bullets == ["Served many clients with poll()."]
    stored = t.store.tailoring(_job().id, t.cache_key)
    # cv-render reads the fitted selection; the AI's own picks stay in the stored answer.
    assert TailorAnswer.model_validate_json(stored).projects[0].bullets == ["poll"]
    kept = StoredAnswer.model_validate_json(stored)
    assert kept.original_projects[0].bullets == ["poll", "parse"]
    assert kept.trimmed == result.trimmed and kept.drop_order == ["poker/math", "webserver/parse"]


def test_a_cached_tailoring_keeps_what_was_trimmed(make_tailor, parts, flat_heights):
    heights = _overflowing(parts, flat_heights, 8.0)
    t, _ = make_tailor(_response(_answer()), heights=heights)
    first = t.tailor(_job(), None)
    fit = FakeFit()
    again, _ = make_tailor(fit=fit, heights=heights)
    cached = again.tailor(_job(), None)
    assert cached.cached and fit.calls == []  # not measured again
    assert cached.trimmed == first.trimmed != []
    assert cached.selection == first.selection


def test_an_answer_that_fits_is_stored_as_it_came(make_tailor, parts, flat_heights):
    t, _ = make_tailor(_response(_answer()), fit=FakeFit(0.0),
                       heights=_overflowing(parts, flat_heights, 0.0))
    result = t.tailor(_job(), None)
    assert result.trimmed == [] and result.selection == to_selection(_answer())
    kept = StoredAnswer.model_validate_json(t.store.tailoring(_job().id, t.cache_key))
    assert kept.original_projects is None and kept.trimmed == []


def test_a_page_that_cannot_be_fitted_is_stored_with_its_overflow(make_tailor, parts, flat_heights):
    fit = FakeFit(5.0, 5.0, 5.0)
    t, fake = make_tailor(_response(_answer()), fit=fit,
                          heights=_overflowing(parts, flat_heights, 0.0))
    result = t.tailor(_job(), None)
    assert result.overflow_mm == 5.0 and len(fake.calls) == 1
    assert t.store.tailoring(_job().id, t.cache_key) is not None  # no TailorError any more


def test_an_answer_with_bad_ids_is_not_measured(make_tailor):
    fit = FakeFit(0.0)
    bad = _answer(summary="nope")
    t, fake = make_tailor(_response(bad), _response(_answer()), fit=fit)
    t.tailor(_job(), None)
    assert len(fit.calls) == 1  # only the second, valid answer
    assert "overflows" not in fake.calls[1]["messages"][2]["content"]


def test_a_cached_tailoring_is_not_measured_again(make_tailor):
    t, _ = make_tailor(_response(_answer()), fit=FakeFit(0.0))
    t.tailor(_job(), None)
    fit = FakeFit()
    again, _ = make_tailor(fit=fit)
    assert again.tailor(_job(), None).cached and fit.calls == []


def test_import_trims_an_overflowing_answer_instead_of_rejecting_it(make_tailor, parts,
                                                                   flat_heights):
    fit = FakeFit(0.0)
    t, _ = make_tailor(fit=fit, heights=_overflowing(parts, flat_heights, 8.0))
    result = t.import_answer(_job(), _answer(drop_order=["webserver/poll"]).model_dump_json())
    assert result.trimmed == [Trim(kind="project", entry="webserver", bullet="poll")]
    assert result.selection.projects[0].bullets == ["parse"]
    assert t.store.tailoring(_job().id, t.cache_key) is not None


def test_import_accepts_an_answer_that_fits(make_tailor):
    t, _ = make_tailor(fit=FakeFit(0.0))
    result = t.import_answer(_job(), _answer().model_dump_json())
    assert result.overflow_mm == 0.0
    assert t.store.tailoring(_job().id, t.cache_key) is not None


def test_drop_order_entries_that_name_no_selected_bullet_are_noted(make_tailor, parts,
                                                                  flat_heights):
    t, _ = make_tailor(heights=_overflowing(parts, flat_heights, 0.0))
    answer = _answer(drop_order=["webserver/cgi", "ghost/x", "poker/math"])
    result = t.import_answer(_job(), answer.model_dump_json())
    assert result.notes == ['drop_order "webserver/cgi" is not a selected bullet; ignored',
                            'drop_order "ghost/x" is not a selected bullet; ignored']
    changes = _changes(to_handoff_markdown(_job(), result, parts))
    assert '- Note: drop_order "ghost/x" is not a selected bullet; ignored' in changes


def test_handoff_says_what_was_trimmed(make_tailor, parts, flat_heights):
    t, _ = make_tailor(heights=_overflowing(parts, flat_heights, 25.0))
    result = t.import_answer(_job(), _answer(drop_order=["webserver/parse"]).model_dump_json())
    # parse, then the fallback: no project has two bullets left, no work entry two, so a project.
    assert [x.label for x in result.trimmed] == ["webserver/parse", "shell"]
    changes = _changes(to_handoff_markdown(_job(), result, parts))
    assert ('- Trimmed automatically to fit the page: "Wrote an incremental request parser."; '
            "project **Shell**") in changes
    plain = make_tailor()[0].import_answer(_job(source_id="2"), _answer().model_dump_json())
    assert "Trimmed automatically" not in to_handoff_markdown(_job(source_id="2"), plain, parts)


def test_handoff_shortens_long_trimmed_bullets(parts, settings, tmp_path, flat_heights):
    raw = parts.model_dump()
    raw["projects"][0]["bullets"][1]["text"] = "Served many clients with poll() " + "and more " * 9
    long_parts = type(parts).model_validate(raw)
    heights = flat_heights(long_parts, mm=10.0)
    content = heights.content_mm(long_parts, to_selection(_answer()))
    heights = heights.model_copy(update={"overhead_mm": 297.0 - content + 5.0})
    t = Tailor(settings.profile, Store(tmp_path / "trim.db"), long_parts, heights=heights)
    result = t.import_answer(_job(), _answer(drop_order=["webserver/poll"]).model_dump_json())
    line = next(x for x in to_handoff_markdown(_job(), result, long_parts).splitlines()
                if x.startswith("- Trimmed automatically"))
    assert line.endswith('…"') and len(line) < 110


def test_tailor_error_problems():
    assert TailorError("one\ntwo").problems == ["one"]
    assert TailorError("head:\nx", ["x"]).problems == ["x"]


# -------------------------------------------------------- subagent path (no API key)


def test_export_prompt_has_everything_a_subagent_needs(make_tailor, tmp_path):
    t, fake = make_tailor()
    job = _job()
    text = export_prompt(t, job, None, None, tmp_path / "answer.json")
    assert t.system in text
    assert "Title: C++ Developer" in text
    assert '"rationale"' in text and '"projects"' in text  # the JSON schema of the answer
    assert str(tmp_path / "answer.json") in text
    assert fake.calls == []


def test_import_answer_validates_and_caches(make_tailor):
    t, fake = make_tailor()
    job = _job()
    result = t.import_answer(job, _answer().model_dump_json())
    assert result.selection.summary == "systems" and not result.cached
    assert t.store.tailoring(job.id, t.cache_key) is not None
    assert fake.calls == []


def test_import_answer_lists_every_problem(make_tailor):
    t, _ = make_tailor()
    bad = _answer(summary="nope", projects=[PickedEntry(id="ghost", bullets=["x"])])
    with pytest.raises(TailorError) as exc:
        t.import_answer(_job(), bad.model_dump_json())
    assert "unknown summary 'nope'" in str(exc.value) and "unknown project 'ghost'" in str(exc.value)


def test_import_answer_rejects_malformed_json(make_tailor):
    t, _ = make_tailor()
    with pytest.raises(TailorError, match="not a valid answer"):
        t.import_answer(_job(), '{"summary": "systems"}')


def test_the_client_is_only_built_when_the_api_is_used(settings, parts, tmp_path, monkeypatch):
    built = []
    monkeypatch.setattr(tailor_mod, "_client", lambda: built.append(1) or SimpleNamespace(
        messages=FakeMessages(_response(_answer()))))
    t = Tailor(settings.profile, Store(tmp_path / "lazy.db"), parts)
    t.import_answer(_job(), _answer().model_dump_json())
    assert built == []
    t.tailor(_job(source_id="9"), None)
    assert built == [1]


# ---------------------------------------------------------------------- hand-off


def test_handoff_markdown_has_the_job_the_steps_and_the_cv(make_tailor, parts):
    t, _ = make_tailor(_response(_answer()))
    job = _job()
    md = to_handoff_markdown(job, t.tailor(job, None), parts)
    assert md.startswith("# Tailored CV: C++ Developer — Acme\n")
    assert "https://example.test/1" in md and job.id in md
    assert "Leads with the server" in md
    assert "- Kubernetes" in md
    # The Canva steps name what to put where, in order.
    assert "Profile: **systems**" in md
    assert "1. **Webserver**" in md and "2. **Shell**" in md
    assert "Poker professional" in md
    # Then the full CV text.
    assert "## SAMPLE PROJECTS" in md and "- Served many clients with poll()." in md
    assert "PDF:" not in md


def _changes(md: str) -> str:
    """The "Changes from your standard CV" section, which sits right before the Canva steps."""
    rest = md.split("## Changes from your standard CV\n", 1)[1]
    return rest.split("## Into the Canva layout", 1)[0]


def test_handoff_lists_the_changes_from_the_standard_cv(make_tailor, parts):
    t, _ = make_tailor()
    md = to_handoff_markdown(_job(), t.import_answer(_job(), _answer().model_dump_json()), parts)
    changes = _changes(md)
    assert "- Summary: `default` → `systems`" in changes
    assert "- Projects in: **Shell** (`shell`)" in changes
    assert "- Projects out: **Raycaster** (`game`)" in changes
    assert ('- **Poker professional**: + "Used probability daily."; − "Kept detailed records."'
            in changes)
    assert "Shop assistant" not in changes  # left out of the answer: its default bullets


def test_handoff_changes_when_nothing_changed(make_tailor, parts):
    t, _ = make_tailor()
    same = _answer(summary="default",
                   projects=[PickedEntry(id="webserver", bullets=["parse", "poll"]),
                             PickedEntry(id="game", bullets=["dda"])],
                   work=[PickedEntry(id="poker", bullets=["discipline"]),
                         PickedEntry(id="shop", bullets=["service"])])
    changes = _changes(to_handoff_markdown(_job(), t.import_answer(_job(), same.model_dump_json()),
                                           parts))
    assert "- Summary: unchanged (`default`)" in changes
    assert "- Projects: the standard ones\n" in changes
    assert "- Work bullets: unchanged" in changes
    reordered = same.model_copy(update={"projects": list(reversed(same.projects))})
    changes = _changes(to_handoff_markdown(
        _job(), t.import_answer(_job(), reordered.model_dump_json()), parts))
    assert "- Projects: the standard ones, reordered" in changes


def test_handoff_changes_shorten_long_bullets_and_show_removed_work(parts, settings, tmp_path):
    raw = parts.model_dump()
    raw["work"][0]["bullets"][1]["text"] = "Used probability daily " + "and very carefully " * 6
    long_parts = type(parts).model_validate(raw)
    t = Tailor(settings.profile, Store(tmp_path / "long.db"), long_parts)
    answer = _answer(work=[PickedEntry(id="poker", bullets=["discipline", "math"]),
                           PickedEntry(id="shop", bullets=[])])
    md = to_handoff_markdown(_job(), t.import_answer(_job(), answer.model_dump_json()), long_parts,
                             pdf_path=tmp_path / "cv.pdf")
    changes = _changes(md)
    line = next(x for x in changes.splitlines() if x.startswith("- **Poker professional**"))
    added = line.split('+ "', 1)[1].split('"', 1)[0]
    assert added.endswith("…") and len(added) <= 60
    assert "−" not in line
    assert '- **Shop assistant**: − "Served customers."' in changes
    assert f"- PDF: {tmp_path / 'cv.pdf'}" in md


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copyfile(ROOT / "config" / "profile.example.yaml", config_dir / "profile.yaml")
    shutil.copyfile(ROOT / "config" / "sources.yaml", config_dir / "sources.yaml")
    shutil.copyfile(FIXTURE, config_dir / "cv-parts.yaml")
    monkeypatch.setenv("JOBSCRAPER_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("JOBSCRAPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("JOBSCRAPER_PROFILE", raising=False)
    db = tmp_path / "jobs.db"
    monkeypatch.setenv("JOBSCRAPER_DB", str(db))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    store = Store(db)
    job = _job()
    store.upsert_jobs([job])
    store.close()
    fake = FakeMessages(_response(_answer()))
    monkeypatch.setattr(tailor_mod, "_client", lambda: SimpleNamespace(messages=fake))
    # The PDF step is covered by test_cv_render*.py; here it must not start a browser.
    def render_pdf(html, pdf, png=None):
        Path(pdf).write_bytes(b"%PDF")
        return render_mod.RenderResult(pages=1, overflow_mm=0.0)

    monkeypatch.setattr(render_mod, "render_pdf", render_pdf)
    measured: list = []
    monkeypatch.setattr(render_mod, "measure_overflow", lambda cv: measured.append(cv) or 0.0)
    return SimpleNamespace(job=job, fake=fake, data=tmp_path / "data", config=config_dir,
                           db=db, measured=measured)


@pytest.fixture(autouse=True)
def _no_real_heights(monkeypatch, flat_heights):
    """The CLI builds the part-height cache with a browser; here every part is 1 mm."""
    calls: list = []

    def measure(parts, *, fonts=True):
        calls.append(parts)
        return flat_heights(parts, fonts=fonts)

    monkeypatch.setattr(measure_mod, "measure_heights", measure)
    return calls


def test_cli_tailor_prints_and_saves_the_handoff(cli_env):
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id])
    assert result.exit_code == 0, result.output
    assert "# Tailored CV: C++ Developer — Acme" in result.output
    saved = list((cli_env.data / "cv" / "tailored").glob("*.md"))
    assert [p.name for p in saved] == ["CV - Acme.md"]
    assert "Profile: **systems**" in saved[0].read_text(encoding="utf-8")
    assert len(cli_env.fake.calls) == 1


def test_cli_tailor_accepts_a_unique_id_prefix(cli_env):
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id[:8]])
    assert result.exit_code == 0, result.output


def test_cli_tailor_unknown_job(cli_env):
    result = runner.invoke(cli_mod.app, ["tailor", "doesnotexist"])
    assert result.exit_code == 1
    assert "no job" in result.output


def test_cli_tailor_ambiguous_prefix(cli_env):
    store = Store(Path(os.environ["JOBSCRAPER_DB"]))
    store.upsert_jobs([_job(source_id=str(n)) for n in range(2, 40)])
    store.close()
    result = runner.invoke(cli_mod.app, ["tailor", ""])
    assert result.exit_code == 1
    assert "matches several jobs" in result.output


def test_cli_tailor_without_a_library(cli_env):
    (cli_env.config / "cv-parts.yaml").unlink()
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id])
    assert result.exit_code == 1
    assert "cv-parts.yaml" in result.output


def test_cli_tailor_reports_a_failed_answer(cli_env):
    cli_env.fake.responses[:] = [_response(None, stop_reason="refusal")]
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id])
    assert result.exit_code == 1
    assert "refusal" in result.output


def test_cli_tailor_export_then_import(cli_env):
    job_id = cli_env.job.id
    result = runner.invoke(cli_mod.app, ["tailor", job_id, "--export"])
    assert result.exit_code == 0, result.output
    prompt = cli_env.data / "cv" / "tailored" / "prompts" / f"{job_id}.md"
    answer = cli_env.data / "cv" / "tailored" / "answers" / f"{job_id}.json"
    assert prompt.is_file() and str(prompt) in result.output
    assert str(answer) in prompt.read_text(encoding="utf-8")
    assert cli_env.fake.calls == []

    missing = runner.invoke(cli_mod.app, ["tailor", job_id, "--import"])
    assert missing.exit_code == 1 and "no answer" in missing.output

    answer.write_text(_answer(summary="nope").model_dump_json(), encoding="utf-8")
    bad = runner.invoke(cli_mod.app, ["tailor", job_id, "--import"])
    assert bad.exit_code == 1 and "unknown summary 'nope'" in bad.output

    answer.write_text(_answer().model_dump_json(), encoding="utf-8")
    good = runner.invoke(cli_mod.app, ["tailor", job_id, "--import"])
    assert good.exit_code == 0, good.output
    assert "Profile: **systems**" in good.output
    assert cli_env.fake.calls == []
    # Imported answers are cached like API ones: a plain call afterwards needs no request.
    again = runner.invoke(cli_mod.app, ["tailor", job_id, "--model", "subagent"])
    assert again.exit_code == 0 and "(cached)" in again.output and cli_env.fake.calls == []


def test_cli_tailor_export_and_import_together_is_an_error(cli_env):
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id, "--export", "--import"])
    assert result.exit_code == 1 and "either" in result.output


def test_cli_tailor_export_puts_the_heights_in_the_prompt(cli_env, _no_real_heights):
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id, "--export"])
    assert result.exit_code == 0, result.output
    prompt = (cli_env.data / "cv" / "tailored" / "prompts" / f"{cli_env.job.id}.md").read_text(
        encoding="utf-8")
    assert "Page budget for summary + projects + work: 197.0 mm" in prompt
    assert "[webserver] Webserver (on the untailored CV) — 1.0 mm" in prompt
    assert len(list((cli_env.data / "cv").glob("heights-*.json"))) == 1
    assert len(_no_real_heights) == 1


def test_cli_tailor_export_without_a_browser_has_no_budget(cli_env, monkeypatch):
    def missing(parts, *, fonts=True):
        raise render_mod.RenderUnavailable("no chromium here")

    monkeypatch.setattr(measure_mod, "measure_heights", missing)
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id, "--export"])
    assert result.exit_code == 0, result.output
    assert "WARNING: no part heights in the prompt: no chromium here" in result.output
    prompt = (cli_env.data / "cv" / "tailored" / "prompts" / f"{cli_env.job.id}.md").read_text(
        encoding="utf-8")
    assert "Page budget for" not in prompt


def test_cli_tailor_no_pdf_reads_the_heights_but_never_measures(cli_env, _no_real_heights):
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id, "--export", "--no-pdf"])
    assert result.exit_code == 0, result.output
    assert _no_real_heights == []
    prompt = cli_env.data / "cv" / "tailored" / "prompts" / f"{cli_env.job.id}.md"
    assert "Page budget for" not in prompt.read_text(encoding="utf-8")
    assert runner.invoke(cli_mod.app, ["cv-heights"]).exit_code == 0  # builds the cache
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id, "--export", "--no-pdf"])
    assert result.exit_code == 0, result.output
    assert "Page budget for" in prompt.read_text(encoding="utf-8")
    assert len(_no_real_heights) == 1


def test_cli_tailor_checks_the_page_fit(cli_env):
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id])
    assert result.exit_code == 0, result.output
    assert len(cli_env.measured) == 1
    no_pdf = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id, "--no-pdf", "--force"])
    assert no_pdf.exit_code == 1  # the one queued answer is spent; the point is no measuring
    assert len(cli_env.measured) == 1


# ------------------------------------------------------------------ CLI batches


@pytest.fixture
def batch_env(cli_env, monkeypatch):
    """Three jobs, two stored reports (the latest one counts) and a wide console, so the summary
    table never wraps a cell."""
    jobs = [cli_env.job, _job(source_id="2", title="Go Developer", company="Beta"),
            _job(source_id="3", title="Data [Platform] Engineer", company="Gamma")]
    store = Store(cli_env.db)
    store.upsert_jobs(jobs[1:])

    def ranked(*order):
        return [ReportItem(job_id=j.id, section="ranked", position=n, score=90 - n)
                for n, j in enumerate(order, start=1)]

    store.save_report(ReportSnapshot(days=30, prompt_version="v", items=ranked(*jobs)))
    store.save_report(ReportSnapshot(days=30, prompt_version="v", items=[
        *ranked(jobs[2], jobs[0], jobs[1]),
        ReportItem(job_id="prefilteronly", section="prefilter", position=1, score=99)]))
    store.close()
    monkeypatch.setattr(cli_mod, "console", Console(width=250))
    cli_env.jobs = jobs
    return cli_env


def _decide(env, *rows: tuple[str, str, str]) -> None:
    store = Store(env.db, decisions_path=env.data / "serve" / "decisions.db")
    for job_id, user, status in rows:
        store.save_decision(Decision(job_id=job_id, user=user, status=status))
    store.close()


def _row(output: str, job: Job) -> str:
    """The summary table's line for ``job`` (its short id is the first column)."""
    return next(line for line in output.splitlines() if job.id[:8] in line)


def test_cli_tailor_needs_exactly_one_job_source(batch_env):
    for args in ([], [batch_env.job.id, "--top", "2"], ["--interested", "--top", "1"]):
        result = runner.invoke(cli_mod.app, ["tailor", *args])
        assert result.exit_code == 1, args
        assert "exactly one of" in result.output


def test_cli_tailor_batch_measures_the_heights_once(batch_env, _no_real_heights):
    batch_env.fake.responses.append(_response(_answer()))
    result = runner.invoke(cli_mod.app, ["tailor", "--top", "2"])
    assert result.exit_code == 0, result.output
    assert len(batch_env.fake.calls) == 2 and len(_no_real_heights) == 1
    assert "Page budget for" in batch_env.fake.calls[0]["system"][0]["text"]


def test_cli_tailor_top_n_follows_the_latest_report(batch_env):
    jobs = batch_env.jobs
    batch_env.fake.responses.append(_response(_answer()))
    result = runner.invoke(cli_mod.app, ["tailor", "--top", "2"])
    assert result.exit_code == 0, result.output
    asked = [call["messages"][0]["content"] for call in batch_env.fake.calls]
    assert "Data [Platform] Engineer" in asked[0] and "C++ Developer" in asked[1]
    assert len(asked) == 2
    # A batch prints the summary only; the hand-offs are still written.
    assert "# Tailored CV" not in result.output
    assert len(list((batch_env.data / "cv" / "tailored").glob("*.md"))) == 2
    row = _row(result.output, jobs[2])
    assert "Data [Platform] Engineer" in row and "Gamma" in row and "tailored" in row
    assert "0.0" in row and "CV - Gamma.pdf" in row
    assert result.output.index(jobs[2].id[:8]) < result.output.index(jobs[0].id[:8])
    assert jobs[1].id[:8] not in result.output


def test_cli_tailor_without_a_report(cli_env):
    result = runner.invoke(cli_mod.app, ["tailor", "--top", "3"])
    assert result.exit_code == 1 and "no report" in result.output


def test_cli_tailor_batch_skips_cached_jobs_unless_forced(batch_env):
    jobs = batch_env.jobs
    assert runner.invoke(cli_mod.app, ["tailor", jobs[0].id]).exit_code == 0  # cached now
    batch_env.fake.responses.append(_response(_answer()))
    result = runner.invoke(cli_mod.app, ["tailor", "--top", "2"])
    assert result.exit_code == 0, result.output
    assert "cached" in _row(result.output, jobs[0]) and "tailored" in _row(result.output, jobs[2])
    assert len(batch_env.fake.calls) == 2
    # The cached one still points at its PDF (found through index.json).
    assert "CV - Acme.pdf" in _row(result.output, jobs[0])

    batch_env.fake.responses += [_response(_answer()), _response(_answer())]
    forced = runner.invoke(cli_mod.app, ["tailor", "--top", "2", "--force"])
    assert forced.exit_code == 0, forced.output
    assert "tailored" in _row(forced.output, jobs[0]) and len(batch_env.fake.calls) == 4


def test_cli_tailor_batch_export_writes_batch_txt(batch_env):
    jobs = batch_env.jobs
    assert runner.invoke(cli_mod.app, ["tailor", jobs[0].id]).exit_code == 0  # cached now
    result = runner.invoke(cli_mod.app, ["tailor", "--top", "3", "--export"])
    assert result.exit_code == 0, result.output
    out = batch_env.data / "cv" / "tailored"
    lines = (out / "prompts" / "batch.txt").read_text(encoding="utf-8").splitlines()
    assert [line.split("\t") for line in lines] == [
        [j.id, str(out / "prompts" / f"{j.id}.md"), str(out / "answers" / f"{j.id}.json")]
        for j in (jobs[2], jobs[1])]
    assert (out / "prompts" / f"{jobs[2].id}.md").is_file()
    assert not (out / "prompts" / f"{jobs[0].id}.md").exists()
    assert "exported" in _row(result.output, jobs[1]) and "cached" in _row(result.output, jobs[0])
    assert "batch.txt" in result.output


def test_cli_tailor_several_ids_are_a_batch(batch_env):
    jobs = batch_env.jobs
    result = runner.invoke(cli_mod.app, ["tailor", jobs[0].id, jobs[1].id, "--export"])
    assert result.exit_code == 0, result.output
    batch = batch_env.data / "cv" / "tailored" / "prompts" / "batch.txt"
    assert len(batch.read_text(encoding="utf-8").splitlines()) == 2
    assert "exported" in _row(result.output, jobs[1])


def test_cli_tailor_batch_import(batch_env):
    jobs = batch_env.jobs
    answers = batch_env.data / "cv" / "tailored" / "answers"
    answers.mkdir(parents=True)
    (answers / f"{jobs[2].id}.json").write_text(_answer().model_dump_json(), encoding="utf-8")
    (answers / f"{jobs[0].id}.json").write_text(_answer(summary="nope").model_dump_json(),
                                                encoding="utf-8")
    result = runner.invoke(cli_mod.app, ["tailor", "--top", "3", "--import"])
    assert result.exit_code == 1
    assert "imported" in _row(result.output, jobs[2])
    assert "error: unknown summary 'nope'" in _row(result.output, jobs[0])
    assert "no answer" in _row(result.output, jobs[1])
    assert batch_env.fake.calls == []


def test_cli_tailor_batch_without_a_browser_warns_once(batch_env, monkeypatch):
    def missing(cv):
        raise render_mod.RenderUnavailable("no chromium here")

    monkeypatch.setattr(render_mod, "measure_overflow", missing)
    batch_env.fake.responses.append(_response(_answer()))
    result = runner.invoke(cli_mod.app, ["tailor", "--top", "2"])
    assert result.exit_code == 0, result.output
    assert result.output.count("no chromium here") == 1
    assert "tailored" in _row(result.output, batch_env.jobs[0])


def test_cli_tailor_interested_picks_the_only_handle(batch_env):
    jobs = batch_env.jobs
    _decide(batch_env, (jobs[1].id, "tonttu", "interested"), (jobs[2].id, "tonttu", "skipped"),
            ("ghost123456789ab", "tonttu", "interested"))
    result = runner.invoke(cli_mod.app, ["tailor", "--interested", "--export"])
    assert result.exit_code == 0, result.output
    assert "exported" in _row(result.output, jobs[1])
    assert jobs[2].id[:8] not in result.output
    assert "ghost123456789ab" in result.output and "not in" in result.output


def test_cli_tailor_interested_with_several_handles(batch_env):
    jobs = batch_env.jobs
    _decide(batch_env, (jobs[1].id, "tonttu", "interested"), (jobs[2].id, "other", "interested"))
    result = runner.invoke(cli_mod.app, ["tailor", "--interested", "--export"])
    assert result.exit_code == 1
    assert "other, tonttu" in result.output and "--user" in result.output
    chosen = runner.invoke(cli_mod.app, ["tailor", "--interested", "--user", "other", "--export"])
    assert chosen.exit_code == 0, chosen.output
    assert "exported" in _row(chosen.output, jobs[2]) and jobs[1].id[:8] not in chosen.output


def test_cli_tailor_interested_without_decisions(batch_env):
    result = runner.invoke(cli_mod.app, ["tailor", "--interested"])
    assert result.exit_code == 1 and "no decisions" in result.output


def test_cli_tailor_interested_with_nothing_marked(batch_env):
    _decide(batch_env, (batch_env.jobs[1].id, "tonttu", "skipped"))
    result = runner.invoke(cli_mod.app, ["tailor", "--interested"])
    assert result.exit_code == 0, result.output
    assert "no jobs to tailor" in result.output


# ------------------------------------------------------------------ closed postings


def _mark(db, job: Job, status: str, reason: str) -> None:
    from datetime import UTC, datetime

    from jobscraper.models import LivenessCheck

    store = Store(db)
    store.save_liveness([LivenessCheck(job_id=job.id, status=status, reason=reason,
                                       checked_at=datetime(2026, 10, 17, 9, 0, tzinfo=UTC))])
    store.close()


def test_cli_tailor_refuses_a_closed_posting(cli_env):
    _mark(cli_env.db, cli_env.job, "closed", "LinkedIn: no longer accepting applications")
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id])
    assert result.exit_code == 1
    assert f"tailor: {cli_env.job.id}: posting closed (checked 2026-10-17)" in result.output
    assert cli_env.fake.calls == []  # no request spent on a vacancy nobody can apply to
    assert not list((cli_env.data / "cv" / "tailored").glob("*.md"))


def test_cli_tailor_refuses_a_gone_posting(cli_env):
    _mark(cli_env.db, cli_env.job, "gone", "HTTP 404")
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id, "--export"])
    assert result.exit_code == 1
    assert "posting gone (HTTP 404, checked 2026-10-17)" in result.output


def test_cli_tailor_tailors_a_posting_whose_check_was_inconclusive(cli_env):
    _mark(cli_env.db, cli_env.job, "unknown", "HTTP 429")
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id])
    assert result.exit_code == 0, result.output


def test_cli_tailor_batch_marks_a_closed_posting_and_goes_on(batch_env):
    jobs = batch_env.jobs
    _mark(batch_env.db, jobs[2], "closed", "LinkedIn: no longer accepting applications")
    result = runner.invoke(cli_mod.app, ["tailor", "--top", "2"])
    assert result.exit_code == 0, result.output  # a closed posting is an outcome, not a failure
    assert "closed" in _row(result.output, jobs[2])
    assert "tailored" in _row(result.output, jobs[0])
    assert len(batch_env.fake.calls) == 1


def test_library_block_shows_the_fixed_sidebar_so_gaps_are_judged_against_the_whole_page(parts):
    """The sidebar is always printed; without it the model listed skills the page already shows
    (HTML and CSS, 2026-10-22) as gaps."""
    block = library_block(parts)
    assert "SIDEBAR (always on the page, not selectable; judge gaps against it too)" in block
    assert "- Technical: C, C++, Python" in block
    assert "- Education: Hive Helsinki" in block
    assert "- Languages: finnish: native; english: fluent" in block
    assert block.index("SIDEBAR") > block.index("WORK")


def test_gaps_are_what_neither_the_library_nor_the_sidebar_covers(settings, parts):
    prompt = tailor_system_prompt(settings.profile, parts)
    assert "neither the library nor the sidebar covers" in prompt
