#!/bin/sh
set -eu

read_secret() {
  value=$(tr -d '\r\n' < "$1")
  [ -n "$value" ] || {
    echo "required MinIO root secret is empty" >&2
    exit 1
  }
  printf '%s' "$value"
}

export MINIO_ROOT_USER
MINIO_ROOT_USER=$(read_secret /run/secrets/minio_root_user)
export MINIO_ROOT_PASSWORD
MINIO_ROOT_PASSWORD=$(read_secret /run/secrets/minio_root_password)

exec minio "$@"
