#!/usr/bin/env bash

rvc_log() {
  printf '[rvc-installer] %s\n' "$*"
}

rvc_warn() {
  printf '[rvc-installer] warning: %s\n' "$*" >&2
}

rvc_die() {
  printf '[rvc-installer] error: %s\n' "$*" >&2
  exit 1
}

rvc_require_command() {
  command -v "$1" >/dev/null 2>&1 || rvc_die "required command not found: $1"
}

rvc_require_root_for_system_paths() {
  if [[ ${EUID:-$(id -u)} -ne 0 && ${RVC_INSTALL_ALLOW_NON_ROOT:-0} != 1 ]]; then
    rvc_die "run as root, or set RVC_INSTALL_ALLOW_NON_ROOT=1 with writable custom paths"
  fi
}

rvc_validate_version() {
  [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || rvc_die "invalid bundle version: $1"
}

rvc_semver_strictly_precedes() {
  local current=$1 target=$2
  python3 - "$current" "$target" <<'PY'
import re
import sys

pattern = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def parse(value: str):
    match = pattern.fullmatch(value)
    if match is None:
        raise ValueError(value)
    core = tuple(int(match.group(index)) for index in range(1, 4))
    prerelease = match.group(4)
    identifiers = None if prerelease is None else prerelease.split(".")
    if identifiers is not None:
        for identifier in identifiers:
            if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
                raise ValueError(value)
    return core, identifiers


def compare(left, right) -> int:
    left_core, left_pre = left
    right_core, right_pre = right
    if left_core != right_core:
        return -1 if left_core < right_core else 1
    if left_pre is None or right_pre is None:
        if left_pre is right_pre:
            return 0
        return 1 if left_pre is None else -1
    for left_item, right_item in zip(left_pre, right_pre):
        if left_item == right_item:
            continue
        left_numeric = left_item.isdigit()
        right_numeric = right_item.isdigit()
        if left_numeric and right_numeric:
            return -1 if int(left_item) < int(right_item) else 1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return -1 if left_item < right_item else 1
    if len(left_pre) == len(right_pre):
        return 0
    return -1 if len(left_pre) < len(right_pre) else 1


try:
    current = parse(sys.argv[1])
    target = parse(sys.argv[2])
except ValueError:
    raise SystemExit(2)
raise SystemExit(0 if compare(current, target) < 0 else 1)
PY
}

rvc_current_release_version() {
  local install_root=$1 current release version
  current="$install_root/current"
  [[ -L $current ]] || return 1
  release=$(cd -- "$current" 2>/dev/null && pwd -P) || return 1
  case "$release" in
    "$install_root"/releases/*) ;;
    *) return 1 ;;
  esac
  [[ -f $release/VERSION && ! -L $release/VERSION ]] || return 1
  version=$(tr -d '\r\n' < "$release/VERSION")
  rvc_validate_version "$version"
  [[ $release == "$install_root/releases/$version" ]] || return 1
  printf '%s' "$version"
}

rvc_require_forward_release_transition() {
  local install_root=$1 target_version=$2 current_version
  if [[ ! -e $install_root/current && ! -L $install_root/current ]]; then
    return 0
  fi
  current_version=$(rvc_current_release_version "$install_root") || \
    rvc_die "current release pointer is missing, unsafe, or inconsistent"
  [[ $current_version != "$target_version" ]] || return 0
  rvc_require_command python3
  if ! rvc_semver_strictly_precedes "$current_version" "$target_version"; then
    rvc_die "refusing non-forward release transition from $current_version to $target_version; use the guarded rollback workflow for an older release"
  fi
}

rvc_switch_current_release() {
  local install_root=$1 version=$2 temporary
  rvc_validate_version "$version"
  [[ -d $install_root/releases/$version && ! -L $install_root/releases/$version ]] || \
    rvc_die "cannot activate missing or unsafe release: $version"
  if [[ -e $install_root/current && ! -L $install_root/current ]]; then
    rvc_die "current path exists and is not a symlink: $install_root/current"
  fi
  temporary="$install_root/.current.install.$$"
  [[ ! -e $temporary && ! -L $temporary ]] || \
    rvc_die "temporary current pointer already exists"
  ln -s "releases/$version" "$temporary"
  if [[ ! -e $install_root/current && ! -L $install_root/current ]]; then
    mv "$temporary" "$install_root/current"
    return 0
  fi
  if mv -Tf "$temporary" "$install_root/current" 2>/dev/null; then
    return 0
  fi
  if mv -hf "$temporary" "$install_root/current" 2>/dev/null; then
    return 0
  fi
  rm -f -- "$temporary"
  rvc_die "atomic current release switch failed"
}

rvc_sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    rvc_die "sha256sum or shasum is required"
  fi
}

rvc_prune_host_cache_files() {
  local root=$1
  [[ -d $root && ! -L $root ]] || rvc_die "bundle staging root is missing or unsafe"
  find "$root" -type f \( -name '*.pyc' -o -name '*.pyo' -o -name '.DS_Store' \) -delete
  find "$root" -depth -type d -name __pycache__ -empty -delete
}

rvc_is_exact_source_tree_root() {
  local root=$1 physical_root git_root physical_git_root
  [[ ! -e $root/manifest.env && ! -L $root/manifest.env ]] || return 1
  [[ -f $root/pyproject.toml && ! -L $root/pyproject.toml ]] || return 1
  for directory in apps infra installers packages; do
    [[ -d $root/$directory && ! -L $root/$directory ]] || return 1
  done
  command -v git >/dev/null 2>&1 || return 1
  physical_root=$(cd -- "$root" 2>/dev/null && pwd -P) || return 1
  git_root=$(git -C "$root" rev-parse --show-toplevel 2>/dev/null) || return 1
  physical_git_root=$(cd -- "$git_root" 2>/dev/null && pwd -P) || return 1
  [[ $physical_root == "$physical_git_root" ]]
}

rvc_verify_bundle_checksums() {
  local root=$1 allow_source_tree=${2:-0} verifier
  local sums="$root/SHA256SUMS"
  [[ $allow_source_tree == 0 || $allow_source_tree == 1 ]] || \
    rvc_die "bundle checksum verifier received an invalid source-tree mode"
  if [[ ! -e $sums && ! -L $sums ]]; then
    if [[ $allow_source_tree == 1 ]] && rvc_is_exact_source_tree_root "$root"; then
      rvc_warn "exact Git source root has no SHA256SUMS; source-tree installation assumed"
      return 0
    fi
    rvc_die "bundle checksum ledger is missing; only an exact Git source root may omit SHA256SUMS"
  fi
  [[ -f $sums && ! -L $sums ]] || \
    rvc_die "bundle checksum ledger is not a regular non-symlink file"
  verifier=$(rvc_image_bundle_verifier "$root" || true)
  [[ -n $verifier ]] || rvc_die "dependency-free checksum verifier is missing"
  rvc_require_command python3
  python3 "$verifier" verify-ledger --root "$root" --ledger-name SHA256SUMS || \
    rvc_die "bundle checksum inventory validation failed"
  rvc_log "bundle checksums verified"
}

rvc_create_release_checksums() {
  local release=$1 verifier=$2
  [[ -f $verifier && ! -L $verifier ]] || \
    rvc_die "release checksum verifier is missing or unsafe"
  python3 "$verifier" create-ledger --root "$release" \
    --ledger-name RELEASE_SHA256SUMS || rvc_die "could not create release checksum ledger"
  rvc_log "release checksum ledger created"
}

rvc_verify_release_checksums() {
  local release=$1 verifier=$2
  [[ -f $verifier && ! -L $verifier ]] || \
    rvc_die "release checksum verifier is missing or unsafe"
  python3 "$verifier" verify-ledger --root "$release" \
    --ledger-name RELEASE_SHA256SUMS || rvc_die "release checksum inventory validation failed"
}

rvc_manifest_value() {
  local root=$1 key=$2
  local manifest="$root/manifest.env"
  [[ -f $manifest ]] || return 1
  awk -F= -v wanted="$key" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' "$manifest"
}

rvc_validate_supply_chain_files() {
  local root=$1 format sbom_format sbom_path sbom_status licenses_path
  if [[ ! -e $root/manifest.env && ! -L $root/manifest.env ]]; then
    return 0
  fi
  [[ -f $root/manifest.env && ! -L $root/manifest.env ]] || \
    rvc_die "bundle manifest is not a regular non-symlink file"
  LC_ALL=C awk '
    /^[[:space:]]*$/ || /^[[:space:]]*#/ { next }
    /[[:cntrl:]]/ { exit 2 }
    $0 !~ /^[A-Za-z_][A-Za-z0-9_]*=/ { exit 3 }
    {
      key=$0
      sub(/=.*/, "", key)
      if (seen[key]++) exit 4
    }
  ' "$root/manifest.env" || rvc_die "bundle manifest has an invalid or duplicate assignment"
  format=$(rvc_manifest_value "$root" BUNDLE_FORMAT_VERSION || true)
  [[ $format == 1 || $format == 2 ]] || {
    [[ $format == source-tree ]] && return 0
    rvc_die "unsupported bundle manifest format: ${format:-missing}"
  }
  sbom_format=$(rvc_manifest_value "$root" SBOM_FORMAT || true)
  sbom_path=$(rvc_manifest_value "$root" SBOM_PATH || true)
  sbom_status=$(rvc_manifest_value "$root" SBOM_STATUS || true)
  licenses_path=$(rvc_manifest_value "$root" THIRD_PARTY_LICENSES_PATH || true)
  [[ $sbom_format == cyclonedx-1.6 ]] || rvc_die "release SBOM format is missing or invalid"
  [[ $sbom_path == supply-chain/sbom.cdx.json ]] || \
    rvc_die "release SBOM path is missing or invalid"
  [[ $sbom_status == partial-release-gates-open ]] || \
    rvc_die "release SBOM status must preserve the open release gates"
  [[ $licenses_path == supply-chain/third-party-licenses.json ]] || \
    rvc_die "release license report path is missing or invalid"
  for path in "$sbom_path" "$licenses_path"; do
    [[ -f $root/$path && ! -L $root/$path ]] || \
      rvc_die "release supply-chain file is missing or unsafe: $path"
    if [[ -f $root/SHA256SUMS ]]; then
      awk -v wanted="$path" '$2 == wanted {found=1} END {exit !found}' \
        "$root/SHA256SUMS" || rvc_die "bundle checksums do not cover $path"
    fi
  done
}

