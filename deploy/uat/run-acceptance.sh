#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${script_dir}/../.." && pwd)"

export MYCLAW_UAT_ENV_FILE="${script_dir}/.env"
export MYCLAW_UAT_COMPOSE_FILE="${root}/deploy/docker-compose.uat.yml"
export MYCLAW_UAT_COMPOSE_PROJECT="myclaw-uat"
export MYCLAW_UAT_APP_VOLUME="myclaw-uat-app-data"
export MYCLAW_UAT_EVIDENCE_ROOT="${script_dir}/evidence"

exec "${root}/deploy/uat_single_node/run-acceptance.sh" "$@"
