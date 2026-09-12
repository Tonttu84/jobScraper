"""Audits of the two cheap, deterministic stages nobody was measuring.

*The rule filter's false negatives.* It drops the great majority of everything scraped, and its
reasons are cheap and deterministic — which also means nobody knows how many good jobs go into
the bin with them. This module draws a *stratified* sample of dropped jobs (up to N per drop
reason, seeded so the same database always yields the same sample), which the owner labels by
hand, and turns the labels back into a false-negative rate per reason and an estimate of how
many good jobs each rule loses. Sampling per reason rather than uniformly is the point: a rule
that fires 100 times and a rule that fires 3000 times each need the same handful of eyeballs
before its rate is worth anything.

*The order the rank queue is read in.* Only ~90 of some 1 600 screen survivors are ranked per
round, so the ordering is most of the product. :func:`prior_comparison` replays both candidate
orderings — the lexical prior (:mod:`jobscraper.prior`) and the Sonnet screen score — against
the rank verdicts the database already holds, and reports how much sooner each of them would
have reached the jobs that ended up in the effective top N.
"""

from __future__ import annotations

import json
import math
import random
import re
from collections import defaultdict
from collections.abc import Collection, Sequence
from pathlib import Path

from jobscraper.config import Profile
from jobscraper.models import AIVerdict, FilterResult, Job
from jobscraper.store import Store

#: Reason template (as built in ``filters/rules.py`` and by ``cli.filter``) → category slug.
#: Ordered: the first pattern that matches wins. ``senior-level title`` is an older wording of
#: ``title level excluded by profile`` and is kept so databases from before 2026-09 still sort.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("non_software_title_hint", re.compile(r"^title looks like a non-software role\b")),
    ("not_software_title", re.compile(r"^title not a software/IT role\b")),
    ("level_excluded", re.compile(r"^title level excluded by profile\b")),
    ("senior_title", re.compile(r"^senior-level title\b")),
    ("seniority_label", re.compile(r"^seniority label .*excluded by profile$")),
    ("years_required", re.compile(r"^asks for .*years of experience$")),
    ("posting_language", re.compile(r"^posting written in ")),
    ("language_required", re.compile(r"^requires ")),
    ("location", re.compile(r"^on-site in .*outside target countries$")),
    ("too_old", re.compile(r"^posting is .*days old")),
    ("deadline_passed", re.compile(r"^application deadline passed on ")),
    ("duplicate", re.compile(r"^duplicate of ")),
)

#: What the owner may write in a row's ``label`` field.
LABELS = ("good", "bad", "unsure")


def reason_category(reason: str) -> str:
    """Short, stable slug for one drop reason. Anything unrecognised is ``other``.

    ``tests/test_audit.py`` scans ``rules.py`` for reason templates and fails if one of them
    lands in ``other``, so a new rule cannot silently escape the audit.
    """
    for slug, pattern in _PATTERNS:
        if pattern.match(reason):
            return slug
    return "other"


def sample_drops(store: Store, per_reason: int = 15, seed: int = 1) -> list[dict]:
    """Up to ``per_reason`` dropped jobs per reason category, drawn with ``seed``.

    Categories come from each job's *primary* (first) reason: that is the one the owner has to
    judge, and the later reasons are shown in the row for context. Rows are ordered by category
    so the labelling file reads like a stratified worksheet.
    """
    drops = store.filter_results("drop")
    by_category: dict[str, list[str]] = defaultdict(list)
    for job_id, result in drops.items():
        primary = result.reasons[0] if result.reasons else ""
        by_category[reason_category(primary)].append(job_id)

    rng = random.Random(seed)
    picked: dict[str, list[str]] = {}
    for category in sorted(by_category):
        ids = sorted(by_category[category])  # sorted first: the draw must not depend on row order
        picked[category] = sorted(rng.sample(ids, min(per_reason, len(ids))))

    wanted = [job_id for ids in picked.values() for job_id in ids]
    jobs = {j.id: j for j in store.jobs(ids=wanted)}
    rows: list[dict] = []
    for category, ids in picked.items():
        for job_id in ids:
            job = jobs.get(job_id)
            if job is None:  # a filter row can outlive its job when a database is pruned
                continue
            rows.append({
                "job_id": job.id,
                "source": job.source,
                "title": job.title,
                "company": job.company,
                "location_raw": job.location_raw,
                "country": job.country,
                "url": job.url,
                "category": category,
                "reasons": list(drops[job_id].reasons),
                "total_in_category": len(by_category[category]),
                "label": "",
                "note": "",
            })
    return rows


def totals_from_rows(rows: list[dict]) -> dict[str, int]:
    """Read the per-category totals back out of a sample file (each row carries its own)."""
    return {row["category"]: int(row.get("total_in_category") or 0) for row in rows}


def _blank_tally() -> dict:
    return {"sampled": 0, "good": 0, "bad": 0, "unsure": 0, "unlabelled": 0, "invalid": 0}


