#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <previous-compatible-clean-sha>" >&2
  exit 2
fi
previous_sha="$1"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${script_dir}/../.." && pwd)"
env_file="${script_dir}/.env"
compose_file="${script_dir}/docker-compose.yml"

set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a
current_sha="$(git -C "${root}" rev-parse HEAD)"
[[ "${MYCLAW_ACCEPTANCE_COMMIT_SHA}" == "${current_sha}" ]]
git -C "${root}" merge-base --is-ancestor "${previous_sha}" "${current_sha}"
git -C "${root}" cat-file -e "${previous_sha}:backend/migrations/versions/0029_remove_v1_orchestration.py"
if ! git -C "${root}" diff --quiet || ! git -C "${root}" diff --cached --quiet; then
  echo "Refusing deployment drill from a dirty workspace" >&2
  exit 2
fi

compose() {
  docker compose --env-file "${env_file}" -f "${compose_file}" "$@"
}
service_id() {
  compose ps -q "$1" | head -n 1
}
image_ref() {
  docker inspect --format '{{.Config.Image}}' "$(service_id "$1")"
}
image_id() {
  docker inspect --format '{{.Image}}' "$(service_id "$1")"
}
api_orm_smoke() {
  local session_id
  session_id="$(compose exec -T mysql sh -c \
    'MYSQL_PWD="$MYSQL_PASSWORD" mysql -N -u"$MYSQL_USER" "$MYSQL_DATABASE" -e "SELECT session_id FROM run ORDER BY created_at LIMIT 1"')"
  [[ -n "${session_id}" ]]
  MYCLAW_DRILL_API_URL="http://127.0.0.1:${UAT_API_PORT}" \
  MYCLAW_DRILL_SESSION_ID="${session_id}" python3 - <<'PY'
import json
import os
import urllib.request

base = os.environ["MYCLAW_DRILL_API_URL"]
payload = json.dumps({
    "email": os.environ["INIT_ADMIN_EMAIL"],
    "password": os.environ["INIT_ADMIN_PASSWORD"],
}).encode()
request = urllib.request.Request(
    f"{base}/api/v1/auth/login", data=payload,
    headers={"Content-Type": "application/json"}, method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    token = json.load(response)["access_token"]
request = urllib.request.Request(
    f"{base}/api/v1/sessions/{os.environ['MYCLAW_DRILL_SESSION_ID']}/runs",
    headers={"Authorization": f"Bearer {token}"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    body = json.load(response)
if not body.get("items"):
    raise SystemExit("rollback image did not return the expected Run projection")
PY
}

for service in api frontend runtime-worker sandbox-worker skill-sync-worker maintenance-worker; do
  [[ -n "$(service_id "${service}")" ]] || { echo "${service} is not running" >&2; exit 2; }
done
curl --fail --silent --show-error "http://127.0.0.1:${UAT_API_PORT}/health" >/dev/null

api_ref="$(image_ref api)"; api_final="$(image_id api)"
frontend_ref="$(image_ref frontend)"; frontend_final="$(image_id frontend)"
runtime_ref="$(image_ref runtime-worker)"; runtime_final="$(image_id runtime-worker)"
sandbox_ref="$(image_ref sandbox-worker)"; sandbox_final="$(image_id sandbox-worker)"
skill_sync_ref="$(image_ref skill-sync-worker)"; skill_sync_final="$(image_id skill-sync-worker)"
maintenance_ref="$(image_ref maintenance-worker)"; maintenance_final="$(image_id maintenance-worker)"
temp_root="$(mktemp -d)"
source_tree="${temp_root}/source"
rollback_backend="myclaw-uat-rollback-backend:${previous_sha:0:12}"
rollback_frontend="myclaw-uat-rollback-frontend:${previous_sha:0:12}"
evidence_dir="${script_dir}/evidence/${current_sha}"
evidence_file="${evidence_dir}/deployment-rollback-drill.json"
started_epoch="$(date +%s)"
restored=0

restore_final_images() {
  docker tag "${api_final}" "${api_ref}" >/dev/null
  docker tag "${frontend_final}" "${frontend_ref}" >/dev/null
  docker tag "${runtime_final}" "${runtime_ref}" >/dev/null
  docker tag "${sandbox_final}" "${sandbox_ref}" >/dev/null
  docker tag "${skill_sync_final}" "${skill_sync_ref}" >/dev/null
  docker tag "${maintenance_final}" "${maintenance_ref}" >/dev/null
  compose up -d --no-build --force-recreate \
    api frontend runtime-worker sandbox-worker skill-sync-worker maintenance-worker >/dev/null
  restored=1
}
cleanup() {
  if [[ ${restored} -eq 0 ]]; then
    restore_final_images || true
  fi
  git -C "${root}" worktree remove --force "${source_tree}" >/dev/null 2>&1 || true
  rmdir "${temp_root}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p "${evidence_dir}"
"${script_dir}/backup.sh" > "${evidence_dir}/deployment-drill-backups.txt"
git -C "${root}" worktree add --detach "${source_tree}" "${previous_sha}" >/dev/null
docker build \
  --label "org.opencontainers.image.revision=${previous_sha}" \
  -t "${rollback_backend}" "${source_tree}/backend" >/dev/null
docker build \
  --label "org.opencontainers.image.revision=${previous_sha}" \
  --build-arg "VITE_API_BASE_URL=http://localhost:${UAT_API_PORT}" \
  -t "${rollback_frontend}" "${source_tree}/frontend" >/dev/null

compose stop api frontend runtime-worker sandbox-worker skill-sync-worker maintenance-worker >/dev/null
docker tag "${rollback_backend}" "${api_ref}"
docker tag "${rollback_backend}" "${runtime_ref}"
docker tag "${rollback_backend}" "${sandbox_ref}"
docker tag "${rollback_backend}" "${maintenance_ref}"
docker tag "${rollback_frontend}" "${frontend_ref}"
compose up -d --no-build --force-recreate \
  api frontend runtime-worker sandbox-worker maintenance-worker >/dev/null
for _ in $(seq 1 90); do
  if curl --fail --silent "http://127.0.0.1:${UAT_API_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl --fail --silent --show-error "http://127.0.0.1:${UAT_API_PORT}/health" >/dev/null
api_orm_smoke
rollback_head="$(compose exec -T api alembic current 2>/dev/null | sed -n 's/ .*//p' | tail -n 1)"
[[ "${rollback_head}" == "0029" ]]
rollback_revision="$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$(service_id api)")"
[[ "${rollback_revision}" == "${previous_sha}" ]]

restore_final_images
for _ in $(seq 1 90); do
  if curl --fail --silent "http://127.0.0.1:${UAT_API_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl --fail --silent --show-error "http://127.0.0.1:${UAT_API_PORT}/health" >/dev/null
api_orm_smoke
forward_revision="$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$(service_id api)")"
[[ "${forward_revision}" == "${current_sha}" ]]
printf '%s\n' \
  "{\"schema\":\"myclaw.deployment_rollback_drill.v1\",\"status\":\"passed\",\"commit_sha\":\"${current_sha}\",\"rollback_sha\":\"${previous_sha}\",\"rollback_alembic_head\":\"${rollback_head}\",\"rollback_image_revision\":\"${rollback_revision}\",\"forward_image_revision\":\"${forward_revision}\",\"backup_manifest\":\"deployment-drill-backups.txt\",\"duration_seconds\":$(( $(date +%s) - started_epoch ))}" \
  > "${evidence_file}"
echo "Deployment, compatible-image rollback, and forward recovery drill passed."
