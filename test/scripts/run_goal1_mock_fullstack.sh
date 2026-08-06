#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TEST_ROOT="$ROOT/test"
COMMIT_SHA="${MYCLAW_ACCEPTANCE_COMMIT_SHA:-$(git -C "$ROOT" rev-parse HEAD)}"
PROJECT_NAME="${MYCLAW_GOAL1_PROJECT_NAME:-myclaw-v2-goal1-${COMMIT_SHA:0:12}}"
EVIDENCE_ROOT="${MYCLAW_GOAL1_EVIDENCE_ROOT:-$TEST_ROOT/test-results/agent-native-v2-goal1/$COMMIT_SHA}"
PYTEST_BIN="${MYCLAW_TEST_PYTEST:-$TEST_ROOT/.venv/bin/pytest}"

export COMPOSE_PROJECT_NAME="$PROJECT_NAME"
export MYCLAW_ACCEPTANCE_COMMIT_SHA="$COMMIT_SHA"
export MYCLAW_FULLSTACK_API_URL="${MYCLAW_FULLSTACK_API_URL:-http://127.0.0.1:8001}"
export MYCLAW_FULLSTACK_UI_URL="${MYCLAW_FULLSTACK_UI_URL:-http://127.0.0.1:8081}"
export MYCLAW_MOCK_LLM_URL="${MYCLAW_MOCK_LLM_URL:-http://127.0.0.1:9090}"
export MYCLAW_GOAL1_SMOKE_EVIDENCE="$EVIDENCE_ROOT/fullstack-smoke.json"

actual_sha="$(git -C "$ROOT" rev-parse HEAD)"
test "$actual_sha" = "$COMMIT_SHA" || {
  echo "Expected $COMMIT_SHA but HEAD is $actual_sha" >&2
  exit 1
}
test -z "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" || {
  echo "Goal 1 acceptance requires a clean worktree" >&2
  exit 1
}
test -x "$PYTEST_BIN"
mkdir -p "$EVIDENCE_ROOT"

cd "$TEST_ROOT"
docker compose -p test -f docker-compose.test.yml --profile fullstack down --remove-orphans >/dev/null 2>&1 || true
docker compose -f docker-compose.test.yml --profile fullstack up -d --build --force-recreate \
  --scale runtime-worker-test=2
"$TEST_ROOT/scripts/wait_for_stack.sh"

"$PYTEST_BIN" e2e/fullstack/test_agent_native_v2_goal1.py \
  -m fullstack -v --tb=short --junitxml="$EVIDENCE_ROOT/mock-fullstack.xml"

docker compose -f docker-compose.test.yml exec -T api-test alembic current \
  > "$EVIDENCE_ROOT/schema-head.txt"
docker compose -f docker-compose.test.yml --profile fullstack logs --no-color 2>&1 \
  | sed -E \
      -e 's/sk-[A-Za-z0-9_-]{16,}/[REDACTED_API_KEY]/g' \
      -e 's/(Authorization:[[:space:]]*Bearer[[:space:]]+)[^[:space:]]+/\1[REDACTED]/Ig' \
  > "$EVIDENCE_ROOT/logs-sanitized.txt"

echo "$PROJECT_NAME"
echo "$EVIDENCE_ROOT"
