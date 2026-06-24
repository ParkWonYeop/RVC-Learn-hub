#!/bin/sh
set -eu

EXPECTED_COMMIT=7ef19867780cf703841ebafb565a4e47d1ea86ff
EXPECTED_ROOT=/opt/rvc-webui

if [ "$#" -ne 4 ] || [ "$1" != -C ] || [ "$2" != "$EXPECTED_ROOT" ] || \
   [ "$3" != rev-parse ] || [ "$4" != HEAD ]; then
  echo "this runtime contains only the reviewed RVC revision verifier, not a general git client" >&2
  exit 2
fi
marker="$EXPECTED_ROOT/.rvc-reviewed-commit"
if [ ! -f "$marker" ] || [ -L "$marker" ]; then
  echo "reviewed RVC commit marker is missing or unsafe" >&2
  exit 1
fi
actual=$(tr -d '\r\n' < "$marker")
if [ "$actual" != "$EXPECTED_COMMIT" ]; then
  echo "reviewed RVC commit marker does not match this runtime" >&2
  exit 1
fi
printf '%s\n' "$actual"
