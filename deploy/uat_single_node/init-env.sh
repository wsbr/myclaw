#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${script_dir}/../.." && pwd)"
env_file="${script_dir}/.env"
source_env="${1:-${script_dir}/../uat/.env}"

if [[ -e "${env_file}" ]]; then
  echo "uat_single_node environment already exists: ${env_file}" >&2
  exit 2
fi
if [[ ! -r "${source_env}" ]]; then
  echo "A readable source environment containing the real-model settings is required" >&2
  exit 2
fi
if ! git -C "${root}" diff --quiet || ! git -C "${root}" diff --cached --quiet; then
  echo "Refusing to bind UAT to a dirty workspace" >&2
  exit 2
fi
uat_volumes=(
  myclaw-uat-single-node-mysql-data
  myclaw-uat-single-node-redis-data
  myclaw-uat-single-node-app-data
)
for volume in "${uat_volumes[@]}"; do
  if docker volume inspect "${volume}" >/dev/null 2>&1; then
    echo "Refusing to reuse existing UAT volume: ${volume}" >&2
    exit 2
  fi
done

read_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "${source_env}" | tail -n 1
}

model_api_key="$(read_value MYCLAW_REAL_LLM_API_KEY)"
model_endpoint="$(read_value MYCLAW_REAL_LLM_ENDPOINT)"
model_name="$(read_value MYCLAW_REAL_LLM_MODEL)"
if [[ -z "${model_api_key}" || -z "${model_endpoint}" || -z "${model_name}" ]]; then
  echo "Source environment is missing real-model key, endpoint, or model" >&2
  exit 2
fi
if [[ "${model_name}" != "qwen3.5-plus" ]]; then
  echo "The frozen UAT pricing profile is valid only for qwen3.5-plus" >&2
  exit 2
fi

commit_sha="$(git -C "${root}" rev-parse HEAD)"
mysql_root_password="$(openssl rand -hex 24)"
mysql_password="$(openssl rand -hex 24)"
redis_password="$(openssl rand -hex 24)"
secret_key="$(openssl rand -hex 32)"
encryption_key="$(openssl rand -hex 32)"
admin_password="$(openssl rand -hex 18)"
temporary_file="${env_file}.tmp"

umask 077
trap 'rm -f -- "${temporary_file}"' EXIT
{
  printf '%s\n' \
    'UAT_BIND_HOST=127.0.0.1' \
    'UAT_UI_PORT=8281' \
    'UAT_API_PORT=8201' \
    'UAT_MYSQL_PORT=3320' \
    'UAT_REDIS_PORT=6400' \
    'UAT_OTLP_PORT=4330' \
    'UAT_OTEL_HEALTH_PORT=13145' \
    'UAT_API_PUBLIC_URL=http://localhost:8201' \
    "MYCLAW_ACCEPTANCE_COMMIT_SHA=${commit_sha}" \
    "MYCLAW_RELEASE=uat-single-node-${commit_sha}" \
    "MYSQL_ROOT_PASSWORD=${mysql_root_password}" \
    'MYSQL_DATABASE=myclaw_uat_single_node' \
    'MYSQL_USER=myclaw_uat_single_node' \
    "MYSQL_PASSWORD=${mysql_password}" \
    "REDIS_PASSWORD=${redis_password}" \
    "DATABASE_URL=mysql+aiomysql://myclaw_uat_single_node:${mysql_password}@mysql:3306/myclaw_uat_single_node" \
    "REDIS_URL=redis://:${redis_password}@redis:6379/0" \
    "SECRET_KEY=${secret_key}" \
    "ENCRYPTION_KEY=${encryption_key}" \
    'INIT_ADMIN_EMAIL=admin@uat-single-node.myclaw.local' \
    "INIT_ADMIN_PASSWORD=${admin_password}" \
    'CORS_ORIGINS=["http://localhost:8281","http://127.0.0.1:8281"]' \
    'DATABASE_POOL_SIZE=10' \
    'DATABASE_MAX_OVERFLOW=20' \
    'RUNTIME_WORKER_POLL_SECONDS=0.5' \
    'RUNTIME_OUTBOX_CLAIM_LEASE_SECONDS=30' \
    'RUNTIME_RUNNER_LEASE_SECONDS=30' \
    'RUNTIME_RUNNER_HEARTBEAT_SECONDS=10' \
    'SANDBOX_WORKER_POLL_SECONDS=0.5' \
    'SANDBOX_OUTBOX_CLAIM_LEASE_SECONDS=30' \
    'SANDBOX_ATTEMPT_STALE_SECONDS=60' \
    'MAINTENANCE_POLL_SECONDS=3600' \
    'MAINTENANCE_COMMAND_POLL_SECONDS=1' \
    'MAINTENANCE_COMMAND_CLAIM_LEASE_SECONDS=300' \
    'MAINTENANCE_COMMAND_HEARTBEAT_SECONDS=30' \
    'MAINTENANCE_BATCH_SIZE=500' \
    'RATE_LIMIT_REQUESTS=600' \
    'RATE_LIMIT_UNAUTHENTICATED_REQUESTS=60' \
    'RATE_LIMIT_WINDOW_SECONDS=60' \
    "MYCLAW_REAL_LLM_API_KEY=${model_api_key}" \
    "MYCLAW_REAL_LLM_ENDPOINT=${model_endpoint}" \
    "MYCLAW_REAL_LLM_MODEL=${model_name}" \
    'MYCLAW_COST_CURRENCY=CNY' \
    'MYCLAW_INPUT_PRICE_PER_MILLION=0.8' \
    'MYCLAW_OUTPUT_PRICE_PER_MILLION=4.8' \
    'MYCLAW_PRICING_TIER_MAX_INPUT_TOKENS=128000' \
    'MYCLAW_PRICING_EFFECTIVE_DATE=2026-08-07' \
    'MYCLAW_PRICING_SOURCE_URL=https://help.aliyun.com/zh/model-studio/qwen3-5-plus'
} > "${temporary_file}"
chmod 600 "${temporary_file}"
mv "${temporary_file}" "${env_file}"
trap - EXIT

for volume in "${uat_volumes[@]}"; do
  docker volume create "${volume}" >/dev/null
done
mkdir -p "${script_dir}/backups" "${script_dir}/evidence/${commit_sha}"
echo "uat_single_node initialized and bound to ${commit_sha}; credentials were not printed."
