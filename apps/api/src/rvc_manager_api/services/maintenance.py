from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.sql.elements import ColumnElement

from rvc_orchestrator_contracts import utc_now

from ..audit import add_audit_event
from ..config import Settings
from ..database import Database
from ..maintenance_queue import (
    MaintenanceQueueEnvelopeConflict,
    MaintenanceQueuePort,
    MaintenanceQueueUnavailable,
)
from ..models import (
    Dataset,
    DatasetUploadSession,
    MaintenanceTaskRun,
    TestSet,
    TestSetItemUploadSession,
)
from ..storage import StorageAdapter, StorageError, storage_namespace_matches
from .datasets import dataset_temporary_object_key
from .test_sets import test_set_temporary_object_key


class MaintenanceRunNotExecutable(RuntimeError):
    pass


LOGGER = logging.getLogger("rvc_manager_api.maintenance_reconciler")
_RECONCILE_ADVISORY_LOCK_ID = 0x5256434D41494E54
_LOCAL_RECONCILE_LOCK = asyncio.Lock()
_DATASET_PARENT_LOCK_FUNCTION = "public.rvc_maintenance_lock_dataset_parent"
_TEST_SET_PARENT_LOCK_FUNCTION = "public.rvc_maintenance_lock_test_set_parent"
_GuardResult = TypeVar("_GuardResult")

_MAINTENANCE_RUN_LOAD_COLUMNS = (
    MaintenanceTaskRun.id,
    MaintenanceTaskRun.task_name,
    MaintenanceTaskRun.job_id,
    MaintenanceTaskRun.dry_run,
    MaintenanceTaskRun.status,
    MaintenanceTaskRun.attempt_count,
    MaintenanceTaskRun.max_attempts,
    MaintenanceTaskRun.result_json,
    MaintenanceTaskRun.started_at,
    MaintenanceTaskRun.heartbeat_at,
    MaintenanceTaskRun.created_at,
    MaintenanceTaskRun.updated_at,
)
_DATASET_UPLOAD_LOAD_COLUMNS = (
    DatasetUploadSession.id,
    DatasetUploadSession.dataset_id,
    DatasetUploadSession.generation,
    DatasetUploadSession.temporary_object_key,
    DatasetUploadSession.storage_backend,
    DatasetUploadSession.storage_namespace_sha256,
    DatasetUploadSession.status,
    DatasetUploadSession.upload_write_token,
    DatasetUploadSession.upload_heartbeat_at,
    DatasetUploadSession.finalization_token,
    DatasetUploadSession.finalization_heartbeat_at,
    DatasetUploadSession.expires_at,
    DatasetUploadSession.failure_code,
    DatasetUploadSession.cleanup_claim_run_id,
    DatasetUploadSession.cleanup_claimed_at,
    DatasetUploadSession.cleanup_claim_generation,
    DatasetUploadSession.cleanup_first_deleted_at,
    DatasetUploadSession.cleanup_completed_at,
    DatasetUploadSession.created_at,
    DatasetUploadSession.updated_at,
)
_TEST_SET_UPLOAD_LOAD_COLUMNS = (
    TestSetItemUploadSession.id,
    TestSetItemUploadSession.test_set_id,
    TestSetItemUploadSession.generation,
    TestSetItemUploadSession.temporary_object_key,
    TestSetItemUploadSession.storage_backend,
    TestSetItemUploadSession.storage_namespace_sha256,
    TestSetItemUploadSession.status,
    TestSetItemUploadSession.upload_write_token,
    TestSetItemUploadSession.upload_heartbeat_at,
    TestSetItemUploadSession.finalization_token,
    TestSetItemUploadSession.finalization_heartbeat_at,
    TestSetItemUploadSession.expires_at,
    TestSetItemUploadSession.failure_code,
    TestSetItemUploadSession.cleanup_claim_run_id,
    TestSetItemUploadSession.cleanup_claimed_at,
    TestSetItemUploadSession.cleanup_claim_generation,
    TestSetItemUploadSession.cleanup_first_deleted_at,
    TestSetItemUploadSession.cleanup_completed_at,
    TestSetItemUploadSession.created_at,
    TestSetItemUploadSession.updated_at,
)


@dataclass(frozen=True, slots=True)
class DatasetCleanupResult:
    run_id: str
    dry_run: bool
    attempt: int
    examined: int
    eligible: int
    deleted: int
    skipped: int
    failed: int
    limit_reached: bool
    time_limit_reached: bool
    session_ids: list[str]
    failure_codes: list[str]

    def as_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DatasetCleanupExecution:
    result: DatasetCleanupResult
    retry_required: bool


@dataclass(frozen=True, slots=True)
class TestSetCleanupResult:
    run_id: str
    dry_run: bool
    attempt: int
    examined: int
    eligible: int
    deleted: int
    skipped: int
    failed: int
    limit_reached: bool
    time_limit_reached: bool
    session_ids: list[str]
    failure_codes: list[str]

    def as_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TestSetCleanupExecution:
    result: TestSetCleanupResult
    retry_required: bool


@dataclass(frozen=True, slots=True)
class MaintenanceReconcileResult:
    leader_acquired: bool
    examined: int
    existing: int
    enqueued: int
    repaired: int
    enqueue_failed: int
    terminal_failed: int


class MaintenanceReconciler:
    def __init__(
        self,
        database: Database,
        queue: MaintenanceQueuePort,
        settings: Settings,
    ) -> None:
        self.database = database
        self.queue = queue
        self.settings = settings
        self._stop_event = asyncio.Event()
        self.running = False
        self.last_completed_at: datetime | None = None
        self.last_error_code: str | None = None

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        self.running = True
        try:
            while not self._stop_event.is_set():
                try:
                    result = await reconcile_maintenance_runs(
                        self.database,
                        self.queue,
                        self.settings,
                    )
                except Exception:
                    self.last_error_code = "maintenance_reconcile_failed"
                    LOGGER.exception("maintenance reconciliation cycle failed")
                else:
                    self.last_completed_at = utc_now()
                    self.last_error_code = (
                        "maintenance_queue_unavailable" if result.enqueue_failed > 0 else None
                    )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.settings.maintenance_reconcile_interval_seconds,
                    )
                except TimeoutError:
                    continue
        finally:
            self.running = False

    def readiness(self) -> tuple[str, bool]:
        if not self.running:
            return "stopped", False
        if self.last_completed_at is None:
            return "starting", False
        if utc_now() - _as_utc(self.last_completed_at) > timedelta(
            seconds=self.settings.maintenance_reconcile_stale_seconds
        ):
            return "stale", False
        if self.last_error_code is not None:
            return "unavailable", False
        return "ok", True


