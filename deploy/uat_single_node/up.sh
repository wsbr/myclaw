#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${script_dir}/../.." && pwd)"
env_file="${script_dir}/.env"
compose_file="${script_dir}/docker-compose.yml"

if [[ ! -r "${env_file}" ]]; then
  echo "Missing ${env_file}; run init-env.sh after creating the delivery commit" >&2
  exit 2
fi
if [[ "$(stat -f '%Lp' "${env_file}" 2>/dev/null || stat -c '%a' "${env_file}")" != "600" ]]; then
  echo "Refusing to use ${env_file}: permissions must be 0600" >&2
  exit 2
fi
if ! git -C "${root}" diff --quiet || ! git -C "${root}" diff --cached --quiet; then
  echo "Refusing to deploy a dirty workspace" >&2
  exit 2
fi
bound_sha="$(sed -n 's/^MYCLAW_ACCEPTANCE_COMMIT_SHA=//p' "${env_file}")"
actual_sha="$(git -C "${root}" rev-parse HEAD)"
if [[ -z "${bound_sha}" || "${bound_sha}" != "${actual_sha}" ]]; then
  echo "Environment SHA ${bound_sha:-missing} does not match clean workspace ${actual_sha}" >&2
  exit 2
fi

docker compose --env-file "${env_file}" -f "${compose_file}" up \
  -d --build --remove-orphans
