#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MAX_WAIT=120
INTERVAL=3
ELAPSED=0

wait_http() {
  local name="$1"
  local url="$2"
  ELAPSED=0
  until curl -sf "$url" > /dev/null 2>&1; do
    ELAPSED=$((ELAPSED + INTERVAL))
    if [ "$ELAPSED" -ge "$MAX_WAIT" ]; then
      echo "Timeout waiting for $name at $url"
      exit 1
    fi
    echo "  waiting for $name... (${ELAPSED}s)"
    sleep "$INTERVAL"
  done
  echo "  $name ready ($url)"
}

# Infra
"$ROOT/scripts/wait_for_services.sh"

# Full stack services. Agent acceptance is real-provider only; no local model
# surrogate belongs to this service graph or readiness contract.
wait_http "API" "${MYCLAW_FULLSTACK_API_URL:-http://localhost:8001}/health"
wait_http "Frontend" "${MYCLAW_FULLSTACK_UI_URL:-http://localhost:8081}"

echo "Full stack ready."
