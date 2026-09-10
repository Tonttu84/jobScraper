# Scheduled runs and the "what's new" diff

The point of running the pipeline on a schedule is that you only read what changed. Every
`jobscraper report` stores its snapshot in the database (`reports` + `report_items`) and
immediately diffs it against the previous snapshot in the same database, so the run ends with a
short list of jobs you have not seen before.

## The two options

**1. cron on your own machine** — `scripts/scheduled_run.sh`:

```bash
crontab -e
```

```cron
MAILTO=you@example.com
0 6 * * * cd /path/to/jobScraper && ./scripts/scheduled_run.sh >> data/cron.log 2>&1
```

The script sources `./.env` (cron does not read your shell profile, so that is where
`ANTHROPIC_API_KEY` comes from), runs `uv run jobscraper run "$@"` and then `uv run jobscraper
diff`, so the diff is the tail of the mail cron sends. `JOBSCRAPER_PROFILE` is inherited from
the environment; set it above the schedule line to run a different candidate's profile.

**2. GitHub Actions on a self-hosted runner** — `.github/workflows/scheduled-run.yml`
(`workflow_dispatch` + daily at 05:00 UTC). It is **disabled** until you register a self-hosted
runner and set the repository variable `SCHEDULED_RUNS` to `true`
(Settings → Secrets and variables → Actions → Variables); `ANTHROPIC_API_KEY` comes from the
repository secrets. GitHub-hosted runners are not an option: several job boards block cloud IP
ranges, and the key should stay on your machine. The workflow uploads `results/` as an artifact
and prints the newest `diff-*.md` into the job summary.

## What the diff contains

`diff_reports()` matches jobs by `Job.id` (sha1 of source + source id), which is stable between
runs, and compares the sections of two snapshots:

| Part | Meaning |
|---|---|
| **New ranked** | in the newer report's ranked section, not ranked in the older one — rendered as full report blocks (score, title, company, location, source, posted date, the Opus summary, why apply, concerns) |
| **New in prefilter** | in the prefilter section now, in neither the prefilter nor the ranked section before — a compact table (score, title, company, location, source) |
| **Dropped out of the ranking** | ranked before, not ranked now — a bullet with the old score |
| **Moved in the ranking** | ranked in both, score moved by ≥ 10 points |

The header line counts the first three: `N new ranked · M new in prefilter · K dropped out of
ranked since report #<id> (created …)`. The first report in a database has nothing to diff
against and says so; everything in it counts as new.

## Where the files land

| File | Written by |
|---|---|
| `results/report-YYYY-MM-DD.md` | `jobscraper report` — the full report, with a short `## New since report #N` section prepended |
| `results/diff-YYYY-MM-DD.md` | `jobscraper report` — the whole diff, next to the report file |
| `data/runs/<timestamp>.db` | `jobscraper run` — the run's database, carrying every earlier snapshot forward |
| `data/cron.log` | the crontab line above |

With `--profile ana` (or `JOBSCRAPER_PROFILE=ana`) both markdown files move to `results/ana/`.

## Reading a diff by hand

```bash
uv run jobscraper diff                          # newest report vs. the one before it
uv run jobscraper diff --report 12 --against 9  # any two snapshots in the same database
uv run jobscraper diff --out /tmp/whats-new.md  # print it and write it to a file
uv run jobscraper --db data/runs/20260910-060000.db diff
```

`jobscraper runs` lists the per-run databases; a diff can only compare snapshots stored in the
same database, which is why `run` copies the previous run's database forward unless you pass
`--fresh`.
