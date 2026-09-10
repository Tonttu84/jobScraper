"""Markdown report + JSONL export of the current state, and the diff between two reports."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jobscraper.config import paths
from jobscraper.filters.rules import CLOSING_SOON_DAYS
from jobscraper.models import (
    AIVerdict,
    FilterResult,
    Job,
    ReportDiff,
    ReportItem,
    ReportMove,
    ReportSnapshot,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle: store imports models, not report
    from jobscraper.store import Store

#: A ranked job whose score moved by less than this is not worth mentioning in a diff.
MOVED_THRESHOLD = 10

_PREFILTER_HEADER = ["| score | title | company | location | source |", "|---|---|---|---|---|"]


def _tier_label(t: int | None) -> str:
    return {1: "FI/EE", 2: "EU/EEA", 3: "extended", 0: "remote"}.get(t, "?") if t is not None else "?"


def effective_score(rank: AIVerdict | None, refine: AIVerdict | None) -> int | None:
    """The score everything orders by: the mean of the two AI passes, or whichever one exists.

    The rank stage scores a posting alone inside a chunk and the refine stage scores it against
    the rest of the shortlist; both say something the other doesn't, so neither is thrown away.
    """
    if rank is not None and refine is not None:
        return round((rank.score + refine.score) / 2)
    if refine is not None:
        return refine.score
    return rank.score if rank is not None else None


#: Sorts a job with no refine verdict after the refined ones at the same effective score.
_NO_POSITION = 10 ** 6


def rank_order(refine: dict[str, AIVerdict]):
    """Sort key for the ranked section: effective score, then refine position, then rank score."""
    def key(v: AIVerdict) -> tuple[int, int, int]:
        r = refine.get(v.job_id)
        position = r.position if r is not None and r.position is not None else _NO_POSITION
        return -(effective_score(v, r) or 0), position, -v.score

    return key


def closes_in(fr: FilterResult | None) -> int | None:
    """Days until the application deadline the rule filter read out of the posting, if any."""
    days = fr.signals.get("closes_in_days") if fr else None
    return days if isinstance(days, int) else None


def closes_text(days: int) -> str:
    """How the closing date reads in prose, on the last day and before it."""
    return "closes today" if days == 0 else f"closes in {days} days"


def evergreen_cue(fr: FilterResult | None) -> str | None:
    """The phrase that gave the posting away as an evergreen advert, if the rules found one."""
    cue = fr.signals.get("evergreen") if fr else None
    return cue if isinstance(cue, str) and cue else None


def ranked_block(job: Job, score: int | None, verdict: AIVerdict | None = None,
                 fr: FilterResult | None = None, refine: AIVerdict | None = None) -> list[str]:
    """The markdown block for one ranked job — shared by the report and the diff."""
    loc = f"{job.location_raw or '?'} · {job.remote}" + (f" · {_tier_label(fr.location_tier)}" if fr else "")
    both = f" (rank {verdict.score} · refine {refine.score})" if verdict is not None and refine is not None else ""
    days = closes_in(fr)
    closing = f" · {closes_text(days)}" if days is not None else ""
    cue = evergreen_cue(fr)
    advert = " · evergreen advert" if cue else ""
    lines = [f"### {score}{both} · [{job.title}]({job.url}) — {job.company or '?'}",
             f"*{loc} · {job.source} · posted {job.posted_at.date() if job.posted_at else '?'}{closing}{advert}*  "]
    # A deadline this close outranks everything the AI has to say about the job.
    if days is not None and days <= CLOSING_SOON_DAYS:
        lines.append(f"- **{closes_text(days).capitalize()}**")
    # And so does "there may be no job here at all", which the score cannot express on its own.
    if cue:
        lines.append(f'- **Evergreen advert:** not a live vacancy ("{cue}")')
    if verdict is not None:
        lines.append(verdict.summary)
        if refine is not None:
            lines.append(f"- **Against the rest of the shortlist:** {refine.summary}")
        if verdict.why_apply:
            lines.append("- **Why apply:** " + "; ".join(verdict.why_apply))
        if verdict.concerns:
            lines.append("- **Concerns:** " + "; ".join(verdict.concerns))
    if fr and fr.signals.get("work_rights_note"):
        lines.append(f"- **Work rights:** {fr.signals['work_rights_note']}")
    lines.append("")
    return lines


def prefilter_row(job: Job, score: int | None) -> str:
    """One row of the compact prefilter table."""
    return f"| {score} | [{job.title}]({job.url}) | {job.company or ''} | {job.location_raw or ''} / {job.remote} | {job.source} |"


def write_report(jobs: list[Job], filters: dict[str, FilterResult], prefilter: dict[str, AIVerdict],
                 ranked: dict[str, AIVerdict], path: Path | None = None, cost: dict | None = None,
                 refine: dict[str, AIVerdict] | None = None) -> Path:
    refine = refine or {}
    path = path or paths().results / f"report-{datetime.now(UTC):%Y-%m-%d}.md"
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
        for v in sorted(ranked.values(), key=rank_order(refine)):
            j = by_id.get(v.job_id)
            if not j:
                continue
            r = refine.get(v.job_id)
            lines += ranked_block(j, effective_score(v, r), v, filters.get(j.id), r)

    if prefilter:
        rest = [v for v in prefilter.values() if v.job_id not in ranked]
        if rest:
            lines += ["## Prefilter survivors not ranked in detail (Sonnet)", "", *_PREFILTER_HEADER]
            for v in sorted(rest, key=lambda v: -v.score):
                j = by_id.get(v.job_id)
                if j and v.relevant:
                    lines.append(prefilter_row(j, v.score))
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
                   path: Path | None = None, prompt_version: str,
                   refine: dict[str, AIVerdict] | None = None) -> ReportSnapshot:
    """The same content as :func:`write_report`, in structured form for the DB and the web UI.

    Sections and their ordering mirror the markdown exactly: ranked by effective score desc
    (the mean of the rank and refine passes where both spoke), then the relevant prefilter
    survivors that were not ranked, then the rule-filter ``review`` leftovers that never
    reached the AI (capped at 300, as in the markdown).
    """
    refine = refine or {}
    by_id = {j.id: j for j in jobs}
    dropped = {f.job_id for f in filters.values() if f.status == "drop"}
    prefilter = {k: v for k, v in prefilter.items() if k not in dropped}
    ranked = {k: v for k, v in ranked.items() if k not in dropped}
    items: list[ReportItem] = []

    for v in sorted(ranked.values(), key=rank_order(refine)):
        if v.job_id in by_id:
            items.append(ReportItem(job_id=v.job_id, section="ranked", position=len(items) + 1,
                                    score=effective_score(v, refine.get(v.job_id))))

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


# ------------------------------------------------- "what's new" between reports


def _items(snap: ReportSnapshot | None, section: str) -> list[ReportItem]:
    """One section of a snapshot, in position order."""
    if snap is None:
        return []
    return sorted((i for i in snap.items if i.section == section), key=lambda i: i.position)


def diff_reports(new: ReportSnapshot, old: ReportSnapshot | None) -> ReportDiff:
    """What changed between two report snapshots of the same database.

    Jobs are matched on :attr:`Job.id`, which is stable across runs. With ``old`` missing
    (the first report in a database) everything in ``new`` counts as new.
    """
    new_ranked, new_pre = _items(new, "ranked"), _items(new, "prefilter")
    old_ranked_items = _items(old, "ranked")
    old_ranked = {i.job_id: i for i in old_ranked_items}
    old_pre = {i.job_id for i in _items(old, "prefilter")}
    now_ranked = {i.job_id for i in new_ranked}

    moved: list[ReportMove] = []
    for item in new_ranked:
        was = old_ranked.get(item.job_id)
        if was is None or was.score is None or item.score is None:
            continue
        if abs(item.score - was.score) >= MOVED_THRESHOLD:
            moved.append(ReportMove(job_id=item.job_id, old_score=was.score, new_score=item.score,
                                    old_position=was.position, new_position=item.position))

    return ReportDiff(
        old_id=old.id if old else None,
        new_id=new.id,
        old_created_at=old.created_at if old else None,
        new_ranked=[i for i in new_ranked if i.job_id not in old_ranked],
        gone_ranked=[i for i in old_ranked_items if i.job_id not in now_ranked],
        new_prefilter=[i for i in new_pre if i.job_id not in old_pre and i.job_id not in old_ranked],
        moved=moved,
    )


def previous_report(store: Store, before: ReportSnapshot) -> ReportSnapshot | None:
    """The report stored just before ``before`` (with its items), or None if it was the first."""
    return store.report_before(before.id)


def _counts_line(diff: ReportDiff) -> str:
    counts = (f"{len(diff.new_ranked)} new ranked · {len(diff.new_prefilter)} new in prefilter · "
              f"{len(diff.gone_ranked)} dropped out of ranked")
    if diff.old_id is None:
        return counts
    created = f" (created {diff.old_created_at:%Y-%m-%d %H:%M} UTC)" if diff.old_created_at else ""
    return f"{counts} since report #{diff.old_id}{created}"


def render_diff(diff: ReportDiff, jobs_by_id: dict[str, Job], ranked_verdicts: dict[str, AIVerdict],
                prefilter_verdicts: dict[str, AIVerdict]) -> str:
    """Markdown for one :func:`diff_reports` result: the whole "what's new" file."""
    lines: list[str] = []
    if diff.old_id is None:
        lines += ["First report in this database — nothing to diff against.", ""]
    lines += [_counts_line(diff), ""]

    new_ranked = [(i, jobs_by_id[i.job_id]) for i in diff.new_ranked if i.job_id in jobs_by_id]
    if new_ranked:
        lines += ["## New ranked", ""]
        for item, job in new_ranked:
            lines += ranked_block(job, item.score, ranked_verdicts.get(item.job_id))

    new_pre = [(i, jobs_by_id[i.job_id]) for i in diff.new_prefilter if i.job_id in jobs_by_id]
    if new_pre:
        lines += ["## New in prefilter", "", *_PREFILTER_HEADER]
        for item, job in new_pre:
            v = prefilter_verdicts.get(item.job_id)
            lines.append(prefilter_row(job, v.score if v else item.score))
        lines.append("")

    gone = [(i, jobs_by_id[i.job_id]) for i in diff.gone_ranked if i.job_id in jobs_by_id]
    if gone:
        lines += ["## Dropped out of the ranking", ""]
        for item, job in gone:
            lines.append(f"- [{job.title}]({job.url}) — {job.company or '?'} (was {item.score})")
        lines.append("")

    moves = [(m, jobs_by_id[m.job_id]) for m in diff.moved if m.job_id in jobs_by_id]
    if moves:
        lines += ["## Moved in the ranking", ""]
        for m, job in moves:
            lines.append(f"- [{job.title}]({job.url}) — {job.company or '?'}: "
                         f"{m.old_score} → {m.new_score} ({m.delta:+d})")
        lines.append("")

    if not (new_ranked or new_pre or gone or moves):
        lines += ["Nothing new since the previous report.", ""]
    return "\n".join(lines)


def render_new_section(diff: ReportDiff, jobs_by_id: dict[str, Job],
                       ranked_verdicts: dict[str, AIVerdict]) -> str:
    """The short section `jobscraper report` puts at the top of the markdown report."""
    heading = "## New in this report" if diff.old_id is None else f"## New since report #{diff.old_id}"
    lines = [heading, "", _counts_line(diff), ""]
    for item in diff.new_ranked:
        job = jobs_by_id.get(item.job_id)
        if job is not None:
            lines += ranked_block(job, item.score, ranked_verdicts.get(item.job_id))
    return "\n".join(lines)


def prepend_section(path: Path, section: str) -> None:
    """Insert ``section`` above the report's first ``## `` heading (or at the end of the file)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    cut = next((n for n, line in enumerate(lines) if line.startswith("## ")), len(lines))
    path.write_text("\n".join([*lines[:cut], *section.splitlines(), "", *lines[cut:]]), encoding="utf-8")


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
