#!/usr/bin/env bash
# Mirror everything personal or run-specific into a PRIVATE git repository, so the public repo
# never carries a CV, a report or a decision, while the history of every run is still kept.
#
#   ./scripts/sync_private.sh            # copy → private clone, commit, push   (default: push)
#   ./scripts/sync_private.sh restore    # copy the config back (fresh clone / CI runner)
#
# The private clone lives next to this checkout by default; override with JOBSCRAPER_PRIVATE_DIR.
# Create it once:  gh repo create <you>/jobScraper-private --private --clone
# scripts/scheduled_run.sh calls the push at the end of every scheduled run.
set -euo pipefail
cd "$(dirname "$0")/.."
PRIVATE_DIR="${JOBSCRAPER_PRIVATE_DIR:-../jobScraper-private}"
MODE="${1:-push}"

# What is mirrored (relative paths; a missing one is skipped):
#   config/profile.yaml           the owner's CV and policies
#   config/profiles/              other candidates' profiles
#   results/                      every report and diff, all profiles
#   data/labels/                  hand labels (drop audit, eval sets)
#   data/exports/ai/              prompt chunks + subagent verdicts of the last AI round
#   data/serve/decisions.db       applied / skipped decisions from the web UI
#   data/profiles/*/serve/decisions.db
CONFIG_ITEMS=(config/profile.yaml config/profiles)
OUTPUT_ITEMS=(results data/labels data/exports/ai data/serve/decisions.db)

if [ ! -d "$PRIVATE_DIR/.git" ]; then
  echo "no private clone at $PRIVATE_DIR (set JOBSCRAPER_PRIVATE_DIR or create one, see the header)" >&2
  exit 1
fi

# copy SRC_ROOT/ITEM → DST_ROOT/ITEM, replacing the target so deletions propagate.
mirror() {
  local src_root="$1" dst_root="$2" item="$3"
  [ -e "$src_root/$item" ] || return 0
  [ "$item" = "config/profiles" ] && [ ! -d "$src_root/$item" ] && return 0
  rm -rf "${dst_root:?}/$item"
  mkdir -p "$(dirname "$dst_root/$item")"
  cp -r "$src_root/$item" "$dst_root/$item"
  # never carry a README that only explains the layout, nor other people's sqlite working files
  find "$dst_root/$item" -name '*.db-journal' -delete 2>/dev/null || true
}

case "$MODE" in
  push)
    for item in "${CONFIG_ITEMS[@]}" "${OUTPUT_ITEMS[@]}"; do mirror . "$PRIVATE_DIR" "$item"; done
    for db in data/profiles/*/serve/decisions.db; do [ -f "$db" ] && mirror . "$PRIVATE_DIR" "$db"; done
    head="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
    git -C "$PRIVATE_DIR" add -A
    if git -C "$PRIVATE_DIR" diff --cached --quiet; then
      echo "private: nothing changed"
    else
      git -C "$PRIVATE_DIR" commit -q -m "sync from jobScraper@$head $(date -u +%Y-%m-%dT%H:%MZ)"
      git -C "$PRIVATE_DIR" push -q -u origin HEAD
      echo "private: committed and pushed (jobScraper@$head)"
    fi
    ;;
  restore)
    for item in "${CONFIG_ITEMS[@]}"; do mirror "$PRIVATE_DIR" . "$item"; done
    echo "private: config restored from $PRIVATE_DIR"
    ;;
  *)
    echo "usage: $0 [push|restore]" >&2
    exit 2
    ;;
esac
