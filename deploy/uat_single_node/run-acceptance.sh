#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${script_dir}/../.." && pwd)"
env_file="${script_dir}/.env"
compose_file="${script_dir}/docker-compose.yml"
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
# Shell sourcing removes the JSON string's inner quotes.  The Compose parser
# does not, so restore the exact JSON value for local pytest imports.
export CORS_ORIGINS='["http://localhost:8281","http://127.0.0.1:8281"]'
actual_sha="$(git -C "${root}" rev-parse HEAD)"
[[ "${MYCLAW_ACCEPTANCE_COMMIT_SHA}" == "${actual_sha}" ]] || {
  echo "UAT SHA does not match the clean workspace" >&2
  exit 2
}

evidence_dir="${script_dir}/evidence/${actual_sha}"
mkdir -p "${evidence_dir}"
standard_gate="${root}/test/test-results/standard-gates.json"
STANDARD_GATE="${standard_gate}" EXPECTED_SHA="${actual_sha}" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["STANDARD_GATE"])
if not path.is_file():
    raise SystemExit("standard CI gate marker is missing")
payload = json.loads(path.read_text(encoding="utf-8"))
if payload.get("status") != "passed" or payload.get("commit_sha") != os.environ["EXPECTED_SHA"]:
    raise SystemExit("standard CI gate marker does not match the acceptance SHA")
PY
standard_evidence="${evidence_dir}/standard-gates"
mkdir -p "${standard_evidence}"
for artifact in \
  standard-gates.json unit.xml critical-coverage.xml migration-current.txt \
  migration-heads.txt schema-contract.txt mysql-integration.xml mock-fullstack.xml; do
  [[ -s "${root}/test/test-results/${artifact}" ]] || {
    echo "Missing standard gate evidence: ${artifact}" >&2
    exit 2
  }
  cp "${root}/test/test-results/${artifact}" "${standard_evidence}/${artifact}"
done
export MYCLAW_LLM_MODE=real
export MYCLAW_FULLSTACK_API_URL="http://127.0.0.1:${UAT_API_PORT}"
export MYCLAW_FULLSTACK_UI_URL="http://127.0.0.1:${UAT_UI_PORT}"
export MYCLAW_TEST_ADMIN_EMAIL="${INIT_ADMIN_EMAIL}"
export MYCLAW_TEST_ADMIN_PASSWORD="${INIT_ADMIN_PASSWORD}"
export MYCLAW_GOAL1_MYSQL_HOST=127.0.0.1
export MYCLAW_GOAL1_MYSQL_PORT="${UAT_MYSQL_PORT}"
export MYCLAW_GOAL1_MYSQL_USER="${MYSQL_USER}"
export MYCLAW_GOAL1_MYSQL_PASSWORD="${MYSQL_PASSWORD}"
export MYCLAW_GOAL1_MYSQL_DATABASE="${MYSQL_DATABASE}"
export MYCLAW_GOAL1_SMOKE_EVIDENCE="${evidence_dir}/agent-native-smoke.json"
export MYCLAW_COMPOSE_PROJECT=myclaw-uat-single-node
export MYCLAW_COMPOSE_FILE="${compose_file}"
export MYCLAW_COMPOSE_ENV_FILE="${env_file}"
export MYCLAW_RUNTIME_WORKER_SERVICE=runtime-worker
export MYCLAW_SANDBOX_WORKER_SERVICE=sandbox-worker

curl --fail --silent --show-error "${MYCLAW_FULLSTACK_API_URL}/health" >/dev/null
curl --fail --silent --show-error "${MYCLAW_FULLSTACK_UI_URL}/healthz" >/dev/null

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
set +e
(
  cd "${root}/test"
  "${pytest_bin}" e2e/fullstack -m fullstack -v --tb=short \
    --junitxml="${evidence_dir}/real-model-fullstack.xml"
)
pytest_status=$?
set -e
docker compose --env-file "${env_file}" -f "${compose_file}" logs --no-color 2>&1 \
  | sed -E \
      -e 's/sk-[A-Za-z0-9_-]{20,}/[REDACTED_API_KEY]/g' \
      -e 's/(Authorization:[[:space:]]*Bearer[[:space:]]+)[^[:space:]]+/\1[REDACTED]/Ig' \
  > "${evidence_dir}/uat-stack.log"
[[ ${pytest_status} -eq 0 ]] || exit "${pytest_status}"

"${pytest_bin%/pytest}/python" "${root}/test/scripts/collect_uat_evidence.py" \
  "${evidence_dir}/metrics.json"
docker run --rm \
  --mount type=volume,src=myclaw-uat-single-node-app-data,dst=/source,readonly \
  --mount type=bind,src="${evidence_dir}",dst=/evidence \
  alpine:3.20 sh -c \
  'test -s /source/otel/uat-otel-traces.jsonl && cp /source/otel/uat-otel-traces.jsonl /evidence/otel-traces.jsonl'
docker run --rm \
  --mount type=volume,src=myclaw-uat-single-node-app-data,dst=/source,readonly \
  --mount type=bind,src="${evidence_dir}",dst=/evidence \
  alpine:3.20 sh -eu -c '
    cd /source
    find . -type f -path "*/.myclaw/artifacts/*" | sort > /tmp/artifacts.list
    test "$(wc -l < /tmp/artifacts.list)" -ge 4
    tar -czf /evidence/artifacts.tar.gz -T /tmp/artifacts.list
    while IFS= read -r path; do sha256sum "$path"; done < /tmp/artifacts.list \
      > /evidence/artifacts.files.sha256
    test -s /evidence/artifacts.files.sha256
  '
shasum -a 256 "${evidence_dir}/artifacts.tar.gz" \
  > "${evidence_dir}/artifacts.tar.gz.sha256"
git -C "${root}" rev-parse HEAD > "${evidence_dir}/commit-sha.txt"
git -C "${root}" status --porcelain=v1 > "${evidence_dir}/git-status.txt"
[[ ! -s "${evidence_dir}/git-status.txt" ]]
docker compose --env-file "${env_file}" -f "${compose_file}" images --format json \
  > "${evidence_dir}/compose-images.jsonl"
docker compose --env-file "${env_file}" -f "${compose_file}" ps --format json \
  > "${evidence_dir}/compose-ps.jsonl"
shasum -a 256 "${root}/docs/technical/openapi-v1.yaml" \
  > "${evidence_dir}/openapi-v1.sha256"
printf '{"started_at":"%s","completed_at":"%s","status":"passed","commit_sha":"%s"}\n' \
  "${started_at}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${actual_sha}" \
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

echo "Real-model UAT passed for ${actual_sha}; evidence: ${evidence_dir}"
