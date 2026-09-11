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


#: Fewer shared jobs than this and the gap between the two stages is noise, not a scale.
MIN_CALIBRATION_JOBS = 3


def refine_offset(rank: dict[str, AIVerdict], refine: dict[str, AIVerdict]) -> float:
    """How many points to add to a refine score to read it on the rank stage's scale.

    The two stages use 0-100 differently. ``rank`` grades one posting at a time against the
    profile; ``refine`` grades the whole shortlist against itself, which pushes its numbers down
    by around twenty points on a list that is already the best of the run. Their ordering agrees
    — that is the part worth keeping — but averaging the raw numbers drags a strong shortlist
    into the sixties. This is the mean of ``rank - refine`` over the jobs both stages scored;
    with fewer than :data:`MIN_CALIBRATION_JOBS` of them there is nothing to measure.
    """
    shared = sorted(set(rank) & set(refine))
    if len(shared) < MIN_CALIBRATION_JOBS:
        return 0.0
    return round(sum(rank[i].score - refine[i].score for i in shared) / len(shared), 1)


def calibrated_refine(refine_score: int, offset: float) -> int:
    """One refine score moved onto the rank scale, kept inside the 0..100 both raters use."""
    return max(0, min(100, round(refine_score + offset)))


def effective_score(rank: AIVerdict | None, refine: AIVerdict | None,
                    offset: float = 0.0) -> int | None:
    """The score everything orders by: the mean of the two AI passes, or whichever one exists.

    The rank stage scores a posting alone inside a chunk and the refine stage scores it against
    the rest of the shortlist; both say something the other doesn't, so neither is thrown away.
    The refine score is put on the rank scale with ``offset`` (see :func:`refine_offset`) before
    the two are averaged, so a job is not marked down for having been read twice.
    """
    if rank is not None and refine is not None:
        return round((rank.score + calibrated_refine(refine.score, offset)) / 2)
    if refine is not None:
        return refine.score
    return rank.score if rank is not None else None


#: Sorts a job with no refine verdict after the refined ones at the same effective score.
_NO_POSITION = 10 ** 6


def _refine_position(refine: AIVerdict | None) -> int:
    """Where the refine pass put a job in the combined ordering; unplaced jobs sort last."""
    if refine is None or refine.position is None:
        return _NO_POSITION
    return refine.position


def rank_order(refine: dict[str, AIVerdict], offset: float = 0.0):
    """Sort key for the ranked section: effective score, then refine position, then rank score."""
    def key(v: AIVerdict) -> tuple[int, int, int]:
        r = refine.get(v.job_id)
        return -(effective_score(v, r, offset) or 0), _refine_position(r), -v.score

    return key


def refine_shortlist(jobs: list[Job], filters: dict[str, FilterResult],
                     rank: dict[str, AIVerdict], refine: dict[str, AIVerdict], *,
                     top: int, first_pass: int) -> tuple[list[Job], float]:
    """The jobs the refine pass should be holding, and the offset they were ordered with.

    Candidates are the rule-kept jobs that carry a rank verdict, ordered by the very score the
    report prints. Ordering them by the rank score instead leaves the pass chasing its own tail:
    a refined job sits at the mean of two samples while its unrefined neighbour still carries a
    single, selection-inflated rank score, so the unrefined one drifts into the effective top N
    on one lucky sample and is never looked at again.

    ``first_pass`` is the width used while nothing on the list has been refined yet — one wider
    request costs little and leaves room for the reshuffle that calibrating the second scale
    causes — and ``top`` the invariant width every pass after it.

    The width is a lower bound, not a cut: every further candidate tied with the last included
    one comes along. Scores are whole numbers and a run of them at the boundary is a genuine
    tie — the job id that separates them says nothing about the job — so refining half of a run
    would leave the rest sitting unrefined just inside the window forever.
    """
    alive = {job_id for job_id, f in filters.items() if f.status != "drop"}
    offset = refine_offset(rank, refine)
    scored = [(effective_score(rank[j.id], refine.get(j.id), offset) or 0, j)
              for j in jobs if j.id in alive and j.id in rank]
    scored.sort(key=lambda pair: (-pair[0], _refine_position(refine.get(pair[1].id)),
                                  -rank[pair[1].id].score, pair[1].id))
    width = top if any(j.id in refine for _, j in scored) else first_pass
    picked = [j for _, j in scored[:width]]
    if picked:
        edge = scored[len(picked) - 1][0]  # descending: everything still tied with it follows here
        picked += [j for score, j in scored[len(picked):] if score == edge]
    return picked, offset


#: What "the top" means to the rank stop rule when the refine stage is switched off
#: (``ai.refine_top_n = 0``): the report still has a head, whether or not it is refined.
RANK_TOP_WATCH = 20


