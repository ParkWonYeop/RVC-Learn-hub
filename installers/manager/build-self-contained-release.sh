#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
# shellcheck source=../common/lib.sh
source "$SCRIPT_DIR/../common/lib.sh"

version=
schema_compatibility=
output_dir="$REPO_ROOT/dist/installers"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) shift; version=${1:?missing version} ;;
    --schema-compatibility) shift; schema_compatibility=${1:?missing schema compatibility marker} ;;
    --output-dir) shift; output_dir=${1:?missing output directory} ;;
    *) rvc_die "unknown Manager release option: $1" ;;
  esac
  shift
done

[[ -n $version ]] || rvc_die "--version is required"
[[ -n $schema_compatibility ]] || rvc_die "--schema-compatibility is required"
rvc_validate_version "$version"
[[ $schema_compatibility =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || \
  rvc_die "invalid schema compatibility marker"

rvc_require_command docker
rvc_require_command git
rvc_require_command python3
rvc_require_command bash

git_commit=$(git -C "$REPO_ROOT" rev-parse --verify 'HEAD^{commit}' 2>/dev/null || true)
[[ $git_commit =~ ^[0-9a-f]{40}$ ]] || \
  rvc_die "self-contained Manager releases require a committed 40-character source revision"
[[ -z $(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=normal 2>/dev/null) ]] || \
  rvc_die "self-contained Manager releases require a clean source tree, including untracked files"
python3 "$REPO_ROOT/tools/verify_release_source.py" --repo-root "$REPO_ROOT" || \
  rvc_die "self-contained Manager releases require a complete non-ignored source closure"

build_backend=docker
if docker buildx version >/dev/null 2>&1; then
  build_backend=buildx
else
  docker_architecture=$(docker info --format '{{.Architecture}}') || \
    rvc_die "Docker daemon architecture could not be inspected"
  [[ $docker_architecture != *$'\n'* && $docker_architecture != *$'\r'* ]] || \
    rvc_die "Docker daemon architecture inspection returned multiple values"
  case "$docker_architecture" in
    amd64|x86_64)
      rvc_warn "Docker Buildx is unavailable; using same-architecture docker build with final platform verification"
      ;;
    *)
      rvc_die "Docker Buildx is required to cross-build linux/amd64 images from $docker_architecture"
      ;;
  esac
fi

archive="$output_dir/rvc-manager-$version-linux-amd64.tar.gz"
[[ ! -e $archive && ! -L $archive && ! -e $archive.sha256 && ! -L $archive.sha256 ]] || \
  rvc_die "output already exists: $archive"

api_image="rvc-orchestrator-api:$version"
web_image="rvc-orchestrator-web:$version"
mlflow_image="rvc-orchestrator-mlflow:$version"
postgres_image="postgres:16-alpine"
redis_image="redis:7.4-alpine"
minio_image="minio/minio:RELEASE.2025-04-22T22-12-26Z"
minio_client_image="minio/mc:RELEASE.2025-04-16T18-13-26Z"
nginx_image="nginx:1.27-alpine"
mlflow_base_image="ghcr.io/mlflow/mlflow:v3.1.1"

build_application_image() {
  local reference=$1 dockerfile=$2
  shift 2
  local -a command=(docker)
  if [[ $build_backend == buildx ]]; then
    command+=(buildx build --load)
  else
    command+=(build)
  fi
  command+=(
    --platform linux/amd64
    --pull
    --file "$REPO_ROOT/$dockerfile"
    --tag "$reference"
    --build-arg "RVC_RELEASE_VERSION=$version"
    --build-arg "RVC_SOURCE_COMMIT=$git_commit"
  )
  "${command[@]}" "$@" "$REPO_ROOT"
}

rvc_log "building Manager API image for linux/amd64"
build_application_image "$api_image" apps/api/Dockerfile
rvc_log "building Manager Web image for linux/amd64"
build_application_image "$web_image" apps/web/Dockerfile
rvc_log "building Manager MLflow image for linux/amd64"
build_application_image \
  "$mlflow_image" infra/mlflow/Dockerfile \
  --build-arg "MLFLOW_BASE_IMAGE=$mlflow_base_image"

dependency_images=(
  "$postgres_image"
  "$redis_image"
  "$minio_image"
  "$minio_client_image"
  "$nginx_image"
)
for reference in "${dependency_images[@]}"; do
  rvc_log "pulling Manager dependency image for linux/amd64: $reference"
  docker pull --platform linux/amd64 "$reference"
done

inspect_field() {
  local reference=$1 template=$2 value
  value=$(docker image inspect --format "$template" "$reference") || \
    rvc_die "container image inspection failed for $reference"
  [[ $value != *$'\n'* && $value != *$'\r'* ]] || \
    rvc_die "container image inspection returned multiple values for $reference"
  printf '%s' "$value"
}

verify_image() {
  local role=$1 reference=$2 expected_user=${3-} image_id operating_system architecture user
  image_id=$(inspect_field "$reference" '{{.Id}}')
  operating_system=$(inspect_field "$reference" '{{.Os}}')
  architecture=$(inspect_field "$reference" '{{.Architecture}}')
  user=$(inspect_field "$reference" '{{.Config.User}}')
  [[ $image_id =~ ^sha256:[0-9a-f]{64}$ ]] || \
    rvc_die "container image ID is not a SHA-256 digest for role $role"
  [[ $operating_system == linux && $architecture == amd64 ]] || \
    rvc_die "container image for role $role must be linux/amd64"
  if [[ -n $expected_user && $user != "$expected_user" ]]; then
    rvc_die "container image user mismatch for role $role"
  fi
}

verify_application_labels() {
  local role=$1 reference=$2 actual_version actual_revision
  actual_version=$(inspect_field \
    "$reference" '{{ index .Config.Labels "org.opencontainers.image.version" }}')
  actual_revision=$(inspect_field \
    "$reference" '{{ index .Config.Labels "org.opencontainers.image.revision" }}')
  [[ $actual_version == "$version" && $actual_revision == "$git_commit" ]] || \
    rvc_die "container image release label mismatch for role $role"
}

verify_image api "$api_image" 10001:10001
verify_application_labels api "$api_image"
verify_image web "$web_image" nextjs
verify_application_labels web "$web_image"
verify_image mlflow "$mlflow_image" 10002:10002
verify_application_labels mlflow "$mlflow_image"
verify_image postgres "$postgres_image"
verify_image redis "$redis_image"
verify_image minio "$minio_image"
verify_image minio-client "$minio_client_image"
verify_image nginx "$nginx_image"

bundle_command=(
  bash "$SCRIPT_DIR/build-bundle.sh"
  --version "$version"
  --schema-compatibility "$schema_compatibility"
  --output-dir "$output_dir"
  --self-contained
  --include-image "api=$api_image"
  --include-image "web=$web_image"
  --include-image "mlflow=$mlflow_image"
  --include-image "postgres=$postgres_image"
  --include-image "redis=$redis_image"
  --include-image "minio=$minio_image"
  --include-image "minio-client=$minio_client_image"
  --include-image "nginx=$nginx_image"
)
"${bundle_command[@]}"

[[ -f $archive && ! -L $archive && -f $archive.sha256 && ! -L $archive.sha256 ]] || \
  rvc_die "Manager bundle builder did not publish the expected archive and checksum"
rvc_log "self-contained Manager release created: $archive"
