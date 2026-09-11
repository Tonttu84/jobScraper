#!/usr/bin/env bash
# Definition of done: lint clean + full test suite + coverage threshold.
#
#   scripts/check.sh            full run, gates on coverage (before every push of a code change)
#   scripts/check.sh --fast     the parallel part only: no browser suites, coverage not gated
#
# The non-browser tests run on all cores (pytest-xdist); the two headless-browser suites run
# afterwards on one worker, because parallel Chromium instances fail intermittently. Coverage is
# collected across both invocations (--cov-append) and gated once at the end.
set -euo pipefail
cd "$(dirname "$0")/.."
THRESHOLD="${COV_THRESHOLD:-99}"
WORKERS="${WORKERS:-auto}"
FAST=0
if [ "${1:-}" = "--fast" ]; then FAST=1; shift; fi
# Interpreter: $PY if set, else the project venv (POSIX or Windows layout), else `python`.
PY="${PY:-}"
if [ -z "$PY" ]; then
  for candidate in .venv/bin/python .venv/Scripts/python.exe; do
    [ -x "$candidate" ] && PY="$candidate" && break
  done
fi
PY="${PY:-python}"
"$PY" -m ruff check src tests
BROWSER=(tests/test_web_browser.py tests/test_browser.py)
IGNORE=(); for f in "${BROWSER[@]}"; do IGNORE+=("--ignore=$f"); done
rm -f .coverage
if [ "$FAST" = 1 ]; then
  "$PY" -m pytest -q -n "$WORKERS" --cov-fail-under=0 "${IGNORE[@]}" "$@"
  echo "fast mode: browser suites skipped, coverage not gated; run the full check before pushing"
else
  "$PY" -m pytest -q -n "$WORKERS" --cov-fail-under=0 --cov-report= "${IGNORE[@]}" "$@"
  "$PY" -m pytest -q --cov-append --cov-fail-under="$THRESHOLD" "${BROWSER[@]}"
fi
