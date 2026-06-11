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

postgres_password=$(read_secret /run/secrets/current/postgres_password)
redis_password=$(read_secret /run/secrets/current/redis_password)

export DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${postgres_password}@postgres:5432/${POSTGRES_DB}"
export REDIS_URL="redis://:${redis_password}@redis:6379/${REDIS_DB:-0}"
export S3_ACCESS_KEY_ID
S3_ACCESS_KEY_ID=$(read_secret /run/secrets/current/minio_app_access_key)
export S3_SECRET_ACCESS_KEY
S3_SECRET_ACCESS_KEY=$(read_secret /run/secrets/current/minio_app_secret_key)
export WORKER_BOOTSTRAP_TOKEN
WORKER_BOOTSTRAP_TOKEN=$(read_secret /run/secrets/current/worker_bootstrap_token)
export WORKER_TOKEN_PEPPER
WORKER_TOKEN_PEPPER=$(read_secret /run/secrets/current/worker_token_pepper)

unset postgres_password redis_password
exec "$@"
