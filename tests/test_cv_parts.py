"""The CV parts library: format, library validation, selections, resolving and rendering.

Everything runs against the generic fixture ``tests/fixtures/cv-parts.yaml`` (or mutated copies
of it) and the repository's ``config/cv-parts.example.yaml``; the owner's own
``config/cv-parts.yaml`` is never read.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from jobscraper import cli as cli_mod
from jobscraper.config import ROOT
from jobscraper.cv.parts import (
    CvParts,
    EntryPick,
    Selection,
    SelectionError,
    TailoredCV,
    default_selection,
    effective_max_bullets,
    load_cv_parts,
    parts_fingerprint,
    resolve,
    to_markdown,
    validate_selection,
)

FIXTURE = Path(__file__).parent / "fixtures" / "cv-parts.yaml"
runner = CliRunner()


@pytest.fixture
def raw() -> dict:
    """The fixture as a plain dict, to mutate per test."""
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def parts(raw) -> CvParts:
    return CvParts.model_validate(raw)


def _sel(**kw) -> Selection:
    base = {"summary": "default",
            "projects": [{"id": "webserver", "bullets": ["parse", "poll"]}],
            "work": {}}
    base.update(kw)
    return Selection.model_validate(base)


# --- loading ---------------------------------------------------------------------------------


def test_loads_fixture(tmp_path):
    (tmp_path / "cv-parts.yaml").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    parts = load_cv_parts(tmp_path)
    assert parts.version == "test-1"
    assert parts.slots.projects == 2
    assert [s.id for s in parts.summaries] == ["default", "systems"]
    assert [p.id for p in parts.projects] == ["webserver", "shell", "game"]
    assert [w.id for w in parts.work] == ["poker", "shop"]
    assert parts.header["name"] == "Test Person"
    assert parts.sidebar["education"] == "Hive Helsinki"
    # folded YAML text is stripped of its trailing newline
    assert parts.summaries[0].text == "A general summary about the test person, folded over two lines."
    assert parts.projects[0].bullets[0].tags == ["cpp"]
    assert parts.projects[0].default is True and parts.projects[1].default is False
    assert parts.work[1].lead == ""


def test_load_uses_config_dir_from_env(tmp_path, monkeypatch):
    (tmp_path / "cv-parts.yaml").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("JOBSCRAPER_CONFIG_DIR", str(tmp_path))
    assert load_cv_parts().version == "test-1"


def test_missing_file_mentions_example(tmp_path):
    (tmp_path / "cv-parts.example.yaml").write_text("version: x\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match=r"cv-parts\.example\.yaml"):
        load_cv_parts(tmp_path)


def test_example_file_is_a_valid_library(tmp_path):
    example = ROOT / "config" / "cv-parts.example.yaml"
    (tmp_path / "cv-parts.yaml").write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    parts = load_cv_parts(tmp_path)
    assert parts.header["name"] == "Jane Example"
    assert len(parts.summaries) == 2
    assert len(parts.projects) == 4
    assert sum(p.default for p in parts.projects) == 3
    assert len(parts.work) == 2
    assert parts.sidebar
    selection = default_selection(parts)
    validate_selection(parts, selection)
    assert len(selection.projects) == 3
    assert "## SAMPLE PROJECTS" in to_markdown(resolve(parts, selection))


# --- library validation ----------------------------------------------------------------------


def _set(d, path: tuple, value) -> None:
    for key in path[:-1]:
        d = d[key]
    d[path[-1]] = value


@pytest.mark.parametrize(("path", "value", "match"), [
    (("summaries", 1, "id"), "default", "duplicate summary id 'default'"),
    (("projects", 1, "id"), "webserver", "duplicate project id 'webserver'"),
    (("work", 1, "id"), "poker", "duplicate work entry id 'poker'"),
    (("projects", 0, "bullets", 1, "id"), "parse", "project 'webserver': duplicate bullet id 'parse'"),
    (("work", 0, "bullets", 1, "id"), "discipline",
     "work entry 'poker': duplicate bullet id 'discipline'"),
    (("projects", 0, "id"), "Web Server", "project id 'Web Server'"),
    (("summaries", 0, "id"), "-lead", "summary id '-lead'"),
    (("projects", 0, "bullets", 0, "id"), "Parse", "bullet id 'Parse'"),
    (("work", 0, "id"), "", "work entry id ''"),
    (("summaries", 0, "text"), "   ", "summary 'default': text is blank"),
    (("projects", 0, "title"), "", "project 'webserver': title is blank"),
    (("projects", 0, "lead"), " ", "project 'webserver': lead is blank"),
    (("projects", 0, "bullets", 0, "text"), "", "bullet 'parse': text is blank"),
    (("work", 0, "title"), "", "work entry 'poker': title is blank"),
    (("work", 0, "period"), "", "work entry 'poker': period is blank"),
    (("version",), " ", "version is blank"),
    (("header", "email"), " ", "header 'email' is blank"),
    (("summaries",), [], "at least one summary"),
    (("projects",), [], "at least one project"),
    (("projects", 0, "bullets"), [], "project 'webserver': needs at least one bullet"),
    (("work", 1, "bullets"), [], "work entry 'shop': needs at least one bullet"),
    (("projects", 1, "default"), True, "3 projects are marked default, but slots.projects is 2"),
    (("projects", 0, "max_bullets"), 1, "project 'webserver': 2 default bullets, but at most 1"),
    (("work", 0, "max_bullets"), 0, "work entry 'poker': max_bullets must be at least 1"),
    (("summaries", 0, "tags"), ["General"], "summary 'default': tag 'General'"),
    (("projects", 0, "tags"), ["cpp", " "], "project 'webserver': tag ''"),
    (("projects", 0, "bullets", 0, "tags"), ["CPP"], "bullet 'parse': tag 'CPP'"),
    (("work", 0, "tags"), ["Poker"], "work entry 'poker': tag 'Poker'"),
    (("slots", "projects"), 0, "slots.projects"),
    (("slots", "project_bullets"), 0, "slots.project_bullets"),
    (("slots", "work_bullets"), 0, "slots.work_bullets"),
    (("projects", 0, "colour"), "blue", "colour"),
])
def test_library_validation_errors(raw, path, value, match):
    _set(raw, path, value)
    with pytest.raises(ValueError, match=re.escape(match)):
        CvParts.model_validate(raw)


def test_numbers_from_yaml_are_kept_as_text(raw):
    raw["version"] = 2026.1
    raw["work"][0]["period"] = 2024
    parts = CvParts.model_validate(raw)
    assert parts.version == "2026.1"
    assert parts.work[0].period == "2024"


def test_too_many_default_work_bullets(raw):
    for bullet in raw["work"][0]["bullets"]:
        bullet["default"] = True
    with pytest.raises(ValueError, match="work entry 'poker': 3 default bullets, but at most 2"):
        CvParts.model_validate(raw)


def test_effective_max_bullets(parts):
    webserver, shell, _ = parts.projects
    poker, _ = parts.work
    assert effective_max_bullets(parts, webserver) == 2  # slots.project_bullets
    assert effective_max_bullets(parts, shell) == 3  # the entry's own max_bullets
    assert effective_max_bullets(parts, poker) == 2  # slots.work_bullets


# --- default selection -----------------------------------------------------------------------


def test_default_selection(parts):
    sel = default_selection(parts)
    assert sel.summary == "default"
    assert sel.projects == [EntryPick(id="webserver", bullets=["parse", "poll"]),
                            EntryPick(id="game", bullets=["dda"])]
    # poker has a default bullet; shop has none, so its first max bullets are taken
    assert sel.work == {"poker": ["discipline"], "shop": ["service"]}
    validate_selection(parts, sel)


def test_default_selection_without_default_projects(raw):
    for project in raw["projects"]:
        project["default"] = False
    parts = CvParts.model_validate(raw)
    sel = default_selection(parts)
    # the first slots.projects projects; shell has no default bullets → its first 3 (its max)
    assert sel.projects == [EntryPick(id="webserver", bullets=["parse", "poll"]),
                            EntryPick(id="shell", bullets=["pipes", "signals", "builtins"])]
    validate_selection(parts, sel)


# --- selection validation --------------------------------------------------------------------


@pytest.mark.parametrize(("selection", "match"), [
    ({"summary": "nope"}, "unknown summary 'nope'"),
    ({"projects": []}, "no projects selected"),
    ({"projects": [{"id": "webserver", "bullets": ["parse"]}, {"id": "game", "bullets": ["dda"]},
                   {"id": "shell", "bullets": ["pipes"]}]},
     "3 projects selected, but slots.projects is 2"),
    ({"projects": [{"id": "nope", "bullets": ["x"]}]}, "unknown project 'nope'"),
    ({"projects": [{"id": "game", "bullets": ["dda"]}, {"id": "game", "bullets": ["dda"]}]},
     "project 'game' selected twice"),
    ({"projects": [{"id": "webserver", "bullets": []}]}, "project 'webserver': no bullets selected"),
    ({"projects": [{"id": "webserver", "bullets": ["nope"]}]},
     "project 'webserver': unknown bullet 'nope'"),
    ({"projects": [{"id": "webserver", "bullets": ["parse", "parse"]}]},
     "project 'webserver': bullet 'parse' selected twice"),
    ({"projects": [{"id": "webserver", "bullets": ["parse", "poll", "cgi"]}]},
     "project 'webserver': 3 bullets selected, but at most 2"),
    ({"work": {"nope": []}}, "unknown work entry 'nope'"),
    ({"work": {"poker": ["nope"]}}, "work entry 'poker': unknown bullet 'nope'"),
    ({"work": {"poker": ["math", "math"]}}, "work entry 'poker': bullet 'math' selected twice"),
    ({"work": {"poker": ["math", "risk", "discipline"]}},
     "work entry 'poker': 3 bullets selected, but at most 2"),
])
def test_selection_errors(parts, selection, match):
    with pytest.raises(SelectionError, match=re.escape(match)):
        validate_selection(parts, _sel(**selection))


def test_selection_error_lists_every_problem(parts):
    sel = _sel(summary="nope",
               projects=[{"id": "webserver", "bullets": ["nope"]}, {"id": "ghost", "bullets": ["x"]}],
               work={"poker": ["math", "math"]})
    with pytest.raises(SelectionError) as info:
        validate_selection(parts, sel)
    lines = str(info.value).splitlines()
    assert lines == [
        "unknown summary 'nope'",
        "project 'webserver': unknown bullet 'nope'",
        "unknown project 'ghost'",
        "work entry 'poker': bullet 'math' selected twice",
    ]
    assert info.value.problems == lines
    assert isinstance(info.value, ValueError)


def test_empty_work_bullet_list_is_allowed(parts):
    validate_selection(parts, _sel(work={"poker": []}))


def test_selection_rejects_unknown_fields():
    with pytest.raises(ValueError, match="bogus"):
        Selection.model_validate({"summary": "default", "projects": [], "bogus": 1})


# --- resolve ---------------------------------------------------------------------------------


def test_resolve(parts):
    sel = _sel(summary="systems",
               projects=[{"id": "shell", "bullets": ["heredoc", "pipes"]},
                         {"id": "webserver", "bullets": ["cgi"]}],
               work={"poker": ["risk", "math"]})
    cv = resolve(parts, sel)
    assert isinstance(cv, TailoredCV)
    assert cv.summary == "A systems-flavoured summary."
    assert [p.title for p in cv.projects] == ["Shell", "Webserver"]
    assert cv.projects[0].lead == "A small Unix shell."
    assert cv.projects[0].bullets == ["Supported heredocs.", "Implemented pipes."]
    assert cv.projects[0].period is None
    assert cv.projects[1].bullets == ["Ran CGI scripts."]
    # every work entry, in file order; omitted ones fall back to their defaults
    assert [w.title for w in cv.work] == ["Poker professional", "Shop assistant"]
    assert cv.work[0].bullets == ["Managed bankroll risk.", "Used probability daily."]
    assert cv.work[0].period == "2010-2024"
    assert cv.work[1].bullets == ["Served customers."]
    assert cv.work[1].lead == ""
    assert cv.header == parts.header
    assert cv.sidebar == parts.sidebar
    assert cv.version == "test-1"
    assert cv.selection is sel


def test_resolve_empty_work_list_shows_no_bullets(parts):
    cv = resolve(parts, _sel(work={"shop": []}))
    assert cv.work[1].bullets == []
    assert cv.work[0].bullets == ["Kept detailed records."]


def test_resolve_validates_first(parts):
    with pytest.raises(SelectionError):
        resolve(parts, _sel(summary="nope"))


# --- markdown --------------------------------------------------------------------------------


def test_to_markdown(parts):
    md = to_markdown(resolve(parts, default_selection(parts)))
    assert md.startswith("# Test Person\n\nJunior developer\n\ntest@example.com · github.com/test\n")
    for heading in ("## PROFILE", "## SAMPLE PROJECTS", "## WORK EXPERIENCE", "## SIDEBAR"):
        assert heading in md
    assert md.index("## PROFILE") < md.index("## SAMPLE PROJECTS") < md.index("## WORK EXPERIENCE")
    assert "A general summary about the test person, folded over two lines." in md
    assert "### Webserver\n\nAn HTTP server in C++.\n\n" in md
    assert "- Wrote an incremental request parser.\n- Served many clients with poll()." in md
    assert "### Raycaster" in md
    assert "Shell" not in md and "Ran CGI scripts." not in md
    assert "### Poker professional (2010-2024)\n\nSelf-employed.\n\n- Kept detailed records." in md
    assert "Used probability daily." not in md
    # sidebar: one key: value line each; lists and mappings flattened
    assert "- Technical: C, C++, Python" in md
    assert "- Education: Hive Helsinki" in md
    assert "- Languages: finnish: native; english: fluent" in md
    assert md.endswith("\n") and not md.endswith("\n\n")
    # no ids leak into the rendered CV
    for ident in ("webserver", "parse", "dda", "discipline", "poker", "shop", "default"):
        assert not re.search(rf"\b{ident}\b", md), ident


def test_to_markdown_minimal_header_and_bare_work_entry(raw):
    raw["header"] = {"name": "Solo"}
    raw["sidebar"] = {}
    parts = CvParts.model_validate(raw)
    md = to_markdown(resolve(parts, _sel(work={"shop": []})))
    assert md.startswith("# Solo\n\n## PROFILE")
    # no lead and no bullets: just the heading
    assert md.endswith("### Shop assistant (2008-2010)\n")
    assert "## SIDEBAR" not in md


def test_to_markdown_scalar_sidebar_value(raw):
    raw["sidebar"] = {"note": 7}
    md = to_markdown(resolve(CvParts.model_validate(raw), _sel()))
    assert "- Note: 7" in md


# --- fingerprint -----------------------------------------------------------------------------


def test_parts_fingerprint(raw):
    base = parts_fingerprint(CvParts.model_validate(raw))
    version, digest = base.split(":", 1)
    assert version == "test-1"
    assert re.fullmatch(r"[0-9a-f]{12}", digest)
    assert parts_fingerprint(CvParts.model_validate(copy.deepcopy(raw))) == base

    changed = copy.deepcopy(raw)
    changed["projects"][0]["bullets"][0]["text"] = "Wrote a different parser."
    assert parts_fingerprint(CvParts.model_validate(changed)) != base

    bumped = copy.deepcopy(raw)
    bumped["version"] = "test-2"
    other = parts_fingerprint(CvParts.model_validate(bumped))
    assert other.startswith("test-2:") and other != base


# --- CLI -------------------------------------------------------------------------------------


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSCRAPER_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("JOBSCRAPER_PROFILE", raising=False)
    return tmp_path


def test_cli_cv_parts(config_dir):
    (config_dir / "cv-parts.yaml").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    result = runner.invoke(cli_mod.app, ["cv-parts"])
    assert result.exit_code == 0, result.output
    first = result.output.splitlines()[0]
    assert first == ("cv parts test-1: 2 summaries, 3 projects, 2 work entries; "
                     "slots: 2 projects × 2 bullets, 2 bullets per work entry")
    assert "## SAMPLE PROJECTS" in result.output
    assert "- Wrote an incremental request parser." in result.output


def test_cli_cv_parts_broken_file(config_dir, raw):
    raw["projects"][1]["id"] = "webserver"
    (config_dir / "cv-parts.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    result = runner.invoke(cli_mod.app, ["cv-parts"])
    assert result.exit_code == 1
    assert "duplicate project id 'webserver'" in result.output


def test_cli_cv_parts_missing_file(config_dir):
    result = runner.invoke(cli_mod.app, ["cv-parts"])
    assert result.exit_code == 1
    assert "cv-parts.yaml does not exist" in result.output


def test_cli_cv_parts_bad_yaml(config_dir):
    (config_dir / "cv-parts.yaml").write_text("version: [unclosed\n", encoding="utf-8")
    result = runner.invoke(cli_mod.app, ["cv-parts"])
    assert result.exit_code == 1
    assert "cv-parts.yaml" in result.output
