#!/bin/sh
set -eu

# PUBLIC_SCHEME is release/operator-owned input. Validate it before the
# official Nginx entrypoint performs envsubst so it cannot become an Nginx
# configuration injection primitive.
case "${PUBLIC_SCHEME:-}" in
  http)
    PUBLIC_HSTS_HEADER=
    ;;
  https)
    PUBLIC_HSTS_HEADER='max-age=31536000'
    ;;
  *)
    echo "PUBLIC_SCHEME must be exactly http or https" >&2
    exit 1
    ;;
esac
export PUBLIC_HSTS_HEADER

case "${ENVIRONMENT:-}" in
  production)
    if [ "$PUBLIC_SCHEME" != https ]; then
      echo "production proxy requires PUBLIC_SCHEME=https" >&2
      exit 1
    fi
    ;;
  development|test) ;;
  *)
    echo "ENVIRONMENT must be exactly development, test, or production" >&2
    exit 1
    ;;
esac

if [ "$#" -eq 0 ]; then
  set -- nginx -g 'daemon off;'
fi

exec /docker-entrypoint.sh "$@"
