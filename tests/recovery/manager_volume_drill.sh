#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
FIXTURE_ROOT="$SCRIPT_DIR/fixtures"
COMPOSE_FILE="$FIXTURE_ROOT/manager-recovery.compose.yml"
WORKSPACE_ROOT="$SCRIPT_DIR/workspaces"

log() {
  printf '[manager-recovery-drill] %s\n' "$*"
}

die() {
  printf '[manager-recovery-drill] error: %s\n' "$*" >&2
  exit 1
}

for command in docker tar grep find cmp; do
  command -v "$command" >/dev/null 2>&1 || die "required command not found: $command"
done
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  die "Docker Compose v2 plugin or docker-compose is required"
fi
docker info >/dev/null 2>&1 || die "Docker daemon is not reachable"

umask 077
install -d -m 0700 "$WORKSPACE_ROOT"
workdir=$(mktemp -d "$WORKSPACE_ROOT/manager-volume.XXXXXX")
project="rvc-recovery-drill-$(date -u +%Y%m%d%H%M%S)-$$"
case "$project" in
  rvc-recovery-drill-[0-9]*) ;;
  *) die "generated an unsafe Compose project name" ;;
esac
api_image=${RVC_RECOVERY_DRILL_API_IMAGE:-rvc-orchestrator-api:recovery-drill}
if ! docker image inspect "$api_image" >/dev/null 2>&1; then
  log "building the real API dependency image used by object metadata recovery"
  docker build -f "$REPO_ROOT/apps/api/Dockerfile" -t "$api_image" "$REPO_ROOT"
fi

install_root="$workdir/install"
config_root="$workdir/config"
backup_root="$workdir/backups"
pre_restore_root="$workdir/pre-restore-backups"
inspect_root="$workdir/archive-inspection"
release="$install_root/releases/drill-1.0.0"

export RVC_INSTALL_ROOT="$install_root"
export RVC_CONFIG_ROOT="$config_root"
export RVC_INSTALL_ALLOW_NON_ROOT=1
export RVC_RECOVERY_DRILL_PROJECT="$project"
export RVC_RECOVERY_DRILL_COMPOSE_FILE="$COMPOSE_FILE"
export RVC_RECOVERY_DRILL_FIXTURE_ROOT="$FIXTURE_ROOT"
export RVC_RECOVERY_DRILL_REPO_ROOT="$REPO_ROOT"
export RVC_RECOVERY_DRILL_API_IMAGE="$api_image"
export RVC_RESTORE_READY_ATTEMPTS=${RVC_RESTORE_READY_ATTEMPTS:-30}
export RVC_RESTORE_READY_INTERVAL_SECONDS=${RVC_RESTORE_READY_INTERVAL_SECONDS:-1}

compose() {
  "${COMPOSE[@]}" \
    --project-name "$project" \
    --env-file "$config_root/manager.env" \
    -f "$COMPOSE_FILE" "$@"
}

cleanup() {
  local status=$? docker_cleanup_ok=1
  trap - EXIT INT TERM
  set +e
  case "$project" in
    rvc-recovery-drill-[0-9]*)
      if [[ -f $config_root/manager.env ]]; then
        if ! compose down --volumes --remove-orphans --timeout 10 >/dev/null 2>&1; then
          docker_cleanup_ok=0
          log "Docker cleanup failed for isolated project $project"
        fi
      fi
      ;;
    *)
      log "refusing Docker cleanup for unexpected project name: $project"
      ;;
  esac
  if [[ $docker_cleanup_ok == 1 ]]; then
    case "$workdir" in
      "$WORKSPACE_ROOT"/manager-volume.*) rm -r -- "$workdir" ;;
      *) log "refusing filesystem cleanup for unexpected path: $workdir" ;;
    esac
    rmdir "$WORKSPACE_ROOT" >/dev/null 2>&1 || true
    log "removed temporary Compose project and its five named volumes"
  else
    log "protected drill workspace retained for scoped cleanup: $workdir"
    [[ $status -ne 0 ]] || status=1
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

