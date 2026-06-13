#!/bin/sh
set -eu

read_secret() {
  secret_path=$1
  if [ ! -r "$secret_path" ]; then
    echo "required secret is not readable: $secret_path" >&2
    exit 1
  fi
  value=$(tr -d '\r\n' < "$secret_path")
  if [ -z "$value" ]; then
    echo "required secret is empty: $secret_path" >&2
    exit 1
  fi
  printf '%s' "$value"
}

case "${MAINTENANCE_POSTGRES_USER:-}" in
  ''|[0-9]*|*[!A-Za-z0-9_]*)
    echo "MAINTENANCE_POSTGRES_USER is invalid" >&2
    exit 1
    ;;
esac
case "${MAINTENANCE_REDIS_USER:-}" in
  ''|[0-9]*|*[!A-Za-z0-9_]*)
    echo "MAINTENANCE_REDIS_USER is invalid" >&2
    exit 1
    ;;
esac

postgres_password=$(read_secret /run/secrets/current/maintenance_postgres_password)
redis_password=$(read_secret /run/secrets/current/maintenance_redis_password)
case "$postgres_password" in
  *[!A-Za-z0-9_-]*)
    echo "maintenance database password contains unsupported characters" >&2
    exit 1
    ;;
esac
case "$redis_password" in
  *[!A-Za-z0-9_-]*)
    echo "maintenance Redis password contains unsupported characters" >&2
    exit 1
    ;;
esac

export DATABASE_URL="postgresql+asyncpg://${MAINTENANCE_POSTGRES_USER}:${postgres_password}@postgres:5432/${POSTGRES_DB}"
export REDIS_URL="redis://${MAINTENANCE_REDIS_USER}:${redis_password}@redis:6379/${REDIS_DB:-0}"
export S3_ACCESS_KEY_ID
S3_ACCESS_KEY_ID=$(read_secret /run/secrets/current/maintenance_s3_access_key)
export S3_SECRET_ACCESS_KEY
S3_SECRET_ACCESS_KEY=$(read_secret /run/secrets/current/maintenance_s3_secret_key)

unset postgres_password redis_password
python /opt/rvc/maintenance-db-authz.py verify-runtime
exec "$@"
