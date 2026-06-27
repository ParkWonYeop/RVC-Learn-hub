#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
# shellcheck source=../common/lib.sh
source "$SCRIPT_DIR/../common/lib.sh"

version=
output_dir="$REPO_ROOT/dist/installers"
images=()
self_contained=false
schema_compatibility=unknown
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) shift; version=${1:?missing version} ;;
    --output-dir) shift; output_dir=${1:?missing output directory} ;;
    --include-image) shift; images+=("${1:?missing image name}") ;;
    --self-contained) self_contained=true ;;
    --schema-compatibility) shift; schema_compatibility=${1:?missing schema compatibility marker} ;;
    *) rvc_die "unknown bundle option: $1" ;;
  esac
  shift
done
[[ -n $version ]] || rvc_die "--version is required"
rvc_validate_version "$version"
[[ $schema_compatibility =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || \
  rvc_die "invalid schema compatibility marker"
rvc_require_command tar
rvc_require_command python3
rvc_require_command sed

work_parent=$(mktemp -d "${TMPDIR:-/tmp}/rvc-manager-bundle.XXXXXX")
cleanup_work_parent() {
  case "$work_parent" in
    "${TMPDIR:-/tmp}"/rvc-manager-bundle.*) rm -r -- "$work_parent" ;;
    *) rvc_warn "refusing to clean unexpected temporary path: $work_parent" ;;
  esac
}
trap cleanup_work_parent EXIT
stage="$work_parent/rvc-manager-$version-linux-amd64"
install -d -m 0755 "$stage/common"
cp -R "$REPO_ROOT/infra" "$stage/infra"
install -m 0644 "$REPO_ROOT/.env.example" "$stage/.env.example"
sed "s/{{VERSION}}/$version/g" "$SCRIPT_DIR/BUNDLE_README.md" > "$stage/README.md"
sed -e '/@@WORKER_ONLY_BEGIN@@/,/@@WORKER_ONLY_END@@/d' \
  -e '/@@MANAGER_ONLY_BEGIN@@/d' -e '/@@MANAGER_ONLY_END@@/d' \
  -e "s/{{COMPONENT}}/manager/g" -e "s/{{VERSION}}/$version/g" \
  "$SCRIPT_DIR/../common/BUNDLE_TESTING.md" > "$stage/TESTING.md"
install -m 0644 "$REPO_ROOT/docs/TEST_RESULT_TEMPLATE.md" \
  "$stage/TEST_RESULT_TEMPLATE.md"
if grep -Eq '\{\{(COMPONENT|VERSION)\}\}|@@(MANAGER|WORKER)_ONLY_' \
  "$stage/README.md" "$stage/TESTING.md"; then
  rvc_die "Manager bundle documentation contains an unresolved template marker"
fi
chmod 0644 "$stage/README.md" "$stage/TESTING.md" "$stage/TEST_RESULT_TEMPLATE.md"
install -m 0644 "$SCRIPT_DIR/../common/lib.sh" "$stage/common/lib.sh"
install -m 0644 "$SCRIPT_DIR/../common/image_bundle.py" "$stage/common/image_bundle.py"
for script in \
  install.sh upgrade.sh uninstall.sh preflight.sh compose.sh bootstrap-admin.sh \
  backup.sh restore.sh rollback.sh; do
  install -m 0755 "$SCRIPT_DIR/$script" "$stage/$script"
done
install -m 0644 "$SCRIPT_DIR/recovery_archive.py" "$stage/recovery_archive.py"
python3 "$REPO_ROOT/tools/generate_supply_chain_report.py" \
  --component manager \
  --version "$version" \
  --output-dir "$stage/supply-chain"

git_commit=$(git -C "$REPO_ROOT" rev-parse --verify HEAD 2>/dev/null || printf 'uncommitted')
if [[ $self_contained == true ]]; then
  [[ $git_commit =~ ^[0-9a-f]{40}$ ]] || \
    rvc_die "self-contained bundles require a committed source revision"
  [[ -z $(git -C "$REPO_ROOT" status --porcelain --untracked-files=normal 2>/dev/null) ]] || \
    rvc_die "self-contained bundles require a clean source tree"
  python3 "$REPO_ROOT/tools/verify_release_source.py" --repo-root "$REPO_ROOT" || \
    rvc_die "self-contained bundles require a complete non-ignored source closure"
fi
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
postgres_image=postgres:16-alpine
redis_image=redis:7.4-alpine
minio_image=minio/minio:RELEASE.2025-04-22T22-12-26Z
minio_client_image=minio/mc:RELEASE.2025-04-16T18-13-26Z
nginx_image=nginx:1.27-alpine
if [[ $self_contained == true ]]; then
  postgres_image=rvc-orchestrator-postgres:$version
  redis_image=rvc-orchestrator-redis:$version
  minio_image=rvc-orchestrator-minio:$version
  minio_client_image=rvc-orchestrator-minio-client:$version
  nginx_image=rvc-orchestrator-nginx:$version
