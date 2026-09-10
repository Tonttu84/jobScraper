"""Anonymous per-run counters: the public ``stats/`` file and the markdown it renders into.

Every report the pipeline writes stays private — it names companies, roles and URLs. What is
worth showing anyone else is the *shape* of the funnel: how many postings came in, how many the
rules threw away and why, how many survived each AI stage, what it cost. This module records
exactly that and nothing else.

``stats/runs.jsonl`` holds one flat row of counters per report per profile (re-running ``report``
for the same report replaces its row); ``stats/README.md`` is that file rendered as markdown.
Both live at the repository root regardless of ``--profile``: they are committed and shared.
**No job title, company, URL or any other text from a posting is ever written here.**
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jobscraper.audit import reason_category
from jobscraper.config import stats_dir
from jobscraper.filters import RULES_VERSION

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jobscraper.config import Settings
    from jobscraper.models import ReportSnapshot
    from jobscraper.store import Store

log = logging.getLogger(__name__)

RUNS_FILE = "runs.jsonl"
README_FILE = "README.md"


def runs_path() -> Path:
    """The committed JSONL of per-run counters."""
    return stats_dir() / RUNS_FILE


def readme_path() -> Path:
    """The rendered markdown next to it."""
    return stats_dir() / README_FILE


# ------------------------------------------------------------------------ collect


def _tokens_by_stage(path: Path | None) -> dict[str, int]:
    """Tokens per pipeline stage from a subagent round's ``usage.jsonl``, if there is one.

    Lines look like ``{"stage": "prefilter", "chunk": "05", "tokens": 124623, ...}``; anything
    without both a stage and an integer token count is ignored.
    """
    totals: dict[str, int] = {}
    if path is None:
        return totals
    for rec in read_rows(path):
        stage, tokens = rec.get("stage"), rec.get("tokens")
        if stage and isinstance(tokens, int):
            totals[str(stage)] = totals.get(str(stage), 0) + tokens
    return totals


def collect(store: Store, snapshot: ReportSnapshot, settings: Settings, *,
            profile: str | None, usage_path: Path | None) -> dict:
    """One JSON-able row of counters for the report just stored.

    ``new_jobs`` counts the jobs first seen at or after the *previous* report's ``created_at``;
    in a database whose first report this is, every job counts as new. ``by_source`` counts every
    job row in the database (a run database is one harvest), while ``jobs_total`` is the report's
    own window of ``--days``. ``prefilter.passed`` applies the profile's ``prefilter_min_score``
    to the prefiltered verdicts, the same threshold ``rank`` uses to pick its candidates.
    """
    counts = snapshot.counts
    previous = store.report_before(snapshot.id) if snapshot.id else None

    drops: dict[str, int] = {}
    for result in store.filter_results("drop").values():
        slug = reason_category(next(iter(result.reasons), ""))  # the primary (first) reason
        drops[slug] = drops.get(slug, 0) + 1

    # Evergreen adverts are never dropped, so they appear nowhere in ``drops``: counting them
    # across every status is the only way to see how much of the funnel is not a live vacancy.
    evergreen = sum(1 for r in store.filter_results().values() if r.signals.get("evergreen"))

    ai = settings.profile.ai
    prefiltered = store.verdicts("prefilter", snapshot.prompt_version)
    passed = sum(1 for v in prefiltered.values() if v.relevant and v.score >= ai.prefilter_min_score)
    runs = store.latest_runs()
    tokens = _tokens_by_stage(usage_path)

    return {
        "recorded_at": datetime.now(UTC).isoformat(),
        "report_id": snapshot.id,
        "report_created_at": snapshot.created_at.isoformat(),
        "profile": profile or "default",
        "jobs_total": counts.get("jobs", 0),
        "new_jobs": store.new_jobs_since(previous.created_at if previous else None),
        "by_source": dict(sorted(store.stats()["jobs_per_source"].items())),
        # ``deadline_passed`` rides along with the statuses: it is the one drop reason that says
        # the vacancy is shut rather than that the profile said no, so it reads as a funnel step.
        "rules": {**{name: counts.get(name, 0) for name in ("keep", "review", "drop")},
                  "deadline_passed": drops.get("deadline_passed", 0),
                  "evergreen": evergreen},
        "drops_by_category": dict(sorted(drops.items())),
        "prefilter": {"prefiltered": len(prefiltered), "passed": passed},
        "ranked": counts.get("ranked", 0),
        "versions": {"rules": RULES_VERSION, "prompt": snapshot.prompt_version},
        "models": {"prefilter": ai.prefilter_model, "rank": ai.rank_model},
        "cost_usd": round(float(snapshot.cost.get("total", 0.0)), 4),
        **({"tokens_by_stage": tokens} if tokens else {}),
        "scrape": {
            "sources": len(runs),
            "fetched": sum(r["fetched"] for r in runs),
            "new": sum(r["new"] for r in runs),
            "errors": sum(1 for r in runs if r["error"]),
        },
    }


# ------------------------------------------------------------------------- record


def read_rows(path: Path) -> list[dict]:
    """Every JSON object in a JSONL file. A missing file is empty; a broken line is skipped."""
    path = Path(path)
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("unreadable line in %s, skipped: %.60s", path, line)
    return rows


def _key(row: dict) -> tuple[str, object]:
    """What makes a row unique: one row per report per profile."""
    return str(row.get("profile") or "default"), row.get("report_id")


def record(row: dict, path: Path) -> Path:
    """Append ``row`` to the JSONL, replacing an earlier row for the same report and profile."""
    path = Path(path)
    rows = [r for r in read_rows(path) if _key(r) != _key(row)]
    rows.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return path


# ------------------------------------------------------------------------- render


_HEADER = """# jobScraper run statistics

