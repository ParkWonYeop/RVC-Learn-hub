#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../../.." && pwd)
# shellcheck source=runtime.lock.env
source "$SCRIPT_DIR/runtime.lock.env"

log() {
  printf '[rvc-runtime-build] %s\n' "$*"
}

die() {
  printf '[rvc-runtime-build] error: %s\n' "$*" >&2
  exit 1
}

source_archive=
source_manifest=
wheelhouse_root=
wheelhouse_manifest=
asset_root=
asset_manifest=
base_image=
image_tag=
output_manifest=
verify_only=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-archive) shift; source_archive=${1:?missing source archive} ;;
    --source-manifest) shift; source_manifest=${1:?missing source manifest} ;;
    --wheelhouse) shift; wheelhouse_root=${1:?missing wheelhouse directory} ;;
    --wheelhouse-manifest) shift; wheelhouse_manifest=${1:?missing wheelhouse manifest} ;;
    --assets) shift; asset_root=${1:?missing asset directory} ;;
    --asset-manifest) shift; asset_manifest=${1:?missing asset manifest} ;;
    --base-image) shift; base_image=${1:?missing digest-pinned base image} ;;
    --tag) shift; image_tag=${1:?missing image tag} ;;
    --output-manifest) shift; output_manifest=${1:?missing output manifest path} ;;
    --verify-only) verify_only=1 ;;
    *) die "unknown runtime build option: $1" ;;
  esac
  shift
done

[[ -n $source_archive && -n $source_manifest && -n $wheelhouse_root && \
   -n $asset_root ]] || die "source archive/manifest, wheelhouse, and assets are required"
wheelhouse_manifest=${wheelhouse_manifest:-$wheelhouse_root/wheelhouse-manifest.json}
asset_manifest=${asset_manifest:-$asset_root/assets-manifest.json}
for path in "$source_archive" "$source_manifest" "$wheelhouse_manifest" "$asset_manifest"; do
  [[ -f $path && ! -L $path ]] || die "required runtime input is missing or unsafe: $path"
done
for path in "$wheelhouse_root" "$asset_root"; do
  [[ -d $path && ! -L $path ]] || die "required runtime input directory is missing or unsafe: $path"
done

python_command=${RVC_RUNTIME_VERIFY_PYTHON:-python3}
command -v "$python_command" >/dev/null 2>&1 || die "Python 3 is required for input verification"

work_parent=$(mktemp -d "${TMPDIR:-/tmp}/rvc-runtime-context.XXXXXX")
chmod 0700 "$work_parent"
cleanup_context() {
  case "$work_parent" in
    "${TMPDIR:-/tmp}"/rvc-runtime-context.*) rm -r -- "$work_parent" ;;
    *) log "refusing to clean unexpected context path: $work_parent" ;;
  esac
}
trap cleanup_context EXIT

# Verify the operator inputs once, copy only verified regular files into a private
# snapshot, then verify that snapshot again. Everything below consumes only this
# snapshot, so a concurrent replacement of an original input fails closed.
"$python_command" "$SCRIPT_DIR/verify_inputs.py" all \
  --source-manifest "$source_manifest" \
  --source-archive "$source_archive" \
  --wheelhouse-manifest "$wheelhouse_manifest" \
  --wheelhouse-root "$wheelhouse_root" \
  --asset-manifest "$asset_manifest" \
  --asset-root "$asset_root" >/dev/null

snapshot="$work_parent/input-snapshot"
snapshot_source="$snapshot/source"
snapshot_wheelhouse="$snapshot/wheelhouse"
snapshot_assets="$snapshot/assets"
install -d -m 0700 "$snapshot" "$snapshot_source" "$snapshot_wheelhouse" "$snapshot_assets"

snapshot_source_archive="$snapshot_source/$(basename "$source_archive")"
snapshot_source_manifest="$snapshot_source/source-manifest.json"
install -m 0600 "$source_archive" "$snapshot_source_archive"
install -m 0600 "$source_manifest" "$snapshot_source_manifest"

