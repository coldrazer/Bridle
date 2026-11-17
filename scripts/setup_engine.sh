#!/usr/bin/env bash
# Creates the engine virtualenv the app uses and installs MLX + server dependencies.
# Same result as the app's "Set Up Engine" button.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SUPPORT="${BRIDLE_HOME:-$HOME/Library/Application Support/com.bridle.app}"
VENV="$SUPPORT/engine-venv"

pick_python() {
  # The lockfile was generated on Python 3.11; prefer it when locking.
  if [ -f "$ROOT/engine/requirements-lock.txt" ]; then
    ORDER="/opt/homebrew/bin/python3.11 /usr/local/bin/python3.11 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.13 python3"
  else
    ORDER="/opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /usr/local/bin/python3.12 /usr/local/bin/python3.11 python3"
  fi
  for p in $ORDER; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
      command -v "$p"; return 0
    fi
  done
  return 1
}

PY="$(pick_python)" || { echo "Python 3.11+ not found. Install it with: brew install python@3.12" >&2; exit 1; }
echo "Using $PY"
mkdir -p "$SUPPORT"
[ -x "$VENV/bin/python3" ] || "$PY" -m venv "$VENV"
# No unpinned `pip install --upgrade pip`: the venv's bundled pip installs the locked wheels.
REQ="$ROOT/engine/requirements-lock.txt"; [ -f "$REQ" ] || REQ="$ROOT/engine/requirements.txt"
"$VENV/bin/python3" -m pip install -r "$REQ"
# Speech runtime for Aria's bundled voice model (always; small).
SPEECH="$ROOT/engine/requirements-speech-lock.txt"; [ -f "$SPEECH" ] && "$VENV/bin/python3" -m pip install -r "$SPEECH"
# Full extras (vision, embeddings, speech) install with setup so no feature
# fails later for a missing runtime. Failure is non-fatal: the app offers
# per-feature Install actions when something is missing.
XREQ="$ROOT/engine/requirements-extras-lock.txt"; [ -f "$XREQ" ] || XREQ="$ROOT/engine/requirements-extras.txt"
"$VENV/bin/python3" -m pip install -r "$XREQ" || echo "warning: some optional support did not install; Bridle will offer it when a feature needs it" >&2
"$VENV/bin/python3" -c "import mlx.core as mx; print('MLX', mx.__version__, '| Metal:', mx.metal.is_available())"
echo "Engine installed at $VENV"
