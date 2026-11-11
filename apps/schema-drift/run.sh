#!/usr/bin/env bash
# Start the Schema Drift app.
# Run from the repo root or any directory — script resolves paths.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  source "$REPO_ROOT/.env"
  set +a
fi

PORT="${SCHEMA_DRIFT_PORT:-8001}"

export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

cd "$SCRIPT_DIR"

echo "Starting Schema Drift on http://localhost:${PORT}"
exec python -m uvicorn main:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --reload \
  --reload-dir "$REPO_ROOT"
