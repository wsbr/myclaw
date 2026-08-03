#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
docker compose --env-file "${script_dir}/.env" \
  -f "${script_dir}/../docker-compose.uat.yml" stop
