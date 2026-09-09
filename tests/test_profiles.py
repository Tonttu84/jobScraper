"""Named profiles: per-profile config/data/results directories plus the two CLI commands.

Everything runs against tmp_path through the ``JOBSCRAPER_*_DIR`` base overrides, so the repo's
own ``config/``, ``data/`` and ``results/`` are never touched.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jobscraper import cli as cli_mod
from jobscraper import config
from tests.conftest import FakeHttp

runner = CliRunner()

DEFAULT_PROFILE_YAML = "name: Default Owner\nsummary: The default candidate.\n"
DEFAULT_SOURCES_YAML = "sources:\n  arbeitnow:\n    enabled: true\n"


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """Point the three base directories at tmp_path and seed a default config."""
    monkeypatch.setenv("JOBSCRAPER_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("JOBSCRAPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JOBSCRAPER_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.delenv("JOBSCRAPER_DB", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "profile.yaml").write_text(DEFAULT_PROFILE_YAML, encoding="utf-8")
    (tmp_path / "config" / "sources.yaml").write_text(DEFAULT_SOURCES_YAML, encoding="utf-8")
    return tmp_path


def _make_profile(root, name: str, *, summary: str = "A second candidate.") -> None:
    d = root / "config" / "profiles" / name
    d.mkdir(parents=True)
    (d / "profile.yaml").write_text(f"name: {name}\nsummary: {summary}\n", encoding="utf-8")
    (d / "sources.yaml").write_text(DEFAULT_SOURCES_YAML, encoding="utf-8")


# --------------------------------------------------------------------------- paths


def test_without_a_profile_the_base_directories_are_used(dirs):
    p = config.paths()
    assert p.config == dirs / "config"
    assert p.data == dirs / "data"
    assert p.results == dirs / "results"
    assert config.active_profile() is None


def test_a_named_profile_nests_config_data_and_results(dirs):
    _make_profile(dirs, "ana")
    p = config.use_profile("ana")
    assert config.active_profile() == "ana"
    assert p == config.paths()
    assert p.config == dirs / "config" / "profiles" / "ana"
    assert p.data == dirs / "data" / "profiles" / "ana"
    assert p.results == dirs / "results" / "ana"
    # switching back restores the plain directories
    assert config.use_profile(None).data == dirs / "data"


def test_a_missing_profile_is_a_clear_error(dirs):
    with pytest.raises(config.ProfileError) as exc:
        config.use_profile("ghost")
    assert "profile.yaml" in str(exc.value) and "ghost" in str(exc.value)
    assert config.active_profile() is None


def test_a_half_built_profile_names_the_missing_file(dirs):
    d = dirs / "config" / "profiles" / "half"
    d.mkdir(parents=True)
    (d / "profile.yaml").write_text(DEFAULT_PROFILE_YAML, encoding="utf-8")
    with pytest.raises(config.ProfileError) as exc:
        config.use_profile("half")
    assert "sources.yaml" in str(exc.value)


@pytest.mark.parametrize("name", ["../evil", "a/b", "..", "with space", "-lead-dash"])
def test_unsafe_profile_names_are_refused(dirs, name):
    with pytest.raises(config.ProfileError):
        config.use_profile(name)


@pytest.mark.parametrize("name", [None, "", "   "])
def test_an_empty_profile_name_means_the_default(dirs, name):
    assert config.use_profile(name).data == dirs / "data"
    assert config.active_profile() is None


def test_load_settings_reads_the_active_profile(dirs):
    _make_profile(dirs, "ana", summary="Senior C++ developer.")
    assert config.load_settings().profile.name == "Default Owner"
    config.use_profile("ana")
    settings = config.load_settings()
    assert settings.profile.name == "ana"
    assert settings.profile.summary == "Senior C++ developer."


def test_known_profiles_lists_complete_directories_only(dirs):
    assert config.known_profiles() == []
    _make_profile(dirs, "ana")
    _make_profile(dirs, "bo")
    (dirs / "config" / "profiles" / "empty").mkdir()
    assert config.known_profiles() == ["ana", "bo"]


# ----------------------------------------------------------------------------- cli


def test_profile_init_copies_the_default_config_and_refuses_to_overwrite(dirs):
    result = runner.invoke(cli_mod.app, ["profile-init", "ana"])
    assert result.exit_code == 0, result.output
    created = dirs / "config" / "profiles" / "ana"
    assert (created / "profile.yaml").read_text(encoding="utf-8") == DEFAULT_PROFILE_YAML
    assert (created / "sources.yaml").read_text(encoding="utf-8") == DEFAULT_SOURCES_YAML
    # the copy is now a usable profile
    assert config.use_profile("ana").config == created

    again = runner.invoke(cli_mod.app, ["profile-init", "ana"])
    assert again.exit_code != 0
    assert "already exists" in again.output


def test_profile_init_rejects_an_unsafe_name(dirs):
    result = runner.invoke(cli_mod.app, ["profile-init", "../evil"])
    assert result.exit_code != 0


def test_profiles_lists_names_and_marks_the_active_one(dirs):
    _make_profile(dirs, "ana")
    listed = runner.invoke(cli_mod.app, ["profiles"])
    assert listed.exit_code == 0, listed.output
    assert "default" in listed.output and "ana" in listed.output

    active = runner.invoke(cli_mod.app, ["--profile", "ana", "profiles"])
    assert active.exit_code == 0, active.output
    assert "ana" in active.output


def test_unknown_profile_fails_the_command_with_a_hint(dirs):
    result = runner.invoke(cli_mod.app, ["--profile", "ghost", "sources"])
    assert result.exit_code != 0
    assert "profile-init" in result.output


def test_profile_option_routes_the_database_and_results(dirs, monkeypatch):
    _make_profile(dirs, "ana")
    monkeypatch.setattr(cli_mod, "_http", lambda settings: FakeHttp({"job-board-api": "arbeitnow.json"}))
    result = runner.invoke(cli_mod.app, ["--profile", "ana", "scrape", "arbeitnow", "--limit", "1"])
    assert result.exit_code == 0, result.output
    assert (dirs / "data" / "profiles" / "ana" / "jobs.db").exists()
    assert not (dirs / "data" / "jobs.db").exists()

    assert runner.invoke(cli_mod.app, ["--profile", "ana", "filter"]).exit_code == 0
    reported = runner.invoke(cli_mod.app, ["--profile", "ana", "report"])
    assert reported.exit_code == 0, reported.output
    assert list((dirs / "results" / "ana").glob("report-*.md"))
    assert not (dirs / "results" / "report-2026-01-01.md").exists()


def test_profile_can_come_from_the_environment(dirs, monkeypatch):
    _make_profile(dirs, "ana")
    monkeypatch.setenv("JOBSCRAPER_PROFILE", "ana")
    monkeypatch.setattr(cli_mod, "_http", lambda settings: FakeHttp({"job-board-api": "arbeitnow.json"}))
    result = runner.invoke(cli_mod.app, ["scrape", "arbeitnow", "--limit", "1"])
    assert result.exit_code == 0, result.output
    assert (dirs / "data" / "profiles" / "ana" / "jobs.db").exists()


def test_publish_and_serve_use_the_profiles_serve_directory(dirs, monkeypatch):
    _make_profile(dirs, "ana")
    monkeypatch.setattr(cli_mod, "_http", lambda settings: FakeHttp({"job-board-api": "arbeitnow.json"}))
    assert runner.invoke(cli_mod.app, ["--profile", "ana", "scrape", "arbeitnow", "--limit", "1"]).exit_code == 0
    published = runner.invoke(cli_mod.app, ["--profile", "ana", "publish", "--name", "week1"])
    assert published.exit_code == 0, published.output
    assert (dirs / "data" / "profiles" / "ana" / "serve" / "week1.db").exists()

    calls: dict = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.update(app=app, kwargs=kw))
    served = runner.invoke(cli_mod.app, ["--profile", "ana", "serve"])
    assert served.exit_code == 0, served.output
    assert calls["app"].state.serve_dir == dirs / "data" / "profiles" / "ana" / "serve"
    assert calls["app"].state.languages == ["en", "fi", "de"]  # LanguagePolicy defaults
