#!/usr/bin/env bash
set -euo pipefail

# Minimal start script
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

# shellcheck disable=SC1091
source .venv/bin/activate 2>/dev/null || true

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"

exec uvicorn app.main:create_app --factory --host "$HOST" --port "$PORT"