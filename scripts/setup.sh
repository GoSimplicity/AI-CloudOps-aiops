#!/usr/bin/env bash
set -euo pipefail

# Minimal local setup
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install -U pip
pip install -r requirements.txt

echo "Setup complete. Start the service with: ./scripts/start.sh"