fi
cat > "$stage/manifest.env" <<MANIFEST
BUNDLE_FORMAT_VERSION=2
PRODUCT=rvc-training-orchestrator
COMPONENT=manager
VERSION=$version
SCHEMA_COMPATIBILITY=$schema_compatibility
PLATFORM=linux-amd64
CREATED_AT=$created_at
GIT_COMMIT=$git_commit
SELF_CONTAINED=$self_contained
IMAGES_MANIFEST_FORMAT_VERSION=2
IMAGES_MANIFEST_PATH=images-manifest.json
API_IMAGE=rvc-orchestrator-api:$version
WEB_IMAGE=rvc-orchestrator-web:$version
MLFLOW_IMAGE=rvc-orchestrator-mlflow:$version
SBOM_FORMAT=cyclonedx-1.6
SBOM_PATH=supply-chain/sbom.cdx.json
SBOM_STATUS=partial-release-gates-open
THIRD_PARTY_LICENSES_PATH=supply-chain/third-party-licenses.json
POSTGRES_IMAGE=$postgres_image
REDIS_IMAGE=$redis_image
MINIO_IMAGE=$minio_image
MINIO_CLIENT_IMAGE=$minio_client_image
NGINX_IMAGE=$nginx_image
MANIFEST

image_specs=()
image_source_references=()
image_references=()
image_index=0
for image in ${images[@]+"${images[@]}"}; do
  image_index=$((image_index + 1))
  if [[ $image == *=* ]]; then
    role=${image%%=*}
    source_reference=${image#*=}
  else
    [[ $self_contained == false ]] || \
      rvc_die "self-contained --include-image values must use ROLE=REFERENCE"
    role=extra-$image_index
    source_reference=$image
  fi
  [[ $role =~ ^[a-z][a-z0-9-]{0,63}$ ]] || rvc_die "invalid image role: $role"
  [[ $source_reference =~ ^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}$ ]] || \
    rvc_die "invalid image reference for role $role"
  reference=$source_reference
  if [[ $self_contained == true ]]; then
    case "$role" in
      postgres) reference=rvc-orchestrator-postgres:$version ;;
      redis) reference=rvc-orchestrator-redis:$version ;;
      minio) reference=rvc-orchestrator-minio:$version ;;
      minio-client) reference=rvc-orchestrator-minio-client:$version ;;
      nginx) reference=rvc-orchestrator-nginx:$version ;;
    esac
  fi
  for existing_spec in ${image_specs[@]+"${image_specs[@]}"}; do
    [[ ${existing_spec%%|*} != "$role" ]] || rvc_die "duplicate image role: $role"
  done
  for existing_source_reference in \
    ${image_source_references[@]+"${image_source_references[@]}"}; do
    [[ $existing_source_reference != "$source_reference" ]] || \
      rvc_die "duplicate source image reference: $source_reference"
  done
  for existing_reference in ${image_references[@]+"${image_references[@]}"}; do
    [[ $existing_reference != "$reference" ]] || rvc_die "duplicate image reference: $reference"
  done
  image_specs+=("$role|$source_reference|$reference|images/manager-images.tar.gz")
  image_source_references+=("$source_reference")
  image_references+=("$reference")
done

if (( ${#images[@]} > 0 )); then
  rvc_require_command docker
  install -d -m 0755 "$stage/images"
  for (( image_index=0; image_index<${#image_references[@]}; image_index++ )); do
    source_reference=${image_source_references[$image_index]}
    reference=${image_references[$image_index]}
    [[ $source_reference == "$reference" ]] || docker tag "$source_reference" "$reference"
  done
  printf '%s\n' "${image_references[@]}" > "$stage/images/REQUIRED_IMAGES"
  docker save "${image_references[@]}" | gzip -n > "$stage/images/manager-images.tar.gz"
fi

image_manifest_arguments=()
for image_spec in ${image_specs[@]+"${image_specs[@]}"}; do
  image_manifest_arguments+=(--image "$image_spec")
done
python3 "$SCRIPT_DIR/../common/image_bundle.py" create \
  --root "$stage" --component manager --version "$version" \
  --source-commit "$git_commit" --self-contained "$self_contained" \
  ${image_manifest_arguments[@]+"${image_manifest_arguments[@]}"}

rvc_prune_host_cache_files "$stage"

(
  cd "$stage"
  find . -type f ! -name SHA256SUMS | LC_ALL=C sort | while read -r file; do
    hash=$(rvc_sha256_file "$file")
    printf '%s  %s\n' "$hash" "${file#./}"
  done > SHA256SUMS
)

install -d -m 0755 "$output_dir"
archive="$output_dir/$(basename "$stage").tar.gz"
[[ ! -e $archive && ! -e $archive.sha256 ]] || rvc_die "output already exists: $archive"
COPYFILE_DISABLE=1 tar -C "$work_parent" -czf "$archive" "$(basename "$stage")"
archive_hash=$(rvc_sha256_file "$archive")
printf '%s  %s\n' "$archive_hash" "$(basename "$archive")" > "$archive.sha256"
rvc_log "created Manager bundle: $archive"
