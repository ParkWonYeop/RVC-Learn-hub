#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
API_IMAGE=${API_SECRET_TEST_IMAGE:-rvc-orchestrator-api:secret-projection-smoke}
MLFLOW_IMAGE=${MLFLOW_SECRET_TEST_IMAGE:-rvc-orchestrator-mlflow:secret-projection-smoke}
prefix=rvc-secret-projection-$$
source_volume=${prefix}-source
api_volume=${prefix}-api
maintenance_volume=${prefix}-maintenance
mlflow_volume=${prefix}-mlflow
database_authz_volume=${prefix}-database-authz
volumes=(
  "$source_volume" "$api_volume" "$maintenance_volume" "$mlflow_volume"
  "$database_authz_volume"
)

cleanup() {
  docker volume rm -f "${volumes[@]}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build --pull=false -f "$ROOT/apps/api/Dockerfile" -t "$API_IMAGE" "$ROOT"
docker build --pull=false -f "$ROOT/infra/mlflow/Dockerfile" -t "$MLFLOW_IMAGE" "$ROOT"
docker volume create "$source_volume" >/dev/null
docker volume create "$api_volume" >/dev/null
docker volume create "$maintenance_volume" >/dev/null
docker volume create "$mlflow_volume" >/dev/null
docker volume create "$database_authz_volume" >/dev/null

write_sources() {
  generation=$1
  docker run --rm --user 0:0 --network none \
    -v "$source_volume:/out" \
    --entrypoint /bin/sh "$API_IMAGE" -c '
      set -eu
      umask 077
      for name in \
        postgres_password redis_password minio_app_access_key minio_app_secret_key \
        maintenance_postgres_password maintenance_redis_password \
        maintenance_s3_access_key maintenance_s3_secret_key \
        worker_bootstrap_token worker_token_pepper jwt_secret \
        mlflow_postgres_password mlflow_s3_access_key mlflow_s3_secret_key; do
        printf "%s-%s" "$1" "$name" > "/out/$name"
        chown 0:0 "/out/$name"
        chmod 0600 "/out/$name"
      done
    ' sh "$generation"
}

run_projection() {
  docker run --rm --user 0:0 --network none --read-only \
    --cap-drop ALL --cap-add CHOWN --security-opt no-new-privileges --pids-limit 32 \
    -v "$source_volume:/run/secrets:ro" \
    -v "$api_volume:/prepared/api" \
    -v "$maintenance_volume:/prepared/maintenance" \
    -v "$mlflow_volume:/prepared/mlflow" \
    -v "$database_authz_volume:/prepared/database-authz" \
    -v "$ROOT/infra/runtime/manager-secrets-init.py:/opt/rvc/manager-secrets-init.py:ro" \
    --entrypoint python "$API_IMAGE" /opt/rvc/manager-secrets-init.py
}

assert_root_sources() {
  docker run --rm --user 0:0 --network none --read-only \
    -v "$source_volume:/run/secrets:ro" \
    --entrypoint /bin/sh "$API_IMAGE" -c '
      set -eu
      for path in /run/secrets/*; do
        test "$(stat -c "%u:%g %a" "$path")" = "0:0 600"
      done
    '
}

assert_projection() {
  generation=$1
  docker run --rm --network none --read-only \
    -v "$api_volume:/run/secrets:ro" \
    --entrypoint /bin/sh "$API_IMAGE" -c '
      set -eu
      test "$(id -u):$(id -g)" = "10001:10001"
      for name in \
        postgres_password redis_password minio_app_access_key minio_app_secret_key \
        worker_bootstrap_token worker_token_pepper jwt_secret; do
        test -r "/run/secrets/current/$name"
        test "$(cat "/run/secrets/current/$name")" = "$1-$name"
        test "$(stat -Lc "%u:%g %a" "/run/secrets/current/$name")" = "10001:10001 400"
      done
      test ! -e /run/secrets/current/mlflow_postgres_password
    ' sh "$generation"

  docker run --rm --network none --read-only \
    -v "$maintenance_volume:/run/secrets:ro" \
    --entrypoint /bin/sh "$API_IMAGE" -c '
      set -eu
      test "$(id -u):$(id -g)" = "10001:10001"
      for name in \
        maintenance_postgres_password maintenance_redis_password \
        maintenance_s3_access_key maintenance_s3_secret_key; do
        test "$(cat "/run/secrets/current/$name")" = "$1-$name"
        test "$(stat -Lc "%u:%g %a" "/run/secrets/current/$name")" = "10001:10001 400"
      done
      test ! -e /run/secrets/current/postgres_password
      test ! -e /run/secrets/current/redis_password
      test ! -e /run/secrets/current/minio_app_access_key
      test ! -e /run/secrets/current/minio_app_secret_key
      test ! -e /run/secrets/current/jwt_secret
      test ! -e /run/secrets/current/worker_bootstrap_token
      test ! -e /run/secrets/current/worker_token_pepper
    ' sh "$generation"

  docker run --rm --network none --read-only \
    -v "$database_authz_volume:/run/secrets:ro" \
    --entrypoint /bin/sh "$API_IMAGE" -c '
      set -eu
      test "$(id -u):$(id -g)" = "10001:10001"
      for name in postgres_password maintenance_postgres_password; do
        test "$(cat "/run/secrets/current/$name")" = "$1-$name"
        test "$(stat -Lc "%u:%g %a" "/run/secrets/current/$name")" = "10001:10001 400"
      done
      test ! -e /run/secrets/current/redis_password
      test ! -e /run/secrets/current/maintenance_redis_password
      test ! -e /run/secrets/current/maintenance_s3_secret_key
      test ! -e /run/secrets/current/jwt_secret
    ' sh "$generation"

  docker run --rm --network none --read-only \
    -v "$mlflow_volume:/run/secrets:ro" \
    --entrypoint /bin/sh "$MLFLOW_IMAGE" -c '
      set -eu
      test "$(id -u):$(id -g)" = "10002:10002"
      for name in mlflow_postgres_password mlflow_s3_access_key mlflow_s3_secret_key; do
        test "$(cat "/run/secrets/current/$name")" = "$1-$name"
        test "$(stat -Lc "%u:%g %a" "/run/secrets/current/$name")" = "10002:10002 400"
      done
      test ! -e /run/secrets/current/jwt_secret
      test ! -e /run/secrets/current/minio_app_secret_key
    ' sh "$generation"
}

assert_deployed_entrypoints() {
  generation=$1
  docker run --rm --network none --read-only \
    -e POSTGRES_USER=rvc_manager \
    -e POSTGRES_DB=rvc_orchestrator \
    -e REDIS_DB=0 \
    -e JWT_SECRET_FILE=/run/secrets/current/jwt_secret \
    -e EXPECTED_GENERATION="$generation" \
    -v "$api_volume:/run/secrets:ro" \
    -v "$ROOT/infra/runtime/api-entrypoint.sh:/opt/rvc/api-entrypoint.sh:ro" \
    --entrypoint /opt/rvc/api-entrypoint.sh "$API_IMAGE" \
    python -c 'import os; from pathlib import Path; expected = os.environ["EXPECTED_GENERATION"]; assert os.environ["DATABASE_URL"].endswith(f":{expected}-postgres_password@postgres:5432/rvc_orchestrator"); assert os.environ["REDIS_URL"].startswith(f"redis://:{expected}-redis_password@"); assert os.environ["S3_ACCESS_KEY_ID"] == f"{expected}-minio_app_access_key"; assert os.environ["S3_SECRET_ACCESS_KEY"] == f"{expected}-minio_app_secret_key"; assert os.environ["WORKER_BOOTSTRAP_TOKEN"] == f"{expected}-worker_bootstrap_token"; assert os.environ["WORKER_TOKEN_PEPPER"] == f"{expected}-worker_token_pepper"; assert Path(os.environ["JWT_SECRET_FILE"]).read_text() == f"{expected}-jwt_secret"'

  docker run --rm --network none --read-only \
    -e MAINTENANCE_POSTGRES_USER=rvc_maintenance \
    -e MAINTENANCE_REDIS_USER=rvc_maintenance \
    -e POSTGRES_DB=rvc_orchestrator \
    -e REDIS_DB=0 \
    -e EXPECTED_GENERATION="$generation" \
    -v "$maintenance_volume:/run/secrets:ro" \
    -v "$ROOT/infra/runtime/rq-worker-entrypoint.sh:/opt/rvc/rq-worker-entrypoint.sh:ro" \
    -v "$ROOT/tests/infra/fake_maintenance_db_authz.py:/opt/rvc/maintenance-db-authz.py:ro" \
    --entrypoint /opt/rvc/rq-worker-entrypoint.sh "$API_IMAGE" \
    python -c 'import os; expected = os.environ["EXPECTED_GENERATION"]; assert os.environ["DATABASE_URL"].endswith(f"rvc_maintenance:{expected}-maintenance_postgres_password@postgres:5432/rvc_orchestrator"); assert os.environ["REDIS_URL"].startswith(f"redis://rvc_maintenance:{expected}-maintenance_redis_password@"); assert os.environ["S3_ACCESS_KEY_ID"] == f"{expected}-maintenance_s3_access_key"; assert os.environ["S3_SECRET_ACCESS_KEY"] == f"{expected}-maintenance_s3_secret_key"; assert "WORKER_BOOTSTRAP_TOKEN" not in os.environ; assert "WORKER_TOKEN_PEPPER" not in os.environ'

  docker run --rm --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges --pids-limit 128 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=128m,mode=0700,uid=10002,gid=10002 \
    -e MLFLOW_POSTGRES_DB=rvc_mlflow \
    -e MLFLOW_POSTGRES_USER=rvc_mlflow \
    -e MLFLOW_S3_BUCKET=rvc-mlflow \
    -e EXPECTED_GENERATION="$generation" \
    -v "$mlflow_volume:/run/secrets:ro" \
    -v "$ROOT/infra/mlflow/entrypoint.sh:/opt/rvc/mlflow-entrypoint.sh:ro" \
    -v "$ROOT/tests/infra/fake_mlflow_command.sh:/usr/local/bin/mlflow:ro" \
    --entrypoint /opt/rvc/mlflow-entrypoint.sh "$MLFLOW_IMAGE"
}

write_sources generation-a
assert_root_sources
run_projection
assert_projection generation-a

write_sources generation-b
run_projection
assert_projection generation-b
assert_deployed_entrypoints generation-b

write_sources generation-collision
docker run --rm --user 0:0 --network none -v "$source_volume:/out" \
  --entrypoint /bin/sh "$API_IMAGE" -c \
  'cp /out/postgres_password /out/maintenance_postgres_password; chmod 0600 /out/maintenance_postgres_password'
if run_projection; then
  echo "equal API and maintenance credentials were accepted" >&2
  exit 1
fi
assert_projection generation-b

docker run --rm --user 0:0 --network none -v "$source_volume:/out" \
  --entrypoint /bin/sh "$API_IMAGE" -c ': > /out/jwt_secret; chmod 0600 /out/jwt_secret'
if run_projection; then
  echo "empty source secret was accepted" >&2
  exit 1
fi
assert_projection generation-b

write_sources generation-c
docker run --rm --user 0:0 --network none -v "$source_volume:/out" \
  --entrypoint /bin/sh "$API_IMAGE" -c '
    rm -f /out/jwt_secret
    ln -s /out/postgres_password /out/jwt_secret
  '
if run_projection; then
  echo "symlink source secret was accepted" >&2
  exit 1
fi
assert_projection generation-b

echo "Manager runtime secret projection smoke: PASS"
