"""Tests for :mod:`jobscraper.audit` — the stratified drop-audit sample and its scoring.

The rule filter drops ~84% of everything scraped and nobody has measured how much of that was
good. ``audit-drops`` draws a seeded, stratified sample the owner labels by hand, then turns the
labels back into a per-reason false-negative rate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobscraper import audit
from jobscraper import cli as cli_mod
from jobscraper import store as store_mod
from jobscraper.audit import (
    read_labels,
    reason_category,
    sample_drops,
    score_labels,
    totals_from_rows,
    write_rows,
)
from jobscraper.models import FilterResult, Job
from jobscraper.store import Store

runner = CliRunner()

#: Rich sizes its tables to the console; keep it wide so assertions see unwrapped cells.
WIDE = {"COLUMNS": "200"}

RULES_PY = Path(cli_mod.__file__).with_name("filters") / "rules.py"


# --------------------------------------------------------------------- categories

EXAMPLES = [
    ("title looks like a non-software role: 'Sales Engineer'", "non_software_title_hint"),
    ("title not a software/IT role: 'Help Desk Technician'", "not_software_title"),
    ("title level excluded by profile: 'Senior Backend Developer'", "level_excluded"),
    ("senior-level title: 'Senior Imaging Software Engineer'", "senior_title"),
    ("seniority label 'Senior' excluded by profile", "seniority_label"),
    ("asks for 7+ years of experience", "years_required"),
    ("posting written in de (confidence 0.95)", "posting_language"),
    ("requires de: 'fließend Deutsch'", "language_required"),
    ("on-site in GB, outside target countries", "location"),
    ("posting is 45 days old (max 30)", "too_old"),
    ("application deadline passed on 2026-09-01", "deadline_passed"),
    ("duplicate of 16af2ba071e7fe11", "duplicate"),
]


@pytest.mark.parametrize(("reason", "expected"), EXAMPLES)
def test_reason_category_maps_every_known_template(reason: str, expected: str) -> None:
    assert reason_category(reason) == expected


def test_unknown_reasons_fall_back_to_other() -> None:
    assert reason_category("the moon is in the wrong phase") == "other"
    assert reason_category("") == "other"


def test_every_drop_reason_in_rules_py_has_a_category() -> None:
    """A new ``reasons.append(f"...")`` in rules.py without a category must fail here."""
    source = RULES_PY.read_text(encoding="utf-8")
    templates = re.findall(r'reasons\.append\(f"([^"]*)"\)', source)
    assert len(templates) >= 9, templates
    for template in templates:
        # Render the f-string naively: every {placeholder} becomes a value-shaped stand-in.
        example = re.sub(r"\{[^{}]*!r\}", "'X'", template)
        example = re.sub(r"\{[^{}]*\}", "7", example)
        assert reason_category(example) != "other", f"no category for {template!r} ({example!r})"


def test_the_duplicate_reason_built_by_the_cli_has_a_category() -> None:
    source = Path(cli_mod.__file__).read_text(encoding="utf-8")
    assert 'reasons=[f"duplicate of {kept}"]' in source
    assert reason_category("duplicate of 0b5498503480f1ad") == "duplicate"


# ------------------------------------------------------------------------ sampling


def make_job(n: int, **kw) -> Job:
    base = {
        "source": "arbeitnow",
        "source_id": f"job-{n}",
        "url": f"https://jobs.example.test/{n}",
        "title": f"Job {n}",
        "company": "Example Oy",
        "location_raw": "Helsinki, Finland",
        "country": "FI",
        "remote": "onsite",
    }
    base.update(kw)
    return Job(**base)


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "audit.db")
    yield s
    s.close()


def seed_store(store: Store, counts: dict[str, int]) -> dict[str, list[Job]]:
    """Fill the store with dropped jobs; ``counts`` maps a primary reason to how many."""
    made: dict[str, list[Job]] = {}
    results: list[FilterResult] = []
    n = 0
    for reason, how_many in counts.items():
        jobs = []
        for _ in range(how_many):
            n += 1
            job = make_job(n, title=f"{reason[:20]} #{n}")
            jobs.append(job)
            results.append(FilterResult(job_id=job.id, status="drop",
                                        reasons=[reason, "asks for 9 years of experience"]))
        made[reason] = jobs
        store.upsert_jobs(jobs)
    store.save_filter_results(results, "test")
    return made


def test_sample_drops_takes_up_to_per_reason_from_each_category(store) -> None:
    seed_store(store, {
        "title not a software/IT role: 'X'": 10,
        "on-site in GB, outside target countries": 3,
    })
    rows = sample_drops(store, per_reason=4, seed=1)
    by_cat: dict[str, list[dict]] = {}
    for row in rows:
        by_cat.setdefault(row["category"], []).append(row)
    assert sorted(by_cat) == ["location", "not_software_title"]
    assert len(by_cat["not_software_title"]) == 4  # capped by per_reason
    assert len(by_cat["location"]) == 3  # the whole (smaller) category
    assert by_cat["not_software_title"][0]["total_in_category"] == 10
    assert by_cat["location"][0]["total_in_category"] == 3


def test_sample_drops_rows_carry_the_fields_the_owner_labels(store) -> None:
    made = seed_store(store, {"posting is 45 days old (max 30)": 1})
    job = made["posting is 45 days old (max 30)"][0]
    (row,) = sample_drops(store, per_reason=5, seed=3)
    assert row == {
        "job_id": job.id,
        "source": "arbeitnow",
        "title": job.title,
        "company": "Example Oy",
        "location_raw": "Helsinki, Finland",
        "country": "FI",
        "url": job.url,
        "category": "too_old",
        "reasons": ["posting is 45 days old (max 30)", "asks for 9 years of experience"],
        "total_in_category": 1,
        "label": "",
        "note": "",
    }


def test_only_the_primary_reason_decides_the_category(store) -> None:
    """A job dropped for several reasons is audited under the first one only."""
    seed_store(store, {"requires de: 'fließend Deutsch'": 2})
    rows = sample_drops(store, per_reason=5, seed=1)
    assert {r["category"] for r in rows} == {"language_required"}


def test_sample_drops_ignores_kept_and_reviewed_jobs(store) -> None:
    seed_store(store, {"on-site in GB, outside target countries": 2})
    keep, review = make_job(900), make_job(901)
    store.upsert_jobs([keep, review])
    store.save_filter_results(
        [FilterResult(job_id=keep.id, status="keep"),
         FilterResult(job_id=review.id, status="review", reasons=["location unknown"])],
        "test",
    )
    rows = sample_drops(store, per_reason=10, seed=1)
    assert {r["job_id"] for r in rows} == {j.id for j in seed_ids(store)}
    assert keep.id not in {r["job_id"] for r in rows}
    assert review.id not in {r["job_id"] for r in rows}


def seed_ids(store: Store) -> list[Job]:
    drops = store.filter_results("drop")
    return [j for j in store.jobs() if j.id in drops]


def test_sample_drops_is_reproducible_for_a_seed_and_varies_with_it(store) -> None:
    seed_store(store, {"title not a software/IT role: 'X'": 40})
    first = [r["job_id"] for r in sample_drops(store, per_reason=5, seed=1)]
    again = [r["job_id"] for r in sample_drops(store, per_reason=5, seed=1)]
    other = [r["job_id"] for r in sample_drops(store, per_reason=5, seed=2)]
    assert first == again
    assert first != other


def test_sample_drops_skips_a_drop_row_whose_job_is_gone(store) -> None:
    """Filter rows outlive their jobs when a database is pruned; the sample must not crash."""
    seed_store(store, {"posting written in de (confidence 0.99)": 2})
    ghost = FilterResult(job_id="deadbeefdeadbeef", status="drop",
                         reasons=["posting written in de (confidence 0.99)"])
    store.save_filter_results([ghost], "test")
    rows = sample_drops(store, per_reason=10, seed=1)
    assert len(rows) == 2
    assert "deadbeefdeadbeef" not in {r["job_id"] for r in rows}
    assert rows[0]["total_in_category"] == 3  # the ghost still counts as a drop


def test_sample_drops_on_an_empty_database(store) -> None:
    assert sample_drops(store, per_reason=5, seed=1) == []


# ------------------------------------------------------------------------ scoring


def label_rows(category: str, labels: list[str], total: int) -> list[dict]:
    return [{"category": category, "total_in_category": total, "label": lab} for lab in labels]


def test_score_labels_computes_the_false_negative_rate_per_category() -> None:
    rows = label_rows("not_software_title", ["good", "bad", "bad", "bad", "unsure", ""], 1000)
    out = score_labels(rows, {"not_software_title": 1000})
    cat = out["categories"]["not_software_title"]
    assert cat["sampled"] == 6
    assert cat["labelled"] == 5  # good + bad + unsure; the blank one is not labelled
    assert (cat["good"], cat["bad"], cat["unsure"], cat["unlabelled"]) == (1, 3, 1, 1)
    assert cat["rate"] == pytest.approx(0.25)  # unsure and unlabelled are out of the denominator
    assert cat["total"] == 1000
    assert cat["estimated_lost"] == pytest.approx(250.0)


def test_score_labels_leaves_the_rate_unknown_without_a_verdict() -> None:
    out = score_labels(label_rows("too_old", ["unsure", ""], 40), {"too_old": 40})
    cat = out["categories"]["too_old"]
    assert cat["rate"] is None
    assert cat["estimated_lost"] is None
    assert cat["labelled"] == 1


def test_score_labels_pools_the_categories_into_an_overall_estimate() -> None:
    rows = label_rows("not_software_title", ["good", "bad"], 1000) + \
        label_rows("location", ["good", "good", "bad", "bad"], 100)
    out = score_labels(rows, {"not_software_title": 1000, "location": 100})
    overall = out["overall"]
    assert overall["sampled"] == 6
    assert overall["labelled"] == 6
    assert (overall["good"], overall["bad"]) == (3, 3)
    assert overall["rate"] == pytest.approx(0.5)
    assert overall["total"] == 1100
    # 0.5 * 1000 + 0.5 * 100, summed per category rather than from the pooled rate
    assert overall["estimated_lost"] == pytest.approx(550.0)


def test_score_labels_counts_labels_it_does_not_understand() -> None:
    rows = label_rows("location", ["good", "yes please", "BAD "], 10)
    out = score_labels(rows, {"location": 10})
    cat = out["categories"]["location"]
    assert cat["invalid"] == 1
    assert cat["bad"] == 1  # case and stray whitespace are forgiven
    assert out["overall"]["invalid"] == 1


def test_score_labels_falls_back_to_zero_for_a_category_without_a_total() -> None:
    out = score_labels(label_rows("other", ["good", "bad"], 0), {})
    assert out["categories"]["other"]["total"] == 0
    assert out["categories"]["other"]["estimated_lost"] == pytest.approx(0.0)


def test_totals_from_rows_reads_the_totals_the_sample_recorded() -> None:
    rows = label_rows("location", ["good"], 12) + label_rows("too_old", ["bad"], 5)
    assert totals_from_rows(rows) == {"location": 12, "too_old": 5}


# ----------------------------------------------------------------------- JSONL I/O


def test_write_rows_and_read_labels_round_trip(tmp_path) -> None:
    rows = [{"job_id": "a", "category": "location", "reasons": ["on-site in GB, outside target countries"],
             "total_in_category": 3, "label": "", "note": "ä"}]
    path = tmp_path / "nested" / "sample.jsonl"
    write_rows(path, rows)
    assert path.exists()
    assert read_labels(path) == rows


def test_read_labels_ignores_blank_lines(tmp_path) -> None:
    path = tmp_path / "sample.jsonl"
    path.write_text('{"category": "location", "label": "good"}\n\n', encoding="utf-8")
    assert read_labels(path) == [{"category": "location", "label": "good"}]


# ------------------------------------------------------------------------ CLI


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSCRAPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JOBSCRAPER_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(store_mod, "DB_OVERRIDE", None)
    monkeypatch.delenv("JOBSCRAPER_DB", raising=False)
    return tmp_path


def build_db(path: Path) -> None:
    s = Store(path)
    try:
        seed_store(s, {"title not a software/IT role: 'X'": 6,
                       "on-site in GB, outside target countries": 2})
    finally:
        s.close()


def test_audit_drops_writes_a_sample_and_prints_the_table(data_dir) -> None:
    db = data_dir / "jobs.db"
    build_db(db)
    result = runner.invoke(cli_mod.app, ["--db", str(db), "audit-drops", "--per-reason", "3", "--seed", "2"], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "not_software_title" in result.output
    assert "location" in result.output

    written = list((data_dir / "labels").glob("drop-audit-*.jsonl"))
    assert len(written) == 1
    rows = [json.loads(line) for line in written[0].read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 5  # 3 of 6 + 2 of 2
    assert all(r["label"] == "" for r in rows)
    assert written[0].name in result.output.replace("\n", "")


def test_audit_drops_honours_an_explicit_output_path(data_dir, tmp_path) -> None:
    db = data_dir / "jobs.db"
    build_db(db)
    out = tmp_path / "elsewhere" / "sample.jsonl"
    result = runner.invoke(cli_mod.app, ["--db", str(db), "audit-drops", "--out", str(out)], env=WIDE)
    assert result.exit_code == 0, result.output
    assert len(read_labels(out)) == 8


def test_audit_drops_refuses_to_overwrite_an_existing_sample(data_dir, tmp_path) -> None:
    db = data_dir / "jobs.db"
    build_db(db)
    out = tmp_path / "sample.jsonl"
    out.write_text("hand-labelled, do not clobber\n", encoding="utf-8")
    result = runner.invoke(cli_mod.app, ["--db", str(db), "audit-drops", "--out", str(out)], env=WIDE)
    assert result.exit_code != 0
    assert "already exists" in result.output
    assert out.read_text(encoding="utf-8").startswith("hand-labelled")


def test_audit_drops_warns_when_there_is_nothing_to_sample(data_dir) -> None:
    db = data_dir / "jobs.db"
    Store(db).close()
    result = runner.invoke(cli_mod.app, ["--db", str(db), "audit-drops"], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "no dropped jobs" in result.output


def test_audit_drops_score_prints_the_rates_and_a_commit_line(data_dir, tmp_path) -> None:
    rows = (label_rows("not_software_title", ["good", "bad", "bad", "bad"], 1000)
            + label_rows("location", ["unsure", ""], 100))
    path = tmp_path / "labelled.jsonl"
    write_rows(path, rows)
    result = runner.invoke(cli_mod.app, ["audit-drops", "--score", str(path)], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "not_software_title" in result.output
    assert "25" in result.output  # the 25% false-negative rate
    assert "250" in result.output  # ≈250 good jobs lost in that category
    assert "drop audit:" in result.output  # the one-line summary for a commit message


def test_audit_drops_score_flags_labels_it_could_not_read(data_dir, tmp_path) -> None:
    path = tmp_path / "labelled.jsonl"
    write_rows(path, label_rows("location", ["good", "maybe?"], 10))
    result = runner.invoke(cli_mod.app, ["audit-drops", "--score", str(path)], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "unrecognised label" in result.output


def test_audit_drops_score_rejects_a_missing_file(data_dir, tmp_path) -> None:
    result = runner.invoke(cli_mod.app, ["audit-drops", "--score", str(tmp_path / "nope.jsonl")])
    assert result.exit_code != 0


def test_the_audit_module_is_documented_in_the_cli_help() -> None:
    assert "audit-drops" in cli_mod.__doc__
    assert audit.__doc__


def test_labels_directory_is_git_ignored() -> None:
    ignore = Path(cli_mod.__file__).resolve().parents[2] / ".gitignore"
    assert "data/labels/" in ignore.read_text(encoding="utf-8")
