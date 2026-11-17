#!/usr/bin/env bash
# Fetches the engine test fixture models into a persistent cache (survives reboot)
# so the real-generation pytest suite can run anywhere. engine/tests/conftest.py
# calls this automatically when fixtures are missing; set BRIDLE_SKIP_MODEL_FETCH=1
# to run without them (those tests then skip loudly, as before).
#   scripts/fetch_test_models.sh     fetch missing fixtures into the cache
#   scripts/fetch_test_models.sh -q  fetch, print nothing on success
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STORE="${BRIDLE_TEST_STORE:-$HOME/Library/Caches/com.bridle.app/test-models}"
MODELS="$STORE/models"
QUIET="${1:-}"
mkdir -p "$MODELS"
PY="${BRIDLE_PYTHON:-$STORE/engine-venv/bin/python3}"
[ -x "$PY" ] || PY="$HOME/Library/Application Support/com.bridle.app/engine-venv/bin/python3"
for REPO in mlx-community/Qwen2.5-0.5B-Instruct-4bit mlx-community/all-MiniLM-L6-v2-bf16; do
  if [ ! -f "$MODELS/$REPO/.bridle.json" ]; then
    [ "$QUIET" = "-q" ] || echo "fetch_test_models: downloading $REPO"
    BRIDLE_HOME="$STORE" BRIDLE_PYTHON="$PY" "$ROOT/scripts/bridle" download "$REPO"
  fi
done
[ "$QUIET" = "-q" ] || echo "BRIDLE_TEST_MODELS_DIR=$MODELS"
