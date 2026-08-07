#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${script_dir}/../.." && pwd)"
env_file="${script_dir}/.env"
compose_file="${script_dir}/docker-compose.yml"
duration_seconds="${MYCLAW_OBSERVE_SECONDS:-1800}"
interval_seconds="${MYCLAW_OBSERVE_INTERVAL_SECONDS:-30}"

[[ -r "${env_file}" ]] || { echo "Missing ${env_file}" >&2; exit 2; }
[[ "${duration_seconds}" =~ ^[1-9][0-9]*$ ]] || { echo "Invalid observation duration" >&2; exit 2; }
[[ "${interval_seconds}" =~ ^[1-9][0-9]*$ ]] || { echo "Invalid observation interval" >&2; exit 2; }
if ! git -C "${root}" diff --quiet || ! git -C "${root}" diff --cached --quiet; then
  echo "Refusing to observe a dirty workspace" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a
actual_sha="$(git -C "${root}" rev-parse HEAD)"
[[ "${MYCLAW_ACCEPTANCE_COMMIT_SHA}" == "${actual_sha}" ]] || {
  echo "Observation environment SHA does not match workspace" >&2
  exit 2
}

compose() {
  docker compose --env-file "${env_file}" -f "${compose_file}" "$@"
}
mysql_query() {
  compose exec -T mysql sh -c \
    'MYSQL_PWD="$MYSQL_PASSWORD" mysql -N -B -u"$MYSQL_USER" "$MYSQL_DATABASE"' <<<"$1"
}
count_logs() {
  local pattern="$1"
  compose logs --no-color --since "${started_at}" api runtime-worker sandbox-worker 2>&1 \
    | grep -Eic "${pattern}" || true
}

for service in api frontend runtime-worker sandbox-worker maintenance-worker mysql redis otel-collector; do
  [[ -n "$(compose ps -q "${service}" | head -n 1)" ]] || {
    echo "${service} is not running" >&2
    exit 2
  }
done

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
started_epoch="$(date +%s)"
evidence_dir="${script_dir}/evidence/${actual_sha}"
samples_file="${evidence_dir}/observation-samples.jsonl"
summary_file="${evidence_dir}/observation-summary.json"
mkdir -p "${evidence_dir}"
: > "${samples_file}"

sample_count=0
while (( $(date +%s) - started_epoch < duration_seconds )); do
  sampled_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  api_status="$(curl --silent --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:${UAT_API_PORT}/health" || true)"
  ui_status="$(curl --silent --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:${UAT_UI_PORT}/healthz" || true)"
  db_values="$(mysql_query "
    SELECT
      (SELECT COUNT(*) FROM provider_attempt WHERE created_at >= '${started_at}' AND status IN ('failed','timed_out','outcome_unknown')),
      (SELECT COUNT(*) FROM outbox_command WHERE status IN ('pending','claimed','retry_wait','dead')),
      (SELECT COALESCE(MAX(TIMESTAMPDIFF(SECOND,created_at,UTC_TIMESTAMP())),0) FROM outbox_command WHERE status IN ('pending','claimed','retry_wait')),
      (SELECT COUNT(*) FROM runner_v1 WHERE status='running' AND lease_expires_at < UTC_TIMESTAMP()),
      (SELECT COUNT(*) FROM runner_v1 WHERE updated_at >= '${started_at}' AND status='lost'),
      (SELECT COUNT(*) FROM execution_event WHERE created_at >= '${started_at}' AND event_type='runner.recovered'),
      (SELECT COUNT(*) FROM tool_attempt WHERE created_at >= '${started_at}'),
      (SELECT COUNT(*) FROM tool_attempt WHERE created_at >= '${started_at}' AND status IN ('failed','timed_out','outcome_unknown')),
      (SELECT COUNT(*) FROM run WHERE status IN ('active','waiting_user','waiting_approval','waiting_tool','waiting_agent')),
      (SELECT COUNT(*) FROM observability_span_link);
  ")"
  IFS=$'\t' read -r provider_errors outbox_open outbox_oldest runner_expired runner_lost checkpoint_recovery sandbox_attempts sandbox_errors open_runs span_links <<<"${db_values}"
  http_5xx="$(count_logs 'HTTP/[0-9.]+\" 5[0-9][0-9]')"
  http_429="$(count_logs 'HTTP/[0-9.]+\" 429')"
  pool_errors="$(count_logs 'QueuePool|pool timeout|connection pool.*(exhaust|timeout)|Too many connections')"
  SAMPLE_FILE="${samples_file}" python3 - \
    "${sampled_at}" "${api_status}" "${ui_status}" "${provider_errors}" \
    "${outbox_open}" "${outbox_oldest}" "${runner_expired}" "${runner_lost}" \
    "${checkpoint_recovery}" "${sandbox_attempts}" "${sandbox_errors}" \
    "${open_runs}" "${span_links}" "${http_5xx}" "${http_429}" "${pool_errors}" <<'PY'
import json
import os
import sys

