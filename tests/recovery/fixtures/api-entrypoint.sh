#!/bin/sh
set -eu

if [ "${1:-}" = alembic ]; then
  case " $* " in
    *" current "*|*" heads "*)
      echo "drill00000001 (head)"
      ;;
    *" upgrade head "*|*" upgrade heads "*)
      ;;
    *)
      echo "unsupported Alembic drill command" >&2
      exit 2
      ;;
  esac
  exit 0
fi

case "${1:-}" in
  serve) listen_port=8000 ;;
  serve-mlflow) listen_port=5000 ;;
  serve-web) listen_port=3000 ;;
  serve-proxy) listen_port=80 ;;
  *) listen_port= ;;
esac
if [ -n "$listen_port" ]; then
  export RECOVERY_LISTEN_PORT="$listen_port"
  exec python -c '
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ready")

    def log_message(self, format, *args):
        return

ThreadingHTTPServer(("0.0.0.0", int(os.environ["RECOVERY_LISTEN_PORT"])), Handler).serve_forever()
'
fi

exec "$@"
