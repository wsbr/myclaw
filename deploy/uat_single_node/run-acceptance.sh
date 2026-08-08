#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${script_dir}/../.." && pwd)"
env_file="${MYCLAW_UAT_ENV_FILE:-${script_dir}/.env}"
compose_file="${MYCLAW_UAT_COMPOSE_FILE:-${script_dir}/docker-compose.yml}"
compose_project="${MYCLAW_UAT_COMPOSE_PROJECT:-myclaw-uat-single-node}"
app_volume="${MYCLAW_UAT_APP_VOLUME:-myclaw-uat-single-node-app-data}"
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
# Shell sourcing removes the JSON string's inner quotes. The Compose parser
# does not, so restore an exact value matching this deployment's UI port.
export CORS_ORIGINS="[\"http://localhost:${UAT_UI_PORT}\",\"http://127.0.0.1:${UAT_UI_PORT}\"]"
actual_sha="$(git -C "${root}" rev-parse HEAD)"
[[ "${MYCLAW_ACCEPTANCE_COMMIT_SHA}" == "${actual_sha}" ]] || {
  echo "UAT SHA does not match the clean workspace" >&2
  exit 2
}

evidence_root="${MYCLAW_UAT_EVIDENCE_ROOT:-${script_dir}/evidence}"
evidence_dir="${evidence_root}/${actual_sha}"
mkdir -p "${evidence_dir}"
standard_gate="${root}/test/test-results/standard-gates.json"
STANDARD_GATE="${standard_gate}" EXPECTED_SHA="${actual_sha}" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["STANDARD_GATE"])
if not path.is_file():
    raise SystemExit("standard CI gate record is missing")
payload = json.loads(path.read_text(encoding="utf-8"))
if payload.get("status") != "passed" or payload.get("commit_sha") != os.environ["EXPECTED_SHA"]:
    raise SystemExit("standard CI gate record does not match the acceptance SHA")
PY
standard_evidence="${evidence_dir}/standard-gates"
mkdir -p "${standard_evidence}"
for artifact in \
  standard-gates.json tool-contracts.xml migration-current.txt \
  migration-heads.txt schema-contract.txt mysql-tool-contracts.xml; do
  [[ -s "${root}/test/test-results/${artifact}" ]] || {
    echo "Missing standard gate evidence: ${artifact}" >&2
    exit 2
  }
  cp "${root}/test/test-results/${artifact}" "${standard_evidence}/${artifact}"
done
: "${MYCLAW_REAL_LLM_API_KEY:?real model key is required for UAT}"
: "${MYCLAW_REAL_LLM_ENDPOINT:?real model endpoint is required for UAT}"
: "${MYCLAW_REAL_LLM_MODEL:?real model id is required for UAT}"
: "${MYCLAW_BOCHA_API_KEY:?Bocha API key is required for UAT}"
: "${MYCLAW_BRAVE_API_KEY:?Brave API key is required for UAT}"
export MYCLAW_FULLSTACK_API_URL="http://127.0.0.1:${UAT_API_PORT}"
export MYCLAW_FULLSTACK_UI_URL="http://127.0.0.1:${UAT_UI_PORT}"
export MYCLAW_TEST_ADMIN_EMAIL="${INIT_ADMIN_EMAIL}"
export MYCLAW_TEST_ADMIN_PASSWORD="${INIT_ADMIN_PASSWORD}"
export MYCLAW_COMPOSE_PROJECT="${compose_project}"
export MYCLAW_COMPOSE_FILE="${compose_file}"
export MYCLAW_COMPOSE_ENV_FILE="${env_file}"
export MYCLAW_RUNTIME_WORKER_SERVICE=runtime-worker
export MYCLAW_SANDBOX_WORKER_SERVICE=sandbox-worker
export MYCLAW_BROWSER_WORKER_SERVICE=browser-worker
export MYCLAW_BROWSER_EVIDENCE_DIR="${evidence_dir}/browser-screenshots"

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
docker compose --project-name "${compose_project}" --env-file "${env_file}" -f "${compose_file}" logs --no-color 2>&1 \
  | sed -E \
      -e 's/sk-[A-Za-z0-9_-]{20,}/[REDACTED_API_KEY]/g' \
      -e 's/(Authorization:[[:space:]]*Bearer[[:space:]]+)[^[:space:]]+/\1[REDACTED]/Ig' \
  > "${evidence_dir}/uat-stack.log"
