#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${script_dir}/.env"

if [[ -e "${env_file}" ]]; then
  echo "UAT environment already exists: ${env_file}" >&2
  exit 2
fi
if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required to generate UAT secrets" >&2
  exit 2
fi

printf 'Real model API key (input hidden): ' >&2
IFS= read -r -s model_api_key
printf '\n' >&2
if [[ -z "${model_api_key}" || "${model_api_key}" == *$'\n'* || "${model_api_key}" == *$'\r'* ]]; then
  echo "A non-empty, single-line API key is required" >&2
  exit 2
fi

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
    'UAT_UI_PORT=8181' \
    'UAT_API_PORT=8101' \
    'UAT_MYSQL_PORT=3310' \
    'UAT_REDIS_PORT=6390' \
    'UAT_OTLP_PORT=4320' \
    'UAT_OTEL_HEALTH_PORT=13135' \
    'UAT_API_PUBLIC_URL=http://localhost:8101' \
    "MYSQL_ROOT_PASSWORD=${mysql_root_password}" \
    'MYSQL_DATABASE=myclaw_uat' \
    'MYSQL_USER=myclaw_uat' \
    "MYSQL_PASSWORD=${mysql_password}" \
    "REDIS_PASSWORD=${redis_password}" \
    "DATABASE_URL=mysql+aiomysql://myclaw_uat:${mysql_password}@mysql:3306/myclaw_uat" \
    "REDIS_URL=redis://:${redis_password}@redis:6379/0" \
    "SECRET_KEY=${secret_key}" \
    "ENCRYPTION_KEY=${encryption_key}" \
    'INIT_ADMIN_EMAIL=admin@uat.myclaw.local' \
    "INIT_ADMIN_PASSWORD=${admin_password}" \
    'MYCLAW_ENVIRONMENT=production' \
    'DEBUG=false' \
    'CORS_ORIGINS=["http://localhost:8181","http://127.0.0.1:8181"]' \
    'RATE_LIMIT_ENABLED=true' \
    'RATE_LIMIT_REQUESTS=600' \
    'RATE_LIMIT_UNAUTHENTICATED_REQUESTS=60' \
    'RATE_LIMIT_WINDOW_SECONDS=60' \
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
    'MAINTENANCE_BATCH_SIZE=500' \
    'OUTBOX_COMPLETED_RETENTION_DAYS=30' \
    'OUTBOX_DEAD_RETENTION_DAYS=180' \
    'CHECKPOINT_RETENTION_DAYS=30' \
    'TRACE_RETENTION_DAYS=30' \
    'DELIVERY_ATTEMPT_RETENTION_DAYS=90' \
    'OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318' \
    'OTEL_EXPORTER_OTLP_HEADERS=' \
    'MYCLAW_RELEASE=uat-local' \
    "MYCLAW_REAL_LLM_API_KEY=${model_api_key}" \
    'MYCLAW_REAL_LLM_ENDPOINT=https://dashscope.aliyuncs.com/compatible-mode/v1' \
    'MYCLAW_REAL_LLM_MODEL=qwen3.5-plus'
} > "${temporary_file}"
chmod 600 "${temporary_file}"
mv "${temporary_file}" "${env_file}"
trap - EXIT

for volume in myclaw-uat-mysql-data myclaw-uat-redis-data myclaw-uat-app-data; do
  docker volume inspect "${volume}" >/dev/null 2>&1 || docker volume create "${volume}" >/dev/null
done
mkdir -p "${script_dir}/backups"

echo "UAT environment created at ${env_file}; credentials were not printed."
