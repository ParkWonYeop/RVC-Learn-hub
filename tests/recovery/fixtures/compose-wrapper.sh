#!/usr/bin/env bash
set -Eeuo pipefail

: "${RVC_INSTALL_ROOT:?install root is required}"
: "${RVC_CONFIG_ROOT:?config root is required}"
: "${RVC_RECOVERY_DRILL_PROJECT:?drill project is required}"
: "${RVC_RECOVERY_DRILL_COMPOSE_FILE:?drill Compose file is required}"

compose_args=(
  --project-name "$RVC_RECOVERY_DRILL_PROJECT"
  --env-file "$RVC_CONFIG_ROOT/manager.env"
  -f "$RVC_RECOVERY_DRILL_COMPOSE_FILE"
)
if docker compose version >/dev/null 2>&1; then
  exec docker compose "${compose_args[@]}" "$@"
fi
if command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose "${compose_args[@]}" "$@"
fi
echo "Docker Compose v2 plugin or docker-compose is required" >&2
exit 1
