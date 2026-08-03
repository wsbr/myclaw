#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
compose_file="${script_dir}/../docker-compose.uat.yml"
env_file="${script_dir}/.env"

if [[ ! -r "${env_file}" ]]; then
  echo "Missing ${env_file}; run ${script_dir}/init-env.sh first" >&2
  exit 2
fi
if [[ "$(stat -f '%Lp' "${env_file}" 2>/dev/null || stat -c '%a' "${env_file}")" != "600" ]]; then
  echo "Refusing to use ${env_file}: permissions must be 0600" >&2
  exit 2
fi

for volume in myclaw-uat-mysql-data myclaw-uat-redis-data myclaw-uat-app-data; do
  docker volume inspect "${volume}" >/dev/null 2>&1 || docker volume create "${volume}" >/dev/null
done

docker compose --env-file "${env_file}" -f "${compose_file}" up \
  -d --build --remove-orphans --scale runtime-worker=2