install -d -m 0755 "$install_root/bin" "$install_root/lib" "$release/infra/compose"
install -d -m 0700 "$config_root/secrets" "$backup_root" "$pre_restore_root" "$inspect_root"
install -m 0644 "$REPO_ROOT/installers/common/lib.sh" "$install_root/lib/common.sh"
install -m 0755 "$REPO_ROOT/installers/manager/backup.sh" "$install_root/bin/backup"
install -m 0755 "$REPO_ROOT/installers/manager/restore.sh" "$install_root/bin/restore"
install -m 0755 "$FIXTURE_ROOT/compose-wrapper.sh" "$install_root/bin/manager-compose"
printf '%s\n' 'drill-1.0.0' > "$release/VERSION"
cat > "$release/manifest.env" <<'MANIFEST'
PRODUCT=rvc-training-orchestrator
COMPONENT=manager
VERSION=drill-1.0.0
SCHEMA_COMPATIBILITY=drill-schema-v1
MANIFEST
chmod 0644 "$release/VERSION" "$release/manifest.env"
ln -s "releases/drill-1.0.0" "$install_root/current"

write_secret() {
  local name=$1 value=$2
  printf '%s\n' "$value" > "$config_root/secrets/$name"
  chmod 0600 "$config_root/secrets/$name"
}

write_secret postgres_password "DrillPostgresPassword${$}A1"
write_secret maintenance_postgres_password "DrillMaintenancePostgresPassword${$}M1"
write_secret mlflow_postgres_password "DrillMlflowPassword${$}B2"
write_secret minio_root_user "drillroot${$}"
write_secret minio_root_password "DrillMinioRootPassword${$}C3"
write_secret redis_password "DrillRedisPassword${$}R4"
write_secret maintenance_redis_password "DrillMaintenanceRedisPassword${$}M2"
write_secret minio_app_access_key "drillapp${$}"
write_secret minio_app_secret_key "DrillMinioAppSecret${$}D4"
write_secret maintenance_s3_access_key "drillmaintenance${$}"
write_secret maintenance_s3_secret_key "DrillMaintenanceObjectSecret${$}M3"
write_secret mlflow_s3_access_key "drillflow${$}"
write_secret mlflow_s3_secret_key "DrillMlflowObjectSecret${$}E5"

config_sentinel="configuration-must-not-be-archived-$project"
cat > "$config_root/manager.env" <<ENV
COMPOSE_PROJECT_NAME=$project
POSTGRES_DB=rvc_orchestrator
POSTGRES_USER=rvc_manager
MLFLOW_POSTGRES_DB=rvc_mlflow
MLFLOW_POSTGRES_USER=rvc_mlflow
S3_BUCKET=rvc-orchestrator
MLFLOW_S3_BUCKET=rvc-mlflow
MANAGER_SECRETS_DIR=$config_root/secrets
POSTGRES_IMAGE=postgres:16-alpine
MINIO_IMAGE=minio/minio:RELEASE.2025-04-22T22-12-26Z
MINIO_CLIENT_IMAGE=minio/mc:RELEASE.2025-04-16T18-13-26Z
REDIS_IMAGE=redis:7.4-alpine
RVC_RECOVERY_DRILL_API_IMAGE=$api_image
RVC_RECOVERY_DRILL_PYTHON_IMAGE=python:3.11-slim-bookworm
REDIS_DB=0
CONFIG_SENTINEL=$config_sentinel
ENV
chmod 0600 "$config_root/manager.env"

