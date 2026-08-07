#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TEST_ROOT="$ROOT/test"
RESULTS_ROOT="$TEST_ROOT/test-results"
MODE="${1:-standard}"
PYTEST_BIN="${MYCLAW_TEST_PYTEST:-$TEST_ROOT/.venv/bin/pytest}"
COMMIT_SHA="${MYCLAW_ACCEPTANCE_COMMIT_SHA:-$(git -C "$ROOT" rev-parse HEAD)}"
COMPOSE_PROJECT_NAME="myclaw-ci-${COMMIT_SHA:0:10}"
export COMPOSE_PROJECT_NAME

compose() {
  docker compose -f "$TEST_ROOT/docker-compose.test.yml" "$@"
}

sanitize_stack_log() {
  local target="$1"
  compose --profile fullstack logs --no-color 2>&1 \
    | sed -E \
        -e 's/sk-[A-Za-z0-9_-]{20,}/[REDACTED_API_KEY]/g' \
        -e 's/(Authorization:[[:space:]]*Bearer[[:space:]]+)[^[:space:]]+/\1[REDACTED]/Ig' \
    > "$target"
}

cleanup_stack() {
  compose --profile fullstack down --volumes --remove-orphans >/dev/null 2>&1 || true
}

require_clean_fixed_commit() {
  local actual
  actual="$(git -C "$ROOT" rev-parse HEAD)"
  test "$actual" = "$COMMIT_SHA" || {
    echo "Expected commit $COMMIT_SHA, but workspace is at $actual" >&2
    exit 1
  }
  git -C "$ROOT" diff --quiet
  git -C "$ROOT" diff --cached --quiet
}

require_runner() {
  command -v docker >/dev/null
  docker info >/dev/null
  test -x "$PYTEST_BIN" || {
    echo "Missing pytest executable at $PYTEST_BIN" >&2
    exit 1
  }
}

wait_for_mysql() {
  local container_id="$1"
  test -n "$container_id"
  for _ in $(seq 1 60); do
    if [ "$(docker inspect --format '{{.State.Health.Status}}' "$container_id" 2>/dev/null || true)" = "healthy" ]; then
      return 0
    fi
    sleep 2
  done
  compose logs --no-color mysql-test >&2 || true
  echo "MySQL did not become healthy" >&2
  return 1
}

run_unit_gate() {
  echo "==> Unit and critical branch coverage"
  (
    cd "$TEST_ROOT"
    PYTHONPATH="$ROOT/backend" "$PYTEST_BIN" unit/backend -m unit -v --tb=short \
      --junitxml="$RESULTS_ROOT/unit.xml" \
      --cov=app.workers.runtime_worker \
      --cov=app.services.group_collaboration_service \
      --cov=app.api.agents \
      --cov=app.api.groups \
      --cov-branch \
      --cov-report=term \
      --cov-report="xml:$RESULTS_ROOT/critical-coverage.xml" \
      --cov-fail-under=30
  )
}

run_frontend_gate() {
  echo "==> Frozen frontend production build"
  docker build \
    --label "org.opencontainers.image.revision=$COMMIT_SHA" \
    --tag "myclaw-frontend-ci:${COMMIT_SHA:0:12}" \
    "$ROOT/frontend"
}

