#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for library in "$SCRIPT_DIR/../common/lib.sh" "$SCRIPT_DIR/common/lib.sh"; do
  if [[ -r $library ]]; then source "$library"; break; fi
done
declare -F rvc_die >/dev/null || { echo "installer common library not found" >&2; exit 1; }

INSTALL_ROOT=${RVC_INSTALL_ROOT:-/opt/rvc-orchestrator/manager}
CONFIG_ROOT=${RVC_CONFIG_ROOT:-/etc/rvc-orchestrator/manager}
email=
password_file=

while [[ $# -gt 0 ]]; do
  case "$1" in
    --email) shift; email=${1:?missing administrator email} ;;
    --password-file) shift; password_file=${1:?missing administrator password file} ;;
    --install-root) shift; INSTALL_ROOT=${1:?missing install root} ;;
    --config-root) shift; CONFIG_ROOT=${1:?missing config root} ;;
    *) rvc_die "unknown administrator bootstrap option: $1" ;;
  esac
  shift
done

[[ $email =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] || \
  rvc_die "--email must be a valid administrator email address"
[[ -n $password_file ]] || rvc_die "--password-file is required"
[[ $password_file == /* && $password_file != *:* ]] || \
  rvc_die "--password-file must be an absolute path without ':'"
[[ -f $password_file && ! -L $password_file && -s $password_file ]] || \
  rvc_die "--password-file must be a non-empty regular non-symlink file"
password_mode=$(stat -c '%a' "$password_file" 2>/dev/null || stat -f '%Lp' "$password_file")
(( (8#$password_mode & 8#077) == 0 )) || \
  rvc_die "--password-file must not be accessible by group or others"

compose="$INSTALL_ROOT/bin/manager-compose"
[[ -x $compose ]] || rvc_die "Manager Compose wrapper is not installed: $compose"
[[ -r $CONFIG_ROOT/manager.env ]] || rvc_die "Manager environment file is missing"

container_password=/run/rvc-bootstrap/admin-password
RVC_INSTALL_ROOT="$INSTALL_ROOT" RVC_CONFIG_ROOT="$CONFIG_ROOT" \
  "$compose" run --rm --user 0:0 \
  --volume "$password_file:$container_password:ro" \
  api rvc-manager-bootstrap-admin --email "$email" --password-file "$container_password"
rvc_log "administrator bootstrap finished; the password file was not copied into Manager storage"
