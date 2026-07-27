#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Building and starting full stack..."
docker compose -f docker-compose.test.yml --profile fullstack up -d --build

echo "==> Waiting for all services..."
"$ROOT/scripts/wait_for_stack.sh"

export MYCLAW_FULLSTACK_API_URL="${MYCLAW_FULLSTACK_API_URL:-http://localhost:8001}"
export MYCLAW_FULLSTACK_UI_URL="${MYCLAW_FULLSTACK_UI_URL:-http://localhost:8081}"
export MYCLAW_MOCK_LLM_INTERNAL_URL="${MYCLAW_MOCK_LLM_INTERNAL_URL:-http://mock-llm:9090}"

source .venv/bin/activate

# Install Playwright browser if needed
python -m playwright install chromium 2>/dev/null || true

echo ""
echo "==> [1/2] Full-stack API tests (real deployment)..."
pytest e2e/fullstack/ -m fullstack -v --tb=short

echo ""
echo "==> [2/2] Full-stack UI tests (Playwright browser)..."
pytest e2e/ui/test_ui_fullstack.py -m fullstack -v --tb=short

echo ""
echo "All full-stack tests passed."
