#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root/frontend"

if command -v pnpm >/dev/null 2>&1; then
  package_manager=(pnpm)
elif command -v corepack >/dev/null 2>&1; then
  package_manager=(corepack pnpm)
elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "pnpm is unavailable; validating the frontend through its production Docker build."
  docker build --tag myclaw-frontend-release-check:local "$repo_root/frontend"
  exit 0
else
  echo "pnpm 10.15.0, Corepack, or a running Docker daemon is required for the frontend build." >&2
  exit 1
fi

"${package_manager[@]}" install --frozen-lockfile
"${package_manager[@]}" build
