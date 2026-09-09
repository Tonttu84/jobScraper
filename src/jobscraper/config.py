"""YAML configuration: candidate profile, source settings, and where everything lives on disk.

The tool serves more than one candidate, so the three directories it works in are resolved
through :func:`paths` instead of module constants. Without a named profile they are the plain
``config/``, ``data/`` and ``results/`` (overridable with ``JOBSCRAPER_CONFIG_DIR`` /
``JOBSCRAPER_DATA_DIR`` / ``JOBSCRAPER_RESULTS_DIR``); ``--profile NAME`` nests them into
``config/profiles/NAME/``, ``data/profiles/NAME/`` and ``results/NAME/``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]

#: Files a profile directory has to provide before it can be used.
PROFILE_FILES = ("profile.yaml", "sources.yaml")
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*$")


class ProfileError(ValueError):
    """A named profile is missing, incomplete, or not a usable directory name."""


@dataclass(frozen=True)
class Paths:
    """The three directories one candidate's run works in."""

    config: Path
    data: Path
    #: Human-facing output (markdown reports); gitignored, separate from the working data.
    results: Path


_ACTIVE_PROFILE: str | None = None


def base_paths() -> Paths:
    """The un-profiled directories, honouring the ``JOBSCRAPER_*_DIR`` overrides.

    Read from the environment on every call so tests (and a shell that exports one mid-session)
    are not stuck with whatever was set at import time.
    """
    return Paths(
        config=Path(os.environ.get("JOBSCRAPER_CONFIG_DIR") or ROOT / "config"),
        data=Path(os.environ.get("JOBSCRAPER_DATA_DIR") or ROOT / "data"),
        results=Path(os.environ.get("JOBSCRAPER_RESULTS_DIR") or ROOT / "results"),
    )


def profile_dir(name: str, base: Paths | None = None) -> Path:
    """Where a named profile's YAML lives. Raises :class:`ProfileError` on an unusable name."""
    if not _SAFE_NAME.fullmatch(name or ""):
        raise ProfileError(f"invalid profile name {name!r}: use letters, digits, '.', '_' or '-'")
    return (base or base_paths()).config / "profiles" / name


def paths() -> Paths:
    """The directories the current command works in (profile-aware)."""
    base = base_paths()
    if _ACTIVE_PROFILE is None:
        return base
    return Paths(
        config=base.config / "profiles" / _ACTIVE_PROFILE,
        data=base.data / "profiles" / _ACTIVE_PROFILE,
        results=base.results / _ACTIVE_PROFILE,
    )


def active_profile() -> str | None:
    return _ACTIVE_PROFILE


def use_profile(name: str | None) -> Paths:
    """Switch to a named profile (``None`` = the default, unprefixed directories)."""
    global _ACTIVE_PROFILE
    name = (name or "").strip() or None
    if name is not None:
        directory = profile_dir(name)
        missing = [f for f in PROFILE_FILES if not (directory / f).is_file()]
        if missing:
            raise ProfileError(
                f"profile {name!r}: {directory} is missing {', '.join(missing)}. "
                f"Create it with `jobscraper profile-init {name}`."
            )
    _ACTIVE_PROFILE = name
    return paths()


def known_profiles() -> list[str]:
    """Names of the usable profile directories under ``config/profiles``."""
    root = base_paths().config / "profiles"
    if not root.is_dir():
        return []
    return sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and all((d / f).is_file() for f in PROFILE_FILES)
    )


class LanguagePolicy(BaseModel):
    ok: list[str] = Field(default_factory=lambda: ["en", "fi", "de"])
    weak: list[str] = Field(default_factory=lambda: ["sv"], description="Known a little: flag, don't drop")
    drop_if_written_in: list[str] = Field(
        default_factory=list, description="Posting text languages that mean an automatic drop"
    )
    min_confidence: float = 0.75


