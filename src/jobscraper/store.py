"""SQLite persistence: jobs, filter results, AI verdicts, run log."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from jobscraper.config import paths
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
    data TEXT NOT NULL,
    -- Which scrape last brought this posting back, and how many complete runs of its source
    -- have missed it since (see Store.mark_missing / Store.gone_ids).
    last_run_id INTEGER,
    missed_runs INTEGER NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS ai_batches (
    id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    job_ids TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ai_batches_status ON ai_batches(stage, status);

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
    path TEXT,
    refine_offset REAL
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


#: Columns added to :data:`SCHEMA` after databases already existed, as (table, column, type).
#: :meth:`Store._migrate` adds each of them to a database that predates it.
_ADDED_COLUMNS = (
    ("reports", "refine_offset", "REAL"),
    ("jobs", "last_run_id", "INTEGER"),
    ("jobs", "missed_runs", "INTEGER NOT NULL DEFAULT 0"),
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


#: Set by the CLI's --db option (and by `run`, which creates a database per run).
DB_OVERRIDE: Path | None = None

_DECISIONS_DDL = re.search(r"CREATE TABLE IF NOT EXISTS decisions \((.*?)\);", SCHEMA, re.S).group(1)


def default_db_path() -> Path:
    """Which database the CLI works on when none is given.

    Precedence: the CLI ``--db`` override, then ``$JOBSCRAPER_DB``, then the newest per-run
    database under ``data/runs/`` (names are timestamps, so lexical order is chronological),
    then the legacy single ``data/jobs.db``.
    """
    if DB_OVERRIDE is not None:
        return DB_OVERRIDE
    env = os.environ.get("JOBSCRAPER_DB")
    if env:
        return Path(env)
    data = paths().data
    runs = sorted((data / "runs").glob("*.db")) if (data / "runs").is_dir() else []
    return runs[-1] if runs else data / "jobs.db"


def copy_db(src: Path, dst: Path) -> Path:
    """Consistent snapshot of ``src`` into ``dst`` via SQLite's online backup (safe mid-write)."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(_uri(Path(src), "ro"), uri=True)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return dst


def new_run_db(fresh: bool = False) -> Path:
    """Create ``data/runs/<timestamp>.db`` for a new pipeline run.

    Unless ``fresh``, the current database is copied forward so first-seen dates, decisions
    and cached AI verdicts carry over while the previous file stays untouched.
    """
    runs = paths().data / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path, n = runs / f"{stamp}.db", 1
    while path.exists():  # "_n" sorts after ".db", keeping lexical order chronological
        path, n = runs / f"{stamp}_{n}.db", n + 1
    previous = default_db_path()
    if not fresh and previous.exists() and previous.resolve() != path.resolve():
        copy_db(previous, path)
    else:
        Store(path).close()
    return path


def _uri(path: Path, mode: str) -> str:
    return f"{path.resolve().as_uri()}?mode={mode}"


