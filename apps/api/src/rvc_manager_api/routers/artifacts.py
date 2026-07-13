from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal, TypeVar, cast
from urllib.parse import urlsplit

import anyio
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import set_committed_value
from starlette.types import Receive, Scope, Send

from rvc_orchestrator_contracts import (
    RVC_REVIEWED_COMMIT,
    SAMPLE_MAX_TOTAL_OUTPUT_BYTES,
    SAMPLE_MAX_TOTAL_OUTPUT_DURATION_SECONDS,
    TERMINAL_JOB_STATUSES,
    ArtifactType,
    JobConfig,
    SampleList,
    SampleRead,
    SampleRegistrationRequest,
    WorkerCapabilities,
    WorkerEngineMode,
    utc_now,
)

from ..audit import add_audit_event
from ..database import Database
from ..dependencies import CurrentUserDep, MlflowDep, SessionDep, SettingsDep, WorkerDep
from ..models import (
    Artifact,
    ArtifactUploadSession,
    Job,
    JobAttempt,
    JobLease,
    Sample,
    Worker,
)
from ..schemas import (
    ArtifactList,
    ArtifactRead,
    ArtifactUploadFinalizeRequest,
    ArtifactUploadInitRequest,
    ArtifactUploadInitResponse,
)
from ..services.artifact_cleanup import reconcile_artifact_upload_cleanup
from ..services.artifacts import (
    ArtifactSpoolError,
    ArtifactVerificationMismatch,
    artifact_to_read,
    attachment_content_disposition,
    canonical_object_key,
    derive_local_upload_token,
    effective_artifact_upload_ttl_seconds,
    remove_spool_file,
    safe_download_filename,
    staging_object_key,
    upload_dedupe_key,
    upload_request_fingerprint,
    upload_token_hash,
    verify_object_to_spool,
    verify_upload_token,
)
from ..services.authorization import require_job_owner_or_admin
from ..services.job_configs import InvalidJobConfigLedger, validated_job_config
from ..services.mlflow import artifact_event_key
from ..services.samples import (
    InvalidSampleWav,
    SamplePcmInspection,
    SampleRegistrationFenceConflict,
    SampleRegistrationFenceUnavailable,
    SampleStorageUnavailable,
    acquire_sample_registration_job_fence,
    artifact_provenance_matches,
    inspect_sample_pcm_wav,
    sample_matches_registration,
    sample_metrics_evidence,
    sample_metrics_match,
    sample_to_read,
    verified_artifact_binding,
    verified_artifact_by_hash,
)
from ..services.workers import as_utc, require_active_lease, verified_test_set_transfer
from ..storage import (
    LocalStorageAdapter,
    ObjectNotFound,
    ObjectSizeMismatch,
    ObjectTooLarge,
    StorageAdapter,
    StorageError,
    storage_namespace_matches,
)

router = APIRouter(tags=["artifacts"])

_T = TypeVar("_T")


class ArtifactFinalizationOwnershipLost(Exception):
    """The finalizer token changed while an external operation was running."""

    def __init__(self, result: object | None = None) -> None:
        super().__init__("artifact finalization ownership was lost")
        self.result = result


class ArtifactUploadWriteOwnershipLost(Exception):
    """The local upload writer token changed while its body was streaming."""


class _VerifiedSampleFileResponse(FileResponse):
    """Keep the verification slot and spool until transfer teardown completes."""

    def __init__(
        self,
        *,
        path: Path,
        media_type: str,
        headers: dict[str, str],
        verification_semaphore: asyncio.Semaphore,
    ) -> None:
        super().__init__(path=path, media_type=media_type, headers=headers)
        self._spool_path = path
        self._verification_semaphore = verification_semaphore

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await remove_spool_file(self._spool_path)
            finally:
                self._verification_semaphore.release()


def get_storage(request: Request) -> StorageAdapter:
    return cast(StorageAdapter, request.app.state.storage)


StorageDep = Annotated[StorageAdapter, Depends(get_storage)]


