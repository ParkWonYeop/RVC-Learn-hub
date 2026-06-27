#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
suffix=$$
network=rvc-minio-policy-$suffix
server=rvc-minio-policy-server-$suffix
secret_volume=rvc-minio-policy-secrets-$suffix
root_user=auditroot
root_password=audit-root-password-123456
app_user=manageraudit
app_password=manager-audit-password-123456
mlflow_user=mlflowaudit
mlflow_password=mlflow-audit-password-123456
maintenance_user=maintenanceaudit
maintenance_password=maintenance-audit-password-123456
manager_bucket=rvc-manager-audit
mlflow_bucket=rvc-mlflow-audit

free_port() {
  "$ROOT/.venv/bin/python" - <<'PY'
import socket

with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
PY
}

port=$(free_port)

cleanup() {
  docker rm -f "$server" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  docker volume rm -f "$secret_volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "$network" >/dev/null
docker volume create "$secret_volume" >/dev/null
docker run --rm --user 0:0 --network none -v "$secret_volume:/out" \
  --entrypoint /bin/sh minio/mc:RELEASE.2025-04-16T18-13-26Z -c '
    set -eu
    umask 077
    printf "%s" "$1" > /out/minio_root_user
    printf "%s" "$2" > /out/minio_root_password
    printf "%s" "$3" > /out/minio_app_access_key
    printf "%s" "$4" > /out/minio_app_secret_key
    printf "%s" "$5" > /out/mlflow_s3_access_key
    printf "%s" "$6" > /out/mlflow_s3_secret_key
    printf "%s" "$7" > /out/maintenance_s3_access_key
    printf "%s" "$8" > /out/maintenance_s3_secret_key
    chmod 0600 /out/*
  ' sh "$root_user" "$root_password" "$app_user" "$app_password" \
  "$mlflow_user" "$mlflow_password" "$maintenance_user" "$maintenance_password"

docker run -d --name "$server" --network "$network" --network-alias minio \
  -p "127.0.0.1:$port:9000" \
  -e MINIO_ROOT_USER="$root_user" \
  -e MINIO_ROOT_PASSWORD="$root_password" \
  minio/minio:RELEASE.2025-04-22T22-12-26Z server /data >/dev/null

for attempt in {1..30}; do
  if docker run --rm --network "$network" --entrypoint /bin/sh \
    minio/mc:RELEASE.2025-04-16T18-13-26Z -c \
    'mc alias set local http://minio:9000 "$1" "$2" >/dev/null' \
    sh "$root_user" "$root_password"; then
    break
  fi
  if [[ $attempt == 30 ]]; then
    docker logs "$server" >&2
    exit 1
  fi
  sleep 0.5
done

run_init() {
  docker run --rm --user 0:0 --network "$network" \
    -e S3_BUCKET="$manager_bucket" \
    -e MLFLOW_S3_BUCKET="$mlflow_bucket" \
    -v "$secret_volume:/run/secrets:ro" \
    -v "$ROOT/infra/minio/init.sh:/opt/rvc/minio-init.sh:ro" \
    --entrypoint /bin/sh minio/mc:RELEASE.2025-04-16T18-13-26Z \
    /opt/rvc/minio-init.sh
}

assert_scopes() {
  docker run --rm --network "$network" --entrypoint /bin/sh \
    minio/mc:RELEASE.2025-04-16T18-13-26Z -c '
      set -eu
      mc alias set root http://minio:9000 "$1" "$2" >/dev/null
      printf dataset-staging | mc pipe root/'"$manager_bucket"'/datasets/staging/11111111-1111-4111-8111-111111111111/21111111-1111-4111-8111-111111111111 >/dev/null
      printf testset-staging | mc pipe root/'"$manager_bucket"'/test-sets/staging/31111111-1111-4111-8111-111111111111/41111111-1111-4111-8111-111111111111 >/dev/null
      printf canonical | mc pipe root/'"$manager_bucket"'/datasets/verified/51111111-1111-4111-8111-111111111111/manifest.json >/dev/null
    ' sh "$root_user" "$root_password"
  docker run --rm --network "$network" --entrypoint /bin/sh \
    minio/mc:RELEASE.2025-04-16T18-13-26Z -c '
      set -eu
      mc alias set app http://minio:9000 "$1" "$2" >/dev/null
      mc alias set mlflow http://minio:9000 "$3" "$4" >/dev/null
      mc alias set maintenance http://minio:9000 "$5" "$6" >/dev/null
      printf manager | mc pipe app/'"$manager_bucket"'/manager-object >/dev/null
      test "$(mc cat app/'"$manager_bucket"'/manager-object)" = manager
      if mc ls app/'"$mlflow_bucket"' >/dev/null 2>&1; then
        echo "Manager identity can list the MLflow bucket" >&2
        exit 51
      fi
      printf mlflow | mc pipe mlflow/'"$mlflow_bucket"'/mlflow-object >/dev/null
      test "$(mc cat mlflow/'"$mlflow_bucket"'/mlflow-object)" = mlflow
      if mc ls mlflow/'"$manager_bucket"' >/dev/null 2>&1; then
        echo "MLflow identity can list the Manager bucket" >&2
        exit 52
      fi
      if mc ls maintenance/'"$manager_bucket"' >/dev/null 2>&1; then
        echo "Maintenance identity can list the Manager bucket" >&2
        exit 53
      fi
      if mc cat maintenance/'"$manager_bucket"'/datasets/verified/51111111-1111-4111-8111-111111111111/manifest.json >/dev/null 2>&1; then
        echo "Maintenance identity can read a canonical object" >&2
        exit 54
      fi
      if printf forbidden | mc pipe maintenance/'"$manager_bucket"'/datasets/staging/forbidden >/dev/null 2>&1; then
        echo "Maintenance identity can write a staging object" >&2
        exit 55
      fi
    ' sh "$app_user" "$app_password" "$mlflow_user" "$mlflow_password" \
      "$maintenance_user" "$maintenance_password"
  S3_ENDPOINT="http://127.0.0.1:$port" \
  S3_ACCESS_KEY="$maintenance_user" \
  S3_SECRET_KEY="$maintenance_password" \
  MANAGER_BUCKET="$manager_bucket" \
  MLFLOW_BUCKET="$mlflow_bucket" \
    "$ROOT/.venv/bin/python" - <<'PY'
import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

client = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_ENDPOINT"],
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
    region_name="us-east-1",
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
)
manager_bucket = os.environ["MANAGER_BUCKET"]
mlflow_bucket = os.environ["MLFLOW_BUCKET"]
for key in (
    "datasets/staging/11111111-1111-4111-8111-111111111111/21111111-1111-4111-8111-111111111111",
    "test-sets/staging/31111111-1111-4111-8111-111111111111/41111111-1111-4111-8111-111111111111",
):
    client.delete_object(Bucket=manager_bucket, Key=key)

for bucket, key in (
    (manager_bucket, "datasets/verified/51111111-1111-4111-8111-111111111111/manifest.json"),
    (mlflow_bucket, "mlflow-object"),
):
    try:
        client.delete_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        assert exc.response["Error"]["Code"] == "AccessDenied"
    else:
        raise AssertionError(f"maintenance identity deleted forbidden object from {bucket}")
PY
  docker run --rm --network "$network" --entrypoint /bin/sh \
    minio/mc:RELEASE.2025-04-16T18-13-26Z -c '
      set -eu
      mc alias set root http://minio:9000 "$1" "$2" >/dev/null
      if mc stat root/'"$manager_bucket"'/datasets/staging/11111111-1111-4111-8111-111111111111/21111111-1111-4111-8111-111111111111 >/dev/null 2>&1; then
        echo "Dataset staging object was not deleted" >&2
        exit 58
      fi
      if mc stat root/'"$manager_bucket"'/test-sets/staging/31111111-1111-4111-8111-111111111111/41111111-1111-4111-8111-111111111111 >/dev/null 2>&1; then
        echo "TestSet staging object was not deleted" >&2
        exit 59
      fi
      test "$(mc cat root/'"$manager_bucket"'/datasets/verified/51111111-1111-4111-8111-111111111111/manifest.json)" = canonical
    ' sh "$root_user" "$root_password"
}

assert_exact_policy() {
  user=$1
  expected_policy=$2
  docker run --rm --network "$network" --entrypoint /bin/sh \
    minio/mc:RELEASE.2025-04-16T18-13-26Z -c '
      set -eu
      mc alias set local http://minio:9000 "$1" "$2" >/dev/null
      entities=$(mc admin policy entities local --user "$3" --json)
      case "$entities" in
        *\"user\":\"$3\"*\"policies\":\[\"$4\"\]*) ;;
        *) echo "unexpected policy mapping" >&2; exit 53 ;;
      esac
    ' sh "$root_user" "$root_password" "$user" "$expected_policy"
}

run_init
assert_scopes
assert_exact_policy "$app_user" rvc-manager-app
assert_exact_policy "$mlflow_user" rvc-mlflow-artifacts
assert_exact_policy "$maintenance_user" rvc-maintenance-staging-cleanup

docker run --rm --network "$network" --entrypoint /bin/sh \
  minio/mc:RELEASE.2025-04-16T18-13-26Z -c '
    set -eu
    mc alias set local http://minio:9000 "$1" "$2" >/dev/null
    mc admin policy attach local readwrite --user "$3" >/dev/null
  ' sh "$root_user" "$root_password" "$mlflow_user"

docker run --rm --network "$network" --entrypoint /bin/sh \
  minio/mc:RELEASE.2025-04-16T18-13-26Z -c '
    set -eu
    mc alias set local http://minio:9000 "$1" "$2" >/dev/null
    mc admin policy attach local readwrite --user "$3" >/dev/null
  ' sh "$root_user" "$root_password" "$maintenance_user"

run_init
assert_scopes
assert_exact_policy "$app_user" rvc-manager-app
assert_exact_policy "$mlflow_user" rvc-mlflow-artifacts
assert_exact_policy "$maintenance_user" rvc-maintenance-staging-cleanup

docker run --rm --network "$network" --entrypoint /bin/sh \
  minio/mc:RELEASE.2025-04-16T18-13-26Z -c '
    set -eu
    mc alias set local http://minio:9000 "$1" "$2" >/dev/null
    mc version enable local/'"$manager_bucket"' >/dev/null
  ' sh "$root_user" "$root_password"
if run_init >/dev/null 2>&1; then
  echo "MinIO init accepted a versioning-enabled Manager bucket" >&2
  exit 58
fi

echo "MinIO exact bucket policy smoke: PASS"
