from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, TypeVar

import anyio
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.sql.elements import ColumnElement

from rvc_orchestrator_contracts import utc_now

from ..config import Settings
from ..database import Database
from ..models import ArtifactUploadSession
from ..storage import StorageAdapter, StorageError, validate_object_key
from .artifacts import canonical_object_key, staging_object_key

LOGGER = logging.getLogger("rvc_manager_api.artifact_cleanup_reconciler")
_TERMINAL_UPLOAD_STATUSES = ("completed", "failed", "expired")
_T = TypeVar("_T")


class ArtifactCleanupOwnershipLost(Exception):
    """Another API replica owns this terminal cleanup claim."""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ArtifactCleanupCandidate:
    upload_id: str
    job_id: str
    attempt_id: str
    artifact_type: str
    status: str
    temporary_object_key: str
    canonical_object_key: str
    storage_backend: str
    storage_namespace_sha256: str
    expires_at: datetime
    finalized_at: datetime | None
    updated_at: datetime
    staging_cleanup_first_deleted_at: datetime | None
    staging_cleanup_completed_at: datetime | None
    canonical_cleanup_first_deleted_at: datetime | None
    canonical_cleanup_completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ArtifactCleanupResult:
    examined: int
    claimed: int
    object_deletes: int
    first_deletes: int
    confirmed_deletes: int
    ledger_updates: int
    conflicts: int
    failed: int


def _candidate_from_upload(upload: ArtifactUploadSession) -> ArtifactCleanupCandidate:
    return ArtifactCleanupCandidate(
        upload_id=upload.id,
        job_id=upload.job_id,
        attempt_id=upload.attempt_id,
        artifact_type=upload.artifact_type,
        status=upload.status,
        temporary_object_key=upload.temporary_object_key,
        canonical_object_key=upload.canonical_object_key,
        storage_backend=upload.storage_backend,
        storage_namespace_sha256=upload.storage_namespace_sha256,
        expires_at=upload.expires_at,
        finalized_at=upload.finalized_at,
        updated_at=upload.updated_at,
        staging_cleanup_first_deleted_at=upload.staging_cleanup_first_deleted_at,
        staging_cleanup_completed_at=upload.staging_cleanup_completed_at,
        canonical_cleanup_first_deleted_at=upload.canonical_cleanup_first_deleted_at,
        canonical_cleanup_completed_at=upload.canonical_cleanup_completed_at,
    )


def _candidate_identity_predicates(
    candidate: ArtifactCleanupCandidate,
) -> tuple[ColumnElement[bool], ...]:
    return (
        ArtifactUploadSession.job_id == candidate.job_id,
        ArtifactUploadSession.attempt_id == candidate.attempt_id,
        ArtifactUploadSession.artifact_type == candidate.artifact_type,
        ArtifactUploadSession.temporary_object_key == candidate.temporary_object_key,
        ArtifactUploadSession.canonical_object_key == candidate.canonical_object_key,
    )


def _validated_candidate_object_keys(
    candidate: ArtifactCleanupCandidate,
) -> tuple[str, str] | None:
    expected_staging = staging_object_key(candidate.attempt_id, candidate.upload_id)
    expected_canonical = canonical_object_key(
        candidate.job_id,
        candidate.attempt_id,
        candidate.artifact_type,
        candidate.upload_id,
    )
    try:
        validate_object_key(expected_staging)
        validate_object_key(expected_canonical)
    except StorageError:
        return None
    if (
        candidate.temporary_object_key != expected_staging
        or candidate.canonical_object_key != expected_canonical
    ):
        return None
    return expected_staging, expected_canonical