async def _join_sample_inspection_after_cancellation(
    task: asyncio.Task[SamplePcmInspection],
) -> SamplePcmInspection:
    """Wait for PCM inspection before its spool may be removed by route cleanup."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    return task.result()


async def _inspect_sample_pcm_wav_joined(
    path: Path,
    settings: SettingsDep,
    *,
    deadline_monotonic: float,
) -> SamplePcmInspection:
    """Run PCM inspection without abandoning a thread on request cancellation."""

    inspection = asyncio.create_task(
        asyncio.to_thread(
            inspect_sample_pcm_wav,
            path,
            settings,
            deadline_monotonic=deadline_monotonic,
        )
    )
    try:
        return await asyncio.shield(inspection)
    except asyncio.CancelledError as cancelled:
        try:
            await _join_sample_inspection_after_cancellation(inspection)
        except BaseException:
            raise cancelled from None
        raise cancelled


def _upload_storage_matches(
    upload: ArtifactUploadSession,
    storage: StorageAdapter,
) -> bool:
    return storage_namespace_matches(
        backend=upload.storage_backend,
        namespace_sha256=upload.storage_namespace_sha256,
        storage=storage,
    )


def _retry_metadata(
    upload: ArtifactUploadSession,
    settings: SettingsDep,
) -> tuple[bool, int | None]:
    if upload.status == "finalizing":
        return True, settings.artifact_retry_after_seconds
    if upload.status == "pending":
        retry_after = settings.artifact_retry_after_seconds if upload.failure_code else None
        return True, retry_after
    if upload.status == "expired":
        return True, settings.artifact_retry_after_seconds
    return False, None


def _public_api_base_url(request: Request, settings: SettingsDep) -> str:
    return settings.public_api_base_url or str(request.base_url).rstrip("/")


def _url_origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        return None
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.lower(), effective_port


def _upload_belongs_to_claim(
    upload: ArtifactUploadSession,
    *,
    job_id: str,
    attempt_id: str,
    lease_id: str,
    worker_id: str,
) -> bool:
    return (
        upload.job_id == job_id
        and upload.attempt_id == attempt_id
        and upload.lease_id == lease_id
        and upload.worker_id == worker_id
    )


async def _init_response(
    upload: ArtifactUploadSession,
    *,
    storage: StorageAdapter,
    settings: SettingsDep,
    request: Request,
    session: SessionDep,
) -> ArtifactUploadInitResponse:
    if not _upload_storage_matches(upload, storage):
        raise HTTPException(status_code=503, detail="upload storage namespace is unavailable")
    artifact = await session.get(Artifact, upload.artifact_id) if upload.artifact_id else None
    retryable, retry_after_seconds = _retry_metadata(upload, settings)
    if upload.status != "pending":
        return ArtifactUploadInitResponse(
            upload_session_id=upload.id,
            status=upload.status,  # type: ignore[arg-type]
            expires_at=upload.expires_at,
            artifact=artifact_to_read(artifact) if artifact else None,
            failure_code=upload.failure_code,
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
        )
    expires_timestamp = int(as_utc(upload.expires_at).timestamp())
    local_token = None
    if upload.storage_backend == "local":
        local_token = derive_local_upload_token(upload.id, expires_timestamp, settings)
    target = await storage.create_upload_target(
        session_id=upload.id,
        object_key=upload.temporary_object_key,
        public_api_base_url=_public_api_base_url(request, settings),
        content_type=upload.content_type,
        content_length=upload.expected_size_bytes,
        sha256=upload.expected_sha256,
        expires_at=upload.expires_at,
        local_upload_token=local_token,
    )
    return ArtifactUploadInitResponse(
        upload_session_id=upload.id,
        status="pending",
        method="PUT",
        upload_url=target.url,
        upload_headers=target.headers,
        expires_at=upload.expires_at,
        failure_code=upload.failure_code,
        retryable=retryable,
        retry_after_seconds=retry_after_seconds,
    )


async def _recover_stale_finalizing(
    upload: ArtifactUploadSession,
    *,
    settings: SettingsDep,
    session: SessionDep,
) -> bool:
    if upload.status != "finalizing":
        return False
    cutoff = utc_now() - timedelta(seconds=settings.artifact_finalizing_stale_seconds)
    if as_utc(upload.updated_at) > cutoff:
        return False
    recovered = await session.execute(
        update(ArtifactUploadSession)
        .where(
            ArtifactUploadSession.id == upload.id,
            ArtifactUploadSession.status == "finalizing",
            ArtifactUploadSession.updated_at <= cutoff,
        )
        .values(
            status="pending",
            upload_write_token=None,
            upload_heartbeat_at=None,
            finalization_token=None,
            failure_code="stale_finalizing_recovered",
            updated_at=utc_now(),
        )
        .execution_options(synchronize_session=False)
    )
    if recovered.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        await session.refresh(upload)
        return False
    await session.commit()
    set_committed_value(upload, "status", "pending")
    set_committed_value(upload, "upload_write_token", None)
    set_committed_value(upload, "upload_heartbeat_at", None)
    set_committed_value(upload, "finalization_token", None)
    set_committed_value(upload, "failure_code", "stale_finalizing_recovered")
    return True


async def _reset_finalizing_to_pending(
    upload: ArtifactUploadSession,
    *,
    finalization_token: str,
    failure_code: str,
    session: SessionDep,
) -> bool:
    return await _transition_owned_finalizing(
        upload,
        finalization_token=finalization_token,
        status="pending",
        failure_code=failure_code,
        session=session,
    )


async def _transition_owned_finalizing(
    upload: ArtifactUploadSession,
    *,
    finalization_token: str,
    status: Literal["pending", "failed", "expired"],
    failure_code: str,
    session: SessionDep,
) -> bool:
    values: dict[str, object] = {
        "status": status,
        "upload_write_token": None,
        "upload_heartbeat_at": None,
        "finalization_token": None,
        "failure_code": failure_code,
        "updated_at": utc_now(),
    }
    if status in {"failed", "expired"}:
        values["dedupe_key"] = None
        values["finalized_at"] = utc_now()
    transitioned = await session.execute(
        update(ArtifactUploadSession)
        .where(
            ArtifactUploadSession.id == upload.id,
            ArtifactUploadSession.status == "finalizing",
            ArtifactUploadSession.finalization_token == finalization_token,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if transitioned.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return False
    await session.commit()
    set_committed_value(upload, "status", status)
    set_committed_value(upload, "upload_write_token", None)
    set_committed_value(upload, "upload_heartbeat_at", None)
    set_committed_value(upload, "finalization_token", None)
    set_committed_value(upload, "failure_code", failure_code)
    if status in {"failed", "expired"}:
        set_committed_value(upload, "dedupe_key", None)
        set_committed_value(upload, "finalized_at", values["finalized_at"])
    return True


async def _heartbeat_owned_finalizing(
    upload: ArtifactUploadSession,
    *,
    finalization_token: str,
    session: SessionDep,
) -> bool:
    heartbeat_at = utc_now()
    heartbeat = await session.execute(
        update(ArtifactUploadSession)
        .where(
            ArtifactUploadSession.id == upload.id,
            ArtifactUploadSession.status == "finalizing",
            ArtifactUploadSession.finalization_token == finalization_token,
        )
        .values(updated_at=heartbeat_at)
        .execution_options(synchronize_session=False)
    )
    if heartbeat.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return False
    await session.commit()
    set_committed_value(upload, "updated_at", heartbeat_at)
    return True


async def _touch_artifact_finalization(
    database: Database,
    *,
    upload_id: str,
    finalization_token: str,
) -> bool:
    """Refresh one finalizer token using a connection independent of the request."""

    heartbeat_at = utc_now()
    async with database.session_factory() as heartbeat_session:
        heartbeat = await heartbeat_session.execute(
            update(ArtifactUploadSession)
            .where(
                ArtifactUploadSession.id == upload_id,
                ArtifactUploadSession.status == "finalizing",
                ArtifactUploadSession.finalization_token == finalization_token,
            )
            .values(updated_at=heartbeat_at)
            .execution_options(synchronize_session=False)
        )
        await heartbeat_session.commit()
    return bool(heartbeat.rowcount == 1)  # type: ignore[attr-defined]


async def _run_with_artifact_finalization_heartbeat(
    operation: Callable[[], Awaitable[_T]],
    *,
    database: Database,
    upload_id: str,
    finalization_token: str,
    heartbeat_seconds: int,
) -> _T:
    """Run storage work while keeping the exact finalizer token live."""

    if not await _touch_artifact_finalization(
        database,
        upload_id=upload_id,
        finalization_token=finalization_token,
    ):
        raise ArtifactFinalizationOwnershipLost

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
            if not await _touch_artifact_finalization(
                database,
                upload_id=upload_id,
                finalization_token=finalization_token,
            ):
                ownership_lost = True
                await done.wait()
                break
        task_group.cancel_scope.cancel()

    if not ownership_lost:
        ownership_lost = not await _touch_artifact_finalization(
            database,
            upload_id=upload_id,
            finalization_token=finalization_token,
        )
    if ownership_lost:
        result = results[0] if results else None
        raise ArtifactFinalizationOwnershipLost(result)
    if errors:
        raise errors[0]
    return results[0]


async def _touch_artifact_upload_write(
    database: Database,
    *,
    upload_id: str,
    write_token: str,
) -> bool:
    heartbeat_at = utc_now()
    async with database.session_factory() as heartbeat_session:
        heartbeat = await heartbeat_session.execute(
            update(ArtifactUploadSession)
            .where(
                ArtifactUploadSession.id == upload_id,
                ArtifactUploadSession.status == "pending",
                ArtifactUploadSession.upload_write_token == write_token,
                ArtifactUploadSession.uploaded_at.is_(None),
                ArtifactUploadSession.expires_at > heartbeat_at,
            )
            .values(upload_heartbeat_at=heartbeat_at, updated_at=heartbeat_at)
            .execution_options(synchronize_session=False)
        )
        await heartbeat_session.commit()
    return bool(heartbeat.rowcount == 1)  # type: ignore[attr-defined]


async def _clear_artifact_upload_write(
    database: Database,
    *,
    upload_id: str,
    write_token: str,
) -> bool:
    async with database.session_factory() as claim_session:
        cleared = await claim_session.execute(
            update(ArtifactUploadSession)
            .where(
                ArtifactUploadSession.id == upload_id,
                ArtifactUploadSession.status == "pending",
                ArtifactUploadSession.upload_write_token == write_token,
                ArtifactUploadSession.uploaded_at.is_(None),
            )
            .values(
                upload_write_token=None,
                upload_heartbeat_at=None,
                updated_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
        )
        await claim_session.commit()
    return bool(cleared.rowcount == 1)  # type: ignore[attr-defined]


async def _expire_artifact_upload_write(
    database: Database,
    *,
    upload_id: str,
    write_token: str,
) -> bool:
    async with database.session_factory() as claim_session:
        expired = await claim_session.execute(
            update(ArtifactUploadSession)
            .where(
                ArtifactUploadSession.id == upload_id,
                ArtifactUploadSession.status == "pending",
                ArtifactUploadSession.upload_write_token == write_token,
                ArtifactUploadSession.uploaded_at.is_(None),
            )
            .values(
                status="expired",
                upload_write_token=None,
                upload_heartbeat_at=None,
                failure_code="upload_write_deadline_exceeded",
                dedupe_key=None,
                finalized_at=utc_now(),
                updated_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
        )
        await claim_session.commit()
    return bool(expired.rowcount == 1)  # type: ignore[attr-defined]


async def _run_with_artifact_upload_write_heartbeat(
    operation: Callable[[], Awaitable[_T]],
    *,
    database: Database,
    upload_id: str,
    write_token: str,
    heartbeat_seconds: int,
) -> _T:
    async def heartbeat() -> bool:
        return await _touch_artifact_upload_write(
            database,
            upload_id=upload_id,
            write_token=write_token,
        )

    if not await heartbeat():
        raise ArtifactUploadWriteOwnershipLost
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
            if not await heartbeat():
                ownership_lost = True
                await done.wait()
                break
        task_group.cancel_scope.cancel()
    if not ownership_lost:
        ownership_lost = not await heartbeat()
    if ownership_lost:
        raise ArtifactUploadWriteOwnershipLost
    if errors:
        raise errors[0]
    return results[0]


async def _enforce_attempt_artifact_quota(
    *,
    attempt_id: str,
    requested_size: int,
    settings: SettingsDep,
    session: SessionDep,
) -> None:
    await session.execute(
        select(JobAttempt.id).where(JobAttempt.id == attempt_id).with_for_update()
    )
    quota_session = or_(
        ArtifactUploadSession.status.in_(("pending", "finalizing", "completed")),
        and_(
            ArtifactUploadSession.status.in_(("failed", "expired")),
            or_(
                ArtifactUploadSession.staging_cleanup_completed_at.is_(None),
                ArtifactUploadSession.canonical_cleanup_completed_at.is_(None),
            ),
        ),
    )
    session_count = (
        await session.scalar(
            select(func.count())
            .select_from(ArtifactUploadSession)
            .where(
                ArtifactUploadSession.attempt_id == attempt_id,
                quota_session,
            )
        )
        or 0
    )
    if session_count >= settings.artifact_attempt_max_sessions:
        raise HTTPException(status_code=409, detail="artifact session quota exceeded")
    active_bytes = (
        await session.scalar(
            select(func.coalesce(func.sum(ArtifactUploadSession.expected_size_bytes), 0)).where(
                ArtifactUploadSession.attempt_id == attempt_id,
                quota_session,
            )
        )
        or 0
    )
    if int(active_bytes) + requested_size > settings.artifact_attempt_max_bytes:
        raise HTTPException(status_code=409, detail="artifact byte quota exceeded")


async def _cleanup_terminal_artifact_staging(
    upload: ArtifactUploadSession,
    *,
    database: Database,
    storage: StorageAdapter,
    settings: SettingsDep,
    session: SessionDep,
) -> bool:
    """Run the claimed reconciler; S3 completion requires its second delete."""
    if upload.status not in {"completed", "failed", "expired"}:
        return False
    upload_id = upload.id
    await session.rollback()
    await reconcile_artifact_upload_cleanup(
        database,
        storage,
        settings,
        upload_ids=(upload_id,),
    )
    refreshed = await session.scalar(
        select(ArtifactUploadSession)
        .where(ArtifactUploadSession.id == upload_id)
        .execution_options(populate_existing=True)
    )
    if refreshed is None:
        return False
    completed = refreshed.staging_cleanup_completed_at is not None
    await session.rollback()
    return completed


async def _expire_upload(
    upload: ArtifactUploadSession,
    *,
    database: Database,
    storage: StorageAdapter,
    settings: SettingsDep,
    session: SessionDep,
) -> bool:
    if not _upload_storage_matches(upload, storage):
        raise HTTPException(status_code=503, detail="upload storage namespace is unavailable")
    expired = await session.execute(
        update(ArtifactUploadSession)
        .where(
            ArtifactUploadSession.id == upload.id,
            ArtifactUploadSession.status == "pending",
            ArtifactUploadSession.upload_write_token.is_(None),
            ArtifactUploadSession.finalization_token.is_(None),
            ArtifactUploadSession.expires_at <= utc_now(),
        )
        .values(
            status="expired",
            upload_heartbeat_at=None,
            failure_code="upload_expired",
            dedupe_key=None,
            finalized_at=utc_now(),
            updated_at=utc_now(),
        )
        .execution_options(synchronize_session=False)
    )
    if expired.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return False
    await session.commit()
    set_committed_value(upload, "status", "expired")
    set_committed_value(upload, "upload_heartbeat_at", None)
    set_committed_value(upload, "failure_code", "upload_expired")
    set_committed_value(upload, "dedupe_key", None)
    set_committed_value(upload, "finalized_at", utc_now())
    await _cleanup_terminal_artifact_staging(
        upload,
        database=database,
        storage=storage,
        settings=settings,
        session=session,
    )
    await session.refresh(upload)
    return True


async def _find_existing_upload(
    session: SessionDep,
    *,
    attempt_id: str,
    idempotency_key: str,
    dedupe_key: str,
) -> ArtifactUploadSession | None:
    by_idempotency = await session.scalar(
        select(ArtifactUploadSession)
        .where(
            ArtifactUploadSession.attempt_id == attempt_id,
            ArtifactUploadSession.idempotency_key == idempotency_key,
        )
        .order_by(ArtifactUploadSession.generation.desc())
        .limit(1)
    )
    if by_idempotency is not None:
        return by_idempotency
    return await _find_deduplicated_upload(session, dedupe_key=dedupe_key)


async def _find_deduplicated_upload(
    session: SessionDep,
    *,
    dedupe_key: str,
) -> ArtifactUploadSession | None:
    return cast(
        ArtifactUploadSession | None,
        await session.scalar(
            select(ArtifactUploadSession).where(ArtifactUploadSession.dedupe_key == dedupe_key)
        ),
    )


@router.post(
    "/workers/jobs/{job_id}/artifact-uploads/init",
    response_model=ArtifactUploadInitResponse,
    status_code=status.HTTP_201_CREATED,
)
async def initialize_artifact_upload(
    job_id: str,
    payload: ArtifactUploadInitRequest,
    request: Request,
    response: Response,
    worker: WorkerDep,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> ArtifactUploadInitResponse:
    response.headers["Cache-Control"] = "no-store"
    database = cast(Database, request.app.state.database)
    actor_worker_id = worker.id
    if payload.size_bytes > settings.artifact_max_bytes:
        raise HTTPException(status_code=413, detail="artifact exceeds configured size limit")
    if (
        payload.artifact_type is ArtifactType.SAMPLE
        and payload.size_bytes > settings.sample_max_bytes
    ):
        raise HTTPException(status_code=413, detail="sample exceeds configured size limit")
    lease = await require_active_lease(
        session,
        worker_id=actor_worker_id,
        job_id=job_id,
        lease_id=payload.lease_id,
    )
    if lease.attempt_id != payload.attempt_id:
        raise HTTPException(status_code=409, detail="upload attempt does not match lease")
    job = await session.get(Job, job_id)
    attempt = await session.get(JobAttempt, lease.attempt_id)
    if job is None or attempt is None or job.current_attempt_id != attempt.id:
        raise HTTPException(status_code=409, detail="job attempt is no longer current")

    fingerprint = upload_request_fingerprint(payload)
    dedupe_key = upload_dedupe_key(
        payload.attempt_id,
        payload.artifact_type.value,
        payload.sha256,
    )
    existing = await _find_existing_upload(
        session,
        attempt_id=payload.attempt_id,
        idempotency_key=payload.idempotency_key,
        dedupe_key=dedupe_key,
    )
    generation = 1
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="idempotency key or artifact checksum conflicts with prior payload",
            )
        if not _upload_storage_matches(existing, storage):
            raise HTTPException(status_code=503, detail="upload storage namespace is unavailable")
        if existing.status == "finalizing":
            await _recover_stale_finalizing(
                existing,
                settings=settings,
                session=session,
            )
        if existing.status == "pending" and as_utc(existing.expires_at) <= utc_now():
            existing_id = existing.id
            active_write_token = existing.upload_write_token
            if active_write_token is not None:
                await session.rollback()
                expired = await _expire_artifact_upload_write(
                    database,
                    upload_id=existing_id,
                    write_token=active_write_token,
                )
                existing = await session.get(ArtifactUploadSession, existing_id)
                if existing is None:
                    raise HTTPException(status_code=409, detail="artifact upload disappeared")
            else:
                expired = await _expire_upload(
                    existing,
                    database=database,
                    storage=storage,
                    settings=settings,
                    session=session,
                )
            if not expired:
                raise HTTPException(status_code=409, detail="artifact upload state changed")
        if existing.status == "expired":
            if existing.idempotency_key == payload.idempotency_key:
                generation = existing.generation + 1
            existing = await _find_deduplicated_upload(session, dedupe_key=dedupe_key)
            if existing is not None and existing.request_fingerprint != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="artifact checksum conflicts with an active upload payload",
                )
        if existing is not None:
            return await _init_response(
                existing,
                storage=storage,
                settings=settings,
                request=request,
                session=session,
            )

    now = utc_now()
    upload_id = str(uuid.uuid4())
    expires_at = now + timedelta(
        seconds=effective_artifact_upload_ttl_seconds(payload.size_bytes, settings)
    )
    local_token = None
    local_token_hash = None
    if storage.backend == "local":
        local_token = derive_local_upload_token(
            upload_id,
            int(expires_at.timestamp()),
            settings,
        )
        local_token_hash = upload_token_hash(local_token)
    temporary_object_key = staging_object_key(payload.attempt_id, upload_id)
    verified_object_key = canonical_object_key(
        job_id,
        payload.attempt_id,
        payload.artifact_type.value,
        upload_id,
    )

    # Target generation may use a thread-backed signer. End the observational
    # transaction before it, then revalidate the exact lease/config and reserve
    # quota atomically. An unused presign is never returned and has no DB row.
    await session.rollback()
    try:
        target = await storage.create_upload_target(
            session_id=upload_id,
            object_key=temporary_object_key,
            public_api_base_url=_public_api_base_url(request, settings),
            content_type=payload.content_type,
            content_length=payload.size_bytes,
            sha256=payload.sha256,
            expires_at=expires_at,
            local_upload_token=local_token,
        )
    except StorageError as exc:
        raise HTTPException(status_code=503, detail="object upload signing failed") from exc

    lease = await require_active_lease(
        session,
        worker_id=actor_worker_id,
        job_id=job_id,
        lease_id=payload.lease_id,
    )
    if lease.attempt_id != payload.attempt_id:
        raise HTTPException(status_code=409, detail="upload attempt does not match lease")
    job = await session.get(Job, job_id)
    attempt = await session.get(JobAttempt, lease.attempt_id)
    if job is None or attempt is None or job.current_attempt_id != attempt.id:
        raise HTTPException(status_code=409, detail="job attempt is no longer current")
    raced_before_reservation = await _find_existing_upload(
        session,
        attempt_id=payload.attempt_id,
        idempotency_key=payload.idempotency_key,
        dedupe_key=dedupe_key,
    )
    if raced_before_reservation is not None:
        if raced_before_reservation.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="artifact upload session conflict")
        if raced_before_reservation.status == "expired":
            if raced_before_reservation.idempotency_key == payload.idempotency_key:
                generation = max(generation, raced_before_reservation.generation + 1)
            raced_before_reservation = await _find_deduplicated_upload(
                session,
                dedupe_key=dedupe_key,
            )
        if raced_before_reservation is not None:
            return await _init_response(
                raced_before_reservation,
                storage=storage,
                settings=settings,
                request=request,
                session=session,
            )

    await _enforce_attempt_artifact_quota(
        attempt_id=payload.attempt_id,
        requested_size=payload.size_bytes,
        settings=settings,
        session=session,
    )
    upload = ArtifactUploadSession(
        id=upload_id,
        job_id=job_id,
        attempt_id=payload.attempt_id,
        lease_id=payload.lease_id,
        worker_id=actor_worker_id,
        artifact_type=payload.artifact_type.value,
        filename=payload.filename,
        content_type=payload.content_type,
        expected_size_bytes=payload.size_bytes,
        expected_sha256=payload.sha256,
        metadata_json=payload.metadata,
        idempotency_key=payload.idempotency_key,
        generation=generation,
        request_fingerprint=fingerprint,
        dedupe_key=dedupe_key,
        temporary_object_key=temporary_object_key,
        canonical_object_key=verified_object_key,
        storage_backend=storage.backend,
        storage_namespace_sha256=storage.namespace_fingerprint,
        status="pending",
        upload_token_hash=local_token_hash,
        expires_at=expires_at,
    )
    session.add(upload)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raced = await _find_existing_upload(
            session,
            attempt_id=payload.attempt_id,
            idempotency_key=payload.idempotency_key,
            dedupe_key=dedupe_key,
        )
        if raced is not None and raced.status == "expired":
            raced = await _find_deduplicated_upload(session, dedupe_key=dedupe_key)
        if raced is None or raced.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="artifact upload session conflict") from exc
        return await _init_response(
            raced,
            storage=storage,
            settings=settings,
            request=request,
            session=session,
        )
    await session.commit()
    return ArtifactUploadInitResponse(
        upload_session_id=upload.id,
        status="pending",
        method="PUT",
        upload_url=target.url,
        upload_headers=target.headers,
        expires_at=upload.expires_at,
        retryable=True,
    )


@router.put(
    "/storage/uploads/{upload_session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def local_presigned_upload(
    upload_session_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    upload_token: Annotated[str | None, Header(alias="X-RVC-Upload-Token")] = None,
) -> Response:
    if not isinstance(storage, LocalStorageAdapter):
        raise HTTPException(status_code=404, detail="upload endpoint not found")
    database = cast(Database, request.app.state.database)
    upload = await session.get(ArtifactUploadSession, upload_session_id)
    if upload is None or upload.storage_backend != "local":
        raise HTTPException(status_code=404, detail="upload session not found")
    if not _upload_storage_matches(upload, storage):
        raise HTTPException(status_code=503, detail="upload storage namespace is unavailable")
    if upload.status != "pending":
        raise HTTPException(status_code=409, detail="upload session is not writable")
    if as_utc(upload.expires_at) <= utc_now():
        active_write_token = upload.upload_write_token
        if active_write_token is not None:
            await session.rollback()
            expired = await _expire_artifact_upload_write(
                database,
                upload_id=upload_session_id,
                write_token=active_write_token,
            )
        else:
            expired = await _expire_upload(
                upload,
                database=database,
                storage=storage,
                settings=settings,
                session=session,
            )
        if not expired:
            raise HTTPException(status_code=409, detail="artifact upload state changed")
        raise HTTPException(status_code=410, detail="upload session expired")
    if upload.uploaded_at is not None:
        raise HTTPException(status_code=409, detail="upload session is already sealed")
    if upload.upload_write_token is not None:
        raise HTTPException(status_code=409, detail="upload session write is active")
    if upload_token is None or not verify_upload_token(
        upload_token,
        upload.upload_token_hash,
    ):
        raise HTTPException(status_code=401, detail="invalid upload token")
    raw_length = request.headers.get("content-length")
    if raw_length is None:
        raise HTTPException(status_code=411, detail="Content-Length is required")
    try:
        content_length = int(raw_length)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
    if content_length != upload.expected_size_bytes:
        raise HTTPException(status_code=422, detail="Content-Length does not match session")
    if request.headers.get("content-type", "").lower() != upload.content_type:
        raise HTTPException(status_code=422, detail="Content-Type does not match session")

    expected_object_key = upload.temporary_object_key
    expected_size_bytes = upload.expected_size_bytes
    write_expires_at = as_utc(upload.expires_at)
    write_token = str(uuid.uuid4())
    claimed_at = utc_now()
    claimed = await session.execute(
        update(ArtifactUploadSession)
        .where(
            ArtifactUploadSession.id == upload_session_id,
            ArtifactUploadSession.status == "pending",
            ArtifactUploadSession.upload_write_token.is_(None),
            ArtifactUploadSession.finalization_token.is_(None),
            ArtifactUploadSession.uploaded_at.is_(None),
            ArtifactUploadSession.expires_at > claimed_at,
        )
        .values(
            upload_write_token=write_token,
            upload_heartbeat_at=claimed_at,
            failure_code=None,
            updated_at=claimed_at,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise HTTPException(status_code=409, detail="upload session write conflict")
    await session.commit()

    try:
        remaining_seconds = max(0.0, (write_expires_at - utc_now()).total_seconds())
        with anyio.fail_after(remaining_seconds):
            await _run_with_artifact_upload_write_heartbeat(
                lambda: storage.write_upload_stream(
                    expected_object_key,
                    request.stream(),
                    expected_size=expected_size_bytes,
                ),
                database=database,
                upload_id=upload_session_id,
                write_token=write_token,
                heartbeat_seconds=settings.artifact_upload_write_heartbeat_seconds,
            )
    except TimeoutError as exc:
        await _expire_artifact_upload_write(
            database,
            upload_id=upload_session_id,
            write_token=write_token,
        )
        raise HTTPException(status_code=408, detail="artifact upload deadline exceeded") from exc
    except ArtifactUploadWriteOwnershipLost as exc:
        expired = await _expire_artifact_upload_write(
            database,
            upload_id=upload_session_id,
            write_token=write_token,
        )
        if not expired:
            await _clear_artifact_upload_write(
                database,
                upload_id=upload_session_id,
                write_token=write_token,
            )
        raise HTTPException(status_code=409, detail="upload write ownership changed") from exc
    except ObjectTooLarge as exc:
        await _clear_artifact_upload_write(
            database,
            upload_id=upload_session_id,
            write_token=write_token,
        )
        raise HTTPException(status_code=413, detail="uploaded object exceeds session size") from exc
    except ObjectSizeMismatch as exc:
        await _clear_artifact_upload_write(
            database,
            upload_id=upload_session_id,
            write_token=write_token,
        )
        raise HTTPException(status_code=422, detail="uploaded object size mismatch") from exc
    except StorageError as exc:
        await _clear_artifact_upload_write(
            database,
            upload_id=upload_session_id,
            write_token=write_token,
        )
        raise HTTPException(status_code=503, detail="local object upload failed") from exc
    except anyio.get_cancelled_exc_class():
        with anyio.CancelScope(shield=True):
            await _clear_artifact_upload_write(
                database,
                upload_id=upload_session_id,
                write_token=write_token,
            )
        raise

    sealed_at = utc_now()
    sealed = await session.execute(
        update(ArtifactUploadSession)
        .where(
            ArtifactUploadSession.id == upload_session_id,
            ArtifactUploadSession.status == "pending",
            ArtifactUploadSession.upload_write_token == write_token,
            ArtifactUploadSession.finalization_token.is_(None),
            ArtifactUploadSession.uploaded_at.is_(None),
            ArtifactUploadSession.expires_at > sealed_at,
        )
        .values(
            uploaded_at=sealed_at,
            upload_write_token=None,
            upload_heartbeat_at=None,
            failure_code=None,
            updated_at=sealed_at,
        )
        .execution_options(synchronize_session=False)
    )
    if sealed.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        await _expire_artifact_upload_write(
            database,
            upload_id=upload_session_id,
            write_token=write_token,
        )
        raise HTTPException(status_code=409, detail="upload write ownership changed")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _mark_verification_failure(
    upload: ArtifactUploadSession,
    *,
    finalization_token: str,
    failure_code: str,
    database: Database,
    storage: StorageAdapter,
    settings: SettingsDep,
    session: SessionDep,
) -> bool:
    if not _upload_storage_matches(upload, storage):
        raise HTTPException(status_code=503, detail="upload storage namespace is unavailable")
    transitioned = await _transition_owned_finalizing(
        upload,
        finalization_token=finalization_token,
        status="failed",
        failure_code=failure_code,
        session=session,
    )
    if not transitioned:
        return False
    canonical_not_required_at = utc_now()
    await session.execute(
        update(ArtifactUploadSession)
        .where(
            ArtifactUploadSession.id == upload.id,
            ArtifactUploadSession.status == "failed",
            ArtifactUploadSession.canonical_cleanup_completed_at.is_(None),
        )
        .values(
            canonical_cleanup_completed_at=canonical_not_required_at,
            updated_at=canonical_not_required_at,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    set_committed_value(
        upload,
        "canonical_cleanup_completed_at",
        canonical_not_required_at,
    )
    await _cleanup_terminal_artifact_staging(
        upload,
        database=database,
        storage=storage,
        settings=settings,
        session=session,
    )
    return True


async def _fail_owned_after_canonical_publish(
    upload: ArtifactUploadSession,
    *,
    finalization_token: str,
    failure_code: str,
    database: Database,
    storage: StorageAdapter,
    settings: SettingsDep,
    session: SessionDep,
) -> bool:
    transitioned = await _transition_owned_finalizing(
        upload,
        finalization_token=finalization_token,
        status="failed",
        failure_code=failure_code,
        session=session,
    )
    if not transitioned:
        return False
    # Canonical cleanup is deliberately deferred. A previously recovered
    # finalizer can still finish its external publish after losing the DB token;
    # the API-owned reconciler waits the stale-operation window and requires a
    # claimed first delete plus a confirmation delete.
    await _cleanup_terminal_artifact_staging(
        upload,
        database=database,
        storage=storage,
        settings=settings,
        session=session,
    )
    return True


async def _reload_artifact_upload_for_cleanup(
    session: SessionDep,
    upload_id: str,
) -> ArtifactUploadSession:
    upload = await session.scalar(
        select(ArtifactUploadSession)
        .where(ArtifactUploadSession.id == upload_id)
        .execution_options(populate_existing=True)
    )
    if upload is None:
        raise HTTPException(status_code=409, detail="upload finalization ownership changed")
    return upload


@router.post(
    "/workers/jobs/{job_id}/artifact-uploads/{upload_session_id}/finalize",
    response_model=ArtifactRead,
)
async def finalize_artifact_upload(
    job_id: str,
    upload_session_id: str,
    payload: ArtifactUploadFinalizeRequest,
    request: Request,
    worker: WorkerDep,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    mlflow: MlflowDep,
) -> ArtifactRead:
    database = cast(Database, request.app.state.database)
    heartbeat_seconds = max(1, min(30, settings.artifact_finalizing_stale_seconds // 3))
    actor_worker_id = worker.id
    lease = await require_active_lease(
        session,
        worker_id=actor_worker_id,
        job_id=job_id,
        lease_id=payload.lease_id,
    )
    upload = await session.get(ArtifactUploadSession, upload_session_id)
    if upload is None or not _upload_belongs_to_claim(
        upload,
        job_id=job_id,
        attempt_id=payload.attempt_id,
        lease_id=payload.lease_id,
        worker_id=actor_worker_id,
    ):
        raise HTTPException(status_code=409, detail="upload session does not match lease")
    owned_upload_id = upload.id
    owned_temporary_object_key = upload.temporary_object_key
    owned_canonical_object_key = upload.canonical_object_key
    owned_expected_size_bytes = upload.expected_size_bytes
    owned_expected_sha256 = upload.expected_sha256
    owned_content_type = upload.content_type
    if not _upload_storage_matches(upload, storage):
        raise HTTPException(status_code=503, detail="upload storage namespace is unavailable")
    if upload.status == "completed" and upload.artifact_id:
        artifact = await session.get(Artifact, upload.artifact_id)
        if artifact is not None:
            await mlflow.sync_after_commit(artifact_event_key(artifact.id))
            return artifact_to_read(artifact)
    if upload.status in {"failed", "expired"}:
        raise HTTPException(status_code=409, detail=f"upload session is {upload.status}")
    if upload.status == "finalizing":
        recovered = await _recover_stale_finalizing(
            upload,
            settings=settings,
            session=session,
        )
        if not recovered:
            raise HTTPException(status_code=409, detail="upload session is already finalizing")
    if upload.upload_write_token is not None:
        raise HTTPException(status_code=409, detail="upload session write is active")
    if upload.storage_backend == "local" and upload.uploaded_at is None:
        raise HTTPException(status_code=409, detail="local upload is not sealed")
    if as_utc(upload.expires_at) <= utc_now():
        expired = await _expire_upload(
            upload,
            database=database,
            storage=storage,
            settings=settings,
            session=session,
        )
        if not expired:
            raise HTTPException(status_code=409, detail="artifact upload state changed")
        raise HTTPException(status_code=409, detail="upload session expired")
    finalization_token = str(uuid.uuid4())
    claimed = await session.execute(
        update(ArtifactUploadSession)
        .where(
            ArtifactUploadSession.id == upload.id,
            ArtifactUploadSession.status == "pending",
            ArtifactUploadSession.upload_write_token.is_(None),
            ArtifactUploadSession.finalization_token.is_(None),
            or_(
                ArtifactUploadSession.storage_backend != "local",
                ArtifactUploadSession.uploaded_at.is_not(None),
            ),
        )
        .values(
            status="finalizing",
            finalization_token=finalization_token,
            failure_code=None,
            updated_at=utc_now(),
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise HTTPException(status_code=409, detail="upload session finalization conflict")
    await session.commit()
    set_committed_value(upload, "status", "finalizing")
    set_committed_value(upload, "finalization_token", finalization_token)
    set_committed_value(upload, "failure_code", None)

    spool_path: Path | None = None
    try:
        spool_path = await _run_with_artifact_finalization_heartbeat(
            lambda: verify_object_to_spool(
                storage,
                owned_temporary_object_key,
                expected_size=owned_expected_size_bytes,
                expected_sha256=owned_expected_sha256,
                settings=settings,
            ),
            database=database,
            upload_id=owned_upload_id,
            finalization_token=finalization_token,
            heartbeat_seconds=heartbeat_seconds,
        )
    except ArtifactFinalizationOwnershipLost as exc:
        if isinstance(exc.result, Path):
            try:
                await remove_spool_file(exc.result)
            except ArtifactSpoolError:
                pass
        raise HTTPException(
            status_code=409,
            detail="upload finalization ownership changed",
        ) from exc
    except ObjectNotFound as exc:
        transitioned = await _reset_finalizing_to_pending(
            upload,
            finalization_token=finalization_token,
            failure_code="uploaded_object_not_found",
            session=session,
        )
        if not transitioned:
            raise HTTPException(
                status_code=409, detail="upload finalization ownership changed"
            ) from exc
        raise HTTPException(status_code=409, detail="uploaded object not found") from exc
    except ArtifactSpoolError as exc:
        transitioned = await _reset_finalizing_to_pending(
            upload,
            finalization_token=finalization_token,
            failure_code=exc.failure_code,
            session=session,
        )
        if not transitioned:
            raise HTTPException(
                status_code=409, detail="upload finalization ownership changed"
            ) from exc
        raise HTTPException(
            status_code=503,
            detail="artifact verification spool is temporarily unavailable",
        ) from exc
    except (ArtifactVerificationMismatch, ObjectTooLarge) as exc:
        failure_code = (
            exc.failure_code if isinstance(exc, ArtifactVerificationMismatch) else "size_mismatch"
        )
        transitioned = await _mark_verification_failure(
            upload,
            finalization_token=finalization_token,
            failure_code=failure_code,
            database=database,
            storage=storage,
            settings=settings,
            session=session,
        )
        if not transitioned:
            raise HTTPException(
                status_code=409, detail="upload finalization ownership changed"
            ) from exc
        raise HTTPException(
            status_code=422,
            detail="uploaded artifact failed size or SHA-256 verification",
        ) from exc
    except StorageError as exc:
        transitioned = await _reset_finalizing_to_pending(
            upload,
            finalization_token=finalization_token,
            failure_code="verification_read_failed",
            session=session,
        )
        if not transitioned:
            raise HTTPException(
                status_code=409, detail="upload finalization ownership changed"
            ) from exc
        raise HTTPException(status_code=503, detail="artifact verification read failed") from exc

    try:
        lease = await require_active_lease(
            session,
            worker_id=actor_worker_id,
            job_id=job_id,
            lease_id=payload.lease_id,
        )
        if lease.attempt_id != payload.attempt_id:
            raise HTTPException(status_code=409, detail="upload attempt does not match lease")
    except HTTPException as exc:
        await session.rollback()
        upload = await _reload_artifact_upload_for_cleanup(session, owned_upload_id)
        spool_failure: ArtifactSpoolError | None = None
        assert spool_path is not None
        try:
            await remove_spool_file(spool_path)
        except ArtifactSpoolError as cleanup_exc:
            spool_failure = cleanup_exc
        failure_code = (
            spool_failure.failure_code
            if spool_failure is not None
            else (
                "job_config_integrity_failed"
                if exc.detail == "job configuration integrity check failed"
                else "claim_changed_after_verification"
            )
        )
        transitioned = await _mark_verification_failure(
            upload,
            finalization_token=finalization_token,
            failure_code=failure_code,
            database=database,
            storage=storage,
            settings=settings,
            session=session,
        )
        if not transitioned:
            raise HTTPException(
                status_code=409, detail="upload finalization ownership changed"
            ) from exc
        if spool_failure is not None:
            raise HTTPException(
                status_code=503,
                detail="artifact verification spool cleanup failed",
            ) from spool_failure
        raise

    # The pre-publish check is observational. Do not keep its transaction open
    # while streaming to the object store; the post-publish write fence below
    # revalidates the complete claim before any ledger commit.
    await session.rollback()
    assert spool_path is not None
    verified_spool_path = spool_path

    async def publish_canonical() -> None:
        await storage.store_verified_file(
            owned_canonical_object_key,
            verified_spool_path,
            content_type=owned_content_type,
            sha256=owned_expected_sha256,
        )

    try:
        await _run_with_artifact_finalization_heartbeat(
            publish_canonical,
            database=database,
            upload_id=owned_upload_id,
            finalization_token=finalization_token,
            heartbeat_seconds=heartbeat_seconds,
        )
    except ArtifactFinalizationOwnershipLost as exc:
        try:
            await remove_spool_file(verified_spool_path)
        except ArtifactSpoolError:
            pass
        # A replacement token may now own the same immutable canonical key.
        raise HTTPException(
            status_code=409,
            detail="upload finalization ownership changed",
        ) from exc
    except StorageError as exc:
        try:
            await remove_spool_file(verified_spool_path)
        except ArtifactSpoolError:
            pass
        upload = await _reload_artifact_upload_for_cleanup(session, owned_upload_id)
        transitioned = await _fail_owned_after_canonical_publish(
            upload,
            finalization_token=finalization_token,
            failure_code="storage_publish_failed",
            database=database,
            storage=storage,
            settings=settings,
            session=session,
        )
        if not transitioned:
            raise HTTPException(
                status_code=409, detail="upload finalization ownership changed"
            ) from exc
        raise HTTPException(status_code=503, detail="verified artifact publish failed") from exc

    try:
        await remove_spool_file(verified_spool_path)
        spool_path = None
    except ArtifactSpoolError as exc:
        upload = await _reload_artifact_upload_for_cleanup(session, owned_upload_id)
        transitioned = await _fail_owned_after_canonical_publish(
            upload,
            finalization_token=finalization_token,
            failure_code=exc.failure_code,
            database=database,
            storage=storage,
            settings=settings,
            session=session,
        )
        if not transitioned:
            raise HTTPException(
                status_code=409, detail="upload finalization ownership changed"
            ) from exc
        raise HTTPException(
            status_code=503,
            detail="artifact verification spool cleanup failed",
        ) from exc

    try:
        lease = await require_active_lease(
            session,
            worker_id=actor_worker_id,
            job_id=job_id,
            lease_id=payload.lease_id,
            for_update=True,
        )
        if lease.attempt_id != payload.attempt_id:
            raise HTTPException(status_code=409, detail="upload attempt does not match lease")
        upload = await _reload_artifact_upload_for_cleanup(session, owned_upload_id)
        if (
            upload.status != "finalizing"
            or upload.finalization_token != finalization_token
        ):
            raise HTTPException(status_code=409, detail="upload finalization ownership changed")
    except HTTPException as exc:
        # Release Lease -> Job -> Attempt locks before any object-store cleanup.
        await session.rollback()
        upload = await _reload_artifact_upload_for_cleanup(session, owned_upload_id)
        claim_failure_code = (
            "job_config_integrity_failed"
            if exc.detail == "job configuration integrity check failed"
            else "claim_changed_after_verification"
        )
        transitioned = await _fail_owned_after_canonical_publish(
            upload,
            finalization_token=finalization_token,
            failure_code=claim_failure_code,
            database=database,
            storage=storage,
            settings=settings,
            session=session,
        )
        if not transitioned:
            raise HTTPException(
                status_code=409, detail="upload finalization ownership changed"
            ) from exc
        raise

    manager_metadata = dict(upload.metadata_json)
    manager_metadata["manager_verification"] = {
        "algorithm": "sha256",
        "bounded_stream": True,
        "upload_session_id": upload.id,
        "storage_backend": storage.backend,
    }
    artifact = Artifact(
        job_id=upload.job_id,
        attempt_id=upload.attempt_id,
        artifact_type=upload.artifact_type,
        filename=upload.filename,
        storage_uri=storage.storage_uri(upload.canonical_object_key),
        size_bytes=upload.expected_size_bytes,
        sha256=upload.expected_sha256,
        mime_type=upload.content_type,
        metadata_json=manager_metadata,
    )
    session.add(artifact)
    try:
        await session.flush()
        job = await session.get(Job, artifact.job_id)
        if job is None:
            raise HTTPException(status_code=409, detail="artifact job no longer exists")
        mlflow_event_key = await mlflow.enqueue_artifact(
            session,
            job=job,
            artifact=artifact,
        )
        now = utc_now()
        completed = await session.execute(
            update(ArtifactUploadSession)
            .where(
                ArtifactUploadSession.id == upload.id,
                ArtifactUploadSession.status == "finalizing",
                ArtifactUploadSession.finalization_token == finalization_token,
            )
            .values(
                artifact_id=artifact.id,
                status="completed",
                upload_write_token=None,
                upload_heartbeat_at=None,
                finalization_token=None,
                failure_code=None,
                uploaded_at=upload.uploaded_at or now,
                finalized_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if completed.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            raise ArtifactFinalizationOwnershipLost
        await session.commit()
        set_committed_value(upload, "artifact_id", artifact.id)
        set_committed_value(upload, "status", "completed")
        set_committed_value(upload, "upload_write_token", None)
        set_committed_value(upload, "upload_heartbeat_at", None)
        set_committed_value(upload, "finalization_token", None)
        set_committed_value(upload, "failure_code", None)
        set_committed_value(upload, "finalized_at", now)
    except ArtifactFinalizationOwnershipLost as exc:
        # Another token may now own the same immutable canonical key.
        await session.rollback()
        raise HTTPException(status_code=409, detail="upload finalization lost ownership") from exc
    except InvalidJobConfigLedger as exc:
        await session.rollback()
        upload = await _reload_artifact_upload_for_cleanup(session, owned_upload_id)
        transitioned = await _fail_owned_after_canonical_publish(
            upload,
            finalization_token=finalization_token,
            failure_code="job_config_integrity_failed",
            database=database,
            storage=storage,
            settings=settings,
            session=session,
        )
        if not transitioned:
            raise HTTPException(
                status_code=409, detail="upload finalization ownership changed"
            ) from exc
        raise HTTPException(
            status_code=409,
            detail="job configuration integrity check failed",
        ) from exc
    except HTTPException as exc:
        await session.rollback()
        upload = await _reload_artifact_upload_for_cleanup(session, owned_upload_id)
        transitioned = await _fail_owned_after_canonical_publish(
            upload,
            finalization_token=finalization_token,
            failure_code="artifact_commit_conflict",
            database=database,
            storage=storage,
            settings=settings,
            session=session,
        )
        if not transitioned:
            raise HTTPException(
                status_code=409, detail="upload finalization ownership changed"
            ) from exc
        raise
    except IntegrityError as exc:
        await session.rollback()
        upload = await _reload_artifact_upload_for_cleanup(session, owned_upload_id)
        transitioned = await _fail_owned_after_canonical_publish(
            upload,
            finalization_token=finalization_token,
            failure_code="artifact_commit_conflict",
            database=database,
            storage=storage,
            settings=settings,
            session=session,
        )
        if not transitioned:
            raise HTTPException(
                status_code=409, detail="upload finalization ownership changed"
            ) from exc
        raise HTTPException(status_code=409, detail="canonical artifact already exists") from exc

    # Local upload credentials are revoked by the completed DB state and can be
    # cleaned immediately. S3 staging remains as the If-None-Match seal until
    # the signed PUT URL and its configured grace have elapsed.
    await _cleanup_terminal_artifact_staging(
        upload,
        database=database,
        storage=storage,
        settings=settings,
        session=session,
    )
    await session.refresh(artifact)
    await mlflow.sync_after_commit(mlflow_event_key)
    return artifact_to_read(artifact)


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: str,
    request: Request,
    user: CurrentUserDep,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> Response:
    artifact = await session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    try:
        await require_job_owner_or_admin(session, job_id=artifact.job_id, user=user)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="artifact not found") from exc
        raise
    upload = await session.scalar(
        select(ArtifactUploadSession).where(
            ArtifactUploadSession.artifact_id == artifact.id,
            ArtifactUploadSession.status == "completed",
        )
    )
    if upload is None:
        raise HTTPException(status_code=409, detail="artifact has not been server-verified")
    if not _upload_storage_matches(upload, storage):
        raise HTTPException(status_code=503, detail="artifact storage namespace is unavailable")
    filename = safe_download_filename(artifact.filename, artifact.id)
    disposition = attachment_content_disposition(filename)
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="artifact.download_requested",
        resource_type="artifact",
        resource_id=artifact.id,
    )
    await session.commit()
    download_url = await storage.create_download_url(
        upload.canonical_object_key,
        content_disposition=disposition,
        expires_in_seconds=settings.artifact_download_ttl_seconds,
    )
    common_headers = {
        "Cache-Control": "private, no-store",
        "Content-Disposition": disposition,
        "X-Content-Type-Options": "nosniff",
    }
    manager_origin = _url_origin(_public_api_base_url(request, settings))
    download_origin = _url_origin(download_url) if download_url is not None else None
    if (
        download_url is not None
        and download_origin is not None
        and download_origin != manager_origin
    ):
        return RedirectResponse(
            download_url,
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers=common_headers,
        )

    async def stream() -> AsyncIterator[bytes]:
        async for chunk in storage.stream_object(
            upload.canonical_object_key,
            chunk_size=settings.artifact_stream_chunk_bytes,
            max_bytes=artifact.size_bytes,
        ):
            yield chunk

    common_headers["Content-Length"] = str(artifact.size_bytes)
    return StreamingResponse(
        stream(),
        media_type=artifact.mime_type or "application/octet-stream",
        headers=common_headers,
    )


@router.get("/jobs/{job_id}/artifacts", response_model=ArtifactList)
async def list_job_artifacts(
    job_id: str,
    user: CurrentUserDep,
    session: SessionDep,
    response: Response,
    artifact_type: Annotated[ArtifactType | None, Query()] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ArtifactList:
    await require_job_owner_or_admin(session, job_id=job_id, user=user)
    filters = [Artifact.job_id == job_id]
    if artifact_type is not None:
        filters.append(Artifact.artifact_type == artifact_type.value)
    total = await session.scalar(select(func.count()).select_from(Artifact).where(*filters)) or 0
    artifacts = list(
        (
            await session.scalars(
                select(Artifact)
                .where(*filters)
                .order_by(Artifact.created_at.desc(), Artifact.id.asc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return ArtifactList(
        items=[artifact_to_read(artifact) for artifact in artifacts],
        total=total,
        offset=offset,
        limit=limit,
    )


def _sample_output_rate(config: JobConfig) -> int:
    resample_rate = config.auto_inference_samples.resample_sr
    if resample_rate:
        return resample_rate
    return 40_000 if config.model.sample_rate.value == "40k" else 48_000


async def _lock_current_sample_claim(
    session: SessionDep,
    *,
    job_id: str,
    attempt_id: str,
    lease_id: str,
    worker_id: str,
    test_set_id: str,
    sample_plan_sha256: str,
    expected_config: JobConfig,
    runtime_image_digest: str,
    runtime_asset_manifest_sha256: str,
) -> JobLease:
    """Fence the final ledger write after potentially long object verification."""

    lease = await session.scalar(
        select(JobLease)
        .where(JobLease.id == lease_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = await session.scalar(
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    attempt = await session.scalar(
        select(JobAttempt)
        .where(JobAttempt.id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    worker = await session.scalar(
        select(Worker)
        .where(Worker.id == worker_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = utc_now()
    terminal_values = {item.value for item in TERMINAL_JOB_STATUSES}
    if job is None or attempt is None:
        raise HTTPException(status_code=409, detail="sample claim changed during verification")
    try:
        locked_config = validated_job_config(job, attempt=attempt)
    except InvalidJobConfigLedger as exc:
        raise HTTPException(
            status_code=409, detail="Job snapshot changed during verification"
        ) from exc
    if (
        lease is None
        or worker is None
        or not lease.active
        or lease.released_at is not None
        or as_utc(lease.expires_at) <= now
        or lease.job_id != job_id
        or lease.attempt_id != attempt_id
        or lease.worker_id != worker_id
        or job.worker_id != worker_id
        or job.current_attempt_id != attempt_id
        or job.test_set_id != test_set_id
        or job.sample_plan_sha256 != sample_plan_sha256
        or job.cancel_requested_at is not None
        or job.status in terminal_values
        or attempt.job_id != job_id
        or attempt.worker_id != worker_id
        or attempt.finished_at is not None
        or attempt.status in terminal_values
        or attempt.runtime_image_digest != runtime_image_digest
        or attempt.runtime_asset_manifest_sha256 != runtime_asset_manifest_sha256
        or worker.current_job_id != job_id
        or locked_config != expected_config
    ):
        raise HTTPException(status_code=409, detail="sample claim changed during verification")
    return lease


def _require_fenced_lease_time(lease: JobLease) -> None:
    if not lease.active or lease.released_at is not None or as_utc(lease.expires_at) <= utc_now():
        raise HTTPException(status_code=409, detail="sample lease expired before commit")


async def _acquire_sample_verification_singleflight(
    session: SessionDep,
    artifact_id: str,
) -> None:
    """Fail fast when another Manager replica is scanning the same Artifact."""

    if session.get_bind().dialect.name != "postgresql":
        return
    value = uuid.UUID(artifact_id).int & ((1 << 63) - 1)
    # Reserve a namespace bit pattern so unrelated advisory-lock users cannot
    # accidentally serialize with Sample verification.
    key = value ^ 0x53414D504C450000
    acquired = await session.scalar(select(func.pg_try_advisory_xact_lock(key)))
    if acquired is not True:
        raise HTTPException(
            status_code=429,
            detail="sample Artifact verification is already in progress",
            headers={"Retry-After": "1"},
        )


async def _acquire_sample_attempt_registration_lock(
    session: SessionDep,
    attempt_id: str,
) -> None:
    """Serialize final Sample ledger decisions across Manager replicas."""

    if session.get_bind().dialect.name != "postgresql":
        return
    value = uuid.UUID(attempt_id).int & ((1 << 63) - 1)
    key = value ^ 0x53414D5041540000
    acquired = await session.scalar(select(func.pg_try_advisory_xact_lock(key)))
    if acquired is not True:
        raise HTTPException(
            status_code=429,
            detail="sample attempt registration is already in progress",
            headers={"Retry-After": "1"},
        )


async def _sample_attempt_totals(
    session: SessionDep,
    attempt_id: str,
) -> tuple[int, float]:
    rows = list(
        (
            await session.execute(
                select(Sample.output_size_bytes, Sample.output_duration_seconds).where(
                    Sample.attempt_id == attempt_id
                )
            )
        ).all()
    )
    if any(
        size_bytes <= 0 or not math.isfinite(duration) or duration <= 0
        for size_bytes, duration in rows
    ):
        raise HTTPException(status_code=409, detail="sample attempt ledger is invalid")
    return (
        sum(size_bytes for size_bytes, _ in rows),
        math.fsum(duration for _, duration in rows),
    )


@router.post(
    "/workers/jobs/{job_id}/samples",
    response_model=SampleRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_200_OK: {
            "model": SampleRead,
            "description": "Exact idempotent registration replay",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Claim, provenance, Artifact, or canonical bytes conflict",
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": "Sample metadata or output exceeds its hard limit",
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "description": "Rate, concurrency, or Artifact single-flight limit",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Storage, spool, or verification deadline unavailable",
        },
    },
)
async def register_sample(
    job_id: str,
    payload: SampleRegistrationRequest,
    request: Request,
    response: Response,
    worker: WorkerDep,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> SampleRead:
    """Bind one canonical PCM Artifact to an immutable TestSet sample plan."""

    actor_worker_id = worker.id
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if payload.output_size_bytes > settings.sample_max_bytes:
        raise HTTPException(status_code=413, detail="sample exceeds configured size limit")
    lease = await require_active_lease(
        session,
        worker_id=worker.id,
        job_id=job_id,
        lease_id=payload.lease_id,
    )
    if lease.attempt_id != payload.attempt_id:
        raise HTTPException(status_code=409, detail="sample attempt does not match lease")
    job = await session.get(Job, job_id)
    attempt = await session.get(JobAttempt, payload.attempt_id)
    if (
        job is None
        or attempt is None
        or attempt.job_id != job.id
        or attempt.worker_id != worker.id
        or attempt.engine_mode != WorkerEngineMode.RVC_WEBUI.value
        or job.worker_id != worker.id
        or job.current_attempt_id != payload.attempt_id
        or attempt.runtime_image_digest != payload.runtime_image_digest
        or attempt.runtime_asset_manifest_sha256 != payload.runtime_asset_manifest_sha256
    ):
        raise HTTPException(status_code=409, detail="job attempt is no longer current")
    try:
        config = validated_job_config(job, attempt=attempt)
    except InvalidJobConfigLedger as exc:
        raise HTTPException(
            status_code=409,
            detail="job configuration integrity check failed",
        ) from exc
    try:
        capabilities = WorkerCapabilities.model_validate(worker.capabilities_json)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail="runtime snapshot is invalid") from exc
    sample_config = config.auto_inference_samples
    if (
        not sample_config.enabled
        or not capabilities.fixed_test_set_inference_ready
        or capabilities.engine_mode is not WorkerEngineMode.RVC_WEBUI
        or not capabilities.rvc_assets_ready
        or sample_config.inference_f0_method not in capabilities.supported_inference_f0_methods
    ):
        raise HTTPException(status_code=409, detail="sample runtime is not ready")
    expected_commit = config.rvc_backend.rvc_commit_hash or RVC_REVIEWED_COMMIT
    runtime_bundle = (
        payload.runtime_image_digest,
        payload.runtime_asset_manifest_sha256,
    )
    if (
        expected_commit != RVC_REVIEWED_COMMIT
        or payload.rvc_commit_hash != expected_commit
        or worker.rvc_commit_hash != expected_commit
        or capabilities.rvc_commit_hash != expected_commit
        or runtime_bundle not in settings.approved_sample_runtime_bundles
        or capabilities.runtime_image_digest != payload.runtime_image_digest
        or capabilities.runtime_asset_manifest_sha256 != payload.runtime_asset_manifest_sha256
    ):
        raise HTTPException(status_code=409, detail="RVC runtime commit does not match")

    transfer = await verified_test_set_transfer(
        session,
        job,
        config,
        storage=storage,
        settings=settings,
    )
    if transfer is None:
        raise HTTPException(status_code=409, detail="sample plan is no longer verifiable")
    descriptor = next(
        (item for item in transfer.items if item.test_set_item_id == payload.test_set_item_id),
        None,
    )
    if (
        payload.test_set_id != transfer.test_set_id
        or payload.sample_plan_sha256 != transfer.sample_plan_sha256
        or payload.inference_config_sha256 != transfer.inference_config_sha256
        or payload.inference_f0_method is not transfer.inference_config.inference_f0_method
    ):
        raise HTTPException(status_code=409, detail="sample provenance does not match Job snapshot")
    if descriptor is None:
        raise HTTPException(status_code=409, detail="sample input is not in the Job TestSet")
    if payload.input_sha256 != descriptor.sha256:
        raise HTTPException(status_code=409, detail="sample input checksum does not match")
    if transfer.inference_config.index_rate > 0 and payload.index_sha256 is None:
        raise HTTPException(status_code=409, detail="sample retrieval index is required")
    if transfer.inference_config.index_rate == 0 and payload.index_sha256 is not None:
        raise HTTPException(status_code=409, detail="no-index sample must not declare an index")

    try:
        model_binding = await verified_artifact_by_hash(
            session,
            storage,
            job_id=job.id,
            attempt_id=payload.attempt_id,
            artifact_type=ArtifactType.FINAL_SMALL_MODEL,
            sha256=payload.model_sha256,
            lease_id=payload.lease_id,
            worker_id=worker.id,
        )
        index_binding = (
            await verified_artifact_by_hash(
                session,
                storage,
                job_id=job.id,
                attempt_id=payload.attempt_id,
                artifact_type=ArtifactType.FINAL_INDEX,
                sha256=payload.index_sha256 or "",
                lease_id=payload.lease_id,
                worker_id=worker.id,
            )
            if payload.index_sha256 is not None
            else None
        )
        output_binding = await verified_artifact_binding(
            session,
            storage,
            artifact_id=payload.artifact_id,
            job_id=job.id,
            attempt_id=payload.attempt_id,
            artifact_type=ArtifactType.SAMPLE,
            sha256=payload.output_sha256,
            lease_id=payload.lease_id,
            worker_id=worker.id,
        )
    except SampleStorageUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="sample storage namespace is unavailable",
        ) from exc
    if model_binding is None:
        raise HTTPException(status_code=409, detail="verified final small model is missing")
    if payload.index_sha256 is not None and index_binding is None:
        raise HTTPException(status_code=409, detail="verified final index is missing")
    if output_binding is None:
        raise HTTPException(status_code=409, detail="verified sample Artifact is missing")
    provenance_bindings = (
        (model_binding, "sample_model"),
        (output_binding, "sample_output"),
        (index_binding, "sample_index"),
    )
    for binding, role in provenance_bindings:
        if binding is not None and not artifact_provenance_matches(
            binding,
            rvc_commit_hash=payload.rvc_commit_hash,
            runtime_image_digest=payload.runtime_image_digest,
            runtime_asset_manifest_sha256=payload.runtime_asset_manifest_sha256,
            native_inference_manifest_sha256=payload.native_inference_manifest_sha256,
            native_inference_request_sha256=payload.native_inference_request_sha256,
            native_sample_role=role,
        ):
            raise HTTPException(
                status_code=409,
                detail="Artifact runtime provenance does not match",
            )
    if (
        output_binding.artifact.mime_type != "audio/wav"
        or output_binding.upload.content_type != "audio/wav"
        or output_binding.artifact.size_bytes != payload.output_size_bytes
        or output_binding.upload.expected_size_bytes != payload.output_size_bytes
    ):
        raise HTTPException(status_code=409, detail="sample Artifact metadata does not match")

    await _acquire_sample_verification_singleflight(session, payload.artifact_id)
    verification_semaphore = cast(
        asyncio.Semaphore,
        request.app.state.sample_verification_semaphore,
    )
    try:
        await asyncio.wait_for(verification_semaphore.acquire(), timeout=0.01)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=429,
            detail="sample verification concurrency limit reached",
            headers={"Retry-After": "1"},
        ) from exc
    spool_path: Path | None = None
    verification_deadline = time.monotonic() + settings.sample_verification_timeout_seconds
    try:
        async with asyncio.timeout(settings.sample_verification_timeout_seconds):
            spool_path = await verify_object_to_spool(
                storage,
                output_binding.upload.canonical_object_key,
                expected_size=payload.output_size_bytes,
                expected_sha256=payload.output_sha256,
                settings=settings,
            )
        inspection = await _inspect_sample_pcm_wav_joined(
            spool_path,
            settings,
            deadline_monotonic=verification_deadline,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503,
            detail="sample verification deadline exceeded",
        ) from exc
    except (ArtifactVerificationMismatch, ObjectTooLarge) as exc:
        raise HTTPException(status_code=409, detail="sample canonical bytes do not match") from exc
    except ObjectNotFound as exc:
        raise HTTPException(status_code=409, detail="sample canonical object is missing") from exc
    except InvalidSampleWav as exc:
        if exc.failure_code == "verification_timeout":
            raise HTTPException(
                status_code=503,
                detail="sample verification deadline exceeded",
            ) from exc
        raise HTTPException(
            status_code=422,
            detail="sample output is not a supported PCM WAV",
        ) from exc
    except ArtifactSpoolError as exc:
        raise HTTPException(
            status_code=503,
            detail="sample verification spool is unavailable",
        ) from exc
    except StorageError as exc:
        raise HTTPException(
            status_code=503,
            detail="sample canonical object cannot be read",
        ) from exc
    finally:
        try:
            if spool_path is not None:
                await remove_spool_file(spool_path)
        except ArtifactSpoolError as exc:
            raise HTTPException(
                status_code=503,
                detail="sample verification spool cleanup failed",
            ) from exc
        finally:
            verification_semaphore.release()

    expected_rate = _sample_output_rate(config)
    duration_tolerance = max(1 / expected_rate, 1e-6)
    if (
        inspection.sample_rate_hz != expected_rate
        or payload.output_sample_rate_hz != inspection.sample_rate_hz
        or payload.output_channels != inspection.channels
        or not abs(payload.output_duration_seconds - inspection.duration_seconds)
        <= duration_tolerance
    ):
        raise HTTPException(status_code=422, detail="sample PCM metadata does not match")
    if not sample_metrics_match(payload.metrics, inspection.metrics):
        raise HTTPException(status_code=422, detail="sample PCM metrics do not match")
    evidence = sample_metrics_evidence(payload, inspection)
    await _acquire_sample_attempt_registration_lock(session, payload.attempt_id)
    fenced_lease = await _lock_current_sample_claim(
        session,
        job_id=job_id,
        attempt_id=payload.attempt_id,
        lease_id=payload.lease_id,
        worker_id=actor_worker_id,
        test_set_id=payload.test_set_id,
        sample_plan_sha256=payload.sample_plan_sha256,
        expected_config=config,
        runtime_image_digest=payload.runtime_image_digest,
        runtime_asset_manifest_sha256=payload.runtime_asset_manifest_sha256,
    )
    try:
        await acquire_sample_registration_job_fence(
            session,
            job_id=job_id,
            attempt_id=payload.attempt_id,
            worker_id=actor_worker_id,
            test_set_id=payload.test_set_id,
            sample_plan_sha256=payload.sample_plan_sha256,
            expected_config=config,
            runtime_image_digest=payload.runtime_image_digest,
            runtime_asset_manifest_sha256=payload.runtime_asset_manifest_sha256,
        )
    except SampleRegistrationFenceConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="sample claim changed during verification",
        ) from exc
    except SampleRegistrationFenceUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="sample registration fence is temporarily unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    fenced_lease = await _lock_current_sample_claim(
        session,
        job_id=job_id,
        attempt_id=payload.attempt_id,
        lease_id=payload.lease_id,
        worker_id=actor_worker_id,
        test_set_id=payload.test_set_id,
        sample_plan_sha256=payload.sample_plan_sha256,
        expected_config=config,
        runtime_image_digest=payload.runtime_image_digest,
        runtime_asset_manifest_sha256=payload.runtime_asset_manifest_sha256,
    )

    existing = await session.scalar(
        select(Sample).where(
            Sample.attempt_id == payload.attempt_id,
            Sample.test_set_item_id == payload.test_set_item_id,
            Sample.inference_config_sha256 == payload.inference_config_sha256,
        )
    )
    if existing is not None:
        if existing.job_id != job.id or not sample_matches_registration(
            existing, payload, evidence
        ):
            raise HTTPException(
                status_code=409,
                detail="sample identity conflicts with prior payload",
            )
        add_audit_event(
            session,
            actor_type="worker",
            actor_id=actor_worker_id,
            action="sample.registration_replayed",
            resource_type="sample",
            resource_id=existing.id,
            details={"job_id": job.id, "attempt_id": payload.attempt_id},
        )
        _require_fenced_lease_time(fenced_lease)
        await session.commit()
        response.status_code = status.HTTP_200_OK
        return sample_to_read(existing)
    total_size_bytes, total_duration_seconds = await _sample_attempt_totals(
        session,
        payload.attempt_id,
    )
    if (
        total_size_bytes + payload.output_size_bytes > SAMPLE_MAX_TOTAL_OUTPUT_BYTES
        or math.fsum((total_duration_seconds, inspection.duration_seconds))
        > SAMPLE_MAX_TOTAL_OUTPUT_DURATION_SECONDS
    ):
        raise HTTPException(
            status_code=413,
            detail="sample attempt exceeds total output limits",
        )
    sample = Sample(
        job_id=job.id,
        attempt_id=payload.attempt_id,
        test_set_id=payload.test_set_id,
        test_set_item_id=payload.test_set_item_id,
        artifact_id=payload.artifact_id,
        input_sha256=payload.input_sha256,
        model_sha256=payload.model_sha256,
        index_sha256=payload.index_sha256,
        inference_f0_method=payload.inference_f0_method.value,
        inference_config_sha256=payload.inference_config_sha256,
        native_inference_manifest_sha256=payload.native_inference_manifest_sha256,
        native_inference_request_sha256=payload.native_inference_request_sha256,
        output_size_bytes=payload.output_size_bytes,
        output_sha256=payload.output_sha256,
        output_sample_rate_hz=inspection.sample_rate_hz,
        output_channels=inspection.channels,
        output_duration_seconds=inspection.duration_seconds,
        metrics_json=evidence.model_dump(mode="json"),
        rvc_commit_hash=payload.rvc_commit_hash,
        runtime_image_digest=payload.runtime_image_digest,
        runtime_asset_manifest_sha256=payload.runtime_asset_manifest_sha256,
    )
    session.add(sample)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raced = await session.scalar(
            select(Sample).where(
                Sample.attempt_id == payload.attempt_id,
                Sample.test_set_item_id == payload.test_set_item_id,
                Sample.inference_config_sha256 == payload.inference_config_sha256,
            )
        )
        if (
            raced is None
            or raced.job_id != job_id
            or not sample_matches_registration(raced, payload, evidence)
        ):
            raise HTTPException(status_code=409, detail="sample registration conflict") from exc
        # The rollback released the row locks. Re-acquire the complete claim fence
        # before treating an insert race as an idempotent success.
        fenced_lease = await _lock_current_sample_claim(
            session,
            job_id=job_id,
            attempt_id=payload.attempt_id,
            lease_id=payload.lease_id,
            worker_id=actor_worker_id,
            test_set_id=payload.test_set_id,
            sample_plan_sha256=payload.sample_plan_sha256,
            expected_config=config,
            runtime_image_digest=payload.runtime_image_digest,
            runtime_asset_manifest_sha256=payload.runtime_asset_manifest_sha256,
        )
        try:
            await acquire_sample_registration_job_fence(
                session,
                job_id=job_id,
                attempt_id=payload.attempt_id,
                worker_id=actor_worker_id,
                test_set_id=payload.test_set_id,
                sample_plan_sha256=payload.sample_plan_sha256,
                expected_config=config,
                runtime_image_digest=payload.runtime_image_digest,
                runtime_asset_manifest_sha256=payload.runtime_asset_manifest_sha256,
            )
        except SampleRegistrationFenceConflict as fence_exc:
            raise HTTPException(
                status_code=409,
                detail="sample claim changed during verification",
            ) from fence_exc
        except SampleRegistrationFenceUnavailable as fence_exc:
            raise HTTPException(
                status_code=503,
                detail="sample registration fence is temporarily unavailable",
                headers={"Retry-After": "1"},
            ) from fence_exc
        fenced_lease = await _lock_current_sample_claim(
            session,
            job_id=job_id,
            attempt_id=payload.attempt_id,
            lease_id=payload.lease_id,
            worker_id=actor_worker_id,
            test_set_id=payload.test_set_id,
            sample_plan_sha256=payload.sample_plan_sha256,
            expected_config=config,
            runtime_image_digest=payload.runtime_image_digest,
            runtime_asset_manifest_sha256=payload.runtime_asset_manifest_sha256,
        )
        add_audit_event(
            session,
            actor_type="worker",
            actor_id=actor_worker_id,
            action="sample.registration_replayed",
            resource_type="sample",
            resource_id=raced.id,
            details={"job_id": job_id, "attempt_id": payload.attempt_id},
        )
        _require_fenced_lease_time(fenced_lease)
        await session.commit()
        response.status_code = status.HTTP_200_OK
        return sample_to_read(raced)
    add_audit_event(
        session,
        actor_type="worker",
        actor_id=actor_worker_id,
        action="sample.registered",
        resource_type="sample",
        resource_id=sample.id,
        details={
            "job_id": job.id,
            "attempt_id": payload.attempt_id,
            "test_set_item_id": payload.test_set_item_id,
        },
    )
    _require_fenced_lease_time(fenced_lease)
    await session.commit()
    await session.refresh(sample)
    return sample_to_read(sample)


@router.get("/jobs/{job_id}/samples", response_model=SampleList)
async def list_job_samples(
    job_id: str,
    user: CurrentUserDep,
    session: SessionDep,
    response: Response,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    attempt_id: Annotated[str | None, Query(min_length=36, max_length=36)] = None,
    include_history: bool = False,
) -> SampleList:
    job = await require_job_owner_or_admin(session, job_id=job_id, user=user)
    if attempt_id is not None:
        belongs = await session.scalar(
            select(JobAttempt.id).where(
                JobAttempt.id == attempt_id,
                JobAttempt.job_id == job_id,
            )
        )
        if belongs is None:
            raise HTTPException(status_code=404, detail="job attempt not found")
        selected_attempt_id = attempt_id
    elif include_history:
        selected_attempt_id = None
    else:
        selected_attempt_id = job.current_attempt_id
    filters = [Sample.job_id == job_id]
    if selected_attempt_id is None and not include_history:
        filters.append(Sample.id.is_(None))
    elif selected_attempt_id is not None:
        filters.append(Sample.attempt_id == selected_attempt_id)
    total = await session.scalar(select(func.count()).select_from(Sample).where(*filters)) or 0
    samples = list(
        (
            await session.scalars(
                select(Sample)
                .where(*filters)
                .order_by(Sample.created_at.asc(), Sample.id.asc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    try:
        items = [sample_to_read(sample) for sample in samples]
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=409, detail="sample ledger is invalid") from exc
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return SampleList(items=items, total=total, offset=offset, limit=limit)


@router.get(
    "/samples/{sample_id}/download",
    responses={
        status.HTTP_206_PARTIAL_CONTENT: {
            "description": "Verified WAV byte range",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Sample ledger or current canonical bytes conflict",
        },
        status.HTTP_416_RANGE_NOT_SATISFIABLE: {
            "description": "Requested WAV byte range is outside the verified object",
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "description": "Sample verification single-flight or concurrency limit",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Storage, spool, or verification deadline unavailable",
        },
    },
)
async def download_sample(
    sample_id: str,
    request: Request,
    user: CurrentUserDep,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> Response:
    sample = await session.get(Sample, sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail="sample not found")
    try:
        await require_job_owner_or_admin(session, job_id=sample.job_id, user=user)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="sample not found") from exc
        raise
    try:
        binding = await verified_artifact_binding(
            session,
            storage,
            artifact_id=sample.artifact_id,
            job_id=sample.job_id,
            attempt_id=sample.attempt_id,
            artifact_type=ArtifactType.SAMPLE,
            sha256=sample.output_sha256,
        )
    except SampleStorageUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="sample storage namespace is unavailable",
        ) from exc
    if (
        binding is None
        or binding.artifact.mime_type != "audio/wav"
        or binding.artifact.size_bytes != sample.output_size_bytes
    ):
        raise HTTPException(status_code=409, detail="sample Artifact is not verified")
    await _acquire_sample_verification_singleflight(session, sample.artifact_id)
    verification_semaphore = cast(
        asyncio.Semaphore,
        request.app.state.sample_verification_semaphore,
    )
    try:
        await asyncio.wait_for(verification_semaphore.acquire(), timeout=0.01)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=429,
            detail="sample verification concurrency limit reached",
            headers={"Retry-After": "1"},
        ) from exc
    spool_path: Path | None = None
    slot_handed_off = False
    try:
        filename = safe_download_filename(binding.artifact.filename, sample.id)
        disposition = attachment_content_disposition(filename)
        try:
            async with asyncio.timeout(settings.sample_verification_timeout_seconds):
                spool_path = await verify_object_to_spool(
                    storage,
                    binding.upload.canonical_object_key,
                    expected_size=sample.output_size_bytes,
                    expected_sha256=sample.output_sha256,
                    settings=settings,
                )
        except TimeoutError as exc:
            raise HTTPException(
                status_code=503,
                detail="sample download verification timed out",
            ) from exc
        except (ArtifactVerificationMismatch, ObjectTooLarge, ObjectNotFound) as exc:
            raise HTTPException(
                status_code=409,
                detail="sample canonical bytes do not match",
            ) from exc
        except (ArtifactSpoolError, StorageError) as exc:
            raise HTTPException(
                status_code=503,
                detail="sample download verification failed",
            ) from exc

        add_audit_event(
            session,
            actor_type="user",
            actor_id=user.id,
            action="sample.download_requested",
            resource_type="sample",
            resource_id=sample.id,
            details={"job_id": sample.job_id},
        )
        await session.commit()
        headers = {
            "Cache-Control": "private, no-store",
            "Content-Disposition": disposition,
            # The verified content digest is stable across per-request spool files.
            # FileResponse otherwise derives an ETag from the temporary inode's
            # mtime and size, which makes browser If-Range seeks fall back to a full
            # 200 response on every request.
            "ETag": f'"{sample.output_sha256}"',
            "Vary": "Authorization",
            "X-Content-Type-Options": "nosniff",
        }
        assert spool_path is not None
        response = _VerifiedSampleFileResponse(
            path=spool_path,
            media_type="audio/wav",
            headers=headers,
            verification_semaphore=verification_semaphore,
        )
        slot_handed_off = True
        return response
    finally:
        if not slot_handed_off:
            try:
                if spool_path is not None:
                    await remove_spool_file(spool_path)
            finally:
                verification_semaphore.release()
