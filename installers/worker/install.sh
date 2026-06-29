#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for library in "$SCRIPT_DIR/../common/lib.sh" "$SCRIPT_DIR/common/lib.sh"; do
  if [[ -r $library ]]; then source "$library"; break; fi
done
declare -F rvc_die >/dev/null || { echo "installer common library not found" >&2; exit 1; }

if [[ -d $SCRIPT_DIR/infra ]]; then
  BUNDLE_ROOT=$SCRIPT_DIR
  source_tree_install=0
else
  BUNDLE_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
  source_tree_install=1
fi
INSTALL_ROOT=${RVC_INSTALL_ROOT:-/opt/rvc-orchestrator/worker}
CONFIG_ROOT=${RVC_CONFIG_ROOT:-/etc/rvc-orchestrator/worker}
DATA_ROOT=${WORKER_DATA_ROOT:-/var/lib/rvc-orchestrator/worker}
SYSTEMD_DIR=${RVC_SYSTEMD_DIR:-/etc/systemd/system}
WORKER_RUNTIME_UID=${RVC_WORKER_RUNTIME_UID:-10001}
WORKER_RUNTIME_GID=${RVC_WORKER_RUNTIME_GID:-10001}
manager_url=
worker_name=
token_file=
profile_file=
ca_bundle_file=
runner_mode=
allow_fake_dev=0
allow_unverified_gpu_runtime=0
no_start=0
allow_unsupported=0
skip_daemon=0
skip_gpu=0
version=${ORCHESTRATOR_VERSION:-}
existing_runner_mode=
existing_native_acknowledged=
CUSTOM_CA_CONTAINER_PATH=/etc/rvc-worker/ca/custom-ca.pem

validate_assignment_file() {
  local path=$1 label=$2
  [[ -f $path && ! -L $path ]] || rvc_die "$label is missing or unsafe"
  LC_ALL=C awk '
    /^[[:space:]]*$/ || /^[[:space:]]*#/ { next }
    /[[:cntrl:]]/ { exit 2 }
    $0 !~ /^[A-Za-z_][A-Za-z0-9_]*=/ { exit 3 }
    {
      key=$0
      sub(/=.*/, "", key)
      if (seen[key]++) exit 4
    }
  ' "$path" || rvc_die "$label contains an invalid or duplicate assignment"
}

