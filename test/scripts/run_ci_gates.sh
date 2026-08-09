#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TEST_ROOT="$ROOT/test"
RESULTS_ROOT="$TEST_ROOT/test-results"
MODE="${1:-standard}"
PYTEST_BIN="${MYCLAW_TEST_PYTEST:-$TEST_ROOT/.venv/bin/pytest}"
COMMIT_SHA="${MYCLAW_ACCEPTANCE_COMMIT_SHA:-$(git -C "$ROOT" rev-parse HEAD)}"
COMPOSE_PROJECT_NAME="myclaw-ci-${COMMIT_SHA:0:10}"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
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

wait_for_mysql_network() {
  local backend_image="$1"
  local mysql_host="$2"
  for _ in $(seq 1 30); do
    if docker run --rm \
      --network "${COMPOSE_PROJECT_NAME}_default" \
      "$backend_image" \
      python -c "import socket; connection = socket.create_connection(('${mysql_host}', 3306), 3); connection.close()"; then
      return 0
    fi
    sleep 1
  done
  echo "MySQL was healthy but not reachable from the Compose network" >&2
  return 1
}

run_unit_gate() {
  echo "==> Pure Tool contract and security gates (no model surrogate)"
  (
    cd "$TEST_ROOT"
    PYTHONPATH="$ROOT/backend" "$PYTEST_BIN" \
      unit/backend \
      -m unit -v --tb=short --junitxml="$RESULTS_ROOT/tool-contracts.xml"
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
  compose run -d --service-ports --no-deps --name "$mysql_container" mysql-test >/dev/null
  wait_for_mysql "$mysql_container"
  wait_for_mysql_network "$backend_image" "$mysql_container"
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
    SELECT CONCAT('tool_contract_tables=', COUNT(*))
      FROM information_schema.TABLES
     WHERE TABLE_SCHEMA='myclaw_test' AND TABLE_NAME IN (
       'platform_secret','platform_tool','tool_version','tool_connection',
       'tool_connection_revision','tool_governance','browser_session_state');
    SELECT CONCAT('legacy_tool_columns=', COUNT(*))
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA='myclaw_test' AND TABLE_NAME='platform_tool'
       AND COLUMN_NAME IN (
         'input_schema','output_schema','implementation_ref','execution_mode',
         'enabled','confirmation_policy','platform_config','registration_hash');
    SELECT CONCAT('immutable_tool_triggers=', COUNT(*))
      FROM information_schema.TRIGGERS
     WHERE TRIGGER_SCHEMA='myclaw_test' AND TRIGGER_NAME IN (
       'trg_tool_version_immutable_update','trg_tool_version_immutable_delete',
       'trg_tool_connection_revision_immutable_update','trg_tool_connection_revision_immutable_delete',
       'trg_platform_secret_material_immutable','trg_agent_version_tool_tool_binding_immutable',
       'trg_tool_invocation_tool_binding_immutable','trg_runner_tool_snapshot_tool_binding_immutable');
    SELECT CONCAT('exact_nonnull_columns=', COUNT(*))
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA='myclaw_test' AND IS_NULLABLE='NO' AND (
       (TABLE_NAME='tool_invocation' AND COLUMN_NAME IN ('tool_version_id','tool_version_content_hash')) OR
       (TABLE_NAME='runner_tool_snapshot' AND COLUMN_NAME IN ('tool_version_id','tool_version_content_hash')));
    SELECT CONCAT('immutable_skill_triggers=', COUNT(*))
      FROM information_schema.TRIGGERS
     WHERE TRIGGER_SCHEMA='myclaw_test' AND TRIGGER_NAME IN (
       'trg_skill_version_immutable_update','trg_skill_version_immutable_delete');
    SELECT CONCAT('skill_runtime_columns=', COUNT(*))
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA='myclaw_test' AND TABLE_NAME='skill_version'
       AND COLUMN_NAME IN ('manifest','content_hash','package_size','archive_type');
  " | tee "$RESULTS_ROOT/schema-contract.txt"
  grep -qx 'tool_contract_tables=7' "$RESULTS_ROOT/schema-contract.txt"
  grep -qx 'legacy_tool_columns=0' "$RESULTS_ROOT/schema-contract.txt"
  grep -qx 'immutable_tool_triggers=8' "$RESULTS_ROOT/schema-contract.txt"
  grep -qx 'exact_nonnull_columns=4' "$RESULTS_ROOT/schema-contract.txt"
  grep -qx 'immutable_skill_triggers=2' "$RESULTS_ROOT/schema-contract.txt"
  grep -qx 'skill_runtime_columns=4' "$RESULTS_ROOT/schema-contract.txt"
  echo "==> Real MySQL Tool contract and trigger verification"
  (
    cd "$TEST_ROOT"
    PYTHONPATH="$ROOT/backend" \
      MYCLAW_V3_ACCEPTANCE_DATABASE_URL="mysql+aiomysql://myclaw:mypassword@127.0.0.1:3307/myclaw_test" \
      "$PYTEST_BIN" \
        integration/test_platform_tool_v3_mysql.py \
        integration/test_skill_runtime_mysql.py \
        -m integration -v --tb=short \
      --junitxml="$RESULTS_ROOT/mysql-tool-contracts.xml"
  )
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
browser_checksum_file=""
require_clean_fixed_commit
require_runner
trap cleanup_stack EXIT

case "$MODE" in
  standard)
    run_unit_gate
    run_frontend_gate
    run_migration_gate
    ;;
  real)
    : "${MYCLAW_REAL_LLM_API_KEY:?MYCLAW_REAL_LLM_API_KEY must be injected as a secret}"
    : "${MYCLAW_REAL_LLM_ENDPOINT:?MYCLAW_REAL_LLM_ENDPOINT is required}"
    : "${MYCLAW_REAL_LLM_MODEL:?MYCLAW_REAL_LLM_MODEL is required}"
    export MYCLAW_REAL_SEARCH_PROVIDER="${MYCLAW_REAL_SEARCH_PROVIDER:-bocha_brave}"
    case "$MYCLAW_REAL_SEARCH_PROVIDER" in
      bailian)
        : "${MYCLAW_BAILIAN_API_KEY:?MYCLAW_BAILIAN_API_KEY must be injected as a secret}"
        ;;
      bocha_brave)
        : "${MYCLAW_BOCHA_API_KEY:?MYCLAW_BOCHA_API_KEY must be injected as a secret}"
        : "${MYCLAW_BRAVE_API_KEY:?MYCLAW_BRAVE_API_KEY must be injected as a secret}"
        ;;
      *)
        echo "MYCLAW_REAL_SEARCH_PROVIDER must be bailian or bocha_brave" >&2
        exit 2
        ;;
    esac
    echo "==> Real-model Docker API and Chromium acceptance"
    export MYCLAW_RUNTIME_WORKER_REPLICAS=2
    rm -rf "$RESULTS_ROOT/real-browser"
    export MYCLAW_BROWSER_EVIDENCE_DIR="$RESULTS_ROOT/real-browser"
    run_fullstack_gate real-model-fullstack.xml
    find "$RESULTS_ROOT/real-browser" -type f -name '*.png' -print0 \
      | sort -z \
      | xargs -0 shasum -a 256 \
      > "$RESULTS_ROOT/real-browser-checksums.txt"
    test -s "$RESULTS_ROOT/real-browser-checksums.txt"
    browser_checksum_file="$RESULTS_ROOT/real-browser-checksums.txt"
    ;;
  minimal-real)
    : "${MYCLAW_REAL_LLM_API_KEY:?MYCLAW_REAL_LLM_API_KEY must be injected as a secret}"
    : "${MYCLAW_REAL_LLM_ENDPOINT:?MYCLAW_REAL_LLM_ENDPOINT is required}"
    : "${MYCLAW_REAL_LLM_MODEL:?MYCLAW_REAL_LLM_MODEL is required}"
    echo "==> Minimal real-model LLM, Skill, maintenance and Chromium acceptance"
    export MYCLAW_RUNTIME_WORKER_REPLICAS=2
    export MYCLAW_FULLSTACK_SCOPE=minimal
    rm -rf "$RESULTS_ROOT/minimal-browser"
    export MYCLAW_BROWSER_EVIDENCE_DIR="$RESULTS_ROOT/minimal-browser"
    run_fullstack_gate minimal-real-fullstack.xml
    find "$RESULTS_ROOT/minimal-browser" -type f -name '*.png' -print0 \
      | sort -z \
      | xargs -0 shasum -a 256 \
      > "$RESULTS_ROOT/minimal-browser-checksums.txt"
    test -s "$RESULTS_ROOT/minimal-browser-checksums.txt"
    browser_checksum_file="$RESULTS_ROOT/minimal-browser-checksums.txt"
    ;;
  *)
    echo "Usage: $0 {standard|minimal-real|real}" >&2
    exit 2
    ;;
esac

browser_checksum_manifest_sha=""
if [ -n "$browser_checksum_file" ] && [ -s "$browser_checksum_file" ]; then
  browser_checksum_manifest_sha="$(shasum -a 256 "$browser_checksum_file" | awk '{print $1}')"
fi
printf '%s\n' \
  "{\"schema\":\"myclaw.ci_gate.v1\",\"mode\":\"${MODE}\",\"status\":\"passed\",\"commit_sha\":\"${COMMIT_SHA}\",\"started_at\":\"${STARTED_AT}\",\"completed_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"search_provider\":\"${MYCLAW_REAL_SEARCH_PROVIDER:-}\",\"browser_checksum_manifest_sha256\":\"${browser_checksum_manifest_sha}\"}" \
  > "${RESULTS_ROOT}/${MODE}-gates.json"
echo "All $MODE CI gates passed for $COMMIT_SHA."
