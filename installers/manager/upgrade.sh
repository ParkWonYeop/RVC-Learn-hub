#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
bundle=$SCRIPT_DIR
if [[ $# -gt 0 && -d $1 ]]; then
  bundle=$1
  shift
fi
[[ -d $bundle ]] || { echo "upgrade expects an extracted Manager bundle directory" >&2; exit 1; }
installer="$bundle/install.sh"
[[ -x $installer ]] || { echo "Manager install.sh not found in bundle" >&2; exit 1; }

for library in "$bundle/common/lib.sh" "$SCRIPT_DIR/../common/lib.sh"; do
  if [[ -r $library ]]; then source "$library"; break; fi
done
declare -F rvc_die >/dev/null || { echo "installer common library not found" >&2; exit 1; }

install_root=${RVC_INSTALL_ROOT:-/opt/rvc-orchestrator/manager}
arguments=("$@")
for ((index=0; index < ${#arguments[@]}; index++)); do
  if [[ ${arguments[$index]} == --install-root ]]; then
    (( index + 1 < ${#arguments[@]} )) || rvc_die "missing install root"
    install_root=${arguments[$((index + 1))]}
  fi
done
target_version=$(rvc_manifest_value "$bundle" VERSION || true)
[[ -n $target_version ]] || rvc_die "Manager upgrade bundle VERSION is missing"
rvc_validate_version "$target_version"
current_version=$(rvc_current_release_version "$install_root") || \
  rvc_die "Manager is not installed safely; use install.sh for a first installation"
[[ $current_version != "$target_version" ]] || \
  rvc_die "Manager upgrade target is already current: $target_version"
rvc_require_forward_release_transition "$install_root" "$target_version"

# install.sh stages and validates the release/environment before atomically
# switching `current`. It never replaces secrets or Docker volumes.
exec "$installer" "$@"