rvc_generate_secret_file() {
  local path=$1
  local bytes=${2:-32}
  local directory temporary
  directory=$(dirname "$path")
  install -d -m 0700 "$directory"

  if [[ -e $path ]]; then
    [[ -f $path && ! -L $path && -s $path ]] || \
      rvc_die "existing secret is not a non-empty regular non-symlink file: $path"
    chmod 0600 "$path"
    return 0
  fi

  umask 077
  temporary=$(mktemp "$directory/.secret.XXXXXX")
  od -An -N "$bytes" -tx1 /dev/urandom | tr -d ' \n' > "$temporary"
  chmod 0600 "$temporary"
  if ln "$temporary" "$path" 2>/dev/null; then
    rm -f "$temporary"
    rvc_log "generated protected secret: $(basename "$path")"
  else
    rm -f "$temporary"
    [[ -s $path ]] || rvc_die "could not create secret: $path"
  fi
}

rvc_install_secret_from_file() {
  local source=$1 destination=$2
  local directory temporary
  [[ -f $source && ! -L $source && -s $source ]] || \
    rvc_die "token source must be a non-empty regular non-symlink file"
  directory=$(dirname "$destination")
  install -d -m 0700 "$directory"

  if [[ -e $destination ]]; then
    [[ -f $destination && ! -L $destination && -s $destination ]] || \
      rvc_die "existing token is invalid: $destination"
    chmod 0600 "$destination"
    return 0
  fi

  umask 077
  temporary=$(mktemp "$directory/.token.XXXXXX")
  tr -d '\r\n' < "$source" > "$temporary"
  [[ -s $temporary ]] || rvc_die "token source is empty after newline removal"
  chmod 0600 "$temporary"
  if ln "$temporary" "$destination" 2>/dev/null; then
    rm -f "$temporary"
  else
    rm -f "$temporary"
    [[ -s $destination ]] || rvc_die "could not install worker token"
  fi
}