def _cleanup_due_predicate(
    storage: StorageAdapter,
    settings: Settings,
    *,
    now: datetime,
) -> ColumnElement[bool]:
    confirmation_before = now - timedelta(
        seconds=settings.artifact_cleanup_confirmation_grace_seconds
    )
    if storage.backend == "local":
        staging_first_due: ColumnElement[bool] = (
            ArtifactUploadSession.staging_cleanup_first_deleted_at.is_(None)
        )
    else:
        staging_first_due = and_(
            ArtifactUploadSession.staging_cleanup_first_deleted_at.is_(None),
            ArtifactUploadSession.expires_at
            <= now - timedelta(seconds=settings.artifact_staging_cleanup_grace_seconds),
        )
    staging_due = and_(
        ArtifactUploadSession.status.in_(_TERMINAL_UPLOAD_STATUSES),
        ArtifactUploadSession.staging_cleanup_completed_at.is_(None),
        or_(
            staging_first_due,
            ArtifactUploadSession.staging_cleanup_first_deleted_at <= confirmation_before,
        ),
    )
    canonical_anchor = func.coalesce(
        ArtifactUploadSession.finalized_at,
        ArtifactUploadSession.updated_at,
    )
    canonical_due = and_(
        ArtifactUploadSession.status.in_(("failed", "expired")),
        ArtifactUploadSession.canonical_cleanup_completed_at.is_(None),
        or_(
            and_(
                ArtifactUploadSession.canonical_cleanup_first_deleted_at.is_(None),
                canonical_anchor
                <= now - timedelta(seconds=settings.artifact_finalizing_stale_seconds),
            ),
            ArtifactUploadSession.canonical_cleanup_first_deleted_at <= confirmation_before,
        ),
    )
    return or_(staging_due, canonical_due)


async def _candidate_ids(
    database: Database,
    storage: StorageAdapter,
    settings: Settings,
    *,
    now: datetime,
    upload_ids: Sequence[str] | None,
) -> list[str]:
    stale_before = now - timedelta(seconds=settings.artifact_cleanup_claim_stale_seconds)
    claimable = or_(
        ArtifactUploadSession.cleanup_token.is_(None),
        ArtifactUploadSession.cleanup_heartbeat_at.is_(None),
        ArtifactUploadSession.cleanup_heartbeat_at <= stale_before,
    )
    predicates: list[ColumnElement[bool]] = [
        ArtifactUploadSession.storage_backend == storage.backend,
        ArtifactUploadSession.storage_namespace_sha256 == storage.namespace_fingerprint,
        claimable,
        _cleanup_due_predicate(storage, settings, now=now),
    ]
    if upload_ids is not None:
        if not upload_ids:
            return []
        predicates.append(ArtifactUploadSession.id.in_(tuple(upload_ids)))
    async with database.session_factory() as session:
        return list(
            (
                await session.scalars(
                    select(ArtifactUploadSession.id)
                    .where(*predicates)
                    .order_by(ArtifactUploadSession.updated_at.asc())
                    .limit(settings.artifact_cleanup_reconcile_batch_size)
                )
            ).all()
        )


