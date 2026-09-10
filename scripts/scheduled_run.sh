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

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

uv run jobscraper run "$@"
uv run jobscraper diff
# Keep the reports and decisions in the private repo (no-op if there is no private clone yet).
./scripts/sync_private.sh || echo "private sync skipped"
