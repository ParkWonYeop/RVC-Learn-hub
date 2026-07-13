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
runtime_image=
runtime_assets=
runtime_asset_manifest=
runtime_build_manifest=
runtime_qualification=
runtime_qualification_evidence=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) shift; version=${1:?missing version} ;;
    --output-dir) shift; output_dir=${1:?missing output directory} ;;
    --include-image) shift; images+=("${1:?missing image name}") ;;
    --self-contained) self_contained=true ;;
    --include-rvc-runtime-image) shift; runtime_image=${1:?missing runtime image} ;;
    --rvc-runtime-assets) shift; runtime_assets=${1:?missing runtime assets directory} ;;
    --rvc-runtime-asset-manifest) shift; runtime_asset_manifest=${1:?missing asset manifest} ;;
    --rvc-runtime-build-manifest) shift; runtime_build_manifest=${1:?missing build manifest} ;;
    --rvc-runtime-qualification) shift; runtime_qualification=${1:?missing qualification manifest} ;;
    --rvc-runtime-qualification-evidence) shift; runtime_qualification_evidence=${1:?missing qualification evidence archive} ;;
    *) rvc_die "unknown bundle option: $1" ;;
  esac
  shift
done
[[ -n $version ]] || rvc_die "--version is required"
rvc_validate_version "$version"
rvc_require_command tar
rvc_require_command python3
rvc_require_command sed

work_parent=$(mktemp -d "${TMPDIR:-/tmp}/rvc-worker-bundle.XXXXXX")
cleanup_work_parent() {
  case "$work_parent" in
    "${TMPDIR:-/tmp}"/rvc-worker-bundle.*) rm -r -- "$work_parent" ;;
    *) rvc_warn "refusing to clean unexpected temporary path: $work_parent" ;;
  esac
}
trap cleanup_work_parent EXIT
git_commit=$(git -C "$REPO_ROOT" rev-parse --verify 'HEAD^{commit}' 2>/dev/null || \
  printf 'uncommitted')
source_root=$REPO_ROOT
if [[ $self_contained == true ]]; then
  rvc_require_command git
  [[ $git_commit =~ ^[0-9a-f]{40}$ ]] || \
    rvc_die "self-contained bundles require a committed source revision"
  git_status=$(
    git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=normal 2>/dev/null
  ) || rvc_die "self-contained bundle source status could not be inspected"
  [[ -z $git_status ]] || rvc_die "self-contained bundles require a clean source tree"

  committed_archive="$work_parent/committed-source.tar"
  committed_source="$work_parent/committed-source"
  install -d -m 0700 "$committed_source"
  git -C "$REPO_ROOT" archive \
    --format=tar --output="$committed_archive" "$git_commit" || \
    rvc_die "exact committed bundle source export failed"
  tar --no-same-owner --no-same-permissions -xf "$committed_archive" \
    -C "$committed_source" || rvc_die "exact committed bundle source extraction failed"
  rm -f -- "$committed_archive"
  [[ -f $committed_source/tools/verify_release_source.py && \
     ! -L $committed_source/tools/verify_release_source.py ]] || \
    rvc_die "exact committed bundle source export is incomplete"
  python3 "$committed_source/tools/verify_release_source.py" \
    --repo-root "$REPO_ROOT" || \
    rvc_die "self-contained bundles require a complete non-ignored source closure"
  current_commit=$(
    git -C "$REPO_ROOT" rev-parse --verify 'HEAD^{commit}' 2>/dev/null || true
  )
  current_status=$(
    git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=normal 2>/dev/null
  ) || rvc_die "self-contained bundle source status could not be re-inspected"
  [[ $current_commit == "$git_commit" && -z $current_status ]] || \
    rvc_die "self-contained bundle source changed during committed export"
  source_root=$committed_source
fi

stage="$work_parent/rvc-worker-$version-linux-amd64"
install -d -m 0755 "$stage/common"
cp -R "$source_root/infra" "$stage/infra"
activation_path="$stage/infra/worker/runtime/runtime-activation.json"
[[ -f $activation_path && ! -L $activation_path ]] || \
  rvc_die "Worker runtime activation source is missing or unsafe"
