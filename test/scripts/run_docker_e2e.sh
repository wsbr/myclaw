#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Starting full Docker E2E stack..."
docker compose -f docker-compose.test.yml --profile docker-e2e up -d --build

echo "==> Waiting for services..."
"$ROOT/scripts/wait_for_services.sh"
sleep 5  # extra time for api/worker startup

export MYCLAW_E2E_URL="${MYCLAW_E2E_URL:-http://localhost:8001}"
export MYCLAW_MOCK_LLM_URL="${MYCLAW_MOCK_LLM_URL:-http://localhost:9090}"

pytest e2e/test_docker_plan_trace.py -v -m docker "$@"
