#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
# shellcheck source=../common/lib.sh
source "$SCRIPT_DIR/../common/lib.sh"

version=
source_archive=
source_manifest=
wheelhouse_root=
wheelhouse_manifest=
asset_root=
asset_manifest=
base_image=
output_dir="$REPO_ROOT/dist/worker-core-candidates"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) shift; version=${1:?missing version} ;;
    --source-archive) shift; source_archive=${1:?missing source archive} ;;
    --source-manifest) shift; source_manifest=${1:?missing source manifest} ;;
    --wheelhouse) shift; wheelhouse_root=${1:?missing wheelhouse directory} ;;
    --wheelhouse-manifest) shift; wheelhouse_manifest=${1:?missing wheelhouse manifest} ;;
    --assets) shift; asset_root=${1:?missing asset directory} ;;
    --asset-manifest) shift; asset_manifest=${1:?missing asset manifest} ;;
    --base-image) shift; base_image=${1:?missing digest-pinned base image} ;;
    --output-dir) shift; output_dir=${1:?missing output directory} ;;
    *) rvc_die "unknown Worker release option: $1" ;;
  esac
  shift
done

[[ -n $version ]] || rvc_die "--version is required"
[[ -n $source_archive && -n $source_manifest && -n $wheelhouse_root && \
   -n $asset_root && -n $base_image ]] || \
  rvc_die "source archive/manifest, wheelhouse, assets, and base image are required"
rvc_validate_version "$version"
wheelhouse_manifest=${wheelhouse_manifest:-$wheelhouse_root/wheelhouse-manifest.json}
asset_manifest=${asset_manifest:-$asset_root/assets-manifest.json}

archive="$output_dir/rvc-worker-$version-linux-amd64.tar.gz"
[[ ! -e $archive && ! -L $archive && ! -e $archive.sha256 && ! -L $archive.sha256 ]] || \
  rvc_die "output already exists: $archive"
if [[ -e $output_dir || -L $output_dir ]]; then
  [[ -d $output_dir && ! -L $output_dir ]] || \
    rvc_die "output directory must be a real directory"
else
  output_parent=$(dirname "$output_dir")
  if [[ $output_parent == "$REPO_ROOT/dist" && ! -e $output_parent && \
        ! -L $output_parent ]]; then
    install -d -m 0755 "$output_parent"
  fi
  [[ -d $output_parent && ! -L $output_parent ]] || \
    rvc_die "output parent must be a real directory"
  install -d -m 0755 "$output_dir"
fi

rvc_require_command bash
rvc_require_command docker
rvc_require_command git
rvc_require_command python3

git_commit=$(git -C "$REPO_ROOT" rev-parse --verify 'HEAD^{commit}' 2>/dev/null || true)
[[ $git_commit =~ ^[0-9a-f]{40}$ ]] || \
  rvc_die "self-contained Worker releases require a committed 40-character source revision"
git_status=$(
  git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=normal 2>/dev/null
) || rvc_die "Worker release source status could not be inspected"
[[ -z $git_status ]] || \
  rvc_die "self-contained Worker releases require a clean source tree, including untracked files"
python3 "$REPO_ROOT/tools/verify_release_source.py" --repo-root "$REPO_ROOT" || \
  rvc_die "self-contained Worker releases require a complete non-ignored source closure"

docker_architecture=$(docker info --format '{{.Architecture}}') || \
  rvc_die "Docker daemon architecture could not be inspected"
case "$docker_architecture" in
  amd64|x86_64) ;;
  *) rvc_die "self-contained Worker releases require an amd64 Docker daemon" ;;
esac

runtime_image="rvc-orchestrator-worker:$version"
if docker image inspect "$runtime_image" >/dev/null 2>&1; then
  rvc_die "runtime image tag already exists; use a new release version or remove it explicitly"
fi

work_parent=$(mktemp -d "${TMPDIR:-/tmp}/rvc-worker-release.XXXXXX")
chmod 0700 "$work_parent"
runtime_created=false
runtime_id=
release_published=false
cleanup_work_parent() {
  local status=$1 current_id
  trap - EXIT
  if [[ $release_published != true && $runtime_created == true && \
        $runtime_id =~ ^sha256:[0-9a-f]{64}$ ]]; then
    current_id=$(docker image inspect --format '{{.Id}}' "$runtime_image" 2>/dev/null || true)
    if [[ $current_id == "$runtime_id" ]]; then
      docker image rm "$runtime_image" >/dev/null 2>&1 || \
        rvc_warn "could not remove the failed factory-owned runtime tag"
    elif [[ -n $current_id ]]; then
      rvc_warn "runtime tag changed ownership; refusing failure cleanup"
    fi
  fi
  case "$work_parent" in
    "${TMPDIR:-/tmp}"/rvc-worker-release.*) rm -r -- "$work_parent" ;;
    *) rvc_warn "refusing to clean unexpected Worker release path: $work_parent" ;;
  esac
  exit "$status"
}
trap 'cleanup_work_parent $?' EXIT
runtime_build_manifest="$work_parent/runtime-build.env"
runtime_image_id_record="$work_parent/runtime-image-id"

inspect_field() {
  local reference=$1 template=$2 value
  value=$(docker image inspect --format "$template" "$reference") || \
    rvc_die "container image inspection failed for $reference"
  [[ $value != *$'\n'* && $value != *$'\r'* ]] || \
    rvc_die "container image inspection returned multiple values for $reference"
  printf '%s' "$value"
}

