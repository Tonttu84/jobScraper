#!/usr/bin/env bash
# Set up a fresh clone: the dev environment and the commit hooks.
#
#   scripts/setup.sh
#
# The hooks in .githooks/ (commit-msg, pre-push) come from Tonttu84/ai-workflow and check
# every commit message; git runs them only once core.hooksPath points there, which a clone
# does not copy, so every clone runs this once.
set -euo pipefail
cd "$(dirname "$0")/.."
git config core.hooksPath .githooks
uv sync --extra dev
echo "setup done: dev environment synced, commit hooks on (core.hooksPath=.githooks)"