[[ ${pytest_status} -eq 0 ]] || exit "${pytest_status}"

"${pytest_bin%/pytest}/python" "${root}/test/scripts/collect_uat_evidence.py" \
  "${evidence_dir}/metrics.json"
docker run --rm \
  --mount type=volume,src="${app_volume}",dst=/source,readonly \
  --mount type=bind,src="${evidence_dir}",dst=/evidence \
  alpine:3.20 sh -c \
  'test -s /source/otel/uat-otel-traces.jsonl && cp /source/otel/uat-otel-traces.jsonl /evidence/otel-traces.jsonl'
docker run --rm \
  --mount type=volume,src="${app_volume}",dst=/source,readonly \
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
docker compose --project-name "${compose_project}" --env-file "${env_file}" -f "${compose_file}" images --format json \
  > "${evidence_dir}/compose-images.jsonl"
docker compose --project-name "${compose_project}" --env-file "${env_file}" -f "${compose_file}" ps --format json \
  > "${evidence_dir}/compose-ps.jsonl"
if grep -qi 'mock' "${evidence_dir}/compose-ps.jsonl"; then
  echo "Forbidden substitute service found in UAT service list" >&2
  exit 3
fi
docker compose --project-name "${compose_project}" --env-file "${env_file}" -f "${compose_file}" exec -T mysql \
  mysql -u"${MYSQL_USER}" -p"${MYSQL_PASSWORD}" "${MYSQL_DATABASE}" -N -e "
    SELECT CONCAT('tool_definitions=', COUNT(*)) FROM platform_tool;
    SELECT CONCAT('tool_versions=', COUNT(*)) FROM tool_version;
    SELECT CONCAT('connection_revisions=', COUNT(*)) FROM tool_connection_revision;
    SELECT CONCAT('tool_invocations=', COUNT(*)) FROM tool_invocation;
    SELECT CONCAT('successful_tool_invocations=', COUNT(*)) FROM tool_invocation WHERE status='succeeded';
    SELECT CONCAT('published_artifacts=', COUNT(*)) FROM artifact WHERE status='published';
    SELECT CONCAT('browser_sessions=', COUNT(*)) FROM browser_session_state;
    SELECT CONCAT('accepted_memories=', COUNT(*)) FROM memory_write_candidate WHERE status='accepted';
  " > "${evidence_dir}/database-facts.txt"
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

"${pytest_bin%/pytest}/python" "${root}/test/scripts/collect_acceptance_evidence.py" \
  --contracts-report "${standard_evidence}/tool-contracts.xml" \
  --mysql-report "${standard_evidence}/mysql-tool-contracts.xml" \
  --real-report "${evidence_dir}/real-model-fullstack.xml" \
  --schema-contract "${standard_evidence}/schema-contract.txt" \
  --database-facts "${evidence_dir}/database-facts.txt" \
  --service-list "${evidence_dir}/compose-ps.jsonl" \
  --browser-evidence-dir "${evidence_dir}/browser-screenshots" \
  --artifact-checksums "${evidence_dir}/artifacts.files.sha256" \
  --model "${MYCLAW_REAL_LLM_MODEL}" \
  --endpoint "${MYCLAW_REAL_LLM_ENDPOINT}" \
  --output "${evidence_dir}/release-evidence.json"

echo "Real-model UAT passed for ${actual_sha}; evidence: ${evidence_dir}"