class _RunHeartbeat:
    """CAS heartbeat for one exact running maintenance execution attempt."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        run_id: str,
        attempt: int,
    ) -> None:
        self.database = database
        self.interval_seconds = settings.maintenance_task_heartbeat_seconds
        self.run_id = run_id
        self.attempt = attempt

    async def pulse(self) -> None:
        now = utc_now()
        async with self.database.session_factory() as session:
            result = await session.execute(
                update(MaintenanceTaskRun)
                .where(
                    MaintenanceTaskRun.id == self.run_id,
                    MaintenanceTaskRun.attempt_count == self.attempt,
                    MaintenanceTaskRun.status == "running",
                )
                .values(heartbeat_at=now, updated_at=now)
            )
            if getattr(result, "rowcount", None) != 1:
                await session.rollback()
                raise MaintenanceRunNotExecutable(
                    "maintenance run lost execution ownership"
                )
            await session.commit()

    async def sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(float(self.interval_seconds), remaining))
            await self.pulse()

    async def guard(self, operation: Awaitable[_GuardResult]) -> _GuardResult:
        """Keep an exact attempt alive while a lock or object operation blocks."""

        task = asyncio.ensure_future(operation)
        try:
            while True:
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=float(self.interval_seconds),
                    )
                except TimeoutError:
                    await self.pulse()
                    continue
                # The operation may have completed just as another actor fenced
                # this attempt.  Recheck ownership before its result is trusted.
                await self.pulse()
                return result
        except BaseException:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
            raise


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _reconcile_candidate_predicate(
    *,
    now: datetime,
    settings: Settings,
) -> ColumnElement[bool]:
    due_before = now - timedelta(seconds=settings.maintenance_reconcile_interval_seconds)
    stale_before = now - timedelta(seconds=settings.maintenance_task_timeout_seconds)
    running_heartbeat = func.coalesce(
        MaintenanceTaskRun.heartbeat_at,
        MaintenanceTaskRun.started_at,
        MaintenanceTaskRun.updated_at,
    )
    return or_(
        and_(
            MaintenanceTaskRun.status.in_(("queued", "retrying", "enqueue_failed")),
            MaintenanceTaskRun.updated_at <= due_before,
        ),
        and_(
            MaintenanceTaskRun.status == "running",
            running_heartbeat <= stale_before,
            MaintenanceTaskRun.updated_at <= due_before,
        ),
    )


def _fail_reconcile_run(
    session: AsyncSession,
    run: MaintenanceTaskRun,
    *,
    now: datetime,
    failure_code: str,
) -> None:
    run.status = "failed"
    run.last_error_code = failure_code
    run.completed_at = now
    run.updated_at = now
    add_audit_event(
        session,
        actor_type="system",
        action=f"maintenance.{run.task_name}.reconcile_failed",
        resource_type="maintenance_task_run",
        resource_id=run.id,
        details={"failure_code": failure_code},
    )


async def _reconcile_locked_run(
    session: AsyncSession,
    run: MaintenanceTaskRun,
    *,
    queue: MaintenanceQueuePort,
    now: datetime,
) -> str:
    previous_status = run.status
    attempts_exhausted = run.attempt_count >= run.max_attempts
    try:
        if run.task_name == "dataset_staging_cleanup":
            result = await queue.enqueue_dataset_cleanup(
                run_id=run.id,
                job_id=run.job_id,
                max_attempts=run.max_attempts,
                create_if_missing=not attempts_exhausted,
            )
        elif run.task_name == "test_set_staging_cleanup":
            result = await queue.enqueue_test_set_cleanup(
                run_id=run.id,
                job_id=run.job_id,
                max_attempts=run.max_attempts,
                create_if_missing=not attempts_exhausted,
            )
        else:
            _fail_reconcile_run(
                session,
                run,
                now=now,
                failure_code="maintenance_task_not_allowlisted",
            )
            return "terminal_failed"
    except MaintenanceQueueEnvelopeConflict as exc:
        _fail_reconcile_run(
            session,
            run,
            now=now,
            failure_code=exc.code,
        )
        return "terminal_failed"
    except MaintenanceQueueUnavailable:
        if previous_status != "running":
            run.status = "enqueue_failed"
        run.last_error_code = "maintenance_queue_unavailable"
        run.updated_at = now
        add_audit_event(
            session,
            actor_type="system",
            action=f"maintenance.{run.task_name}.reconcile_deferred",
            resource_type="maintenance_task_run",
            resource_id=run.id,
            details={"failure_code": "maintenance_queue_unavailable"},
        )
        return "enqueue_failed"

    if attempts_exhausted:
        if result.existing and result.job_state == "started":
            run.updated_at = now
            return "existing"
        _fail_reconcile_run(
            session,
            run,
            now=now,
            failure_code="maintenance_attempts_exhausted",
        )
        return "terminal_failed"

    if previous_status == "enqueue_failed":
        run.status = "queued"
        run.last_error_code = None
    run.queued_at = now
    run.updated_at = now
    if not result.existing or result.repaired or previous_status == "enqueue_failed":
        add_audit_event(
            session,
            actor_type="system",
            action=f"maintenance.{run.task_name}.reconciled",
            resource_type="maintenance_task_run",
            resource_id=run.id,
            details={
                "previous_status": previous_status,
                "job_state": result.job_state,
                "existing": result.existing,
                "repaired": result.repaired,
                "repair_code": result.repair_code,
            },
        )
    if result.repaired:
        return "repaired"
    if result.existing:
        return "existing"
    return "enqueued"


async def reconcile_maintenance_runs(
    database: Database,
    queue: MaintenanceQueuePort,
    settings: Settings,
) -> MaintenanceReconcileResult:
    """Reconcile existing ledger rows without creating a maintenance run."""

    if _LOCAL_RECONCILE_LOCK.locked():
        return MaintenanceReconcileResult(False, 0, 0, 0, 0, 0, 0)
    await _LOCAL_RECONCILE_LOCK.acquire()
    try:
        async with database.session_factory() as session:
            async with session.begin():
                if session.get_bind().dialect.name == "postgresql":
                    leader = await session.scalar(
                        text("SELECT pg_try_advisory_xact_lock(:maintenance_lock_id)"),
                        {"maintenance_lock_id": _RECONCILE_ADVISORY_LOCK_ID},
                    )
                    if leader is not True:
                        return MaintenanceReconcileResult(False, 0, 0, 0, 0, 0, 0)
                now = utc_now()
                runs = list(
                    (
                        await session.scalars(
                            select(MaintenanceTaskRun)
                            .options(load_only(*_MAINTENANCE_RUN_LOAD_COLUMNS))
                            .where(
                                _reconcile_candidate_predicate(
                                    now=now,
                                    settings=settings,
                                )
                            )
                            .order_by(
                                MaintenanceTaskRun.updated_at.asc(),
                                MaintenanceTaskRun.created_at.asc(),
                                MaintenanceTaskRun.id.asc(),
                            )
                            .with_for_update(skip_locked=True)
                            .limit(settings.maintenance_reconcile_batch_size)
                        )
                    ).all()
                )
                outcomes: list[str] = []
                for run in runs:
                    outcome = await _reconcile_locked_run(
                        session,
                        run,
                        queue=queue,
                        now=now,
                    )
                    outcomes.append(outcome)
                    if outcome == "enqueue_failed":
                        break
        return MaintenanceReconcileResult(
            leader_acquired=True,
            examined=len(outcomes),
            existing=outcomes.count("existing"),
            enqueued=outcomes.count("enqueued"),
            repaired=outcomes.count("repaired"),
            enqueue_failed=outcomes.count("enqueue_failed"),
            terminal_failed=outcomes.count("terminal_failed"),
        )
    finally:
        _LOCAL_RECONCILE_LOCK.release()


def _dataset_cleanup_grace(settings: Settings) -> timedelta:
    return timedelta(
        seconds=max(
            settings.maintenance_cleanup_grace_seconds,
            settings.dataset_cleanup_late_writer_grace_seconds,
        )
    )


def _dataset_writer_is_active(
    upload: DatasetUploadSession,
    *,
    now: datetime,
    settings: Settings,
) -> bool:
    if upload.upload_write_token is None or upload.upload_heartbeat_at is None:
        return False
    stale_before = now - timedelta(seconds=settings.dataset_upload_write_stale_seconds)
    return _as_utc(upload.upload_heartbeat_at) > stale_before


def _is_cleanup_eligible(
    upload: DatasetUploadSession,
    *,
    run_id: str,
    now: datetime,
    settings: Settings,
) -> bool:
    if upload.cleanup_completed_at is not None:
        return False
    if upload.status in {"completed", "finalizing"}:
        return False
    if upload.status not in {"pending", "expired", "failed"}:
        return False
    if _dataset_writer_is_active(upload, now=now, settings=settings):
        return False
    if upload.cleanup_claim_run_id == run_id:
        return upload.cleanup_claim_generation == upload.generation
    if upload.cleanup_claim_run_id is not None:
        claimed_at = upload.cleanup_claimed_at
        if claimed_at is None:
            return False
        stale_before = now - timedelta(seconds=settings.maintenance_cleanup_claim_stale_seconds)
        if _as_utc(claimed_at) > stale_before:
            return False
    grace_before = now - _dataset_cleanup_grace(settings)
    if upload.status == "pending":
        return _as_utc(upload.expires_at) <= grace_before
    return _as_utc(upload.updated_at) <= grace_before


def _candidate_predicate(
    *,
    run_id: str,
    now: datetime,
    settings: Settings,
) -> ColumnElement[bool]:
    grace_before = now - _dataset_cleanup_grace(settings)
    stale_before = now - timedelta(seconds=settings.maintenance_cleanup_claim_stale_seconds)
    writer_stale_before = now - timedelta(seconds=settings.dataset_upload_write_stale_seconds)
    no_active_writer = or_(
        DatasetUploadSession.upload_write_token.is_(None),
        DatasetUploadSession.upload_heartbeat_at.is_(None),
        DatasetUploadSession.upload_heartbeat_at <= writer_stale_before,
    )
    unclaimed = DatasetUploadSession.cleanup_claim_run_id.is_(None)
    return and_(
        DatasetUploadSession.cleanup_completed_at.is_(None),
        DatasetUploadSession.status.in_(("pending", "expired", "failed")),
        no_active_writer,
        or_(
            and_(
                DatasetUploadSession.cleanup_claim_run_id == run_id,
                DatasetUploadSession.cleanup_claim_generation == DatasetUploadSession.generation,
            ),
            and_(
                DatasetUploadSession.cleanup_claim_run_id.is_not(None),
                DatasetUploadSession.cleanup_claimed_at <= stale_before,
            ),
            and_(
                unclaimed,
                DatasetUploadSession.status == "pending",
                DatasetUploadSession.expires_at <= grace_before,
            ),
            and_(
                unclaimed,
                DatasetUploadSession.status.in_(("expired", "failed")),
                DatasetUploadSession.updated_at <= grace_before,
            ),
        ),
    )


async def _start_run(
    session: AsyncSession,
    *,
    run_id: str,
    settings: Settings,
) -> tuple[MaintenanceTaskRun, int]:
    run = await session.scalar(
        select(MaintenanceTaskRun)
        .options(load_only(*_MAINTENANCE_RUN_LOAD_COLUMNS))
        .where(MaintenanceTaskRun.id == run_id)
        .with_for_update()
    )
    if run is None or run.task_name != "dataset_staging_cleanup":
        raise MaintenanceRunNotExecutable("maintenance run does not exist")
    if run.status in {"completed", "failed"}:
        return run, 0
    now = utc_now()
    if run.status == "running":
        heartbeat = run.heartbeat_at or run.started_at
        stale_before = now - timedelta(seconds=settings.maintenance_task_timeout_seconds)
        if heartbeat is None or _as_utc(heartbeat) > stale_before:
            raise MaintenanceRunNotExecutable("maintenance run is already active")
    if run.status not in {"queued", "retrying", "running"}:
        raise MaintenanceRunNotExecutable("maintenance run is not queued")
    if run.attempt_count >= run.max_attempts:
        run.status = "failed"
        run.last_error_code = "maintenance_attempts_exhausted"
        run.completed_at = now
        await session.commit()
        return run, 0
    run.status = "running"
    run.attempt_count += 1
    run.started_at = run.started_at or now
    run.heartbeat_at = now
    run.last_error_code = None
    add_audit_event(
        session,
        actor_type="system",
        action="maintenance.dataset_staging_cleanup.started",
        resource_type="maintenance_task_run",
        resource_id=run.id,
        details={"attempt": run.attempt_count, "dry_run": run.dry_run},
    )
    await session.commit()
    return run, run.attempt_count


async def _candidate_ids(
    session: AsyncSession,
    *,
    run_id: str,
    settings: Settings,
) -> tuple[list[str], bool]:
    now = utc_now()
    ids = list(
        await session.scalars(
            select(DatasetUploadSession.id)
            .where(_candidate_predicate(run_id=run_id, now=now, settings=settings))
            .order_by(
                DatasetUploadSession.expires_at.asc(),
                DatasetUploadSession.created_at.asc(),
                DatasetUploadSession.id.asc(),
            )
            .limit(settings.maintenance_cleanup_batch_size + 1)
        )
    )
    limit_reached = len(ids) > settings.maintenance_cleanup_batch_size
    return ids[: settings.maintenance_cleanup_batch_size], limit_reached


async def _lock_dataset_then_upload_for_cleanup(
    session: AsyncSession,
    *,
    upload_id: str,
) -> tuple[DatasetUploadSession | None, str | None]:
    if session.get_bind().dialect.name == "postgresql":
        dataset_id = await session.scalar(
            text(f"SELECT {_DATASET_PARENT_LOCK_FUNCTION}(:upload_id)"),
            {"upload_id": upload_id},
        )
    else:
        dataset_id = await session.scalar(
            select(DatasetUploadSession.dataset_id).where(
                DatasetUploadSession.id == upload_id
            )
        )
        if dataset_id is not None:
            dataset_id = await session.scalar(
                select(Dataset.id).where(Dataset.id == dataset_id).with_for_update()
            )
    if dataset_id is None:
        return None, "candidate_no_longer_eligible"
    upload = await session.scalar(
        select(DatasetUploadSession)
        .options(load_only(*_DATASET_UPLOAD_LOAD_COLUMNS))
        .where(DatasetUploadSession.id == upload_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if upload is None or upload.dataset_id != dataset_id:
        return None, "candidate_no_longer_eligible"
    return upload, None


async def _claim_candidate(
    session: AsyncSession,
    *,
    upload_id: str,
    run_id: str,
    settings: Settings,
    dry_run: bool,
) -> tuple[DatasetUploadSession | None, str | None]:
    upload, lock_error = await _lock_dataset_then_upload_for_cleanup(
        session,
        upload_id=upload_id,
    )
    now = utc_now()
    if upload is None or not _is_cleanup_eligible(
        upload,
        run_id=run_id,
        now=now,
        settings=settings,
    ):
        return None, lock_error or "candidate_no_longer_eligible"
    expected_key = dataset_temporary_object_key(upload.dataset_id, upload.id)
    if upload.temporary_object_key != expected_key:
        add_audit_event(
            session,
            actor_type="system",
            action="maintenance.dataset_staging_cleanup.rejected",
            resource_type="dataset_upload_session",
            resource_id=upload.id,
            details={"run_id": run_id, "failure_code": "unsafe_staging_object_key"},
        )
        await session.commit()
        return None, "unsafe_staging_object_key"
    if dry_run:
        add_audit_event(
            session,
            actor_type="system",
            action="maintenance.dataset_staging_cleanup.previewed",
            resource_type="dataset_upload_session",
            resource_id=upload.id,
            details={"run_id": run_id, "generation": upload.generation},
        )
        await session.commit()
        return upload, None
    if upload.status == "pending":
        upload.status = "expired"
    upload.upload_write_token = None
    upload.upload_heartbeat_at = None
    upload.finalization_token = None
    upload.finalization_heartbeat_at = None
    upload.cleanup_claim_run_id = run_id
    upload.cleanup_claimed_at = now
    if upload.cleanup_claim_generation != upload.generation:
        upload.cleanup_first_deleted_at = None
    upload.cleanup_claim_generation = upload.generation
    upload.failure_code = "staging_cleanup_pending"
    add_audit_event(
        session,
        actor_type="system",
        action="maintenance.dataset_staging_cleanup.claimed",
        resource_type="dataset_upload_session",
        resource_id=upload.id,
        details={"run_id": run_id, "generation": upload.generation},
    )
    await session.commit()
    return upload, None


async def _delete_claimed_staging_object(
    session: AsyncSession,
    *,
    upload_id: str,
    run_id: str,
    storage: StorageAdapter,
    settings: Settings,
) -> str | None:
    upload, _ = await _lock_dataset_then_upload_for_cleanup(
        session,
        upload_id=upload_id,
    )
    if (
        upload is None
        or upload.cleanup_claim_run_id != run_id
        or upload.cleanup_claim_generation != upload.generation
        or upload.cleanup_completed_at is not None
        or upload.status not in {"expired", "failed"}
        or upload.upload_write_token is not None
        or upload.finalization_token is not None
        or upload.temporary_object_key != dataset_temporary_object_key(upload.dataset_id, upload.id)
    ):
        await session.rollback()
        return "cleanup_claim_lost"
    if not storage_namespace_matches(
        backend=upload.storage_backend,
        namespace_sha256=upload.storage_namespace_sha256,
        storage=storage,
    ):
        upload.failure_code = "staging_cleanup_pending"
        add_audit_event(
            session,
            actor_type="system",
            action="maintenance.dataset_staging_cleanup.deferred",
            resource_type="dataset_upload_session",
            resource_id=upload.id,
            details={"run_id": run_id, "failure_code": "storage_namespace_mismatch"},
        )
        await session.commit()
        return "storage_namespace_mismatch"
    now = utc_now()
    first_deleted_at = upload.cleanup_first_deleted_at
    if first_deleted_at is not None:
        confirmation_at = _as_utc(first_deleted_at) + timedelta(
            seconds=settings.dataset_cleanup_confirmation_grace_seconds
        )
        if confirmation_at > now:
            return "confirmation_not_due"
    try:
        # Dataset maintenance owns only the exact generation staging key;
        # canonical publications are never deleted here.
        await storage.delete_object(upload.temporary_object_key)
    except StorageError:
        upload.failure_code = "staging_cleanup_pending"
        add_audit_event(
            session,
            actor_type="system",
            action="maintenance.dataset_staging_cleanup.deferred",
            resource_type="dataset_upload_session",
            resource_id=upload.id,
            details={"run_id": run_id, "failure_code": "staging_cleanup_failed"},
        )
        await session.commit()
        return "staging_cleanup_failed"
    if first_deleted_at is None:
        upload.cleanup_first_deleted_at = now
        upload.failure_code = "staging_cleanup_confirmation_pending"
        add_audit_event(
            session,
            actor_type="system",
            action="maintenance.dataset_staging_cleanup.first_deleted",
            resource_type="dataset_upload_session",
            resource_id=upload.id,
            details={"run_id": run_id, "generation": upload.generation},
        )
        await session.commit()
        return "confirmation_pending"
    upload.cleanup_completed_at = now
    upload.failure_code = "staging_cleanup_complete"
    add_audit_event(
        session,
        actor_type="system",
        action="maintenance.dataset_staging_cleanup.completed",
        resource_type="dataset_upload_session",
        resource_id=upload.id,
        details={"run_id": run_id, "generation": upload.generation},
    )
    await session.commit()
    return None


async def _finish_run(
    session: AsyncSession,
    *,
    result: DatasetCleanupResult,
) -> bool:
    run = await session.scalar(
        select(MaintenanceTaskRun)
        .options(load_only(*_MAINTENANCE_RUN_LOAD_COLUMNS))
        .where(MaintenanceTaskRun.id == result.run_id)
        .with_for_update()
    )
    if run is None:
        raise MaintenanceRunNotExecutable("maintenance run disappeared")
    if run.status != "running" or run.attempt_count != result.attempt:
        raise MaintenanceRunNotExecutable("maintenance run lost execution ownership")
    retry_required = result.failed > 0 and run.attempt_count < run.max_attempts
    now = utc_now()
    run.result_json = result.as_json()
    run.heartbeat_at = now
    if retry_required:
        run.status = "retrying"
        run.last_error_code = "staging_cleanup_deferred"
    elif result.failed > 0:
        run.status = "failed"
        run.last_error_code = "staging_cleanup_failed"
        run.completed_at = now
    else:
        run.status = "completed"
        run.last_error_code = None
        run.completed_at = now
    add_audit_event(
        session,
        actor_type="system",
        action=(
            "maintenance.dataset_staging_cleanup.retrying"
            if retry_required
            else "maintenance.dataset_staging_cleanup.finished"
        ),
        resource_type="maintenance_task_run",
        resource_id=run.id,
        details={
            "status": run.status,
            "attempt": run.attempt_count,
            "eligible": result.eligible,
            "deleted": result.deleted,
            "failed": result.failed,
            "dry_run": result.dry_run,
        },
    )
    await session.commit()
    return retry_required


async def run_dataset_staging_cleanup(
    database: Database,
    storage: StorageAdapter,
    settings: Settings,
    *,
    run_id: str,
) -> DatasetCleanupExecution:
    async with database.session_factory() as session:
        run, attempt = await _start_run(session, run_id=run_id, settings=settings)
        if attempt == 0:
            existing = DatasetCleanupResult(
                run_id=run.id,
                dry_run=run.dry_run,
                attempt=run.attempt_count,
                examined=0,
                eligible=0,
                deleted=0,
                skipped=0,
                failed=0,
                limit_reached=False,
                time_limit_reached=False,
                session_ids=[],
                failure_codes=[],
            )
            if run.result_json:
                existing = DatasetCleanupResult(**run.result_json)
            return DatasetCleanupExecution(result=existing, retry_required=False)
        heartbeat = _RunHeartbeat(
            database,
            settings,
            run_id=run_id,
            attempt=attempt,
        )
        ids, limit_reached = await _candidate_ids(
            session,
            run_id=run_id,
            settings=settings,
        )
        await heartbeat.pulse()

    started = time.monotonic()
    examined = 0
    eligible = 0
    deleted = 0
    skipped = 0
    failed = 0
    time_limit_reached = False
    session_ids: list[str] = []
    failure_codes: list[str] = []
    confirmation_ids: list[str] = []
    for upload_id in ids:
        if time.monotonic() - started >= settings.maintenance_task_timeout_seconds:
            time_limit_reached = True
            break
        examined += 1
        await heartbeat.pulse()
        async with database.session_factory() as session:
            upload, claim_error = await heartbeat.guard(
                _claim_candidate(
                    session,
                    upload_id=upload_id,
                    run_id=run_id,
                    settings=settings,
                    dry_run=run.dry_run,
                )
            )
        if upload is None:
            skipped += 1
            if claim_error is not None:
                failure_codes.append(claim_error)
            continue
        eligible += 1
        session_ids.append(upload.id)
        if run.dry_run:
            continue
        await heartbeat.pulse()
        async with database.session_factory() as session:
            cleanup_error = await heartbeat.guard(
                _delete_claimed_staging_object(
                    session,
                    upload_id=upload.id,
                    run_id=run_id,
                    storage=storage,
                    settings=settings,
                )
            )
        if cleanup_error is None:
            deleted += 1
        elif cleanup_error in {"confirmation_pending", "confirmation_not_due"}:
            confirmation_ids.append(upload.id)
        elif cleanup_error == "cleanup_claim_lost":
            skipped += 1
            failure_codes.append(cleanup_error)
        else:
            failed += 1
            failure_codes.append(cleanup_error)

    if confirmation_ids and not run.dry_run:
        elapsed = time.monotonic() - started
        confirmation_wait = settings.dataset_cleanup_confirmation_grace_seconds
        if elapsed + confirmation_wait >= settings.maintenance_task_timeout_seconds:
            failed += len(confirmation_ids)
            failure_codes.extend("confirmation_time_limit" for _ in confirmation_ids)
            time_limit_reached = True
        else:
            await heartbeat.sleep(confirmation_wait)
            for upload_id in confirmation_ids:
                if time.monotonic() - started >= settings.maintenance_task_timeout_seconds:
                    failed += 1
                    failure_codes.append("confirmation_time_limit")
                    time_limit_reached = True
                    continue
                await heartbeat.pulse()
                async with database.session_factory() as session:
                    cleanup_error = await heartbeat.guard(
                        _delete_claimed_staging_object(
                            session,
                            upload_id=upload_id,
                            run_id=run_id,
                            storage=storage,
                            settings=settings,
                        )
                    )
                if cleanup_error is None:
                    deleted += 1
                elif cleanup_error == "cleanup_claim_lost":
                    skipped += 1
                    failure_codes.append(cleanup_error)
                else:
                    failed += 1
                    failure_codes.append(cleanup_error)

    result = DatasetCleanupResult(
        run_id=run_id,
        dry_run=run.dry_run,
        attempt=attempt,
        examined=examined,
        eligible=eligible,
        deleted=deleted,
        skipped=skipped,
        failed=failed,
        limit_reached=limit_reached,
        time_limit_reached=time_limit_reached,
        session_ids=session_ids,
        failure_codes=failure_codes,
    )
    await heartbeat.pulse()
    async with database.session_factory() as session:
        retry_required = await _finish_run(session, result=result)
    return DatasetCleanupExecution(result=result, retry_required=retry_required)


def _test_set_cleanup_grace(settings: Settings) -> timedelta:
    return timedelta(
        seconds=max(
            settings.maintenance_cleanup_grace_seconds,
            settings.test_set_cleanup_late_writer_grace_seconds,
        )
    )


def _test_set_writer_is_active(
    upload: TestSetItemUploadSession,
    *,
    now: datetime,
    settings: Settings,
) -> bool:
    if upload.upload_write_token is None:
        return False
    heartbeat = upload.upload_heartbeat_at
    if heartbeat is None:
        return False
    stale_before = now - timedelta(seconds=settings.test_set_upload_write_stale_seconds)
    return _as_utc(heartbeat) > stale_before


def _is_test_set_cleanup_eligible(
    upload: TestSetItemUploadSession,
    *,
    run_id: str,
    now: datetime,
    settings: Settings,
) -> bool:
    if upload.cleanup_completed_at is not None:
        return False
    if upload.status in {"completed", "finalizing"}:
        return False
    if upload.status not in {"pending", "expired", "failed"}:
        return False
    if _test_set_writer_is_active(upload, now=now, settings=settings):
        return False
    if upload.cleanup_claim_run_id == run_id:
        return upload.cleanup_claim_generation == upload.generation
    if upload.cleanup_claim_run_id is not None:
        claimed_at = upload.cleanup_claimed_at
        if claimed_at is None:
            return False
        stale_before = now - timedelta(seconds=settings.maintenance_cleanup_claim_stale_seconds)
        if _as_utc(claimed_at) > stale_before:
            return False
    grace_before = now - _test_set_cleanup_grace(settings)
    if upload.status == "pending":
        return _as_utc(upload.expires_at) <= grace_before
    return _as_utc(upload.updated_at) <= grace_before


def _test_set_candidate_predicate(
    *,
    run_id: str,
    now: datetime,
    settings: Settings,
) -> ColumnElement[bool]:
    grace_before = now - _test_set_cleanup_grace(settings)
    claim_stale_before = now - timedelta(seconds=settings.maintenance_cleanup_claim_stale_seconds)
    writer_stale_before = now - timedelta(seconds=settings.test_set_upload_write_stale_seconds)
    no_active_writer = or_(
        TestSetItemUploadSession.upload_write_token.is_(None),
        TestSetItemUploadSession.upload_heartbeat_at.is_(None),
        TestSetItemUploadSession.upload_heartbeat_at <= writer_stale_before,
    )
    unclaimed = TestSetItemUploadSession.cleanup_claim_run_id.is_(None)
    return and_(
        TestSetItemUploadSession.cleanup_completed_at.is_(None),
        TestSetItemUploadSession.status.in_(("pending", "expired", "failed")),
        no_active_writer,
        or_(
            and_(
                TestSetItemUploadSession.cleanup_claim_run_id == run_id,
                TestSetItemUploadSession.cleanup_claim_generation
                == TestSetItemUploadSession.generation,
            ),
            and_(
                TestSetItemUploadSession.cleanup_claim_run_id.is_not(None),
                TestSetItemUploadSession.cleanup_claimed_at <= claim_stale_before,
            ),
            and_(
                unclaimed,
                TestSetItemUploadSession.status == "pending",
                TestSetItemUploadSession.expires_at <= grace_before,
            ),
            and_(
                unclaimed,
                TestSetItemUploadSession.status.in_(("expired", "failed")),
                TestSetItemUploadSession.updated_at <= grace_before,
            ),
        ),
    )


async def _start_test_set_cleanup_run(
    session: AsyncSession,
    *,
    run_id: str,
    settings: Settings,
) -> tuple[MaintenanceTaskRun, int]:
    run = await session.scalar(
        select(MaintenanceTaskRun)
        .options(load_only(*_MAINTENANCE_RUN_LOAD_COLUMNS))
        .where(MaintenanceTaskRun.id == run_id)
        .with_for_update()
    )
    if run is None or run.task_name != "test_set_staging_cleanup":
        raise MaintenanceRunNotExecutable("maintenance run does not exist")
    if run.status in {"completed", "failed"}:
        return run, 0
    now = utc_now()
    if run.status == "running":
        heartbeat = run.heartbeat_at or run.started_at
        stale_before = now - timedelta(seconds=settings.maintenance_task_timeout_seconds)
        if heartbeat is None or _as_utc(heartbeat) > stale_before:
            raise MaintenanceRunNotExecutable("maintenance run is already active")
    if run.status not in {"queued", "retrying", "running"}:
        raise MaintenanceRunNotExecutable("maintenance run is not queued")
    if run.attempt_count >= run.max_attempts:
        run.status = "failed"
        run.last_error_code = "maintenance_attempts_exhausted"
        run.completed_at = now
        await session.commit()
        return run, 0
    run.status = "running"
    run.attempt_count += 1
    run.started_at = run.started_at or now
    run.heartbeat_at = now
    run.last_error_code = None
    add_audit_event(
        session,
        actor_type="system",
        action="maintenance.test_set_staging_cleanup.started",
        resource_type="maintenance_task_run",
        resource_id=run.id,
        details={"attempt": run.attempt_count, "dry_run": run.dry_run},
    )
    await session.commit()
    return run, run.attempt_count


async def _test_set_candidate_ids(
    session: AsyncSession,
    *,
    run_id: str,
    settings: Settings,
) -> tuple[list[str], bool]:
    now = utc_now()
    ids = list(
        await session.scalars(
            select(TestSetItemUploadSession.id)
            .where(
                _test_set_candidate_predicate(
                    run_id=run_id,
                    now=now,
                    settings=settings,
                )
            )
            .order_by(
                TestSetItemUploadSession.expires_at.asc(),
                TestSetItemUploadSession.created_at.asc(),
                TestSetItemUploadSession.id.asc(),
            )
            .limit(settings.maintenance_cleanup_batch_size + 1)
        )
    )
    limit_reached = len(ids) > settings.maintenance_cleanup_batch_size
    return ids[: settings.maintenance_cleanup_batch_size], limit_reached


async def _lock_test_set_then_upload(
    session: AsyncSession,
    *,
    upload_id: str,
) -> tuple[TestSet | None, TestSetItemUploadSession | None]:
    if session.get_bind().dialect.name == "postgresql":
        test_set_id = await session.scalar(
            text(f"SELECT {_TEST_SET_PARENT_LOCK_FUNCTION}(:upload_id)"),
            {"upload_id": upload_id},
        )
    else:
        test_set_id = await session.scalar(
            select(TestSetItemUploadSession.test_set_id).where(
                TestSetItemUploadSession.id == upload_id
            )
        )
        if test_set_id is not None:
            test_set_id = await session.scalar(
                select(TestSet.id).where(TestSet.id == test_set_id).with_for_update()
            )
    if test_set_id is None:
        return None, None
    upload = await session.scalar(
        select(TestSetItemUploadSession)
        .options(load_only(*_TEST_SET_UPLOAD_LOAD_COLUMNS))
        .where(TestSetItemUploadSession.id == upload_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if upload is None or upload.test_set_id != test_set_id:
        return None, None
    # Callers only need the parent-lock success and the exact upload row.  The
    # PostgreSQL SECURITY DEFINER function deliberately does not grant direct
    # SELECT/UPDATE access to the TestSet parent table.
    return None, upload


async def _claim_test_set_candidate(
    session: AsyncSession,
    *,
    upload_id: str,
    run_id: str,
    settings: Settings,
    dry_run: bool,
) -> tuple[TestSetItemUploadSession | None, str | None]:
    _, upload = await _lock_test_set_then_upload(session, upload_id=upload_id)
    now = utc_now()
    if upload is None or not _is_test_set_cleanup_eligible(
        upload,
        run_id=run_id,
        now=now,
        settings=settings,
    ):
        return None, "candidate_no_longer_eligible"
    expected_key = test_set_temporary_object_key(upload.test_set_id, upload.id)
    if upload.temporary_object_key != expected_key:
        add_audit_event(
            session,
            actor_type="system",
            action="maintenance.test_set_staging_cleanup.rejected",
            resource_type="test_set_item_upload_session",
            resource_id=upload.id,
            details={"run_id": run_id, "failure_code": "unsafe_staging_object_key"},
        )
        await session.commit()
        return None, "unsafe_staging_object_key"
    if dry_run:
        add_audit_event(
            session,
            actor_type="system",
            action="maintenance.test_set_staging_cleanup.previewed",
            resource_type="test_set_item_upload_session",
            resource_id=upload.id,
            details={"run_id": run_id, "generation": upload.generation},
        )
        await session.commit()
        return upload, None
    if upload.status == "pending":
        upload.status = "expired"
    upload.upload_write_token = None
    upload.upload_heartbeat_at = None
    upload.finalization_token = None
    upload.finalization_heartbeat_at = None
    upload.cleanup_claim_run_id = run_id
    upload.cleanup_claimed_at = now
    if upload.cleanup_claim_generation != upload.generation:
        upload.cleanup_first_deleted_at = None
    upload.cleanup_claim_generation = upload.generation
    upload.failure_code = "staging_cleanup_pending"
    add_audit_event(
        session,
        actor_type="system",
        action="maintenance.test_set_staging_cleanup.claimed",
        resource_type="test_set_item_upload_session",
        resource_id=upload.id,
        details={"run_id": run_id, "generation": upload.generation},
    )
    await session.commit()
    return upload, None


async def _delete_claimed_test_set_staging_object(
    session: AsyncSession,
    *,
    upload_id: str,
    run_id: str,
    storage: StorageAdapter,
    settings: Settings,
) -> str | None:
    _, upload = await _lock_test_set_then_upload(session, upload_id=upload_id)
    if (
        upload is None
        or upload.cleanup_claim_run_id != run_id
        or upload.cleanup_claim_generation != upload.generation
        or upload.cleanup_completed_at is not None
        or upload.status not in {"expired", "failed"}
        or upload.upload_write_token is not None
        or upload.finalization_token is not None
        or upload.temporary_object_key
        != test_set_temporary_object_key(upload.test_set_id, upload.id)
    ):
        await session.rollback()
        return "cleanup_claim_lost"
    if not storage_namespace_matches(
        backend=upload.storage_backend,
        namespace_sha256=upload.storage_namespace_sha256,
        storage=storage,
    ):
        upload.failure_code = "staging_cleanup_pending"
        add_audit_event(
            session,
            actor_type="system",
            action="maintenance.test_set_staging_cleanup.deferred",
            resource_type="test_set_item_upload_session",
            resource_id=upload.id,
            details={"run_id": run_id, "failure_code": "storage_namespace_mismatch"},
        )
        await session.commit()
        return "storage_namespace_mismatch"
    now = utc_now()
    first_deleted_at = upload.cleanup_first_deleted_at
    if first_deleted_at is not None:
        confirmation_at = _as_utc(first_deleted_at) + timedelta(
            seconds=settings.test_set_cleanup_confirmation_grace_seconds
        )
        if confirmation_at > now:
            return "confirmation_not_due"
    try:
        # Never delete canonical_object_key here. Maintenance owns only the
        # exact generation-scoped staging key captured by the claim.
        await storage.delete_object(upload.temporary_object_key)
    except StorageError:
        upload.failure_code = "staging_cleanup_pending"
        add_audit_event(
            session,
            actor_type="system",
            action="maintenance.test_set_staging_cleanup.deferred",
            resource_type="test_set_item_upload_session",
            resource_id=upload.id,
            details={"run_id": run_id, "failure_code": "staging_cleanup_failed"},
        )
        await session.commit()
        return "staging_cleanup_failed"
    if first_deleted_at is None:
        upload.cleanup_first_deleted_at = now
        upload.failure_code = "staging_cleanup_confirmation_pending"
        add_audit_event(
            session,
            actor_type="system",
            action="maintenance.test_set_staging_cleanup.first_deleted",
            resource_type="test_set_item_upload_session",
            resource_id=upload.id,
            details={"run_id": run_id, "generation": upload.generation},
        )
        await session.commit()
        return "confirmation_pending"
    upload.cleanup_completed_at = now
    upload.failure_code = "staging_cleanup_complete"
    add_audit_event(
        session,
        actor_type="system",
        action="maintenance.test_set_staging_cleanup.completed",
        resource_type="test_set_item_upload_session",
        resource_id=upload.id,
        details={"run_id": run_id, "generation": upload.generation},
    )
    await session.commit()
    return None


async def _finish_test_set_cleanup_run(
    session: AsyncSession,
    *,
    result: TestSetCleanupResult,
) -> bool:
    run = await session.scalar(
        select(MaintenanceTaskRun)
        .options(load_only(*_MAINTENANCE_RUN_LOAD_COLUMNS))
        .where(MaintenanceTaskRun.id == result.run_id)
        .with_for_update()
    )
    if run is None or run.task_name != "test_set_staging_cleanup":
        raise MaintenanceRunNotExecutable("maintenance run disappeared")
    if run.status != "running" or run.attempt_count != result.attempt:
        raise MaintenanceRunNotExecutable("maintenance run lost execution ownership")
    retry_required = result.failed > 0 and run.attempt_count < run.max_attempts
    now = utc_now()
    run.result_json = result.as_json()
    run.heartbeat_at = now
    if retry_required:
        run.status = "retrying"
        run.last_error_code = "staging_cleanup_deferred"
    elif result.failed > 0:
        run.status = "failed"
        run.last_error_code = "staging_cleanup_failed"
        run.completed_at = now
    else:
        run.status = "completed"
        run.last_error_code = None
        run.completed_at = now
    add_audit_event(
        session,
        actor_type="system",
        action=(
            "maintenance.test_set_staging_cleanup.retrying"
            if retry_required
            else "maintenance.test_set_staging_cleanup.finished"
        ),
        resource_type="maintenance_task_run",
        resource_id=run.id,
        details={
            "status": run.status,
            "attempt": run.attempt_count,
            "eligible": result.eligible,
            "deleted": result.deleted,
            "failed": result.failed,
            "dry_run": result.dry_run,
        },
    )
    await session.commit()
    return retry_required


async def run_test_set_staging_cleanup(
    database: Database,
    storage: StorageAdapter,
    settings: Settings,
    *,
    run_id: str,
) -> TestSetCleanupExecution:
    async with database.session_factory() as session:
        run, attempt = await _start_test_set_cleanup_run(
            session,
            run_id=run_id,
            settings=settings,
        )
        if attempt == 0:
            existing = TestSetCleanupResult(
                run_id=run.id,
                dry_run=run.dry_run,
                attempt=run.attempt_count,
                examined=0,
                eligible=0,
                deleted=0,
                skipped=0,
                failed=0,
                limit_reached=False,
                time_limit_reached=False,
                session_ids=[],
                failure_codes=[],
            )
            if run.result_json:
                existing = TestSetCleanupResult(**run.result_json)
            return TestSetCleanupExecution(result=existing, retry_required=False)
        heartbeat = _RunHeartbeat(
            database,
            settings,
            run_id=run_id,
            attempt=attempt,
        )
        ids, limit_reached = await _test_set_candidate_ids(
            session,
            run_id=run_id,
            settings=settings,
        )
        await heartbeat.pulse()

    started = time.monotonic()
    examined = 0
    eligible = 0
    deleted = 0
    skipped = 0
    failed = 0
    time_limit_reached = False
    session_ids: list[str] = []
    failure_codes: list[str] = []
    confirmation_ids: list[str] = []
    for upload_id in ids:
        if time.monotonic() - started >= settings.maintenance_task_timeout_seconds:
            time_limit_reached = True
            break
        examined += 1
        await heartbeat.pulse()
        async with database.session_factory() as session:
            upload, claim_error = await heartbeat.guard(
                _claim_test_set_candidate(
                    session,
                    upload_id=upload_id,
                    run_id=run_id,
                    settings=settings,
                    dry_run=run.dry_run,
                )
            )
        if upload is None:
            skipped += 1
            if claim_error is not None:
                failure_codes.append(claim_error)
            continue
        eligible += 1
        session_ids.append(upload.id)
        if run.dry_run:
            continue
        await heartbeat.pulse()
        async with database.session_factory() as session:
            cleanup_error = await heartbeat.guard(
                _delete_claimed_test_set_staging_object(
                    session,
                    upload_id=upload.id,
                    run_id=run_id,
                    storage=storage,
                    settings=settings,
                )
            )
        if cleanup_error is None:
            deleted += 1
        elif cleanup_error in {"confirmation_pending", "confirmation_not_due"}:
            confirmation_ids.append(upload.id)
        elif cleanup_error == "cleanup_claim_lost":
            skipped += 1
            failure_codes.append(cleanup_error)
        else:
            failed += 1
            failure_codes.append(cleanup_error)

    if confirmation_ids and not run.dry_run:
        elapsed = time.monotonic() - started
        confirmation_wait = settings.test_set_cleanup_confirmation_grace_seconds
        if elapsed + confirmation_wait >= settings.maintenance_task_timeout_seconds:
            failed += len(confirmation_ids)
            failure_codes.extend("confirmation_time_limit" for _ in confirmation_ids)
            time_limit_reached = True
        else:
            await heartbeat.sleep(confirmation_wait)
            for upload_id in confirmation_ids:
                if time.monotonic() - started >= settings.maintenance_task_timeout_seconds:
                    failed += 1
                    failure_codes.append("confirmation_time_limit")
                    time_limit_reached = True
                    continue
                await heartbeat.pulse()
                async with database.session_factory() as session:
                    cleanup_error = await heartbeat.guard(
                        _delete_claimed_test_set_staging_object(
                            session,
                            upload_id=upload_id,
                            run_id=run_id,
                            storage=storage,
                            settings=settings,
                        )
                    )
                if cleanup_error is None:
                    deleted += 1
                elif cleanup_error == "cleanup_claim_lost":
                    skipped += 1
                    failure_codes.append(cleanup_error)
                else:
                    failed += 1
                    failure_codes.append(cleanup_error)

    result = TestSetCleanupResult(
        run_id=run_id,
        dry_run=run.dry_run,
        attempt=attempt,
        examined=examined,
        eligible=eligible,
        deleted=deleted,
        skipped=skipped,
        failed=failed,
        limit_reached=limit_reached,
        time_limit_reached=time_limit_reached,
        session_ids=session_ids,
        failure_codes=failure_codes,
    )
    await heartbeat.pulse()
    async with database.session_factory() as session:
        retry_required = await _finish_test_set_cleanup_run(session, result=result)
    return TestSetCleanupExecution(result=result, retry_required=retry_required)
