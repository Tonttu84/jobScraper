"""Markdown report + JSONL export of the current state."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from jobscraper.config import DATA_DIR, RESULTS_DIR  # noqa: F401 - DATA_DIR patched by tests
from jobscraper.models import AIVerdict, FilterResult, Job, ReportItem, ReportSnapshot


def _tier_label(t: int | None) -> str:
    return {1: "FI/EE", 2: "EU/EEA", 3: "extended", 0: "remote"}.get(t, "?") if t is not None else "?"


def write_report(jobs: list[Job], filters: dict[str, FilterResult], prefilter: dict[str, AIVerdict],
                 ranked: dict[str, AIVerdict], path: Path | None = None, cost: dict | None = None) -> Path:
    path = path or RESULTS_DIR / f"report-{datetime.now(UTC):%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    by_id = {j.id: j for j in jobs}
    # Rules tighten between runs (max age, seniority, language); a stale verdict must not resurrect a drop.
    dropped = {f.job_id for f in filters.values() if f.status == "drop"}
    prefilter = {k: v for k, v in prefilter.items() if k not in dropped}
    ranked = {k: v for k, v in ranked.items() if k not in dropped}

    lines = [f"# Job report {datetime.now(UTC):%Y-%m-%d %H:%M} UTC", ""]
    lines.append(f"Jobs in DB: {len(jobs)} · rule-kept: {sum(f.status == 'keep' for f in filters.values())} · "
                 f"review: {sum(f.status == 'review' for f in filters.values())} · dropped: {sum(f.status == 'drop' for f in filters.values())} · "
                 f"prefiltered: {len(prefilter)} · ranked: {len(ranked)}")
    if cost:
        lines.append(f"Estimated API cost this state: ${cost.get('total', 0):.2f} " + " ".join(f"({m}: ${c:.2f})" for m, c in cost.items() if m != 'total'))
    lines.append("")

    if ranked:
        lines += ["## Ranked (Opus)", ""]
        for v in sorted(ranked.values(), key=lambda v: -v.score):
            j = by_id.get(v.job_id)
            if not j:
                continue
            fr = filters.get(j.id)
            loc = f"{j.location_raw or '?'} · {j.remote}" + (f" · {_tier_label(fr.location_tier)}" if fr else "")
            lines.append(f"### {v.score} · [{j.title}]({j.url}) — {j.company or '?'}")
            lines.append(f"*{loc} · {j.source} · posted {j.posted_at.date() if j.posted_at else '?'}*  ")
            lines.append(v.summary)
            if v.why_apply:
                lines.append("- **Why apply:** " + "; ".join(v.why_apply))
            if v.concerns:
                lines.append("- **Concerns:** " + "; ".join(v.concerns))
            if fr and fr.signals.get("work_rights_note"):
                lines.append(f"- **Work rights:** {fr.signals['work_rights_note']}")
            lines.append("")

    if prefilter:
        rest = [v for v in prefilter.values() if v.job_id not in ranked]
        if rest:
            lines += ["## Prefilter survivors not ranked in detail (Sonnet)", "", "| score | title | company | location | source |", "|---|---|---|---|---|"]
            for v in sorted(rest, key=lambda v: -v.score):
                j = by_id.get(v.job_id)
                if j and v.relevant:
                    lines.append(f"| {v.score} | [{j.title}]({j.url}) | {j.company or ''} | {j.location_raw or ''} / {j.remote} | {j.source} |")
            lines.append("")

    review = [f for f in filters.values() if f.status == "review" and f.job_id not in prefilter]
    if review:
        lines += ["## Rule filter: needs review (not yet AI-scored)", "", "| title | company | source | notes |", "|---|---|---|---|"]
        for f in review[:300]:
            j = by_id.get(f.job_id)
            if j:
                lines.append(f"| [{j.title}]({j.url}) | {j.company or ''} | {j.source} | {'; '.join(f.reasons)} |")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def build_snapshot(jobs: list[Job], filters: dict[str, FilterResult], prefilter: dict[str, AIVerdict],
                   ranked: dict[str, AIVerdict], *, days: int, cost: dict | None = None,
                   path: Path | None = None, prompt_version: str) -> ReportSnapshot:
    """The same content as :func:`write_report`, in structured form for the DB and the web UI.

    Sections and their ordering mirror the markdown exactly: ranked by rank score desc,
    then the relevant prefilter survivors that were not ranked, then the rule-filter
    ``review`` leftovers that never reached the AI (capped at 300, as in the markdown).
    """
    by_id = {j.id: j for j in jobs}
    dropped = {f.job_id for f in filters.values() if f.status == "drop"}
    prefilter = {k: v for k, v in prefilter.items() if k not in dropped}
    ranked = {k: v for k, v in ranked.items() if k not in dropped}
    items: list[ReportItem] = []

    for v in sorted(ranked.values(), key=lambda v: -v.score):
        if v.job_id in by_id:
            items.append(ReportItem(job_id=v.job_id, section="ranked", position=len(items) + 1, score=v.score))

    rest = [v for v in prefilter.values() if v.job_id not in ranked]
    start = len(items)
    for v in sorted(rest, key=lambda v: -v.score):
        if v.job_id in by_id and v.relevant:
            items.append(ReportItem(job_id=v.job_id, section="prefilter", position=len(items) - start + 1, score=v.score))

    review = [f for f in filters.values() if f.status == "review" and f.job_id not in prefilter]
    start = len(items)
    for f in review[:300]:
        if f.job_id in by_id:
            items.append(ReportItem(job_id=f.job_id, section="review", position=len(items) - start + 1, score=None))

    counts = {
        "jobs": len(jobs),
        "keep": sum(f.status == "keep" for f in filters.values()),
        "review": sum(f.status == "review" for f in filters.values()),
        "drop": sum(f.status == "drop" for f in filters.values()),
        "prefiltered": len(prefilter),
        "ranked": len(ranked),
    }
    return ReportSnapshot(days=days, prompt_version=prompt_version, counts=counts,
                          cost=dict(cost or {}), path=str(path) if path else None, items=items)


def export_jsonl(jobs: list[Job], filters: dict[str, FilterResult], verdicts: dict[str, AIVerdict], path: Path) -> Path:
    """Flat JSONL for review in Claude Code or a spreadsheet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for j in jobs:
            fr, v = filters.get(j.id), verdicts.get(j.id)
            rec = j.model_dump(mode="json", exclude={"raw"})
            rec["id"] = j.id
            rec["filter"] = fr.model_dump(mode="json") if fr else None
            rec["verdict"] = v.model_dump(mode="json") if v else None
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path
