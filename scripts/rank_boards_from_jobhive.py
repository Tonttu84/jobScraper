#!/usr/bin/env python
"""Rank company career boards by their tech postings, from the ``jobhive`` dataset.

``ats-scrapers`` publishes a daily dump of everything its scrapers see
(https://storage.stapply.ai/jobhive/v1/manifest.json). This script reads the per-ATS
``jobs.parquet`` slices for the ATSes our ``ats_boards`` source can drive, keeps the
postings in the profile's target countries, and ranks each ``(company, ats)`` pair by how
many of them look like tech roles. The output is the markdown table in
``docs/boards-ranked-<date>.md``; the rows with the most tech postings are what gets added
to ``config/sources.yaml``.

**This script is not part of the package and is not covered by the test suite.** It needs
``pyarrow``/``fsspec``/``aiohttp``/``pyyaml``, which are deliberately NOT project
dependencies — do not add them. Run it from a throwaway venv instead::

    python -m venv /tmp/jobhive/venv
    /tmp/jobhive/venv/Scripts/python -m pip install pyarrow fsspec aiohttp pyyaml
    /tmp/jobhive/venv/Scripts/python scripts/rank_boards_from_jobhive.py \
        --cache /tmp/jobhive/cache --out docs/boards-ranked-2026-09-10.md

Only the six columns we aggregate on are read, and parquet supports HTTP range requests,
so the whole run pulls a few hundred MB rather than the ~17 GB the full CSV dump weighs.
Nothing it downloads may be committed.

``--out`` rewrites the whole file, so the hand-written "What went into config/sources.yaml"
and "Things to know about this dataset" sections of ``docs/boards-ranked-2026-09-10.md``
have to be pasted back after a re-run (or written to a fresh dated file instead).
``--rows-cache`` keeps the per-board aggregates so tweaking the report costs nothing.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

MANIFEST = "https://storage.stapply.ai/jobhive/v1/manifest.json"
HEADERS = {"User-Agent": "jobScraper/board-ranking (+https://github.com/)"}

# ATSes whose boards `ats_boards` can drive: either `get_scraper_for_url` recognizes the
# careers URL, or `{ats: <name>, slug: <companies.csv slug>}` goes straight to `get_scraper`.
SUPPORTED_ATS = {
    # URL-resolvable, one slug per tenant
    "greenhouse", "lever", "ashby", "workable", "personio", "smartrecruiters", "recruitee",
    "breezy", "teamtailor", "bamboohr", "pinpoint", "jazzhr", "rippling", "gem",
    "recruiterbox", "join_com", "softgarden",
    # custom-domain enterprise sites: named with `ats:` + `slug:`
    "workday", "successfactors", "oracle", "icims", "phenom", "eightfold", "cornerstone",
    "jobvite", "dayforce", "ukg", "taleo", "pageup", "paycom", "paylocity", "darwinbox",
    "keka", "adp",
    # one company, one dedicated scraper (slug is ignored by these)
    "amazon", "apple", "google", "tesla", "uber", "tiktok", "bytedance", "meta",
}

# Why the rest are out — printed with --explain so the exclusions stay auditable.
SKIPPED_ATS = {
    "eures": "public EU aggregator, not a company board (jobscraper has its own source)",
    "bundesagentur": "German public employment agency aggregator",
    "arbetsformedlingen": "Swedish public employment agency aggregator",
    "jobbankca": "Canadian public job bank",
    "jobsch": "Swiss job board aggregator",
    "jobs_cz": "Czech job board aggregator",
    "infojobs_es": "Spanish job board aggregator",
    "usajobs": "US federal jobs",
    "seek": "AU/NZ job board",
    "ycombinator": "startup-directory aggregator",
    "builtin": "US tech job board",
    "wellfound": "startup job board aggregator",
    "remoteok": "aggregator (jobscraper has its own source)",
    "weworkremotely": "aggregator (disabled in sources.yaml)",
    "thehub": "aggregator (jobscraper has its own source)",
    "manfred": "Spanish job board",
    "getonbrd": "LatAm job board",
    "programathor": "Brazilian job board",
    "mercor": "marketplace, not a company board",
    "gupy": "Brazil-only",
    "beisen": "China-only",
    "beisen_legacy": "China-only",
    "moka": "China-only",
    "herp": "Japan-only",
    "hrmos": "Japan-only",
    "wanted": "Korea-only",
    "avature": "needs Browserbase; the Siemens tenant is a known dead end (6 jobs/page)",
    "welcometothejungle": "a job board, not a company ATS — jobscraper has its own `wttj` source",
}

TECH_RE = re.compile(
    r"engineer|developer|software|programmer|data|devops|cloud|backend|frontend|"
    r"full.?stack|embedded|firmware|intern|trainee|graduate|working student|werkstudent|"
    r"harjoittelija|kehittäjä",
    re.IGNORECASE,
)
SENIOR_RE = re.compile(
    r"senior|lead|principal|staff|manager|director|head of|architect|vp\b", re.IGNORECASE
)

COLUMNS = ["company", "ats_type", "title", "country_iso", "location", "url"]


def _fs():
    import fsspec

    return fsspec.filesystem("http", client_kwargs={"headers": HEADERS})


def _fetch_text(url: str, cache: Path | None, name: str) -> str:
    if cache is not None:
        path = cache / name
        if path.exists():
            return path.read_text(encoding="utf-8")
    with _fs().open(url, "rb") as fh:
        text = fh.read().decode("utf-8")
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
        (cache / name).write_text(text, encoding="utf-8")
    return text


def target_countries(config_dir: Path) -> set[str]:
    """Union of the location tiers of every profile under ``config/`` (felipe is gitignored)."""
    import yaml

    paths = [config_dir / "profile.yaml", *sorted(config_dir.glob("profiles/*/profile.yaml"))]
    out: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        loc = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("location") or {}
        for key, value in loc.items():
            if key.startswith("tier") and isinstance(value, list):
                out |= {str(v).upper() for v in value if v}
    return out


@dataclass
class Row:
    company: str
    ats: str
    total: int = 0
    tech: int = 0
    junior: int = 0
    board_total: int = 0  # postings worldwide on that board, for the include: decision
    countries: Counter = field(default_factory=Counter)
    sample_url: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.company, self.ats)

    def top_countries(self, n: int = 3) -> str:
        return ", ".join(f"{c} {n_}" for c, n_ in self.countries.most_common(n))


def scan_ats(ats: str, url: str, wanted: set[str], rows: dict[tuple[str, str], Row]) -> int:
    """Aggregate one per-ATS parquet slice into ``rows``. Returns the number of rows read."""
    import pyarrow.parquet as pq

    with _fs().open(url, "rb") as fh:
        table = pq.ParquetFile(fh).read(columns=COLUMNS)
    companies = table.column("company").to_pylist()
    titles = table.column("title").to_pylist()
    countries = table.column("country_iso").to_pylist()
    urls = table.column("url").to_pylist()
    ats_types = table.column("ats_type").to_pylist()

    for company, title, country, job_url, ats_type in zip(
        companies, titles, countries, urls, ats_types, strict=True
    ):
        if not company:
            continue
        name = str(ats_type or ats)
        row = rows.get((company, name))
        if row is None:
            row = rows[(company, name)] = Row(company=company, ats=name)
        row.board_total += 1
        code = (country or "").upper()
        if code not in wanted:
            continue
        row.total += 1
        row.countries[code] += 1
        text = title or ""
        if TECH_RE.search(text):
            row.tech += 1
            if not row.sample_url:
                row.sample_url = job_url or ""
            if not SENIOR_RE.search(text):
                row.junior += 1
    return table.num_rows


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def load_boards(ats: str, url: str, cache: Path | None) -> dict[str, dict[str, str]]:
    """``companies.csv`` for one ATS, indexed by every name the dataset might use.

    The dataset's ``company`` column is the tenant's display name for some ATSes, the raw
    slug for others (SmartRecruiters says ``BoschGroup`` where the CSV says ``Bosch Group``)
    and the careers host for Phenom, so all three are indexed, punctuation-insensitively.
    """
    text = _fetch_text(url, cache, f"companies-{ats}.csv")
    out: dict[str, dict[str, str]] = {}
    for raw in csv.DictReader(io.StringIO(text)):
        rec = {k: (v or "").strip() for k, v in raw.items() if k}
        host = re.sub(r"^https?://", "", rec.get("url", "")).split("/")[0]
        for key in (rec.get("name"), rec.get("company_name"), rec.get("slug"), host):
            if key:
                out.setdefault(_norm(key), rec)
    return out


# Companies whose board is their own scraper: the slug is ignored, nothing to look up.
DEDICATED = {"amazon", "apple", "google", "tesla", "uber", "tiktok", "bytedance", "meta"}
# ATSes whose slug is just the careers site's origin, so one job URL is enough to rebuild it.
ORIGIN_IS_THE_SLUG = {"successfactors", "icims", "phenom", "oracle", "eightfold", "taleo"}


def board_ref(ats: str, rec: dict[str, str] | None, sample_url: str) -> tuple[str, str]:
    """The board's careers URL (or slug), and where that came from."""
    if ats in DEDICATED:
        return f"ats: {ats}", "dedicated scraper"
    if rec:
        return rec.get("url") or rec.get("slug") or "", "companies.csv"
    if sample_url and ats in ORIGIN_IS_THE_SLUG:
        parts = urlsplit(sample_url)
        base = f"{parts.scheme}://{parts.netloc}"
        segments = [s for s in parts.path.split("/") if s]
        # SuccessFactors tenants often sit under one path segment: /<tenant>/job/<id>
        if ats == "successfactors" and len(segments) > 1 and segments[1] == "job":
            base = f"{base}/{segments[0]}"
        return base, "derived from a job URL"
    return "", "unknown"


