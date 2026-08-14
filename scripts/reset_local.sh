#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [ "${MYCLAW_CONFIRM_LOCAL_RESET:-}" != "ERASE_MYCLAW_LOCAL_VOLUMES" ]; then
  echo "This removes the local Compose containers and volumes." >&2
  echo "Re-run with MYCLAW_CONFIRM_LOCAL_RESET=ERASE_MYCLAW_LOCAL_VOLUMES make reset" >&2
  exit 2
fi

docker compose down --volumes --remove-orphans
echo "Removed only the local MyClaw Compose containers and named volumes."