chmod 0444 "$activation_path"
install -m 0644 "$source_root/.env.example" "$stage/.env.example"
sed "s/{{VERSION}}/$version/g" \
  "$source_root/installers/worker/BUNDLE_README.md" > "$stage/README.md"
sed -e '/@@MANAGER_ONLY_BEGIN@@/,/@@MANAGER_ONLY_END@@/d' \
  -e '/@@WORKER_ONLY_BEGIN@@/d' -e '/@@WORKER_ONLY_END@@/d' \
  -e "s/{{COMPONENT}}/worker/g" -e "s/{{VERSION}}/$version/g" \
  "$source_root/installers/common/BUNDLE_TESTING.md" > "$stage/TESTING.md"
install -m 0644 "$source_root/docs/TEST_RESULT_TEMPLATE.md" \
  "$stage/TEST_RESULT_TEMPLATE.md"
if grep -Eq '\{\{(COMPONENT|VERSION)\}\}|@@(MANAGER|WORKER)_ONLY_' \
  "$stage/README.md" "$stage/TESTING.md"; then
  rvc_die "Worker bundle documentation contains an unresolved template marker"
fi
chmod 0644 "$stage/README.md" "$stage/TESTING.md" "$stage/TEST_RESULT_TEMPLATE.md"
install -m 0644 "$source_root/installers/common/lib.sh" "$stage/common/lib.sh"
install -m 0644 "$source_root/installers/common/image_bundle.py" \
  "$stage/common/image_bundle.py"
install -m 0644 "$source_root/apps/worker/src/rvc_worker/tls.py" \
  "$stage/common/worker_ca.py"
for script in install.sh upgrade.sh uninstall.sh preflight.sh compose.sh; do
  install -m 0755 "$source_root/installers/worker/$script" "$stage/$script"
done
python3 "$source_root/tools/generate_supply_chain_report.py" \
  --component worker \
  --version "$version" \
  --output-dir "$stage/supply-chain"

runtime_included=false
runtime_asset_manifest_hash=none
runtime_source_commit=none
runtime_base_image=none
runtime_fairseq_commit=none
runtime_source_manifest_hash=none
runtime_wheelhouse_manifest_hash=none
runtime_projection_manifest_hash=none
gpu_smoke_verified=false
profile_stage_set_verified=false
native_sample_inference_verified=false
runtime_arguments=(
  "$runtime_image" "$runtime_assets" "$runtime_asset_manifest" "$runtime_build_manifest"
)
runtime_argument_count=0
for runtime_argument in "${runtime_arguments[@]}"; do
  [[ -z $runtime_argument ]] || runtime_argument_count=$((runtime_argument_count + 1))
done
if (( runtime_argument_count != 0 && runtime_argument_count != 4 )); then
  rvc_die "real runtime bundling requires image, asset root/manifest, and build manifest together"
fi
qualification_argument_count=0
for qualification_argument in "$runtime_qualification" "$runtime_qualification_evidence"; do
  [[ -z $qualification_argument ]] || \
    qualification_argument_count=$((qualification_argument_count + 1))
done
if (( qualification_argument_count != 0 && qualification_argument_count != 2 )); then
  rvc_die "runtime qualification requires its manifest and evidence archive together"
fi
if (( qualification_argument_count == 2 && runtime_argument_count != 4 )); then
  rvc_die "runtime qualification requires the exact bundled RVC runtime"
fi

