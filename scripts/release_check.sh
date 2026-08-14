#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

echo "==> [1/8] Repository secret and history scan"
./scripts/check_repository_secrets.sh --history

echo "==> [2/8] Documentation links"
./scripts/check_documentation.sh

echo "==> [3/8] Patch hygiene"
git diff --check

echo "==> [4/8] Python compilation"
test -x test/.venv/bin/python || {
  echo "Missing test/.venv; run 'make test-env' first." >&2
  exit 1
}
PYTHONPATH=backend test/.venv/bin/python -m compileall -q backend/app backend/migrations test/unit/backend

echo "==> [5/8] Alembic revision graph"
(
  cd backend
  ../test/.venv/bin/alembic -c alembic.ini heads
)

echo "==> [6/8] Backend deterministic unit tests"
PYTHONPATH=backend test/.venv/bin/pytest -q test/unit/backend

echo "==> [7/8] Frontend production build"
./scripts/run_frontend_build.sh

echo "==> [8/8] Docker Compose configurations"
./scripts/check_compose_configs.sh

echo "Release content gate passed for v$(tr -d '\n' < VERSION)."
