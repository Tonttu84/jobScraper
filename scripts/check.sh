#!/usr/bin/env bash
# Definition of done: lint clean + full test suite + coverage threshold.
set -euo pipefail
cd "$(dirname "$0")/.."
THRESHOLD="${COV_THRESHOLD:-85}"
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python"
"$PY" -m ruff check src tests
"$PY" -m pytest -q --cov-fail-under="$THRESHOLD" "$@"