build_manifest_value() {
  local key=$1 count value
  count=$(awk -F= -v wanted="$key" '$1 == wanted {count++} END {print count+0}' \
    "$runtime_build_manifest")
  [[ $count == 1 ]] || rvc_die "runtime build manifest must contain exactly one $key"
  value=$(awk -F= -v wanted="$key" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' \
    "$runtime_build_manifest")
  printf '%s' "$value"
}

if (( runtime_argument_count == 4 )); then
  [[ $runtime_image == "rvc-orchestrator-worker:$version" ]] || \
    rvc_die "real runtime image must use the installer-selected tag rvc-orchestrator-worker:$version"
  [[ -d $runtime_assets && ! -L $runtime_assets ]] || \
    rvc_die "runtime asset root is missing or unsafe"
  [[ -f $runtime_asset_manifest && ! -L $runtime_asset_manifest ]] || \
    rvc_die "runtime asset manifest is missing or unsafe"
  [[ -f $runtime_build_manifest && ! -L $runtime_build_manifest ]] || \
    rvc_die "runtime build manifest is missing or unsafe"
  rvc_require_command python3
  rvc_require_command docker
  python3 "$source_root/infra/worker/runtime/verify_inputs.py" assets \
    --manifest "$runtime_asset_manifest" --root "$runtime_assets" >/dev/null
  runtime_asset_manifest_hash=$(rvc_sha256_file "$runtime_asset_manifest")
  python3 "$source_root/infra/worker/runtime/qualification.py" \
    verify-build-manifest \
    --runtime-build-manifest "$runtime_build_manifest" \
    --image "$runtime_image" \
    --release-version "$version" \
    --orchestrator-commit "$git_commit" || \
    rvc_die "runtime build manifest is not the exact requested release identity"
  [[ $(build_manifest_value RUNTIME_BUILD_FORMAT_VERSION) == 1 && \
     $(build_manifest_value COMPONENT) == worker-rvc-runtime && \
     $(build_manifest_value IMAGE) == "$runtime_image" && \
     $(build_manifest_value RELEASE_VERSION) == "$version" && \
     $(build_manifest_value ORCHESTRATOR_SOURCE_COMMIT) == "$git_commit" ]] || \
    rvc_die "runtime build manifest identity does not match the requested image"
  runtime_source_commit=$(build_manifest_value RVC_SOURCE_COMMIT)
  runtime_base_image=$(build_manifest_value BASE_IMAGE)
  runtime_fairseq_commit=$(build_manifest_value RVC_FAIRSEQ_COMMIT)
  runtime_source_manifest_hash=$(build_manifest_value RVC_SOURCE_MANIFEST_SHA256)
  runtime_wheelhouse_manifest_hash=$(build_manifest_value RVC_WHEELHOUSE_MANIFEST_SHA256)
  runtime_projection_manifest_hash=$(build_manifest_value RVC_PROJECTION_MANIFEST_SHA256)
  [[ $runtime_source_commit == 7ef19867780cf703841ebafb565a4e47d1ea86ff ]] || \
    rvc_die "runtime build manifest uses an unreviewed RVC commit"
  [[ $(build_manifest_value RVC_ASSET_MANIFEST_SHA256) == "$runtime_asset_manifest_hash" ]] || \
    rvc_die "runtime build and asset manifests disagree"
  [[ $(build_manifest_value GPU_SMOKE_VERIFIED) == false ]] || \
    rvc_die "this foundation cannot certify a GPU smoke result"
  [[ $(build_manifest_value PROFILE_STAGE_SET_VERIFIED) == false ]] || \
    rvc_die "this foundation cannot certify the complete Worker profile stage set"
  [[ $(build_manifest_value RVC_TORCH_VERSION) == 2.6.0+cu124 && \
     $(build_manifest_value RVC_CUDA_RUNTIME_VERSION) == 12.4 && \
     $(build_manifest_value RVC_CUDNN_MAJOR) == 9 ]] || \
    rvc_die "runtime build manifest differs from the reviewed Torch/CUDA/cuDNN lock"
  [[ $runtime_base_image =~ ^pytorch/pytorch:2\.6\.0-cuda12\.4-cudnn9-runtime@sha256:[0-9a-f]{64}$ ]] || \
    rvc_die "runtime build manifest lacks the fixed digest-pinned base image"
  [[ $runtime_fairseq_commit =~ ^[0-9a-f]{40}$ && \
     $runtime_source_manifest_hash =~ ^[0-9a-f]{64}$ && \
     $runtime_wheelhouse_manifest_hash =~ ^[0-9a-f]{64}$ && \
     $runtime_projection_manifest_hash =~ ^[0-9a-f]{64}$ ]] || \
    rvc_die "runtime build manifest provenance is incomplete"
  [[ $(docker image inspect --format '{{.Architecture}}' "$runtime_image") == amd64 ]] || \
    rvc_die "real runtime image must be linux/amd64"
  for label_and_expected in \
    "org.rvc-orchestrator.runtime=rvc" \
    "org.rvc-orchestrator.rvc.commit=$runtime_source_commit" \
    "org.rvc-orchestrator.rvc.python=3.11" \
    "org.rvc-orchestrator.rvc.torch=2.6.0+cu124" \
    "org.rvc-orchestrator.rvc.cuda=12.4" \
    "org.rvc-orchestrator.rvc.cudnn=9" \
    "org.rvc-orchestrator.rvc.base=$runtime_base_image" \
    "org.rvc-orchestrator.rvc.source.sha256=$runtime_source_manifest_hash" \
    "org.rvc-orchestrator.rvc.wheelhouse.sha256=$runtime_wheelhouse_manifest_hash" \
    "org.rvc-orchestrator.rvc.assets.sha256=$runtime_asset_manifest_hash" \
    "org.rvc-orchestrator.rvc.projection.sha256=$runtime_projection_manifest_hash" \
    "org.rvc-orchestrator.rvc.fairseq.commit=$runtime_fairseq_commit" \
    "org.rvc-orchestrator.gpu-smoke-verified=false" \
    "org.rvc-orchestrator.profile-stage-set-verified=false"; do
    label=${label_and_expected%%=*}
    expected=${label_and_expected#*=}
    actual=$(docker image inspect --format "{{ index .Config.Labels \"$label\" }}" \
      "$runtime_image")
    [[ $actual == "$expected" ]] || rvc_die "runtime image label mismatch: $label"
  done
  install -d -m 0755 "$stage/runtime"
  install -m 0644 "$runtime_asset_manifest" "$stage/runtime/assets-manifest.json"
  install -m 0644 "$runtime_build_manifest" "$stage/runtime/build-manifest.env"
  install -m 0644 "$source_root/infra/worker/runtime/runtime.lock.env" \
    "$stage/runtime/runtime.lock.env"
  if (( qualification_argument_count == 2 )); then
    [[ $self_contained == true ]] || \
      rvc_die "qualified runtime activation requires --self-contained"
    [[ -f $runtime_qualification && ! -L $runtime_qualification ]] || \
      rvc_die "runtime qualification manifest is missing or unsafe"
    [[ -f $runtime_qualification_evidence && ! -L $runtime_qualification_evidence ]] || \
      rvc_die "runtime qualification evidence archive is missing or unsafe"
    runtime_qualification_evidence_name=${runtime_qualification_evidence##*/}
    [[ $runtime_qualification_evidence_name =~ \
       ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.tar\.gz$ ]] || \
      rvc_die "runtime qualification evidence must use a safe .tar.gz basename"
    [[ $git_commit =~ ^[0-9a-f]{40}$ ]] || \
      rvc_die "qualified runtime activation requires a committed source revision"
    install -d -m 0755 "$stage/runtime/qualification"
    install -m 0644 "$runtime_qualification" \
      "$stage/runtime/qualification/qualification.json"
    install -m 0644 "$runtime_qualification_evidence" \
      "$stage/runtime/qualification/$runtime_qualification_evidence_name"
    runtime_image_digest=$(docker image inspect --format '{{.Id}}' "$runtime_image")
    activation_output="$stage/runtime/runtime-activation.generated.json"
    python3 "$source_root/infra/worker/runtime/qualification.py" project \
      --qualification "$stage/runtime/qualification/qualification.json" \
      --evidence-archive \
        "$stage/runtime/qualification/$runtime_qualification_evidence_name" \
      --runtime-build-manifest "$stage/runtime/build-manifest.env" \
      --asset-manifest "$stage/runtime/assets-manifest.json" \
      --runtime-image-digest "$runtime_image_digest" \
      --output "$activation_output"
    install -m 0444 "$activation_output" \
      "$stage/infra/worker/runtime/runtime-activation.json"
    rm -f -- "$activation_output"
    gpu_smoke_verified=true
    profile_stage_set_verified=true
    native_sample_inference_verified=true
  fi
  runtime_included=true
fi

if [[ $self_contained == true ]]; then
  [[ $runtime_included == true ]] || \
    rvc_die "self-contained Worker bundles require the verified runtime image"
  (( ${#images[@]} == 0 )) || \
    rvc_die "self-contained Worker bundles contain exactly the verified runtime image"
fi
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat > "$stage/manifest.env" <<MANIFEST
BUNDLE_FORMAT_VERSION=2
PRODUCT=rvc-training-orchestrator
COMPONENT=worker
VERSION=$version
PLATFORM=linux-amd64
CREATED_AT=$created_at
GIT_COMMIT=$git_commit
SELF_CONTAINED=$self_contained
IMAGES_MANIFEST_FORMAT_VERSION=2
IMAGES_MANIFEST_PATH=images-manifest.json
WORKER_IMAGE=rvc-orchestrator-worker:$version
SBOM_FORMAT=cyclonedx-1.6
SBOM_PATH=supply-chain/sbom.cdx.json
SBOM_STATUS=partial-release-gates-open
THIRD_PARTY_LICENSES_PATH=supply-chain/third-party-licenses.json
RVC_RUNTIME_INCLUDED=$runtime_included
RVC_NATIVE_RUNNER_AVAILABLE=$runtime_included
RVC_RUNTIME_IMAGE=${runtime_image:-none}
RVC_SOURCE_COMMIT=$runtime_source_commit
RVC_BASE_IMAGE=$runtime_base_image
RVC_FAIRSEQ_COMMIT=$runtime_fairseq_commit
RVC_SOURCE_MANIFEST_SHA256=$runtime_source_manifest_hash
RVC_WHEELHOUSE_MANIFEST_SHA256=$runtime_wheelhouse_manifest_hash
RVC_ASSET_MANIFEST_SHA256=$runtime_asset_manifest_hash
RVC_PROJECTION_MANIFEST_SHA256=$runtime_projection_manifest_hash
RVC_GPU_SMOKE_VERIFIED=$gpu_smoke_verified
RVC_PROFILE_STAGE_SET_VERIFIED=$profile_stage_set_verified
RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=$native_sample_inference_verified
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
    role=extra-$image_index
    source_reference=$image
  fi
  [[ $role =~ ^[a-z][a-z0-9-]{0,63}$ ]] || rvc_die "invalid image role: $role"
  [[ $source_reference =~ ^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}$ ]] || \
    rvc_die "invalid image reference for role $role"
  for existing_spec in ${image_specs[@]+"${image_specs[@]}"}; do
    [[ ${existing_spec%%|*} != "$role" ]] || rvc_die "duplicate image role: $role"
  done
  for existing_source_reference in \
    ${image_source_references[@]+"${image_source_references[@]}"}; do
    [[ $existing_source_reference != "$source_reference" ]] || \
      rvc_die "duplicate source image reference: $source_reference"
  done
  image_specs+=("$role|$source_reference|$source_reference|images/worker-images.tar.gz")
  image_source_references+=("$source_reference")
  image_references+=("$source_reference")
done
if [[ $runtime_included == true ]]; then
  for existing_reference in ${image_references[@]+"${image_references[@]}"}; do
    [[ $existing_reference != "$runtime_image" ]] || \
      rvc_die "runtime image must not also use --include-image"
  done
  image_specs+=("runtime|$runtime_image|$runtime_image|images/rvc-runtime-image.tar.gz")
fi

if (( ${#images[@]} > 0 )) || [[ $runtime_included == true ]]; then
  rvc_require_command docker
  install -d -m 0755 "$stage/images"
  if (( ${#images[@]} > 0 )); then
    docker save "${image_references[@]}" | gzip -n > "$stage/images/worker-images.tar.gz"
  fi
  if [[ $runtime_included == true ]]; then
    docker save "$runtime_image" | gzip -n > "$stage/images/rvc-runtime-image.tar.gz"
  fi
  {
    (( ${#images[@]} == 0 )) || printf '%s\n' "${image_references[@]}"
    [[ $runtime_included != true ]] || printf '%s\n' "$runtime_image"
  } > "$stage/images/REQUIRED_IMAGES"
fi

image_manifest_arguments=()
for image_spec in ${image_specs[@]+"${image_specs[@]}"}; do
  image_manifest_arguments+=(--image "$image_spec")
done
python3 "$source_root/installers/common/image_bundle.py" create \
  --root "$stage" --component worker --version "$version" \
  --source-commit "$git_commit" --self-contained "$self_contained" \
  ${image_manifest_arguments[@]+"${image_manifest_arguments[@]}"}

rvc_prune_host_cache_files "$stage"
[[ -f $activation_path && ! -L $activation_path ]] || \
  rvc_die "Worker runtime activation became unsafe during bundle staging"
chmod 0444 "$activation_path"

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
rvc_log "created Worker bundle: $archive"