def board_options(ats: str, rec: dict[str, str] | None) -> dict[str, str]:
    """Extra ctor kwargs the ATS needs beyond the slug (Phenom locale/country, …)."""
    if not rec:
        return {}
    keys = {"phenom": ("locale", "country"), "eightfold": ("domain",), "cornerstone": ("site_id",)}
    return {k: rec[k] for k in keys.get(ats, ()) if rec.get(k)}


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", type=Path, help="directory for the downloaded manifest/companies.csv")
    ap.add_argument("--out", type=Path, help="write the markdown report here (default: stdout)")
    ap.add_argument("--json-out", type=Path, help="also dump the rows as JSON (for follow-up scripts)")
    ap.add_argument("--min-tech", type=int, default=5, help="rows with fewer tech postings are dropped")
    ap.add_argument("--config-dir", type=Path, default=repo / "config")
    ap.add_argument("--ats", action="append", help="limit to these ATSes (repeatable)")
    ap.add_argument("--explain", action="store_true", help="print the ATS include/exclude decisions")
    ap.add_argument(
        "--rows-cache",
        type=Path,
        help="reuse (or write) the per-board aggregates here, so re-running the report "
        "does not re-read every parquet slice",
    )
    args = ap.parse_args(argv)

    manifest = json.loads(_fetch_text(MANIFEST, args.cache, "manifest.json"))
    wanted = target_countries(args.config_dir)
    print(f"target countries ({len(wanted)}): {' '.join(sorted(wanted))}", file=sys.stderr)

    by_ats = manifest["by_ats"]
    selected = sorted(set(by_ats) & SUPPORTED_ATS)
    if args.ats:
        selected = [a for a in selected if a in set(args.ats)]
    if args.explain:
        for name in sorted(by_ats):
            why = "supported" if name in SUPPORTED_ATS else SKIPPED_ATS.get(name, "not driveable")
            print(f"  {name:22s} {by_ats[name].get('rows', 0):>9} rows  {why}", file=sys.stderr)

    rows: dict[tuple[str, str], Row] = {}
    if args.rows_cache and args.rows_cache.exists():
        for rec in json.loads(args.rows_cache.read_text(encoding="utf-8")):
            row = Row(**{**rec, "countries": Counter(rec["countries"])})
            rows[row.key] = row
        print(f"reused {len(rows)} aggregates from {args.rows_cache}", file=sys.stderr)
    else:
        for name in selected:
            slice_url = by_ats[name].get("parquet")
            if not slice_url:
                print(f"  {name}: no parquet in the manifest, skipped", file=sys.stderr)
                continue
            read = scan_ats(name, slice_url, wanted, rows)
            print(f"  {name}: {read} rows", file=sys.stderr, flush=True)
        if args.rows_cache:
            args.rows_cache.parent.mkdir(parents=True, exist_ok=True)
            args.rows_cache.write_text(
                json.dumps(
                    [{**vars(r), "countries": dict(r.countries)} for r in rows.values()],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

    boards: dict[str, dict[str, dict[str, str]]] = {}
    for name in selected:
        entry = manifest.get("by_ats_companies", {}).get(name)
        if entry and entry.get("csv"):
            boards[name] = load_boards(name, entry["csv"], args.cache)

    keep = sorted(
        (r for r in rows.values() if r.tech >= args.min_tech),
        key=lambda r: (-r.tech, -r.junior, r.company.lower()),
    )
    records = []
    for row in keep:
        rec = boards.get(row.ats, {}).get(_norm(row.company))
        board, origin = board_ref(row.ats, rec, row.sample_url)
        records.append(
            {
                "company": (rec or {}).get("name") or row.company,
                "dataset_key": row.company,
                "ats": row.ats,
                "board": board,
                "board_source": origin,
                "slug": (rec or {}).get("slug", ""),
                "options": board_options(row.ats, rec),
                "target_total": row.total,
                "tech": row.tech,
                "junior_tech": row.junior,
                "board_total": row.board_total,
                "top_countries": row.top_countries(),
                "sample_url": row.sample_url,
            }
        )

    per_ats = Counter(r["ats"] for r in records)
    lines = [
        f"# Career boards ranked by tech postings ({manifest['updated_at'][:10]})",
        "",
        "## How this was made",
        "",
        f"- Source: the `ats-scrapers` **jobhive** dataset, manifest generated "
        f"`{manifest['generated_at'][:19]}Z` ({manifest['stats']['total_jobs']:,} postings, "
        f"{manifest['stats']['total_companies']:,} companies, "
        f"{manifest['stats']['ats_count']} ATSes).",
        "- Script: `scripts/rank_boards_from_jobhive.py` (needs a throwaway venv with "
        "`pyarrow fsspec aiohttp pyyaml`; see its docstring — those are **not** project "
        "dependencies).",
        f"- Only the ATSes `ats_boards` can drive are scanned "
        f"({len(selected)} of {len(by_ats)}): aggregators (EURES, Bundesagentur, "
        f"Arbetsförmedlingen, …), region-locked ATSes (Gupy/Beisen/HRMOS/Wanted) and Avature "
        f"(needs Browserbase) are out.",
        f"- Target countries: the union of the `location` tiers of every profile under "
        f"`config/` — {len(wanted)} codes: `{' '.join(sorted(wanted))}`.",
        "- A posting counts as **tech** when its title matches "
        "`engineer|developer|software|programmer|data|devops|cloud|backend|frontend|"
        "full.?stack|embedded|firmware|intern|trainee|graduate|working student|werkstudent|"
        "harjoittelija|kehittäjä`, and as **junior** when it additionally does *not* match "
        "`senior|lead|principal|staff|manager|director|head of|architect|vp\\b`.",
        f"- Rows: every `(company, ats)` with at least {args.min_tech} tech postings in those "
        f"countries — {len(records)} of {len(rows):,} boards seen.",
        "- `Board total` is the board's worldwide posting count; boards over ~500 need a "
        "per-board `include:` in `config/sources.yaml` or the scrape takes forever.",
        "- The board column comes from that ATS's `companies.csv`. Where the tenant is missing "
        "from it (mostly SuccessFactors), the origin of one of its job URLs is used instead — "
        "that origin *is* what those scrapers take as their slug, but it stays a guess until "
        "`scripts/probe_boards.py` says otherwise.",
        "",
        f"Boards per ATS: {', '.join(f'{a} {n}' for a, n in per_ats.most_common())}.",
        "",
        "## Ranking",
        "",
        "| # | Company | ATS | Board (URL or slug) | Target postings | Tech | Non-senior tech | Board total | Top countries |",
        "| --: | --- | --- | --- | --: | --: | --: | --: | --- |",
    ]
    for i, rec in enumerate(records, 1):
        board = rec["board"] or "_(unknown — not in companies.csv)_"
        company = rec["company"].replace("|", "\\|")
        lines.append(
            f"| {i} | {company} | {rec['ats']} | {board} | {rec['target_total']} | "
            f"{rec['tech']} | {rec['junior_tech']} | {rec['board_total']} | {rec['top_countries']} |"
        )
    lines.append("")
    report = "\n".join(lines)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"wrote {args.out} ({len(records)} rows)", file=sys.stderr)
    else:
        sys.stdout.write(report)
    if args.json_out:
        args.json_out.write_text(json.dumps(records, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
