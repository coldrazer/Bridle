#!/usr/bin/env bash
# Downloads Aria's bundled helper model into assets/Aria/ so build_app.sh can
# ship it inside Bridle.app (Resources/Aria). Rerun to refresh the bundle;
# the pinned revision is recorded in assets/Aria/.bridle.json by the downloader.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SUPPORT="${BRIDLE_HOME:-$HOME/Library/Application Support/com.bridle.app}"
PY="${BRIDLE_PYTHON:-$SUPPORT/engine-venv/bin/python3}"
REPO="mlx-community/Qwen3-0.6B-4bit"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

if [ ! -x "$PY" ]; then
  echo "Engine not installed. Run: $ROOT/scripts/setup_engine.sh" >&2
  exit 1
fi

BRIDLE_HOME="$STAGE" BRIDLE_PYTHON="$PY" "$ROOT/scripts/bridle" download "$REPO"
SRC="$STAGE/models/$REPO"
DEST="$ROOT/assets/Aria"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -R "$SRC"/. "$DEST"/
echo "Aria bundle ready at $DEST ($(du -sh "$DEST" | cut -f1))"
