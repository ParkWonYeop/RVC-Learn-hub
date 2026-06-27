#!/bin/sh
set -eu

mlflow_password=$(tr -d '\r\n' < "$MLFLOW_POSTGRES_PASSWORD_FILE")
case "$MLFLOW_POSTGRES_USER:$MLFLOW_POSTGRES_DB:$mlflow_password" in
  *[!A-Za-z0-9_:.-]*)
    echo "MLflow PostgreSQL bootstrap values contain unsupported characters" >&2
    exit 1
    ;;
esac

# PostgreSQL init hooks run only for a new data directory. The role and database
# checks still make this hook idempotent when it is invoked manually.
psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  --set mlflow_user="$MLFLOW_POSTGRES_USER" \
  --set mlflow_database="$MLFLOW_POSTGRES_DB" \
  --set mlflow_password="$mlflow_password" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'mlflow_user', :'mlflow_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'mlflow_user') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'mlflow_user', :'mlflow_password') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'mlflow_database', :'mlflow_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'mlflow_database') \gexec
SQL