forbidden_values="$workdir/forbidden-values"
printf '%s\n' "$config_sentinel" > "$forbidden_values"
for secret_file in "$config_root"/secrets/*; do
  tr -d '\r\n' < "$secret_file" >> "$forbidden_values"
  printf '\n' >> "$forbidden_values"
done
chmod 0600 "$forbidden_values"
install -d -m 0700 "$workdir/config-before"
install -m 0600 "$config_root/manager.env" "$workdir/config-before/manager.env"
for secret_file in "$config_root"/secrets/*; do
  install -m 0600 "$secret_file" "$workdir/config-before/$(basename "$secret_file")"
done

database_sql() {
  local secret_path=$1 database_user=$2 database_name=$3 sql=$4
  printf '%s\n' "$sql" | compose exec -T postgres sh -eu -c '
    secret_path=$1
    database_user=$2
    database_name=$3
    export PGPASSWORD="$(tr -d "\r\n" < "$secret_path")"
    exec psql --quiet --set ON_ERROR_STOP=1 --tuples-only --no-align \
      --username "$database_user" --dbname "$database_name"
  ' _ "$secret_path" "$database_user" "$database_name"
}

database_value() {
  database_sql "$1" "$2" "$3" "$4" | tr -d '\r\n'
}

minio_action() {
  local action=$1 bucket=$2 object_key=$3 payload=${4:-}
  if [[ $action == put ]]; then
    printf '%s' "$payload" | compose run -T --rm --no-deps \
      --entrypoint /bin/sh minio-init -eu -c '
        action=$1
        bucket=$2
        object_key=$3
        root_user=$(tr -d "\r\n" < /run/secrets/minio_root_user)
        root_password=$(tr -d "\r\n" < /run/secrets/minio_root_password)
        mc alias set local http://minio:9000 "$root_user" "$root_password" >/dev/null
        mc pipe --attr "Content-Type=text/plain;X-Amz-Meta-Sha256=drill-reviewed;X-Amz-Meta-Verified=true" \
          "local/$bucket/$object_key" >/dev/null
        mc tag set "local/$bucket/$object_key" "scope=recovery" >/dev/null
      ' _ "$action" "$bucket" "$object_key"
    return
  fi
  compose run -T --rm --no-deps --entrypoint /bin/sh minio-init -eu -c '
    action=$1
    bucket=$2
    object_key=$3
    root_user=$(tr -d "\r\n" < /run/secrets/minio_root_user)
    root_password=$(tr -d "\r\n" < /run/secrets/minio_root_password)
    mc alias set local http://minio:9000 "$root_user" "$root_password" >/dev/null
    case "$action" in
      get) mc cat "local/$bucket/$object_key" ;;
      remove) mc rm --force "local/$bucket/$object_key" >/dev/null ;;
      exists) mc stat "local/$bucket/$object_key" >/dev/null ;;
      stat) mc stat --json "local/$bucket/$object_key" ;;
      tags) mc tag list --json "local/$bucket/$object_key" ;;
      *) echo "unsupported MinIO drill action" >&2; exit 2 ;;
    esac
  ' _ "$action" "$bucket" "$object_key"
}

redis_action() {
  local action=$1 key=$2 value=${3:-}
  compose exec -T redis sh -eu -c '
    action=$1
    key=$2
    value=$3
    export REDISCLI_AUTH="$(tr -d "\r\n" < /run/secrets/redis_password)"
    case "$action" in
      set) redis-cli --no-auth-warning -n 0 SET "$key" "$value" >/dev/null ;;
      get) redis-cli --no-auth-warning -n 0 GET "$key" ;;
      exists) redis-cli --no-auth-warning -n 0 EXISTS "$key" ;;
      *) exit 2 ;;
    esac
  ' _ "$action" "$key" "$value"
}

wait_for_postgres() {
  local attempt
  for (( attempt=1; attempt<=30; attempt++ )); do
    if compose exec -T postgres pg_isready -U rvc_manager -d rvc_orchestrator >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_api() {
  local attempt
  for (( attempt=1; attempt<=30; attempt++ )); do
    if compose exec -T api python -c \
      "import urllib.request; urllib.request.urlopen('http://localhost:8000/ready', timeout=2)" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

log "validating isolated Compose fixture for project $project"
compose config --quiet
log "starting real PostgreSQL and MinIO volumes"
compose up -d postgres minio redis api mlflow web proxy
wait_for_postgres || die "PostgreSQL did not become ready"
wait_for_api || die "API readiness fixture did not become ready"
compose run --rm minio-init >/dev/null
compose run --rm artifact-spool-init >/dev/null
compose run --rm dataset-ingestion-init >/dev/null

manager_marker="manager-original-$project"
mlflow_marker="mlflow-original-$project"
manager_object="manager-object-original-$project"
mlflow_object="mlflow-object-original-$project"

database_sql /run/secrets/postgres_password rvc_manager rvc_orchestrator \
  "CREATE TABLE recovery_probe (id integer PRIMARY KEY, marker text NOT NULL); INSERT INTO recovery_probe VALUES (1, '$manager_marker');"
database_sql /run/secrets/mlflow_postgres_password rvc_mlflow rvc_mlflow \
  "CREATE TABLE recovery_probe (id integer PRIMARY KEY, marker text NOT NULL); INSERT INTO recovery_probe VALUES (1, '$mlflow_marker');"
minio_action put rvc-orchestrator recovery/manager.txt "$manager_object"
minio_action put rvc-mlflow recovery/mlflow.txt "$mlflow_object"

[[ $(database_value /run/secrets/postgres_password rvc_manager rvc_orchestrator \
  'SELECT marker FROM recovery_probe WHERE id = 1;') == "$manager_marker" ]] || \
  die "Manager database seed verification failed"
[[ $(database_value /run/secrets/mlflow_postgres_password rvc_mlflow rvc_mlflow \
  'SELECT marker FROM recovery_probe WHERE id = 1;') == "$mlflow_marker" ]] || \
  die "MLflow database seed verification failed"
[[ $(minio_action get rvc-orchestrator recovery/manager.txt) == "$manager_object" ]] || \
  die "Manager object seed verification failed"
[[ $(minio_action get rvc-mlflow recovery/mlflow.txt) == "$mlflow_object" ]] || \
  die "MLflow object seed verification failed"

log "creating recovery archive from both databases and buckets"
if ! backup_output=$(RVC_BACKUP_TIMESTAMP=20000101T000000Z \
  "$install_root/bin/backup" --destination "$backup_root"); then
  printf '%s\n' "$backup_output" >&2
  die "Manager backup command failed"
fi
printf '%s\n' "$backup_output"
backup_path=$(printf '%s\n' "$backup_output" | sed -n 's/^BACKUP_PATH=//p' | tail -n 1)
[[ -n $backup_path && -d $backup_path ]] || die "backup did not publish a directory"
archive_count=$(find "$backup_path" -maxdepth 1 -type f -name '*.tar.gz' | wc -l | tr -d ' ')
[[ $archive_count == 1 ]] || die "backup directory does not contain exactly one archive"
archive=$(find "$backup_path" -maxdepth 1 -type f -name '*.tar.gz' -print)
tar -xzf "$archive" -C "$inspect_root"
for required_component in \
  databases/manager.pgdump databases/mlflow.pgdump objects/inventory.json; do
  component_path=$(find "$inspect_root" -type f -path "*/$required_component" -print -quit)
  [[ -n $component_path ]] || die "backup archive is missing component: $required_component"