rvc_find_compose() {
  if docker compose version >/dev/null 2>&1; then
    RVC_COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    RVC_COMPOSE=(docker-compose)
  else
    rvc_die "Docker Compose plugin is required"
  fi
}

rvc_check_ubuntu_platform() {
  local allow_unsupported=${1:-0}
  local machine
  machine=$(uname -m)
  if [[ $machine != x86_64 && $machine != amd64 ]]; then
    [[ $allow_unsupported == 1 ]] || rvc_die "supported architecture is x86_64; found $machine"
    rvc_warn "continuing on unsupported architecture: $machine"
  fi

  if [[ ! -r /etc/os-release ]]; then
    [[ $allow_unsupported == 1 ]] || rvc_die "/etc/os-release is unavailable"
    rvc_warn "operating system could not be identified"
    return 0
  fi

  # shellcheck disable=SC1091
  source /etc/os-release
  if [[ ${ID:-} != ubuntu || ( ${VERSION_ID:-} != 22.04 && ${VERSION_ID:-} != 24.04 ) ]]; then
    [[ $allow_unsupported == 1 ]] || rvc_die "supported OS is Ubuntu 22.04/24.04"
    rvc_warn "continuing on unsupported OS: ${PRETTY_NAME:-unknown}"
  fi
}