def rank_queue(jobs: list[Job], filters: dict[str, FilterResult], prefilter: dict[str, AIVerdict],
               rank: dict[str, AIVerdict], *, min_score: int) -> list[Job]:
    """The jobs still waiting for a rank verdict, in the order the rank stage should read them.

    Candidates are the rule-kept jobs the screening pass called relevant and scored at least
    ``min_score`` — the same gate the report's prefilter section uses — minus everything that
    already carries a rank verdict, so the queue is a refill list.

    The order is the screen score, best first, with the job id breaking ties. That is a weak
    prior and known to be one: measured over the ranked jobs on 2026-09-11 the screen score has
    no relation to the rank score (Spearman -0.06), so reading further down this queue keeps
    finding strong jobs. It is still the only prior there is, and the stop rule — not the
    ordering — is what decides how deep to go.
    """
    by_id = {j.id: j for j in jobs}
    alive = {job_id for job_id, f in filters.items() if f.status != "drop"}
    ordered = sorted((v for v in prefilter.values()
                      if v.relevant and v.score >= min_score and v.job_id in alive
                      and v.job_id in by_id and v.job_id not in rank),
                     key=lambda v: (-v.score, v.job_id))
    return [by_id[v.job_id] for v in ordered]


def effective_top(jobs: list[Job], filters: dict[str, FilterResult], rank: dict[str, AIVerdict],
                  refine: dict[str, AIVerdict], *, top: int) -> tuple[list[str], int | None]:
    """The ids of the effective top ``top``, and the effective score at its boundary.

    The same list :func:`refine_shortlist` holds refined, asked for at one fixed width: this is
    the "top 20" the rank stop rule watches and the boundary the ``run`` pipeline compares
    across a refine loop. ``None`` as the boundary means nothing is ranked yet.
    """
    picked, offset = refine_shortlist(jobs, filters, rank, refine, top=top, first_pass=top)
    if not picked:
        return [], None
    last = picked[-1]
    return [j.id for j in picked], effective_score(rank[last.id], refine.get(last.id), offset)


def entered_top(before_ids: set[str], after_ids: list[str], new_ids) -> int:
    """How many of the jobs just ranked are in the effective top N that were not in it before."""
    return len(set(new_ids) & (set(after_ids) - set(before_ids)))


def miss_run(windows) -> int:
    """Jobs ranked since the last one that entered the effective top N.

    ``windows`` is ``(ranked, entered)`` per window in order. A window that let somebody in
    resets the run — that is the signal the top is still moving — and the rest add up. When the
    run reaches ``ai.rank_patience`` the queue is not paying for itself any more.
    """
    run = 0
    for ranked, entered in windows:
        run = 0 if entered else run + ranked
    return run


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
                 fr: FilterResult | None = None, refine: AIVerdict | None = None,
                 offset: float = 0.0) -> list[str]:
    """The markdown block for one ranked job — shared by the report and the diff."""
    loc = f"{job.location_raw or '?'} · {job.remote}" + (f" · {_tier_label(fr.location_tier)}" if fr else "")
    # The refine number is shown the way the mean uses it — on the rank scale, shift named.
    both = (f" (rank {verdict.score} · refine {calibrated_refine(refine.score, offset)}, "
            f"calibrated {offset:+.1f})") if verdict is not None and refine is not None else ""
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

    offset = refine_offset(ranked, refine)
    if ranked:
        lines += ["## Ranked (Opus)", ""]
        for v in sorted(ranked.values(), key=rank_order(refine, offset)):
            j = by_id.get(v.job_id)
            if not j:
                continue
            r = refine.get(v.job_id)
            lines += ranked_block(j, effective_score(v, r, offset), v, filters.get(j.id), r, offset)

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

    The refine scores are read on the rank stage's scale first; the shift that took them there
    is measured once here and kept on the snapshot as ``refine_offset``.
    """
    refine = refine or {}
    by_id = {j.id: j for j in jobs}
    dropped = {f.job_id for f in filters.values() if f.status == "drop"}
    prefilter = {k: v for k, v in prefilter.items() if k not in dropped}
    ranked = {k: v for k, v in ranked.items() if k not in dropped}
    items: list[ReportItem] = []
    offset = refine_offset(ranked, refine)

    for v in sorted(ranked.values(), key=rank_order(refine, offset)):
        if v.job_id in by_id:
            items.append(ReportItem(job_id=v.job_id, section="ranked", position=len(items) + 1,
                                    score=effective_score(v, refine.get(v.job_id), offset)))

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
                          cost=dict(cost or {}), path=str(path) if path else None, items=items,
                          refine_offset=offset if refine else None)


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
