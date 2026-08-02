#!/usr/bin/env bash
set -euo pipefail

umask 077

: "${MYSQL_BACKUP_DEFAULTS_FILE:?set MYSQL_BACKUP_DEFAULTS_FILE to a mounted MySQL client secret file}"
: "${MYSQL_BACKUP_DATABASE:?set MYSQL_BACKUP_DATABASE}"
: "${MYSQL_BACKUP_OUTPUT_DIR:?set MYSQL_BACKUP_OUTPUT_DIR}"

if [[ ! -r "$MYSQL_BACKUP_DEFAULTS_FILE" ]]; then
  echo "MySQL defaults secret is not readable" >&2
  exit 2
fi
if [[ ! "$MYSQL_BACKUP_DATABASE" =~ ^[A-Za-z0-9_]+$ ]]; then
  echo "MYSQL_BACKUP_DATABASE must contain only letters, digits, and underscores" >&2
  exit 2
fi

mysql_host="${MYSQL_BACKUP_HOST:-mysql}"
mysql_port="${MYSQL_BACKUP_PORT:-3306}"
mkdir -p "$MYSQL_BACKUP_OUTPUT_DIR"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_path="${MYSQL_BACKUP_OUTPUT_DIR%/}/myclaw-${timestamp}.sql.gz"
partial_path="${backup_path}.partial"

cleanup_partial() {
  rm -f -- "$partial_path"
}
trap cleanup_partial EXIT

mysqldump \
  --defaults-extra-file="$MYSQL_BACKUP_DEFAULTS_FILE" \
  --host="$mysql_host" \
  --port="$mysql_port" \
  --single-transaction \
  --quick \
  --routines \
  --triggers \
  --events \
  --hex-blob \
  --set-gtid-purged=OFF \
  "$MYSQL_BACKUP_DATABASE" \
  | gzip -9 > "$partial_path"

gzip -t "$partial_path"
mv -- "$partial_path" "$backup_path"
shasum -a 256 "$backup_path" > "${backup_path}.sha256"
trap - EXIT

echo "$backup_path"
