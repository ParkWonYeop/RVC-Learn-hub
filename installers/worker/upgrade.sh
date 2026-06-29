#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
bundle=$SCRIPT_DIR
if [[ $# -gt 0 && -d $1 ]]; then
  bundle=$1
  shift
fi
[[ -d $bundle && -x $bundle/install.sh ]] || {
  echo "upgrade expects an extracted Worker bundle directory" >&2
  exit 1
}

for library in "$bundle/common/lib.sh" "$SCRIPT_DIR/../common/lib.sh"; do
  if [[ -r $library ]]; then source "$library"; break; fi
done
declare -F rvc_die >/dev/null || { echo "installer common library not found" >&2; exit 1; }

install_root=${RVC_INSTALL_ROOT:-/opt/rvc-orchestrator/worker}
arguments=("$@")
for ((index=0; index < ${#arguments[@]}; index++)); do
  if [[ ${arguments[$index]} == --install-root ]]; then
    (( index + 1 < ${#arguments[@]} )) || rvc_die "missing install root"
    install_root=${arguments[$((index + 1))]}
  fi
done
target_version=$(rvc_manifest_value "$bundle" VERSION || true)
[[ -n $target_version ]] || rvc_die "Worker upgrade bundle VERSION is missing"
rvc_validate_version "$target_version"
current_version=$(rvc_current_release_version "$install_root") || \
  rvc_die "Worker is not installed safely; use install.sh for a first installation"
[[ $current_version != "$target_version" ]] || \
  rvc_die "Worker upgrade target is already current: $target_version"
rvc_require_forward_release_transition "$install_root" "$target_version"

# The existing token, profile, data root, and old release stay in place. The
# installer stages and validates release-owned environment changes before activation.
exec "$bundle/install.sh" "$@"
