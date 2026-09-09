#!/usr/bin/env bash
# Definition of done: lint clean + full test suite + coverage threshold.
set -euo pipefail
cd "$(dirname "$0")/.."
THRESHOLD="${COV_THRESHOLD:-91}"
# Interpreter: $PY if set, else the project venv (POSIX or Windows layout), else `python`.
PY="${PY:-}"
if [ -z "$PY" ]; then
  for candidate in .venv/bin/python .venv/Scripts/python.exe; do
    [ -x "$candidate" ] && PY="$candidate" && break
  done
fi
PY="${PY:-python}"
"$PY" -m ruff check src tests
"$PY" -m pytest -q --cov-fail-under="$THRESHOLD" "$@"