def score_labels(rows: list[dict], totals: dict[str, int]) -> dict:
    """Turn hand-written labels into a false-negative rate per category.

    ``rate`` is ``good / (good + bad)``: rows the owner left blank or marked ``unsure`` are
    counted but kept out of the denominator, because they say nothing either way. Multiplying
    the rate by the number of jobs the category dropped gives ``estimated_lost`` — the headline
    number, and the reason each row remembers its category total. The overall estimate is the
    sum of the per-category ones, not the pooled rate times everything: the sample is
    stratified, so pooling would weight a small reason like a huge one.
    """
    tallies: dict[str, dict] = {}
    for row in rows:
        tally = tallies.setdefault(str(row.get("category") or "other"), _blank_tally())
        tally["sampled"] += 1
        label = str(row.get("label") or "").strip().lower()
        if label in LABELS:
            tally[label] += 1
        elif not label:
            tally["unlabelled"] += 1
        else:
            tally["invalid"] += 1
            tally["unlabelled"] += 1

    categories: dict[str, dict] = {}
    for name in sorted(tallies):
        t = tallies[name]
        judged = t["good"] + t["bad"]
        rate = (t["good"] / judged) if judged else None
        total = int(totals.get(name, 0))
        categories[name] = {
            **t,
            "labelled": t["good"] + t["bad"] + t["unsure"],
            "rate": rate,
            "total": total,
            "estimated_lost": None if rate is None else rate * total,
        }

    overall = _blank_tally()
    overall.update({"labelled": 0, "total": 0, "estimated_lost": 0.0, "rate": None})
    for cat in categories.values():
        for key in ("sampled", "good", "bad", "unsure", "unlabelled", "invalid", "labelled", "total"):
            overall[key] += cat[key]
        overall["estimated_lost"] += cat["estimated_lost"] or 0.0
    judged = overall["good"] + overall["bad"]
    overall["rate"] = (overall["good"] / judged) if judged else None
    return {"categories": categories, "overall": overall}


# ------------------------------------------------------------------------- JSONL


def write_rows(path: Path, rows: list[dict]) -> Path:
    """Write the sample as one JSON object per line, ready to be edited by hand."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def read_labels(path: Path) -> list[dict]:
    """Read a labelled sample file back; blank lines (easy to leave behind) are ignored."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ------------------------------------------------------ is the queue in a useful order?

#: How deep into the queue to count, in jobs. Three cuts of the 90-call round budget: what one
#: window-and-a-half, two thirds of a round, and two thirds again would have reached.
DEPTHS: tuple[int, ...] = (20, 40, 60)


def average_ranks(values: Sequence[float]) -> list[float]:
    """1-based ranks of ``values``, smallest first, with ties sharing the mean of their places."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        stop = start
        while stop + 1 < len(order) and values[order[stop + 1]] == values[order[start]]:
            stop += 1
        shared = (start + stop) / 2 + 1  # 1-based, averaged over the positions the tie covers
        for i in order[start : stop + 1]:
            ranks[i] = shared
        start = stop + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman's rho: Pearson over :func:`average_ranks`, so ties are handled properly.

    ``None`` when there is nothing to correlate — fewer than two pairs, or one side that never
    moves (an all-zero prior on a profile with no skills would otherwise divide by zero).
    Implemented here rather than pulled in: scipy is a large dependency for eight lines.
    """
    if len(xs) != len(ys):
        raise ValueError(f"spearman needs two series of the same length, got {len(xs)} and {len(ys)}")
    if len(xs) < 2:
        return None
    rx, ry = average_ranks(xs), average_ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    var_x = sum((a - mx) ** 2 for a in rx)
    var_y = sum((b - my) ** 2 for b in ry)
    if not var_x or not var_y:
        return None
    return cov / math.sqrt(var_x * var_y)


def reading_depth(order: Sequence[str], targets: Collection[str],
                  depths: Sequence[int] = DEPTHS) -> dict:
    """Where ``targets`` sit in ``order``: how many inside each cut, and their median position.

    Positions are 1-based; a target missing from ``order`` is simply not counted (``found`` says
    how many were located at all). The median is the honest summary — a single target buried at
    position 400 says more about the ordering than any one cut does.
    """
    positions = sorted(n for n, job_id in enumerate(order, start=1) if job_id in targets)
    return {
        "found": len(positions),
        "at": {int(d): sum(1 for p in positions if p <= d) for d in depths},
        "median_position": _median(positions),
    }


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def prior_comparison(jobs: list[Job], filters: dict[str, FilterResult],
                     prefilter: dict[str, AIVerdict], rank: dict[str, AIVerdict],
                     refine: dict[str, AIVerdict], profile: Profile, *,
                     top: int = 20, depths: Sequence[int] = DEPTHS) -> dict:
    """Would the lexical prior have found the good jobs sooner than the screen score did?

    Both orderings are replayed over the jobs that carry a rank verdict — the only jobs whose
    answer is known — and measured two ways: Spearman against the rank score, and how deep into
    the ordering the current effective top ``top`` sits.

    The comparison is conservative by construction and cannot be made otherwise: the ranked jobs
    were themselves chosen by the screen ordering, so the screen score is being graded on its
    own sample. A prior that ties here is already ahead.
    """
    from jobscraper.prior import rank_prior
    from jobscraper.report import effective_top

    prior = rank_prior(jobs, profile)
    screen = {job_id: float(v.score) for job_id, v in prefilter.items()}
    measured = sorted(job_id for job_id in rank if job_id in prior)
    top_ids, _edge = effective_top(jobs, filters, rank, refine, top=top)

    orderings = {}
    for name, scores in (("prior", prior), ("screen", screen)):
        order = sorted(measured, key=lambda i: (-scores.get(i, 0.0), -screen.get(i, 0.0), i))
        pairs = [(scores.get(i, 0.0), float(rank[i].score)) for i in measured if i in scores]
        orderings[name] = {
            "n": len(pairs),
            "spearman": spearman([p[0] for p in pairs], [p[1] for p in pairs]),
            **reading_depth(order, set(top_ids), depths),
        }
    return {
        "sample": len(measured),
        "top": list(top_ids),
        "depths": tuple(int(d) for d in depths),
        "orderings": orderings,
    }
