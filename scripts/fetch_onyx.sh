#!/usr/bin/env bash
# Downloads the bundled Onyx Whisper Tiny speech model (encoder + decoder)
# into assets/OnyxWhisper/ so build_app.sh can ship it inside Bridle.app
# (Resources/OnyxWhisper). Rerun to refresh the bundle.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SUPPORT="${BRIDLE_HOME:-$HOME/Library/Application Support/com.bridle.app}"
PY="${BRIDLE_PYTHON:-$SUPPORT/engine-venv/bin/python3}"
REPO="wabibito/Onyx-Whisper-Tiny"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

if [ ! -x "$PY" ]; then
  echo "Engine not installed. Run: $ROOT/scripts/setup_engine.sh" >&2
  exit 1
fi

BRIDLE_HOME="$STAGE" BRIDLE_PYTHON="$PY" "$ROOT/scripts/bridle" download "$REPO"
SRC="$STAGE/models/$REPO"
DEST="$ROOT/assets/OnyxWhisper"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -R "$SRC"/. "$DEST"/
echo "Onyx Whisper bundle ready at $DEST ($(du -sh "$DEST" | cut -f1))"
