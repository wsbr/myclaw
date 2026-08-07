#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${script_dir}/.env"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_path="${script_dir}/backups/myclaw-uat-single-node-${timestamp}.sql.gz"
partial_path="${backup_path}.partial"

umask 077
mkdir -p "${script_dir}/backups"
trap 'rm -f -- "${partial_path}"' EXIT
docker compose --env-file "${env_file}" -f "${script_dir}/docker-compose.yml" exec -T mysql \
  sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" exec mysqldump -u"$MYSQL_USER" --single-transaction --quick --routines --triggers --events --hex-blob --no-tablespaces --set-gtid-purged=OFF "$MYSQL_DATABASE"' \
  | gzip -9 > "${partial_path}"
gzip -t "${partial_path}"
mv "${partial_path}" "${backup_path}"
shasum -a 256 "${backup_path}" > "${backup_path}.sha256"
trap - EXIT
echo "${backup_path}"
