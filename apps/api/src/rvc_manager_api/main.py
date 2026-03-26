from __future__ import annotations

import uvicorn

from .app import create_app
from .config import Settings
from .logging_config import configure_logging

settings = Settings()
configure_logging(settings.log_level)
app = create_app(settings)


def run() -> None:
    uvicorn.run(
        "rvc_manager_api.main:app",
        host="0.0.0.0",
        port=8000,
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    run()