class Store:
    def __init__(self, path: Path | None = None, *, readonly: bool = False,
                 decisions_path: Path | None = None) -> None:
        """Open a database.

        ``readonly`` opens an existing file with ``mode=ro`` (no schema creation, writes fail).
        ``decisions_path`` keeps the ``decisions`` table in a separate, writable sidecar file so
        the web UI can annotate read-only copies and the notes survive swapping the copy.

        ``check_same_thread=False`` lifts sqlite3's own thread guard: the web app opens a Store
        in one threadpool worker (the dependency's ``__enter__``) and runs the endpoint in
        another, and hitting the guard there turned into a 500. A Store is still used by one
        request at a time, never concurrently.
        """
        self.path = Path(path) if path else default_db_path()
        self.readonly = readonly
        if readonly:
            if not self.path.exists():
                raise FileNotFoundError(self.path)
            self.conn = sqlite3.connect(_uri(self.path, "ro"), uri=True, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(_uri(self.path, "rwc"), uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if not readonly:
            self.conn.executescript(SCHEMA)
            self._migrate()
        self._decisions = "decisions"
        if decisions_path is not None:
            decisions_path = Path(decisions_path)
            decisions_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn.execute("ATTACH DATABASE ? AS dec", (_uri(decisions_path, "rwc"),))
            self.conn.execute(f"CREATE TABLE IF NOT EXISTS dec.decisions ({_DECISIONS_DDL})")
            self.conn.commit()
            self._decisions = "dec.decisions"

    def _migrate(self) -> None:
        """Add the columns ``CREATE TABLE IF NOT EXISTS`` cannot add to a table that exists.

        A database written by an earlier version keeps its rows; only the new column is empty.
        A read-only copy is never migrated, so every reader treats a missing column as unknown.
        """
        for table, column, decl in _ADDED_COLUMNS:
            have = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------------ jobs
    def upsert_jobs(self, jobs: list[Job], run_id: int | None = None) -> int:
        """Insert new jobs, refresh last_seen for known ones. Returns number of new jobs.

        ``run_id`` (from :meth:`start_run`) records *which* scrape saw each posting and clears
        its miss counter, which is what :meth:`mark_missing` later reads. Without one the two
        columns are left exactly as they were, so a caller that is not a scrape — a LinkedIn
        description top-up, say — cannot make a posting look freshly seen.
        """
        new = 0
        now = _now()
        with self.tx() as c:
            for job in jobs:
                row = c.execute("SELECT id FROM jobs WHERE id=?", (job.id,)).fetchone()
                data = job.model_dump_json()
                if row and run_id is None:
                    c.execute("UPDATE jobs SET last_seen=?, data=?, title=?, url=? WHERE id=?", (now, data, job.title, job.url, job.id))
                elif row:
                    c.execute("UPDATE jobs SET last_seen=?, data=?, title=?, url=?, last_run_id=?, missed_runs=0 WHERE id=?",
                              (now, data, job.title, job.url, run_id, job.id))
                else:
                    new += 1
                    c.execute(
                        "INSERT INTO jobs (id, source, source_id, url, title, company, country, city, remote, posted_at, first_seen, last_seen, data, last_run_id)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (job.id, job.source, job.source_id, job.url, job.title, job.company, job.country, job.city, job.remote,
                         job.posted_at.isoformat() if job.posted_at else None, now, now, data, run_id),
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

    def verdicts(self, stage: str,
                 prompt_version: str | Sequence[str] | None = None) -> dict[str, AIVerdict]:
        """Latest verdict per job for ``stage``, optionally restricted by prompt version.

        ``prompt_version`` takes one version or a sequence of them
        (:data:`~jobscraper.ai.prompts.COMPATIBLE_PROMPT_VERSIONS`, so a wording change that kept
        the scoring scale does not hide the verdicts written before it); None reads every version.
        """
        sql, args = "SELECT data FROM ai_verdicts WHERE stage=?", [stage]
        if prompt_version:
            versions = [prompt_version] if isinstance(prompt_version, str) else list(prompt_version)
            sql += f" AND prompt_version IN ({','.join('?' * len(versions))})"
            args.extend(versions)
        out: dict[str, AIVerdict] = {}
        for row in self.conn.execute(sql + " ORDER BY created_at", args):
            v = AIVerdict.model_validate_json(row["data"])
            out[v.job_id] = v  # latest wins
        return out

    # --------------------------------------------------------- AI batch jobs
    def save_batch(self, batch_id: str, stage: str, model: str, prompt_version: str,
                   job_ids: list[str], status: str = "submitted") -> None:
        """Remember a Message Batch we submitted, so a later run can pick its results up."""
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO ai_batches (id, stage, model, prompt_version, created_at, status, job_ids)"
                " VALUES (?,?,?,?,?,?,?)",
                (batch_id, stage, model, prompt_version, _now(), status, dumps(job_ids)),
            )

    def pending_batches(self, stage: str | None = None, status: str = "submitted") -> list[dict]:
        """Batch rows still in ``status`` (oldest first), with ``job_ids`` decoded."""
        sql, args = "SELECT * FROM ai_batches WHERE status=?", [status]
        if stage:
            sql += " AND stage=?"
            args.append(stage)
        rows = self.conn.execute(sql + " ORDER BY created_at, id", args)
        return [{**dict(r), "job_ids": json.loads(r["job_ids"])} for r in rows]

    def finish_batch(self, batch_id: str, status: str = "done") -> None:
        with self.tx() as c:
            c.execute("UPDATE ai_batches SET status=? WHERE id=?", (status, batch_id))

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
                f"INSERT OR REPLACE INTO {self._decisions} (job_id, user, status, note, updated_at) VALUES (?,?,?,?,?)",
                (stored.job_id, stored.user, stored.status, stored.note, stored.updated_at.isoformat()),
            )
        return stored

    def delete_decision(self, job_id: str, user: str) -> bool:
        """Drop one decision. True when a row was actually removed."""
        with self.tx() as c:
            cur = c.execute(f"DELETE FROM {self._decisions} WHERE job_id=? AND user=?", (job_id, user))
        return cur.rowcount > 0

    def decisions(self, user: str | None = None) -> list[Decision]:
        sql, args = f"SELECT job_id, user, status, note, updated_at FROM {self._decisions}", []
        if user is not None:
            sql += " WHERE user=?"
            args.append(user)
        sql += " ORDER BY updated_at DESC, job_id"
        return [Decision(**dict(row)) for row in self.conn.execute(sql, args)]

    def decision_users(self) -> list[str]:
        return [r["user"] for r in self.conn.execute(f"SELECT DISTINCT user FROM {self._decisions} ORDER BY user")]

    # ---------------------------------------------------------------- reports
    @staticmethod
    def _snapshot(row: sqlite3.Row, items: list[ReportItem] | None = None) -> ReportSnapshot:
        columns = row.keys()  # a sqlite3.Row is not a mapping: `in` would search the values
        return ReportSnapshot(
            id=row["id"],
            created_at=row["created_at"],
            days=row["days"],
            prompt_version=row["prompt_version"],
            counts=json.loads(row["counts"]),
            cost=json.loads(row["cost"]),
            path=row["path"],
            # A read-only copy written before the column existed cannot be migrated on open.
            refine_offset=row["refine_offset"] if "refine_offset" in columns else None,
            items=items or [],
        )

    def save_report(self, snap: ReportSnapshot) -> ReportSnapshot:
        """Insert the report row and its items in one transaction; returns a copy with the id."""
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO reports (created_at, days, prompt_version, counts, cost, path, refine_offset)"
                " VALUES (?,?,?,?,?,?,?)",
                (snap.created_at.isoformat(), snap.days, snap.prompt_version, dumps(snap.counts),
                 dumps(snap.cost), snap.path, snap.refine_offset),
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

    def report_before(self, report_id: int) -> ReportSnapshot | None:
        """The newest report older than ``report_id`` (with items), or None if it was the first."""
        row = self.conn.execute("SELECT id FROM reports WHERE id < ? ORDER BY id DESC LIMIT 1", (report_id,)).fetchone()
        return self.report(row["id"]) if row else None

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
    def start_run(self, source: str) -> int:
        """Open a ``runs`` row for a scrape that is about to start, and return its id.

        The id has to exist before the jobs are upserted, because that is what stamps each
        posting with the run that saw it. Timestamps cannot stand in for the link: the row used
        to be written *after* the jobs, so every posting looked older than its own run.
        """
        with self.tx() as c:
            cur = c.execute("INSERT INTO runs (started_at, source, fetched, new) VALUES (?,?,0,0)", (_now(), source))
        return cur.lastrowid

    def finish_run(self, run_id: int, fetched: int, new: int, error: str | None = None) -> None:
        """Fill in what the run ended up doing (or the error that ended it)."""
        with self.tx() as c:
            c.execute("UPDATE runs SET fetched=?, new=?, error=? WHERE id=?", (fetched, new, error, run_id))

    def log_run(self, source: str, fetched: int, new: int, error: str | None = None) -> int:
        """One completed ``runs`` row, for a caller that has nothing to stamp jobs with."""
        run_id = self.start_run(source)
        self.finish_run(run_id, fetched, new, error)
        return run_id

    def mark_missing(self, source: str, run_id: int) -> int:
        """Count one miss against every posting of ``source`` that ``run_id`` did not bring back.

        Only call this for a run that actually enumerated the whole listing (see
        ``Source.complete_listing``): a failed, empty or limited fetch says nothing about what
        the board still shows. A row that predates the mechanism has ``last_run_id`` NULL, which
        is not this run, so it starts counting from its first complete run. Returns rows touched.
        """
        with self.tx() as c:
            cur = c.execute(
                "UPDATE jobs SET missed_runs = missed_runs + 1"
                " WHERE source=? AND (last_run_id IS NULL OR last_run_id != ?)",
                (source, run_id),
            )
        return cur.rowcount

    def gone_ids(self, min_misses: int) -> set[str]:
        """Postings missed by at least ``min_misses`` complete runs of their source in a row.

        ``min_misses <= 0`` turns the whole mechanism off and returns nothing.
        """
        if min_misses <= 0:
            return set()
        return {r["id"] for r in self.conn.execute("SELECT id FROM jobs WHERE missed_runs >= ?", (min_misses,))}

    def latest_runs(self) -> list[dict]:
        """The newest ``runs`` row per source — one line per source of the last scrape."""
        rows = self.conn.execute(
            "SELECT r.source, r.started_at, r.fetched, r.new, r.error FROM runs r"
            " JOIN (SELECT source, MAX(id) AS id FROM runs GROUP BY source) last ON r.id = last.id"
            " ORDER BY r.source"
        )
        return [dict(r) for r in rows]

    def new_jobs_since(self, when: datetime | str | None) -> int:
        """How many jobs were first seen at or after ``when`` (every job when it is None).

        ``first_seen`` is an ISO-8601 UTC string, so the comparison is a plain string compare.
        """
        if when is None:
            return self.conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
        stamp = when.isoformat() if isinstance(when, datetime) else str(when)
        return self.conn.execute("SELECT COUNT(*) n FROM jobs WHERE first_seen >= ?", (stamp,)).fetchone()["n"]

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


def partition_listed(jobs: list[Job], store: Store, min_misses: int) -> tuple[list[Job], list[Job]]:
    """Split ``jobs`` into the ones their board still lists and the ones it has dropped.

    Nothing is deleted: a posting that is gone keeps its row, its verdicts and its history, it
    just stops being a candidate for the AI stages and the report.
    """
    gone = store.gone_ids(min_misses)
    return [j for j in jobs if j.id not in gone], [j for j in jobs if j.id in gone]


def still_listed(jobs: list[Job], store: Store, min_misses: int) -> list[Job]:
    """Only the postings their board still lists — see :func:`partition_listed`."""
    return partition_listed(jobs, store, min_misses)[0]


def dumps(obj) -> str:  # small helper used by exports
    return json.dumps(obj, ensure_ascii=False, default=str)
