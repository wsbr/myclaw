#!/usr/bin/env bash
set -euo pipefail

umask 077

if [[ $# -ne 1 ]]; then
  echo "usage: $0 /absolute/path/to/myclaw-backup.sql.gz" >&2
  exit 2
fi

backup_path="$1"
: "${MYSQL_RESTORE_DEFAULTS_FILE:?set MYSQL_RESTORE_DEFAULTS_FILE to a mounted MySQL client secret file}"
: "${MYSQL_RESTORE_DATABASE:?set MYSQL_RESTORE_DATABASE}"
: "${MYCLAW_RESTORE_CONFIRM:?set MYCLAW_RESTORE_CONFIRM=restore:<database>}"

if [[ "$MYCLAW_RESTORE_CONFIRM" != "restore:${MYSQL_RESTORE_DATABASE}" ]]; then
  echo "restore confirmation does not match target database" >&2
  exit 2
fi
if [[ ! "$MYSQL_RESTORE_DATABASE" =~ ^[A-Za-z0-9_]+$ ]]; then
  echo "MYSQL_RESTORE_DATABASE must contain only letters, digits, and underscores" >&2
  exit 2
fi
if [[ ! -r "$MYSQL_RESTORE_DEFAULTS_FILE" || ! -r "$backup_path" || ! -r "${backup_path}.sha256" ]]; then
  echo "restore secret, backup, or checksum is not readable" >&2
  exit 2
fi

checksum_dir="$(dirname "$backup_path")"
checksum_file="$(basename "${backup_path}.sha256")"
(cd "$checksum_dir" && shasum -a 256 -c "$checksum_file")
gzip -t "$backup_path"

mysql_host="${MYSQL_RESTORE_HOST:-mysql}"
mysql_port="${MYSQL_RESTORE_PORT:-3306}"
gzip -dc "$backup_path" | mysql \
  --defaults-extra-file="$MYSQL_RESTORE_DEFAULTS_FILE" \
  --host="$mysql_host" \
  --port="$mysql_port" \
  "$MYSQL_RESTORE_DATABASE"

echo "restore completed for ${MYSQL_RESTORE_DATABASE}"
