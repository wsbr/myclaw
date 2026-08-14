#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

echo "==> [1/6] Repository secret and history scan"
./scripts/check_repository_secrets.sh --history

echo "==> [2/6] Patch hygiene"
git diff --check

echo "==> [3/6] Python compilation"
test -x test/.venv/bin/python || {
  echo "Missing test/.venv; run 'make test-env' first." >&2
  exit 1
}
PYTHONPATH=backend test/.venv/bin/python -m compileall -q backend/app test/unit/backend

echo "==> [4/6] Backend deterministic unit tests"
PYTHONPATH=backend test/.venv/bin/pytest -q test/unit/backend

echo "==> [5/6] Frontend production build"
./scripts/run_frontend_build.sh

echo "==> [6/6] Docker Compose configurations"
./scripts/check_compose_configs.sh

echo "Release content gate passed for v$(tr -d '\n' < VERSION)."
