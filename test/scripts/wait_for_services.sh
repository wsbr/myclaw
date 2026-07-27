#!/usr/bin/env bash
set -euo pipefail

MAX_WAIT=60
INTERVAL=2
ELAPSED=0

wait_for() {
  local name="$1"
  local cmd="$2"
  ELAPSED=0
  until eval "$cmd"; do
    ELAPSED=$((ELAPSED + INTERVAL))
    if [ "$ELAPSED" -ge "$MAX_WAIT" ]; then
      echo "Timeout waiting for $name"
      exit 1
    fi
    echo "  waiting for $name... (${ELAPSED}s)"
    sleep "$INTERVAL"
  done
  echo "  $name ready"
}

wait_for "MySQL" "docker compose -f docker-compose.test.yml exec -T mysql-test mysqladmin ping -h localhost -u myclaw -pmypassword --silent"
wait_for "Redis" "docker compose -f docker-compose.test.yml exec -T redis-test redis-cli -a redispassword ping | grep -q PONG"

echo "All services ready."
