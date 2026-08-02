#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -x "$ROOT/.venv/bin/pytest" ]; then
  echo "Missing test/.venv; create it and install test/requirements.txt first."
  exit 1
fi

PYTHONPATH="$ROOT/../backend" "$ROOT/.venv/bin/pytest" unit/backend/ -v -m unit "$@"
