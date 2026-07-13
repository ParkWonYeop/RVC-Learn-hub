#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
PROJECT="rvc-manager-stack-smoke-$$"
SKIP_BUILD=${RVC_STACK_SMOKE_SKIP_BUILD:-0}
STACK_VERSION=${RVC_STACK_SMOKE_VERSION:-stack-smoke}
STACK_REVISION=${RVC_STACK_SMOKE_REVISION:-uncommitted}
if [ "$SKIP_BUILD" = 1 ]; then
  [ -n "${RVC_STACK_SMOKE_API_IMAGE:-}" ] && \
    [ -n "${RVC_STACK_SMOKE_WEB_IMAGE:-}" ] && \
    [ -n "${RVC_STACK_SMOKE_MLFLOW_IMAGE:-}" ] || {
      echo "release-image smoke requires explicit API, Web, and MLflow images" >&2
      exit 1
    }
fi
REMOVE_WORK_PARENT=0
if [ -n "${RVC_STACK_SMOKE_WORK_PARENT:-}" ]; then
  WORK_PARENT=$RVC_STACK_SMOKE_WORK_PARENT
elif [ "$(uname -s)" = Darwin ]; then
  # Colima/Docker Desktop reliably share the repository's /Users path, while
  # macOS's resolved /private/var TMPDIR is commonly outside the VM share.
  WORK_PARENT="$ROOT/.rvc-stack-smoke"
  REMOVE_WORK_PARENT=1
else
  WORK_PARENT=${TMPDIR:-/tmp}
fi
mkdir -p "$WORK_PARENT"
WORK_PARENT=$(CDPATH= cd -- "$WORK_PARENT" && pwd -P)
WORK_ROOT=$(mktemp -d "$WORK_PARENT/rvc-manager-stack-smoke.XXXXXX")
WORK_ROOT=$(CDPATH= cd -- "$WORK_ROOT" && pwd -P)
ENV_FILE="$WORK_ROOT/manager.env"
SECRETS_DIR="$WORK_ROOT/secrets"
COMPOSE_FILE="$ROOT/infra/compose/manager.compose.yml"
BUILD_FILE="$ROOT/infra/compose/manager.compose.build.yml"
API_IMAGE=${RVC_STACK_SMOKE_API_IMAGE:-$PROJECT-api:smoke}
WEB_IMAGE=${RVC_STACK_SMOKE_WEB_IMAGE:-$PROJECT-web:smoke}
MLFLOW_IMAGE=${RVC_STACK_SMOKE_MLFLOW_IMAGE:-$PROJECT-mlflow:smoke}
KEEP=${RVC_STACK_SMOKE_KEEP:-0}

if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif docker-compose version >/dev/null 2>&1; then
  COMPOSE=docker-compose
else
  echo "Docker Compose is required" >&2
  exit 1
fi

compose() {
  # Both supported Compose frontends accept these common arguments.
  # shellcheck disable=SC2086
  $COMPOSE --env-file "$ENV_FILE" -f "$COMPOSE_FILE" -f "$BUILD_FILE" "$@"
}

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$status" -ne 0 ] && [ -f "$ENV_FILE" ]; then
    echo "Manager stack smoke failed; final Compose state:" >&2
    compose ps -a >&2 || true
    compose logs --tail=100 proxy >&2 || true
  fi
  if [ -f "$ENV_FILE" ] && [ "$KEEP" != 1 ]; then
    compose down --volumes --remove-orphans >/dev/null 2>&1 || status=1
    if [ "$SKIP_BUILD" != 1 ]; then
      docker image rm "$API_IMAGE" "$WEB_IMAGE" "$MLFLOW_IMAGE" >/dev/null 2>&1 || true
    fi
  elif [ "$KEEP" = 1 ]; then
    echo "Manager stack smoke resources retained: project=$PROJECT work_root=$WORK_ROOT" >&2
  fi
  if [ "$KEEP" != 1 ]; then
    rm -rf -- "$WORK_ROOT"
    if [ "$REMOVE_WORK_PARENT" = 1 ]; then
      rmdir "$WORK_PARENT" >/dev/null 2>&1 || true
    fi
  fi
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

