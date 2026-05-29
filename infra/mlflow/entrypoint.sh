#!/bin/sh
set -eu

read_secret() {
  value=$(tr -d '\r\n' < "$1")
  [ -n "$value" ] || {
    echo "required MLflow secret is empty" >&2
    exit 1
  }
  printf '%s' "$value"
}

db_password=$(read_secret /run/secrets/current/mlflow_postgres_password)
export AWS_ACCESS_KEY_ID
AWS_ACCESS_KEY_ID=$(read_secret /run/secrets/current/mlflow_s3_access_key)
export AWS_SECRET_ACCESS_KEY
AWS_SECRET_ACCESS_KEY=$(read_secret /run/secrets/current/mlflow_s3_secret_key)
export MLFLOW_BACKEND_STORE_URI="postgresql+psycopg2://${MLFLOW_POSTGRES_USER}:${db_password}@postgres:5432/${MLFLOW_POSTGRES_DB}"
unset db_password

exec mlflow server \
  --host 0.0.0.0 \
  --port 5000 \
  --serve-artifacts \
  --artifacts-destination "s3://${MLFLOW_S3_BUCKET}"
