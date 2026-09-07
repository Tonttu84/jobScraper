"""YAML configuration: candidate profile and source settings."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("JOBSCRAPER_CONFIG_DIR", ROOT / "config"))
DATA_DIR = Path(os.environ.get("JOBSCRAPER_DATA_DIR", ROOT / "data"))


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


class LocationPolicy(BaseModel):
    tier1: list[str] = Field(default_factory=lambda: ["FI", "EE"])
    tier2: list[str] = Field(default_factory=list)
    tier3: list[str] = Field(default_factory=list)
    keep_all_remote: bool = True
    notes: dict[str, str] = Field(default_factory=dict)


class RolePolicy(BaseModel):
    title_terms: list[str] = Field(default_factory=list)
    exclude_title_terms: list[str] = Field(default_factory=list)


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
    ai: AIPolicy = Field(default_factory=AIPolicy)


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
    cfg = Path(config_dir) if config_dir else CONFIG_DIR
    profile_raw = _load_yaml(cfg / "profile.yaml")
    sources_raw = _load_yaml(cfg / "sources.yaml")
    http_raw = sources_raw.pop("http", {}) or {}
    sources = {
        name: SourceConfig(**(opts or {})) if isinstance(opts, dict) and set(opts) <= {"enabled", "options"}
        else SourceConfig(enabled=(opts or {}).get("enabled", True), options={k: v for k, v in (opts or {}).items() if k != "enabled"})
        for name, opts in (sources_raw.get("sources") or {}).items()
    }
    return Settings(profile=Profile(**profile_raw), sources=sources, http=http_raw)