free_port() {
  python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

wait_for_url() {
  label=$1
  url=$2
  attempts=${3:-30}
  current=0
  while [ "$current" -lt "$attempts" ]; do
    if curl --fail --silent --show-error "$url" >/dev/null 2>&1; then
      return 0
    fi
    current=$((current + 1))
    sleep 1
  done
  echo "$label did not become reachable: $url" >&2
  return 1
}

HTTP_PORT=$(free_port)
MINIO_API_PORT=$(free_port)
MINIO_CONSOLE_PORT=$(free_port)
MLFLOW_PORT=$(free_port)

mkdir -m 0700 "$SECRETS_DIR"
write_secret() {
  name=$1
  value=$2
  umask 077
  printf '%s' "$value" > "$SECRETS_DIR/$name"
  chmod 0600 "$SECRETS_DIR/$name"
}

write_secret postgres_password "StackSmokePostgresPassword12345"
write_secret maintenance_postgres_password "StackSmokeMaintenancePostgresPassword123"
write_secret mlflow_postgres_password "StackSmokeMlflowPostgresPassword123"
write_secret redis_password "StackSmokeRedisPassword123456789"
write_secret maintenance_redis_password "StackSmokeMaintenanceRedisPassword123"
write_secret minio_root_user "stacksmokeroot"
write_secret minio_root_password "StackSmokeMinioRootPassword123456"
write_secret minio_app_access_key "stacksmokeapp"
write_secret minio_app_secret_key "StackSmokeMinioAppSecret123456789"
write_secret maintenance_s3_access_key "stacksmokemaintenance"
write_secret maintenance_s3_secret_key "StackSmokeMaintenanceObjectSecret123"
write_secret mlflow_s3_access_key "stacksmokemlflow"
write_secret mlflow_s3_secret_key "StackSmokeMlflowSecret1234567890"
write_secret worker_bootstrap_token "StackSmokeWorkerBootstrapToken123456"
write_secret worker_token_pepper "StackSmokeWorkerPepper123456789012"
write_secret jwt_secret "StackSmokeJwtSecret12345678901234567890"

umask 077
{
  printf 'COMPOSE_PROJECT_NAME=%s\n' "$PROJECT"
  printf 'ORCHESTRATOR_VERSION=%s\n' "$STACK_VERSION"
  printf 'GIT_COMMIT=%s\n' "$STACK_REVISION"
  printf 'ENVIRONMENT=development\n'
  printf 'PUBLIC_SCHEME=http\n'
  printf 'PUBLIC_SERVER_NAME=127.0.0.1\n'
  printf 'ALLOW_FAKE_WORKERS=true\n'
  printf 'API_IMAGE=%s\n' "$API_IMAGE"
  printf 'WEB_IMAGE=%s\n' "$WEB_IMAGE"
  printf 'MLFLOW_IMAGE=%s\n' "$MLFLOW_IMAGE"
  printf 'POSTGRES_IMAGE=postgres:16-alpine\n'
  printf 'REDIS_IMAGE=redis:7.4-alpine\n'
  printf 'MINIO_IMAGE=minio/minio:RELEASE.2025-04-22T22-12-26Z\n'
  printf 'MINIO_CLIENT_IMAGE=minio/mc:RELEASE.2025-04-16T18-13-26Z\n'
  printf 'NGINX_IMAGE=nginx:1.27-alpine\n'
  printf 'MLFLOW_BASE_IMAGE=ghcr.io/mlflow/mlflow:v3.1.1\n'
  printf 'RVC_IMAGE_PULL_POLICY=missing\n'
  printf 'MANAGER_SECRETS_DIR=%s\n' "$SECRETS_DIR"
  printf 'HTTP_BIND_ADDRESS=127.0.0.1\n'
  printf 'HTTP_PORT=%s\n' "$HTTP_PORT"
  printf 'MINIO_API_BIND_ADDRESS=127.0.0.1\n'
  printf 'MINIO_API_PORT=%s\n' "$MINIO_API_PORT"
  printf 'MINIO_CONSOLE_BIND_ADDRESS=127.0.0.1\n'
  printf 'MINIO_CONSOLE_PORT=%s\n' "$MINIO_CONSOLE_PORT"
  printf 'MLFLOW_BIND_ADDRESS=127.0.0.1\n'
  printf 'MLFLOW_PORT=%s\n' "$MLFLOW_PORT"
  printf 'CORS_ORIGINS=http://127.0.0.1:%s\n' "$HTTP_PORT"
  printf 'S3_PRESIGN_ENDPOINT_URL=http://127.0.0.1:%s\n' "$MINIO_API_PORT"
  printf 'S3_VERIFY_TLS=false\n'
  printf 'MLFLOW_ENABLED=true\n'
  printf 'MLFLOW_FAIL_CLOSED=true\n'
  printf 'RQ_ENABLED=true\n'
  printf 'RATE_LIMIT_ENABLED=true\n'
  printf 'MAINTENANCE_POSTGRES_USER=rvc_maintenance\n'
  printf 'MAINTENANCE_REDIS_USER=rvc_maintenance\n'
} > "$ENV_FILE"
chmod 0600 "$ENV_FILE"

docker info >/dev/null

if [ "$SKIP_BUILD" != 1 ]; then
  compose build api web mlflow
fi

for image in "$API_IMAGE" "$WEB_IMAGE" "$MLFLOW_IMAGE"; do
  [ "$(docker image inspect --format '{{.Os}}' "$image")" = linux ]
  [ "$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$image")" = "$STACK_VERSION" ]
  [ "$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")" = "$STACK_REVISION" ]
done

docker run --rm --network none --read-only --entrypoint /bin/sh "$WEB_IMAGE" -eu -c '
  test -f "/app/.next/server/app/bff/artifacts/[artifactId]/download/route.js"
  test -f "/app/.next/server/app/bff/jobs/[jobId]/artifacts/route.js"
'

compose up -d --remove-orphans

ready=0
attempt=0
while [ "$attempt" -lt 120 ]; do
  if curl --fail --silent --show-error \
    "http://127.0.0.1:$HTTP_PORT/readyz" > "$WORK_ROOT/ready.json" 2>/dev/null; then
    ready=1
    break
  fi
  attempt=$((attempt + 1))
  sleep 2
done
if [ "$ready" != 1 ]; then
  compose ps -a >&2 || true
  compose logs --tail=200 postgres redis minio mlflow api rq-worker web proxy >&2 || true
  echo "Manager full stack did not become ready" >&2
  exit 1
fi

wait_for_url "Manager proxy health" "http://127.0.0.1:$HTTP_PORT/healthz"
wait_for_url "Manager Web UI" "http://127.0.0.1:$HTTP_PORT/"
wait_for_url "MLflow health" "http://127.0.0.1:$MLFLOW_PORT/health"
wait_for_url "MinIO readiness" "http://127.0.0.1:$MINIO_API_PORT/minio/health/ready"

[ "$(compose exec -T api id -u | tr -d '\r')" = 10001 ]
[ "$(compose exec -T rq-worker id -u | tr -d '\r')" = 10001 ]
[ "$(compose exec -T mlflow id -u | tr -d '\r')" = 10002 ]
[ "$(compose exec -T web id -u | tr -d '\r')" = 1001 ]

compose exec -T --user 0:0 api python -c '
import os, stat
base = "/run/secrets"
root = os.path.join(base, "current")
expected = {
    "jwt_secret", "minio_app_access_key", "minio_app_secret_key",
    "postgres_password", "redis_password", "worker_bootstrap_token",
    "worker_token_pepper",
}
base_info = os.stat(base, follow_symlinks=False)
assert stat.S_ISDIR(base_info.st_mode)
assert stat.S_IMODE(base_info.st_mode) == 0o711
assert (base_info.st_uid, base_info.st_gid) == (0, 0)
assert stat.S_ISLNK(os.lstat(root).st_mode)
target = os.readlink(root)
assert target.startswith("generation-") and "/" not in target, target
generation = os.path.join(base, target)
generation_info = os.stat(generation, follow_symlinks=False)
assert stat.S_ISDIR(generation_info.st_mode)
assert stat.S_IMODE(generation_info.st_mode) == 0o710
assert (generation_info.st_uid, generation_info.st_gid) == (0, 10001)
actual = set(os.listdir(root))
assert actual == expected, (actual, expected)
for name in expected:
    info = os.stat(os.path.join(root, name), follow_symlinks=False)
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o400
    assert (info.st_uid, info.st_gid) == (10001, 10001)
    assert info.st_size > 0
'
compose exec -T api python -c '
import os
root = "/run/secrets/current"
expected = {
    "jwt_secret", "minio_app_access_key", "minio_app_secret_key",
    "postgres_password", "redis_password", "worker_bootstrap_token",
    "worker_token_pepper",
}
try:
    os.listdir(root)
except PermissionError:
    pass
else:
    raise AssertionError("API service user unexpectedly enumerated runtime secrets")
flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
for name in expected:
    descriptor = os.open(os.path.join(root, name), flags)
    try:
        assert os.read(descriptor, 1), name
    finally:
        os.close(descriptor)
'
compose exec -T --user 0:0 rq-worker python -c '
import os, stat
base = "/run/secrets"
root = os.path.join(base, "current")
expected = {
    "maintenance_postgres_password", "maintenance_redis_password",
    "maintenance_s3_access_key", "maintenance_s3_secret_key",
}
base_info = os.stat(base, follow_symlinks=False)
assert stat.S_ISDIR(base_info.st_mode)
assert stat.S_IMODE(base_info.st_mode) == 0o711
assert (base_info.st_uid, base_info.st_gid) == (0, 0)
assert stat.S_ISLNK(os.lstat(root).st_mode)
target = os.readlink(root)
assert target.startswith("generation-") and "/" not in target, target
generation = os.path.join(base, target)
generation_info = os.stat(generation, follow_symlinks=False)
assert stat.S_ISDIR(generation_info.st_mode)
assert stat.S_IMODE(generation_info.st_mode) == 0o710
assert (generation_info.st_uid, generation_info.st_gid) == (0, 10001)
actual = set(os.listdir(root))
assert actual == expected, (actual, expected)
for name in expected:
    info = os.stat(os.path.join(root, name), follow_symlinks=False)
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o400
    assert (info.st_uid, info.st_gid) == (10001, 10001)
    assert info.st_size > 0
'
compose exec -T rq-worker python -c '
import os
root = "/run/secrets/current"
expected = {
    "maintenance_postgres_password", "maintenance_redis_password",
    "maintenance_s3_access_key", "maintenance_s3_secret_key",
}
try:
    os.listdir(root)
except PermissionError:
    pass
else:
    raise AssertionError("maintenance service user unexpectedly enumerated runtime secrets")
flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
for name in expected:
    descriptor = os.open(os.path.join(root, name), flags)
    try:
        assert os.read(descriptor, 1), name
    finally:
        os.close(descriptor)
'
compose run --rm --no-deps --user 0:0 --entrypoint python maintenance-db-authz -c '
import os, stat
base = "/run/secrets"
root = os.path.join(base, "current")
expected = {"postgres_password", "maintenance_postgres_password"}
base_info = os.stat(base, follow_symlinks=False)
assert stat.S_ISDIR(base_info.st_mode)
assert stat.S_IMODE(base_info.st_mode) == 0o711
assert (base_info.st_uid, base_info.st_gid) == (0, 0)
assert stat.S_ISLNK(os.lstat(root).st_mode)
target = os.readlink(root)
assert target.startswith("generation-") and "/" not in target, target
generation = os.path.join(base, target)
generation_info = os.stat(generation, follow_symlinks=False)
assert stat.S_ISDIR(generation_info.st_mode)
assert stat.S_IMODE(generation_info.st_mode) == 0o710
assert (generation_info.st_uid, generation_info.st_gid) == (0, 10001)
assert set(os.listdir(root)) == expected
for name in expected:
    info = os.stat(os.path.join(root, name), follow_symlinks=False)
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o400
    assert (info.st_uid, info.st_gid) == (10001, 10001)
    assert info.st_size > 0
'
compose run --rm --no-deps --entrypoint python maintenance-db-authz -c '
import os
root = "/run/secrets/current"
try:
    os.listdir(root)
except PermissionError:
    pass
else:
    raise AssertionError("database authz service user unexpectedly enumerated runtime secrets")
flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
for name in ("postgres_password", "maintenance_postgres_password"):
    descriptor = os.open(os.path.join(root, name), flags)
    try:
        assert os.read(descriptor, 1), name
    finally:
        os.close(descriptor)
'
compose exec -T --user 0:0 mlflow python -c '
import os, stat
base = "/run/secrets"
root = os.path.join(base, "current")
expected = {
    "mlflow_postgres_password", "mlflow_s3_access_key", "mlflow_s3_secret_key",
}
base_info = os.stat(base, follow_symlinks=False)
assert stat.S_ISDIR(base_info.st_mode)
assert stat.S_IMODE(base_info.st_mode) == 0o711
assert (base_info.st_uid, base_info.st_gid) == (0, 0)
assert stat.S_ISLNK(os.lstat(root).st_mode)
target = os.readlink(root)
assert target.startswith("generation-") and "/" not in target, target
generation = os.path.join(base, target)
generation_info = os.stat(generation, follow_symlinks=False)
assert stat.S_ISDIR(generation_info.st_mode)
assert stat.S_IMODE(generation_info.st_mode) == 0o710
assert (generation_info.st_uid, generation_info.st_gid) == (0, 10002)
actual = set(os.listdir(root))
assert actual == expected, (actual, expected)
for name in expected:
    info = os.stat(os.path.join(root, name), follow_symlinks=False)
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o400
    assert (info.st_uid, info.st_gid) == (10002, 10002)
    assert info.st_size > 0
'
compose exec -T mlflow python -c '
import os
root = "/run/secrets/current"
expected = {
    "mlflow_postgres_password", "mlflow_s3_access_key", "mlflow_s3_secret_key",
}
try:
    os.listdir(root)
except PermissionError:
    pass
else:
    raise AssertionError("MLflow service user unexpectedly enumerated runtime secrets")
flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
for name in expected:
    descriptor = os.open(os.path.join(root, name), flags)
    try:
        assert os.read(descriptor, 1), name
    finally:
        os.close(descriptor)
'

compose run --rm --no-deps --entrypoint /bin/sh minio-init -eu -c '
root_user=$(tr -d "\r\n" < /run/secrets/minio_root_user)
root_password=$(tr -d "\r\n" < /run/secrets/minio_root_password)
app_user=$(tr -d "\r\n" < /run/secrets/minio_app_access_key)
app_password=$(tr -d "\r\n" < /run/secrets/minio_app_secret_key)
mlflow_user=$(tr -d "\r\n" < /run/secrets/mlflow_s3_access_key)
mlflow_password=$(tr -d "\r\n" < /run/secrets/mlflow_s3_secret_key)
mc alias set root http://minio:9000 "$root_user" "$root_password" >/dev/null
app_policy=$(mc admin policy entities root --user "$app_user" --json)
flow_policy=$(mc admin policy entities root --user "$mlflow_user" --json)
case "$app_policy" in *\"policies\":\[\"rvc-manager-app\"\]*) ;; *) exit 51 ;; esac
case "$flow_policy" in *\"policies\":\[\"rvc-mlflow-artifacts\"\]*) ;; *) exit 52 ;; esac
mc alias set app http://minio:9000 "$app_user" "$app_password" >/dev/null
mc alias set flow http://minio:9000 "$mlflow_user" "$mlflow_password" >/dev/null
mc ls "app/$S3_BUCKET" >/dev/null
if mc ls "app/$MLFLOW_S3_BUCKET" >/dev/null 2>&1; then
  echo "Manager identity unexpectedly accessed the MLflow bucket" >&2
  exit 53
fi
mc ls "flow/$MLFLOW_S3_BUCKET" >/dev/null
if mc ls "flow/$S3_BUCKET" >/dev/null 2>&1; then
  echo "MLflow identity unexpectedly accessed the Manager bucket" >&2
  exit 54
fi
'

architecture=$(docker image inspect --format '{{.Architecture}}' "$API_IMAGE")
printf 'Manager full Compose stack smoke: PASS (docker_architecture=%s)\n' "$architecture"
