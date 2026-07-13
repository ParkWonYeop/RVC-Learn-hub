#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
# shellcheck source=../common/lib.sh
source "$SCRIPT_DIR/../common/lib.sh"

version=
expected_runtime_id=
runtime_build_manifest=
asset_root=
asset_manifest=
qualification=
qualification_evidence=
output_dir="$REPO_ROOT/dist/installers"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) shift; version=${1:?missing version} ;;
    --runtime-image-id) shift; expected_runtime_id=${1:?missing runtime image ID} ;;
    --runtime-build-manifest) shift; runtime_build_manifest=${1:?missing build manifest} ;;
    --assets) shift; asset_root=${1:?missing asset directory} ;;
    --asset-manifest) shift; asset_manifest=${1:?missing asset manifest} ;;
    --qualification) shift; qualification=${1:?missing qualification manifest} ;;
    --qualification-evidence) shift; qualification_evidence=${1:?missing qualification evidence} ;;
    --output-dir) shift; output_dir=${1:?missing output directory} ;;
    *) rvc_die "unknown qualified Worker release option: $1" ;;
  esac
  shift
done

[[ -n $version && -n $expected_runtime_id && -n $runtime_build_manifest && \
   -n $asset_root && \
   -n $qualification && -n $qualification_evidence ]] || \
  rvc_die "version, runtime image ID, build manifest, assets, qualification, and evidence are required"
rvc_validate_version "$version"
[[ $expected_runtime_id =~ ^sha256:[0-9a-f]{64}$ ]] || \
  rvc_die "qualified Worker runtime image ID must be a SHA-256 digest"
asset_manifest=${asset_manifest:-$asset_root/assets-manifest.json}
for input in \
  "$runtime_build_manifest" "$asset_manifest" "$qualification" "$qualification_evidence"; do
  [[ -f $input && ! -L $input ]] || rvc_die "qualified Worker input is missing or unsafe"
done
[[ -d $asset_root && ! -L $asset_root ]] || \
  rvc_die "qualified Worker asset root is missing or unsafe"

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
  rvc_die "qualified Worker bundles require a committed 40-character source revision"
git_status=$(
  git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=normal 2>/dev/null
) || rvc_die "qualified Worker source status could not be inspected"
[[ -z $git_status ]] || \
  rvc_die "qualified Worker bundles require a clean source tree, including untracked files"
python3 "$REPO_ROOT/tools/verify_release_source.py" --repo-root "$REPO_ROOT" || \
  rvc_die "qualified Worker bundles require a complete non-ignored source closure"

docker_architecture=$(docker info --format '{{.Architecture}}') || \
  rvc_die "Docker daemon architecture could not be inspected"
case "$docker_architecture" in
  amd64|x86_64) ;;
  *) rvc_die "qualified Worker bundles require an amd64 Docker daemon" ;;
esac

runtime_image="rvc-orchestrator-worker:$version"
docker image inspect "$runtime_image" >/dev/null 2>&1 || \
  rvc_die "qualified Worker bundling requires the existing core candidate runtime image"

work_parent=$(mktemp -d "${TMPDIR:-/tmp}/rvc-worker-qualified.XXXXXX")
chmod 0700 "$work_parent"
cleanup_work_parent() {
  local status=$1
  trap - EXIT
  case "$work_parent" in
    "${TMPDIR:-/tmp}"/rvc-worker-qualified.*) rm -r -- "$work_parent" ;;
    *) rvc_warn "refusing to clean unexpected qualified Worker path: $work_parent" ;;
  esac
  exit "$status"
}
trap 'cleanup_work_parent $?' EXIT

inspect_field() {
  local reference=$1 template=$2 value
  value=$(docker image inspect --format "$template" "$reference") || \
    rvc_die "container image inspection failed for $reference"
  [[ $value != *$'\n'* && $value != *$'\r'* ]] || \
    rvc_die "container image inspection returned multiple values for $reference"
  printf '%s' "$value"
}

runtime_id=$(inspect_field "$runtime_image" '{{.Id}}')
[[ $runtime_id =~ ^sha256:[0-9a-f]{64}$ ]] || \
  rvc_die "runtime image ID is not a SHA-256 digest"
[[ $runtime_id == "$expected_runtime_id" ]] || \
  rvc_die "existing runtime image differs from the qualified core candidate ID"
python3 "$REPO_ROOT/infra/worker/runtime/qualification.py" verify-build-manifest \
  --runtime-build-manifest "$runtime_build_manifest" \
  --image "$runtime_image" \
  --release-version "$version" \
  --orchestrator-commit "$git_commit" || \
  rvc_die "runtime build manifest differs from the qualified Worker candidate"

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
  rvc_die "runtime image release labels differ from the qualified Worker candidate"
[[ $runtime_kind == rvc && $runtime_gpu_gate == false && $runtime_profile_gate == false ]] || \
  rvc_die "runtime image identity or pre-qualification gates are invalid"

assert_runtime_tag_identity() {
  local current_id
  current_id=$(inspect_field "$runtime_image" '{{.Id}}')
  [[ $current_id == "$runtime_id" ]] || \
    rvc_die "runtime image tag changed during qualified Worker bundling"
}
assert_runtime_tag_identity

bundle_output="$work_parent/bundle-output"
install -d -m 0700 "$bundle_output"
bash "$SCRIPT_DIR/build-bundle.sh" \
  --version "$version" \
  --output-dir "$bundle_output" \
  --self-contained \
  --include-rvc-runtime-image "$runtime_image" \
  --rvc-runtime-assets "$asset_root" \
  --rvc-runtime-asset-manifest "$asset_manifest" \
  --rvc-runtime-build-manifest "$runtime_build_manifest" \
  --rvc-runtime-qualification "$qualification" \
  --rvc-runtime-qualification-evidence "$qualification_evidence"

private_archive="$bundle_output/$(basename "$archive")"
private_checksum="$private_archive.sha256"
[[ -f $private_archive && ! -L $private_archive && \
   -f $private_checksum && ! -L $private_checksum ]] || \
  rvc_die "qualified Worker builder did not publish the expected private archive pair"
assert_runtime_tag_identity
python3 "$REPO_ROOT/installers/common/publish_release_bundle.py" \
  --archive "$private_archive" \
  --checksum "$private_checksum" \
  --output-dir "$output_dir" \
  --verifier "$REPO_ROOT/installers/common/image_bundle.py" \
  --component worker \
  --version "$version" \
  --source-commit "$git_commit" \
  --runtime-image-id "$expected_runtime_id" >/dev/null || \
  rvc_die "qualified Worker candidate verification or publication failed"
[[ -f $archive && ! -L $archive && -f $archive.sha256 && ! -L $archive.sha256 ]] || \
  rvc_die "verified qualified Worker candidate pair was not published"
rvc_log "qualified Worker bundle candidate created: $archive"
rvc_warn "scan, license, reviewer-attestation, and clean-host release gates remain separate"
