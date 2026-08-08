#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE_FILE="$REPOSITORY_ROOT/deploy/docker-compose.uat.yml"
ENV_FILE="$REPOSITORY_ROOT/deploy/uat/.env"
CONFIRMATION="${MYCLAW_CONFIRM_DESTRUCTIVE_RESET:-}"
EXPECTED_CONFIRMATION="ERASE_MYCLAW_UAT_WITHOUT_BACKUP"
TARGET_VOLUMES=(
  "myclaw-uat-mysql-data"
  "myclaw-uat-redis-data"
  "myclaw-uat-app-data"
)

if [ "$CONFIRMATION" != "$EXPECTED_CONFIRMATION" ]; then
  echo "Refusing destructive reset." >&2
  echo "Set MYCLAW_CONFIRM_DESTRUCTIVE_RESET=$EXPECTED_CONFIRMATION to erase the UAT database, Redis, Workspaces, Artifacts and Browser/Sandbox state without a backup." >&2
  exit 2
fi

test -f "$COMPOSE_FILE" || { echo "Missing $COMPOSE_FILE" >&2; exit 2; }
test -f "$ENV_FILE" || { echo "Missing $ENV_FILE" >&2; exit 2; }
docker info >/dev/null

target_containers_file="$(mktemp)"
target_projects_file="$(mktemp)"
cleanup_files() {
  rm -f "$target_containers_file" "$target_projects_file"
}
trap cleanup_files EXIT

# Resolve destructive targets only from the three exact external volume
# names. Every attached container must prove it came from this repository's
# deploy directory and from a MyClaw Compose project.
for volume in "${TARGET_VOLUMES[@]}"; do
  if ! docker volume inspect "$volume" >/dev/null 2>&1; then
    continue
  fi
  docker ps -aq --filter "volume=$volume" >> "$target_containers_file"
done
sort -u "$target_containers_file" -o "$target_containers_file"

while IFS= read -r container_id; do
  test -n "$container_id" || continue
  compose_project="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$container_id")"
  working_dir="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$container_id")"
  config_files="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$container_id")"
  case "$compose_project" in
    myclaw-*) ;;
    *) echo "Refusing container $container_id from non-MyClaw project '$compose_project'." >&2; exit 3 ;;
  esac
  case "$working_dir" in
    "$REPOSITORY_ROOT/deploy"|"$REPOSITORY_ROOT/deploy/uat_single_node") ;;
    *) echo "Refusing container $container_id outside the repository deploy directory: $working_dir" >&2; exit 3 ;;
  esac
  case "$config_files" in
    "$REPOSITORY_ROOT/deploy/"*|"$REPOSITORY_ROOT/deploy/uat_single_node/"*) ;;
    *) echo "Refusing unrecognized Compose file for container $container_id: $config_files" >&2; exit 3 ;;
  esac
  printf '%s\n' "$compose_project" >> "$target_projects_file"
done < "$target_containers_file"
sort -u "$target_projects_file" -o "$target_projects_file"

echo "Validated exact destructive targets: ${TARGET_VOLUMES[*]}"
echo "No backup will be created."

# Stop and remove every API/Worker/UI container in the validated projects,
# including frontends that do not mount one of the state volumes themselves.
while IFS= read -r compose_project; do
  test -n "$compose_project" || continue
  docker ps -aq --filter "label=com.docker.compose.project=$compose_project" | while IFS= read -r container_id; do
    test -n "$container_id" || continue
    observed_project="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$container_id")"
    test "$observed_project" = "$compose_project" || {
      echo "Container project changed during reset validation: $container_id" >&2
      exit 3
    }
    docker stop "$container_id" >/dev/null
    docker rm "$container_id" >/dev/null
  done
done < "$target_projects_file"

for volume in "${TARGET_VOLUMES[@]}"; do
  if docker volume inspect "$volume" >/dev/null 2>&1; then
    docker volume rm "$volume" >/dev/null
  fi
  docker volume create \
    --label com.myclaw.project=myclaw \
    --label com.myclaw.scope=uat \
    --label com.myclaw.schema=0030 \
    "$volume" >/dev/null
done

docker compose \
  --project-name myclaw-uat \
  --env-file "$ENV_FILE" \
  -f "$COMPOSE_FILE" \
  up -d --build --remove-orphans

uat_api_port="$(awk -F= '$1 == "UAT_API_PORT" {print $2}' "$ENV_FILE" | tail -1)"
uat_api_port="${uat_api_port:-8101}"
for _ in $(seq 1 90); do
  if curl --fail --silent "http://127.0.0.1:${uat_api_port}/health" >/dev/null; then
    echo "MyClaw UAT reset completed at Alembic head; the bootstrap administrator was recreated."
    echo "Model, Bocha, Brave and MCP connections must now be configured through write-only administrator inputs."
    exit 0
  fi
  sleep 2
done

docker compose --project-name myclaw-uat --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps >&2
echo "Reset volumes were created, but the new API did not become healthy." >&2
exit 1
