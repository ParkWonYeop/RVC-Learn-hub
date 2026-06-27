from __future__ import annotations

import asyncio
import uuid
from typing import Any

from .config import Settings
from .database import Database
from .services.maintenance import (
    run_dataset_staging_cleanup,
    run_test_set_staging_cleanup,
)
from .storage import create_storage_adapter


class RetryableMaintenanceTaskError(RuntimeError):
    pass


def _validate_run_id(run_id: str) -> None:
    try:
        parsed = str(uuid.UUID(run_id))
    except (ValueError, AttributeError) as exc:
        raise ValueError("maintenance run ID must be a UUID") from exc
    if parsed != run_id:
        raise ValueError("maintenance run ID must use canonical UUID form")


def execute_dataset_staging_cleanup(run_id: str) -> dict[str, Any]:
    """Allowlisted Dataset RQ entry point for a server-created ledger UUID."""

    _validate_run_id(run_id)
    return asyncio.run(_execute_dataset_staging_cleanup(run_id))


async def _execute_dataset_staging_cleanup(run_id: str) -> dict[str, Any]:
    settings = Settings()
    if settings.process_role != "maintenance":
        raise RuntimeError("maintenance task requires PROCESS_ROLE=maintenance")
    database = Database(settings)
    storage = create_storage_adapter(settings)
    try:
        execution = await run_dataset_staging_cleanup(
            database,
            storage,
            settings,
            run_id=run_id,
        )
    finally:
        await storage.close()
        await database.dispose()
    if execution.retry_required:
        raise RetryableMaintenanceTaskError("Dataset staging cleanup was deferred")
    return execution.result.as_json()


def execute_test_set_staging_cleanup(run_id: str) -> dict[str, Any]:
    """Allowlisted TestSet RQ entry point for a server-created ledger UUID."""

    _validate_run_id(run_id)
    return asyncio.run(_execute_test_set_staging_cleanup(run_id))


async def _execute_test_set_staging_cleanup(run_id: str) -> dict[str, Any]:
    settings = Settings()
    if settings.process_role != "maintenance":
        raise RuntimeError("maintenance task requires PROCESS_ROLE=maintenance")
    database = Database(settings)
    storage = create_storage_adapter(settings)
    try:
        execution = await run_test_set_staging_cleanup(
            database,
            storage,
            settings,
            run_id=run_id,
        )
    finally:
        await storage.close()
        await database.dispose()
    if execution.retry_required:
        raise RetryableMaintenanceTaskError("TestSet staging cleanup was deferred")
    return execution.result.as_json()
