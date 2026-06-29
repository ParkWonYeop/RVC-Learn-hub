#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_ROOT=${RVC_INSTALL_ROOT:-/opt/rvc-orchestrator/worker}
CONFIG_ROOT=${RVC_CONFIG_ROOT:-/etc/rvc-orchestrator/worker}
ENV_FILE=${RVC_WORKER_ENV_FILE:-$CONFIG_ROOT/worker.env}
COMPOSE_FILE=$INSTALL_ROOT/current/infra/compose/worker.compose.yml
CUSTOM_CA_CONTAINER_PATH=/etc/rvc-worker/ca/custom-ca.pem

[[ -r $ENV_FILE ]] || { echo "Worker environment file is missing: $ENV_FILE" >&2; exit 1; }
[[ -r $COMPOSE_FILE ]] || { echo "Worker Compose file is missing: $COMPOSE_FILE" >&2; exit 1; }

environment_value_exact() {
  local key=$1 count value
  count=$(awk -F= -v wanted="$key" '$1 == wanted {count++} END {print count+0}' "$ENV_FILE")
  [[ $count == 1 ]] || {
    echo "Worker environment must contain exactly one $key" >&2
    return 1
  }
  value=$(awk -F= -v wanted="$key" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' \
    "$ENV_FILE")
  printf '%s' "$value"
}

verify_worker_ca_before_start() {
  local configured_path host_dir expected_uid mode validator
  configured_path=$(environment_value_exact WORKER_CA_BUNDLE_PATH) || return 1
  host_dir=$(environment_value_exact WORKER_CA_BUNDLE_HOST_DIR) || return 1
  [[ $host_dir == "$CONFIG_ROOT/ca" ]] || {
    echo "Worker custom CA host directory differs from the installed configuration root" >&2
    return 1
  }
  [[ -d $host_dir && ! -L $host_dir ]] || {
    echo "Worker custom CA host directory is missing or unsafe" >&2
    return 1
  }
  expected_uid=0
  if [[ ${EUID:-$(id -u)} -ne 0 ]]; then expected_uid=$(id -u); fi
  [[ $(stat -c '%u' "$host_dir" 2>/dev/null || stat -f '%u' "$host_dir") == \
     "$expected_uid" ]] || {
    echo "Worker custom CA host directory has an unexpected owner" >&2
    return 1
  }
  mode=$(stat -c '%a' "$host_dir" 2>/dev/null || stat -f '%Lp' "$host_dir")
  [[ $mode == 755 ]] || {
    echo "Worker custom CA host directory mode must be 0755" >&2
    return 1
  }
  validator=$INSTALL_ROOT/lib/worker_ca.py
  [[ -f $validator && ! -L $validator ]] || {
    echo "Worker custom CA validator is missing or unsafe" >&2
    return 1
  }
  case "$configured_path" in
    "")
      [[ ! -e $host_dir/custom-ca.pem && ! -L $host_dir/custom-ca.pem ]] || {
        echo "Worker custom CA exists but is not enabled by the installed environment" >&2
        return 1
      }
      ;;
    "$CUSTOM_CA_CONTAINER_PATH")
      python3 "$validator" validate --path "$host_dir/custom-ca.pem" \
        --required-uid "$expected_uid" || return 1
      ;;
    *)
      echo "Worker custom CA must use the fixed container path $CUSTOM_CA_CONTAINER_PATH" >&2
      return 1
      ;;
  esac
}

verify_release_images_before_start() {
  local release manifest image_manifest
  local verifier=$INSTALL_ROOT/lib/image_bundle.py version source_commit
  release=$(cd -- "$INSTALL_ROOT/current" && pwd -P) || exit 1
  case "$release" in
    "$INSTALL_ROOT"/releases/*) ;;
    *) echo "Worker current release resolves outside the release directory" >&2; exit 1 ;;
  esac
  manifest=$release/manifest.env
  image_manifest=$release/images-manifest.json
  [[ -f $verifier && ! -L $verifier ]] || {
    echo "Worker release image verifier is missing or unsafe" >&2
    exit 1
  }
  python3 "$verifier" verify-ledger --root "$release" \
    --ledger-name RELEASE_SHA256SUMS || exit 1
  [[ -e $image_manifest || -L $image_manifest ]] || return 0
  [[ -f $image_manifest && ! -L $image_manifest && -f $manifest && ! -L $manifest ]] || {
    echo "Worker release image provenance is missing or unsafe" >&2
    exit 1
  }
  version=$(awk -F= '$1 == "VERSION" {count++; value=$0; sub(/^[^=]*=/, "", value)}
    END {if (count == 1) print value; else exit 1}' "$manifest") || exit 1
  source_commit=$(awk -F= '$1 == "GIT_COMMIT" {count++; value=$0; sub(/^[^=]*=/, "", value)}
    END {if (count == 1) print value; else exit 1}' "$manifest") || exit 1
  python3 "$verifier" verify-environment --root "$release" --component worker \
    --version "$version" --source-commit "$source_commit" --environment "$ENV_FILE" || exit 1
  python3 "$verifier" verify-loaded --root "$release" --component worker \
    --version "$version" --source-commit "$source_commit" || exit 1
}

case "${1:-}" in
  up|start|restart|run|create)
    verify_release_images_before_start
    verify_worker_ca_before_start
    ;;
esac

if docker compose version >/dev/null 2>&1; then
  exec docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
elif command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
else
  echo "Docker Compose plugin is required" >&2
  exit 1
fi
