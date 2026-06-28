#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_ROOT=${RVC_INSTALL_ROOT:-/opt/rvc-orchestrator/manager}
CONFIG_ROOT=${RVC_CONFIG_ROOT:-/etc/rvc-orchestrator/manager}
operations_failed=0

if command -v systemctl >/dev/null 2>&1; then
  if ! systemctl disable --now rvc-orchestrator-manager.service; then
    printf '%s\n' \
      "ERROR: failed to stop and disable rvc-orchestrator-manager.service." >&2
    operations_failed=1
  fi
else
  printf '%s\n' \
    "ERROR: systemctl is unavailable; the Manager unit was not stopped or disabled." >&2
  operations_failed=1
fi
if [[ -x $INSTALL_ROOT/bin/manager-compose ]]; then
  if ! RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
    "$INSTALL_ROOT/bin/manager-compose" down --remove-orphans; then
    printf '%s\n' "ERROR: failed to stop the Manager Compose services." >&2
    operations_failed=1
  fi
else
  printf 'ERROR: installed Manager Compose wrapper is missing or not executable: %s\n' \
    "$INSTALL_ROOT/bin/manager-compose" >&2
  operations_failed=1
fi

if [[ $operations_failed -ne 0 ]]; then
  printf '%s\n' \
    "Manager uninstall is incomplete; inspect systemd and Compose state before retrying." \
    "No files, secrets, releases, or Docker volumes were deleted." \
    "Retained install path: $INSTALL_ROOT" \
    "Retained config path: $CONFIG_ROOT" >&2
  exit 1
fi

printf '%s\n' \
  "Manager services are stopped and disabled." \
  "No files, secrets, releases, or Docker volumes were deleted." \
  "Retained install path: $INSTALL_ROOT" \
  "Retained config path: $CONFIG_ROOT"