Counts only. One row per `jobscraper report`, per candidate profile: how many postings came in,
how many the rule filter kept, prefiltered and ranked, and what the AI stages cost. No job title,
company, URL or any other text from a posting is recorded here — this file is public, while the
reports it counts stay private.

Columns: **jobs** in the report's window, **new** since the previous report, **keep/review/drop**
from the rule filter, **prefiltered/passed** from the Sonnet prefilter stage, **ranked** by Opus, **cost**
in USD (0 when the AI stages ran through subagents), and the rule and prompt versions behind them.

Written by `jobscraper report`; regenerate from `stats/runs.jsonl` with `jobscraper stats --public`."""

_TABLE_HEAD = (
    "| date | report | jobs | new | keep | review | drop | prefiltered | passed | ranked | cost | rules | prompt |",
    "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
)


def _num(value: object) -> str:
    return f"{value:,}" if isinstance(value, int) else "—"


def _date(row: dict) -> str:
    stamp = str(row.get("report_created_at") or row.get("recorded_at") or "")
    return stamp[:10] or "?"


def _table_row(row: dict) -> str:
    rules = row.get("rules") or {}
    pre = row.get("prefilter") or {}
    versions = row.get("versions") or {}
    cells = [
        _date(row),
        f"#{row.get('report_id') if row.get('report_id') is not None else '?'}",
        _num(row.get("jobs_total")), _num(row.get("new_jobs")),
        _num(rules.get("keep")), _num(rules.get("review")), _num(rules.get("drop")),
        _num(pre.get("prefiltered")), _num(pre.get("passed")),
        _num(row.get("ranked")),
        f"${float(row.get('cost_usd') or 0):.2f}",
        str(versions.get("rules") or "—"), str(versions.get("prompt") or "—"),
    ]
    return "| " + " | ".join(cells) + " |"


def render(path: Path) -> str:
    """The whole ``stats/README.md``: a table per profile, newest run first, plus the drop mix."""
    rows = read_rows(path)
    lines = [_HEADER, ""]
    if not rows:
        return "\n".join([*lines, "No runs recorded yet.", ""])

    by_profile: dict[str, list[dict]] = {}
    for row in rows:
        by_profile.setdefault(str(row.get("profile") or "default"), []).append(row)
    names = sorted(by_profile, key=lambda n: (n != "default", n))  # the unnamed profile leads

    for name in names:
        runs = sorted(by_profile[name], key=lambda r: str(r.get("report_created_at") or ""), reverse=True)
        by_profile[name] = runs
        lines += [f"## {name}", "", *_TABLE_HEAD, *(_table_row(r) for r in runs), ""]

    drop_rows = []
    for name in names:
        newest = by_profile[name][0]
        counts = (newest.get("drops_by_category") or {}).items()
        for slug, n in sorted(counts, key=lambda kv: (-kv[1], kv[0])):
            drop_rows.append(f"| {name} | {slug} | {_num(n)} |")

    lines += ["## Rule drops by category", "", "Primary drop reason of each profile's newest run.", ""]
    if drop_rows:
        lines += ["| profile | category | drops |", "|---|---|---:|", *drop_rows, ""]
    else:
        lines += ["No rule drops recorded.", ""]
    return "\n".join(lines)


def refresh() -> Path:
    """Rewrite ``stats/README.md`` from ``stats/runs.jsonl``. Returns the file written."""
    out = readme_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(runs_path()), encoding="utf-8")
    return out
