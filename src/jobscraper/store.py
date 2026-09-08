"""SQLite persistence: jobs, filter results, AI verdicts, run log."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from jobscraper.config import DATA_DIR
from jobscraper.models import (
    AIVerdict,
    Decision,
    FilterResult,
    Job,
    JobFacets,
    ReportItem,
    ReportSnapshot,
)

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

CREATE TABLE IF NOT EXISTS job_facets (
    job_id TEXT PRIMARY KEY,
    facets_version TEXT NOT NULL,
    posting_language TEXT,
    web_dev INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    job_id TEXT NOT NULL,
    user TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job_id, user)
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    days INTEGER NOT NULL,
    prompt_version TEXT NOT NULL,
    counts TEXT NOT NULL,
    cost TEXT NOT NULL,
    path TEXT
);

CREATE TABLE IF NOT EXISTS report_items (
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL,
    section TEXT NOT NULL,
    position INTEGER NOT NULL,
    score INTEGER,
    PRIMARY KEY (report_id, job_id)
);
CREATE INDEX IF NOT EXISTS report_items_report ON report_items(report_id, section, position);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DATA_DIR / "jobs.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
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

    # ----------------------------------------------------------------- facets
    def save_facets(self, facets: list[JobFacets]) -> None:
        """Store one row per job; re-running the facet stage replaces the old row."""
        now = _now()
        with self.tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO job_facets (job_id, facets_version, posting_language, web_dev, updated_at, data) VALUES (?,?,?,?,?,?)",
                [(f.job_id, f.facets_version, f.posting_language, int(f.web_dev), now, f.model_dump_json()) for f in facets],
            )

    def facets(self, ids: list[str] | None = None) -> dict[str, JobFacets]:
        sql, args = "SELECT data FROM job_facets", []
        if ids is not None:
            if not ids:
                return {}
            sql += f" WHERE job_id IN ({','.join('?' * len(ids))})"
            args.extend(ids)
        return {f.job_id: f for f in (JobFacets.model_validate_json(r["data"]) for r in self.conn.execute(sql, args))}

    # -------------------------------------------------------------- decisions
    def save_decision(self, d: Decision) -> Decision:
        """Insert or replace one user's decision about one job, stamped now."""
        stored = d.model_copy(update={"updated_at": datetime.now(UTC)})
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO decisions (job_id, user, status, note, updated_at) VALUES (?,?,?,?,?)",
                (stored.job_id, stored.user, stored.status, stored.note, stored.updated_at.isoformat()),
            )
        return stored

    def delete_decision(self, job_id: str, user: str) -> bool:
        """Drop one decision. True when a row was actually removed."""
        with self.tx() as c:
            cur = c.execute("DELETE FROM decisions WHERE job_id=? AND user=?", (job_id, user))
        return cur.rowcount > 0

    def decisions(self, user: str | None = None) -> list[Decision]:
        sql, args = "SELECT job_id, user, status, note, updated_at FROM decisions", []
        if user is not None:
            sql += " WHERE user=?"
            args.append(user)
        sql += " ORDER BY updated_at DESC, job_id"
        return [Decision(**dict(row)) for row in self.conn.execute(sql, args)]

    def decision_users(self) -> list[str]:
        return [r["user"] for r in self.conn.execute("SELECT DISTINCT user FROM decisions ORDER BY user")]

    # ---------------------------------------------------------------- reports
    @staticmethod
    def _snapshot(row: sqlite3.Row, items: list[ReportItem] | None = None) -> ReportSnapshot:
        return ReportSnapshot(
            id=row["id"],
            created_at=row["created_at"],
            days=row["days"],
            prompt_version=row["prompt_version"],
            counts=json.loads(row["counts"]),
            cost=json.loads(row["cost"]),
            path=row["path"],
            items=items or [],
        )

    def save_report(self, snap: ReportSnapshot) -> ReportSnapshot:
        """Insert the report row and its items in one transaction; returns a copy with the id."""
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO reports (created_at, days, prompt_version, counts, cost, path) VALUES (?,?,?,?,?,?)",
                (snap.created_at.isoformat(), snap.days, snap.prompt_version, dumps(snap.counts), dumps(snap.cost), snap.path),
            )
            report_id = cur.lastrowid
            c.executemany(
                "INSERT OR REPLACE INTO report_items (report_id, job_id, section, position, score) VALUES (?,?,?,?,?)",
                [(report_id, i.job_id, i.section, i.position, i.score) for i in snap.items],
            )
        return snap.model_copy(update={"id": report_id})

    def reports(self) -> list[ReportSnapshot]:
        """Newest first, without items — cheap listing for the web UI."""
        return [self._snapshot(row) for row in self.conn.execute("SELECT * FROM reports ORDER BY id DESC")]

    def report(self, report_id: int | None = None) -> ReportSnapshot | None:
        """One report with its items, the latest when no id is given."""
        if report_id is None:
            row = self.conn.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 1").fetchone()
        else:
            row = self.conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if row is None:
            return None
        items = [
            ReportItem(job_id=r["job_id"], section=r["section"], position=r["position"], score=r["score"])
            for r in self.conn.execute(
                "SELECT job_id, section, position, score FROM report_items WHERE report_id=? ORDER BY section, position",
                (row["id"],),
            )
        ]
        return self._snapshot(row, items)

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
        per_decision = {r["status"]: r["n"] for r in self.conn.execute("SELECT status, COUNT(*) n FROM decisions GROUP BY status")}
        reports = self.conn.execute("SELECT COUNT(*) n FROM reports").fetchone()["n"]
        return {"jobs_per_source": per_source, "filter": per_status, "ai": per_stage,
                "decisions": per_decision, "reports": reports}

    def close(self) -> None:
        self.conn.close()


def dumps(obj) -> str:  # small helper used by exports
    return json.dumps(obj, ensure_ascii=False, default=str)