class SeniorityPolicy(BaseModel):
    max_years_keep: int = 2
    max_years_review: int = 4
    drop_title_terms: list[str] = Field(default_factory=list)
    keep_title_terms: list[str] = Field(default_factory=list)
    #: Board seniority labels (``seniority_raw``) that mean an automatic drop, e.g. "mid",
    #: "medior", "regular" for a junior candidate. ``keep_title_terms`` still wins over these.
    drop_label_terms: list[str] = Field(default_factory=list)


class LocationPolicy(BaseModel):
    tier1: list[str] = Field(default_factory=lambda: ["FI", "EE"])
    tier2: list[str] = Field(default_factory=list)
    tier3: list[str] = Field(default_factory=list)
    keep_all_remote: bool = True
    notes: dict[str, str] = Field(default_factory=dict)


class RolePolicy(BaseModel):
    title_terms: list[str] = Field(default_factory=list)
    exclude_title_terms: list[str] = Field(default_factory=list)


class PromptPolicy(BaseModel):
    """Candidate-specific wording of the AI prompts. Everything here is free text from YAML.

    The scaffolding around these fields (the CANDIDATE block, the language/location bullets,
    the scoring instructions) is generated in :mod:`jobscraper.ai.prompts`.
    """

    #: Filled into "You are screening job postings for a specific {role_label}".
    role_label: str = "software developer"
    #: The "Seniority:" bullet. ``{max_years_keep}`` / ``{max_years_review}`` are interpolated.
    seniority_rule: str = (
        "roles asking for at most {max_years_keep} years of experience are a clear fit, up to "
        "{max_years_review} years is worth a look, and clearly beyond that is out."
    )
    #: The "Role:" bullet: what kind of work counts at all.
    role_rule: str = (
        "software development / IT in the broad sense (backend, frontend, full-stack, embedded, "
        "DevOps, QA automation, data engineering, game dev, systems, cloud)."
    )
    #: Extra bullets appended to the match criteria, one line each.
    extra_rules: list[str] = Field(default_factory=list)


class AIPolicy(BaseModel):
    prefilter_model: str = "claude-sonnet-5"
    rank_model: str = "claude-opus-5"
    prefilter_effort: str = "low"
    rank_effort: str = "high"
    rank_top_n: int = 60
    prefilter_min_score: int = 30
    concurrency: int = 4
    max_description_chars: int = 6000


class Profile(BaseModel):
    name: str
    summary: str
    cv_text: str = ""
    skills: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    languages: LanguagePolicy = Field(default_factory=LanguagePolicy)
    seniority: SeniorityPolicy = Field(default_factory=SeniorityPolicy)
    location: LocationPolicy = Field(default_factory=LocationPolicy)
    role: RolePolicy = Field(default_factory=RolePolicy)
    prompt: PromptPolicy = Field(default_factory=PromptPolicy)
    ai: AIPolicy = Field(default_factory=AIPolicy)
    max_age_days: int = 45  # postings older than this are dropped by the rule filter (unknown dates stay)


class SourceConfig(BaseModel):
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


class Settings(BaseModel):
    profile: Profile
    sources: dict[str, SourceConfig]
    http: dict[str, Any] = Field(default_factory=dict)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_settings(config_dir: Path | None = None) -> Settings:
    cfg = Path(config_dir) if config_dir else paths().config
    profile_raw = _load_yaml(cfg / "profile.yaml")
    sources_raw = _load_yaml(cfg / "sources.yaml")
    http_raw = sources_raw.pop("http", {}) or {}
    sources = {
        name: SourceConfig(**(opts or {})) if isinstance(opts, dict) and set(opts) <= {"enabled", "options"}
        else SourceConfig(enabled=(opts or {}).get("enabled", True), options={k: v for k, v in (opts or {}).items() if k != "enabled"})
        for name, opts in (sources_raw.get("sources") or {}).items()
    }
    return Settings(profile=Profile(**profile_raw), sources=sources, http=http_raw)
