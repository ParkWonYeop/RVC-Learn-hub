#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_ROOT=${RVC_INSTALL_ROOT:-/opt/rvc-orchestrator/manager}
CONFIG_ROOT=${RVC_CONFIG_ROOT:-/etc/rvc-orchestrator/manager}
ENV_FILE=${RVC_MANAGER_ENV_FILE:-$CONFIG_ROOT/manager.env}
COMPOSE_FILE=$INSTALL_ROOT/current/infra/compose/manager.compose.yml

[[ -r $ENV_FILE ]] || { echo "Manager environment file is missing: $ENV_FILE" >&2; exit 1; }
[[ -r $COMPOSE_FILE ]] || { echo "Manager Compose file is missing: $COMPOSE_FILE" >&2; exit 1; }

verify_release_images_before_start() {
  local release manifest image_manifest
  local verifier=$INSTALL_ROOT/lib/image_bundle.py version source_commit
  release=$(cd -- "$INSTALL_ROOT/current" && pwd -P) || exit 1
  case "$release" in
    "$INSTALL_ROOT"/releases/*) ;;
    *) echo "Manager current release resolves outside the release directory" >&2; exit 1 ;;
  esac
  manifest=$release/manifest.env
  image_manifest=$release/images-manifest.json
  [[ -f $verifier && ! -L $verifier ]] || {
    echo "Manager release image verifier is missing or unsafe" >&2
    exit 1
  }
  python3 "$verifier" verify-ledger --root "$release" \
    --ledger-name RELEASE_SHA256SUMS || exit 1
  [[ -e $image_manifest || -L $image_manifest ]] || return 0
  [[ -f $image_manifest && ! -L $image_manifest && -f $manifest && ! -L $manifest ]] || {
    echo "Manager release image provenance is missing or unsafe" >&2
    exit 1
  }
  version=$(awk -F= '$1 == "VERSION" {count++; value=$0; sub(/^[^=]*=/, "", value)}
    END {if (count == 1) print value; else exit 1}' "$manifest") || exit 1
  source_commit=$(awk -F= '$1 == "GIT_COMMIT" {count++; value=$0; sub(/^[^=]*=/, "", value)}
    END {if (count == 1) print value; else exit 1}' "$manifest") || exit 1
  python3 "$verifier" verify-environment --root "$release" --component manager \
    --version "$version" --source-commit "$source_commit" --environment "$ENV_FILE" || exit 1
  python3 "$verifier" verify-loaded --root "$release" --component manager \
    --version "$version" --source-commit "$source_commit" || exit 1
}

validate_public_scheme_before_start() {
  local environment public_scheme
  environment=$(awk -F= '
    $1 == "ENVIRONMENT" {count++; value=$0; sub(/^[^=]*=/, "", value)}
    END {if (count == 1) print value; else exit 1}
  ' "$ENV_FILE") || {
    echo "Manager environment must contain exactly one ENVIRONMENT assignment" >&2
    exit 1
  }
  public_scheme=$(awk -F= '
    $1 == "PUBLIC_SCHEME" {count++; value=$0; sub(/^[^=]*=/, "", value)}
    END {if (count == 1) print value; else exit 1}
  ' "$ENV_FILE") || {
    echo "Manager environment must contain exactly one PUBLIC_SCHEME assignment" >&2
    exit 1
  }
  case "$public_scheme" in
    http|https) ;;
    *)
      echo "PUBLIC_SCHEME must be exactly http or https" >&2
      exit 1
      ;;
  esac
  if [[ $environment == production && $public_scheme != https ]]; then
    echo "production Manager start requires PUBLIC_SCHEME=https" >&2
    exit 1
  fi
}

case "${1:-}" in
  up|start|restart|run|create)
    validate_public_scheme_before_start
    verify_release_images_before_start
    ;;
esac

if docker compose version >/dev/null 2>&1; then
  compose=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")
elif command -v docker-compose >/dev/null 2>&1; then
  compose=(docker-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")
else
  echo "Docker Compose plugin is required" >&2
  exit 1
fi

case "${1:-}" in
  up|start|restart|run|create)
    "${compose[@]}" run --rm --no-deps manager-secrets-init
    ;;
esac

# Raw Compose start/restart does not provide a reliable dependency-completion
# boundary for exited one-shot containers. Reconcile the complete stack through
# `up --force-recreate` so PostgreSQL grants, Redis ACL configuration and MinIO
# policy are all applied before the RQ service can start. Service-scoped
# start/restart would bypass that trust boundary and is intentionally rejected.
case "${1:-}" in
  start|restart)
    if [[ $# -ne 1 ]]; then
      echo "Manager start/restart does not accept service-scoped arguments" >&2
      exit 1
    fi
    exec "${compose[@]}" up -d --force-recreate --remove-orphans
    ;;
esac

exec "${compose[@]}" "$@"
