#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="$repo_root/.env"
example_file="$repo_root/.env.example"

command -v docker >/dev/null 2>&1 || {
  echo "Docker is required." >&2
  exit 1
}
docker compose version >/dev/null 2>&1 || {
  echo "Docker Compose v2 is required." >&2
  exit 1
}

if [ -f "$env_file" ]; then
  echo "Using existing Git-ignored .env"
  exit 0
fi

command -v openssl >/dev/null 2>&1 || {
  echo "openssl is required to generate local development secrets." >&2
  exit 1
}

cp "$example_file" "$env_file"
chmod 600 "$env_file"

replace_env() {
  local key="$1"
  local value="$2"
  local temp_file
  temp_file="$(mktemp "$repo_root/.env.tmp.XXXXXX")"
  awk -v key="$key" -v value="$value" '
    BEGIN { found = 0 }
    index($0, key "=") == 1 { print key "=" value; found = 1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "$env_file" > "$temp_file"
  chmod 600 "$temp_file"
  mv "$temp_file" "$env_file"
}

mysql_root_password="$(openssl rand -hex 24)"
mysql_password="$(openssl rand -hex 24)"
redis_password="$(openssl rand -hex 24)"

replace_env "MYCLAW_ENVIRONMENT" "development"
replace_env "SECRET_KEY" "$(openssl rand -hex 32)"
replace_env "ENCRYPTION_KEY" "$(openssl rand -hex 32)"
replace_env "MYSQL_ROOT_PASSWORD" "$mysql_root_password"
replace_env "MYSQL_PASSWORD" "$mysql_password"
replace_env "REDIS_PASSWORD" "$redis_password"
replace_env "DATABASE_URL" "mysql+aiomysql://myclaw:${mysql_password}@mysql:3306/myclaw"
replace_env "REDIS_URL" "redis://:${redis_password}@redis:6379/0"
replace_env "INIT_ADMIN_PASSWORD" "$(openssl rand -hex 12)"
replace_env "MYCLAW_RELEASE" "$(tr -d '\n' < "$repo_root/VERSION")"

echo "Created .env with random local development secrets (mode 0600)."
echo "Read the bootstrap login with: grep '^INIT_ADMIN_' .env"
