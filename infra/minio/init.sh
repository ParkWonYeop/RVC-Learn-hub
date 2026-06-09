#!/bin/sh
set -eu

read_secret() {
  value=$(tr -d '\r\n' < "$1")
  [ -n "$value" ] || {
    echo "required MinIO secret is empty" >&2
    exit 1
  }
  printf '%s' "$value"
}

validate_bucket() {
  case "$1" in
    ''|*[!a-z0-9.-]*|.*|*.)
      echo "invalid bucket name" >&2
      exit 1
      ;;
  esac
}

validate_access_key() {
  case "$1" in
    ''|*[!A-Za-z0-9._-]*)
      echo "invalid service access key" >&2
      exit 1
      ;;
  esac
}

write_bucket_policy() {
  policy_path=$1
  bucket=$2
  cat > "$policy_path" <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetBucketLocation",
        "s3:ListBucket",
        "s3:ListBucketMultipartUploads"
      ],
      "Resource": ["arn:aws:s3:::$bucket"]
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:AbortMultipartUpload",
        "s3:DeleteObject",
        "s3:GetObject",
        "s3:ListMultipartUploadParts",
        "s3:PutObject"
      ],
      "Resource": ["arn:aws:s3:::$bucket/*"]
    }
  ]
}
POLICY
}

write_maintenance_cleanup_policy() {
  policy_path=$1
  bucket=$2
  cat > "$policy_path" <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:DeleteObject"],
      "Resource": [
        "arn:aws:s3:::$bucket/datasets/staging/*",
        "arn:aws:s3:::$bucket/test-sets/staging/*"
      ]
    }
  ]
}
POLICY
}

require_unversioned_bucket() {
  bucket=$1
  versioning=$(mc version info "local/$bucket" 2>&1) || {
    echo "could not verify MinIO bucket versioning state" >&2
    exit 1
  }
  [ "$versioning" = "local/$bucket is un-versioned" ] || {
    echo "MinIO bucket versioning must be disabled for exact cleanup semantics: $versioning" >&2
    exit 1
  }
}

attach_exact_policy() {
  user=$1
  policy=$2
  entities=
  mc admin policy attach local "$policy" --user "$user" >/dev/null
  for built_in_policy in readwrite readonly writeonly consoleAdmin diagnostics; do
    mc admin policy detach local "$built_in_policy" --user "$user" >/dev/null
  done
  entities=$(mc admin policy entities local --user "$user" --json)
  case "$entities" in
    *\"user\":\"$user\"*\"policies\":\[\"$policy\"\]*) ;;
    *)
      echo "service user policy scope is not exact" >&2
      exit 1
      ;;
  esac
}

root_user=$(read_secret /run/secrets/minio_root_user)
root_password=$(read_secret /run/secrets/minio_root_password)
app_access_key=$(read_secret /run/secrets/minio_app_access_key)
app_secret_key=$(read_secret /run/secrets/minio_app_secret_key)
mlflow_access_key=$(read_secret /run/secrets/mlflow_s3_access_key)
mlflow_secret_key=$(read_secret /run/secrets/mlflow_s3_secret_key)
maintenance_access_key=$(read_secret "${MAINTENANCE_S3_ACCESS_KEY_FILE:-/run/secrets/maintenance_s3_access_key}")
maintenance_secret_key=$(read_secret "${MAINTENANCE_S3_SECRET_KEY_FILE:-/run/secrets/maintenance_s3_secret_key}")

validate_bucket "$S3_BUCKET"
validate_bucket "$MLFLOW_S3_BUCKET"
validate_access_key "$app_access_key"
validate_access_key "$mlflow_access_key"
validate_access_key "$maintenance_access_key"
if [ "$app_access_key" = "$mlflow_access_key" ] || \
   [ "$app_access_key" = "$maintenance_access_key" ] || \
   [ "$mlflow_access_key" = "$maintenance_access_key" ]; then
  echo "MinIO service access keys must be distinct" >&2
  exit 1
fi

mc alias set local http://minio:9000 "$root_user" "$root_password" >/dev/null
mc mb --ignore-existing "local/$S3_BUCKET" >/dev/null
mc mb --ignore-existing "local/$MLFLOW_S3_BUCKET" >/dev/null
require_unversioned_bucket "$S3_BUCKET"
require_unversioned_bucket "$MLFLOW_S3_BUCKET"

app_policy_path=$(mktemp /tmp/rvc-manager-policy.XXXXXX.json)
mlflow_policy_path=$(mktemp /tmp/rvc-mlflow-policy.XXXXXX.json)
maintenance_policy_path=$(mktemp /tmp/rvc-maintenance-policy.XXXXXX.json)
trap 'rm -f "$app_policy_path" "$mlflow_policy_path" "$maintenance_policy_path"' EXIT
write_bucket_policy "$app_policy_path" "$S3_BUCKET"
write_bucket_policy "$mlflow_policy_path" "$MLFLOW_S3_BUCKET"
write_maintenance_cleanup_policy "$maintenance_policy_path" "$S3_BUCKET"
mc admin policy create local rvc-manager-app "$app_policy_path" >/dev/null
mc admin policy create local rvc-mlflow-artifacts "$mlflow_policy_path" >/dev/null
mc admin policy create local rvc-maintenance-staging-cleanup "$maintenance_policy_path" >/dev/null

# `user add` and policy attachment are safe to repeat. Credentials remain in
# Docker secret files and are never printed by this script.
mc admin user add local "$app_access_key" "$app_secret_key" >/dev/null
attach_exact_policy "$app_access_key" rvc-manager-app
mc admin user add local "$mlflow_access_key" "$mlflow_secret_key" >/dev/null
attach_exact_policy "$mlflow_access_key" rvc-mlflow-artifacts
mc admin user add local "$maintenance_access_key" "$maintenance_secret_key" >/dev/null
attach_exact_policy "$maintenance_access_key" rvc-maintenance-staging-cleanup

echo "MinIO buckets and service users are ready"