run_migration_gate() {
  local backend_image="myclaw-backend-ci:${COMMIT_SHA:0:12}"
  local mysql_container="${COMPOSE_PROJECT_NAME}-migration-mysql"
  local database_url="mysql+aiomysql://myclaw:mypassword@${mysql_container}:3306/myclaw_test"
  echo "==> Empty MySQL migration and single-head verification"
  cleanup_stack
  docker build \
    --label "org.opencontainers.image.revision=$COMMIT_SHA" \
    --tag "$backend_image" \
    "$ROOT/backend"
  compose run -d --no-deps --name "$mysql_container" mysql-test >/dev/null
  wait_for_mysql "$mysql_container"
  docker run --rm \
    --network "${COMPOSE_PROJECT_NAME}_default" \
    --env DATABASE_URL="$database_url" \
    "$backend_image" alembic upgrade head
  docker run --rm \
    --network "${COMPOSE_PROJECT_NAME}_default" \
    --env DATABASE_URL="$database_url" \
    "$backend_image" alembic current | tee "$RESULTS_ROOT/migration-current.txt"
  docker run --rm \
    --network "${COMPOSE_PROJECT_NAME}_default" \
    --env DATABASE_URL="$database_url" \
    "$backend_image" alembic heads | tee "$RESULTS_ROOT/migration-heads.txt"
  test "$(grep -cve '^[[:space:]]*$' "$RESULTS_ROOT/migration-heads.txt")" = "1"
  docker exec "$mysql_container" mysql -umyclaw -pmypassword myclaw_test -N -e "
    SELECT CONCAT('legacy_tables=', COUNT(*))
      FROM information_schema.TABLES
     WHERE TABLE_SCHEMA='myclaw_test'
       AND TABLE_NAME IN (
         'plan_execution','plan_work_item_node','plan_work_item_dependency',
         'plan_declared_agent','group_formation_proposal','group_formation_revision',
         'group_formation_participant','group_formation_work_item',
         'group_formation_suggested_edge','group_agent_node',
         'group_initial_suggested_edge','group_run_agent_version',
         'group_collaboration_edge'
       );
    SELECT CONCAT('legacy_run_columns=', COUNT(*))
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA='myclaw_test' AND TABLE_NAME='run'
       AND COLUMN_NAME IN ('lifecycle','outcome','execution_kind');
    SELECT CONCAT('archive_seal_triggers=', COUNT(*))
      FROM information_schema.TRIGGERS
     WHERE TRIGGER_SCHEMA='myclaw_test'
       AND EVENT_OBJECT_TABLE='legacy_uat_fact_archive';
  " | tee "$RESULTS_ROOT/schema-contract.txt"
  grep -qx 'legacy_tables=0' "$RESULTS_ROOT/schema-contract.txt"
  grep -qx 'legacy_run_columns=0' "$RESULTS_ROOT/schema-contract.txt"
  grep -qx 'archive_seal_triggers=3' "$RESULTS_ROOT/schema-contract.txt"
  cleanup_stack
}

run_fullstack_gate() {
  local report_name="$1"
  cleanup_stack
  trap 'sanitize_stack_log "$RESULTS_ROOT/fullstack-failure.log" || true; cleanup_stack' ERR
  (
    cd "$TEST_ROOT"
    MYCLAW_TEST_PYTEST="$PYTEST_BIN" \
      "$TEST_ROOT/scripts/run_fullstack.sh" \
      --junitxml="$RESULTS_ROOT/$report_name"
  )
  trap - ERR
  cleanup_stack
}

mkdir -p "$RESULTS_ROOT"
require_clean_fixed_commit
require_runner
trap cleanup_stack EXIT

case "$MODE" in
  standard)
    run_unit_gate
    run_frontend_gate
    run_migration_gate
    echo "==> Two Runtime Worker Mock Docker full-stack"
    export MYCLAW_LLM_MODE=mock
    export MYCLAW_RUNTIME_WORKER_REPLICAS=2
    run_fullstack_gate mock-fullstack.xml
    ;;
  real)
    : "${MYCLAW_REAL_LLM_API_KEY:?MYCLAW_REAL_LLM_API_KEY must be injected as a secret}"
    : "${MYCLAW_REAL_LLM_ENDPOINT:?MYCLAW_REAL_LLM_ENDPOINT is required}"
    : "${MYCLAW_REAL_LLM_MODEL:?MYCLAW_REAL_LLM_MODEL is required}"
    echo "==> Real-model Docker API and Chromium acceptance"
    export MYCLAW_LLM_MODE=real
    export MYCLAW_RUNTIME_WORKER_REPLICAS=2
    run_fullstack_gate real-model-fullstack.xml
    ;;
  *)
    echo "Usage: $0 {standard|real}" >&2
    exit 2
    ;;
esac

echo "All $MODE CI gates passed for $COMMIT_SHA."
