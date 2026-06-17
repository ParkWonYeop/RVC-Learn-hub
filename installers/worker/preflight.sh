#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for library in "$SCRIPT_DIR/../common/lib.sh" "$SCRIPT_DIR/common/lib.sh"; do
  if [[ -r $library ]]; then source "$library"; break; fi
done
declare -F rvc_die >/dev/null || { echo "installer common library not found" >&2; exit 1; }

allow_unsupported=0
skip_daemon=0
skip_gpu=0
minimum_disk_gb=${RVC_WORKER_MINIMUM_DISK_GB:-50}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow-unsupported-os) allow_unsupported=1 ;;
    --skip-daemon-check) skip_daemon=1 ;;
    --skip-gpu-check) skip_gpu=1 ;;
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

if [[ $skip_gpu == 0 ]]; then
  rvc_require_command nvidia-smi
  nvidia-smi -L >/dev/null 2>&1 || rvc_die "NVIDIA GPU/driver is not available"
  if ! command -v nvidia-ctk >/dev/null 2>&1 && \
     ! command -v nvidia-container-cli >/dev/null 2>&1; then
    rvc_die "NVIDIA Container Toolkit is not installed"
  fi
  if [[ $skip_daemon == 0 ]] && ! docker info --format '{{json .Runtimes}}' | grep -qi nvidia; then
    rvc_warn "Docker does not list an nvidia runtime; CDI may still be configured"
  fi
fi

disk_path=${WORKER_DATA_ROOT:-/var/lib/rvc-orchestrator/worker}
disk_parent=$disk_path
while [[ ! -e $disk_parent && $disk_parent != / ]]; do disk_parent=$(dirname "$disk_parent"); done
available_kb=$(df -Pk "$disk_parent" 2>/dev/null | awk 'NR == 2 {print $4}')
if [[ $available_kb =~ ^[0-9]+$ ]]; then
  required_kb=$((minimum_disk_gb * 1024 * 1024))
  (( available_kb >= required_kb )) || rvc_die "at least ${minimum_disk_gb} GiB free space is required"
else
  rvc_warn "free disk space could not be determined"
fi

rvc_log "Worker preflight checks passed"
