#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_ROOT=${RVC_INSTALL_ROOT:-/opt/rvc-orchestrator/worker}
CONFIG_ROOT=${RVC_CONFIG_ROOT:-/etc/rvc-orchestrator/worker}
DATA_ROOT=${WORKER_DATA_ROOT:-/var/lib/rvc-orchestrator/worker}
operations_failed=0

if command -v systemctl >/dev/null 2>&1; then
  if ! systemctl disable --now rvc-orchestrator-worker.service; then
    printf '%s\n' \
      "ERROR: failed to stop and disable rvc-orchestrator-worker.service." >&2
    operations_failed=1
  fi
else
  printf '%s\n' \
    "ERROR: systemctl is unavailable; the Worker unit was not stopped or disabled." >&2
  operations_failed=1
fi
if [[ -x $INSTALL_ROOT/bin/worker-compose ]]; then
  if ! RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
    "$INSTALL_ROOT/bin/worker-compose" down --remove-orphans; then
    printf '%s\n' "ERROR: failed to stop the Worker Compose service." >&2
    operations_failed=1
  fi
else
  printf 'ERROR: installed Worker Compose wrapper is missing or not executable: %s\n' \
    "$INSTALL_ROOT/bin/worker-compose" >&2
  operations_failed=1
fi

if [[ $operations_failed -ne 0 ]]; then
  printf '%s\n' \
    "Worker uninstall is incomplete; inspect systemd and Compose state before retrying." \
    "No files, token, profile, job data, or Docker objects were deleted." \
    "Retained install path: $INSTALL_ROOT" \
    "Retained config path: $CONFIG_ROOT" \
    "Retained data path: $DATA_ROOT" >&2
  exit 1
fi

printf '%s\n' \
  "Worker service is stopped and disabled." \
  "No files, token, profile, job data, or Docker objects were deleted." \
  "Retained install path: $INSTALL_ROOT" \
  "Retained config path: $CONFIG_ROOT" \
  "Retained data path: $DATA_ROOT"
