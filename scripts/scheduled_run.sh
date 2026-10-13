#!/usr/bin/env bash
# One scheduled pipeline run: scrape → filter → prefilter → rank → report, then the diff
# against the previous report, so the tail of the output is only what changed.
#
# cron (`crontab -e`), 06:00 local time every day — MAILTO makes cron mail the output:
#
#   MAILTO=you@example.com
#   0 6 * * * cd /path/to/jobScraper && ./scripts/scheduled_run.sh >> data/cron.log 2>&1
#
# cron does not read your shell profile, so ANTHROPIC_API_KEY has to come from somewhere:
# this script sources ./.env when it exists. $JOBSCRAPER_PROFILE is inherited from the
# environment (set it above the schedule line in the crontab, or run
# `JOBSCRAPER_PROFILE=ana ./scripts/scheduled_run.sh`) and moves config, data and results
# under that candidate's directories.
#
# Any extra arguments go to `jobscraper run`, e.g. `./scripts/scheduled_run.sh --skip-ai`.
set -euo pipefail
cd "$(dirname "$0")/.."
# 2026-10-13: with stdout redirected to a log, Windows Python falls back to cp1252 and the
# scrape's "✓ source" line raised UnicodeEncodeError, which ended the whole scrape after the
# first source. UTF-8 mode makes stdout UTF-8 regardless of the console.
export PYTHONUTF8=1

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

# `uv run` when uv is on PATH, else `python -m uv` (the owner's Windows machine has uv only as a
# module of the system Python), else the project venv's own `jobscraper` entry point.
if command -v uv >/dev/null 2>&1; then
  JOBSCRAPER=(uv run jobscraper)
elif python -m uv --version >/dev/null 2>&1; then
  JOBSCRAPER=(python -m uv run jobscraper)
else
  BIN=""
  for candidate in .venv/bin/jobscraper .venv/Scripts/jobscraper.exe; do
    [ -x "$candidate" ] && BIN="$candidate" && break
  done
  JOBSCRAPER=("${BIN:-jobscraper}")
fi

"${JOBSCRAPER[@]}" run "$@"
"${JOBSCRAPER[@]}" diff
# Keep the reports and decisions in the private repo (no-op if there is no private clone yet).
./scripts/sync_private.sh || echo "private sync skipped"
