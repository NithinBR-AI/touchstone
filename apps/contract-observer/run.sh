#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  source "$REPO_ROOT/.env"
  set +a
fi

PORT="${CONTRACT_OBSERVER_PORT:-8002}"

export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:$PYTHONPATH}"

PYTHON="${REPO_ROOT}/.venv/Scripts/python.exe"
if [ ! -f "$PYTHON" ]; then
  PYTHON="python"
fi

cd "$SCRIPT_DIR"

echo "Starting Contract Observer on http://localhost:${PORT}"
exec "$PYTHON" -m uvicorn main:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --reload
