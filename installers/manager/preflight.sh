#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for library in "$SCRIPT_DIR/../common/lib.sh" "$SCRIPT_DIR/common/lib.sh"; do
  if [[ -r $library ]]; then
    # shellcheck source=../common/lib.sh
    source "$library"
    break
  fi
done
declare -F rvc_die >/dev/null || { echo "installer common library not found" >&2; exit 1; }

allow_unsupported=0
skip_daemon=0
minimum_disk_gb=${RVC_MANAGER_MINIMUM_DISK_GB:-20}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow-unsupported-os) allow_unsupported=1 ;;
    --skip-daemon-check) skip_daemon=1 ;;
    --minimum-disk-gb) shift; minimum_disk_gb=${1:?missing disk size} ;;
    *) rvc_die "unknown preflight option: $1" ;;
  esac
  shift
done

rvc_check_ubuntu_platform "$allow_unsupported"
rvc_require_command docker
rvc_require_command gzip
rvc_require_command python3
rvc_find_compose

if [[ $skip_daemon == 0 ]]; then
  docker info >/dev/null 2>&1 || rvc_die "Docker daemon is not reachable"
fi

disk_path=${RVC_INSTALL_ROOT:-/opt}
disk_parent=$disk_path
while [[ ! -e $disk_parent && $disk_parent != / ]]; do disk_parent=$(dirname "$disk_parent"); done
available_kb=$(df -Pk "$disk_parent" 2>/dev/null | awk 'NR == 2 {print $4}')
if [[ $available_kb =~ ^[0-9]+$ ]]; then
  required_kb=$((minimum_disk_gb * 1024 * 1024))
  (( available_kb >= required_kb )) || rvc_die "at least ${minimum_disk_gb} GiB free space is required"
else
  rvc_warn "free disk space could not be determined"
fi

rvc_log "Manager preflight checks passed"
