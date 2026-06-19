#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for library in \
  "$SCRIPT_DIR/../common/lib.sh" \
  "$SCRIPT_DIR/common/lib.sh" \
  "$SCRIPT_DIR/../lib/common.sh"; do
  if [[ -r $library ]]; then source "$library"; break; fi
done
declare -F rvc_die >/dev/null || { echo "installer common library not found" >&2; exit 1; }

INSTALL_ROOT=${RVC_INSTALL_ROOT:-/opt/rvc-orchestrator/manager}
CONFIG_ROOT=${RVC_CONFIG_ROOT:-/etc/rvc-orchestrator/manager}
destination=${RVC_MANAGER_BACKUP_ROOT:-/var/backups/rvc-orchestrator/manager}
online_inconsistent=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --destination) shift; destination=${1:?missing backup destination} ;;
    --install-root) shift; INSTALL_ROOT=${1:?missing install root} ;;
    --config-root) shift; CONFIG_ROOT=${1:?missing config root} ;;
    --online-inconsistent) online_inconsistent=1 ;;
    *) rvc_die "unknown backup option: $1" ;;
  esac
  shift
done

rvc_require_root_for_system_paths
rvc_require_command tar
[[ $destination == /* && $destination != *:* && $destination != *$'\n'* ]] || \
  rvc_die "backup destination must be an absolute path without ':' or newlines"
[[ -L $INSTALL_ROOT/current ]] || rvc_die "Manager current release symlink is missing"
[[ -r $INSTALL_ROOT/current/VERSION ]] || rvc_die "Manager release VERSION is missing"
[[ -r $CONFIG_ROOT/manager.env ]] || rvc_die "Manager environment file is missing"
compose="$INSTALL_ROOT/bin/manager-compose"
[[ -x $compose ]] || rvc_die "Manager Compose wrapper is not installed: $compose"

version=$(tr -d '\r\n' < "$INSTALL_ROOT/current/VERSION")
rvc_validate_version "$version"
timestamp=${RVC_BACKUP_TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}
[[ $timestamp =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || rvc_die "invalid backup timestamp"
backup_id="rvc-manager-backup-${version}-${timestamp}"

install -d -m 0700 "$destination"
final_dir="$destination/$backup_id"
[[ ! -e $final_dir ]] || rvc_die "backup destination already exists: $final_dir"
publish_lock="$destination/.${backup_id}.publish-lock"
if ! mkdir -m 0700 "$publish_lock" 2>/dev/null; then
  rvc_die "another backup is publishing this backup ID, or a stale lock exists: $publish_lock"
fi
staging=
published=0
maintenance=0
services_restarted=0
cleanup_staging() {
  local status=$?
  if [[ $maintenance == 1 && $services_restarted == 0 ]]; then
    set +e
    if RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
      "$compose" up -d --remove-orphans >/dev/null 2>&1; then
      services_restarted=1
    else
      rvc_warn "backup maintenance ended but Manager services could not be restarted"
      [[ $status -ne 0 ]] || status=1
    fi
    set -e
  fi
  if [[ $published == 0 && -n $staging && -d $staging ]]; then
    case "$staging" in
      "$destination"/."$backup_id".staging.*) rm -r -- "$staging" ;;
      *) rvc_warn "refusing to clean unexpected backup staging path: $staging" ;;
    esac
  fi
  if [[ -d $publish_lock ]]; then
    rmdir "$publish_lock" 2>/dev/null || rvc_warn "could not remove backup publication lock: $publish_lock"
  fi
  exit "$status"
}
trap cleanup_staging EXIT
staging=$(mktemp -d "$destination/.${backup_id}.staging.XXXXXX")
chmod 0700 "$staging"

content_parent="$staging/content"
content="$content_parent/$backup_id"
publish="$staging/publish"
install -d -m 0700 \
  "$content/databases" \
  "$content/objects" \
  "$content/metadata" \
  "$publish"

env_value() {
  local key=$1 fallback=$2 value
  value=$(awk -F= -v wanted="$key" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' \
    "$CONFIG_ROOT/manager.env")
  printf '%s' "${value:-$fallback}"
}

postgres_db=$(env_value POSTGRES_DB rvc_orchestrator)
postgres_user=$(env_value POSTGRES_USER rvc_manager)
mlflow_db=$(env_value MLFLOW_POSTGRES_DB rvc_mlflow)
mlflow_user=$(env_value MLFLOW_POSTGRES_USER rvc_mlflow)
s3_bucket=$(env_value S3_BUCKET rvc-orchestrator)
mlflow_bucket=$(env_value MLFLOW_S3_BUCKET rvc-mlflow)
for identifier in "$postgres_db" "$postgres_user" "$mlflow_db" "$mlflow_user"; do
  [[ $identifier =~ ^[A-Za-z_][A-Za-z0-9_]{0,62}$ ]] || \
    rvc_die "database configuration contains an unsafe identifier"
done
for bucket in "$s3_bucket" "$mlflow_bucket"; do
  [[ $bucket =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || \
    rvc_die "object storage configuration contains an unsafe bucket"
done

canonical_schema_set() {
  awk 'NF {print $1}' | LC_ALL=C sort -u | paste -sd, -
}

assert_no_active_upload_sessions() {
  local table exists active
  for table in dataset_upload_sessions artifact_upload_sessions; do
    exists=$(RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
      "$compose" exec -T postgres sh -eu -c '
        table=$1
        export PGPASSWORD="$(cat /run/secrets/postgres_password)"
        psql --quiet --tuples-only --no-align --username "$POSTGRES_USER" \
          --dbname "$POSTGRES_DB" \
          --command "SELECT to_regclass('"'"'public.$table'"'"') IS NOT NULL;"
      ' _ "$table" | tr -d '\r\n ')
    case "$exists" in
      f) continue ;;
      t) ;;
      *) rvc_die "could not determine whether $table is present" ;;
    esac
    active=$(RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
      "$compose" exec -T postgres sh -eu -c '
        table=$1
        export PGPASSWORD="$(cat /run/secrets/postgres_password)"
        psql --quiet --tuples-only --no-align --username "$POSTGRES_USER" \
          --dbname "$POSTGRES_DB" \
          --command "SELECT count(*) FROM \"$table\" WHERE status IN ('"'"'pending'"'"', '"'"'finalizing'"'"');"
      ' _ "$table" | tr -d '\r\n ')
    [[ $active =~ ^[0-9]+$ ]] || rvc_die "could not count active upload sessions in $table"
    [[ $active == 0 ]] || \
      rvc_die "backup maintenance found $active active sessions in $table; finish or expire them before retrying"
  done
}

consistency_mode=online-inconsistent-explicit
if [[ $online_inconsistent == 0 ]]; then
  maintenance=1
  consistency_mode=maintenance-quiesced
  rvc_log "entering maintenance mode for a cross-store-consistent backup"
  RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
    "$compose" stop proxy web api rq-worker mlflow api-migrate
  assert_no_active_upload_sessions
else
  rvc_warn "online inconsistent backup explicitly selected; databases and objects may represent different times"
fi

schema_current=$(RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" run --rm --no-deps api alembic -c /app/alembic.ini current | \
  canonical_schema_set)
schema_head=$(RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" run --rm --no-deps api alembic -c /app/alembic.ini heads | \
  canonical_schema_set)
[[ -n $schema_current && -n $schema_head ]] || rvc_die "Alembic schema metadata is unavailable"
[[ $schema_current == "$schema_head" ]] || \
  rvc_die "Manager database is not at the complete installed Alembic head set"

rvc_log "creating PostgreSQL custom dump for Manager database"
RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" exec -T postgres sh -eu -c '
    export PGPASSWORD="$(cat /run/secrets/postgres_password)"
    exec pg_dump --format=custom --no-owner --no-privileges \
      --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"
  ' > "$content/databases/manager.pgdump"
[[ -s $content/databases/manager.pgdump ]] || rvc_die "Manager database dump is empty"

rvc_log "creating PostgreSQL custom dump for MLflow database"
RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" exec -T postgres sh -eu -c '
    export PGPASSWORD="$(cat "$MLFLOW_POSTGRES_PASSWORD_FILE")"
    exec pg_dump --format=custom --no-owner --no-privileges \
      --username "$MLFLOW_POSTGRES_USER" --dbname "$MLFLOW_POSTGRES_DB"
  ' > "$content/databases/mlflow.pgdump"
[[ -s $content/databases/mlflow.pgdump ]] || rvc_die "MLflow database dump is empty"

rvc_log "snapshotting Manager and MLflow object bytes, metadata, tags, and headers"
RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" run --rm --no-deps \
  --volume "$content/objects:/snapshot" \
  object-recovery backup \
  --root /snapshot \
  --access-key-file /run/secrets/minio_root_user \
  --secret-key-file /run/secrets/minio_root_password \
  --bucket "manager=$s3_bucket" \
  --bucket "mlflow=$mlflow_bucket"

services=$(RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" config --services | tr '\r\n ' ',,' | sed 's/,,*/,/g; s/,$//')
compatibility=unknown
if [[ -r $INSTALL_ROOT/current/manifest.env ]]; then
  compatibility=$(rvc_manifest_value "$INSTALL_ROOT/current" SCHEMA_COMPATIBILITY || true)
  compatibility=${compatibility:-unknown}
fi
[[ $compatibility =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || \
  rvc_die "installed release has an invalid schema compatibility marker"
[[ $schema_current =~ ^[A-Za-z0-9._(),-]+$ && $schema_head =~ ^[A-Za-z0-9._(),-]+$ ]] || \
  rvc_die "Alembic schema metadata contains unsafe characters"
[[ $services =~ ^[A-Za-z0-9_-]+(,[A-Za-z0-9_-]+)*$ ]] || \
  rvc_die "Compose service metadata contains unsafe characters"

cat > "$content/manifest.env" <<MANIFEST
BACKUP_FORMAT_VERSION=1
PRODUCT=rvc-training-orchestrator
COMPONENT=manager
BACKUP_ID=$backup_id
CREATED_AT=$timestamp
SOURCE_VERSION=$version
SCHEMA_COMPATIBILITY=$compatibility
SCHEMA_CURRENT=$schema_current
SCHEMA_HEAD=$schema_head
POSTGRES_DB=$postgres_db
POSTGRES_USER=$postgres_user
MLFLOW_POSTGRES_DB=$mlflow_db
MLFLOW_POSTGRES_USER=$mlflow_user
S3_BUCKET=$s3_bucket
MLFLOW_S3_BUCKET=$mlflow_bucket
COMPOSE_SERVICES=$services
CONSISTENCY_MODE=$consistency_mode
INCLUDES_CONFIG=false
INCLUDES_SECRETS=false
DATABASE_DUMP_FORMAT=postgresql-custom
OBJECT_SNAPSHOT_FORMAT=s3-object-inventory-v1
OBJECT_VERSION_SEMANTICS=unversioned-current-object
MANIFEST
chmod 0600 "$content/manifest.env" "$content/databases/manager.pgdump" \
  "$content/databases/mlflow.pgdump"
chmod -R go-rwx "$content"

(
  cd "$content"
  find . -type f ! -name SHA256SUMS | LC_ALL=C sort | while read -r file; do
    hash=$(rvc_sha256_file "$file")
    printf '%s  %s\n' "$hash" "${file#./}"
  done > SHA256SUMS
)
chmod 0600 "$content/SHA256SUMS"

archive="$publish/$backup_id.tar.gz"
COPYFILE_DISABLE=1 tar -C "$content_parent" -czf "$archive" "$backup_id"
chmod 0600 "$archive"
archive_hash=$(rvc_sha256_file "$archive")
printf '%s  %s\n' "$archive_hash" "$(basename "$archive")" > "$archive.sha256"
chmod 0600 "$archive.sha256"
tar -tzf "$archive" >/dev/null

if [[ $maintenance == 1 ]]; then
  rvc_log "leaving backup maintenance mode"
  RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
    "$compose" up -d --remove-orphans
  services_restarted=1
fi

rm -r -- "$content_parent"
[[ ! -e $final_dir ]] || rvc_die "backup destination appeared during publication: $final_dir"
mv "$publish" "$final_dir"
published=1
rmdir "$staging"
chmod 0700 "$final_dir"
rvc_log "Manager backup published atomically: $final_dir"
printf 'BACKUP_PATH=%s\n' "$final_dir"
