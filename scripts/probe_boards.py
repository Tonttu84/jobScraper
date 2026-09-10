#!/usr/bin/env python
"""Probe every ``ats_boards`` entry of a config, one board at a time.

``jobscraper probe ats_boards`` runs the whole source and stops at the first N jobs, which
tells you nothing about *which* of the sixty configured boards actually answered. This does
the opposite: it opens each board separately and prints how many postings it listed, how
many survived that board's title filter, how long it took, and one fully converted sample
so you can see whether company/country/remote/posted_at came out right.

    python scripts/probe_boards.py                          # every board in config/sources.yaml
    python scripts/probe_boards.py greenhouse successfactors # only boards whose label matches
    python scripts/probe_boards.py --config config/sources.yaml --timeout 60

Label = the careers URL, or ``<ats>:<slug>`` for an explicit-ATS entry; the positional
arguments are case-insensitive substrings of it, ORed together.

Exit code: 0 = every probed board answered, 1 = at least one failed, 2 = bad usage/config.
This hits the real sites, so it is an owner-machine tool — it is never run by the tests
(they drive it with fake scrapers).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from jobscraper.models import Job  # noqa: E402
from jobscraper.sources.ats_boards import (  # noqa: E402
    Board,
    _compile,
    _fill_descriptions,
    _lazy_flag,
    _open_board,
    convert,
    make_board,
)

DEFAULT_CONFIG = REPO / "config" / "sources.yaml"


@dataclass
class Result:
    label: str
    seconds: float
    listed: int = 0
    kept: int = 0
    sample: Job | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def load_boards(path: Path, *, timeout: float | None) -> tuple[list[Board], float]:
    """The `ats_boards` block of ``path`` → normalized boards + the configured timeout."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = (raw.get("sources") or {}).get("ats_boards")
    if not isinstance(section, dict):
        raise ValueError(f"{path}: no 'sources.ats_boards' section")
    entries = [
        e
        for e in (section.get("urls") or [])
        if e is not None and not (isinstance(e, str) and not e.strip())
    ]
    if not entries:
        raise ValueError(f"{path}: sources.ats_boards has no 'urls' entries")
    defaults = {
        "default_include": _compile(section.get("default_include"), "default_include", "options"),
        "default_exclude": _compile(section.get("default_exclude"), "default_exclude", "options"),
        "default_lazy": _lazy_flag(section.get("lazy_descriptions"), "option 'lazy_descriptions'"),
    }
    boards = [make_board(entry, **defaults) for entry in entries]
    return boards, float(timeout if timeout is not None else section.get("timeout", 30.0))


def probe_board(board: Board, *, timeout: float) -> Result:
    """Open one board, list it, filter it, and convert the first survivor."""
    start = time.perf_counter()
    try:
        scraper, lazy = _open_board(board, timeout=timeout, include_descriptions=True)
        ats_jobs = list(scraper.fetch())
    except Exception as exc:
        return Result(board.label, time.perf_counter() - start, error=f"{type(exc).__name__}: {exc}")
    kept = [job for job in ats_jobs if board.keep(job.title)]
    sample = None
    if kept:
        # Only the one posting we are about to print, never the whole survivor list.
        if lazy:
            _fill_descriptions(scraper, kept[:1], board.label)
        try:
            sample = convert(kept[0], company=board.company)
        except Exception as exc:
            return Result(
                board.label,
                time.perf_counter() - start,
                listed=len(ats_jobs),
                kept=len(kept),
                error=f"conversion failed: {type(exc).__name__}: {exc}",
            )
    return Result(board.label, time.perf_counter() - start, len(ats_jobs), len(kept), sample)


def _describe_sample(job: Job) -> list[str]:
    posted = job.posted_at.date().isoformat() if job.posted_at else "-"
    body = len(job.description or "")
    return [
        f"  sample: {job.title}",
        f"          company={job.company or '-'}  country={job.country or '-'} "
        f"city={job.city or '-'}  remote={job.remote}  posted_at={posted}",
        f"          location_raw={job.location_raw or '-'}  description={body} chars",
        f"          {job.url}",
    ]


def report(result: Result, index: int, total: int, out: Any) -> None:
    print(f"[{index}/{total}] {result.label}", file=out)
    if result.error:
        print(f"  FAILED after {result.seconds:.1f}s: {result.error}", file=out)
        return
    print(f"  listed {result.listed}, kept {result.kept}, {result.seconds:.1f}s", file=out)
    if result.sample is None:
        print("  no sample: nothing survived the title filter", file=out)
        return
    for line in _describe_sample(result.sample):
        print(line, file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe each ats_boards entry separately.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Label substrings are case-insensitive and ORed; with none, every board runs.",
    )
    parser.add_argument("labels", nargs="*", help="only probe boards whose label contains one of these")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="sources.yaml to read")
    parser.add_argument("--timeout", type=float, default=None, help="per-request timeout, seconds")
    parser.add_argument("-v", "--verbose", action="store_true", help="show the adapter's own logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        boards, timeout = load_boards(args.config, timeout=args.timeout)
    except (OSError, ValueError) as exc:
        print(f"probe_boards: {exc}", file=sys.stderr)
        return 2

    if args.labels:
        wanted = [s.lower() for s in args.labels]
        boards = [b for b in boards if any(s in b.label.lower() for s in wanted)]
        if not boards:
            print(
                f"probe_boards: no board label matches {args.labels}", file=sys.stderr
            )
            return 2

    results: list[Result] = []
    for i, board in enumerate(boards, 1):
        result = probe_board(board, timeout=timeout)
        results.append(result)
        report(result, i, len(boards), sys.stdout)
        sys.stdout.flush()

    failed = [r for r in results if not r.ok]
    empty = [r for r in results if r.ok and r.kept == 0]
    print(
        f"\n{len(results)} board(s): {len(results) - len(failed)} answered, {len(failed)} failed, "
        f"{len(empty)} answered with nothing after the title filter. "
        f"{sum(r.kept for r in results)} postings kept in total."
    )
    if failed:
        print("failed: " + ", ".join(r.label for r in failed))
    if empty:
        print("empty:  " + ", ".join(r.label for r in empty))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