done
object_payload_count=$(find "$inspect_root" -type f -path '*/objects/data/*/*.bin' | wc -l | tr -d ' ')
[[ $object_payload_count == 2 ]] || die "backup archive object inventory payload count differs"
config_or_secret_path=$(find "$inspect_root" \( -name manager.env -o -path '*/secrets/*' \) \
  -print -quit)
if [[ -n $config_or_secret_path ]]; then
  die "backup archive contains a config or secret path"
fi
if grep -aR -F -q -f "$forbidden_values" "$inspect_root"; then
  die "backup archive contains a config or secret value"
fi

log "mutating and deleting probe data before restore"
database_sql /run/secrets/postgres_password rvc_manager rvc_orchestrator \
  "UPDATE recovery_probe SET marker = 'manager-corrupt-$project' WHERE id = 1;"
database_sql /run/secrets/mlflow_postgres_password rvc_mlflow rvc_mlflow \
  'DELETE FROM recovery_probe;'
database_sql /run/secrets/postgres_password rvc_manager rvc_orchestrator \
  "CREATE TABLE unexpected_after_backup (marker text); INSERT INTO unexpected_after_backup VALUES ('future');"
database_sql /run/secrets/mlflow_postgres_password rvc_mlflow rvc_mlflow \
  "CREATE TABLE unexpected_after_backup (marker text); INSERT INTO unexpected_after_backup VALUES ('future');"
redis_action set future-job "future-queue-state-$project"
compose run --rm --no-deps --user 0:0 --entrypoint /bin/sh artifact-spool-init -eu -c '
  printf future > /var/lib/rvc-artifact-spool/verify/future.part
'
compose run --rm --no-deps --user 0:0 --entrypoint /bin/sh dataset-ingestion-init -eu -c '
  mkdir -p /var/lib/rvc-dataset-ingestion/future-job
  printf future > /var/lib/rvc-dataset-ingestion/future-job/archive.part
'
minio_action put rvc-orchestrator recovery/manager.txt "manager-object-corrupt-$project"
minio_action put rvc-orchestrator recovery/unexpected.txt "unexpected-manager-$project"
minio_action remove rvc-mlflow recovery/mlflow.txt
minio_action put rvc-mlflow recovery/unexpected.txt "unexpected-mlflow-$project"

