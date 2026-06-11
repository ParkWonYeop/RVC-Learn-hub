#!/bin/sh
set -eu

read_password() {
  password_path=$1
  password_label=$2
  value=$(tr -d '\r\n' < "$password_path")
  if [ -z "$value" ]; then
    echo "$password_label is empty" >&2
    exit 1
  fi
  case "$value" in
    *[!A-Za-z0-9_-]*)
      echo "$password_label contains unsupported characters" >&2
      exit 1
      ;;
  esac
  printf '%s' "$value"
}

operator_password=$(read_password /run/secrets/redis_password "redis operator password")
maintenance_password=$(read_password \
  "${MAINTENANCE_REDIS_PASSWORD_FILE:-/run/secrets/maintenance_redis_password}" \
  "maintenance redis password")
maintenance_user=${MAINTENANCE_REDIS_USER:-rvc_maintenance}
queue_name=${RQ_QUEUE_NAME:-rvc-maintenance}
maintenance_id_glob='????????????????????????????????????????????????????????????????'
execution_id_glob='????????????????????????????????'
case "$maintenance_user" in
  ''|*[!A-Za-z0-9._-]*)
    echo "maintenance redis user is invalid" >&2
    exit 1
    ;;
esac
case "$queue_name" in
  ''|*[!a-z0-9-]*)
    echo "maintenance RQ queue name is invalid" >&2
    exit 1
    ;;
esac

umask 077
config=$(mktemp /tmp/rvc-redis.XXXXXX)
trap 'rm -f "$config"' EXIT HUP INT TERM
{
  printf 'bind 0.0.0.0\n'
  printf 'protected-mode yes\n'
  printf 'port 6379\n'
  printf 'dir /data\n'
  printf 'appendonly yes\n'
  # Keep the historical default/operator password for recovery tooling while
  # giving the runtime RQ worker a separate command/key-scoped ACL identity.
  printf 'requirepass %s\n' "$operator_password"
  printf 'user %s on >%s resetkeys resetchannels ' "$maintenance_user" "$maintenance_password"
  printf '~rvc:maintenance:* '
  printf '~rq:queues '
  printf '~rq:queue:%s ~rq:queue:%s:intermediate ' "$queue_name" "$queue_name"
  printf '~rq:job:rvc-maintenance-%s ' "$maintenance_id_glob"
  printf '~rq:job:rvc-maintenance-%s:dependents ' "$maintenance_id_glob"
  printf '~rq:job::rvc-maintenance-%s:dependencies ' "$maintenance_id_glob"
  printf '~rq:wip:%s ~rq:finished:%s ~rq:failed:%s ' "$queue_name" "$queue_name" "$queue_name"
  printf '~rq:deferred:%s ~rq:scheduled:%s ~rq:canceled:%s ' "$queue_name" "$queue_name" "$queue_name"
  printf '~rq:execution:rvc-maintenance-%s:%s ' \
    "$maintenance_id_glob" "$execution_id_glob"
  printf '~rq:executions:rvc-maintenance-%s ' "$maintenance_id_glob"
  printf '~rq:results:rvc-maintenance-%s ' "$maintenance_id_glob"
  printf '~rq:worker:rvc-maintenance-%s ~rq:workers ~rq:workers:%s ' \
    "$maintenance_id_glob" "$queue_name"
  printf '~rq:scheduler-lock:%s ' "$queue_name"
  printf '+auth +hello +ping +select +echo +quit +client|setinfo +info '
  printf '+exists +hexists +get +set +del +expire +persist '
  printf '+hget +hgetall +hset +hdel +hincrby +hincrbyfloat '
  printf '+sadd +srem +smembers +scard '
  printf '+lmove +blmove +lpos +lrange +lrem +rpush '
  printf '+zadd +zrem +zscore +zrange +zrangebyscore +zremrangebyscore '
  printf '+xadd +multi +exec +watch +unwatch +discard\n'
} > "$config"
chown redis:redis "$config"
unset operator_password maintenance_password

exec docker-entrypoint.sh redis-server "$config"
