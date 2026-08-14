#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${script_dir}/../.." && pwd)"
env_file="${script_dir}/.env"

[[ -r "${env_file}" ]] || { echo "Missing ${env_file}" >&2; exit 2; }
set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a
commit_sha="$(git -C "${root}" rev-parse HEAD)"
output="${script_dir}/evidence/${commit_sha}/semantic-knowledge.json"

PYTHONPATH="${root}/backend" "${root}/test/.venv/bin/python" \
  "${root}/test/scripts/run_semantic_knowledge_acceptance.py" \
  --base-url "http://127.0.0.1:${UAT_API_PORT:-8101}" \
  --commit-sha "${commit_sha}" \
  --output "${output}"

echo "Semantic Knowledge acceptance passed: ${output}"
