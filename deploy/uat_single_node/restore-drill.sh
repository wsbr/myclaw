#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 /absolute/path/to/mysql.sql.gz /absolute/path/to/app-data.tar.gz" >&2
  exit 2
fi
mysql_backup="$1"
app_backup="$2"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${script_dir}/.env"

[[ "${mysql_backup}" = /* && "${app_backup}" = /* ]] || {
  echo "Backup paths must be absolute" >&2
  exit 2
}
for path in \
  "${mysql_backup}" "${mysql_backup}.sha256" \
  "${app_backup}" "${app_backup}.sha256" "${app_backup}.files.sha256"; do
  [[ -r "${path}" ]] || { echo "Missing restore input: ${path}" >&2; exit 2; }
done

set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a
short_sha="${MYCLAW_ACCEPTANCE_COMMIT_SHA:0:12}"
container="myclaw-uat-restore-${short_sha}"
volume="myclaw-uat-restore-${short_sha}-mysql"
app_volume="myclaw-uat-restore-${short_sha}-app"
restore_database="myclaw_restore_${short_sha}"
restore_user="myclaw_restore"
restore_password="$(openssl rand -hex 24)"
evidence_dir="${script_dir}/evidence/${MYCLAW_ACCEPTANCE_COMMIT_SHA}"
evidence_file="${evidence_dir}/restore-drill.json"
started_epoch="$(date +%s)"

cleanup() {
  docker rm -f "${container}" >/dev/null 2>&1 || true
  docker volume rm "${volume}" "${app_volume}" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup
mkdir -p "${evidence_dir}"
(cd "$(dirname "${mysql_backup}")" && shasum -a 256 -c "$(basename "${mysql_backup}.sha256")")
(cd "$(dirname "${app_backup}")" && shasum -a 256 -c "$(basename "${app_backup}.sha256")")
gzip -t "${mysql_backup}"
gzip -t "${app_backup}"
docker volume create "${volume}" >/dev/null
docker volume create "${app_volume}" >/dev/null
docker run -d --name "${container}" \
  --mount type=volume,src="${volume}",dst=/var/lib/mysql \
  -e MYSQL_ROOT_PASSWORD="${restore_password}" \
  -e MYSQL_DATABASE="${restore_database}" \
  -e MYSQL_USER="${restore_user}" \
  -e MYSQL_PASSWORD="${restore_password}" \
  mysql:8.0 --log-bin-trust-function-creators=1 >/dev/null
for _ in $(seq 1 60); do
  if docker exec "${container}" sh -c \
    'MYSQL_PWD="$MYSQL_PASSWORD" mysqladmin ping -h 127.0.0.1 -u"$MYSQL_USER" --silent' \
    >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
docker exec "${container}" sh -c \
  'MYSQL_PWD="$MYSQL_PASSWORD" mysqladmin ping -h 127.0.0.1 -u"$MYSQL_USER" --silent' \
  >/dev/null
gzip -dc "${mysql_backup}" \
  | sed -E 's/DEFINER=`[^`]+`@`[^`]+`/DEFINER=CURRENT_USER/g' \
  | docker exec -i "${container}" sh -c \
  'MYSQL_PWD="$MYSQL_PASSWORD" mysql -u"$MYSQL_USER" "$MYSQL_DATABASE"'

backup_dir="$(dirname "${app_backup}")"
docker run --rm \
  --mount type=volume,src="${app_volume}",dst=/restore \
  --mount type=bind,src="${backup_dir}",dst=/backup,readonly \
  alpine:3.20 sh -c \
  "tar -C /restore -xzf /backup/$(basename "${app_backup}") && cd /restore && sha256sum -c /backup/$(basename "${app_backup}").files.sha256"

source_query() {
  docker compose --env-file "${env_file}" -f "${script_dir}/docker-compose.yml" exec -T mysql \
    sh -c "MYSQL_PWD=\"\$MYSQL_PASSWORD\" mysql -N -u\"\$MYSQL_USER\" \"\$MYSQL_DATABASE\" -e \"$1\""
}
restore_query() {
  docker exec "${container}" sh -c \
    "MYSQL_PWD=\"\$MYSQL_PASSWORD\" mysql -N -u\"\$MYSQL_USER\" \"\$MYSQL_DATABASE\" -e \"$1\""
}
head="$(restore_query 'SELECT version_num FROM alembic_version')"
table_count="$(restore_query 'SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()')"
source_table_count="$(source_query 'SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()')"
run_count="$(restore_query 'SELECT COUNT(*) FROM run')"
source_run_count="$(source_query 'SELECT COUNT(*) FROM run')"
artifact_count="$(restore_query 'SELECT COUNT(*) FROM artifact')"
source_artifact_count="$(source_query 'SELECT COUNT(*) FROM artifact')"
trigger_count="$(restore_query "SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE='legacy_uat_fact_archive'")"
terminal_revival_count="$(restore_query "SELECT COUNT(*) FROM run WHERE status IN ('completed','cancelled','failed') AND completed_at IS NULL")"

[[ "${head}" == "0029" ]]
[[ "${table_count}" == "${source_table_count}" ]]
[[ "${run_count}" == "${source_run_count}" ]]
[[ "${artifact_count}" == "${source_artifact_count}" ]]
[[ "${trigger_count}" == "3" ]]
[[ "${terminal_revival_count}" == "0" ]]

printf '%s\n' \
  "{\"schema\":\"myclaw.restore_drill.v1\",\"commit_sha\":\"${MYCLAW_ACCEPTANCE_COMMIT_SHA}\",\"status\":\"passed\",\"alembic_head\":\"${head}\",\"tables\":${table_count},\"runs\":${run_count},\"artifacts\":${artifact_count},\"archive_seal_triggers\":${trigger_count},\"terminal_revival_count\":${terminal_revival_count},\"duration_seconds\":$(( $(date +%s) - started_epoch )),\"rpo\":\"uncommitted-UAT writes since backup are outside this isolated drill\",\"rto\":\"not committed for phase one\"}" \
  > "${evidence_file}"
echo "Isolated restore drill passed; temporary container and volumes will be removed."
