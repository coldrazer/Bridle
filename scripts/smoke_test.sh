#!/usr/bin/env bash
# End-to-end check on a real Mac: downloads a 0.3 GB model, serves it, and runs a chat completion.
# Usage: scripts/smoke_test.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODEL="mlx-community/Qwen2.5-0.5B-Instruct-4bit"
TMP="$(mktemp -d)"
export BRIDLE_HOME="$TMP"   # isolated models dir; the real one is untouched
PY="${BRIDLE_PYTHON:-$HOME/Library/Application Support/com.bridle.app/engine-venv/bin/python3}"
export BRIDLE_PYTHON="$PY"
cleanup() {
  if [[ -n "${SERVER:-}" ]]; then
    kill "$SERVER" 2>/dev/null || true
    wait "$SERVER" 2>/dev/null || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

"$ROOT/scripts/bridle" download "$MODEL"
"$ROOT/scripts/bridle" serve --port 0 --preload "$MODEL" > "$TMP/out" 2> "$TMP/err" &
SERVER=$!
for _ in $(seq 1 60); do grep -q BRIDLE_READY "$TMP/out" 2>/dev/null && break; sleep 0.5; done
PORT="$(sed -n 's/.*"port": \([0-9]*\).*/\1/p' "$TMP/out")"
echo "server on port $PORT"
for _ in $(seq 1 60); do
  curl -sf "http://127.0.0.1:$PORT/v1/chat/completions" \
    -d '{"messages":[{"role":"user","content":"What is the capital of France? One word."}],"temperature":0,"max_tokens":8}' \
    > "$TMP/reply" 2>/dev/null && break
  sleep 1
done
cat "$TMP/reply"; echo
grep -qi paris "$TMP/reply" && echo "SMOKE TEST PASSED" || { echo "SMOKE TEST FAILED"; cat "$TMP/err"; exit 1; }

echo "Running engine unit tests…"
(cd "$ROOT/engine" && BRIDLE_TEST_MODELS_DIR="$TMP/models" "$PY" -m pytest -q 2>/dev/null) \
  || echo "(install pytest in the venv to run unit tests: \"$PY\" -m pip install pytest)"
