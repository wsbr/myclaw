#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Starting test infrastructure (MySQL + Redis)..."
docker compose -f docker-compose.test.yml up -d mysql-test redis-test

echo "==> Waiting for services..."
"$ROOT/scripts/wait_for_services.sh"

echo "==> Running all tests..."
pytest "$@" -v --tb=short
