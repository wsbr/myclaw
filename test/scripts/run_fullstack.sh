#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

docker_host_addr="${MYCLAW_DOCKER_HOST_ADDR:-localhost}"
export MYCLAW_FULLSTACK_API_URL="${MYCLAW_FULLSTACK_API_URL:-http://${docker_host_addr}:8001}"
export MYCLAW_FULLSTACK_UI_URL="${MYCLAW_FULLSTACK_UI_URL:-http://${docker_host_addr}:8081}"

: "${MYCLAW_REAL_LLM_API_KEY:?MYCLAW_REAL_LLM_API_KEY is required for real-model acceptance}"
: "${MYCLAW_REAL_LLM_ENDPOINT:?MYCLAW_REAL_LLM_ENDPOINT is required for real-model acceptance}"
: "${MYCLAW_REAL_LLM_MODEL:?MYCLAW_REAL_LLM_MODEL is required for real-model acceptance}"
fullstack_scope="${MYCLAW_FULLSTACK_SCOPE:-full}"
if [ "$fullstack_scope" = "full" ]; then
  export MYCLAW_REAL_SEARCH_PROVIDER="${MYCLAW_REAL_SEARCH_PROVIDER:-bocha_brave}"
  case "$MYCLAW_REAL_SEARCH_PROVIDER" in
    bailian)
      : "${MYCLAW_BAILIAN_API_KEY:?MYCLAW_BAILIAN_API_KEY is required for Bailian real Search acceptance}"
      ;;
    bocha_brave)
      : "${MYCLAW_BOCHA_API_KEY:?MYCLAW_BOCHA_API_KEY is required for real Search acceptance}"
      : "${MYCLAW_BRAVE_API_KEY:?MYCLAW_BRAVE_API_KEY is required for real Search acceptance}"
      ;;
    *)
      echo "MYCLAW_REAL_SEARCH_PROVIDER must be bailian or bocha_brave" >&2
      exit 2
      ;;
  esac
elif [ "$fullstack_scope" != "minimal" ]; then
  echo "MYCLAW_FULLSTACK_SCOPE must be full or minimal" >&2
  exit 2
fi

echo "==> Building and starting full stack..."
runtime_replicas="${MYCLAW_RUNTIME_WORKER_REPLICAS:-2}"
docker compose -f docker-compose.test.yml --profile fullstack up -d --build \
  --scale runtime-worker-test="$runtime_replicas"

echo "==> Waiting for all services..."
"$ROOT/scripts/wait_for_stack.sh"

pytest_bin="${MYCLAW_TEST_PYTEST:-$ROOT/.venv/bin/pytest}"
if [ ! -x "$pytest_bin" ]; then
  echo "Missing pytest executable at $pytest_bin; install test/requirements.txt or set MYCLAW_TEST_PYTEST."
  exit 1
fi

echo "==> Full-stack acceptance (real API, workers, database, sandbox and frontend)..."
if [ "$fullstack_scope" = "minimal" ]; then
  export MYCLAW_BROWSER_EVIDENCE_DIR="${MYCLAW_BROWSER_EVIDENCE_DIR:-$ROOT/test-results/minimal-browser}"
  "$pytest_bin" \
    e2e/fullstack/test_platform_tool_v3_real.py::test_chat_working_plan_finishes_after_report_artifact_is_published \
    e2e/fullstack/test_skill_runtime_real.py::test_skill_model_progressive_reads_manifest_and_reference \
    e2e/fullstack/test_history_memory_real.py::test_long_session_summary_is_ready_and_used_by_next_runner \
    e2e/fullstack/test_history_memory_real.py::test_explicit_user_fact_is_extracted_and_recalled_with_user_source_only \
    e2e/fullstack/test_skill_ui_real.py::test_skill_markdown_view_edit_preview_creates_immutable_version \
    -m fullstack -v --tb=short "$@"
else
  "$pytest_bin" e2e/fullstack/ -m fullstack -v --tb=short "$@"
fi

echo ""
echo "All full-stack tests passed."