async def _claim_candidate(
    database: Database,
    storage: StorageAdapter,
    settings: Settings,
    *,
    upload_id: str,
) -> tuple[str, ArtifactCleanupCandidate] | None:
    token = str(uuid.uuid4())
    claimed_at = utc_now()
    stale_before = claimed_at - timedelta(seconds=settings.artifact_cleanup_claim_stale_seconds)
    async with database.session_factory() as session:
        claimed = await session.execute(
            update(ArtifactUploadSession)
            .where(
                ArtifactUploadSession.id == upload_id,
                ArtifactUploadSession.storage_backend == storage.backend,
                ArtifactUploadSession.storage_namespace_sha256 == storage.namespace_fingerprint,
                ArtifactUploadSession.status.in_(_TERMINAL_UPLOAD_STATUSES),
                or_(
                    ArtifactUploadSession.cleanup_token.is_(None),
                    ArtifactUploadSession.cleanup_heartbeat_at.is_(None),
                    ArtifactUploadSession.cleanup_heartbeat_at <= stale_before,
                ),
                or_(
                    ArtifactUploadSession.staging_cleanup_completed_at.is_(None),
                    and_(
                        ArtifactUploadSession.status.in_(("failed", "expired")),
                        ArtifactUploadSession.canonical_cleanup_completed_at.is_(None),
                    ),
                ),
            )
            .values(
                cleanup_token=token,
                cleanup_heartbeat_at=claimed_at,
                finalized_at=func.coalesce(
                    ArtifactUploadSession.finalized_at,
                    ArtifactUploadSession.updated_at,
                ),
                updated_at=claimed_at,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            return None
        await session.commit()
        upload = await session.scalar(
            select(ArtifactUploadSession).where(
                ArtifactUploadSession.id == upload_id,
                ArtifactUploadSession.cleanup_token == token,
            )
        )
        if upload is None:
            return None
        return token, _candidate_from_upload(upload)


async def _touch_cleanup_claim(
    database: Database,
    *,
    upload_id: str,
    cleanup_token: str,
) -> bool:
    heartbeat_at = utc_now()
    async with database.session_factory() as session:
        touched = await session.execute(
            update(ArtifactUploadSession)
            .where(
                ArtifactUploadSession.id == upload_id,
                ArtifactUploadSession.status.in_(_TERMINAL_UPLOAD_STATUSES),
                ArtifactUploadSession.cleanup_token == cleanup_token,
            )
            .values(cleanup_heartbeat_at=heartbeat_at, updated_at=heartbeat_at)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    return bool(touched.rowcount == 1)  # type: ignore[attr-defined]


async def _run_with_cleanup_heartbeat(
    operation: Callable[[], Awaitable[_T]],
    *,
    database: Database,
    upload_id: str,
    cleanup_token: str,
    heartbeat_seconds: int,
) -> _T:
    if not await _touch_cleanup_claim(
        database,
        upload_id=upload_id,
        cleanup_token=cleanup_token,
    ):
        raise ArtifactCleanupOwnershipLost
    done = anyio.Event()
    results: list[_T] = []
    errors: list[BaseException] = []

    async def run_operation() -> None:
        try:
            results.append(await operation())
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    ownership_lost = False
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(run_operation)
        while not done.is_set():
            with anyio.move_on_after(heartbeat_seconds):
                await done.wait()
            if done.is_set():
                break
            if not await _touch_cleanup_claim(
                database,
                upload_id=upload_id,
                cleanup_token=cleanup_token,
            ):
                ownership_lost = True
                await done.wait()
                break
        task_group.cancel_scope.cancel()
    if not ownership_lost:
        ownership_lost = not await _touch_cleanup_claim(
            database,
            upload_id=upload_id,
            cleanup_token=cleanup_token,
        )
    if ownership_lost:
        raise ArtifactCleanupOwnershipLost
    if errors:
        raise errors[0]
    return results[0]


async def _record_cleanup_failure(
    database: Database,
    candidate: ArtifactCleanupCandidate,
    *,
    cleanup_token: str,
    cleanup_kind: Literal["canonical", "staging"],
) -> bool:
    changed_at = utc_now()
    predicates: tuple[ColumnElement[bool], ...]
    if cleanup_kind == "canonical":
        predicates = (
            ArtifactUploadSession.status.in_(("failed", "expired")),
            ArtifactUploadSession.canonical_cleanup_completed_at.is_(None),
        )
        failure_code: object = "canonical_cleanup_failed"
    else:
        predicates = (
            ArtifactUploadSession.status == candidate.status,
            ArtifactUploadSession.staging_cleanup_completed_at.is_(None),
        )
        failure_code = case(
            (
                and_(
                    ArtifactUploadSession.canonical_cleanup_completed_at.is_(None),
                    ArtifactUploadSession.failure_code == "canonical_cleanup_failed",
                ),
                ArtifactUploadSession.failure_code,
            ),
            else_="cleanup_failed",
        )
    async with database.session_factory() as session:
        failed = await session.execute(
            update(ArtifactUploadSession)
            .where(
                ArtifactUploadSession.id == candidate.upload_id,
                ArtifactUploadSession.cleanup_token == cleanup_token,
                ArtifactUploadSession.storage_backend == candidate.storage_backend,
                ArtifactUploadSession.storage_namespace_sha256
                == candidate.storage_namespace_sha256,
                *_candidate_identity_predicates(candidate),
                *predicates,
            )
            .values(
                failure_code=failure_code,
                cleanup_heartbeat_at=changed_at,
                updated_at=changed_at,
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    return bool(failed.rowcount == 1)  # type: ignore[attr-defined]


async def _record_cleanup_key_mismatch(
    database: Database,
    candidate: ArtifactCleanupCandidate,
    *,
    cleanup_token: str,
) -> bool:
    changed_at = utc_now()
    async with database.session_factory() as session:
        failed = await session.execute(
            update(ArtifactUploadSession)
            .where(
                ArtifactUploadSession.id == candidate.upload_id,
                ArtifactUploadSession.cleanup_token == cleanup_token,
                ArtifactUploadSession.storage_backend == candidate.storage_backend,
                ArtifactUploadSession.storage_namespace_sha256
                == candidate.storage_namespace_sha256,
                *_candidate_identity_predicates(candidate),
            )
            .values(
                failure_code="cleanup_key_mismatch",
                cleanup_heartbeat_at=changed_at,
                updated_at=changed_at,
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    return bool(failed.rowcount == 1)  # type: ignore[attr-defined]


async def _record_first_delete(
    database: Database,
    candidate: ArtifactCleanupCandidate,
    *,
    cleanup_token: str,
    cleanup_kind: Literal["canonical", "staging"],
) -> bool:
    deleted_at = utc_now()
    predicates: tuple[ColumnElement[bool], ...]
    values: dict[str, datetime]
    if cleanup_kind == "canonical":
        predicates = (
            ArtifactUploadSession.status.in_(("failed", "expired")),
            ArtifactUploadSession.canonical_cleanup_first_deleted_at.is_(None),
            ArtifactUploadSession.canonical_cleanup_completed_at.is_(None),
        )
        values = {"canonical_cleanup_first_deleted_at": deleted_at}
    else:
        predicates = (
            ArtifactUploadSession.status == candidate.status,
            ArtifactUploadSession.staging_cleanup_first_deleted_at.is_(None),
            ArtifactUploadSession.staging_cleanup_completed_at.is_(None),
        )
        values = {"staging_cleanup_first_deleted_at": deleted_at}
    async with database.session_factory() as session:
        recorded = await session.execute(
            update(ArtifactUploadSession)
            .where(
                ArtifactUploadSession.id == candidate.upload_id,
                ArtifactUploadSession.cleanup_token == cleanup_token,
                ArtifactUploadSession.storage_backend == candidate.storage_backend,
                ArtifactUploadSession.storage_namespace_sha256
                == candidate.storage_namespace_sha256,
                *_candidate_identity_predicates(candidate),
                *predicates,
            )
            .values(
                **values,
                cleanup_heartbeat_at=deleted_at,
                updated_at=deleted_at,
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    return bool(recorded.rowcount == 1)  # type: ignore[attr-defined]


async def _record_cleanup_complete(
    database: Database,
    candidate: ArtifactCleanupCandidate,
    *,
    cleanup_token: str,
    cleanup_kind: Literal["canonical", "staging"],
    local_single_pass: bool = False,
) -> bool:
    completed_at = utc_now()
    predicates: tuple[ColumnElement[bool], ...]
    values: dict[str, datetime]
    if cleanup_kind == "canonical":
        predicates = (
            ArtifactUploadSession.status.in_(("failed", "expired")),
            ArtifactUploadSession.canonical_cleanup_first_deleted_at.is_not(None),
            ArtifactUploadSession.canonical_cleanup_completed_at.is_(None),
        )
        values = {"canonical_cleanup_completed_at": completed_at}
    else:
        if local_single_pass:
            predicates = (
                ArtifactUploadSession.status == candidate.status,
                ArtifactUploadSession.staging_cleanup_completed_at.is_(None),
            )
        else:
            predicates = (
                ArtifactUploadSession.status == candidate.status,
                ArtifactUploadSession.staging_cleanup_first_deleted_at.is_not(None),
                ArtifactUploadSession.staging_cleanup_completed_at.is_(None),
            )
        values = {
            "staging_cleanup_completed_at": completed_at,
        }
        if local_single_pass:
            values["staging_cleanup_first_deleted_at"] = completed_at
    async with database.session_factory() as session:
        completed = await session.execute(
            update(ArtifactUploadSession)
            .where(
                ArtifactUploadSession.id == candidate.upload_id,
                ArtifactUploadSession.cleanup_token == cleanup_token,
                ArtifactUploadSession.storage_backend == candidate.storage_backend,
                ArtifactUploadSession.storage_namespace_sha256
                == candidate.storage_namespace_sha256,
                *_candidate_identity_predicates(candidate),
                *predicates,
            )
            .values(
                **values,
                cleanup_heartbeat_at=completed_at,
                updated_at=completed_at,
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    return bool(completed.rowcount == 1)  # type: ignore[attr-defined]


async def _release_cleanup_claim(
    database: Database,
    *,
    upload_id: str,
    cleanup_token: str,
) -> bool:
    async with database.session_factory() as session:
        released = await session.execute(
            update(ArtifactUploadSession)
            .where(
                ArtifactUploadSession.id == upload_id,
                ArtifactUploadSession.cleanup_token == cleanup_token,
            )
            .values(
                cleanup_token=None,
                cleanup_heartbeat_at=None,
                updated_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    return bool(released.rowcount == 1)  # type: ignore[attr-defined]


def _canonical_action(
    candidate: ArtifactCleanupCandidate,
    settings: Settings,
    *,
    now: datetime,
) -> Literal["first", "confirm"] | None:
    if (
        candidate.status not in {"failed", "expired"}
        or candidate.canonical_cleanup_completed_at is not None
    ):
        return None
    first_deleted_at = candidate.canonical_cleanup_first_deleted_at
    if first_deleted_at is not None:
        confirmation_at = _as_utc(first_deleted_at) + timedelta(
            seconds=settings.artifact_cleanup_confirmation_grace_seconds
        )
        return "confirm" if confirmation_at <= now else None
    anchor = _as_utc(candidate.finalized_at or candidate.updated_at)
    return (
        "first"
        if anchor + timedelta(seconds=settings.artifact_finalizing_stale_seconds) <= now
        else None
    )


def _staging_action(
    candidate: ArtifactCleanupCandidate,
    storage: StorageAdapter,
    settings: Settings,
    *,
    now: datetime,
) -> Literal["local", "first", "confirm"] | None:
    if (
        candidate.status not in set(_TERMINAL_UPLOAD_STATUSES)
        or candidate.staging_cleanup_completed_at is not None
    ):
        return None
    first_deleted_at = candidate.staging_cleanup_first_deleted_at
    if first_deleted_at is not None:
        confirmation_at = _as_utc(first_deleted_at) + timedelta(
            seconds=settings.artifact_cleanup_confirmation_grace_seconds
        )
        return "confirm" if confirmation_at <= now else None
    if storage.backend == "local":
        return "local"
    cleanup_at = _as_utc(candidate.expires_at) + timedelta(
        seconds=settings.artifact_staging_cleanup_grace_seconds
    )
    return "first" if cleanup_at <= now else None


async def reconcile_artifact_upload_cleanup(
    database: Database,
    storage: StorageAdapter,
    settings: Settings,
    *,
    upload_ids: Sequence[str] | None = None,
) -> ArtifactCleanupResult:
    """Run bounded, claimed first/confirmation deletes for terminal uploads."""

    now = utc_now()
    candidate_ids = await _candidate_ids(
        database,
        storage,
        settings,
        now=now,
        upload_ids=upload_ids,
    )
    claimed_count = 0
    object_deletes = 0
    first_deletes = 0
    confirmed_deletes = 0
    ledger_updates = 0
    conflicts = 0
    failed = 0
    for upload_id in candidate_ids:
        claimed = await _claim_candidate(
            database,
            storage,
            settings,
            upload_id=upload_id,
        )
        if claimed is None:
            conflicts += 1
            continue
        cleanup_token, candidate = claimed
        claimed_count += 1
        ownership_lost = False
        try:
            validated_keys = _validated_candidate_object_keys(candidate)
            if validated_keys is None:
                failed += 1
                if await _record_cleanup_key_mismatch(
                    database,
                    candidate,
                    cleanup_token=cleanup_token,
                ):
                    ledger_updates += 1
                else:
                    conflicts += 1
                continue
            expected_staging_key, expected_canonical_key = validated_keys
            actions: tuple[
                tuple[
                    Literal["canonical", "staging"],
                    str,
                    Literal["local", "first", "confirm"] | None,
                ],
                ...,
            ] = (
                (
                    "canonical",
                    expected_canonical_key,
                    _canonical_action(candidate, settings, now=now),
                ),
                (
                    "staging",
                    expected_staging_key,
                    _staging_action(candidate, storage, settings, now=now),
                ),
            )
            for cleanup_kind, object_key, action in actions:
                if action is None:
                    continue

                async def delete_current_object(key: str = object_key) -> None:
                    await storage.delete_object(key)

                try:
                    await _run_with_cleanup_heartbeat(
                        delete_current_object,
                        database=database,
                        upload_id=candidate.upload_id,
                        cleanup_token=cleanup_token,
                        heartbeat_seconds=settings.artifact_cleanup_heartbeat_seconds,
                    )
                except ArtifactCleanupOwnershipLost:
                    ownership_lost = True
                    conflicts += 1
                    break
                except StorageError:
                    failed += 1
                    if await _record_cleanup_failure(
                        database,
                        candidate,
                        cleanup_token=cleanup_token,
                        cleanup_kind=cleanup_kind,
                    ):
                        ledger_updates += 1
                    else:
                        conflicts += 1
                    continue
                object_deletes += 1
                if action == "first":
                    recorded = await _record_first_delete(
                        database,
                        candidate,
                        cleanup_token=cleanup_token,
                        cleanup_kind=cleanup_kind,
                    )
                    first_deletes += int(recorded)
                else:
                    recorded = await _record_cleanup_complete(
                        database,
                        candidate,
                        cleanup_token=cleanup_token,
                        cleanup_kind=cleanup_kind,
                        local_single_pass=action == "local",
                    )
                    confirmed_deletes += int(recorded)
                if recorded:
                    ledger_updates += 1
                else:
                    conflicts += 1
        finally:
            if not ownership_lost and not await _release_cleanup_claim(
                database,
                upload_id=candidate.upload_id,
                cleanup_token=cleanup_token,
            ):
                conflicts += 1
    return ArtifactCleanupResult(
        examined=len(candidate_ids),
        claimed=claimed_count,
        object_deletes=object_deletes,
        first_deletes=first_deletes,
        confirmed_deletes=confirmed_deletes,
        ledger_updates=ledger_updates,
        conflicts=conflicts,
        failed=failed,
    )


class ArtifactCleanupReconciler:
    def __init__(
        self,
        database: Database,
        storage: StorageAdapter,
        settings: Settings,
    ) -> None:
        self.database = database
        self.storage = storage
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
                    result = await reconcile_artifact_upload_cleanup(
                        self.database,
                        self.storage,
                        self.settings,
                    )
                except Exception:
                    self.last_error_code = "artifact_cleanup_reconcile_failed"
                    LOGGER.exception("artifact cleanup reconciliation cycle failed")
                else:
                    self.last_completed_at = utc_now()
                    self.last_error_code = (
                        "artifact_cleanup_storage_unavailable" if result.failed else None
                    )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.settings.artifact_cleanup_reconcile_interval_seconds,
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
            seconds=self.settings.artifact_cleanup_reconcile_stale_seconds
        ):
            return "stale", False
        if self.last_error_code is not None:
            return "unavailable", False
        return "ok", True
