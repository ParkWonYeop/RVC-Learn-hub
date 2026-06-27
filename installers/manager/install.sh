#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for library in "$SCRIPT_DIR/../common/lib.sh" "$SCRIPT_DIR/common/lib.sh"; do
  if [[ -r $library ]]; then source "$library"; break; fi
done
declare -F rvc_die >/dev/null || { echo "installer common library not found" >&2; exit 1; }

if [[ -d $SCRIPT_DIR/infra ]]; then
  BUNDLE_ROOT=$SCRIPT_DIR
  source_tree_install=0
else
  BUNDLE_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
  source_tree_install=1
fi

INSTALL_ROOT=${RVC_INSTALL_ROOT:-/opt/rvc-orchestrator/manager}
CONFIG_ROOT=${RVC_CONFIG_ROOT:-/etc/rvc-orchestrator/manager}
SYSTEMD_DIR=${RVC_SYSTEMD_DIR:-/etc/systemd/system}
no_start=0
allow_unsupported=0
skip_daemon=0
version=${ORCHESTRATOR_VERSION:-}
admin_email=
admin_password_file=
s3_presign_endpoint_url=
minio_api_bind_address=
public_scheme=

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-root) shift; INSTALL_ROOT=${1:?missing install root} ;;
    --config-root) shift; CONFIG_ROOT=${1:?missing config root} ;;
    --systemd-dir) shift; SYSTEMD_DIR=${1:?missing systemd directory} ;;
    --version) shift; version=${1:?missing version} ;;
    --admin-email) shift; admin_email=${1:?missing administrator email} ;;
    --admin-password-file) shift; admin_password_file=${1:?missing password file} ;;
    --s3-presign-endpoint-url) shift; s3_presign_endpoint_url=${1:?missing S3 public endpoint} ;;
    --minio-api-bind-address) shift; minio_api_bind_address=${1:?missing MinIO bind address} ;;
    --public-scheme) shift; public_scheme=${1:?missing public scheme} ;;
    --no-start) no_start=1 ;;
    --allow-unsupported-os) allow_unsupported=1 ;;
    --skip-daemon-check) skip_daemon=1 ;;
    *) rvc_die "unknown install option: $1" ;;
  esac
  shift
done

if [[ -n $public_scheme && $public_scheme != http && $public_scheme != https ]]; then
  rvc_die "--public-scheme must be exactly http or https"
fi

