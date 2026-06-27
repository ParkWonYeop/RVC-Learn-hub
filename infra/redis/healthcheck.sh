#!/bin/sh
set -eu

password=$(tr -d '\r\n' < /run/secrets/redis_password)
[ -n "$password" ] || exit 1
response=$(redis-cli --no-auth-warning -a "$password" ping 2>/dev/null)
[ "$response" = "PONG" ]

