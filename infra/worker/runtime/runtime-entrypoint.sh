#!/bin/sh
set -eu

if [ "${RVC_RUNNER_MODE:-}" = native ] && \
   [ "${RVC_GPU_SMOKE_VERIFIED:-false}" != true ] && \
   [ "${RVC_NATIVE_UNVERIFIED_GPU_ACKNOWLEDGED:-false}" != true ]; then
  echo "native RVC runtime has no verified GPU smoke; explicit operator acknowledgement is required" >&2
  exit 1
fi

python /opt/rvc-runtime/runtime_preflight.py
exec python -m rvc_worker "$@"
