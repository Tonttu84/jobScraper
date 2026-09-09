"""Per-run databases, read-only opening, and the decisions sidecar."""

from __future__ import annotations

import sqlite3

import pytest

from jobscraper import store as store_mod
from jobscraper.models import Decision, Job
from jobscraper.store import Store, copy_db, default_db_path, new_run_db


def _job(sid: str = "1") -> Job:
    return Job(source="t", source_id=sid, url=f"https://x/{sid}", title=f"Junior Dev {sid}", company="Acme")


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBSCRAPER_DB", raising=False)
    monkeypatch.setattr(store_mod, "DATA_DIR", tmp_path)
    return tmp_path


def test_default_db_path_prefers_env_then_newest_run_then_legacy(data_dir, monkeypatch):
    assert default_db_path() == data_dir / "jobs.db"
    runs = data_dir / "runs"
    runs.mkdir()
    (runs / "20260901-100000.db").write_bytes(b"")
    (runs / "20260909-100000.db").write_bytes(b"")
    assert default_db_path() == runs / "20260909-100000.db"
    monkeypatch.setenv("JOBSCRAPER_DB", str(data_dir / "elsewhere.db"))
    assert default_db_path() == data_dir / "elsewhere.db"


def test_new_run_db_copies_previous_forward(data_dir):
    s = Store()  # legacy data/jobs.db
    s.upsert_jobs([_job("1")])
    s.close()
    path = new_run_db()
    assert path.parent == data_dir / "runs" and path.suffix == ".db"
    assert default_db_path() == path
    s2 = Store()
    assert [j.source_id for j in s2.jobs()] == ["1"]
    s2.upsert_jobs([_job("2")])
    s2.close()
    # the previous database is untouched
    legacy = Store(data_dir / "jobs.db")
    assert len(legacy.jobs()) == 1
    legacy.close()
    fresh = new_run_db(fresh=True)
    assert fresh != path
    s3 = Store(fresh)
    assert s3.jobs() == []
    s3.close()


def test_new_run_db_names_do_not_collide(data_dir):
    a = new_run_db(fresh=True)
    b = new_run_db(fresh=True)
    assert a != b


def test_copy_db_is_a_faithful_snapshot(tmp_path):
    src = Store(tmp_path / "a.db")
    src.upsert_jobs([_job("1"), _job("2")])
    copy_db(src.path, tmp_path / "out" / "b.db")
    src.close()
    dst = Store(tmp_path / "out" / "b.db")
    assert len(dst.jobs()) == 2
    dst.close()


def test_readonly_store_rejects_writes(tmp_path):
    w = Store(tmp_path / "a.db")
    w.upsert_jobs([_job("1")])
    w.close()
    ro = Store(tmp_path / "a.db", readonly=True)
    assert len(ro.jobs()) == 1
    with pytest.raises(sqlite3.OperationalError):
        ro.upsert_jobs([_job("2")])
    ro.close()


def test_readonly_store_missing_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        Store(tmp_path / "nope.db", readonly=True)


def test_decisions_sidecar_survives_swapping_the_job_db(tmp_path):
    for name in ("run1.db", "run2.db"):
        s = Store(tmp_path / name)
        s.upsert_jobs([_job("1")])
        s.close()
    side = tmp_path / "decisions.db"
    a = Store(tmp_path / "run1.db", readonly=True, decisions_path=side)
    a.save_decision(Decision(job_id=_job("1").id, user="jo", status="applied"))
    assert [d.user for d in a.decisions()] == ["jo"] and a.decision_users() == ["jo"]
    a.close()
    # the job DB itself was not written to
    raw = sqlite3.connect(tmp_path / "run1.db")
    assert raw.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    raw.close()
    b = Store(tmp_path / "run2.db", readonly=True, decisions_path=side)
    assert [d.status for d in b.decisions("jo")] == ["applied"]
    assert b.delete_decision(_job("1").id, "jo") is True
    assert b.decisions() == []
    b.close()
