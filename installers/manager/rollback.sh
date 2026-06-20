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
target_version=
allow_schema_mismatch=0
schema_mismatch_confirmation=
pre_rollback_destination=${RVC_MANAGER_BACKUP_ROOT:-/var/backups/rvc-orchestrator/manager}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --to-version) shift; target_version=${1:?missing rollback version} ;;
    --allow-schema-mismatch) allow_schema_mismatch=1 ;;
    --confirm-schema-mismatch-risk) shift; schema_mismatch_confirmation=${1:?missing confirmation} ;;
    --pre-rollback-backup-destination) shift; pre_rollback_destination=${1:?missing destination} ;;
    --install-root) shift; INSTALL_ROOT=${1:?missing install root} ;;
    --config-root) shift; CONFIG_ROOT=${1:?missing config root} ;;
    *) rvc_die "unknown rollback option: $1" ;;
  esac
  shift
done

rvc_require_root_for_system_paths
[[ -n $target_version ]] || rvc_die "--to-version is required"
rvc_validate_version "$target_version"
[[ -L $INSTALL_ROOT/current ]] || rvc_die "Manager current release symlink is missing"
manager_env="$CONFIG_ROOT/manager.env"
[[ -f $manager_env && ! -L $manager_env && -r $manager_env ]] || \
  rvc_die "Manager environment file is missing or unsafe"
compose="$INSTALL_ROOT/bin/manager-compose"
[[ -x $compose ]] || rvc_die "Manager Compose wrapper is not installed: $compose"
release_checksum_verifier="$INSTALL_ROOT/lib/image_bundle.py"
[[ -f $release_checksum_verifier && ! -L $release_checksum_verifier ]] || \
  rvc_die "installed release checksum verifier is missing or unsafe"

current_release=$(cd "$INSTALL_ROOT/current" && pwd -P)
case "$current_release" in
  "$INSTALL_ROOT"/releases/*) ;;
  *) rvc_die "current symlink resolves outside the Manager release directory" ;;
esac
[[ -r $current_release/VERSION ]] || rvc_die "current release VERSION is missing"
current_version=$(tr -d '\r\n' < "$current_release/VERSION")
rvc_validate_version "$current_version"
[[ $target_version != "$current_version" ]] || rvc_die "target version is already current"

target_release="$INSTALL_ROOT/releases/$target_version"
[[ -d $target_release && ! -L $target_release ]] || rvc_die "target release is not installed: $target_release"
[[ -r $target_release/VERSION ]] || rvc_die "target release VERSION is missing"
[[ $(tr -d '\r\n' < "$target_release/VERSION") == "$target_version" ]] || \
  rvc_die "target release VERSION does not match its directory"

verify_release() {
  local release=$1 expected_version=$2 manifest sums expected file extra actual relative
  manifest="$release/manifest.env"
  sums="$release/RELEASE_SHA256SUMS"
  [[ -f $manifest && ! -L $manifest ]] || rvc_die "release manifest is missing or unsafe: $release"
  [[ -f $sums && ! -L $sums ]] || rvc_die "release checksums are missing or unsafe: $release"
  rvc_verify_release_checksums "$release" "$release_checksum_verifier"
  [[ $(rvc_manifest_value "$release" PRODUCT) == rvc-training-orchestrator ]] || \
    rvc_die "release product does not match"
  [[ $(rvc_manifest_value "$release" COMPONENT) == manager ]] || \
    rvc_die "release component does not match Manager"
  [[ $(rvc_manifest_value "$release" VERSION) == "$expected_version" ]] || \
    rvc_die "release manifest version does not match $expected_version"
  while read -r expected file extra; do
    [[ -n ${expected:-} && -n ${file:-} ]] || continue
    [[ -z ${extra:-} ]] || rvc_die "release checksum line contains unexpected fields"
    file=${file#\*}
    [[ $expected =~ ^[a-fA-F0-9]{64}$ ]] || rvc_die "invalid release checksum"
    case "$file" in
      /*|..|../*|*/../*|*\\*|RELEASE_SHA256SUMS) rvc_die "unsafe release checksum path: $file" ;;
    esac
    [[ -f "$release/$file" && ! -L "$release/$file" ]] || \
      rvc_die "release file is missing or unsafe: $file"
    actual=$(rvc_sha256_file "$release/$file")
    [[ $actual == "$expected" ]] || rvc_die "release checksum mismatch: $file"
  done < "$sums"
  awk '{path=$2; sub(/^\*/, "", path); if (seen[path]++) exit 1}' "$sums" || \
    rvc_die "release checksums contain a duplicate path"
  while IFS= read -r file; do
    relative=${file#"$release/"}
    [[ $relative == RELEASE_SHA256SUMS ]] && continue
    awk -v wanted="$relative" '{path=$2; sub(/^\*/, "", path); if (path == wanted) found=1} END {exit !found}' \
      "$sums" || \
      rvc_die "release regular file is not covered by checksums: $relative"
  done < <(find "$release" -type f | LC_ALL=C sort)
  for file in VERSION manifest.env infra/compose/manager.compose.yml; do
    awk -v wanted="$file" '$2 == wanted {found=1} END {exit !found}' "$sums" || \
      rvc_die "release checksums do not cover required file: $file"
  done
}

