"""scripts/probe_boards.py — the live check for every `ats_boards` entry of a config.

The script is not part of the package, so it is loaded from its path. Its scrapers are the
same fakes the adapter tests use: no network is touched.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ats_scrapers import Job as ATSJob
from ats_scrapers.exceptions import ScraperError

from jobscraper.sources import ats_boards

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_boards.py"
GREENHOUSE = "https://boards.greenhouse.io/wolt"


def load_script():
    spec = importlib.util.spec_from_file_location("probe_boards", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_boards"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe():
    return load_script()


def ats_job(
    title: str,
    *,
    ats_id: str = "1",
    location: str = "Helsinki, Finland",
    base: str = GREENHOUSE,
) -> ATSJob:
    return ATSJob(
        url=f"{base}/jobs/{ats_id}",
        title=title,
        company="Wolt",
        ats_type="greenhouse",
        ats_id=ats_id,
        location=location,
        country_iso="FI",
        posted_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


class FakeScraper:
    def __init__(self, jobs, **kwargs):
        self.jobs = jobs
        self.kwargs = kwargs

    def fetch(self):
        return list(self.jobs)


class LazyScraper(FakeScraper):
    """Detail-fetch support, like Workday/Oracle: the listing carries no descriptions."""

    def __init__(self, jobs, **kwargs):
        super().__init__(jobs, **kwargs)
        self.described: list[str] = []

    def get_description(self, job):
        self.described.append(job.title)
        return f"body of {job.title}"


def patch_boards(monkeypatch, mapping):
    """Board key (careers URL, or ``(ats, slug)``) → jobs, an exception, or (cls, jobs)."""
    made: list[FakeScraper] = []

    def build(key, kwargs):
        result = mapping[key]
        if isinstance(result, Exception):
            raise result
        cls, jobs = result if isinstance(result, tuple) else (FakeScraper, result)
        scraper = cls(jobs, **kwargs)
        made.append(scraper)
        return scraper

    monkeypatch.setattr(ats_boards, "get_scraper_for_url", lambda url, **kw: build(url, kw))
    monkeypatch.setattr(ats_boards, "get_scraper", lambda ats, slug, **kw: build((ats, slug), kw))
    return made


def write_config(tmp_path: Path, urls: list, **options) -> Path:
    import yaml

    path = tmp_path / "sources.yaml"
    body = {
        "sources": {
            "ats_boards": {"enabled": True, "urls": urls, **options},
            "duunitori": {"enabled": True},
        }
    }
    path.write_text(yaml.safe_dump(body, allow_unicode=True), encoding="utf-8")
    return path


def test_probes_each_board_and_prints_a_converted_sample(probe, monkeypatch, tmp_path, capsys):
    patch_boards(
        monkeypatch,
        {
            GREENHOUSE: [
                ats_job("Junior Software Engineer", ats_id="1"),
                ats_job("Senior Software Engineer", ats_id="2"),
                ats_job("Head of Sales", ats_id="3"),
            ]
        },
    )
    cfg = write_config(
        tmp_path,
        [GREENHOUSE],
        default_include="engineer|developer",
        default_exclude=r"senior|head of",
    )

    assert probe.main(["--config", str(cfg)]) == 0
    out = capsys.readouterr().out

    assert GREENHOUSE in out
    assert "listed 3" in out
    assert "kept 1" in out
    # The converted sample carries our Job's fields, not the ats-scrapers ones.
    assert "Junior Software Engineer" in out
    assert "Wolt" in out
    assert "FI" in out
    assert "2026-09-01" in out
    assert "https://boards.greenhouse.io/wolt/jobs/1" in out
    assert "remote" in out.lower()


def test_label_substring_selects_boards(probe, monkeypatch, tmp_path, capsys):
    patch_boards(
        monkeypatch,
        {
            GREENHOUSE: [ats_job("Software Engineer")],
            ("oracle", "https://nokia.example/CX_1"): [
                ats_job("Software Engineer", ats_id="9", base="https://nokia.example")
            ],
        },
    )
    cfg = write_config(
        tmp_path,
        [
            GREENHOUSE,
            {"ats": "oracle", "slug": "https://nokia.example/CX_1", "company": "Nokia"},
        ],
    )

    assert probe.main(["--config", str(cfg), "oracle"]) == 0
    out = capsys.readouterr().out
    assert "oracle:https://nokia.example/CX_1" in out
    assert GREENHOUSE not in out
    assert "Nokia" in out  # the company override reaches the converted sample


def test_unmatched_substring_is_an_error(probe, tmp_path, capsys):
    cfg = write_config(tmp_path, [GREENHOUSE])
    assert probe.main(["--config", str(cfg), "nothing-matches-this"]) == 2
    assert "no board" in capsys.readouterr().err.lower()


def test_failing_board_is_reported_and_sets_the_exit_code(probe, monkeypatch, tmp_path, capsys):
    patch_boards(
        monkeypatch,
        {
            GREENHOUSE: ScraperError("403 from the board"),
            ("lever", "spotify"): [ats_job("Backend Developer", ats_id="7")],
        },
    )
    cfg = write_config(tmp_path, [GREENHOUSE, {"ats": "lever", "slug": "spotify"}])

    assert probe.main(["--config", str(cfg)]) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "403 from the board" in out
    assert "Backend Developer" in out  # the other board still ran
    assert "1 failed" in out


def test_sample_description_is_detail_fetched_only_once(probe, monkeypatch, tmp_path, capsys):
    """A lazy board must not pay for descriptions of postings the probe never shows."""
    jobs = [ats_job(f"Software Engineer {i}", ats_id=str(i)) for i in range(5)]
    made = patch_boards(monkeypatch, {("oracle", "acme"): (LazyScraper, jobs)})
    cfg = write_config(tmp_path, [{"ats": "oracle", "slug": "acme"}], default_include="engineer")

    assert probe.main(["--config", str(cfg)]) == 0
    lazy = made[-1]
    assert lazy.described == ["Software Engineer 0"]
    assert "kept 5" in capsys.readouterr().out


def test_empty_board_is_not_a_failure_but_says_so(probe, monkeypatch, tmp_path, capsys):
    patch_boards(monkeypatch, {GREENHOUSE: []})
    cfg = write_config(tmp_path, [GREENHOUSE])

    assert probe.main(["--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "listed 0" in out
    assert "no sample" in out.lower()


def test_missing_ats_boards_section_is_an_error(probe, tmp_path, capsys):
    path = tmp_path / "sources.yaml"
    path.write_text("sources:\n  duunitori:\n    enabled: true\n", encoding="utf-8")
    assert probe.main(["--config", str(path)]) == 2
    assert "ats_boards" in capsys.readouterr().err
