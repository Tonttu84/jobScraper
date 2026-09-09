"""Serving a directory of database copies: newest wins, decisions persist in a sidecar."""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from jobscraper.models import Job
from jobscraper.store import Store
from jobscraper.web.app import create_app


def _seed(path, sid: str) -> str:
    s = Store(path)
    job = Job(source="t", source_id=sid, url=f"https://x/{sid}", title=f"Junior Dev {sid}", company="Acme")
    s.upsert_jobs([job])
    s.close()
    return job.id


@pytest.fixture
def serve_dir(tmp_path):
    d = tmp_path / "serve"
    d.mkdir()
    return d


def test_empty_serve_dir_gives_503(serve_dir):
    with TestClient(create_app(serve_dir=serve_dir)) as client:
        r = client.get("/api/meta")
        assert r.status_code == 503
        assert "no database" in r.json()["detail"].lower()


def test_newest_db_is_served_and_named(serve_dir):
    old_id = _seed(serve_dir / "old.db", "old")
    old_time = time.time() - 100
    os.utime(serve_dir / "old.db", (old_time, old_time))
    new_id = _seed(serve_dir / "new.db", "new")
    with TestClient(create_app(serve_dir=serve_dir)) as client:
        meta = client.get("/api/meta").json()
        assert meta["database"] == "new.db"
        assert client.get(f"/api/jobs/{new_id}").status_code == 200
        assert client.get(f"/api/jobs/{old_id}").status_code == 404


def test_decisions_persist_when_the_served_db_is_replaced(serve_dir):
    job_id = _seed(serve_dir / "run1.db", "1")
    with TestClient(create_app(serve_dir=serve_dir)) as client:
        r = client.put(f"/api/jobs/{job_id}/decision", json={"user": "jo", "status": "applied"})
        assert r.status_code == 200, r.text
        assert client.get("/api/meta").json()["users"] == ["jo"]
    # decisions went to the sidecar, not into the copy
    assert (serve_dir / "decisions.db").exists()
    ro = Store(serve_dir / "run1.db")
    assert ro.decisions() == []
    ro.close()
    # drop in a newer copy containing the same job
    (serve_dir / "run1.db").unlink()
    _seed(serve_dir / "run2.db", "1")
    with TestClient(create_app(serve_dir=serve_dir)) as client:
        assert client.get("/api/meta").json()["database"] == "run2.db"
        decisions = client.get("/api/decisions", params={"user": "jo"}).json()
        assert [d["status"] for d in decisions] == ["applied"]
        assert client.get(f"/api/jobs/{job_id}", params={"user": "jo"}).status_code == 200


def test_sidecar_is_never_picked_as_the_served_db(serve_dir):
    _seed(serve_dir / "decisions.db", "x")  # a stray file with the reserved name
    with TestClient(create_app(serve_dir=serve_dir)) as client:
        assert client.get("/api/meta").status_code == 503


def test_single_db_mode_still_works(tmp_path):
    job_id = _seed(tmp_path / "one.db", "1")
    with TestClient(create_app(tmp_path / "one.db")) as client:
        meta = client.get("/api/meta").json()
        assert meta["database"] == "one.db"
        assert client.put(f"/api/jobs/{job_id}/decision", json={"user": "jo", "status": "applied"}).status_code == 200
