#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${script_dir}/../.." && pwd)"
env_file="${script_dir}/.env"
pytest_bin="${root}/test/.venv/bin/pytest"

[[ -r "${env_file}" ]] || { echo "Missing ${env_file}" >&2; exit 2; }
[[ -x "${pytest_bin}" ]] || { echo "Missing ${pytest_bin}" >&2; exit 2; }
if ! git -C "${root}" diff --quiet || ! git -C "${root}" diff --cached --quiet; then
  echo "Refusing to accept a dirty workspace" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a
: "${MYCLAW_REAL_LLM_API_KEY:?real model key is required}"
: "${MYCLAW_REAL_LLM_ENDPOINT:?real model endpoint is required}"
: "${MYCLAW_REAL_LLM_MODEL:?real model id is required}"

actual_sha="$(git -C "${root}" rev-parse HEAD)"
[[ "${MYCLAW_ACCEPTANCE_COMMIT_SHA}" == "${actual_sha}" ]] || {
  echo "UAT SHA does not match the clean workspace" >&2
  exit 2
}

standard_gate="${root}/test/test-results/standard-gates.json"
STANDARD_GATE="${standard_gate}" EXPECTED_SHA="${actual_sha}" python3 - <<'PY'
import json
import os
from pathlib import Path

payload = json.loads(Path(os.environ["STANDARD_GATE"]).read_text(encoding="utf-8"))
if payload.get("status") != "passed" or payload.get("commit_sha") != os.environ["EXPECTED_SHA"]:
    raise SystemExit("standard CI gate record does not match the acceptance SHA")
PY

export MYCLAW_FULLSTACK_API_URL="http://127.0.0.1:${UAT_API_PORT}"
export MYCLAW_FULLSTACK_UI_URL="http://127.0.0.1:${UAT_UI_PORT}"
export MYCLAW_TEST_ADMIN_EMAIL="${INIT_ADMIN_EMAIL}"
export MYCLAW_TEST_ADMIN_PASSWORD="${INIT_ADMIN_PASSWORD}"
export MYCLAW_COMPOSE_PROJECT=myclaw-uat
export MYCLAW_COMPOSE_FILE="${root}/deploy/docker-compose.uat.yml"
export MYCLAW_COMPOSE_ENV_FILE="${env_file}"
export MYCLAW_RUNTIME_WORKER_SERVICE=runtime-worker
export MYCLAW_SANDBOX_WORKER_SERVICE=sandbox-worker
export MYCLAW_BROWSER_WORKER_SERVICE=browser-worker

curl --fail --silent --show-error "${MYCLAW_FULLSTACK_API_URL}/health" >/dev/null
curl --fail --silent --show-error "${MYCLAW_FULLSTACK_UI_URL}/healthz" >/dev/null

evidence_dir="${script_dir}/evidence/${actual_sha}/emergent-group"
mkdir -p "${evidence_dir}"
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
set +e
(
  cd "${root}/test"
  "${pytest_bin}" \
    e2e/fullstack/test_platform_tool_v3_real.py::test_group_real_model_emergence_async_handoff_and_restart \
    -m fullstack -v --tb=short \
    --junitxml="${evidence_dir}/real-model-emergent-group.xml"
)
test_status=$?
set -e

docker compose --project-name myclaw-uat --env-file "${env_file}" \
  -f "${root}/deploy/docker-compose.uat.yml" logs --no-color 2>&1 \
  | sed -E \
      -e 's/sk-[A-Za-z0-9_-]{20,}/[REDACTED_API_KEY]/g' \
      -e 's/(Authorization:[[:space:]]*Bearer[[:space:]]+)[^[:space:]]+/\1[REDACTED]/Ig' \
  > "${evidence_dir}/uat-stack.log"
run_status="passed"
if (( test_status != 0 )); then
  run_status="failed"
fi
printf '{"started_at":"%s","completed_at":"%s","status":"%s","commit_sha":"%s","pytest_exit_code":%s}\n' \
  "${started_at}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${run_status}" "${actual_sha}" "${test_status}" \
  > "${evidence_dir}/acceptance-run.json"

MYCLAW_EVIDENCE_DIR="${evidence_dir}" python3 - <<'PY'
import os
from pathlib import Path

root = Path(os.environ["MYCLAW_EVIDENCE_DIR"])
secret = os.environ["MYCLAW_REAL_LLM_API_KEY"].encode()
for path in root.rglob("*"):
    if path.is_file() and secret and secret in path.read_bytes():
        raise SystemExit(f"real-model credential leaked into evidence: {path}")
PY

if (( test_status != 0 )); then
  echo "Emergent Group real-model UAT failed for ${actual_sha}; evidence: ${evidence_dir}" >&2
  exit "${test_status}"
fi

echo "Emergent Group real-model UAT passed for ${actual_sha}; evidence: ${evidence_dir}"