if [[ -n $s3_presign_endpoint_url ]]; then
  [[ $s3_presign_endpoint_url =~ ^https?://(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+)(:[0-9]{1,5})?(/[^[:space:]?#]*)?$ ]] || \
    rvc_die "--s3-presign-endpoint-url must be an HTTP(S) origin/base URL without credentials, query, or fragment"
  if [[ $s3_presign_endpoint_url != https://* ]]; then
    [[ $no_start == 1 ]] || \
      rvc_die "production Manager artifact transfer endpoint must use HTTPS"
    rvc_warn "plaintext artifact endpoint is development-only and cannot start in production"
  fi
fi
if [[ -n $minio_api_bind_address ]]; then
  [[ $minio_api_bind_address =~ ^[0-9A-Fa-f:.]+$ ]] || \
    rvc_die "--minio-api-bind-address must be an IP address"
fi

if [[ -n $admin_email || -n $admin_password_file ]]; then
  [[ -n $admin_email && -n $admin_password_file ]] || \
    rvc_die "--admin-email and --admin-password-file must be provided together"
  [[ $no_start == 0 ]] || rvc_die "administrator bootstrap cannot be combined with --no-start"
fi

rvc_require_root_for_system_paths
rvc_verify_bundle_checksums "$BUNDLE_ROOT" "$source_tree_install"
rvc_validate_supply_chain_files "$BUNDLE_ROOT"
if [[ -f $BUNDLE_ROOT/manifest.env ]]; then
  [[ $(rvc_manifest_value "$BUNDLE_ROOT" PRODUCT || true) == \
     rvc-training-orchestrator ]] || rvc_die "Manager bundle manifest product is invalid"
  [[ $(rvc_manifest_value "$BUNDLE_ROOT" COMPONENT || true) == manager ]] || \
    rvc_die "Manager bundle manifest component is invalid"
fi
bundle_version=$(rvc_manifest_value "$BUNDLE_ROOT" VERSION || true)
if [[ -z $version ]]; then
  version=$bundle_version
elif [[ -n $bundle_version && $version != "$bundle_version" ]]; then
  rvc_die "requested version $version does not match bundle version $bundle_version"
fi
version=${version:-dev}
rvc_validate_version "$version"
rvc_require_forward_release_transition "$INSTALL_ROOT" "$version"
bundle_source_commit=$(rvc_manifest_value "$BUNDLE_ROOT" GIT_COMMIT || true)
bundle_source_commit=${bundle_source_commit:-uncommitted}
rvc_prepare_image_bundle "$BUNDLE_ROOT" manager "$version" "$bundle_source_commit"
release_checksum_verifier=$(rvc_image_bundle_verifier "$BUNDLE_ROOT" || true)
[[ -n $release_checksum_verifier ]] || \
  rvc_die "dependency-free release checksum verifier is missing"

write_release_manifest() {
  local destination=$1
  if [[ -f $BUNDLE_ROOT/manifest.env && ! -L $BUNDLE_ROOT/manifest.env ]]; then
    install -m 0644 "$BUNDLE_ROOT/manifest.env" "$destination"
  else
    cat > "$destination" <<MANIFEST
BUNDLE_FORMAT_VERSION=source-tree
PRODUCT=rvc-training-orchestrator
COMPONENT=manager
VERSION=$version
SCHEMA_COMPATIBILITY=unknown
PLATFORM=linux-amd64
MANIFEST
    chmod 0644 "$destination"
  fi
}

validate_release_manifest() {
  local release=$1 compatibility
  [[ -f $release/manifest.env && ! -L $release/manifest.env ]] || \
    rvc_die "release manifest is missing or unsafe: $release"
  [[ $(rvc_manifest_value "$release" PRODUCT) == rvc-training-orchestrator ]] || \
    rvc_die "release manifest product does not match"
  [[ $(rvc_manifest_value "$release" COMPONENT) == manager ]] || \
    rvc_die "release manifest component does not match Manager"
  [[ $(rvc_manifest_value "$release" VERSION) == "$version" ]] || \
    rvc_die "release manifest version does not match $version"
  compatibility=$(rvc_manifest_value "$release" SCHEMA_COMPATIBILITY || true)
  [[ $compatibility =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || \
    rvc_die "release manifest has an invalid schema compatibility marker"
}

write_release_checksums() {
  rvc_create_release_checksums "$1" "$release_checksum_verifier"
}

verify_release_checksums() {
  local release=$1 sums file
  sums="$release/RELEASE_SHA256SUMS"
  [[ -f $sums && ! -L $sums ]] || rvc_die "release checksums are missing or unsafe"
  rvc_verify_release_checksums "$release" "$release_checksum_verifier"
  for file in VERSION manifest.env infra/compose/manager.compose.yml; do
    awk -v wanted="$file" '$2 == wanted {found=1} END {exit !found}' "$sums" || \
      rvc_die "release checksums do not cover required file: $file"
  done
}

update_manager_release_environment() {
  local env_path=$1 release=$2 release_version=$3 api_image web_image mlflow_image
  local postgres_image redis_image minio_image minio_client_image nginx_image pull_policy temporary
  api_image=$(rvc_manifest_value "$release" API_IMAGE || true)
  web_image=$(rvc_manifest_value "$release" WEB_IMAGE || true)
  mlflow_image=$(rvc_manifest_value "$release" MLFLOW_IMAGE || true)
  postgres_image=$(rvc_manifest_value "$release" POSTGRES_IMAGE || true)
  redis_image=$(rvc_manifest_value "$release" REDIS_IMAGE || true)
  minio_image=$(rvc_manifest_value "$release" MINIO_IMAGE || true)
  minio_client_image=$(rvc_manifest_value "$release" MINIO_CLIENT_IMAGE || true)
  nginx_image=$(rvc_manifest_value "$release" NGINX_IMAGE || true)
  api_image=${api_image:-rvc-orchestrator-api:$release_version}
  web_image=${web_image:-rvc-orchestrator-web:$release_version}
  mlflow_image=${mlflow_image:-rvc-orchestrator-mlflow:$release_version}
  postgres_image=${postgres_image:-postgres:16-alpine}
  redis_image=${redis_image:-redis:7.4-alpine}
  minio_image=${minio_image:-minio/minio:RELEASE.2025-04-22T22-12-26Z}
  minio_client_image=${minio_client_image:-minio/mc:RELEASE.2025-04-16T18-13-26Z}
  nginx_image=${nginx_image:-nginx:1.27-alpine}
  if [[ $(rvc_manifest_value "$release" SELF_CONTAINED || true) == true ]]; then
    pull_policy=never
  else
    pull_policy=missing
  fi
  for image in "$api_image" "$web_image" "$mlflow_image" "$postgres_image" \
    "$redis_image" "$minio_image" "$minio_client_image" "$nginx_image"; do
    [[ $image =~ ^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}$ ]] || \
      rvc_die "release manifest contains an invalid image reference"
  done
  [[ -f $env_path && ! -L $env_path ]] || rvc_die "environment file is missing or unsafe: $env_path"
  umask 077
  temporary=$(mktemp "$CONFIG_ROOT/.manager.env.release.XXXXXX")
  awk -v version="$release_version" -v api="$api_image" -v web="$web_image" \
      -v mlflow="$mlflow_image" -v postgres="$postgres_image" -v redis="$redis_image" \
      -v minio="$minio_image" -v minio_client="$minio_client_image" \
      -v nginx="$nginx_image" -v pull_policy="$pull_policy" '
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
  ' "$env_path" > "$temporary"
  chmod 0600 "$temporary"
  mv "$temporary" "$env_path"
}

set_manager_environment_value() {
  local env_path=$1 key=$2 value=$3 temporary
  [[ -f $env_path && ! -L $env_path ]] || rvc_die "environment file is missing or unsafe"
  umask 077
  temporary=$(mktemp "$CONFIG_ROOT/.manager.env.setting.XXXXXX")
  awk -v key="$key" -v value="$value" '
    index($0, key "=") == 1 { print key "=" value; seen=1; next }
    { print }
    END { if (!seen) print key "=" value }
  ' "$env_path" > "$temporary"
  chmod 0600 "$temporary"
  mv "$temporary" "$env_path"
}

preflight="$SCRIPT_DIR/preflight.sh"
[[ -x $preflight ]] || preflight="$BUNDLE_ROOT/preflight.sh"
preflight_args=()
[[ $allow_unsupported == 1 ]] && preflight_args+=(--allow-unsupported-os)
[[ $skip_daemon == 1 ]] && preflight_args+=(--skip-daemon-check)
RVC_INSTALL_ROOT="$INSTALL_ROOT" "$preflight" "${preflight_args[@]}"
selected_release="$INSTALL_ROOT/releases/$version"
if [[ -e $selected_release || -L $selected_release ]]; then
  [[ -d $selected_release && ! -L $selected_release ]] || \
    rvc_die "existing release path is not a regular directory: $selected_release"
  [[ -f $selected_release/VERSION && ! -L $selected_release/VERSION && \
     $(<"$selected_release/VERSION") == "$version" ]] || \
    rvc_die "existing release directory is incomplete: $selected_release"
  validate_release_manifest "$selected_release"
  if [[ -f $BUNDLE_ROOT/manifest.env ]]; then
    cmp -s "$BUNDLE_ROOT/manifest.env" "$selected_release/manifest.env" || \
      rvc_die "existing release provenance differs from the selected bundle"
  fi
  if [[ -f $BUNDLE_ROOT/images-manifest.json ]]; then
    [[ -f $selected_release/images-manifest.json && \
       ! -L $selected_release/images-manifest.json ]] || \
      rvc_die "existing release lacks its image manifest"
    cmp -s "$BUNDLE_ROOT/images-manifest.json" "$selected_release/images-manifest.json" || \
      rvc_die "existing release image provenance differs from the selected bundle"
  fi
  verify_release_checksums "$selected_release"
fi
rvc_load_verified_image_bundle "$BUNDLE_ROOT"

install -d -m 0755 "$INSTALL_ROOT/releases" "$INSTALL_ROOT/bin"
install -d -m 0700 "$CONFIG_ROOT" "$CONFIG_ROOT/secrets"
release_dir="$INSTALL_ROOT/releases/$version"
if [[ ! -d $release_dir ]]; then
  temporary_release="$INSTALL_ROOT/releases/.${version}.installing.$$"
  install -d -m 0755 "$temporary_release"
  cp -R "$BUNDLE_ROOT/infra" "$temporary_release/infra"
  if [[ -f $BUNDLE_ROOT/supply-chain/sbom.cdx.json && \
        ! -L $BUNDLE_ROOT/supply-chain ]]; then
    cp -R "$BUNDLE_ROOT/supply-chain" "$temporary_release/supply-chain"
  fi
  install -m 0644 "$BUNDLE_ROOT/.env.example" "$temporary_release/.env.example"
  printf '%s\n' "$version" > "$temporary_release/VERSION"
  write_release_manifest "$temporary_release/manifest.env"
  if [[ -f $BUNDLE_ROOT/images-manifest.json && ! -L $BUNDLE_ROOT/images-manifest.json ]]; then
    install -m 0644 "$BUNDLE_ROOT/images-manifest.json" \
      "$temporary_release/images-manifest.json"
  fi
  validate_release_manifest "$temporary_release"
  write_release_checksums "$temporary_release"
  mv "$temporary_release" "$release_dir"
else
  [[ -f $release_dir/VERSION && $(<"$release_dir/VERSION") == "$version" ]] || \
    rvc_die "existing release directory is incomplete: $release_dir"
  if [[ ! -e $release_dir/manifest.env ]]; then
    [[ ! -e $release_dir/RELEASE_SHA256SUMS ]] || \
      rvc_die "existing release has checksums but no manifest: $release_dir"
    write_release_manifest "$release_dir/manifest.env"
  fi
  validate_release_manifest "$release_dir"
  if [[ -f $BUNDLE_ROOT/images-manifest.json ]]; then
    [[ -f $release_dir/images-manifest.json && ! -L $release_dir/images-manifest.json ]] || \
      rvc_die "existing release lacks its image manifest"
    cmp -s "$BUNDLE_ROOT/images-manifest.json" "$release_dir/images-manifest.json" || \
      rvc_die "existing release image provenance differs from the selected bundle"
  fi
  verify_release_checksums "$release_dir"
  rvc_log "release $version is already installed; preserving it"
fi

secret_names=(
  postgres_password maintenance_postgres_password mlflow_postgres_password
  redis_password maintenance_redis_password minio_root_user
  minio_root_password minio_app_access_key minio_app_secret_key
  maintenance_s3_access_key maintenance_s3_secret_key
  mlflow_s3_access_key mlflow_s3_secret_key worker_bootstrap_token
  worker_token_pepper jwt_secret
)
for secret_name in "${secret_names[@]}"; do
  case "$secret_name" in
    minio_root_user|minio_app_access_key|maintenance_s3_access_key|mlflow_s3_access_key)
      rvc_generate_secret_file "$CONFIG_ROOT/secrets/$secret_name" 12 ;;
    *) rvc_generate_secret_file "$CONFIG_ROOT/secrets/$secret_name" 32 ;;
  esac
done

# Keep the host source directory a closed, root-owned inventory. Runtime
# services never mount this directory; the network-none initializer projects
# only each role's exact allowlist into non-enumerable named volumes.
python3 - "$CONFIG_ROOT/secrets" "${secret_names[@]}" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
expected = set(sys.argv[2:])
root_info = root.lstat()
if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
    raise SystemExit("Manager secret root is not a real directory")
expected_uid = os.geteuid()
expected_gid = os.getegid()
if (root_info.st_uid, root_info.st_gid) != (expected_uid, expected_gid):
    raise SystemExit("Manager secret root ownership is invalid")
if stat.S_IMODE(root_info.st_mode) != 0o700:
    raise SystemExit("Manager secret root mode must be 0700")
actual = {entry.name for entry in os.scandir(root)}
if actual != expected:
    raise SystemExit("Manager source secret inventory is not exact")
for name in sorted(expected):
    path = root / name
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SystemExit(f"Manager source secret is not a regular file: {name}")
    if (info.st_uid, info.st_gid) != (expected_uid, expected_gid):
        raise SystemExit(f"Manager source secret ownership is invalid: {name}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise SystemExit(f"Manager source secret mode must be 0600: {name}")
    if info.st_size <= 0 or info.st_size > 16 * 1024:
        raise SystemExit(f"Manager source secret size is invalid: {name}")
    value = path.read_bytes()
    if b"\x00" in value or not value.replace(b"\r", b"").replace(b"\n", b""):
        raise SystemExit(f"Manager source secret content is invalid: {name}")
PY

env_file="$CONFIG_ROOT/manager.env"
pending_env=$(mktemp "$CONFIG_ROOT/.manager.env.pending.XXXXXX")
pending_env_active=1
activation_started=0
activation_committed=0
previous_env_existed=0
previous_env_backup=
previous_version=
previous_runtime_stopped=0

recover_manager_activation() {
  local status=$?
  trap - EXIT
  if [[ $status -ne 0 && $activation_started == 1 && $activation_committed == 0 ]]; then
    rvc_warn "Manager activation did not commit; restoring the previous release pointers"
    if [[ $previous_env_existed == 1 && -n $previous_env_backup && \
          -f $previous_env_backup && ! -L $previous_env_backup ]]; then
      mv -f -- "$previous_env_backup" "$env_file" || \
        rvc_warn "could not restore the previous Manager environment"
      previous_env_backup=
    else
      rm -f -- "$env_file"
    fi
    if [[ -n $previous_version ]]; then
      rvc_switch_current_release "$INSTALL_ROOT" "$previous_version" || \
        rvc_warn "could not restore the previous Manager current pointer"
    else
      rm -f -- "$INSTALL_ROOT/current"
    fi
    if [[ $previous_runtime_stopped == 1 ]]; then
      systemctl start rvc-orchestrator-manager.service >/dev/null 2>&1 || \
        rvc_warn "the previous Manager service must be restarted manually"
    fi
  fi
  if [[ $pending_env_active == 1 ]]; then
    rm -f -- "$pending_env"
  fi
  if [[ -n $previous_env_backup ]]; then
    rm -f -- "$previous_env_backup"
  fi
  exit "$status"
}
trap recover_manager_activation EXIT

if [[ ! -e $env_file ]]; then
  umask 077
  awk -v version="$version" -v secrets="$CONFIG_ROOT/secrets" '
    /^ORCHESTRATOR_VERSION=/ { print "ORCHESTRATOR_VERSION=" version; next }
    /^ENVIRONMENT=/ { print "ENVIRONMENT=production"; next }
    /^ALLOW_FAKE_WORKERS=/ { print "ALLOW_FAKE_WORKERS=false"; next }
    /^API_IMAGE=/ { print "API_IMAGE=rvc-orchestrator-api:" version; next }
    /^WEB_IMAGE=/ { print "WEB_IMAGE=rvc-orchestrator-web:" version; next }
    /^MLFLOW_IMAGE=/ { print "MLFLOW_IMAGE=rvc-orchestrator-mlflow:" version; next }
    /^MANAGER_SECRETS_DIR=/ { print "MANAGER_SECRETS_DIR=" secrets; next }
    { print }
  ' "$BUNDLE_ROOT/.env.example" > "$pending_env"
else
  [[ -f $env_file && ! -L $env_file ]] || rvc_die "environment path is not a safe regular file: $env_file"
  chmod 0600 "$env_file"
  cp -p -- "$env_file" "$pending_env"
  rvc_log "preserving existing Manager configuration"
fi
chmod 0600 "$pending_env"
if [[ -n $s3_presign_endpoint_url ]]; then
  set_manager_environment_value "$pending_env" S3_PRESIGN_ENDPOINT_URL "$s3_presign_endpoint_url"
fi
if [[ -n $minio_api_bind_address ]]; then
  set_manager_environment_value "$pending_env" MINIO_API_BIND_ADDRESS "$minio_api_bind_address"
fi
if [[ -n $public_scheme ]]; then
  set_manager_environment_value "$pending_env" PUBLIC_SCHEME "$public_scheme"
fi
configured_presign_endpoint=$(awk -F= '
  $1 == "S3_PRESIGN_ENDPOINT_URL" {sub(/^[^=]*=/, ""); print; exit}
' "$pending_env")
configured_public_scheme=$(awk -F= '
  $1 == "PUBLIC_SCHEME" {count++; value=$0; sub(/^[^=]*=/, "", value)}
  END {if (count == 1) print value; else exit 1}
' "$pending_env") || rvc_die "manager.env must contain exactly one PUBLIC_SCHEME assignment"
if [[ $configured_public_scheme != http && $configured_public_scheme != https ]]; then
  rvc_die "PUBLIC_SCHEME must be exactly http or https"
fi
if [[ $no_start == 0 && $configured_presign_endpoint != https://* ]]; then
  rvc_die "starting Manager requires an HTTPS S3_PRESIGN_ENDPOINT_URL reachable by remote Workers"
fi
if [[ $no_start == 0 && $configured_public_scheme != https ]]; then
  rvc_die "starting a production Manager requires PUBLIC_SCHEME=https"
fi
if [[ $no_start == 1 && -z $s3_presign_endpoint_url && \
      $configured_presign_endpoint == http://127.0.0.1:* ]]; then
  rvc_warn "edit S3_PRESIGN_ENDPOINT_URL before starting; the bundled loopback value is development-only"
fi
update_manager_release_environment "$pending_env" "$release_dir" "$version"
rvc_log "staged release-owned Manager version and image references for $version"

install -d -m 0755 "$INSTALL_ROOT/lib"
common_source="$SCRIPT_DIR/../common/lib.sh"
[[ -f $common_source ]] || common_source="$BUNDLE_ROOT/common/lib.sh"
install -m 0644 "$common_source" "$INSTALL_ROOT/lib/common.sh"
image_verifier_source="$SCRIPT_DIR/../common/image_bundle.py"
[[ -f $image_verifier_source ]] || \
  image_verifier_source="$BUNDLE_ROOT/common/image_bundle.py"
[[ -f $image_verifier_source && ! -L $image_verifier_source ]] || \
  rvc_die "container image bundle verifier is missing or unsafe"
install -m 0644 "$image_verifier_source" "$INSTALL_ROOT/lib/image_bundle.py"
archive_helper_source="$SCRIPT_DIR/recovery_archive.py"
[[ -f $archive_helper_source ]] || archive_helper_source="$BUNDLE_ROOT/recovery_archive.py"
[[ -f $archive_helper_source && ! -L $archive_helper_source ]] || \
  rvc_die "Manager recovery archive verifier is missing or unsafe"
install -m 0644 "$archive_helper_source" "$INSTALL_ROOT/lib/recovery_archive.py"

for installed_script in \
  compose:manager-compose bootstrap-admin:bootstrap-admin \
  backup:backup restore:restore rollback:rollback; do
  source_name=${installed_script%%:*}
  destination_name=${installed_script#*:}
  script_source="$SCRIPT_DIR/$source_name.sh"
  [[ -f $script_source ]] || script_source="$BUNDLE_ROOT/$source_name.sh"
  [[ -f $script_source && ! -L $script_source ]] || \
    rvc_die "Manager command is missing or unsafe: $source_name.sh"
  install -m 0755 "$script_source" "$INSTALL_ROOT/bin/$destination_name"
done

install -d -m 0755 "$SYSTEMD_DIR"
unit_temp=$(mktemp "$SYSTEMD_DIR/.rvc-orchestrator-manager.service.XXXXXX")
cat > "$unit_temp" <<UNIT
[Unit]
Description=RVC Training Orchestrator Manager
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=RVC_INSTALL_ROOT=$INSTALL_ROOT
Environment=RVC_CONFIG_ROOT=$CONFIG_ROOT
ExecStart=$INSTALL_ROOT/bin/manager-compose up -d --remove-orphans
ExecStop=$INSTALL_ROOT/bin/manager-compose stop
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
UNIT
chmod 0644 "$unit_temp"
if [[ ! -f $SYSTEMD_DIR/rvc-orchestrator-manager.service ]] || \
   ! cmp -s "$unit_temp" "$SYSTEMD_DIR/rvc-orchestrator-manager.service"; then
  mv "$unit_temp" "$SYSTEMD_DIR/rvc-orchestrator-manager.service"
else
  rm -f "$unit_temp"
fi

rvc_find_compose
"${RVC_COMPOSE[@]}" --env-file "$pending_env" \
  -f "$release_dir/infra/compose/manager.compose.yml" config --quiet

if [[ $no_start == 0 ]]; then
  systemctl daemon-reload
  systemctl enable rvc-orchestrator-manager.service
fi
previous_version=$(rvc_current_release_version "$INSTALL_ROOT" || true)
if [[ -n $previous_version ]]; then
  if command -v systemctl >/dev/null 2>&1 && \
     systemctl is-active --quiet rvc-orchestrator-manager.service; then
    previous_runtime_stopped=1
  fi
  RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
    "$INSTALL_ROOT/bin/manager-compose" stop
  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop rvc-orchestrator-manager.service
  fi
fi

if [[ -e $env_file || -L $env_file ]]; then
  [[ -f $env_file && ! -L $env_file ]] || \
    rvc_die "environment path is not a safe regular file: $env_file"
  previous_env_existed=1
  previous_env_backup=$(mktemp "$CONFIG_ROOT/.manager.env.before-activation.XXXXXX")
  cp -p -- "$env_file" "$previous_env_backup"
  chmod 0600 "$previous_env_backup"
fi
activation_started=1
mv -f -- "$pending_env" "$env_file"
pending_env_active=0
rvc_switch_current_release "$INSTALL_ROOT" "$version"
activation_committed=1
if [[ -n $previous_env_backup ]]; then
  rm -f -- "$previous_env_backup"
  previous_env_backup=
fi

RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$INSTALL_ROOT/bin/manager-compose" config --quiet

if [[ $no_start == 0 ]]; then
  systemctl start rvc-orchestrator-manager.service
  rvc_log "Manager installation is running"
  if [[ -n $admin_email ]]; then
    RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
      "$INSTALL_ROOT/bin/bootstrap-admin" \
      --email "$admin_email" --password-file "$admin_password_file"
  else
    rvc_warn "no administrator was bootstrapped; run $INSTALL_ROOT/bin/bootstrap-admin with --email and --password-file"
  fi
else
  rvc_log "Manager installed without starting services (--no-start)"
fi
rvc_log "configuration and persistent Docker volumes were preserved"
