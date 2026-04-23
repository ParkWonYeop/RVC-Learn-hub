#!/bin/sh
set -eu

test "$(id -u):$(id -g)" = "10002:10002"
test "$AWS_ACCESS_KEY_ID" = "$EXPECTED_GENERATION-mlflow_s3_access_key"
test "$AWS_SECRET_ACCESS_KEY" = "$EXPECTED_GENERATION-mlflow_s3_secret_key"
test "$MLFLOW_BACKEND_STORE_URI" = \
  "postgresql+psycopg2://rvc_mlflow:$EXPECTED_GENERATION-mlflow_postgres_password@postgres:5432/rvc_mlflow"
test "$*" = \
  "server --host 0.0.0.0 --port 5000 --serve-artifacts --artifacts-destination s3://rvc-mlflow"

echo "MLflow deployed entrypoint secret read: PASS"