rvc_image_bundle_verifier() {
  local root=$1 candidate
  for candidate in \
    "$root/common/image_bundle.py" \
    "$root/installers/common/image_bundle.py"; do
    if [[ -f $candidate && ! -L $candidate ]]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

rvc_prepare_image_bundle() {
  local root=$1 component=$2 version=$3 source_commit=$4 verifier self_contained
  local legacy_archives=()
  RVC_IMAGE_BUNDLE_SELF_CONTAINED=false
  RVC_IMAGE_BUNDLE_VERIFIER=
  RVC_IMAGE_BUNDLE_COMPONENT=$component
  RVC_IMAGE_BUNDLE_VERSION=$version
  RVC_IMAGE_BUNDLE_SOURCE_COMMIT=$source_commit

  if [[ ! -e $root/images-manifest.json ]]; then
    shopt -s nullglob
    legacy_archives=("$root"/images/*.tar "$root"/images/*.tar.gz)
    shopt -u nullglob
    (( ${#legacy_archives[@]} == 0 )) || \
      rvc_die "legacy image archives require images-manifest.json; refusing ambiguous image load"
    [[ $(rvc_manifest_value "$root" SELF_CONTAINED || true) != true ]] || \
      rvc_die "self-contained bundle is missing images-manifest.json"
    rvc_warn "bundle has no images-manifest.json; legacy partial/source-tree mode assumed"
    return 0
  fi
  [[ -f $root/images-manifest.json && ! -L $root/images-manifest.json ]] || \
    rvc_die "images manifest is not a regular non-symlink file"
  verifier=$(rvc_image_bundle_verifier "$root" || true)
  [[ -n $verifier ]] || rvc_die "dependency-free image bundle verifier is missing"
  rvc_require_command python3
  python3 "$verifier" verify-bundle \
    --root "$root" --component "$component" --version "$version" \
    --source-commit "$source_commit" || rvc_die "container image bundle validation failed"
  self_contained=$(python3 "$verifier" print-self-contained \
    --root "$root" --component "$component" --version "$version" \
    --source-commit "$source_commit") || rvc_die "could not read image bundle mode"
  [[ $self_contained == true || $self_contained == false ]] || \
    rvc_die "images manifest has an invalid self-contained mode"
  [[ $(rvc_manifest_value "$root" SELF_CONTAINED || true) == "$self_contained" ]] || \
    rvc_die "bundle and images manifests disagree on self-contained mode"
  [[ $(rvc_manifest_value "$root" IMAGES_MANIFEST_FORMAT_VERSION || true) == 2 ]] || \
    rvc_die "bundle manifest does not select images manifest format 2"
  RVC_IMAGE_BUNDLE_SELF_CONTAINED=$self_contained
  RVC_IMAGE_BUNDLE_VERIFIER=$verifier
  rvc_log "container image manifest verified (self-contained=$self_contained)"
}

rvc_load_verified_image_bundle() {
  local root=$1 archives archive
  [[ ${RVC_IMAGE_BUNDLE_COMPONENT:-} ]] || rvc_die "image bundle was not prepared"
  [[ ${RVC_IMAGE_BUNDLE_VERIFIER:-} ]] || return 0
  rvc_require_command docker
  archives=$(python3 "$RVC_IMAGE_BUNDLE_VERIFIER" list-archives \
    --root "$root" --component "$RVC_IMAGE_BUNDLE_COMPONENT" \
    --version "$RVC_IMAGE_BUNDLE_VERSION" \
    --source-commit "$RVC_IMAGE_BUNDLE_SOURCE_COMMIT") || \
    rvc_die "could not enumerate verified image archives"
  if [[ -n $archives ]]; then
    while IFS= read -r archive; do
      [[ -n $archive ]] || continue
      rvc_log "loading verified container image archive: $archive"
      case "$archive" in
        *.tar.gz) gzip -dc "$root/$archive" | docker load >/dev/null ;;
        *.tar) docker load -i "$root/$archive" >/dev/null ;;
        *) rvc_die "verified image archive has an unsupported extension" ;;
      esac
    done <<< "$archives"
  fi
  python3 "$RVC_IMAGE_BUNDLE_VERIFIER" verify-loaded \
    --root "$root" --component "$RVC_IMAGE_BUNDLE_COMPONENT" \
    --version "$RVC_IMAGE_BUNDLE_VERSION" \
    --source-commit "$RVC_IMAGE_BUNDLE_SOURCE_COMMIT" || \
    rvc_die "loaded container image identity verification failed"
  rvc_log "loaded container image identities verified"
}
