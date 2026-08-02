#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Building and starting full stack..."
runtime_replicas="${MYCLAW_RUNTIME_WORKER_REPLICAS:-2}"
docker compose -f docker-compose.test.yml --profile fullstack up -d --build \
  --scale runtime-worker-test="$runtime_replicas"

echo "==> Waiting for all services..."
"$ROOT/scripts/wait_for_stack.sh"

export MYCLAW_FULLSTACK_API_URL="${MYCLAW_FULLSTACK_API_URL:-http://localhost:8001}"
export MYCLAW_FULLSTACK_UI_URL="${MYCLAW_FULLSTACK_UI_URL:-http://localhost:8081}"

if [ "${MYCLAW_LLM_MODE:-mock}" = "real" ]; then
  : "${MYCLAW_REAL_LLM_API_KEY:?MYCLAW_REAL_LLM_API_KEY is required for real-model acceptance}"
  : "${MYCLAW_REAL_LLM_ENDPOINT:?MYCLAW_REAL_LLM_ENDPOINT is required for real-model acceptance}"
  : "${MYCLAW_REAL_LLM_MODEL:?MYCLAW_REAL_LLM_MODEL is required for real-model acceptance}"
fi

pytest_bin="${MYCLAW_TEST_PYTEST:-$ROOT/.venv/bin/pytest}"
if [ ! -x "$pytest_bin" ]; then
  echo "Missing pytest executable at $pytest_bin; install test/requirements.txt or set MYCLAW_TEST_PYTEST."
  exit 1
fi

echo "==> Full-stack acceptance (real API, workers, database, sandbox and frontend)..."
"$pytest_bin" e2e/fullstack/ -m fullstack -v --tb=short "$@"

echo ""
echo "All full-stack tests passed."