runtime_command=(
  bash "$REPO_ROOT/infra/worker/runtime/build-runtime-image.sh"
  --source-archive "$source_archive"
  --source-manifest "$source_manifest"
  --wheelhouse "$wheelhouse_root"
  --wheelhouse-manifest "$wheelhouse_manifest"
  --assets "$asset_root"
  --asset-manifest "$asset_manifest"
  --base-image "$base_image"
  --tag "$runtime_image"
  --output-manifest "$runtime_build_manifest"
  --output-image-id "$runtime_image_id_record"
)
if ! "${runtime_command[@]}"; then
  if [[ -f $runtime_image_id_record && ! -L $runtime_image_id_record ]]; then
    IFS= read -r failed_runtime_id < "$runtime_image_id_record" || true
    if [[ $failed_runtime_id =~ ^sha256:[0-9a-f]{64}$ ]]; then
      runtime_created=true
      runtime_id=$failed_runtime_id
    fi
  fi
  rvc_die "runtime image builder failed"
fi
[[ -f $runtime_image_id_record && ! -L $runtime_image_id_record ]] || \
  rvc_die "runtime image builder did not publish a safe image ID record"
IFS= read -r runtime_id < "$runtime_image_id_record" || \
  rvc_die "runtime image builder published an unreadable image ID record"
[[ $runtime_id =~ ^sha256:[0-9a-f]{64}$ ]] || \
  rvc_die "runtime image ID is not a SHA-256 digest"
runtime_created=true
[[ $(inspect_field "$runtime_image" '{{.Id}}') == "$runtime_id" ]] || \
  rvc_die "runtime image tag differs from the builder ownership record"
[[ -f $runtime_build_manifest && ! -L $runtime_build_manifest ]] || \
  rvc_die "runtime image builder did not publish a safe build manifest"

python3 "$REPO_ROOT/infra/worker/runtime/qualification.py" verify-build-manifest \
  --runtime-build-manifest "$runtime_build_manifest" \
  --image "$runtime_image" \
  --release-version "$version" \
  --orchestrator-commit "$git_commit" || \
  rvc_die "runtime build manifest differs from the requested Worker candidate"

runtime_os=$(inspect_field "$runtime_image" '{{.Os}}')
runtime_architecture=$(inspect_field "$runtime_image" '{{.Architecture}}')
runtime_user=$(inspect_field "$runtime_image" '{{with index .Config "User"}}{{.}}{{end}}')
runtime_version=$(inspect_field \
  "$runtime_image" '{{ index .Config.Labels "org.opencontainers.image.version" }}')
runtime_revision=$(inspect_field \
  "$runtime_image" '{{ index .Config.Labels "org.opencontainers.image.revision" }}')
runtime_kind=$(inspect_field \
  "$runtime_image" '{{ index .Config.Labels "org.rvc-orchestrator.runtime" }}')
runtime_gpu_gate=$(inspect_field \
  "$runtime_image" '{{ index .Config.Labels "org.rvc-orchestrator.gpu-smoke-verified" }}')
runtime_profile_gate=$(inspect_field \
  "$runtime_image" '{{ index .Config.Labels "org.rvc-orchestrator.profile-stage-set-verified" }}')
[[ $runtime_os == linux && $runtime_architecture == amd64 ]] || \
  rvc_die "runtime image must be linux/amd64"
[[ $runtime_user == 10001:10001 ]] || rvc_die "runtime image user must be 10001:10001"
[[ $runtime_version == "$version" && $runtime_revision == "$git_commit" ]] || \
  rvc_die "runtime image release labels differ from the requested Worker release"
[[ $runtime_kind == rvc && $runtime_gpu_gate == false && $runtime_profile_gate == false ]] || \
  rvc_die "runtime image identity or pre-qualification gates are invalid"

assert_runtime_tag_identity() {
  local current_id
  current_id=$(inspect_field "$runtime_image" '{{.Id}}')
  [[ $current_id == "$runtime_id" ]] || \
    rvc_die "runtime image tag changed during Worker candidate creation"
}
assert_runtime_tag_identity

bundle_output="$work_parent/bundle-output"
install -d -m 0700 "$bundle_output"
bundle_command=(
  bash "$SCRIPT_DIR/build-bundle.sh"
  --version "$version"
  --output-dir "$bundle_output"
  --self-contained
  --include-rvc-runtime-image "$runtime_image"
  --rvc-runtime-assets "$asset_root"
  --rvc-runtime-asset-manifest "$asset_manifest"
  --rvc-runtime-build-manifest "$runtime_build_manifest"
)
"${bundle_command[@]}"

private_archive="$bundle_output/$(basename "$archive")"
private_checksum="$private_archive.sha256"
[[ -f $private_archive && ! -L $private_archive && \
   -f $private_checksum && ! -L $private_checksum ]] || \
  rvc_die "Worker bundle builder did not publish the expected private archive pair"
assert_runtime_tag_identity
python3 "$REPO_ROOT/installers/common/publish_release_bundle.py" \
  --archive "$private_archive" \
  --checksum "$private_checksum" \
  --output-dir "$output_dir" \
  --verifier "$REPO_ROOT/installers/common/image_bundle.py" \
  --component worker \
  --version "$version" \
  --source-commit "$git_commit" \
  --runtime-image-id "$runtime_id" >/dev/null || \
  rvc_die "private Worker candidate verification or publication failed"
release_published=true
[[ -f $archive && ! -L $archive && -f $archive.sha256 && ! -L $archive.sha256 ]] || \
  rvc_die "verified Worker candidate pair was not published"
rvc_log "self-contained Worker candidate created: $archive"
rvc_log "core candidate runtime image ID: $runtime_id"
rvc_warn "core-only guarded candidate: GPU/profile/Sample activation gates remain closed"
rvc_warn "use build-qualified-release.sh only after evidence is bound to this exact image ID"
