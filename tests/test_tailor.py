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
from typer.testing import CliRunner

from jobscraper import cli as cli_mod
from jobscraper.config import ROOT
from jobscraper.cv import render as render_mod
from jobscraper.cv import tailor as tailor_mod
from jobscraper.cv.parts import load_cv_parts, parts_fingerprint
from jobscraper.cv.tailor import (
    TAILOR_PROMPT_VERSION,
    PickedEntry,
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
from jobscraper.models import AIVerdict, FilterResult, Job
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

    def _make(*responses, model: str | None = None) -> tuple[Tailor, FakeMessages]:
        t = Tailor(settings.profile, store, parts, model=model)
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


def test_handoff_markdown_has_the_job_the_steps_and_the_cv(make_tailor):
    t, _ = make_tailor(_response(_answer()))
    job = _job()
    md = to_handoff_markdown(job, t.tailor(job, None))
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
    monkeypatch.setattr(render_mod, "render_pdf",
                        lambda html, pdf, png=None: render_mod.RenderResult(pages=1, overflow_mm=0.0))
    return SimpleNamespace(job=job, fake=fake, data=tmp_path / "data", config=config_dir)


def test_cli_tailor_prints_and_saves_the_handoff(cli_env):
    result = runner.invoke(cli_mod.app, ["tailor", cli_env.job.id])
    assert result.exit_code == 0, result.output
    assert "# Tailored CV: C++ Developer — Acme" in result.output
    saved = list((cli_env.data / "cv" / "tailored").glob("*.md"))
    assert len(saved) == 1 and cli_env.job.id in saved[0].name
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