[[ $(database_value /run/secrets/postgres_password rvc_manager rvc_orchestrator \
  'SELECT marker FROM recovery_probe WHERE id = 1;') == "manager-corrupt-$project" ]] || \
  die "Manager database mutation did not take effect"
[[ $(database_value /run/secrets/mlflow_postgres_password rvc_mlflow rvc_mlflow \
  'SELECT count(*) FROM recovery_probe;') == 0 ]] || die "MLflow row deletion did not take effect"
if minio_action exists rvc-mlflow recovery/mlflow.txt >/dev/null 2>&1; then
  die "MLflow object deletion did not take effect"
fi

log "restoring the verified archive with the default pre-restore safety backup"
if ! restore_output=$("$install_root/bin/restore" \
  --backup "$backup_path" \
  --confirm-destructive-restore \
  --pre-restore-destination "$pre_restore_root"); then
  printf '%s\n' "$restore_output" >&2
  die "Manager restore command failed"
fi
printf '%s\n' "$restore_output"

[[ $(database_value /run/secrets/postgres_password rvc_manager rvc_orchestrator \
  'SELECT count(*) FROM recovery_probe;') == 1 ]] || die "Manager row count was not restored"
[[ $(database_value /run/secrets/postgres_password rvc_manager rvc_orchestrator \
  'SELECT marker FROM recovery_probe WHERE id = 1;') == "$manager_marker" ]] || \
  die "Manager database marker was not restored"
[[ $(database_value /run/secrets/mlflow_postgres_password rvc_mlflow rvc_mlflow \
  'SELECT count(*) FROM recovery_probe;') == 1 ]] || die "MLflow row count was not restored"
[[ $(database_value /run/secrets/mlflow_postgres_password rvc_mlflow rvc_mlflow \
  'SELECT marker FROM recovery_probe WHERE id = 1;') == "$mlflow_marker" ]] || \
  die "MLflow database marker was not restored"
[[ $(database_value /run/secrets/postgres_password rvc_manager rvc_orchestrator \
  "SELECT to_regclass('public.unexpected_after_backup') IS NULL;") == t ]] || \
  die "post-backup Manager table survived exact database recreation"
[[ $(database_value /run/secrets/mlflow_postgres_password rvc_mlflow rvc_mlflow \
  "SELECT to_regclass('public.unexpected_after_backup') IS NULL;") == t ]] || \
  die "post-backup MLflow table survived exact database recreation"
[[ $(minio_action get rvc-orchestrator recovery/manager.txt) == "$manager_object" ]] || \
  die "Manager object was not restored"
[[ $(minio_action get rvc-mlflow recovery/mlflow.txt) == "$mlflow_object" ]] || \
  die "MLflow object was not restored"
if minio_action exists rvc-orchestrator recovery/unexpected.txt >/dev/null 2>&1; then
  die "unexpected Manager object survived scoped restore"
fi
if minio_action exists rvc-mlflow recovery/unexpected.txt >/dev/null 2>&1; then
  die "unexpected MLflow object survived scoped restore"
fi
[[ $(redis_action exists future-job | tr -d '\r\n') == 0 ]] || \
  die "future Redis state survived restore"
compose run --rm --no-deps --user 0:0 --entrypoint /bin/sh artifact-spool-init -eu -c '
  test ! -e /var/lib/rvc-artifact-spool/verify/future.part
' || die "future artifact verification spool state survived restore"
compose run --rm --no-deps --user 0:0 --entrypoint /bin/sh dataset-ingestion-init -eu -c '
  test -z "$(find /var/lib/rvc-dataset-ingestion -mindepth 1 -print -quit)"
' || die "future Dataset ingestion working state survived restore"
minio_action stat rvc-orchestrator recovery/manager.txt | \
  grep -q 'text/plain' || die "Manager object Content-Type metadata was not restored"
minio_action tags rvc-orchestrator recovery/manager.txt | \
  grep -q 'recovery' || die "Manager object tags were not restored"

cmp -s "$config_root/manager.env" "$workdir/config-before/manager.env" || \
  die "Manager configuration changed during recovery"
for secret_file in "$config_root"/secrets/*; do
  cmp -s "$secret_file" "$workdir/config-before/$(basename "$secret_file")" || \
    die "a Manager secret changed during recovery"
done

log "PASS: databases, object metadata, Redis, artifact spool, and Dataset work state were restored/reset"
log "PASS: archive excluded Manager config paths, secret paths, and their values"