assignment_value_exact() {
  local path=$1 key=$2 label=$3 count value
  count=$(awk -F= -v wanted="$key" '$1 == wanted {count++} END {print count+0}' "$path")
  [[ $count == 1 ]] || rvc_die "$label must contain exactly one $key"
  value=$(awk -F= -v wanted="$key" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' "$path")
  [[ -n $value ]] || rvc_die "$label contains an empty $key"
  printf '%s' "$value"
}

validate_config_root() {
  case "$CONFIG_ROOT" in
    /|*/|/*/../*|*/..|/*/./*|*/.|*//*|*$'\n'*|*$'\r'*)
      rvc_die "config root must be an absolute normalized path"
      ;;
    /*) ;;
    *) rvc_die "config root must be an absolute normalized path" ;;
  esac
  if [[ -e $CONFIG_ROOT || -L $CONFIG_ROOT ]]; then
    [[ -d $CONFIG_ROOT && ! -L $CONFIG_ROOT ]] || \
      rvc_die "config root is not a regular non-symlink directory"
  fi
}

validate_ca_config_root() {
  local path=$1 owner_uid=$2 mode
  [[ -d $path && ! -L $path ]] || \
    rvc_die "Worker CA configuration directory is missing or unsafe"
  [[ $(stat -c '%u' "$path" 2>/dev/null || stat -f '%u' "$path") == "$owner_uid" ]] || \
    rvc_die "Worker CA configuration directory has an unexpected owner"
  mode=$(stat -c '%a' "$path" 2>/dev/null || stat -f '%Lp' "$path")
  [[ $mode == 755 ]] || rvc_die "Worker CA configuration directory mode must be 0755"
}

update_worker_ca_environment() {
  local env_path=$1 host_dir=$2 container_path=$3 temporary projection
  validate_assignment_file "$env_path" "Worker environment"
  umask 077
  projection=$(mktemp "$CONFIG_ROOT/.worker.env.ca-values.XXXXXX")
  temporary=$(mktemp "$CONFIG_ROOT/.worker.env.ca.XXXXXX")
  printf 'WORKER_CA_BUNDLE_HOST_DIR=%s\nWORKER_CA_BUNDLE_PATH=%s\n' \
    "$host_dir" "$container_path" > "$projection"
  if ! awk '
    FNR == NR {
      separator=index($0, "=")
      key=substr($0, 1, separator-1)
      values[key]=substr($0, separator+1)
      order[++count]=key
      next
    }
    {
      separator=index($0, "=")
      key=separator ? substr($0, 1, separator-1) : ""
      if (key in values) {
        print key "=" values[key]
        seen[key]=1
      } else {
        print
      }
    }
    END {
      for (item=1; item<=count; item++) {
        key=order[item]
        if (!seen[key]) print key "=" values[key]
      }
    }
  ' "$projection" "$env_path" > "$temporary"; then
    rm -f -- "$projection" "$temporary"
    rvc_die "could not prepare the Worker CA environment"
  fi
  rm -f -- "$projection"
  chmod 0600 "$temporary"
  mv -f -- "$temporary" "$env_path"
}

update_worker_release_environment() {
  local env_path=$1 release_manifest=$2 pull_policy=$3 temporary projection key value
  local release_keys=(
    WORKER_IMAGE
    RVC_RUNTIME_INCLUDED RVC_NATIVE_RUNNER_AVAILABLE RVC_RUNTIME_IMAGE
    RVC_SOURCE_COMMIT RVC_BASE_IMAGE RVC_FAIRSEQ_COMMIT
    RVC_SOURCE_MANIFEST_SHA256 RVC_WHEELHOUSE_MANIFEST_SHA256
    RVC_ASSET_MANIFEST_SHA256 RVC_PROJECTION_MANIFEST_SHA256
    RVC_GPU_SMOKE_VERIFIED RVC_PROFILE_STAGE_SET_VERIFIED
    RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED
  )
  validate_assignment_file "$env_path" "Worker environment"
  case "$env_path" in
    "$CONFIG_ROOT/worker.env"|"$CONFIG_ROOT"/.worker.env.pending.*) ;;
    *) rvc_die "Worker environment path is outside the configured root" ;;
  esac
  umask 077
  projection=$(mktemp "$CONFIG_ROOT/.worker.env.release-values.XXXXXX")
  temporary=$(mktemp "$CONFIG_ROOT/.worker.env.release.XXXXXX")
  printf 'ORCHESTRATOR_VERSION=%s\n' \
    "$(assignment_value_exact "$release_manifest" VERSION "Worker release manifest")" \
    > "$projection"
  for key in "${release_keys[@]}"; do
    value=$(assignment_value_exact "$release_manifest" "$key" "Worker release manifest")
    printf '%s=%s\n' "$key" "$value" >> "$projection"
  done
  printf 'RVC_IMAGE_PULL_POLICY=%s\n' "$pull_policy" >> "$projection"
  if ! awk '
    FNR == NR {
      separator=index($0, "=")
      key=substr($0, 1, separator-1)
      values[key]=substr($0, separator+1)
      order[++count]=key
      next
    }
    {
      separator=index($0, "=")
      key=separator ? substr($0, 1, separator-1) : ""
      if (key in values) {
        print key "=" values[key]
        seen[key]=1
      } else {
        print
      }
    }
    END {
      for (item=1; item<=count; item++) {
        key=order[item]
        if (!seen[key]) print key "=" values[key]
      }
    }
  ' "$projection" "$env_path" > "$temporary"; then
    rm -f -- "$projection" "$temporary"
    rvc_die "could not prepare the Worker release environment"
  fi
  rm -f -- "$projection"
  chmod 0600 "$temporary"
  mv -f -- "$temporary" "$env_path"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manager-url) shift; manager_url=${1:?missing Manager URL} ;;
    --worker-name) shift; worker_name=${1:?missing Worker name} ;;
    --token-file) shift; token_file=${1:?missing token file} ;;
    --profile-file) shift; profile_file=${1:?missing RVC profile file} ;;
    --ca-bundle-file) shift; ca_bundle_file=${1:?missing custom CA bundle file} ;;
    --runner-mode) shift; runner_mode=${1:?missing runner mode} ;;
    --allow-fake-dev) allow_fake_dev=1 ;;
    --allow-unverified-gpu-runtime) allow_unverified_gpu_runtime=1 ;;
    --install-root) shift; INSTALL_ROOT=${1:?missing install root} ;;
    --config-root) shift; CONFIG_ROOT=${1:?missing config root} ;;
    --data-root) shift; DATA_ROOT=${1:?missing data root} ;;
    --systemd-dir) shift; SYSTEMD_DIR=${1:?missing systemd directory} ;;
    --version) shift; version=${1:?missing version} ;;
    --no-start) no_start=1 ;;
    --allow-unsupported-os) allow_unsupported=1 ;;
    --skip-daemon-check) skip_daemon=1 ;;
    --skip-gpu-check) skip_gpu=1 ;;
    *) rvc_die "unknown install option: $1" ;;
  esac
  shift
done

rvc_require_root_for_system_paths
validate_config_root
[[ $WORKER_RUNTIME_UID =~ ^[1-9][0-9]{0,9}$ && \
   $WORKER_RUNTIME_GID =~ ^[1-9][0-9]{0,9}$ ]] || \
  rvc_die "Worker runtime UID/GID must be positive decimal integers"
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  WORKER_RUNTIME_UID=$(id -u)
  WORKER_RUNTIME_GID=$(id -g)
fi
CONFIG_OWNER_UID=0
CONFIG_OWNER_GID=0
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  CONFIG_OWNER_UID=$(id -u)
  CONFIG_OWNER_GID=$(id -g)
fi
rvc_verify_bundle_checksums "$BUNDLE_ROOT" "$source_tree_install"
rvc_validate_supply_chain_files "$BUNDLE_ROOT"
bundle_manifest="$BUNDLE_ROOT/manifest.env"
validate_assignment_file "$bundle_manifest" "Worker bundle manifest"
[[ $(assignment_value_exact "$bundle_manifest" PRODUCT "Worker bundle manifest") == \
   rvc-training-orchestrator ]] || rvc_die "Worker bundle manifest product is invalid"
[[ $(assignment_value_exact "$bundle_manifest" COMPONENT "Worker bundle manifest") == worker ]] || \
  rvc_die "Worker bundle manifest component is invalid"
bundle_version=$(assignment_value_exact "$bundle_manifest" VERSION "Worker bundle manifest")
if [[ -n $version && $version != "$bundle_version" ]]; then
  rvc_die "requested version differs from the Worker bundle manifest"
fi
version=$bundle_version
rvc_validate_version "$version"
rvc_require_forward_release_transition "$INSTALL_ROOT" "$version"
bundle_source_commit=$(
  assignment_value_exact "$bundle_manifest" GIT_COMMIT "Worker bundle manifest"
)
rvc_prepare_image_bundle "$BUNDLE_ROOT" worker "$version" "$bundle_source_commit"
release_checksum_verifier=$(rvc_image_bundle_verifier "$BUNDLE_ROOT" || true)
[[ -n $release_checksum_verifier ]] || \
  rvc_die "dependency-free release checksum verifier is missing"
bundle_self_contained=$RVC_IMAGE_BUNDLE_SELF_CONTAINED
if [[ $bundle_self_contained == true ]]; then
  bundle_pull_policy=never
else
  bundle_pull_policy=missing
fi
bundle_worker_image=$(
  assignment_value_exact "$bundle_manifest" WORKER_IMAGE "Worker bundle manifest"
)
[[ $bundle_worker_image == "rvc-orchestrator-worker:$version" ]] || \
  rvc_die "Worker bundle manifest contains an invalid image reference"

existing_env="$CONFIG_ROOT/worker.env"
if [[ -e $existing_env || -L $existing_env ]]; then
  validate_assignment_file "$existing_env" "Worker environment"
  manager_url=${manager_url:-$(awk -F= '$1 == "MANAGER_URL" {sub(/^[^=]*=/, ""); print; exit}' "$existing_env")}
  worker_name=${worker_name:-$(awk -F= '$1 == "WORKER_NAME" {sub(/^[^=]*=/, ""); print; exit}' "$existing_env")}
  existing_runner_mode=$(awk -F= '$1 == "RVC_RUNNER_MODE" {sub(/^[^=]*=/, ""); print; exit}' "$existing_env")
  existing_native_acknowledged=$(
    awk -F= '$1 == "RVC_NATIVE_UNVERIFIED_GPU_ACKNOWLEDGED" {sub(/^[^=]*=/, ""); print; exit}' \
      "$existing_env"
  )
  runner_mode=${runner_mode:-$existing_runner_mode}
fi
runner_mode=${runner_mode:-profile}
[[ $runner_mode == fake || $runner_mode == profile || $runner_mode == native ]] || \
  rvc_die "runner mode must be fake, profile, or native"
if [[ $runner_mode == fake && $allow_fake_dev != 1 ]]; then
  rvc_die "fake mode is development-only; pass --allow-fake-dev explicitly"
fi
if [[ $runner_mode == fake ]]; then
  rvc_warn "installing a development-only fake Worker; it cannot run RVC training"
fi
bundle_runtime_included=$(
  assignment_value_exact "$bundle_manifest" RVC_RUNTIME_INCLUDED "Worker bundle manifest"
)
bundle_native_available=$(
  assignment_value_exact "$bundle_manifest" RVC_NATIVE_RUNNER_AVAILABLE \
    "Worker bundle manifest"
)
bundle_runtime_commit=$(
  assignment_value_exact "$bundle_manifest" RVC_SOURCE_COMMIT "Worker bundle manifest"
)
bundle_gpu_smoke_verified=$(
  assignment_value_exact "$bundle_manifest" RVC_GPU_SMOKE_VERIFIED "Worker bundle manifest"
)
bundle_stage_set_verified=$(
  assignment_value_exact "$bundle_manifest" RVC_PROFILE_STAGE_SET_VERIFIED \
    "Worker bundle manifest"
)
bundle_native_sample_verified=$(
  assignment_value_exact "$bundle_manifest" RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED \
    "Worker bundle manifest"
)
[[ $bundle_runtime_included == true || $bundle_runtime_included == false ]] || \
  rvc_die "Worker bundle manifest has an invalid runtime-included gate"
[[ $bundle_native_available == true || $bundle_native_available == false ]] || \
  rvc_die "Worker bundle manifest has an invalid native-runner gate"
[[ $bundle_gpu_smoke_verified == true || $bundle_gpu_smoke_verified == false ]] || \
  rvc_die "Worker bundle manifest has an invalid GPU smoke gate"
[[ $bundle_stage_set_verified == true || $bundle_stage_set_verified == false ]] || \
  rvc_die "Worker bundle manifest has an invalid stage-set gate"
[[ $bundle_native_sample_verified == true || $bundle_native_sample_verified == false ]] || \
  rvc_die "Worker bundle manifest has an invalid native Sample inference gate"
native_runtime_acknowledged=false
if [[ $runner_mode == native ]]; then
  [[ $bundle_runtime_included == true && $bundle_native_available == true ]] || \
    rvc_die "native mode requires a Worker bundle with a verified offline RVC runtime"
  [[ $bundle_runtime_commit == 7ef19867780cf703841ebafb565a4e47d1ea86ff ]] || \
    rvc_die "native runtime bundle does not contain the reviewed RVC commit"
  [[ -f $BUNDLE_ROOT/runtime/assets-manifest.json && \
     ! -L $BUNDLE_ROOT/runtime/assets-manifest.json && \
     -f $BUNDLE_ROOT/runtime/build-manifest.env && \
     ! -L $BUNDLE_ROOT/runtime/build-manifest.env ]] || \
    rvc_die "native runtime bundle lacks verified runtime manifests"
  [[ $bundle_gpu_smoke_verified == true || $bundle_gpu_smoke_verified == false ]] || \
    rvc_die "native runtime bundle has no valid GPU smoke gate"
  [[ $bundle_stage_set_verified == true || $bundle_stage_set_verified == false ]] || \
    rvc_die "native runtime bundle has no valid stage-set gate"
  if [[ $bundle_gpu_smoke_verified != true ]]; then
    [[ $allow_unverified_gpu_runtime == 1 || $existing_native_acknowledged == true ]] || \
      rvc_die "GPU smoke is unverified; pass --allow-unverified-gpu-runtime explicitly"
    native_runtime_acknowledged=true
    rvc_warn "starting a native runtime whose GPU smoke matrix is not yet verified"
  fi
  if [[ $bundle_stage_set_verified != true ]]; then
    rvc_warn "native core stages are guarded, but the full GPU/TestSet stage set is not release-verified"
  fi
  if [[ $bundle_native_sample_verified != true ]]; then
    rvc_warn "native fixed-TestSet Sample inference is not release-qualified and remains disabled"
  fi
fi
if [[ -f $existing_env && $existing_runner_mode != "$runner_mode" ]]; then
  rvc_die "--runner-mode differs from preserved worker.env; stop the service and migrate the configuration explicitly"
fi
if [[ -f $existing_env && $runner_mode == native && \
      $bundle_gpu_smoke_verified != true && $existing_native_acknowledged != true ]]; then
  rvc_die "preserved native worker.env lacks the unverified-GPU acknowledgement; migrate the configuration explicitly"
fi
[[ $manager_url =~ ^https?://[^[:space:]]+$ ]] || rvc_die "--manager-url must be an HTTP(S) URL"
[[ $worker_name =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || rvc_die "invalid --worker-name"
if [[ $runner_mode == profile && ! -s $CONFIG_ROOT/rvc-profile.yaml && -z $profile_file ]]; then
  rvc_die "profile mode requires --profile-file on first installation"
fi
if [[ -n $profile_file ]]; then
  [[ -f $profile_file && -s $profile_file ]] || rvc_die "--profile-file must be a non-empty regular file"
  if grep -q REPLACE_WITH_PINNED_RVC_COMMIT "$profile_file"; then
    rvc_die "--profile-file must pin a real RVC commit"
  fi
fi

preflight="$SCRIPT_DIR/preflight.sh"
[[ -x $preflight ]] || preflight="$BUNDLE_ROOT/preflight.sh"
preflight_args=()
[[ $allow_unsupported == 1 ]] && preflight_args+=(--allow-unsupported-os)
[[ $skip_daemon == 1 ]] && preflight_args+=(--skip-daemon-check)
[[ $skip_gpu == 1 ]] && preflight_args+=(--skip-gpu-check)
WORKER_DATA_ROOT="$DATA_ROOT" "$preflight" "${preflight_args[@]}"
selected_release="$INSTALL_ROOT/releases/$version"
activation_relative=infra/worker/runtime/runtime-activation.json
if [[ -e $selected_release || -L $selected_release ]]; then
  [[ -d $selected_release && ! -L $selected_release ]] || \
    rvc_die "existing release path is not a regular directory: $selected_release"
  [[ -f $selected_release/VERSION && ! -L $selected_release/VERSION && \
     $(<"$selected_release/VERSION") == "$version" ]] || \
    rvc_die "existing release directory is incomplete: $selected_release"
  [[ -f $selected_release/manifest.env && ! -L $selected_release/manifest.env ]] || \
    rvc_die "existing release manifest is missing or unsafe"
  cmp -s "$bundle_manifest" "$selected_release/manifest.env" || \
    rvc_die "installed Worker release manifest differs from the selected bundle"
  if [[ -f $BUNDLE_ROOT/images-manifest.json ]]; then
    [[ -f $selected_release/images-manifest.json && \
       ! -L $selected_release/images-manifest.json ]] || \
      rvc_die "existing release lacks its image manifest"
    cmp -s "$BUNDLE_ROOT/images-manifest.json" "$selected_release/images-manifest.json" || \
      rvc_die "existing release image provenance differs from the selected bundle"
  fi
  [[ -f $BUNDLE_ROOT/$activation_relative && ! -L $BUNDLE_ROOT/$activation_relative && \
     -f $selected_release/$activation_relative && \
     ! -L $selected_release/$activation_relative ]] || \
    rvc_die "Worker runtime activation projection is missing or unsafe"
  cmp -s "$BUNDLE_ROOT/$activation_relative" \
    "$selected_release/$activation_relative" || \
    rvc_die "existing Worker release activation differs from the selected bundle"
  chmod 0444 "$selected_release/$activation_relative"
  rvc_verify_release_checksums "$selected_release" "$release_checksum_verifier"
fi
rvc_load_verified_image_bundle "$BUNDLE_ROOT"

install -d -m 0755 "$INSTALL_ROOT/releases" "$INSTALL_ROOT/bin"
install -d -o "$WORKER_RUNTIME_UID" -g "$WORKER_RUNTIME_GID" -m 0700 "$DATA_ROOT"
install -d -m 0700 "$CONFIG_ROOT" "$CONFIG_ROOT/secrets"
if [[ -e $CONFIG_ROOT/ca || -L $CONFIG_ROOT/ca ]]; then
  [[ -d $CONFIG_ROOT/ca && ! -L $CONFIG_ROOT/ca ]] || \
    rvc_die "Worker CA configuration path is unsafe"
  validate_ca_config_root "$CONFIG_ROOT/ca" "$CONFIG_OWNER_UID"
else
  install -d -m 0755 "$CONFIG_ROOT/ca"
fi
validate_ca_config_root "$CONFIG_ROOT/ca" "$CONFIG_OWNER_UID"
ca_validator_source="$BUNDLE_ROOT/common/worker_ca.py"
if [[ $source_tree_install == 1 ]]; then
  ca_validator_source="$BUNDLE_ROOT/apps/worker/src/rvc_worker/tls.py"
fi
[[ -f $ca_validator_source && ! -L $ca_validator_source ]] || \
  rvc_die "Worker custom CA validator is missing or unsafe"
custom_ca_path="$CONFIG_ROOT/ca/custom-ca.pem"
pending_ca_dir=
pending_ca_path=
cleanup_pending_ca_on_early_exit() {
  if [[ -n $pending_ca_path ]]; then
    rm -f -- "$pending_ca_path"
  fi
  if [[ -n $pending_ca_dir ]]; then
    rmdir -- "$pending_ca_dir" 2>/dev/null || true
  fi
}
trap cleanup_pending_ca_on_early_exit EXIT
if [[ -e $custom_ca_path || -L $custom_ca_path ]]; then
  [[ -f $custom_ca_path && ! -L $custom_ca_path ]] || \
    rvc_die "preserved Worker custom CA path is unsafe"
  python3 "$ca_validator_source" validate \
    --path "$custom_ca_path" --required-uid "$CONFIG_OWNER_UID" || \
    rvc_die "preserved Worker custom CA failed validation"
fi
if [[ -n $ca_bundle_file ]]; then
  pending_ca_dir=$(mktemp -d "$CONFIG_ROOT/ca/.custom-ca.pending.XXXXXX")
  chmod 0700 "$pending_ca_dir"
  pending_ca_path="$pending_ca_dir/custom-ca.pem"
  python3 "$ca_validator_source" install \
    --source "$ca_bundle_file" --destination "$pending_ca_path" \
    --required-source-uid "$CONFIG_OWNER_UID" \
    --output-uid "$CONFIG_OWNER_UID" --output-gid "$CONFIG_OWNER_GID" || \
    rvc_die "--ca-bundle-file failed strict ownership, mode, size, or PEM validation"
elif [[ -f $custom_ca_path && ! -L $custom_ca_path ]]; then
  rvc_log "preserving existing Worker custom CA bundle"
fi
release_dir="$INSTALL_ROOT/releases/$version"
if [[ -e $release_dir || -L $release_dir ]]; then
  [[ -d $release_dir && ! -L $release_dir ]] || \
    rvc_die "existing release path is not a regular directory: $release_dir"
  [[ -f $release_dir/VERSION && ! -L $release_dir/VERSION && \
     $(<"$release_dir/VERSION") == "$version" ]] || \
    rvc_die "existing release directory is incomplete: $release_dir"
  rvc_verify_release_checksums "$release_dir" "$release_checksum_verifier"
  rvc_log "release $version is already installed; preserving it"
else
  temporary_release="$INSTALL_ROOT/releases/.${version}.installing.$$"
  install -d -m 0755 "$temporary_release"
  cp -R "$BUNDLE_ROOT/infra" "$temporary_release/infra"
  chmod 0444 "$temporary_release/$activation_relative"
  if [[ -f $BUNDLE_ROOT/manifest.env && ! -L $BUNDLE_ROOT/manifest.env ]]; then
    install -m 0644 "$BUNDLE_ROOT/manifest.env" "$temporary_release/manifest.env"
  fi
  if [[ -f $BUNDLE_ROOT/images-manifest.json && ! -L $BUNDLE_ROOT/images-manifest.json ]]; then
    install -m 0644 "$BUNDLE_ROOT/images-manifest.json" \
      "$temporary_release/images-manifest.json"
  fi
  if [[ -f $BUNDLE_ROOT/supply-chain/sbom.cdx.json && \
        ! -L $BUNDLE_ROOT/supply-chain ]]; then
    cp -R "$BUNDLE_ROOT/supply-chain" "$temporary_release/supply-chain"
  fi
  if [[ -d $BUNDLE_ROOT/runtime && ! -L $BUNDLE_ROOT/runtime ]]; then
    cp -R "$BUNDLE_ROOT/runtime" "$temporary_release/runtime"
  fi
  install -m 0644 "$BUNDLE_ROOT/.env.example" "$temporary_release/.env.example"
  printf '%s\n' "$version" > "$temporary_release/VERSION"
  rvc_create_release_checksums "$temporary_release" "$release_checksum_verifier"
  mv "$temporary_release" "$release_dir"
fi
release_manifest="$release_dir/manifest.env"
validate_assignment_file "$release_manifest" "installed Worker release manifest"
cmp -s "$release_manifest" "$bundle_manifest" || \
  rvc_die "installed Worker release manifest differs from the selected bundle"
if [[ -f $BUNDLE_ROOT/images-manifest.json ]]; then
  [[ -f $release_dir/images-manifest.json && ! -L $release_dir/images-manifest.json ]] || \
    rvc_die "installed Worker release lacks its image manifest"
  cmp -s "$BUNDLE_ROOT/images-manifest.json" "$release_dir/images-manifest.json" || \
    rvc_die "installed Worker release image provenance differs from the selected bundle"
fi
[[ -f $release_dir/$activation_relative && ! -L $release_dir/$activation_relative ]] || \
  rvc_die "installed Worker release activation projection is missing or unsafe"
cmp -s "$BUNDLE_ROOT/$activation_relative" "$release_dir/$activation_relative" || \
  rvc_die "installed Worker release activation differs from the selected bundle"
for release_key in VERSION WORKER_IMAGE RVC_GPU_SMOKE_VERIFIED \
  RVC_PROFILE_STAGE_SET_VERIFIED RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED; do
  release_value=$(
    assignment_value_exact "$release_manifest" "$release_key" \
      "installed Worker release manifest"
  )
  bundle_value=$(
    assignment_value_exact "$bundle_manifest" "$release_key" "Worker bundle manifest"
  )
  [[ $release_value == "$bundle_value" ]] || \
    rvc_die "installed Worker release provenance differs for $release_key"
done

worker_token="$CONFIG_ROOT/secrets/worker_token"
if [[ ! -e $worker_token ]]; then
  [[ -n $token_file ]] || rvc_die "first installation requires --token-file; tokens are never accepted on the command line"
  rvc_install_secret_from_file "$token_file" "$worker_token"
else
  chmod 0600 "$worker_token"
  rvc_log "preserving existing Worker token"
fi
[[ -f $worker_token && ! -L $worker_token ]] || \
  rvc_die "Worker token is not a safe regular file"
chown "$WORKER_RUNTIME_UID:$WORKER_RUNTIME_GID" "$worker_token"
chmod 0600 "$worker_token"

profile="$CONFIG_ROOT/rvc-profile.yaml"
if [[ ! -e $profile ]]; then
  if [[ -n $profile_file ]]; then
    install -m 0600 "$profile_file" "$profile"
  else
    install -m 0600 "$BUNDLE_ROOT/infra/worker/rvc-profile.example.yaml" "$profile"
  fi
elif [[ -n $profile_file ]]; then
  rvc_warn "preserving existing RVC profile; --profile-file was not applied"
fi
if [[ $runner_mode == profile ]] && grep -q REPLACE_WITH_PINNED_RVC_COMMIT "$profile"; then
  rvc_die "profile mode requires a reviewed $profile with a pinned commit"
fi
[[ -f $profile && ! -L $profile ]] || rvc_die "Worker profile is not a safe regular file"
chown "$WORKER_RUNTIME_UID:$WORKER_RUNTIME_GID" "$profile"
chmod 0600 "$profile"

env_file="$CONFIG_ROOT/worker.env"
pending_env=$(mktemp "$CONFIG_ROOT/.worker.env.pending.XXXXXX")
pending_env_active=1
activation_started=0
activation_committed=0
previous_env_existed=0
previous_env_backup=
previous_version=
previous_runtime_stopped=0
ca_activation_changed=0
previous_ca_existed=0
previous_ca_backup=

recover_worker_activation() {
  local status=$?
  trap - EXIT
  if [[ $status -ne 0 && $activation_started == 1 && $activation_committed == 0 ]]; then
    rvc_warn "Worker activation did not commit; restoring the previous release pointers"
    if [[ $previous_env_existed == 1 && -n $previous_env_backup && \
          -f $previous_env_backup && ! -L $previous_env_backup ]]; then
      mv -f -- "$previous_env_backup" "$env_file" || \
        rvc_warn "could not restore the previous Worker environment"
      previous_env_backup=
    else
      rm -f -- "$env_file"
    fi
    if [[ -n $previous_version ]]; then
      rvc_switch_current_release "$INSTALL_ROOT" "$previous_version" || \
        rvc_warn "could not restore the previous Worker current pointer"
    else
      rm -f -- "$INSTALL_ROOT/current"
    fi
    if [[ $ca_activation_changed == 1 ]]; then
      if [[ $previous_ca_existed == 1 && -n $previous_ca_backup && \
            -f $previous_ca_backup && ! -L $previous_ca_backup ]]; then
        mv -f -- "$previous_ca_backup" "$custom_ca_path" || \
          rvc_warn "could not restore the previous Worker custom CA"
        previous_ca_backup=
      else
        rm -f -- "$custom_ca_path"
      fi
    fi
    if [[ $previous_runtime_stopped == 1 ]]; then
      systemctl start rvc-orchestrator-worker.service >/dev/null 2>&1 || \
        rvc_warn "the previous Worker service must be restarted manually"
    fi
  fi
  if [[ $pending_env_active == 1 ]]; then
    rm -f -- "$pending_env"
  fi
  if [[ -n $previous_env_backup ]]; then
    rm -f -- "$previous_env_backup"
  fi
  cleanup_pending_ca_on_early_exit
  if [[ -n $previous_ca_backup ]]; then
    rm -f -- "$previous_ca_backup"
  fi
  exit "$status"
}
trap recover_worker_activation EXIT

if [[ ! -e $env_file ]]; then
  umask 077
  cat > "$pending_env" <<ENV
WORKER_COMPOSE_PROJECT_NAME=rvc-orchestrator-worker
ORCHESTRATOR_VERSION=$version
WORKER_IMAGE=$bundle_worker_image
RVC_IMAGE_PULL_POLICY=$bundle_pull_policy
MANAGER_URL=$manager_url
WORKER_NAME=$worker_name
WORKER_DATA_ROOT=$DATA_ROOT
WORKER_SECRETS_DIR=$CONFIG_ROOT/secrets
WORKER_CA_BUNDLE_HOST_DIR=$CONFIG_ROOT/ca
WORKER_CA_BUNDLE_PATH=
WORKER_RVC_PROFILE_HOST_PATH=$profile
RVC_RUNNER_MODE=$runner_mode
RVC_NATIVE_SOURCE_ROOT=/opt/rvc-webui
RVC_NATIVE_PYTHON_EXECUTABLE=/opt/conda/bin/python
RVC_NATIVE_CPU_WORKERS=2
RVC_NATIVE_DEVICE=cuda
RVC_NATIVE_USE_HALF=true
RVC_NATIVE_PREPROCESS_TIMEOUT_SECONDS=3600
RVC_NATIVE_EXTRACTION_TIMEOUT_SECONDS=7200
RVC_NATIVE_TRAINING_TIMEOUT_SECONDS=604800
RVC_NATIVE_INDEX_TIMEOUT_SECONDS=86400
RVC_NATIVE_SMALL_MODEL_TIMEOUT_SECONDS=3600
RVC_GPU_SMOKE_VERIFIED=${bundle_gpu_smoke_verified:-false}
RVC_PROFILE_STAGE_SET_VERIFIED=${bundle_stage_set_verified:-false}
RVC_NATIVE_UNVERIFIED_GPU_ACKNOWLEDGED=$native_runtime_acknowledged
HEARTBEAT_INTERVAL_SECONDS=15
SYSTEM_TELEMETRY_INTERVAL_SECONDS=60
POLL_INTERVAL_SECONDS=5
LEASE_RENEW_INTERVAL_SECONDS=30
TELEMETRY_SPOOL_MAX_BYTES=268435456
ARTIFACT_UPLOAD_TIMEOUT_SECONDS=3600
ARTIFACT_UPLOAD_MAX_ATTEMPTS=3
ARTIFACT_MAX_OBJECT_BYTES=5368709120
ARTIFACT_MAX_FILES_PER_ATTEMPT=256
ARTIFACT_MAX_TOTAL_BYTES_PER_ATTEMPT=107374182400
ARTIFACT_CHECKPOINT_RETENTION=20
DATASET_DOWNLOAD_TIMEOUT_SECONDS=3600
DATASET_DOWNLOAD_MAX_ATTEMPTS=3
DATASET_MAX_ARCHIVE_BYTES=5368709120
DATASET_MAX_ENTRIES=10000
DATASET_MAX_FILE_BYTES=2147483648
DATASET_MAX_TOTAL_BYTES=21474836480
DATASET_MAX_COMPRESSION_RATIO=200
ENV
else
  [[ -f $env_file && ! -L $env_file ]] || \
    rvc_die "Worker environment path is not a safe regular file"
  cp -p -- "$env_file" "$pending_env"
  rvc_log "preserving user-owned Worker environment settings"
fi
chmod 0600 "$pending_env"
update_worker_release_environment "$pending_env" "$release_manifest" "$bundle_pull_policy"
target_ca_container_path=
if [[ -n $pending_ca_path || ( -f $custom_ca_path && ! -L $custom_ca_path ) ]]; then
  target_ca_container_path=$CUSTOM_CA_CONTAINER_PATH
fi
update_worker_ca_environment \
  "$pending_env" "$CONFIG_ROOT/ca" "$target_ca_container_path"
rvc_log "staged release-owned Worker environment keys from installed provenance"

install -d -m 0755 "$INSTALL_ROOT/lib"
image_verifier_source="$SCRIPT_DIR/../common/image_bundle.py"
[[ -f $image_verifier_source ]] || \
  image_verifier_source="$BUNDLE_ROOT/common/image_bundle.py"
[[ -f $image_verifier_source && ! -L $image_verifier_source ]] || \
  rvc_die "container image bundle verifier is missing or unsafe"
install -m 0644 "$image_verifier_source" "$INSTALL_ROOT/lib/image_bundle.py"
install -m 0644 "$ca_validator_source" "$INSTALL_ROOT/lib/worker_ca.py"

compose_source="$SCRIPT_DIR/compose.sh"
[[ -f $compose_source ]] || compose_source="$BUNDLE_ROOT/compose.sh"
install -m 0755 "$compose_source" "$INSTALL_ROOT/bin/worker-compose"

install -d -m 0755 "$SYSTEMD_DIR"
unit_temp=$(mktemp "$SYSTEMD_DIR/.rvc-orchestrator-worker.service.XXXXXX")
cat > "$unit_temp" <<UNIT
[Unit]
Description=RVC Training Orchestrator GPU Worker
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=RVC_INSTALL_ROOT=$INSTALL_ROOT
Environment=RVC_CONFIG_ROOT=$CONFIG_ROOT
ExecStart=$INSTALL_ROOT/bin/worker-compose up -d --remove-orphans
ExecStop=$INSTALL_ROOT/bin/worker-compose stop
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
UNIT
chmod 0644 "$unit_temp"
if [[ ! -f $SYSTEMD_DIR/rvc-orchestrator-worker.service ]] || \
   ! cmp -s "$unit_temp" "$SYSTEMD_DIR/rvc-orchestrator-worker.service"; then
  mv "$unit_temp" "$SYSTEMD_DIR/rvc-orchestrator-worker.service"
else
  rm -f "$unit_temp"
fi

rvc_find_compose
"${RVC_COMPOSE[@]}" --env-file "$pending_env" \
  -f "$release_dir/infra/compose/worker.compose.yml" config --quiet

if [[ $no_start == 0 ]]; then
  systemctl daemon-reload
  systemctl enable rvc-orchestrator-worker.service
fi
previous_version=$(rvc_current_release_version "$INSTALL_ROOT" || true)
if [[ -n $previous_version ]]; then
  if command -v systemctl >/dev/null 2>&1 && \
     systemctl is-active --quiet rvc-orchestrator-worker.service; then
    previous_runtime_stopped=1
  fi
  RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
    "$INSTALL_ROOT/bin/worker-compose" stop
  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop rvc-orchestrator-worker.service
  fi
fi

if [[ -e $env_file || -L $env_file ]]; then
  [[ -f $env_file && ! -L $env_file ]] || \
    rvc_die "Worker environment path is not a safe regular file"
  previous_env_existed=1
  previous_env_backup=$(mktemp "$CONFIG_ROOT/.worker.env.before-activation.XXXXXX")
  cp -p -- "$env_file" "$previous_env_backup"
  chmod 0600 "$previous_env_backup"
fi
activation_started=1
if [[ -n $pending_ca_path ]]; then
  if [[ -e $custom_ca_path || -L $custom_ca_path ]]; then
    [[ -f $custom_ca_path && ! -L $custom_ca_path ]] || \
      rvc_die "existing Worker custom CA path became unsafe before activation"
    python3 "$ca_validator_source" validate \
      --path "$custom_ca_path" --required-uid "$CONFIG_OWNER_UID" || \
      rvc_die "existing Worker custom CA changed before activation"
    previous_ca_existed=1
    previous_ca_backup=$(mktemp "$CONFIG_ROOT/ca/.custom-ca.before-activation.XXXXXX")
    cp -p -- "$custom_ca_path" "$previous_ca_backup"
  fi
  mv -f -- "$pending_ca_path" "$custom_ca_path"
  pending_ca_path=
  rmdir -- "$pending_ca_dir"
  pending_ca_dir=
  ca_activation_changed=1
fi
mv -f -- "$pending_env" "$env_file"
pending_env_active=0
rvc_switch_current_release "$INSTALL_ROOT" "$version"
activation_committed=1
if [[ -n $previous_env_backup ]]; then
  rm -f -- "$previous_env_backup"
  previous_env_backup=
fi
if [[ -n $previous_ca_backup ]]; then
  rm -f -- "$previous_ca_backup"
  previous_ca_backup=
fi

RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$INSTALL_ROOT/bin/worker-compose" config --quiet

if [[ $no_start == 0 ]]; then
  systemctl start rvc-orchestrator-worker.service
  rvc_log "Worker installation is running"
else
  rvc_log "Worker installed without starting services (--no-start)"
fi
rvc_log "user configuration, token, profile, and job data were preserved; release provenance was refreshed"
