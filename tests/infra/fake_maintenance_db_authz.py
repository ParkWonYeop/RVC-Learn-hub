from __future__ import annotations

import os
import sys

if sys.argv != [sys.argv[0], "verify-runtime"]:
    raise SystemExit("unexpected maintenance DB preflight invocation")
if not os.environ.get("DATABASE_URL", "").startswith(
    "postgresql+asyncpg://rvc_maintenance:"
):
    raise SystemExit("maintenance DB URL was not projected before preflight")