keys = (
    "sampled_at", "api_http_status", "ui_http_status", "provider_errors",
    "outbox_open", "outbox_oldest_age_seconds", "runner_expired_leases",
    "runner_lost", "checkpoint_recoveries", "sandbox_attempts", "sandbox_errors",
    "open_runs", "span_links", "http_5xx", "http_429", "database_pool_errors",
)
values = [sys.argv[1], *[int(value or 0) for value in sys.argv[2:]]]
with open(os.environ["SAMPLE_FILE"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(dict(zip(keys, values)), sort_keys=True) + "\n")
PY
  sample_count=$((sample_count + 1))
  remaining=$((duration_seconds - ($(date +%s) - started_epoch)))
  (( remaining > 0 )) || break
  (( remaining < interval_seconds )) && sleep "${remaining}" || sleep "${interval_seconds}"
done

integrity_values="$(mysql_query "
  SELECT
    (SELECT COUNT(*) FROM (
      SELECT session_id FROM execution_event GROUP BY session_id
      HAVING COUNT(*) <> MAX(sequence) - MIN(sequence) + 1
    ) AS event_gaps),
    (SELECT COUNT(*) FROM (
      SELECT r.id FROM run AS r
      LEFT JOIN execution_event AS e ON e.run_id=r.id
        AND e.event_type IN ('run.completed','run.failed','run.cancelled')
      WHERE r.status IN ('completed','failed','cancelled')
      GROUP BY r.id HAVING COUNT(e.id) <> 1
    ) AS terminal_duplicates),
    (SELECT COUNT(*) FROM run WHERE status IN ('completed','failed','cancelled') AND completed_at IS NULL),
    (SELECT COUNT(*) FROM observability_span_link),
    (SELECT COUNT(*) FROM observability_span WHERE
      LOWER(CAST(attributes AS CHAR)) REGEXP '(authorization|api[_-]?key|password|bearer[[:space:]])');
")"
IFS=$'\t' read -r sequence_gaps terminal_duplicates invalid_terminal span_links sensitive_spans <<<"${integrity_values}"

OBSERVATION_SAMPLES="${samples_file}" OBSERVATION_SUMMARY="${summary_file}" python3 - \
  "${actual_sha}" "${started_at}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "${duration_seconds}" "${interval_seconds}" "${sample_count}" "${sequence_gaps}" \
  "${terminal_duplicates}" "${invalid_terminal}" "${span_links}" "${sensitive_spans}" <<'PY'
import json
import os
import sys
from pathlib import Path

samples = [json.loads(line) for line in Path(os.environ["OBSERVATION_SAMPLES"]).read_text().splitlines()]
if not samples:
    raise SystemExit("observation produced no samples")

integer_fields = (
    "provider_errors", "outbox_open", "outbox_oldest_age_seconds",
    "runner_expired_leases", "runner_lost", "checkpoint_recoveries",
    "sandbox_attempts", "sandbox_errors", "open_runs", "span_links",
    "http_5xx", "http_429", "database_pool_errors",
)
maxima = {field: max(sample[field] for sample in samples) for field in integer_fields}
integrity = {
    "execution_event_sequence_gap_sessions": int(sys.argv[7]),
    "runs_with_non_unique_terminal_event": int(sys.argv[8]),
    "terminal_runs_without_completed_at": int(sys.argv[9]),
    "span_links": int(sys.argv[10]),
    "spans_with_sensitive_attributes": int(sys.argv[11]),
}
failures = []
if any(sample["api_http_status"] != 200 or sample["ui_http_status"] != 200 for sample in samples):
    failures.append("health_status")
for field in ("provider_errors", "runner_expired_leases", "runner_lost", "sandbox_errors", "http_5xx", "database_pool_errors"):
    if maxima[field] != 0:
        failures.append(field)
if maxima["outbox_open"] != 0 or maxima["outbox_oldest_age_seconds"] != 0:
    failures.append("outbox_backlog")
if any(value != 0 for key, value in integrity.items() if key != "span_links"):
    failures.append("integrity")
if integrity["span_links"] < 1:
    failures.append("span_links")

result = {
    "schema": "myclaw.uat_observation.v1",
    "status": "passed" if not failures else "failed",
    "commit_sha": sys.argv[1],
    "started_at": sys.argv[2],
    "completed_at": sys.argv[3],
    "duration_seconds": int(sys.argv[4]),
    "interval_seconds": int(sys.argv[5]),
    "sample_count": int(sys.argv[6]),
    "maxima": maxima,
    "integrity": integrity,
    "failures": failures,
    "notes": {
        "http_429": "recorded for capacity review; no requests are generated by this observer except health checks",
        "checkpoint_recoveries": "recorded; recovery injection evidence remains in the acceptance JUnit and metrics",
        "database_pool": "observed through health probes and backend pool-exhaustion log signatures",
    },
}
Path(os.environ["OBSERVATION_SUMMARY"]).write_text(
    json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
if failures:
    raise SystemExit("observation gate failed: " + ", ".join(failures))
PY

echo "UAT observation passed for ${duration_seconds}s (${sample_count} samples): ${summary_file}"
