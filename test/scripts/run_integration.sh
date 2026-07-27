#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

docker compose -f docker-compose.test.yml up -d mysql-test redis-test
"$ROOT/scripts/wait_for_services.sh"

pytest integration/ -v -m integration "$@"