verify_release "$current_release" "$current_version"
verify_release "$target_release" "$target_version"
current_compatibility=$(rvc_manifest_value "$current_release" SCHEMA_COMPATIBILITY || true)
target_compatibility=$(rvc_manifest_value "$target_release" SCHEMA_COMPATIBILITY || true)
for marker in "$current_compatibility" "$target_compatibility"; do
  [[ $marker =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || \
    rvc_die "release has a missing or invalid schema compatibility marker"
done
marker_mismatch=0
if [[ $current_compatibility == unknown || $target_compatibility == unknown || \
      $current_compatibility != "$target_compatibility" ]]; then
  marker_mismatch=1
fi

switch_current() {
  local version=$1 temporary="$INSTALL_ROOT/.current.rollback.$$"
  rm -f -- "$temporary"
  ln -s "releases/$version" "$temporary"
  if mv -Tf "$temporary" "$INSTALL_ROOT/current" 2>/dev/null; then
    return 0
  fi
  if mv -hf "$temporary" "$INSTALL_ROOT/current" 2>/dev/null; then
    return 0
  fi
  rm -f -- "$temporary"
  return 1
}

release_images() {
  local release=$1 version=$2
  RELEASE_API_IMAGE=$(rvc_manifest_value "$release" API_IMAGE || true)
  RELEASE_WEB_IMAGE=$(rvc_manifest_value "$release" WEB_IMAGE || true)
  RELEASE_MLFLOW_IMAGE=$(rvc_manifest_value "$release" MLFLOW_IMAGE || true)
  RELEASE_POSTGRES_IMAGE=$(rvc_manifest_value "$release" POSTGRES_IMAGE || true)
  RELEASE_REDIS_IMAGE=$(rvc_manifest_value "$release" REDIS_IMAGE || true)
  RELEASE_MINIO_IMAGE=$(rvc_manifest_value "$release" MINIO_IMAGE || true)
  RELEASE_MINIO_CLIENT_IMAGE=$(rvc_manifest_value "$release" MINIO_CLIENT_IMAGE || true)
  RELEASE_NGINX_IMAGE=$(rvc_manifest_value "$release" NGINX_IMAGE || true)
  RELEASE_API_IMAGE=${RELEASE_API_IMAGE:-rvc-orchestrator-api:$version}
  RELEASE_WEB_IMAGE=${RELEASE_WEB_IMAGE:-rvc-orchestrator-web:$version}
  RELEASE_MLFLOW_IMAGE=${RELEASE_MLFLOW_IMAGE:-rvc-orchestrator-mlflow:$version}
  RELEASE_POSTGRES_IMAGE=${RELEASE_POSTGRES_IMAGE:-postgres:16-alpine}
  RELEASE_REDIS_IMAGE=${RELEASE_REDIS_IMAGE:-redis:7.4-alpine}
  RELEASE_MINIO_IMAGE=${RELEASE_MINIO_IMAGE:-minio/minio:RELEASE.2025-04-22T22-12-26Z}
  RELEASE_MINIO_CLIENT_IMAGE=${RELEASE_MINIO_CLIENT_IMAGE:-minio/mc:RELEASE.2025-04-16T18-13-26Z}
  RELEASE_NGINX_IMAGE=${RELEASE_NGINX_IMAGE:-nginx:1.27-alpine}
  if [[ $(rvc_manifest_value "$release" SELF_CONTAINED || true) == true ]]; then
    RELEASE_PULL_POLICY=never
  else
    RELEASE_PULL_POLICY=missing
  fi
  for image in "$RELEASE_API_IMAGE" "$RELEASE_WEB_IMAGE" "$RELEASE_MLFLOW_IMAGE" \
    "$RELEASE_POSTGRES_IMAGE" "$RELEASE_REDIS_IMAGE" "$RELEASE_MINIO_IMAGE" \
    "$RELEASE_MINIO_CLIENT_IMAGE" "$RELEASE_NGINX_IMAGE"; do
    [[ $image =~ ^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}$ ]] || \
      rvc_die "release manifest contains an invalid image reference"
  done
}

compose_for_release() {
  local release=$1 version=$2
  shift 2
  release_images "$release" "$version"
  ORCHESTRATOR_VERSION="$version" \
  API_IMAGE="$RELEASE_API_IMAGE" \
  WEB_IMAGE="$RELEASE_WEB_IMAGE" \
  MLFLOW_IMAGE="$RELEASE_MLFLOW_IMAGE" \
  POSTGRES_IMAGE="$RELEASE_POSTGRES_IMAGE" \
  REDIS_IMAGE="$RELEASE_REDIS_IMAGE" \
  MINIO_IMAGE="$RELEASE_MINIO_IMAGE" \
  MINIO_CLIENT_IMAGE="$RELEASE_MINIO_CLIENT_IMAGE" \
  NGINX_IMAGE="$RELEASE_NGINX_IMAGE" \
  RVC_IMAGE_PULL_POLICY="$RELEASE_PULL_POLICY" \
  RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
    "$compose" "$@"
}

wait_ready() {
  local release=$1 version=$2 attempt
  for (( attempt=1; attempt<=${RVC_ROLLBACK_READY_ATTEMPTS:-30}; attempt++ )); do
    if compose_for_release "$release" "$version" exec -T api python -c \
      "import urllib.request; urllib.request.urlopen('http://localhost:8000/ready', timeout=3)" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep "${RVC_ROLLBACK_READY_INTERVAL_SECONDS:-2}"
  done
  return 1
}

verify_release_image_identity() {
  local release=$1 version=$2 image_manifest source_commit verifier
  image_manifest="$release/images-manifest.json"
  [[ -e $image_manifest || -L $image_manifest ]] || return 0
  [[ -f $image_manifest && ! -L $image_manifest ]] || \
    rvc_die "release image manifest is missing or unsafe"
  verifier="$INSTALL_ROOT/lib/image_bundle.py"
  [[ -f $verifier && ! -L $verifier ]] || \
    rvc_die "installed image bundle verifier is missing or unsafe"
  source_commit=$(rvc_manifest_value "$release" GIT_COMMIT || true)
  [[ -n $source_commit ]] || rvc_die "release source commit is missing"
  python3 "$verifier" verify-loaded --root "$release" --component manager \
    --version "$version" --source-commit "$source_commit" || \
    rvc_die "release container image identity verification failed"
}

start_release() {
  local release=$1 version=$2
  verify_release_image_identity "$release" "$version"
  compose_for_release "$release" "$version" up -d --remove-orphans || return 1
  wait_ready "$release" "$version"
}

persist_release_environment() {
  local release=$1 version=$2 temporary
  release_images "$release" "$version"
  umask 077
  temporary=$(mktemp "$CONFIG_ROOT/.manager.env.rollback-target.XXXXXX")
  awk -v version="$version" -v api="$RELEASE_API_IMAGE" \
      -v web="$RELEASE_WEB_IMAGE" -v mlflow="$RELEASE_MLFLOW_IMAGE" \
      -v postgres="$RELEASE_POSTGRES_IMAGE" -v redis="$RELEASE_REDIS_IMAGE" \
      -v minio="$RELEASE_MINIO_IMAGE" -v minio_client="$RELEASE_MINIO_CLIENT_IMAGE" \
      -v nginx="$RELEASE_NGINX_IMAGE" -v pull_policy="$RELEASE_PULL_POLICY" '
    /^ORCHESTRATOR_VERSION=/ { print "ORCHESTRATOR_VERSION=" version; seen_version=1; next }
    /^API_IMAGE=/ { print "API_IMAGE=" api; seen_api=1; next }
    /^WEB_IMAGE=/ { print "WEB_IMAGE=" web; seen_web=1; next }
    /^MLFLOW_IMAGE=/ { print "MLFLOW_IMAGE=" mlflow; seen_mlflow=1; next }
    /^POSTGRES_IMAGE=/ { print "POSTGRES_IMAGE=" postgres; seen_postgres=1; next }
    /^REDIS_IMAGE=/ { print "REDIS_IMAGE=" redis; seen_redis=1; next }
    /^MINIO_IMAGE=/ { print "MINIO_IMAGE=" minio; seen_minio=1; next }
    /^MINIO_CLIENT_IMAGE=/ {
      print "MINIO_CLIENT_IMAGE=" minio_client; seen_minio_client=1; next
    }
    /^NGINX_IMAGE=/ { print "NGINX_IMAGE=" nginx; seen_nginx=1; next }
    /^RVC_IMAGE_PULL_POLICY=/ {
      print "RVC_IMAGE_PULL_POLICY=" pull_policy; seen_pull_policy=1; next
    }
    { print }
    END {
      if (!seen_version) print "ORCHESTRATOR_VERSION=" version
      if (!seen_api) print "API_IMAGE=" api
      if (!seen_web) print "WEB_IMAGE=" web
      if (!seen_mlflow) print "MLFLOW_IMAGE=" mlflow
      if (!seen_postgres) print "POSTGRES_IMAGE=" postgres
      if (!seen_redis) print "REDIS_IMAGE=" redis
      if (!seen_minio) print "MINIO_IMAGE=" minio
      if (!seen_minio_client) print "MINIO_CLIENT_IMAGE=" minio_client
      if (!seen_nginx) print "NGINX_IMAGE=" nginx
      if (!seen_pull_policy) print "RVC_IMAGE_PULL_POLICY=" pull_policy
    }
  ' "$manager_env" > "$temporary"
  chmod 0600 "$temporary"
  mv "$temporary" "$manager_env"
}

canonical_schema_set() {
  awk 'NF {print $1}' | LC_ALL=C sort -u | paste -sd, -
}

database_schema_set=$(compose_for_release "$current_release" "$current_version" \
  exec -T api alembic -c /app/alembic.ini current | canonical_schema_set)
current_head_set=$(compose_for_release "$current_release" "$current_version" \
  run --rm --no-deps api alembic -c /app/alembic.ini heads | canonical_schema_set)
target_head_set=$(compose_for_release "$target_release" "$target_version" \
  run --rm --no-deps api alembic -c /app/alembic.ini heads | canonical_schema_set)
for schema_set in "$database_schema_set" "$current_head_set" "$target_head_set"; do
  [[ -n $schema_set && $schema_set =~ ^[A-Za-z0-9._-]+(,[A-Za-z0-9._-]+)*$ ]] || \
    rvc_die "rollback schema preflight returned an unavailable or unsafe revision set"
done

schema_mismatch=$marker_mismatch
if [[ $database_schema_set != "$current_head_set" || \
      $database_schema_set != "$target_head_set" ]]; then
  schema_mismatch=1
fi
if [[ $schema_mismatch == 1 ]]; then
  [[ $allow_schema_mismatch == 1 ]] || \
    rvc_die "rollback requires matching non-unknown SCHEMA_COMPATIBILITY markers and an actual database revision set supported by the target heads; no database downgrade will be attempted"
  [[ $schema_mismatch_confirmation == I_UNDERSTAND_NO_DATABASE_DOWNGRADE ]] || \
    rvc_die "schema mismatch override requires --confirm-schema-mismatch-risk I_UNDERSTAND_NO_DATABASE_DOWNGRADE"
  backup_command="$INSTALL_ROOT/bin/backup"
  [[ -x $backup_command ]] || \
    rvc_die "schema mismatch override requires the installed Manager backup command"
  rvc_log "creating mandatory backup before schema mismatch rollback override"
  pre_rollback_output=$(RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
    "$backup_command" --destination "$pre_rollback_destination")
  printf '%s\n' "$pre_rollback_output"
  pre_rollback_path=$(printf '%s\n' "$pre_rollback_output" | \
    sed -n 's/^BACKUP_PATH=//p' | tail -n 1)
  [[ -n $pre_rollback_path && -d $pre_rollback_path ]] || \
    rvc_die "mandatory pre-rollback backup did not publish a recovery path"
  rvc_warn "schema mismatch override accepted after backup; database migrations will not be downgraded"
fi

env_snapshot=$(mktemp "$CONFIG_ROOT/.manager.env.before-rollback.XXXXXX")
chmod 0600 "$env_snapshot"
cp "$manager_env" "$env_snapshot"
restore_environment_snapshot() {
  [[ -n ${env_snapshot:-} && -f $env_snapshot ]] || return 1
  chmod 0600 "$env_snapshot"
  mv "$env_snapshot" "$manager_env"
  env_snapshot=
}

rollback_pending=0
switched_to_target=0
recover_interrupted_rollback() {
  local status=$?
  if [[ $status -ne 0 && $rollback_pending == 1 ]]; then
    set +e
    rvc_warn "rollback was interrupted after entering maintenance; restoring $current_version"
    if [[ $switched_to_target == 1 ]]; then
      compose_for_release "$target_release" "$target_version" \
        stop proxy web api rq-worker mlflow api-migrate >/dev/null 2>&1
      if ! switch_current "$current_version"; then
        rvc_warn "automatic symlink recovery failed; point $INSTALL_ROOT/current to releases/$current_version manually"
      else
        switched_to_target=0
      fi
    fi
    restore_environment_snapshot || rvc_warn "could not restore the previous Manager environment file"
    start_release "$current_release" "$current_version" >/dev/null 2>&1 || \
      rvc_warn "previous release was selected but Manager readiness failed"
  fi
  if [[ -n ${env_snapshot:-} && -f $env_snapshot ]]; then rm -f -- "$env_snapshot"; fi
  exit "$status"
}
trap recover_interrupted_rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

rvc_log "stopping Manager write services before rollback"
rollback_pending=1
RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" stop proxy web api rq-worker mlflow api-migrate
if ! switch_current "$target_version"; then
  rvc_warn "could not switch current release; restarting version $current_version"
  start_release "$current_release" "$current_version" || \
    rvc_warn "version $current_version did not recover readiness; inspect Compose logs"
  rvc_die "atomic current symlink switch failed"
fi
switched_to_target=1

rvc_log "current release switched to $target_version; no database downgrade was run"
persist_release_environment "$target_release" "$target_version"
if start_release "$target_release" "$target_version"; then
  rollback_pending=0
  switched_to_target=0
  rm -f -- "$env_snapshot"
  env_snapshot=
  rvc_log "Manager rollback completed and readiness was verified"
  exit 0
fi

rvc_warn "target version $target_version failed readiness; reverting current symlink to $current_version"
compose_for_release "$target_release" "$target_version" \
  stop proxy web api rq-worker mlflow api-migrate >/dev/null 2>&1 || true
if ! switch_current "$current_version"; then
  rvc_die "target failed and the current symlink could not be reverted; manually point $INSTALL_ROOT/current to releases/$current_version before recovery"
fi
switched_to_target=0
restore_environment_snapshot || \
  rvc_die "previous release symlink was restored but its Manager environment could not be recovered"
rollback_pending=0
if start_release "$current_release" "$current_version"; then
  rvc_die "target version failed readiness; previous version $current_version was restored and is ready"
fi
rvc_die "target version failed readiness; previous version symlink was restored but Manager is not ready. Inspect Compose logs before serving traffic"
