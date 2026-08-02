#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> [1/2] Unit and service tests"
"$ROOT/scripts/run_unit.sh" "$@"

echo "==> [2/2] Real deployment acceptance"
"$ROOT/scripts/run_fullstack.sh" "$@"
