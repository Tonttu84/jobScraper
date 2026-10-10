"""`jobscraper eval`: the screen and the ranker graded against the owner's own decisions.

docs/PLAN.md item 4. The web UI records what the owner did with each job (applied, skipped, ...);
this module turns those decisions into Sonnet screen precision/recall and Opus precision@k, so a
rule or prompt change can carry a before/after number in its commit message.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest
from typer.testing import CliRunner

from jobscraper import audit
from jobscraper import cli as cli_mod
from jobscraper import store as store_mod
from jobscraper.ai.prompts import PROMPT_VERSION
from jobscraper.config import Profile
from jobscraper.models import AIVerdict, Decision, DecisionStatus, FilterResult, Job
from jobscraper.store import Store

runner = CliRunner()
WIDE = {"COLUMNS": "200"}
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def make_job(n: int) -> Job:
    return Job(source="arbeitnow", source_id=f"eval-{n}", url=f"https://jobs.example.test/{n}",
               title=f"Job {n}", company="Example Oy", location_raw="Helsinki, Finland",
               country="FI", remote="onsite")


def verdict(job_id: str, stage: str, score: int, *, relevant: bool = True,
            position: int | None = None) -> AIVerdict:
    return AIVerdict(job_id=job_id, stage=stage, model="m", prompt_version=PROMPT_VERSION,
                     relevant=relevant, score=score, language_ok=True, seniority_ok=True,
                     location_ok=True, summary="s", position=position)


def decision(job_id: str, status: str, *, user: str = "alice", hours: int = 0) -> Decision:
    return Decision(job_id=job_id, user=user, status=status, updated_at=T0 + timedelta(hours=hours))


def build_state() -> dict:
    """Ten jobs, worked out by hand (screen gate: relevant and score >= 30).

    job  decision                       filter  screen        rank  refine  effective
    1    applied                        keep    80 pass       90    70 #1   80
    2    interview                      keep    75 pass       85            85
    3    skipped                        keep    70 pass       60            60
    4    offer                          keep    60 not rel.   -             -
    5    skipped                        keep    75 pass       -             -
    6    rejected (employer)            drop    -             -             -
    7    interested                     keep    -             -             -
    8    closed                         keep    90 pass       95            95
    9    bob skipped, later alice appl. keep    65 pass       50            50
    10   (none)                         keep    85 pass       88            88
    """
    jobs = [make_job(n) for n in range(1, 11)]
    j = {n: job.id for n, job in enumerate(jobs, start=1)}
    filters = {i: FilterResult(job_id=i, status="keep") for i in j.values()}
    filters[j[6]] = FilterResult(job_id=j[6], status="drop",
                                 reasons=["title not a software/IT role: 'Cashier'",
                                          "asks for 9 years of experience"])
    prefilter = {j[n]: verdict(j[n], "prefilter", s) for n, s in
                 ((1, 80), (2, 75), (3, 70), (5, 75), (8, 90), (9, 65), (10, 85))}
    prefilter[j[4]] = verdict(j[4], "prefilter", 60, relevant=False)
    rank = {j[n]: verdict(j[n], "rank", s) for n, s in
            ((1, 90), (2, 85), (3, 60), (8, 95), (9, 50), (10, 88))}
    refine = {j[1]: verdict(j[1], "refine", 70, position=1)}
    decisions = [
        decision(j[1], "applied", hours=1),
        decision(j[2], "interview", hours=2),
        decision(j[3], "skipped", hours=3),
        decision(j[4], "offer", hours=4),
        decision(j[5], "skipped", hours=5),
        decision(j[6], "rejected", hours=6),
        decision(j[7], "interested", hours=7),
        decision(j[8], "closed", hours=8),
        decision(j[9], "skipped", user="bob", hours=9),
        decision(j[9], "applied", hours=10),  # later: wins over bob's skip
    ]
    return {"jobs": jobs, "ids": j, "filters": filters, "prefilter": prefilter, "rank": rank,
            "refine": refine, "decisions": decisions}


def run_eval(state: dict, *, decisions: list[Decision] | None = None, jobs: list[Job] | None = None,
             top_ks=(3, 10), min_score: int = 30) -> dict:
    profile = Profile(name="T", summary="")
    profile.ai.prefilter_min_score = min_score
    return audit.decision_eval(state["jobs"] if jobs is None else jobs, state["filters"],
                               state["prefilter"], state["rank"], state["refine"],
                               state["decisions"] if decisions is None else decisions,
                               profile, top_ks=top_ks)


# ------------------------------------------------------------------ labels


def test_every_decision_status_is_either_labelled_or_excluded() -> None:
    statuses = set(get_args(DecisionStatus))
    assert set(audit.DECISION_LABELS) | set(audit.EXCLUDED_STATUSES) == statuses
    assert not set(audit.DECISION_LABELS) & set(audit.EXCLUDED_STATUSES)
    assert audit.EXCLUDED_STATUSES == ("closed",)
    assert audit.DECISION_LABELS["rejected"] is True  # the employer's no, not the owner's
    assert audit.DECISION_LABELS["skipped"] is False


def test_latest_decision_per_job_wins_across_users() -> None:
    older = decision("a", "applied", user="alice", hours=1)
    newer = decision("a", "skipped", user="bob", hours=2)
    assert audit.latest_decisions([older, newer]) == {"a": newer}
    assert audit.latest_decisions([newer, older]) == {"a": newer}


def test_counts_merge_users_and_keep_closed_out_of_both_sides() -> None:
    out = run_eval(build_state())
    assert out["counts"] == {
        "decisions_read": 10, "jobs_decided": 9, "superseded": 1, "closed": 1, "usable": 8,
        "positives": 6, "negatives": 2, "without_verdict": 2, "outside_window": 0,
    }


def test_the_later_user_decides_a_contested_job() -> None:
    state = build_state()
    j9 = state["ids"][9]
    flipped = [d for d in state["decisions"] if d.job_id != j9] + [
        decision(j9, "applied", hours=9), decision(j9, "skipped", user="bob", hours=10)]
    out = run_eval(state, decisions=flipped)
    assert out["counts"]["positives"] == 5 and out["counts"]["negatives"] == 3


# ------------------------------------------------------------------ screen


def test_screen_precision_and_recall_by_hand() -> None:
    screen = run_eval(build_state())["screen"]
    # screened labelled: 1+ 2+ 3- 4+(fails) 5- 9+ ; passed: 1 2 3 5 9
    assert screen["min_score"] == 30
    assert screen["screened"] == 6
    assert screen["passed"] == 5
    assert screen["positives_screened"] == 4
    assert screen["positives_passed"] == 3
    assert screen["negatives_passed"] == 2
    assert screen["precision"] == pytest.approx(3 / 5)
    assert screen["recall"] == pytest.approx(3 / 4)


def test_positives_the_screen_never_saw_are_split_by_why() -> None:
    unseen = run_eval(build_state())["screen"]["unscreened_positives"]
    assert unseen == {"rule_dropped": {"not_software_title": 1}, "rule_dropped_total": 1,
                      "never_screened": 1}


def test_the_screen_gate_is_inclusive_of_the_minimum_score() -> None:
    state = build_state()
    j1 = state["ids"][1]
    state["prefilter"][j1] = verdict(j1, "prefilter", 30)
    assert run_eval(state)["screen"]["positives_passed"] == 3
    assert run_eval(state, min_score=31)["screen"]["positives_passed"] == 2


# ------------------------------------------------------------------ rank


def test_global_effective_top_is_split_by_label() -> None:
    ranks = run_eval(build_state())["rank"]
    assert ranks["offset"] == 0.0  # one refine verdict: too few to calibrate
    # effective order: 8 (closed) 95, 10 (unlabelled) 88, 2+ 85, 1+ 80, 3- 60, 9+ 50
    assert ranks["global"] == [
        {"k": 3, "size": 3, "positive": 1, "negative": 0, "unlabelled": 1, "excluded": 1},
        {"k": 10, "size": 6, "positive": 3, "negative": 1, "unlabelled": 1, "excluded": 1},
    ]


def test_labelled_precision_at_k_caps_k_at_the_labelled_count() -> None:
    labelled = run_eval(build_state())["rank"]["labelled"]
    # labelled with an effective score, best first: 2+ 85, 1+ 80, 3- 60, 9+ 50
    assert labelled["scored"] == 4
    assert labelled["order"] == [build_state()["ids"][n] for n in (2, 1, 3, 9)]
    at3, at10 = labelled["at"]
    assert at3 == {"k": 3, "k_used": 3, "positives": 2, "precision": pytest.approx(2 / 3)}
    assert at10 == {"k": 10, "k_used": 4, "positives": 3, "precision": pytest.approx(3 / 4)}


def test_mean_scores_and_spearman_against_the_label() -> None:
    labelled = run_eval(build_state())["rank"]["labelled"]
    assert labelled["mean_positive"] == pytest.approx((85 + 80 + 50) / 3)
    assert labelled["mean_negative"] == pytest.approx(60)
    # ranks of [85, 80, 60, 50] = [4, 3, 2, 1]; of labels [1, 1, 0, 1] = [3, 3, 1, 3]
    assert labelled["spearman"] == pytest.approx(1 / math.sqrt(15))


def test_a_labelled_job_outside_the_day_window_still_counts_in_the_labelled_view() -> None:
    state = build_state()
    j9 = state["ids"][9]
    out = run_eval(state, jobs=[job for job in state["jobs"] if job.id != j9])
    assert out["counts"]["outside_window"] == 1
    assert out["rank"]["global"][1]["size"] == 5  # the global top only reads the window
    assert out["rank"]["labelled"]["scored"] == 4


def test_nothing_to_measure_gives_none_rather_than_dividing_by_zero() -> None:
    state = build_state()
    out = audit.decision_eval([], {}, {}, {}, {}, [], Profile(name="T", summary=""))
    assert out["counts"]["usable"] == 0
    assert out["screen"]["precision"] is None and out["screen"]["recall"] is None
    assert out["rank"]["global"] == [
        {"k": 10, "size": 0, "positive": 0, "negative": 0, "unlabelled": 0, "excluded": 0},
        {"k": 20, "size": 0, "positive": 0, "negative": 0, "unlabelled": 0, "excluded": 0},
    ]
    labelled = out["rank"]["labelled"]
    assert labelled["at"][0] == {"k": 10, "k_used": 0, "positives": 0, "precision": None}
    assert labelled["mean_positive"] is None and labelled["mean_negative"] is None
    assert labelled["spearman"] is None
    json.dumps(out)
    json.dumps(run_eval(state))  # the full dict is JSON-serialisable


def test_eval_summary_line_formats_missing_numbers_as_a_dash() -> None:
    out = run_eval(build_state())
    assert audit.eval_summary(out) == "eval: 8 decisions — screen P 0.60 / R 0.75, rank P@3 0.67 P@10 0.75 (k=4)"
    empty = audit.decision_eval([], {}, {}, {}, {}, [], Profile(name="T", summary=""))
    assert audit.eval_summary(empty) == "eval: 0 decisions — screen P — / R —, rank P@10 — (k=0) P@20 — (k=0)"


# ------------------------------------------------------------------ CLI


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSCRAPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JOBSCRAPER_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(store_mod, "DB_OVERRIDE", None)
    monkeypatch.delenv("JOBSCRAPER_DB", raising=False)
    return tmp_path


def save_decisions(store: Store, decisions: list[Decision]) -> None:
    """save_decision stamps "now"; put the hand-chosen timestamps back so latest-wins is exact."""
    for d in decisions:
        store.save_decision(d)
        store.conn.execute(f"UPDATE {store._decisions} SET updated_at=? WHERE job_id=? AND user=?",
                           (d.updated_at.isoformat(), d.job_id, d.user))
    store.conn.commit()


def build_db(path: Path, *, sidecar: Path | None = None) -> None:
    state = build_state()
    s = Store(path)
    try:
        s.upsert_jobs(state["jobs"])
        s.save_filter_results(list(state["filters"].values()), "test")
        for stage in ("prefilter", "rank", "refine"):
            for v in state[stage].values():
                s.save_verdict(v)
        if sidecar is None:
            save_decisions(s, state["decisions"])
    finally:
        s.close()
    if sidecar is not None:
        side = Store(path, decisions_path=sidecar)
        try:
            save_decisions(side, state["decisions"])
        finally:
            side.close()


def test_eval_prints_the_tables_and_a_commit_line(data_dir) -> None:
    db = data_dir / "jobs.db"
    build_db(db)
    result = runner.invoke(cli_mod.app, ["--db", str(db), "eval", "--min-decisions", "5"], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "not_software_title" in result.output  # the rule-dropped positive
    assert "eval: 8 decisions — screen P 0.60 / R 0.75, rank P@10 0.75 (k=4) P@20 0.75 (k=4)" \
        in result.output


def test_eval_refuses_to_grade_on_too_few_decisions(data_dir) -> None:
    db = data_dir / "jobs.db"
    build_db(db)
    result = runner.invoke(cli_mod.app, ["--db", str(db), "eval"], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "8 usable decisions" in result.output and "at least 10" in result.output
    assert "screen P" not in result.output


def test_eval_user_restricts_to_one_users_decisions(data_dir) -> None:
    db = data_dir / "jobs.db"
    build_db(db)
    result = runner.invoke(cli_mod.app, ["--db", str(db), "eval", "--user", "bob",
                                         "--min-decisions", "1"], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "eval: 1 decisions" in result.output


def test_eval_json_writes_the_full_metrics(data_dir, tmp_path) -> None:
    db = data_dir / "jobs.db"
    build_db(db)
    out = tmp_path / "out" / "eval.json"
    result = runner.invoke(cli_mod.app, ["--db", str(db), "eval", "--min-decisions", "5",
                                         "--json", str(out)], env=WIDE)
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["counts"]["usable"] == 8 and data["counts"]["closed"] == 1
    assert data["screen"]["precision"] == pytest.approx(0.6)
    assert data["rank"]["labelled"]["at"][0]["k"] == 10


def test_eval_reads_the_web_uis_decision_sidecar_when_there_is_one(data_dir) -> None:
    db = data_dir / "jobs.db"
    build_db(db, sidecar=data_dir / "serve" / "decisions.db")
    result = runner.invoke(cli_mod.app, ["--db", str(db), "eval", "--min-decisions", "5"], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "eval: 8 decisions" in result.output


def test_eval_takes_an_explicit_decisions_file(data_dir, tmp_path) -> None:
    db = data_dir / "jobs.db"
    sidecar = tmp_path / "elsewhere" / "decisions.db"
    build_db(db, sidecar=sidecar)
    result = runner.invoke(cli_mod.app, ["--db", str(db), "eval", "--min-decisions", "5",
                                         "--decisions", str(sidecar)], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "eval: 8 decisions" in result.output


def test_eval_rejects_a_missing_decisions_file(data_dir, tmp_path) -> None:
    db = data_dir / "jobs.db"
    build_db(db)
    result = runner.invoke(cli_mod.app, ["--db", str(db), "eval", "--decisions",
                                         str(tmp_path / "nope.db")])
    assert result.exit_code != 0
