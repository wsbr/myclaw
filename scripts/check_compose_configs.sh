#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

MYCLAW_ENV_FILE=.env.example \
  docker compose --env-file .env.example -f docker-compose.yml config --quiet
MYCLAW_ENV_FILE=.env.example \
  docker compose --env-file .env.example -f docker-compose.yml -f docker-compose.dev.yml config --quiet
MYCLAW_ENV_FILE=.env.example \
  docker compose --env-file .env.example -f docker-compose.yml -f docker-compose.s3.yml config --quiet
docker compose --env-file deploy/uat/.env.example \
  -f deploy/docker-compose.uat.yml config --quiet
MYCLAW_ACCEPTANCE_COMMIT_SHA=0000000000000000000000000000000000000000 \
MYCLAW_RELEASE=v0.1.0-config-check \
  docker compose --env-file deploy/uat_single_node/.env.example \
  -f deploy/uat_single_node/docker-compose.yml config --quiet
docker compose --env-file test/.env.test \
  -f test/docker-compose.test.yml config --quiet

echo "All Compose configurations parsed successfully."
