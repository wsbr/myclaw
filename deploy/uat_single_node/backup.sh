#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${script_dir}/.env"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_path="${script_dir}/backups/myclaw-uat-single-node-${timestamp}.sql.gz"
partial_path="${backup_path}.partial"
app_backup_path="${script_dir}/backups/myclaw-uat-single-node-app-${timestamp}.tar.gz"
app_partial_path="${app_backup_path}.partial"

[[ -r "${env_file}" ]] || { echo "Missing ${env_file}" >&2; exit 2; }
set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a
actual_sha="$(git -C "${script_dir}/../.." rev-parse HEAD)"
[[ "${MYCLAW_ACCEPTANCE_COMMIT_SHA}" == "${actual_sha}" ]] || {
  echo "Backup environment SHA does not match workspace" >&2
  exit 2
}

umask 077
mkdir -p "${script_dir}/backups"
trap 'rm -f -- "${partial_path}" "${app_partial_path}"' EXIT
docker compose --env-file "${env_file}" -f "${script_dir}/docker-compose.yml" exec -T mysql \
  sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" exec mysqldump -u"$MYSQL_USER" --single-transaction --quick --routines --triggers --events --hex-blob --no-tablespaces --set-gtid-purged=OFF "$MYSQL_DATABASE"' \
  | gzip -9 > "${partial_path}"
gzip -t "${partial_path}"
mv "${partial_path}" "${backup_path}"
shasum -a 256 "${backup_path}" > "${backup_path}.sha256"

docker run --rm \
  --mount type=volume,src=myclaw-uat-single-node-app-data,dst=/source,readonly \
  --mount type=bind,src="${script_dir}/backups",dst=/backup \
  alpine:3.20 sh -c \
  "tar -C /source -czf /backup/$(basename "${app_partial_path}") ."
docker run --rm \
  --mount type=volume,src=myclaw-uat-single-node-app-data,dst=/source,readonly \
  --mount type=bind,src="${script_dir}/backups",dst=/backup \
  alpine:3.20 sh -c \
  "cd /source && find . -type f -print0 | sort -z | xargs -0 sha256sum > /backup/$(basename "${app_backup_path}").files.sha256"
gzip -t "${app_partial_path}"
mv "${app_partial_path}" "${app_backup_path}"
shasum -a 256 "${app_backup_path}" > "${app_backup_path}.sha256"
printf '%s\n' \
  "{\"schema\":\"myclaw.uat_backup.v1\",\"commit_sha\":\"${actual_sha}\",\"created_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"mysql\":\"$(basename "${backup_path}")\",\"app_data\":\"$(basename "${app_backup_path}")\"}" \
  > "${backup_path}.manifest.json"
trap - EXIT
printf '%s\n%s\n' "${backup_path}" "${app_backup_path}"