for wheelhouse_file in "$wheelhouse_root"/*; do
  [[ -f $wheelhouse_file && ! -L $wheelhouse_file ]] || \
    die "wheelhouse changed or contains an unsafe entry while snapshotting"
  install -m 0600 "$wheelhouse_file" \
    "$snapshot_wheelhouse/$(basename "$wheelhouse_file")"
done
snapshot_wheelhouse_manifest="$snapshot_wheelhouse/$(basename "$wheelhouse_manifest")"

asset_listing="$work_parent/verified-asset-files.tsv"
"$python_command" "$SCRIPT_DIR/verify_inputs.py" assets \
  --manifest "$asset_manifest" --root "$asset_root" --emit-files > "$asset_listing"
snapshot_asset_manifest="$snapshot_assets/$(basename "$asset_manifest")"
install -m 0600 "$asset_manifest" "$snapshot_asset_manifest"
while IFS=$'\t' read -r relative executable; do
  [[ -n $relative && ( $executable == 0 || $executable == 1 ) ]] || \
    die "verified asset listing is malformed"
  source_path="$asset_root/$relative"
  [[ -f $source_path && ! -L $source_path ]] || \
    die "asset changed or became unsafe while snapshotting: $relative"
  destination="$snapshot_assets/$relative"
  install -d -m 0700 "$(dirname "$destination")"
  if [[ $executable == 1 ]]; then
    install -m 0700 "$source_path" "$destination"
  else
    install -m 0600 "$source_path" "$destination"
  fi
done < "$asset_listing"

"$python_command" "$SCRIPT_DIR/verify_inputs.py" all \
  --source-manifest "$snapshot_source_manifest" \
  --source-archive "$snapshot_source_archive" \
  --wheelhouse-manifest "$snapshot_wheelhouse_manifest" \
  --wheelhouse-root "$snapshot_wheelhouse" \
  --asset-manifest "$snapshot_asset_manifest" \
  --asset-root "$snapshot_assets" >/dev/null
log "source, wheelhouse, and assets copied to a private snapshot and reverified"
if [[ $verify_only == 1 ]]; then
  log "verification-only mode completed; no image was built"
  exit 0
fi

[[ -n $base_image && -n $image_tag && -n $output_manifest ]] || \
  die "--base-image, --tag, and --output-manifest are required for a build"
[[ $base_image =~ ^pytorch/pytorch:2\.6\.0-cuda12\.4-cudnn9-runtime@sha256:[0-9a-f]{64}$ ]] || \
  die "base image must be the fixed PyTorch 2.6.0/CUDA 12.4/cuDNN 9 tag plus an amd64-reviewed digest"
[[ $image_tag =~ ^[A-Za-z0-9][A-Za-z0-9./_-]*:[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || \
  die "invalid runtime image tag"
release_version=${image_tag##*:}
command -v git >/dev/null 2>&1 || die "Git is required to export the exact orchestrator source"
orchestrator_source_commit=$(
  git -C "$REPO_ROOT" rev-parse --verify 'HEAD^{commit}' 2>/dev/null || true
)
[[ $orchestrator_source_commit =~ ^[0-9a-f]{40}$ ]] || \
  die "runtime image builds require a committed 40-character orchestrator revision"
orchestrator_source_status=$(
  git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=normal 2>/dev/null
) || die "runtime image build source status could not be inspected"
[[ -z $orchestrator_source_status ]] || \
  die "runtime image builds require a clean orchestrator source tree"
"$python_command" "$REPO_ROOT/tools/verify_release_source.py" --repo-root "$REPO_ROOT" || \
  die "runtime image builds require a complete non-ignored source closure"
[[ ! -e $output_manifest ]] || die "output manifest already exists: $output_manifest"
command -v docker >/dev/null 2>&1 || die "Docker is required to build the runtime image"
docker_architecture=$(docker info --format '{{.Architecture}}') || \
  die "Docker daemon architecture could not be inspected"
case "$docker_architecture" in
  amd64|x86_64) ;;
  *) die "offline runtime image builds require an amd64 Docker daemon" ;;
esac
docker image inspect "$base_image" >/dev/null 2>&1 || \
  die "digest-pinned base image is not loaded locally; offline build will not pull it"
base_platform=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$base_image")
[[ $base_platform == linux/amd64 ]] || die "runtime base image must be linux/amd64"

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

source_manifest_hash=$(sha256_file "$snapshot_source_manifest")
wheelhouse_manifest_hash=$(sha256_file "$snapshot_wheelhouse_manifest")
asset_manifest_hash=$(sha256_file "$snapshot_asset_manifest")
fairseq_commit=$("$python_command" -c \
  'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["fairseq"]["commit"])' \
  "$snapshot_wheelhouse_manifest")
[[ $fairseq_commit =~ ^[0-9a-f]{40}$ ]] || die "verified fairseq commit is invalid"

context="$work_parent/context"
install -d -m 0755 \
  "$context/apps" "$context/packages" "$context/runtime" "$context/manifests" \
  "$context/wheelhouse" "$context/source-extract"
orchestrator_source_archive="$work_parent/orchestrator-source.tar"
git -C "$REPO_ROOT" archive \
  --format=tar \
  --output="$orchestrator_source_archive" \
  "$orchestrator_source_commit" \
  apps/worker packages/contracts infra/worker/runtime || \
  die "exact orchestrator source export failed"
[[ -f $orchestrator_source_archive && ! -L $orchestrator_source_archive ]] || \
  die "exact orchestrator source export is missing or unsafe"
tar --no-same-owner --no-same-permissions -xf "$orchestrator_source_archive" -C "$context"
[[ -f $context/apps/worker/pyproject.toml && \
   -f $context/packages/contracts/pyproject.toml && \
   -f $context/infra/worker/runtime/runtime.lock.env ]] || \
  die "exact orchestrator source export is incomplete"
install -m 0644 "$context/apps/worker/Dockerfile.rvc" "$context/Dockerfile"
for runtime_file in \
  verify_inputs.py runtime_preflight.py runtime-entrypoint.sh git-pin-shim.sh; do
  install -m 0755 \
    "$context/infra/worker/runtime/$runtime_file" "$context/runtime/$runtime_file"
done
install -m 0644 \
  "$context/infra/worker/runtime/runtime.lock.env" "$context/runtime/runtime.lock.env"
rm -r -- "$context/infra"
install -m 0644 "$snapshot_source_manifest" "$context/manifests/source-manifest.json"
install -m 0644 "$snapshot_wheelhouse_manifest" "$context/manifests/wheelhouse-manifest.json"
for wheelhouse_file in "$snapshot_wheelhouse"/*; do
  install -m 0644 "$wheelhouse_file" "$context/wheelhouse/$(basename "$wheelhouse_file")"
done

source_root=$("$python_command" "$SCRIPT_DIR/verify_inputs.py" source \
  --manifest "$snapshot_source_manifest" --archive "$snapshot_source_archive" --emit-root)
tar --no-same-owner --no-same-permissions -xzf "$snapshot_source_archive" \
  -C "$context/source-extract"
[[ -d $context/source-extract/$source_root && ! -L $context/source-extract/$source_root ]] || \
  die "verified source root was not extracted"
mv "$context/source-extract/$source_root" "$context/rvc-webui"
rmdir "$context/source-extract"
printf '%s\n' "$RVC_SOURCE_COMMIT" > "$context/rvc-webui/.rvc-reviewed-commit"
chmod 0644 "$context/rvc-webui/.rvc-reviewed-commit"

while IFS=$'\t' read -r relative executable; do
  [[ -n $relative && ( $executable == 0 || $executable == 1 ) ]] || \
    die "verified asset listing is malformed"
  source_path="$snapshot_assets/$relative"
  destination="$context/rvc-webui/$relative"
  install -d -m 0755 "$(dirname "$destination")"
  if [[ $executable == 1 ]]; then
    install -m 0755 "$source_path" "$destination"
  else
    install -m 0644 "$source_path" "$destination"
  fi
done < <("$python_command" "$SCRIPT_DIR/verify_inputs.py" assets \
  --manifest "$snapshot_asset_manifest" --root "$snapshot_assets" --emit-files)
install -m 0644 "$snapshot_asset_manifest" "$context/rvc-webui/assets-manifest.json"
install -m 0644 "$snapshot_source_manifest" "$context/rvc-webui/source-manifest.json"
projection_manifest="$context/rvc-webui/projection-manifest.json"
"$python_command" "$SCRIPT_DIR/verify_inputs.py" projection \
  --root "$context/rvc-webui" \
  --source-manifest "$context/rvc-webui/source-manifest.json" \
  --asset-manifest "$context/rvc-webui/assets-manifest.json" \
  --output "$projection_manifest" >/dev/null
projection_manifest_hash=$(sha256_file "$projection_manifest")
printf '%s\n' "$projection_manifest_hash" > \
  "$context/rvc-webui/projection-manifest.sha256"
chmod 0644 "$context/rvc-webui/projection-manifest.sha256"

log "building with network disabled and a preloaded digest-pinned base image"
docker build \
  --platform linux/amd64 \
  --network=none \
  --pull=false \
  --build-arg "RVC_BASE_IMAGE=$base_image" \
  --build-arg "RVC_BASE_IMAGE_REF=$base_image" \
  --build-arg "RVC_SOURCE_MANIFEST_SHA256=$source_manifest_hash" \
  --build-arg "RVC_WHEELHOUSE_MANIFEST_SHA256=$wheelhouse_manifest_hash" \
  --build-arg "RVC_ASSET_MANIFEST_SHA256=$asset_manifest_hash" \
  --build-arg "RVC_PROJECTION_MANIFEST_SHA256=$projection_manifest_hash" \
  --build-arg "RVC_FAIRSEQ_COMMIT=$fairseq_commit" \
  --build-arg "RVC_RELEASE_VERSION=$release_version" \
  --build-arg "RVC_SOURCE_COMMIT=$orchestrator_source_commit" \
  --tag "$image_tag" \
  --file "$context/Dockerfile" \
  "$context"

for label_and_expected in \
  "org.opencontainers.image.version=$release_version" \
  "org.opencontainers.image.revision=$orchestrator_source_commit" \
  "org.rvc-orchestrator.rvc.commit=$RVC_SOURCE_COMMIT" \
  "org.rvc-orchestrator.rvc.python=$RVC_PYTHON_VERSION" \
  "org.rvc-orchestrator.rvc.torch=$RVC_TORCH_VERSION" \
  "org.rvc-orchestrator.rvc.cuda=$RVC_CUDA_RUNTIME_VERSION" \
  "org.rvc-orchestrator.rvc.cudnn=$RVC_CUDNN_MAJOR" \
  "org.rvc-orchestrator.rvc.base=$base_image" \
  "org.rvc-orchestrator.rvc.source.sha256=$source_manifest_hash" \
  "org.rvc-orchestrator.rvc.assets.sha256=$asset_manifest_hash" \
  "org.rvc-orchestrator.rvc.projection.sha256=$projection_manifest_hash" \
  "org.rvc-orchestrator.rvc.wheelhouse.sha256=$wheelhouse_manifest_hash" \
  "org.rvc-orchestrator.rvc.fairseq.commit=$fairseq_commit" \
  "org.rvc-orchestrator.gpu-smoke-verified=false" \
  "org.rvc-orchestrator.profile-stage-set-verified=false"; do
  label=${label_and_expected%%=*}
  expected=${label_and_expected#*=}
  actual=$(docker image inspect --format "{{ index .Config.Labels \"$label\" }}" "$image_tag")
  [[ $actual == "$expected" ]] || die "built image label verification failed: $label"
done

output_directory=$(dirname "$output_manifest")
install -d -m 0755 "$output_directory"
umask 077
temporary_manifest=$(mktemp "$output_directory/.rvc-runtime-build.XXXXXX")
cat > "$temporary_manifest" <<MANIFEST
RUNTIME_BUILD_FORMAT_VERSION=1
PRODUCT=rvc-training-orchestrator
COMPONENT=worker-rvc-runtime
IMAGE=$image_tag
RELEASE_VERSION=$release_version
ORCHESTRATOR_SOURCE_COMMIT=$orchestrator_source_commit
BASE_IMAGE=$base_image
RVC_SOURCE_COMMIT=$RVC_SOURCE_COMMIT
RVC_SOURCE_MANIFEST_SHA256=$source_manifest_hash
RVC_WHEELHOUSE_MANIFEST_SHA256=$wheelhouse_manifest_hash
RVC_ASSET_MANIFEST_SHA256=$asset_manifest_hash
RVC_PROJECTION_MANIFEST_SHA256=$projection_manifest_hash
RVC_FAIRSEQ_COMMIT=$fairseq_commit
RVC_TORCH_VERSION=$RVC_TORCH_VERSION
RVC_CUDA_RUNTIME_VERSION=$RVC_CUDA_RUNTIME_VERSION
RVC_CUDNN_MAJOR=$RVC_CUDNN_MAJOR
GPU_SMOKE_VERIFIED=false
PROFILE_STAGE_SET_VERIFIED=false
MANIFEST
chmod 0600 "$temporary_manifest"
mv "$temporary_manifest" "$output_manifest"
log "created offline RVC runtime image: $image_tag"
log "GPU smoke remains a release gate: $output_manifest"
