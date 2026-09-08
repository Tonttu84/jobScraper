"""SQLite persistence: jobs, filter results, AI verdicts, run log."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from jobscraper.config import DATA_DIR
from jobscraper.models import AIVerdict, FilterResult, Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    country TEXT,
    city TEXT,
    remote TEXT,
    posted_at TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_source ON jobs(source);
CREATE INDEX IF NOT EXISTS jobs_last_seen ON jobs(last_seen);

CREATE TABLE IF NOT EXISTS filter_results (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    location_tier INTEGER,
    rules_version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_verdicts (
    job_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    relevant INTEGER NOT NULL,
    score INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (job_id, stage, model, prompt_version)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    source TEXT NOT NULL,
    fetched INTEGER NOT NULL,
    new INTEGER NOT NULL,
    error TEXT
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DATA_DIR / "jobs.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------------ jobs
    def upsert_jobs(self, jobs: list[Job]) -> int:
        """Insert new jobs, refresh last_seen for known ones. Returns number of new jobs."""
        new = 0
        now = _now()
        with self.tx() as c:
            for job in jobs:
                row = c.execute("SELECT id FROM jobs WHERE id=?", (job.id,)).fetchone()
                data = job.model_dump_json()
                if row:
                    c.execute("UPDATE jobs SET last_seen=?, data=?, title=?, url=? WHERE id=?", (now, data, job.title, job.url, job.id))
                else:
                    new += 1
                    c.execute(
                        "INSERT INTO jobs (id, source, source_id, url, title, company, country, city, remote, posted_at, first_seen, last_seen, data)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (job.id, job.source, job.source_id, job.url, job.title, job.company, job.country, job.city, job.remote,
                         job.posted_at.isoformat() if job.posted_at else None, now, now, data),
                    )
        return new

    def jobs(self, *, source: str | None = None, seen_within_days: int | None = None, ids: list[str] | None = None) -> list[Job]:
        sql, args = "SELECT data FROM jobs", []
        clauses = []
        if source:
            clauses.append("source=?")
            args.append(source)
        if seen_within_days is not None:
            clauses.append("julianday('now') - julianday(last_seen) <= ?")
            args.append(seen_within_days)
        if ids is not None:
            if not ids:
                return []
            clauses.append(f"id IN ({','.join('?' * len(ids))})")
            args.extend(ids)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return [Job.model_validate_json(r["data"]) for r in self.conn.execute(sql, args)]

    def job_meta(self, job_id: str) -> dict | None:
        row = self.conn.execute("SELECT first_seen, last_seen FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- filters
    def save_filter_results(self, results: list[FilterResult], rules_version: str) -> None:
        now = _now()
        with self.tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO filter_results (job_id, status, location_tier, rules_version, updated_at, data) VALUES (?,?,?,?,?,?)",
                [(r.job_id, r.status, r.location_tier, rules_version, now, r.model_dump_json()) for r in results],
            )

    def filter_results(self, status: str | list[str] | None = None) -> dict[str, FilterResult]:
        sql, args = "SELECT data FROM filter_results", []
        if status:
            statuses = [status] if isinstance(status, str) else status
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
            args.extend(statuses)
        return {r.job_id: r for r in (FilterResult.model_validate_json(row["data"]) for row in self.conn.execute(sql, args))}

    # ------------------------------------------------------------- AI verdicts
    def save_verdict(self, v: AIVerdict) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO ai_verdicts (job_id, stage, model, prompt_version, relevant, score, created_at, data) VALUES (?,?,?,?,?,?,?,?)",
                (v.job_id, v.stage, v.model, v.prompt_version, int(v.relevant), v.score, v.created_at.isoformat(), v.model_dump_json()),
            )

    def verdicts(self, stage: str, prompt_version: str | None = None) -> dict[str, AIVerdict]:
        sql, args = "SELECT data FROM ai_verdicts WHERE stage=?", [stage]
        if prompt_version:
            sql += " AND prompt_version=?"
            args.append(prompt_version)
        out: dict[str, AIVerdict] = {}
        for row in self.conn.execute(sql + " ORDER BY created_at", args):
            v = AIVerdict.model_validate_json(row["data"])
            out[v.job_id] = v  # latest wins
        return out

    def delete_verdicts(self, job_ids: list[str], stage: str | None = None) -> int:
        """Forget verdicts for ``job_ids`` (one ``stage`` or every stage). Returns rows deleted.

        Used when a job's input changed — a hydrated description, say — and the stored verdict was
        formed without it, so the job has to go through the stage again.
        """
        if not job_ids:
            return 0
        sql = f"DELETE FROM ai_verdicts WHERE job_id IN ({','.join('?' * len(job_ids))})"
        args = list(job_ids)
        if stage:
            sql += " AND stage=?"
            args.append(stage)
        with self.tx() as c:
            return c.execute(sql, args).rowcount

    # ------------------------------------------------------------------- runs
    def log_run(self, source: str, fetched: int, new: int, error: str | None = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO runs (started_at, source, fetched, new, error) VALUES (?,?,?,?,?)", (_now(), source, fetched, new, error))

    def stats(self) -> dict:
        per_source = {r["source"]: r["n"] for r in self.conn.execute("SELECT source, COUNT(*) n FROM jobs GROUP BY source")}
        per_status = {r["status"]: r["n"] for r in self.conn.execute("SELECT status, COUNT(*) n FROM filter_results GROUP BY status")}
        per_stage = {r["stage"]: r["n"] for r in self.conn.execute("SELECT stage, COUNT(*) n FROM ai_verdicts GROUP BY stage")}
        return {"jobs_per_source": per_source, "filter": per_status, "ai": per_stage}

    def close(self) -> None:
        self.conn.close()


def dumps(obj) -> str:  # small helper used by exports
    return json.dumps(obj, ensure_ascii=False, default=str)
