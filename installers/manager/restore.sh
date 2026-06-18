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
backup_path=
confirmation=
skip_pre_restore_backup=0
allow_version_mismatch=0
allow_online_inconsistent_backup=0
pre_restore_destination=${RVC_MANAGER_BACKUP_ROOT:-/var/backups/rvc-orchestrator/manager}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup) shift; backup_path=${1:?missing backup path} ;;
    --confirm-destructive-restore) confirmation=confirmed ;;
    --skip-pre-restore-backup) skip_pre_restore_backup=1 ;;
    --pre-restore-destination) shift; pre_restore_destination=${1:?missing destination} ;;
    --allow-version-mismatch) allow_version_mismatch=1 ;;
    --allow-online-inconsistent-backup) allow_online_inconsistent_backup=1 ;;
    --install-root) shift; INSTALL_ROOT=${1:?missing install root} ;;
    --config-root) shift; CONFIG_ROOT=${1:?missing config root} ;;
    *) rvc_die "unknown restore option: $1" ;;
  esac
  shift
done

rvc_require_root_for_system_paths
rvc_require_command python3
[[ -n $backup_path ]] || rvc_die "--backup is required"
[[ $backup_path == /* && $backup_path != *$'\n'* ]] || \
  rvc_die "backup path must be absolute and contain no newlines"
[[ $confirmation == confirmed ]] || \
  rvc_die "restore replaces Manager databases and bucket contents; pass --confirm-destructive-restore exactly"
[[ -L $INSTALL_ROOT/current ]] || rvc_die "Manager current release symlink is missing"
[[ -r $INSTALL_ROOT/current/VERSION ]] || rvc_die "Manager release VERSION is missing"
[[ -r $CONFIG_ROOT/manager.env ]] || rvc_die "Manager environment file is missing"
compose="$INSTALL_ROOT/bin/manager-compose"
backup_command="$INSTALL_ROOT/bin/backup"
[[ -x $compose ]] || rvc_die "Manager Compose wrapper is not installed: $compose"
if [[ $skip_pre_restore_backup == 0 ]]; then
  [[ -x $backup_command ]] || rvc_die "pre-restore backup command is not installed: $backup_command"
fi

archive_helper=
for candidate in \
  "$SCRIPT_DIR/recovery_archive.py" \
  "$SCRIPT_DIR/../lib/recovery_archive.py"; do
  if [[ -f $candidate && ! -L $candidate ]]; then archive_helper=$candidate; break; fi
done
[[ -n $archive_helper ]] || rvc_die "recovery archive verifier is not installed"

max_archive_bytes=${RVC_RESTORE_MAX_ARCHIVE_BYTES:-536870912000}
max_unpacked_bytes=${RVC_RESTORE_MAX_UNPACKED_BYTES:-1099511627776}
max_members=${RVC_RESTORE_MAX_MEMBERS:-1000000}
reserve_bytes=${RVC_RESTORE_RESERVE_BYTES:-1073741824}
reserve_inodes=${RVC_RESTORE_RESERVE_INODES:-4096}
for value in "$max_archive_bytes" "$max_unpacked_bytes" "$max_members" \
  "$reserve_bytes" "$reserve_inodes"; do
  [[ $value =~ ^[1-9][0-9]*$ ]] || rvc_die "restore archive limits must be positive integers"
done

staging=$(mktemp -d "${TMPDIR:-/tmp}/rvc-manager-restore.XXXXXX")
chmod 0700 "$staging"
install -d -m 0700 "$staging/input" "$staging/extracted"
maintenance=0
pre_restore_backup=
finish_restore() {
  local status=$?
  if [[ $status -ne 0 && $maintenance == 1 ]]; then
    set +e
    RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
      "$compose" stop proxy web api rq-worker mlflow api-migrate redis >/dev/null 2>&1
    rvc_warn "restore failed; Manager write services were left stopped for recovery; Redis was also left stopped"
    if [[ -n $pre_restore_backup ]]; then
      printf -v quoted_pre_restore_backup '%q' "$pre_restore_backup"
      rvc_warn "after diagnosing the cause, recover with: $INSTALL_ROOT/bin/restore --backup $quoted_pre_restore_backup --confirm-destructive-restore --skip-pre-restore-backup"
    else
      rvc_warn "repair the failed component or restore a verified backup before restarting Manager services"
    fi
  fi
  case "$staging" in
    "${TMPDIR:-/tmp}"/rvc-manager-restore.*) rm -r -- "$staging" ;;
    *) rvc_warn "refusing to clean unexpected restore staging path: $staging" ;;
  esac
  exit "$status"
}
trap finish_restore EXIT

source_archive=
source_outer_checksum=
if [[ -d $backup_path && ! -L $backup_path ]]; then
  shopt -s nullglob
  archives=("$backup_path"/*.tar.gz)
  shopt -u nullglob
  (( ${#archives[@]} == 1 )) || rvc_die "backup directory must contain exactly one .tar.gz archive"
  source_archive=${archives[0]}
  source_outer_checksum="$source_archive.sha256"
elif [[ -f $backup_path && ! -L $backup_path && $backup_path == *.tar.gz ]]; then
  source_archive=$backup_path
  source_outer_checksum="$source_archive.sha256"
else
  rvc_die "backup must be a non-symlink .tar.gz file or published backup directory"
fi
[[ -f $source_archive && ! -L $source_archive ]] || \
  rvc_die "backup archive is missing or unsafe: $source_archive"
[[ -f $source_outer_checksum && ! -L $source_outer_checksum ]] || \
  rvc_die "backup archive checksum is missing or unsafe: $source_outer_checksum"

archive="$staging/input/$(basename "$source_archive")"
outer_checksum="$staging/input/$(basename "$source_outer_checksum")"
python3 "$archive_helper" snapshot \
  --source "$source_archive" --destination "$archive" --max-bytes "$max_archive_bytes" \
  >/dev/null
python3 "$archive_helper" snapshot \
  --source "$source_outer_checksum" --destination "$outer_checksum" --max-bytes 1048576 \
  >/dev/null

read -r expected_hash checksum_name checksum_extra < "$outer_checksum" || \
  rvc_die "could not read backup archive checksum"
checksum_name=${checksum_name#\*}
[[ -z ${checksum_extra:-} && $expected_hash =~ ^[a-fA-F0-9]{64}$ ]] || \
  rvc_die "invalid backup archive checksum file"
[[ $checksum_name == "$(basename "$archive")" ]] || \
  rvc_die "backup archive checksum names an unexpected file"
actual_hash=$(rvc_sha256_file "$archive")
[[ $actual_hash == "$expected_hash" ]] || rvc_die "backup archive checksum mismatch"

backup_id=$(basename "$archive" .tar.gz)
[[ $backup_id =~ ^rvc-manager-backup-[A-Za-z0-9][A-Za-z0-9._-]{0,63}-[0-9]{8}T[0-9]{6}Z$ ]] || \
  rvc_die "backup archive has an invalid name"

python3 "$archive_helper" extract \
  --archive "$archive" \
  --destination "$staging/extracted" \
  --expected-root "$backup_id" \
  --max-members "$max_members" \
  --max-unpacked-bytes "$max_unpacked_bytes" \
  --reserve-bytes "$reserve_bytes" \
  --reserve-inodes "$reserve_inodes" >/dev/null
content="$staging/extracted/$backup_id"
manifest="$content/manifest.env"
sums="$content/SHA256SUMS"
[[ -d $content && ! -L $content && -f $manifest && ! -L $manifest ]] || \
  rvc_die "backup component manifest is missing or unsafe"
[[ -f $sums && ! -L $sums ]] || rvc_die "backup component checksums are missing or unsafe"

while IFS= read -r checksum_line; do
  file_hash=${checksum_line%%  *}
  file_path=${checksum_line#*  }
  [[ -n $file_hash && -n $file_path && $checksum_line == "$file_hash  $file_path" ]] || \
    rvc_die "invalid entry in backup component checksums"
  file_path=${file_path#\*}
  [[ $file_hash =~ ^[a-fA-F0-9]{64}$ ]] || rvc_die "invalid component checksum"
  case "$file_path" in
    /*|..|../*|*/../*|*\\*|SHA256SUMS) rvc_die "unsafe path in component checksums" ;;
  esac
  [[ -f "$content/$file_path" && ! -L "$content/$file_path" ]] || \
    rvc_die "backup component file is missing or unsafe: $file_path"
  [[ $(rvc_sha256_file "$content/$file_path") == "$file_hash" ]] || \
    rvc_die "backup component checksum mismatch: $file_path"
done < "$sums"

while IFS= read -r extracted_file; do
  relative=${extracted_file#"$content/"}
  [[ $relative == SHA256SUMS ]] && continue
  awk -v wanted="$relative" 'substr($0, 67) == wanted {found=1} END {exit !found}' "$sums" || \
    rvc_die "backup component file is not covered by checksums: $relative"
done < <(find "$content" -type f | LC_ALL=C sort)

manifest_value() {
  local key=$1 count value
  count=$(awk -F= -v wanted="$key" '$1 == wanted {count++} END {print count+0}' "$manifest")
  [[ $count == 1 ]] || rvc_die "backup manifest must contain exactly one $key"
  value=$(awk -F= -v wanted="$key" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' "$manifest")
  [[ $value != *$'\n'* && $value != *$'\r'* ]] || rvc_die "backup manifest contains an unsafe $key"
  printf '%s' "$value"
}

[[ $(manifest_value BACKUP_FORMAT_VERSION) == 1 ]] || rvc_die "unsupported backup format"
[[ $(manifest_value PRODUCT) == rvc-training-orchestrator ]] || rvc_die "backup product does not match"
[[ $(manifest_value COMPONENT) == manager ]] || rvc_die "backup component does not match Manager"
[[ $(manifest_value BACKUP_ID) == "$backup_id" ]] || rvc_die "backup ID does not match archive"
created_at=$(manifest_value CREATED_AT)
[[ $backup_id == *"-$created_at" && $created_at =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || \
  rvc_die "backup creation metadata does not match archive"
[[ $(manifest_value INCLUDES_CONFIG) == false && $(manifest_value INCLUDES_SECRETS) == false ]] || \
  rvc_die "backup manifest has unexpected configuration or secret semantics"
[[ $(manifest_value DATABASE_DUMP_FORMAT) == postgresql-custom ]] || rvc_die "unsupported database dump format"
[[ $(manifest_value OBJECT_SNAPSHOT_FORMAT) == s3-object-inventory-v1 ]] || \
  rvc_die "unsupported object snapshot format"
[[ $(manifest_value OBJECT_VERSION_SEMANTICS) == unversioned-current-object ]] || \
  rvc_die "unsupported object version semantics"
consistency_mode=$(manifest_value CONSISTENCY_MODE)
case "$consistency_mode" in
  maintenance-quiesced) ;;
  online-inconsistent-explicit)
    [[ $allow_online_inconsistent_backup == 1 ]] || \
      rvc_die "backup was explicitly created without cross-store consistency; pass --allow-online-inconsistent-backup only after reviewing that risk"
    ;;
  *) rvc_die "backup consistency mode is invalid" ;;
esac
source_version=$(manifest_value SOURCE_VERSION)
rvc_validate_version "$source_version"
schema_compatibility=$(manifest_value SCHEMA_COMPATIBILITY)
schema_at_backup=$(manifest_value SCHEMA_CURRENT)
schema_head_at_backup=$(manifest_value SCHEMA_HEAD)
[[ $schema_compatibility =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || \
  rvc_die "backup schema compatibility metadata is invalid"
[[ $schema_at_backup =~ ^[A-Za-z0-9._(),-]+$ && \
   $schema_head_at_backup =~ ^[A-Za-z0-9._(),-]+$ ]] || \
  rvc_die "backup schema revision metadata is invalid"
[[ $schema_at_backup == "$schema_head_at_backup" ]] || \
  rvc_die "backup database was not at its complete source Alembic head set"
compose_services=$(manifest_value COMPOSE_SERVICES)
[[ $compose_services =~ ^[A-Za-z0-9_-]+(,[A-Za-z0-9_-]+)*$ ]] || \
  rvc_die "backup service metadata is invalid"
for required_service in postgres minio mlflow api; do
  case ",$compose_services," in
    *",$required_service,"*) ;;
    *) rvc_die "backup service metadata is missing $required_service" ;;
  esac
done
current_version=$(tr -d '\r\n' < "$INSTALL_ROOT/current/VERSION")
rvc_validate_version "$current_version"
if [[ $source_version != "$current_version" && $allow_version_mismatch == 0 ]]; then
  rvc_die "backup version $source_version does not match installed version $current_version; review compatibility and pass --allow-version-mismatch explicitly"
fi

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
redis_db=$(env_value REDIS_DB 0)
for identifier in "$postgres_db" "$postgres_user" "$mlflow_db" "$mlflow_user"; do
  [[ $identifier =~ ^[A-Za-z_][A-Za-z0-9_]{0,62}$ ]] || \
    rvc_die "database configuration contains an unsafe identifier"
done
[[ $redis_db =~ ^[0-9]{1,4}$ && $redis_db -le 1023 ]] || \
  rvc_die "Redis database configuration is unsafe"
for bucket in "$s3_bucket" "$mlflow_bucket"; do
  [[ $bucket =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || \
    rvc_die "object storage configuration contains an unsafe bucket"
done
[[ $(manifest_value POSTGRES_DB) == "$postgres_db" && \
   $(manifest_value POSTGRES_USER) == "$postgres_user" && \
   $(manifest_value MLFLOW_POSTGRES_DB) == "$mlflow_db" && \
   $(manifest_value MLFLOW_POSTGRES_USER) == "$mlflow_user" ]] || \
  rvc_die "backup database component names do not match this installation"
[[ $(manifest_value S3_BUCKET) == "$s3_bucket" && \
   $(manifest_value MLFLOW_S3_BUCKET) == "$mlflow_bucket" ]] || \
  rvc_die "backup object bucket names do not match this installation"
[[ -s $content/databases/manager.pgdump && -s $content/databases/mlflow.pgdump ]] || \
  rvc_die "backup database components are missing or empty"
[[ -d $content/objects && ! -L $content/objects && \
   -f $content/objects/inventory.json && ! -L $content/objects/inventory.json ]] || \
  rvc_die "backup object inventory is missing or unsafe"
object_snapshot_helper=
for candidate in \
  "$SCRIPT_DIR/../../infra/runtime/recovery_object_snapshot.py" \
  "$INSTALL_ROOT/current/infra/runtime/recovery_object_snapshot.py"; do
  if [[ -f $candidate && ! -L $candidate ]]; then object_snapshot_helper=$candidate; break; fi
done
[[ -n $object_snapshot_helper ]] || rvc_die "object snapshot verifier is not installed"
python3 "$object_snapshot_helper" verify \
  --root "$content/objects" \
  --bucket "manager=$s3_bucket" \
  --bucket "mlflow=$mlflow_bucket" >/dev/null

canonical_schema_set() {
  awk 'NF {print $1}' | LC_ALL=C sort -u | paste -sd, -
}
installed_schema_heads=$(RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" run --rm --no-deps api alembic -c /app/alembic.ini heads | \
  canonical_schema_set)
[[ -n $installed_schema_heads && $installed_schema_heads =~ ^[A-Za-z0-9._-]+(,[A-Za-z0-9._-]+)*$ ]] || \
  rvc_die "installed release Alembic head set is unavailable or unsafe"

if [[ $skip_pre_restore_backup == 0 ]]; then
  rvc_log "creating mandatory pre-restore safety backup"
  pre_output=$(RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
    "$backup_command" --destination "$pre_restore_destination")
  printf '%s\n' "$pre_output"
  pre_restore_backup=$(printf '%s\n' "$pre_output" | sed -n 's/^BACKUP_PATH=//p' | tail -n 1)
  [[ -n $pre_restore_backup && -d $pre_restore_backup ]] || \
    rvc_die "pre-restore backup did not publish a recovery path"
fi

rvc_log "entering maintenance mode before destructive restore"
maintenance=1
RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" stop proxy web api rq-worker mlflow api-migrate

rvc_log "flushing transient Redis queue/cache state before restoring PostgreSQL"
RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" exec -T redis sh -eu -c '
    redis_db=$1
    export REDISCLI_AUTH="$(tr -d "\r\n" < /run/secrets/redis_password)"
    exec redis-cli --no-auth-warning -n "$redis_db" FLUSHDB SYNC
  ' _ "$redis_db"
RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" stop redis
RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" run --rm --no-deps --user 0:0 \
  --entrypoint /bin/sh artifact-spool-init -eu -c '
    find /var/lib/rvc-artifact-spool/verify -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
  '
RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" run --rm --no-deps --user 0:0 \
  --entrypoint /bin/sh dataset-ingestion-init -eu -c '
    find /var/lib/rvc-dataset-ingestion -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
  '

rvc_log "recreating empty Manager and MLflow databases for an exact restore"
RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" exec -T postgres sh -eu -c '
    export PGPASSWORD="$(cat /run/secrets/postgres_password)"
    dropdb --if-exists --force --username "$POSTGRES_USER" "$POSTGRES_DB"
    createdb --owner "$POSTGRES_USER" --username "$POSTGRES_USER" "$POSTGRES_DB"
    dropdb --if-exists --force --username "$POSTGRES_USER" "$MLFLOW_POSTGRES_DB"
    createdb --owner "$MLFLOW_POSTGRES_USER" --username "$POSTGRES_USER" "$MLFLOW_POSTGRES_DB"
  '

rvc_log "restoring Manager PostgreSQL database"
RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" exec -T postgres sh -eu -c '
    export PGPASSWORD="$(cat /run/secrets/postgres_password)"
    exec pg_restore --no-owner --no-privileges \
      --exit-on-error --single-transaction \
      --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"
  ' < "$content/databases/manager.pgdump"

rvc_log "restoring MLflow PostgreSQL database"
RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" exec -T postgres sh -eu -c '
    export PGPASSWORD="$(cat "$MLFLOW_POSTGRES_PASSWORD_FILE")"
    exec pg_restore --no-owner --no-privileges \
      --exit-on-error --single-transaction \
      --username "$MLFLOW_POSTGRES_USER" --dbname "$MLFLOW_POSTGRES_DB"
  ' < "$content/databases/mlflow.pgdump"

rvc_log "verifying the restored database revision set before migration"
restored_schema_set=$(RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" run --rm --no-deps api alembic -c /app/alembic.ini current | \
  canonical_schema_set)
[[ $restored_schema_set == "$schema_at_backup" ]] || \
  rvc_die "restored database revision set differs from the backup manifest"

rvc_log "restoring and verifying Manager and MLflow object metadata inventories"
RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" run --rm --no-deps \
  --volume "$content/objects:/snapshot:ro" \
  object-recovery restore \
  --root /snapshot \
  --access-key-file /run/secrets/minio_root_user \
  --secret-key-file /run/secrets/minio_root_password \
  --bucket "manager=$s3_bucket" \
  --bucket "mlflow=$mlflow_bucket"

rvc_log "migrating restored Manager schema to the installed release head"
RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" run --rm --no-deps api alembic -c /app/alembic.ini upgrade heads
schema_current=$(RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" run --rm --no-deps api alembic -c /app/alembic.ini current | \
  canonical_schema_set)
[[ -n $schema_current && $schema_current == "$installed_schema_heads" ]] || \
  rvc_die "restored schema did not reach the installed release head"

RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" up -d --remove-orphans
ready=0
for (( attempt=1; attempt<=${RVC_RESTORE_READY_ATTEMPTS:-30}; attempt++ )); do
  if RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
    "$compose" ps --format json | python3 -c '
import json
import sys

payload = sys.stdin.read().strip()
try:
    parsed = json.loads(payload)
    records = parsed if isinstance(parsed, list) else [parsed]
except json.JSONDecodeError:
    records = [json.loads(line) for line in payload.splitlines() if line.strip()]
services = {str(item.get("Service", "")): item for item in records if isinstance(item, dict)}
required = {"postgres", "redis", "minio", "mlflow", "api", "web", "proxy"}
if set(services).intersection(required) != required:
    raise SystemExit(1)
for name in required:
    state = str(services[name].get("State", "")).lower()
    health = str(services[name].get("Health", "")).lower()
    if state != "running" or health != "healthy":
        raise SystemExit(1)
' >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep "${RVC_RESTORE_READY_INTERVAL_SECONDS:-2}"
done
[[ $ready == 1 ]] || rvc_die "Manager PostgreSQL, Redis, MinIO, MLflow, API, Web, or proxy did not become healthy after restore"

maintenance=0
rvc_log "Manager restore completed and readiness was verified"
[[ -z $pre_restore_backup ]] || printf 'PRE_RESTORE_BACKUP_PATH=%s\n' "$pre_restore_backup"
