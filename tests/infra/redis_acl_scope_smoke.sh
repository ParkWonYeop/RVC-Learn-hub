#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
suffix=$$
server=rvc-redis-acl-$suffix
secret_volume=rvc-redis-acl-secrets-$suffix
operator_password=RedisOperatorPassword123456789
maintenance_user=rvc_maintenance
maintenance_password=RedisMaintenancePassword123456789
queue_name=rvc-maintenance

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
  docker volume rm -f "$secret_volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker volume create "$secret_volume" >/dev/null
docker run --rm --user 0:0 --network none -v "$secret_volume:/out" \
  --entrypoint /bin/sh redis:7.4-alpine -c '
    set -eu
    umask 077
    printf "%s" "$1" > /out/redis_password
    printf "%s" "$2" > /out/maintenance_redis_password
    chmod 0600 /out/*
  ' sh "$operator_password" "$maintenance_password"

docker run -d --name "$server" \
  -p "127.0.0.1:$port:6379" \
  -e MAINTENANCE_REDIS_USER="$maintenance_user" \
  -e RQ_QUEUE_NAME="$queue_name" \
  -v "$secret_volume:/run/secrets:ro" \
  -v "$ROOT/infra/redis/entrypoint.sh:/opt/rvc/redis-entrypoint.sh:ro" \
  --entrypoint /opt/rvc/redis-entrypoint.sh redis:7.4-alpine >/dev/null

for attempt in {1..40}; do
  if docker exec "$server" redis-cli --no-auth-warning -a "$operator_password" ping \
    2>/dev/null | grep -qx PONG; then
    break
  fi
  if [[ $attempt == 40 ]]; then
    docker logs "$server" >&2
    exit 1
  fi
  sleep 0.25
done

dryrun() {
  docker exec "$server" redis-cli --no-auth-warning -a "$operator_password" \
    ACL DRYRUN "$maintenance_user" "$@"
}

expect_allowed() {
  result=$(dryrun "$@")
  [[ $result == OK ]] || {
    echo "expected Redis ACL command to be allowed: $1" >&2
    exit 20
  }
}

expect_denied() {
  result=$(dryrun "$@")
  [[ $result == *"permissions"* ]] || {
    echo "expected Redis ACL command to be denied: $1 ($result)" >&2
    exit 21
  }
}

job_id=rvc-maintenance-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
worker_id=rvc-maintenance-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
expect_allowed HSET "rq:job:$job_id" status queued
expect_allowed BLMOVE "rq:queue:$queue_name" "rq:queue:$queue_name:intermediate" LEFT RIGHT 1
expect_allowed HSET "rq:worker:$worker_id" last_heartbeat now
expect_allowed SET "rq:scheduler-lock:$queue_name" 1 EX 60 NX
expect_allowed XADD "rq:results:$job_id" '*' type 1
expect_allowed SET "rvc:maintenance:worker-smoke" 1 EX 60

expect_denied FLUSHDB
expect_denied CONFIG GET '*'
expect_denied ACL WHOAMI
expect_denied MODULE LIST
expect_denied CLIENT LIST
expect_denied CLIENT SETNAME rvc-maintenance-smoke
expect_denied SHUTDOWN NOSAVE
expect_denied KEYS '*'
expect_denied SCAN 0
expect_denied EVAL 'return 1' 0
expect_denied HSET rq:worker:foreign last_heartbeat forged
expect_denied SET rvc:rate-limit:api:forbidden 1
expect_denied PUBLISH "rq:pubsub:$worker_id" stop

operator_dryrun=$(docker exec "$server" redis-cli --no-auth-warning -a "$operator_password" \
  ACL DRYRUN default FLUSHDB)
[[ $operator_dryrun == OK ]] || {
  echo "historical Redis operator identity lost recovery permission" >&2
  exit 22
}

export PYTHONPATH="$ROOT/packages/contracts/src:$ROOT/apps/api/src"
export REDIS_OPERATOR_URL="redis://:$operator_password@127.0.0.1:$port/0"
export REDIS_MAINTENANCE_URL="redis://$maintenance_user:$maintenance_password@127.0.0.1:$port/0"
"$ROOT/.venv/bin/python" - <<'PY'
from datetime import timedelta

from redis import Redis
from rq import Queue, Retry
from rq.job import Job
from rq.registry import StartedJobRegistry
from rq.scheduler import RQScheduler
from rq.serializers import JSONSerializer
from rq.utils import now

from rvc_manager_api.config import Settings
from rvc_manager_api.maintenance_queue import DATASET_CLEANUP_TASK_PATH
from rvc_manager_api.rq_worker import AllowlistedMaintenanceWorker

import os

operator = Redis.from_url(os.environ["REDIS_OPERATOR_URL"])
maintenance = Redis.from_url(os.environ["REDIS_MAINTENANCE_URL"])
queue_name = "rvc-maintenance"
job_id = "rvc-maintenance-" + "c" * 64
queue = Queue(queue_name, connection=operator, serializer=JSONSerializer)
queue.enqueue_call(
    func=DATASET_CLEANUP_TASK_PATH,
    args=("11111111-1111-4111-8111-111111111111",),
    timeout=300,
    result_ttl=86400,
    failure_ttl=604800,
    ttl=86400,
    description="allowlisted Dataset staging cleanup",
    job_id=job_id,
    retry=Retry(max=2, interval=[30, 60]),
)

maintenance_queue = Queue(queue_name, connection=maintenance, serializer=JSONSerializer)
dequeued = Queue.dequeue_any(
    [maintenance_queue],
    1,
    connection=maintenance,
    serializer=JSONSerializer,
)
assert dequeued is not None
job, maintenance_queue = dequeued
settings = Settings(
    process_role="maintenance",
    redis_url=os.environ["REDIS_MAINTENANCE_URL"],
    rq_enabled=True,
    rate_limit_enabled=False,
    mlflow_enabled=False,
)
worker = AllowlistedMaintenanceWorker(
    [maintenance_queue],
    settings,
    name="rvc-maintenance-" + "d" * 64,
    connection=maintenance,
    serializer=JSONSerializer,
)
worker.register_birth()
worker.heartbeat()
worker.prepare_execution(job)
worker.prepare_job_execution(job, remove_from_intermediate_queue=True)
job.started_at = now() - timedelta(seconds=1)
job.ended_at = now()
job._result = {"status": "ok"}
worker.handle_job_success(
    job,
    maintenance_queue,
    StartedJobRegistry(queue_name, connection=maintenance, serializer=JSONSerializer),
)
worker.run_maintenance_tasks()
worker.register_death()

stored = Job.fetch(job_id, connection=operator, serializer=JSONSerializer)
assert stored.get_status(refresh=True).value == "finished"

scheduler = RQScheduler(
    [maintenance_queue],
    connection=maintenance,
    serializer=JSONSerializer,
)
assert scheduler.acquire_locks() == {queue_name}
scheduler.enqueue_scheduled_jobs()
scheduler.heartbeat()
scheduler.release_locks()

maintenance.close()
operator.close()
PY

echo "Redis maintenance ACL scope smoke: PASS"
