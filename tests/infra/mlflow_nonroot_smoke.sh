#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
IMAGE=${MLFLOW_TEST_IMAGE:-rvc-orchestrator-mlflow:nonroot-smoke}

docker build --pull=false \
  -f "$ROOT/infra/mlflow/Dockerfile" \
  -t "$IMAGE" \
  "$ROOT"

image_user=$(docker image inspect --format '{{.Config.User}}' "$IMAGE")
[[ $image_user == 10002:10002 ]] || {
  printf 'unexpected MLflow image user: %s\n' "$image_user" >&2
  exit 1
}

docker run --rm \
  --network none \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 128 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=128m,mode=0700,uid=10002,gid=10002 \
  --entrypoint /bin/sh \
  "$IMAGE" \
  -c '
    set -eu
    test "$(id -u):$(id -g)" = "10002:10002"
    cap_eff=$(python -c "print(next(line.split()[1] for line in open(\"/proc/self/status\") if line.startswith(\"CapEff:\")))")
    test "$cap_eff" = "0000000000000000"
    touch /tmp/nonroot-write
    if touch /home/rvc-mlflow/read-only-must-fail 2>/dev/null; then
      echo "MLflow root filesystem is writable" >&2
      exit 41
    fi
    python -c "import boto3, mlflow, psycopg2"
    mkdir -p /tmp/artifacts
    mlflow server \
      --host 127.0.0.1 \
      --port 5000 \
      --backend-store-uri sqlite:////tmp/mlflow.db \
      --serve-artifacts \
      --artifacts-destination /tmp/artifacts \
      >/tmp/mlflow.log 2>&1 &
    pid=$!
    cleanup() {
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    }
    trap cleanup EXIT
    ready=0
    for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
      if response=$(python -c "import urllib.request; print(urllib.request.urlopen(\"http://127.0.0.1:5000/health\", timeout=1).read().decode())" 2>/dev/null); then
        test "$response" = "OK"
        ready=1
        break
      fi
      sleep 0.5
    done
    if test "$ready" != 1; then
      cat /tmp/mlflow.log >&2
      exit 42
    fi
    echo "MLflow non-root/read-only health smoke: PASS"
  '
