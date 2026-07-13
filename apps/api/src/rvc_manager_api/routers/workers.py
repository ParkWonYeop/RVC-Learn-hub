from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated, cast
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.exc import StaleDataError

from rvc_orchestrator_contracts import (
    TERMINAL_JOB_STATUSES,
    ArtifactBatch,
    ArtifactType,
    InvalidJobTransition,
    JobClaim,
    JobClaimRequest,
    JobStatus,
    JobStatusUpdate,
    LeaseRenewRequest,
    LeaseRenewResponse,
    LogBatch,
    LogEntry,
    MetricBatch,
    MetricEntry,
    WorkerCapabilities,
    WorkerHeartbeatRequest,
    WorkerHeartbeatResponse,
    WorkerReEnrollRequest,
    WorkerRegisterRequest,
    WorkerRegisterResponse,
    WorkerSessionResponse,
    WorkerStatus,
    WorkerTokenRotationActivated,
    WorkerTokenRotationPrepareResponse,
    WorkerTokenRotationRequest,
    WorkerTokenRotationStatus,
    utc_now,
    validate_job_transition,
)

from ..audit import add_audit_event
from ..dependencies import AdminUserDep, MlflowDep, SessionDep, SettingsDep, WorkerDep
from ..models import (
    Artifact,
    ArtifactUploadSession,
    Dataset,
    DatasetUploadSession,
    IngestBatch,
    Job,
    JobAttempt,
    JobLease,
    JobLog,
    JobStatusEvent,
    Metric,
    TestSetItem,
    TestSetItemUploadSession,
    Worker,
)
from ..schemas import (
    OperationAck,
    StatusAck,
    WorkerList,
    WorkerRead,
    WorkerTokenRevokeRequest,
)
from ..security import hash_worker_token, issue_worker_token, verify_bootstrap_token
from ..services.artifacts import attachment_content_disposition
from ..services.datasets import dataset_ready_for_training
from ..services.job_configs import InvalidJobConfigLedger, validated_job_config
from ..services.job_observability import redact_log_fields, redact_log_text
from ..services.mlflow import artifact_event_key, metric_event_key
from ..services.samples import SampleCompletionUnavailable, sample_completion_ready
from ..services.test_sets import test_set_item_object_key
from ..services.workers import (
    as_utc,
    claim_job,
    lease_expiry,
    recover_expired_leases,
    require_active_lease,
    verified_test_set_transfer,
)
from ..storage import StorageAdapter, StorageError, storage_namespace_matches

router = APIRouter(prefix="/workers", tags=["workers"])


def get_storage(request: Request) -> StorageAdapter:
    return cast(StorageAdapter, request.app.state.storage)


StorageDep = Annotated[StorageAdapter, Depends(get_storage)]


def _validate_current_epoch(job: Job, current_epoch: int | None) -> None:
    if current_epoch is None:
        return
    if current_epoch > job.total_epoch:
        raise HTTPException(status_code=422, detail="current_epoch exceeds total_epoch")
    if job.current_epoch is not None and current_epoch < job.current_epoch:
        raise HTTPException(status_code=409, detail="current_epoch cannot move backwards")


@router.post(
    "/register",
    response_model=WorkerRegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_worker(
    payload: WorkerRegisterRequest,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    bootstrap_token: Annotated[str | None, Header(alias="X-Worker-Bootstrap-Token")] = None,
) -> WorkerRegisterResponse:
    if settings.worker_bootstrap_token is None:
        raise HTTPException(status_code=503, detail="worker registration is disabled")
    if not verify_bootstrap_token(bootstrap_token, settings):
        raise HTTPException(status_code=401, detail="invalid worker bootstrap token")

    raw_token = issue_worker_token()
    now = utc_now()
    worker = Worker(
        name=payload.name,
        token_hash=hash_worker_token(raw_token, settings),
        status="idle",
        capabilities_json=payload.capabilities.model_dump(mode="json"),
        worker_version=payload.capabilities.worker_version,
        rvc_commit_hash=payload.capabilities.rvc_commit_hash,
        token_issued_at=now,
        last_heartbeat_at=now,
    )
    session.add(worker)
    try:
        await session.commit()
    except (IntegrityError, StaleDataError) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="worker name already registered") from exc
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    return WorkerRegisterResponse(
        worker_id=worker.id,
        worker_token=raw_token,
        issued_at=now,
    )


@router.get("/me", response_model=WorkerSessionResponse)
async def worker_session(worker: WorkerDep) -> WorkerSessionResponse:
    return WorkerSessionResponse(
        worker_id=worker.id,
        name=worker.name,
        status=WorkerStatus(worker.status),
        current_job_id=worker.current_job_id,
        last_heartbeat_at=worker.last_heartbeat_at,
    )


def _token_rotation_status(worker: Worker) -> WorkerTokenRotationStatus:
    pending = worker.pending_token_hash is not None
    return WorkerTokenRotationStatus(
        worker_id=worker.id,
        token_issued_at=worker.token_issued_at,
        pending=pending,
        rotation_id=worker.token_rotation_id if pending else None,
        started_at=worker.token_rotation_started_at if pending else None,
        expires_at=worker.token_rotation_expires_at if pending else None,
    )


def _clear_token_rotation(worker: Worker) -> None:
    worker.token_rotation_id = None
    worker.pending_token_hash = None
    worker.token_rotation_started_at = None
    worker.token_rotation_expires_at = None


async def _lock_worker_token_rotation_boundary(
    session: SessionDep,
    *,
    worker_id: str,
    allow_inactive: bool = False,
) -> tuple[Worker, list[JobLease]]:
    """Lock active leases before the Worker row to preserve the global lock order."""

    leases = list(
        (
            await session.scalars(
                select(JobLease)
                .where(
                    JobLease.worker_id == worker_id,
                    JobLease.active.is_(True),
                    JobLease.released_at.is_(None),
                )
                .order_by(JobLease.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    locked_worker = await session.scalar(
        select(Worker)
        .where(Worker.id == worker_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_worker is None or (not allow_inactive and not locked_worker.is_active):
        raise HTTPException(status_code=401, detail="invalid worker token")
    return locked_worker, leases


def _require_rotation_idle(worker: Worker, leases: list[JobLease]) -> None:
    if worker.current_job_id is not None or leases:
        raise HTTPException(
            status_code=409,
            detail="worker token rotation requires an idle Worker with no active lease",
        )


@router.get("/token-rotation", response_model=WorkerTokenRotationStatus)
async def token_rotation_status(worker: WorkerDep) -> WorkerTokenRotationStatus:
    return _token_rotation_status(worker)


@router.post(
    "/token-rotation/prepare",
    response_model=WorkerTokenRotationPrepareResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "Worker is busy or another one-time rotation is pending"
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rotation rate limit exceeded"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Distributed rate limiter unavailable"
        },
    },
)
async def prepare_token_rotation(
    payload: WorkerTokenRotationRequest,
    response: Response,
    worker: WorkerDep,
    session: SessionDep,
    settings: SettingsDep,
) -> WorkerTokenRotationPrepareResponse:
    authenticated_token_hash = worker.token_hash
    worker, leases = await _lock_worker_token_rotation_boundary(
        session,
        worker_id=worker.id,
    )
    if not hmac.compare_digest(worker.token_hash, authenticated_token_hash):
        raise HTTPException(status_code=401, detail="worker token changed during rotation")
    _require_rotation_idle(worker, leases)
    now = utc_now()
    if worker.pending_token_hash is not None:
        if (
            worker.token_rotation_expires_at is not None
            and as_utc(worker.token_rotation_expires_at) <= now
        ):
            add_audit_event(
                session,
                actor_type="worker",
                actor_id=worker.id,
                action="worker.token_rotation_expired",
                resource_type="worker",
                resource_id=worker.id,
                details={"rotation_id": worker.token_rotation_id},
            )
            _clear_token_rotation(worker)
        else:
            raise HTTPException(
                status_code=409,
                detail="a one-time Worker token rotation is already pending",
            )

    raw_token = issue_worker_token()
    expires_at = now + timedelta(seconds=settings.worker_token_rotation_ttl_seconds)
    worker.token_rotation_id = payload.rotation_id
    worker.pending_token_hash = hash_worker_token(raw_token, settings)
    worker.token_rotation_started_at = now
    worker.token_rotation_expires_at = expires_at
    add_audit_event(
        session,
        actor_type="worker",
        actor_id=worker.id,
        action="worker.token_rotation_prepared",
        resource_type="worker",
        resource_id=worker.id,
        details={"rotation_id": payload.rotation_id, "expires_at": expires_at.isoformat()},
    )
    try:
        await session.commit()
    except (IntegrityError, StaleDataError) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Worker token rotation conflicted") from exc
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    return WorkerTokenRotationPrepareResponse(
        worker_id=worker.id,
        rotation_id=payload.rotation_id,
        worker_token=raw_token,
        expires_at=expires_at,
    )


@router.post(
    "/token-rotation/abort",
    response_model=WorkerTokenRotationStatus,
    responses={
        status.HTTP_409_CONFLICT: {"description": "A different rotation is pending"},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rotation rate limit exceeded"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Distributed rate limiter unavailable"
        },
    },
)
async def abort_token_rotation(
    payload: WorkerTokenRotationRequest,
    response: Response,
    worker: WorkerDep,
    session: SessionDep,
) -> WorkerTokenRotationStatus:
    authenticated_token_hash = worker.token_hash
    worker, _leases = await _lock_worker_token_rotation_boundary(
        session,
        worker_id=worker.id,
    )
    if not hmac.compare_digest(worker.token_hash, authenticated_token_hash):
        raise HTTPException(status_code=401, detail="worker token changed during rotation")
    if worker.pending_token_hash is None:
        response.headers["Cache-Control"] = "private, no-store"
        return _token_rotation_status(worker)
    if worker.token_rotation_id != payload.rotation_id:
        raise HTTPException(status_code=409, detail="a different Worker token rotation is pending")
    _clear_token_rotation(worker)
    add_audit_event(
        session,
        actor_type="worker",
        actor_id=worker.id,
        action="worker.token_rotation_aborted",
        resource_type="worker",
        resource_id=worker.id,
        details={"rotation_id": payload.rotation_id},
    )
    try:
        await session.commit()
    except StaleDataError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Worker token rotation changed before abort commit",
        ) from exc
    response.headers["Cache-Control"] = "private, no-store"
    return _token_rotation_status(worker)


@router.post(
    "/token-rotation/activate",
    response_model=WorkerTokenRotationActivated,
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "Rotation expired, changed, or Worker became busy"
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rotation rate limit exceeded"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Distributed rate limiter unavailable"
        },
    },
)
async def activate_token_rotation(
    payload: WorkerTokenRotationRequest,
    response: Response,
    worker: WorkerDep,
    session: SessionDep,
    settings: SettingsDep,
    pending_worker_token: Annotated[
        str | None,
        Header(alias="X-RVC-Pending-Worker-Token", min_length=37, max_length=133),
    ] = None,
) -> WorkerTokenRotationActivated:
    if pending_worker_token is None:
        raise HTTPException(status_code=401, detail="pending Worker token required")
    authenticated_token_hash = worker.token_hash
    worker, leases = await _lock_worker_token_rotation_boundary(
        session,
        worker_id=worker.id,
    )
    if not hmac.compare_digest(worker.token_hash, authenticated_token_hash):
        raise HTTPException(status_code=401, detail="worker token changed during rotation")
    _require_rotation_idle(worker, leases)
    now = utc_now()
    pending_hash = hash_worker_token(pending_worker_token, settings)
    if (
        worker.token_rotation_id != payload.rotation_id
        or worker.pending_token_hash is None
        or worker.token_rotation_expires_at is None
        or as_utc(worker.token_rotation_expires_at) <= now
        or not hmac.compare_digest(worker.pending_token_hash, pending_hash)
    ):
        raise HTTPException(status_code=409, detail="pending Worker token rotation is invalid")
    worker.token_hash = worker.pending_token_hash
    worker.token_issued_at = now
    _clear_token_rotation(worker)
    add_audit_event(
        session,
        actor_type="worker",
        actor_id=worker.id,
        action="worker.token_rotated",
        resource_type="worker",
        resource_id=worker.id,
        details={"rotation_id": payload.rotation_id, "old_token_revoked": True},
    )
    try:
        await session.commit()
    except StaleDataError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Worker token rotation changed before activation commit",
        ) from exc
    response.headers["Cache-Control"] = "private, no-store"
    return WorkerTokenRotationActivated(
        worker_id=worker.id,
        rotation_id=payload.rotation_id,
        token_issued_at=now,
    )


@router.post(
    "/re-enroll",
    response_model=WorkerRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "Worker is active, assigned, or has an active lease"
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Registration rate limit exceeded"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Registration disabled or rate limiter unavailable"
        },
    },
)
async def re_enroll_worker(
    payload: WorkerReEnrollRequest,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    bootstrap_token: Annotated[str | None, Header(alias="X-Worker-Bootstrap-Token")] = None,
) -> WorkerRegisterResponse:
    if settings.worker_bootstrap_token is None:
        raise HTTPException(status_code=503, detail="worker re-enrollment is disabled")
    if not verify_bootstrap_token(bootstrap_token, settings):
        raise HTTPException(status_code=401, detail="invalid worker bootstrap token")
    worker, leases = await _lock_worker_token_rotation_boundary(
        session,
        worker_id=payload.worker_id,
        allow_inactive=True,
    )
    if worker.name != payload.name:
        raise HTTPException(status_code=409, detail="Worker re-enrollment identity does not match")
    if worker.is_active or worker.current_job_id is not None or leases:
        raise HTTPException(
            status_code=409,
            detail="only an inactive unassigned Worker can be re-enrolled",
        )
    raw_token = issue_worker_token()
    now = utc_now()
    worker.token_hash = hash_worker_token(raw_token, settings)
    worker.token_issued_at = now
    worker.status = WorkerStatus.IDLE.value
    worker.capabilities_json = payload.capabilities.model_dump(mode="json")
    worker.worker_version = payload.capabilities.worker_version
    worker.rvc_commit_hash = payload.capabilities.rvc_commit_hash
    worker.last_heartbeat_at = None
    worker.is_active = True
    _clear_token_rotation(worker)
    add_audit_event(
        session,
        actor_type="system",
        action="worker.re_enrolled",
        resource_type="worker",
        resource_id=worker.id,
        details={"authentication": "bootstrap", "worker_name": worker.name},
    )
    try:
        await session.commit()
    except (IntegrityError, StaleDataError) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Worker re-enrollment conflicted") from exc
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    return WorkerRegisterResponse(
        worker_id=worker.id,
        worker_token=raw_token,
        issued_at=now,
    )


async def _lock_worker_revocation_boundary(
    session: SessionDep,
    worker_id: str,
) -> tuple[Worker, list[JobLease], dict[str, Job], dict[str, JobAttempt]]:
    leases = list(
        (
            await session.scalars(
                select(JobLease)
                .where(
                    JobLease.worker_id == worker_id,
                    JobLease.active.is_(True),
                    JobLease.released_at.is_(None),
                )
                .order_by(JobLease.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    job_ids = sorted({lease.job_id for lease in leases})
    attempt_ids = sorted({lease.attempt_id for lease in leases})
    jobs = {
        job.id: job
        for job in (
            (
                await session.scalars(
                    select(Job)
                    .where(Job.id.in_(job_ids))
                    .order_by(Job.id.asc())
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
            if job_ids
            else []
        )
    }
    attempts = {
        attempt.id: attempt
        for attempt in (
            (
                await session.scalars(
                    select(JobAttempt)
                    .where(JobAttempt.id.in_(attempt_ids))
                    .order_by(JobAttempt.id.asc())
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
            if attempt_ids
            else []
        )
    }
    worker = await session.scalar(
        select(Worker)
        .where(Worker.id == worker_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if worker is None:
        raise HTTPException(status_code=404, detail="worker not found")
    return worker, leases, jobs, attempts


@router.post(
    "/{worker_id}/token/revoke",
    response_model=WorkerRead,
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "Identity mismatch or active assignment requires explicit force"
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Revocation rate limit exceeded"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Distributed rate limiter unavailable"
        },
    },
)
async def revoke_worker_token(
    worker_id: str,
    payload: WorkerTokenRevokeRequest,
    session: SessionDep,
    settings: SettingsDep,
    admin: AdminUserDep,
    mlflow: MlflowDep,
) -> WorkerRead:
    stored_admin_id = session.info.get("worker_token_revoke_admin_id")
    if isinstance(stored_admin_id, str):
        admin_actor_id = stored_admin_id
    else:
        admin_actor_id = admin.id
        session.info["worker_token_revoke_admin_id"] = admin_actor_id
    worker, leases, jobs, attempts = await _lock_worker_revocation_boundary(
        session,
        worker_id,
    )
    if worker.name != payload.expected_worker_name:
        raise HTTPException(status_code=409, detail="Worker revocation identity does not match")
    has_assignment = worker.current_job_id is not None or bool(leases)
    if has_assignment and not payload.force_cancel_active:
        raise HTTPException(
            status_code=409,
            detail="active Worker revocation requires explicit force_cancel_active",
        )
    lease_job_ids = {lease.job_id for lease in leases}
    if (
        bool(worker.current_job_id) != bool(leases)
        or len(leases) > 1
        or (worker.current_job_id is not None and worker.current_job_id not in lease_job_ids)
    ):
        raise HTTPException(
            status_code=409,
            detail="Worker assignment ledger is inconsistent; revoke was not applied",
        )

    now = utc_now()
    mlflow_event_keys: list[str | None] = []
    cancelled_job_ids: list[str] = []
    for lease in leases:
        job = jobs.get(lease.job_id)
        attempt = attempts.get(lease.attempt_id)
        if (
            job is None
            or attempt is None
            or lease.worker_id != worker.id
            or job.worker_id != worker.id
            or job.current_attempt_id != attempt.id
            or attempt.job_id != job.id
            or attempt.worker_id != worker.id
            or attempt.finished_at is not None
            or job.status in {item.value for item in TERMINAL_JOB_STATUSES}
        ):
            raise HTTPException(
                status_code=409,
                detail="Worker assignment ledger is inconsistent; revoke was not applied",
            )
        previous = job.status
        job.status = JobStatus.CANCELLED.value
        job.cancel_requested_at = now
        job.error_code = "worker_token_emergency_revoked"
        job.error_message = "The assigned Worker credential was revoked by an administrator."
        attempt.status = JobStatus.CANCELLED.value
        attempt.error_code = job.error_code
        attempt.error_message = job.error_message
        attempt.finished_at = now
        lease.active = False
        lease.released_at = now
        session.add(
            JobStatusEvent(
                job_id=job.id,
                attempt_id=attempt.id,
                previous_status=previous,
                status=JobStatus.CANCELLED.value,
                occurred_at=now,
                source="manager",
            )
        )
        mlflow_event_keys.append(
            await mlflow.enqueue_terminal_status(
                session,
                job=job,
                attempt_id=attempt.id,
                status=JobStatus.CANCELLED.value,
                ended_at=now,
            )
        )
        cancelled_job_ids.append(job.id)

    # Replace, rather than merely disable, the current hash so an accidental
    # future is_active toggle cannot resurrect the compromised credential.
    worker.token_hash = hash_worker_token(issue_worker_token(), settings)
    worker.token_issued_at = now
    worker.is_active = False
    worker.status = WorkerStatus.DRAINING.value
    worker.current_job_id = None
    _clear_token_rotation(worker)
    add_audit_event(
        session,
        actor_type="user",
        actor_id=admin_actor_id,
        action="worker.token_revoked",
        resource_type="worker",
        resource_id=worker.id,
        details={
            "reason_code": payload.reason_code,
            "force_cancel_active": payload.force_cancel_active,
            "cancelled_job_ids": sorted(cancelled_job_ids),
            "old_token_revoked": True,
        },
    )
    try:
        await session.commit()
    except StaleDataError as exc:
        await session.rollback()
        retry_count = int(session.info.get("worker_token_revoke_retry", 0))
        if retry_count < 1:
            # A status/claim commit may win the first optimistic CAS after the
            # admin has already proved the emergency-revoke intent. Reload the
            # complete lease->Job->attempt->Worker boundary exactly once; never
            # reuse the stale ORM graph and never loop without a bound.
            session.info["worker_token_revoke_retry"] = retry_count + 1
            return await revoke_worker_token(
                worker_id=worker_id,
                payload=payload,
                session=session,
                settings=settings,
                admin=admin,
                mlflow=mlflow,
            )
        raise HTTPException(
            status_code=409,
            detail="Worker revocation raced with another assignment update",
        ) from exc
    for event_key in mlflow_event_keys:
        await mlflow.sync_after_commit(event_key)
    return _worker_to_read(worker, settings)


def _worker_to_read(worker: Worker, settings: SettingsDep) -> WorkerRead:
    online_after = utc_now() - timedelta(seconds=settings.worker_offline_seconds)
    online = (
        worker.is_active
        and worker.last_heartbeat_at is not None
        and as_utc(worker.last_heartbeat_at) >= online_after
    )
    return WorkerRead(
        id=worker.id,
        name=worker.name,
        status=WorkerStatus(worker.status),
        capabilities=WorkerCapabilities.model_validate(worker.capabilities_json),
        worker_version=worker.worker_version,
        rvc_commit_hash=worker.rvc_commit_hash,
        last_heartbeat_at=worker.last_heartbeat_at,
        current_job_id=worker.current_job_id,
        is_active=worker.is_active,
        online=online,
        token_issued_at=worker.token_issued_at,
        token_rotation_pending=worker.pending_token_hash is not None,
        token_rotation_expires_at=worker.token_rotation_expires_at,
        created_at=worker.created_at,
        updated_at=worker.updated_at,
    )


@router.get("", response_model=WorkerList)
async def list_workers(
    session: SessionDep,
    settings: SettingsDep,
    _admin: AdminUserDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> WorkerList:
    total = await session.scalar(select(func.count()).select_from(Worker)) or 0
    workers = list(
        (
            await session.scalars(
                select(Worker)
                .order_by(Worker.created_at.desc(), Worker.id.asc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    return WorkerList(
        items=[_worker_to_read(worker, settings) for worker in workers],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.post("/heartbeat", response_model=WorkerHeartbeatResponse)
async def heartbeat(
    payload: WorkerHeartbeatRequest,
    worker: WorkerDep,
    session: SessionDep,
    settings: SettingsDep,
) -> WorkerHeartbeatResponse:
    await recover_expired_leases(session, settings)
    now = utc_now()
    renewed_until = None
    if payload.current_job_id:
        lease = await session.scalar(
            select(JobLease)
            .where(JobLease.id == (payload.current_lease_id or ""))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        job = await session.scalar(
            select(Job)
            .where(Job.id == payload.current_job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        attempt = await session.scalar(
            select(JobAttempt)
            .where(JobAttempt.id == (lease.attempt_id if lease is not None else ""))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        locked_worker = await session.scalar(
            select(Worker)
            .where(Worker.id == worker.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is not None and attempt is not None:
            try:
                validated_job_config(job, attempt=attempt)
            except InvalidJobConfigLedger as exc:
                raise HTTPException(
                    status_code=409,
                    detail="job configuration integrity check failed",
                ) from exc
        if (
            lease is None
            or job is None
            or attempt is None
            or locked_worker is None
            or not lease.active
            or lease.released_at is not None
            or as_utc(lease.expires_at) <= now
            or lease.worker_id != worker.id
            or lease.job_id != payload.current_job_id
            or job.worker_id != worker.id
            or job.current_attempt_id != lease.attempt_id
            or attempt.id != lease.attempt_id
            or attempt.job_id != job.id
            or attempt.worker_id != worker.id
            or attempt.finished_at is not None
            or locked_worker.current_job_id != payload.current_job_id
        ):
            raise HTTPException(status_code=409, detail="heartbeat lease is no longer current")
        worker = locked_worker
        renewed_until = max(as_utc(lease.expires_at), lease_expiry(settings, now=now))
        lease.expires_at = renewed_until
        lease.last_renewed_at = max(as_utc(lease.last_renewed_at), now)
    else:
        locked_worker = await session.scalar(
            select(Worker)
            .where(Worker.id == worker.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked_worker is None:
            raise HTTPException(status_code=401, detail="invalid worker token")
        worker = locked_worker
        if worker.current_job_id:
            raise HTTPException(status_code=409, detail="heartbeat omitted active job assignment")

    worker.status = payload.status.value
    worker.capabilities_json = payload.capabilities.model_dump(mode="json")
    worker.worker_version = payload.capabilities.worker_version
    worker.rvc_commit_hash = payload.capabilities.rvc_commit_hash
    worker.last_heartbeat_at = now
    cancel_job_ids = list(
        (
            await session.scalars(
                select(Job.id).where(
                    Job.worker_id == worker.id,
                    Job.cancel_requested_at.is_not(None),
                    Job.status.not_in([item.value for item in TERMINAL_JOB_STATUSES]),
                )
            )
        ).all()
    )
    try:
        await session.commit()
    except StaleDataError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Worker changed before heartbeat commit",
        ) from exc
    return WorkerHeartbeatResponse(
        server_time=now,
        lease_expires_at=renewed_until,
        cancel_job_ids=cancel_job_ids,
    )


@router.post(
    "/jobs/claim",
    response_model=JobClaim,
    responses={status.HTTP_204_NO_CONTENT: {"description": "No compatible job"}},
)
async def claim(
    payload: JobClaimRequest,
    worker: WorkerDep,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> JobClaim | Response:
    capabilities = payload.capabilities or WorkerCapabilities.model_validate(
        worker.capabilities_json
    )
    claimed = await claim_job(
        session,
        worker,
        capabilities,
        settings,
        storage=storage,
    )
    if claimed is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return claimed


@router.get(
    "/next-job",
    response_model=JobClaim,
    responses={status.HTTP_204_NO_CONTENT: {"description": "No compatible job"}},
)
async def next_job(
    worker: WorkerDep,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> JobClaim | Response:
    capabilities = WorkerCapabilities.model_validate(worker.capabilities_json)
    claimed = await claim_job(
        session,
        worker,
        capabilities,
        settings,
        storage=storage,
    )
    if claimed is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return claimed


@router.post("/jobs/{job_id}/lease/renew", response_model=LeaseRenewResponse)
async def renew_lease(
    job_id: str,
    payload: LeaseRenewRequest,
    worker: WorkerDep,
    session: SessionDep,
    settings: SettingsDep,
) -> LeaseRenewResponse:
    lease = await require_active_lease(
        session,
        worker_id=worker.id,
        job_id=job_id,
        lease_id=payload.lease_id,
        for_update=True,
    )
    now = utc_now()
    lease.expires_at = max(as_utc(lease.expires_at), lease_expiry(settings, now=now))
    lease.last_renewed_at = max(as_utc(lease.last_renewed_at), now)
    # The dedicated heartbeat request owns Worker telemetry. Updating the
    # versioned Worker row here races that request and can turn a valid lease
    # renewal into a StaleDataError even though the lease itself is current.
    await session.commit()
    return LeaseRenewResponse(lease_id=lease.id, lease_expires_at=lease.expires_at)


@router.get("/jobs/{job_id}/dataset")
async def download_job_dataset(
    job_id: str,
    worker: WorkerDep,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    lease_id: Annotated[
        str,
        Header(alias="X-RVC-Lease-ID", min_length=1, max_length=128),
    ],
    attempt_id: Annotated[
        str,
        Header(alias="X-RVC-Attempt-ID", min_length=1, max_length=128),
    ],
) -> Response:
    """Return the verified canonical flat archive to its active Worker only."""

    lease = await require_active_lease(
        session,
        worker_id=worker.id,
        job_id=job_id,
        lease_id=lease_id,
    )
    if lease.attempt_id != attempt_id:
        raise HTTPException(status_code=409, detail="dataset attempt does not match lease")
    job = await session.get(Job, job_id)
    if (
        job is None
        or job.worker_id != worker.id
        or job.current_attempt_id != attempt_id
        or job.dataset_id is None
    ):
        raise HTTPException(status_code=409, detail="job attempt is no longer current")
    dataset = await session.get(Dataset, job.dataset_id)
    if (
        dataset is None
        or dataset.status != "ready"
        or not dataset_ready_for_training(dataset)
        or dataset.prepared_flat_size_bytes is None
        or dataset.prepared_flat_size_bytes <= 0
        or dataset.prepared_flat_sha256 is None
        or len(dataset.prepared_flat_sha256) != 64
    ):
        raise HTTPException(status_code=409, detail="dataset is not ready for transfer")
    upload = await session.scalar(
        select(DatasetUploadSession)
        .where(
            DatasetUploadSession.dataset_id == dataset.id,
            DatasetUploadSession.status == "completed",
        )
        .order_by(
            DatasetUploadSession.finalized_at.desc(),
            DatasetUploadSession.generation.desc(),
        )
        .limit(1)
    )
    if upload is None:
        raise HTTPException(status_code=409, detail="dataset has not been server-verified")
    if not storage_namespace_matches(
        backend=upload.storage_backend,
        namespace_sha256=upload.storage_namespace_sha256,
        storage=storage,
    ):
        raise HTTPException(status_code=503, detail="dataset storage namespace is unavailable")
    try:
        canonical_uri = storage.storage_uri(upload.prepared_flat_object_key)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail="dataset storage is unavailable") from exc
    if dataset.flat_storage_uri != canonical_uri:
        raise HTTPException(status_code=409, detail="dataset canonical object does not match")

    disposition = attachment_content_disposition("prepared_flat.zip")
    add_audit_event(
        session,
        actor_type="worker",
        actor_id=worker.id,
        action="dataset.worker_download_requested",
        resource_type="dataset",
        resource_id=dataset.id,
        details={"job_id": job.id, "attempt_id": attempt_id},
    )
    await session.commit()
    try:
        download_url = await storage.create_download_url(
            upload.prepared_flat_object_key,
            content_disposition=disposition,
            expires_in_seconds=settings.dataset_download_ttl_seconds,
        )
    except StorageError as exc:
        raise HTTPException(status_code=503, detail="dataset download is unavailable") from exc
    headers = {
        "Cache-Control": "private, no-store",
        "Content-Disposition": disposition,
        "Vary": "Authorization",
        "X-Content-Type-Options": "nosniff",
    }
    if download_url is not None:
        return RedirectResponse(
            download_url,
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers=headers,
        )

    async def stream() -> AsyncIterator[bytes]:
        async for chunk in storage.stream_object(
            upload.prepared_flat_object_key,
            chunk_size=settings.artifact_stream_chunk_bytes,
            max_bytes=dataset.prepared_flat_size_bytes or 0,
        ):
            yield chunk

    headers["Content-Length"] = str(dataset.prepared_flat_size_bytes)
    return StreamingResponse(
        stream(),
        media_type="application/zip",
        headers=headers,
    )


@router.get("/jobs/{job_id}/test-set/items/{test_set_item_id}")
async def download_job_test_set_item(
    job_id: str,
    test_set_item_id: str,
    worker: WorkerDep,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    lease_id: Annotated[
        str,
        Header(alias="X-RVC-Lease-ID", min_length=1, max_length=128),
    ],
    attempt_id: Annotated[
        str,
        Header(alias="X-RVC-Attempt-ID", min_length=1, max_length=128),
    ],
) -> Response:
    """Return one immutable WAV from the TestSet snapshotted by this Job."""

    lease = await require_active_lease(
        session,
        worker_id=worker.id,
        job_id=job_id,
        lease_id=lease_id,
    )
    if lease.attempt_id != attempt_id:
        raise HTTPException(status_code=409, detail="test set attempt does not match lease")
    job = await session.get(Job, job_id)
    if (
        job is None
        or job.worker_id != worker.id
        or job.current_attempt_id != attempt_id
        or job.test_set_id is None
    ):
        raise HTTPException(status_code=409, detail="job attempt is no longer current")
    item = await session.scalar(
        select(TestSetItem).where(
            TestSetItem.id == test_set_item_id,
            TestSetItem.test_set_id == job.test_set_id,
        )
    )
    if item is None:
        raise HTTPException(status_code=404, detail="test set item not found")
    upload = await session.scalar(
        select(TestSetItemUploadSession)
        .where(
            TestSetItemUploadSession.test_set_id == job.test_set_id,
            TestSetItemUploadSession.item_key == item.item_key,
            TestSetItemUploadSession.status == "completed",
        )
        .order_by(TestSetItemUploadSession.finalized_at.desc())
        .limit(1)
    )
    if upload is None:
        raise HTTPException(status_code=409, detail="test set item is not server-verified")
    if not storage_namespace_matches(
        backend=upload.storage_backend,
        namespace_sha256=upload.storage_namespace_sha256,
        storage=storage,
    ):
        raise HTTPException(status_code=503, detail="test set storage namespace is unavailable")

    attempt = await session.get(JobAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=409, detail="job attempt is no longer current")
    try:
        config = validated_job_config(job, attempt=attempt)
    except InvalidJobConfigLedger as exc:
        raise HTTPException(
            status_code=409,
            detail="job configuration integrity check failed",
        ) from exc
    transfer = await verified_test_set_transfer(
        session,
        job,
        config,
        storage=storage,
        settings=settings,
    )
    if transfer is None:
        raise HTTPException(status_code=409, detail="test set snapshot is no longer verifiable")
    descriptor = next(
        (
            candidate
            for candidate in transfer.items
            if candidate.test_set_item_id == test_set_item_id
        ),
        None,
    )
    if descriptor is None:
        raise HTTPException(status_code=404, detail="test set item is not in the Job snapshot")
    expected_key = test_set_item_object_key(job.test_set_id, upload.id)
    disposition = attachment_content_disposition(descriptor.filename)
    add_audit_event(
        session,
        actor_type="worker",
        actor_id=worker.id,
        action="test_set_item.worker_download_requested",
        resource_type="test_set_item",
        resource_id=item.id,
        details={"job_id": job.id, "attempt_id": attempt_id},
    )
    await session.commit()
    try:
        download_url = await storage.create_download_url(
            expected_key,
            content_disposition=disposition,
            expires_in_seconds=settings.dataset_download_ttl_seconds,
        )
    except StorageError as exc:
        raise HTTPException(status_code=503, detail="test set download is unavailable") from exc
    headers = {
        "Cache-Control": "private, no-store",
        "Content-Disposition": disposition,
        "Vary": "Authorization",
        "X-Content-Type-Options": "nosniff",
    }
    if download_url is not None:
        return RedirectResponse(
            download_url,
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers=headers,
        )

    async def stream() -> AsyncIterator[bytes]:
        async for chunk in storage.stream_object(
            expected_key,
            chunk_size=settings.artifact_stream_chunk_bytes,
            max_bytes=descriptor.size_bytes,
        ):
            yield chunk

    headers["Content-Length"] = str(descriptor.size_bytes)
    return StreamingResponse(
        stream(),
        media_type=descriptor.content_type,
        headers=headers,
    )


async def _required_artifacts_exist(
    session: SessionDep,
    job: Job,
    settings: SettingsDep,
) -> bool:
    attempt = (
        await session.get(JobAttempt, job.current_attempt_id) if job.current_attempt_id else None
    )
    if attempt is None:
        return False
    try:
        config = validated_job_config(job, attempt=attempt)
    except InvalidJobConfigLedger:
        return False
    required = set()
    if config.artifacts.collect_small_model:
        required.add(ArtifactType.FINAL_SMALL_MODEL.value)
    if (
        config.index.build_index
        and config.index.collect_added_index
        and config.artifacts.collect_index
    ):
        required.add(ArtifactType.FINAL_INDEX.value)
    if not required:
        return True
    allow_legacy_fake = (
        attempt.engine_mode == "fake"
        and settings.allow_fake_workers
        and settings.environment != "production"
    )
    statement = select(Artifact.artifact_type).where(
        Artifact.job_id == job.id,
        Artifact.attempt_id == job.current_attempt_id,
        Artifact.artifact_type.in_(required),
    )
    if not allow_legacy_fake:
        statement = statement.join(
            ArtifactUploadSession,
            ArtifactUploadSession.artifact_id == Artifact.id,
        ).where(ArtifactUploadSession.status == "completed")
    present = set((await session.scalars(statement)).all())
    return required.issubset(present)


async def _lock_current_status_claim(
    session: SessionDep,
    *,
    job_id: str,
    attempt_id: str,
    lease_id: str,
    worker_id: str,
    expected_status: str,
) -> tuple[JobLease, Job, JobAttempt, Worker]:
    """Acquire the canonical claim fence immediately before a status commit."""

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
    locked_worker = await session.scalar(
        select(Worker)
        .where(Worker.id == worker_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = utc_now()
    terminal_values = {item.value for item in TERMINAL_JOB_STATUSES}
    if job is not None and attempt is not None:
        try:
            validated_job_config(job, attempt=attempt)
        except InvalidJobConfigLedger as exc:
            raise HTTPException(
                status_code=409,
                detail="job configuration integrity check failed",
            ) from exc
    if (
        lease is None
        or job is None
        or attempt is None
        or locked_worker is None
        or not lease.active
        or lease.released_at is not None
        or as_utc(lease.expires_at) <= now
        or lease.job_id != job_id
        or lease.attempt_id != attempt_id
        or lease.worker_id != worker_id
        or job.worker_id != worker_id
        or job.current_attempt_id != attempt_id
        or job.status != expected_status
        or job.status in terminal_values
        or attempt.job_id != job_id
        or attempt.worker_id != worker_id
        or attempt.finished_at is not None
        or attempt.status != expected_status
        or attempt.status in terminal_values
        or not locked_worker.is_active
        or locked_worker.current_job_id != job_id
    ):
        raise HTTPException(status_code=409, detail="job claim changed before status commit")
    return lease, job, attempt, locked_worker


async def _acquire_status_write_fence(
    session: SessionDep,
    *,
    job_id: str,
    attempt_id: str,
    lease_id: str,
    worker_id: str,
    expected_status: str,
) -> tuple[JobLease, Job, JobAttempt, Worker]:
    fenced = await session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.worker_id == worker_id,
            Job.current_attempt_id == attempt_id,
            Job.status == expected_status,
        )
        .values(row_version=Job.row_version, updated_at=Job.updated_at)
        .execution_options(synchronize_session=False)
    )
    if fenced.rowcount != 1:  # type: ignore[attr-defined]
        raise HTTPException(status_code=409, detail="job claim changed before status commit")
    return await _lock_current_status_claim(
        session,
        job_id=job_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        worker_id=worker_id,
        expected_status=expected_status,
    )


async def _validate_terminal_telemetry_watermarks(
    session: SessionDep,
    *,
    attempt_id: str,
    log_count: int | None,
    metric_count: int | None,
) -> None:
    if log_count is None or metric_count is None:
        return
    max_log_sequence = await session.scalar(
        select(func.max(JobLog.sequence)).where(JobLog.attempt_id == attempt_id)
    )
    max_metric_sequence = await session.scalar(
        select(func.max(Metric.sequence)).where(Metric.attempt_id == attempt_id)
    )
    if max_log_sequence is not None and max_log_sequence >= log_count:
        raise HTTPException(
            status_code=409,
            detail="telemetry_log_count is below an ingested sequence",
        )
    if max_metric_sequence is not None and max_metric_sequence >= metric_count:
        raise HTTPException(
            status_code=409,
            detail="telemetry_metric_count is below an ingested sequence",
        )


@router.post("/jobs/{job_id}/status", response_model=StatusAck)
async def update_job_status(
    job_id: str,
    payload: JobStatusUpdate,
    worker: WorkerDep,
    session: SessionDep,
    settings: SettingsDep,
    mlflow: MlflowDep,
    storage: StorageDep,
) -> StatusAck:
    stored_actor_worker_id = session.info.get("job_status_actor_worker_id")
    if isinstance(stored_actor_worker_id, str):
        actor_worker_id = stored_actor_worker_id
    else:
        actor_worker_id = worker.id
        session.info["job_status_actor_worker_id"] = actor_worker_id
    lease = await require_active_lease(
        session, worker_id=actor_worker_id, job_id=job_id, lease_id=payload.lease_id
    )
    job = await session.get(Job, job_id)
    attempt = await session.get(JobAttempt, lease.attempt_id)
    if job is None or attempt is None or job.current_attempt_id != attempt.id:
        raise HTTPException(status_code=409, detail="job attempt is no longer current")
    _validate_current_epoch(job, payload.current_epoch)
    try:
        target = validate_job_transition(job.status, payload.status)
    except InvalidJobTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if job.cancel_requested_at is not None and target is not JobStatus.CANCELLED:
        raise HTTPException(status_code=409, detail="job cancellation superseded status update")
    if target is JobStatus.COMPLETED and not await _required_artifacts_exist(
        session,
        job,
        settings,
    ):
        raise HTTPException(status_code=409, detail="required artifacts are not registered")
    if target is JobStatus.COMPLETED:
        try:
            samples_ready = await sample_completion_ready(
                session,
                job,
                settings,
                storage,
                lease_id=lease.id,
                worker_id=actor_worker_id,
            )
        except SampleCompletionUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail="sample completion verification is temporarily unavailable",
                headers={"Retry-After": str(settings.artifact_retry_after_seconds)},
            ) from exc
        if not samples_ready:
            raise HTTPException(status_code=409, detail="required samples are not registered")

    expected_status = job.status
    lease, job, attempt, worker = await _lock_current_status_claim(
        session,
        job_id=job_id,
        attempt_id=attempt.id,
        lease_id=payload.lease_id,
        worker_id=actor_worker_id,
        expected_status=expected_status,
    )
    try:
        target = validate_job_transition(job.status, payload.status)
    except InvalidJobTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if job.cancel_requested_at is not None and target is not JobStatus.CANCELLED:
        raise HTTPException(status_code=409, detail="job cancellation superseded status update")
    _validate_current_epoch(job, payload.current_epoch)
    if target in TERMINAL_JOB_STATUSES:
        try:
            lease, job, attempt, worker = await _acquire_status_write_fence(
                session,
                job_id=job_id,
                attempt_id=attempt.id,
                lease_id=payload.lease_id,
                worker_id=actor_worker_id,
                expected_status=expected_status,
            )
        except OperationalError as exc:
            await session.rollback()
            retry_count = int(session.info.get("job_status_fence_retry", 0))
            if retry_count < 1:
                session.info["job_status_fence_retry"] = retry_count + 1
                return await update_job_status(
                    job_id=job_id,
                    payload=payload,
                    worker=worker,
                    session=session,
                    settings=settings,
                    mlflow=mlflow,
                    storage=storage,
                )
            raise HTTPException(
                status_code=409,
                detail="job claim changed before status commit",
            ) from exc
        try:
            target = validate_job_transition(job.status, payload.status)
        except InvalidJobTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if job.cancel_requested_at is not None and target is not JobStatus.CANCELLED:
            raise HTTPException(
                status_code=409,
                detail="job cancellation superseded status update",
            )
        _validate_current_epoch(job, payload.current_epoch)
    if target is JobStatus.COMPLETED and not await _required_artifacts_exist(
        session,
        job,
        settings,
    ):
        raise HTTPException(status_code=409, detail="required artifacts changed before commit")
    if target in TERMINAL_JOB_STATUSES:
        await _validate_terminal_telemetry_watermarks(
            session,
            attempt_id=attempt.id,
            log_count=payload.telemetry_log_count,
            metric_count=payload.telemetry_metric_count,
        )

    previous = job.status
    now = utc_now()
    if payload.current_epoch is not None:
        job.current_epoch = payload.current_epoch
    job.error_code = payload.error_code
    job.error_message = payload.error_message
    attempt.error_code = payload.error_code
    attempt.error_message = payload.error_message
    if previous != target.value:
        job.status = target.value
        attempt.status = target.value
        session.add(
            JobStatusEvent(
                job_id=job.id,
                attempt_id=attempt.id,
                previous_status=previous,
                status=target.value,
                occurred_at=payload.occurred_at,
                source="worker",
            )
        )
    if target in TERMINAL_JOB_STATUSES:
        attempt.telemetry_log_count = payload.telemetry_log_count
        attempt.telemetry_metric_count = payload.telemetry_metric_count
        lease.active = False
        lease.released_at = now
        attempt.finished_at = now
        worker.current_job_id = None
        worker.status = "idle"
        if target is JobStatus.COMPLETED:
            job.completed_at = now
        mlflow_event_key = await mlflow.enqueue_terminal_status(
            session,
            job=job,
            attempt_id=attempt.id,
            status=target.value,
            ended_at=now,
        )
    else:
        mlflow_event_key = None
    try:
        await session.commit()
    except StaleDataError as exc:
        await session.rollback()
        retry_count = int(session.info.get("job_status_stale_retry", 0))
        if retry_count < 1:
            # Heartbeat owns telemetry on the same versioned Worker row that a
            # terminal transition releases. SQLite cannot honor SELECT FOR
            # UPDATE, so a heartbeat can win that optimistic CAS after this
            # request has already checked every lease/attempt/Worker fence.
            # Reload the complete boundary exactly once; a cancelled, expired,
            # reassigned, or otherwise changed claim will fail the normal
            # checks on the retry instead of being accepted from stale state.
            session.info["job_status_stale_retry"] = retry_count + 1
            return await update_job_status(
                job_id=job_id,
                payload=payload,
                worker=worker,
                session=session,
                settings=settings,
                mlflow=mlflow,
                storage=storage,
            )
        raise HTTPException(
            status_code=409,
            detail="job claim changed before status commit",
        ) from exc
    await mlflow.sync_after_commit(mlflow_event_key)
    return StatusAck(
        job_id=job.id,
        status=target,
        lease_expires_at=lease.expires_at if lease.active else None,
    )


async def _find_ingest_batch(
    session: SessionDep,
    *,
    attempt_id: str,
    batch_type: str,
    idempotency_key: str,
) -> IngestBatch | None:
    return cast(
        IngestBatch | None,
        await session.scalar(
            select(IngestBatch).where(
                IngestBatch.attempt_id == attempt_id,
                IngestBatch.batch_type == batch_type,
                IngestBatch.idempotency_key == idempotency_key,
            )
        ),
    )


async def _is_duplicate_batch(
    session: SessionDep,
    *,
    attempt_id: str,
    batch_type: str,
    idempotency_key: str,
) -> bool:
    return (
        await _find_ingest_batch(
            session,
            attempt_id=attempt_id,
            batch_type=batch_type,
            idempotency_key=idempotency_key,
        )
        is not None
    )


def _canonical_fingerprint(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="telemetry payload is not canonical JSON",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _log_batch_fingerprint(payload: LogBatch) -> str:
    entries: list[dict[str, object]] = []
    for entry in payload.entries:
        document: dict[str, object] = {
            "sequence": entry.sequence,
            "level": entry.level.value,
            "message": redact_log_text(entry.message),
            "fields": redact_log_fields(entry.fields),
        }
        if "occurred_at" in entry.model_fields_set:
            document["occurred_at"] = as_utc(entry.occurred_at).isoformat()
        entries.append(document)
    return _canonical_fingerprint(entries)


def _metric_batch_fingerprint(payload: MetricBatch) -> str:
    entries: list[dict[str, object]] = []
    for entry in payload.entries:
        document: dict[str, object] = {
            "sequence": entry.sequence,
            "key": entry.key,
            "value": entry.value,
            "epoch": entry.epoch,
            "step": entry.step,
        }
        if "occurred_at" in entry.model_fields_set:
            document["occurred_at"] = as_utc(entry.occurred_at).isoformat()
        entries.append(document)
    return _canonical_fingerprint(entries)


def _validate_batch_replay(existing: IngestBatch, fingerprint: str) -> None:
    if existing.payload_fingerprint != fingerprint:
        raise HTTPException(
            status_code=409,
            detail="telemetry idempotency key conflicts with a prior batch",
        )


async def _lock_telemetry_claim(
    session: SessionDep,
    *,
    worker_id: str,
    job_id: str,
    lease_id: str,
    attempt_id: str,
    batch_type: str,
    sequences: list[int],
) -> tuple[JobLease, Job, JobAttempt, bool]:
    lease = await session.scalar(
        select(JobLease)
        .where(
            JobLease.id == lease_id,
            JobLease.job_id == job_id,
            JobLease.worker_id == worker_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if lease is None:
        raise HTTPException(status_code=409, detail="invalid job lease")
    if lease.attempt_id != attempt_id:
        raise HTTPException(status_code=409, detail="batch attempt does not match lease")
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
    locked_worker = await session.scalar(
        select(Worker)
        .where(Worker.id == worker_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    terminal_values = {item.value for item in TERMINAL_JOB_STATUSES}
    common_invalid = (
        job is None
        or attempt is None
        or locked_worker is None
        or attempt.job_id != job_id
        or attempt.worker_id != worker_id
        or not locked_worker.is_active
    )
    if common_invalid:
        raise HTTPException(status_code=409, detail="job claim changed before telemetry commit")
    assert job is not None
    assert attempt is not None
    assert locked_worker is not None
    try:
        validated_job_config(job, attempt=attempt)
    except InvalidJobConfigLedger as exc:
        raise HTTPException(
            status_code=409,
            detail="job configuration integrity check failed",
        ) from exc

    active_claim = lease.active and lease.released_at is None
    if active_claim:
        if as_utc(lease.expires_at) <= utc_now():
            raise HTTPException(status_code=409, detail="job lease expired")
        if (
            job.worker_id != worker_id
            or job.current_attempt_id != attempt_id
            or job.status in terminal_values
            or attempt.finished_at is not None
            or attempt.status != job.status
            or attempt.status in terminal_values
            or attempt.telemetry_log_count is not None
            or attempt.telemetry_metric_count is not None
            or locked_worker.current_job_id != job_id
        ):
            raise HTTPException(status_code=409, detail="job claim changed before telemetry commit")
        return lease, job, attempt, False

    if (
        lease.active
        or lease.released_at is None
        or attempt.finished_at is None
        or attempt.status not in terminal_values
    ):
        raise HTTPException(status_code=409, detail="job lease is not a terminal telemetry lease")
    log_count = attempt.telemetry_log_count
    metric_count = attempt.telemetry_metric_count
    if log_count is None or metric_count is None:
        raise HTTPException(
            status_code=409,
            detail="terminal attempt has no telemetry watermarks",
        )
    watermark = log_count if batch_type == "logs" else metric_count
    if any(sequence >= watermark for sequence in sequences):
        raise HTTPException(
            status_code=409,
            detail="telemetry sequence exceeds terminal watermark",
        )
    return lease, job, attempt, True


def _retryable_telemetry_fence_conflict() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="telemetry claim changed; retry against the terminal watermark",
        headers={"Retry-After": "1"},
    )


async def _acquire_active_telemetry_write_fence(
    session: SessionDep,
    *,
    worker_id: str,
    job_id: str,
    lease_id: str,
    attempt_id: str,
    batch_type: str,
    sequences: list[int],
) -> tuple[JobLease, Job, JobAttempt]:
    active_lease_exists = (
        select(JobLease.id)
        .where(
            JobLease.id == lease_id,
            JobLease.job_id == job_id,
            JobLease.attempt_id == attempt_id,
            JobLease.worker_id == worker_id,
            JobLease.active.is_(True),
            JobLease.released_at.is_(None),
            JobLease.expires_at > utc_now(),
        )
        .exists()
    )
    try:
        fenced = await session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.worker_id == worker_id,
                Job.current_attempt_id == attempt_id,
                Job.cancel_requested_at.is_(None),
                Job.status.not_in([item.value for item in TERMINAL_JOB_STATUSES]),
                active_lease_exists,
            )
            .values(row_version=Job.row_version, updated_at=Job.updated_at)
            .execution_options(synchronize_session=False)
        )
    except OperationalError as exc:
        await session.rollback()
        raise _retryable_telemetry_fence_conflict() from exc
    if fenced.rowcount != 1:  # type: ignore[attr-defined]
        raise _retryable_telemetry_fence_conflict()
    lease, job, attempt, late = await _lock_telemetry_claim(
        session,
        worker_id=worker_id,
        job_id=job_id,
        lease_id=lease_id,
        attempt_id=attempt_id,
        batch_type=batch_type,
        sequences=sequences,
    )
    if late:
        raise _retryable_telemetry_fence_conflict()
    if job.cancel_requested_at is not None:
        raise _retryable_telemetry_fence_conflict()
    return lease, job, attempt


def _log_entry_matches(stored: JobLog, entry: LogEntry) -> bool:
    return (
        stored.level == entry.level.value
        and stored.message == redact_log_text(entry.message)
        and stored.fields_json == redact_log_fields(entry.fields)
        and (
            "occurred_at" not in entry.model_fields_set
            or as_utc(stored.occurred_at) == as_utc(entry.occurred_at)
        )
    )


def _metric_entry_matches(stored: Metric, entry: MetricEntry) -> bool:
    return (
        stored.key == entry.key
        and stored.value == entry.value
        and stored.epoch == entry.epoch
        and stored.step == entry.step
        and (
            "occurred_at" not in entry.model_fields_set
            or as_utc(stored.occurred_at) == as_utc(entry.occurred_at)
        )
    )


def _metric_epoch(entry: MetricEntry, *, total_epoch: int) -> int | None:
    epoch = entry.epoch
    if epoch is not None and epoch > total_epoch:
        raise HTTPException(status_code=422, detail="metric epoch exceeds total_epoch")
    key = entry.key
    value = entry.value
    if key == "total_epoch":
        if not value.is_integer() or int(value) != total_epoch:
            raise HTTPException(status_code=422, detail="total_epoch metric does not match Job")
    if key != "current_epoch":
        return epoch
    if value < 0 or not value.is_integer():
        raise HTTPException(status_code=422, detail="current_epoch metric must be an integer")
    current_epoch = int(value)
    if current_epoch > total_epoch:
        raise HTTPException(status_code=422, detail="current_epoch metric exceeds total_epoch")
    if epoch is not None and epoch != current_epoch:
        raise HTTPException(status_code=422, detail="current_epoch metric conflicts with epoch")
    return current_epoch


@router.post("/jobs/{job_id}/logs", response_model=OperationAck)
async def ingest_logs(
    job_id: str,
    payload: LogBatch,
    worker: WorkerDep,
    session: SessionDep,
) -> OperationAck:
    actor_worker_id = worker.id
    fingerprint = _log_batch_fingerprint(payload)
    sequences = [entry.sequence for entry in payload.entries]
    lease, _, _, late = await _lock_telemetry_claim(
        session,
        worker_id=actor_worker_id,
        job_id=job_id,
        lease_id=payload.lease_id,
        attempt_id=payload.attempt_id,
        batch_type="logs",
        sequences=sequences,
    )
    replay = await _find_ingest_batch(
        session,
        attempt_id=lease.attempt_id,
        batch_type="logs",
        idempotency_key=payload.idempotency_key,
    )
    if replay is not None:
        _validate_batch_replay(replay, fingerprint)
        await session.rollback()
        return OperationAck(duplicate=True)
    existing = {
        entry.sequence: entry
        for entry in (
            await session.scalars(
                select(JobLog).where(
                    JobLog.attempt_id == lease.attempt_id,
                    JobLog.sequence.in_([entry.sequence for entry in payload.entries]),
                )
            )
        ).all()
    }
    for entry in payload.entries:
        stored = existing.get(entry.sequence)
        if stored is not None and not _log_entry_matches(stored, entry):
            raise HTTPException(status_code=409, detail="log sequence conflicts with prior payload")
    new_entries = [entry for entry in payload.entries if entry.sequence not in existing]
    if not late:
        lease, _, _ = await _acquire_active_telemetry_write_fence(
            session,
            worker_id=actor_worker_id,
            job_id=job_id,
            lease_id=payload.lease_id,
            attempt_id=payload.attempt_id,
            batch_type="logs",
            sequences=sequences,
        )
    session.add_all(
        [
            JobLog(
                job_id=job_id,
                attempt_id=lease.attempt_id,
                sequence=entry.sequence,
                level=entry.level.value,
                message=redact_log_text(entry.message),
                fields_json=redact_log_fields(entry.fields),
                occurred_at=entry.occurred_at,
            )
            for entry in new_entries
        ]
    )
    session.add(
        IngestBatch(
            job_id=job_id,
            attempt_id=lease.attempt_id,
            batch_type="logs",
            idempotency_key=payload.idempotency_key,
            payload_fingerprint=fingerprint,
            item_count=len(new_entries),
        )
    )
    try:
        await session.commit()
    except OperationalError as exc:
        await session.rollback()
        raise _retryable_telemetry_fence_conflict() from exc
    except (IntegrityError, StaleDataError) as exc:
        await session.rollback()
        raced = await _find_ingest_batch(
            session,
            attempt_id=payload.attempt_id,
            batch_type="logs",
            idempotency_key=payload.idempotency_key,
        )
        if raced is not None:
            _validate_batch_replay(raced, fingerprint)
            await _lock_telemetry_claim(
                session,
                worker_id=actor_worker_id,
                job_id=job_id,
                lease_id=payload.lease_id,
                attempt_id=payload.attempt_id,
                batch_type="logs",
                sequences=sequences,
            )
            # The replay check above deliberately reacquires the current
            # lease/job/attempt/worker locks after the failed commit.  A
            # duplicate response must not keep those locks until dependency
            # teardown, however, and metrics must never call MLflow while
            # holding them.
            await session.rollback()
            return OperationAck(duplicate=True)
        raise HTTPException(status_code=409, detail="telemetry ingest conflicted") from exc
    return OperationAck(accepted=len(new_entries))


@router.post("/jobs/{job_id}/metrics", response_model=OperationAck)
async def ingest_metrics(
    job_id: str,
    payload: MetricBatch,
    worker: WorkerDep,
    session: SessionDep,
    mlflow: MlflowDep,
) -> OperationAck:
    actor_worker_id = worker.id
    fingerprint = _metric_batch_fingerprint(payload)
    sequences = [entry.sequence for entry in payload.entries]
    lease, job, attempt, late = await _lock_telemetry_claim(
        session,
        worker_id=actor_worker_id,
        job_id=job_id,
        lease_id=payload.lease_id,
        attempt_id=payload.attempt_id,
        batch_type="metrics",
        sequences=sequences,
    )
    replay = await _find_ingest_batch(
        session,
        attempt_id=lease.attempt_id,
        batch_type="metrics",
        idempotency_key=payload.idempotency_key,
    )
    if replay is not None:
        _validate_batch_replay(replay, fingerprint)
        await session.rollback()
        await mlflow.sync_after_commit(
            metric_event_key(payload.attempt_id, payload.idempotency_key)
        )
        return OperationAck(duplicate=True)
    incoming_epochs = [
        _metric_epoch(entry, total_epoch=job.total_epoch) for entry in payload.entries
    ]
    existing = {
        entry.sequence: entry
        for entry in (
            await session.scalars(
                select(Metric).where(
                    Metric.attempt_id == lease.attempt_id,
                    Metric.sequence.in_([entry.sequence for entry in payload.entries]),
                )
            )
        ).all()
    }
    for entry in payload.entries:
        stored = existing.get(entry.sequence)
        if stored is not None and not _metric_entry_matches(stored, entry):
            raise HTTPException(
                status_code=409,
                detail="metric sequence conflicts with prior payload",
            )
    new_entries = [entry for entry in payload.entries if entry.sequence not in existing]
    if not late:
        lease, job, attempt = await _acquire_active_telemetry_write_fence(
            session,
            worker_id=actor_worker_id,
            job_id=job_id,
            lease_id=payload.lease_id,
            attempt_id=payload.attempt_id,
            batch_type="metrics",
            sequences=sequences,
        )
    new_sequences = {entry.sequence for entry in new_entries}
    projected_epochs = [
        epoch
        for entry, epoch in zip(payload.entries, incoming_epochs, strict=True)
        if entry.sequence in new_sequences and epoch is not None
    ]
    if projected_epochs and not late:
        projected_epoch = max(projected_epochs)
        if job.current_epoch is None or projected_epoch > job.current_epoch:
            job.current_epoch = projected_epoch
    session.add_all(
        [
            Metric(
                job_id=job_id,
                attempt_id=lease.attempt_id,
                sequence=entry.sequence,
                epoch=entry.epoch,
                step=entry.step,
                key=entry.key,
                value=entry.value,
                occurred_at=entry.occurred_at,
            )
            for entry in new_entries
        ]
    )
    session.add(
        IngestBatch(
            job_id=job_id,
            attempt_id=lease.attempt_id,
            batch_type="metrics",
            idempotency_key=payload.idempotency_key,
            payload_fingerprint=fingerprint,
            item_count=len(new_entries),
        )
    )
    mlflow_event_key = await mlflow.enqueue_metric_batch(
        session,
        job=job,
        attempt_id=attempt.id,
        attempt_number=attempt.attempt_number,
        idempotency_key=payload.idempotency_key,
        entries=new_entries,
    )
    try:
        await session.commit()
    except OperationalError as exc:
        await session.rollback()
        raise _retryable_telemetry_fence_conflict() from exc
    except (IntegrityError, StaleDataError) as exc:
        await session.rollback()
        raced = await _find_ingest_batch(
            session,
            attempt_id=payload.attempt_id,
            batch_type="metrics",
            idempotency_key=payload.idempotency_key,
        )
        if raced is not None:
            _validate_batch_replay(raced, fingerprint)
            await _lock_telemetry_claim(
                session,
                worker_id=actor_worker_id,
                job_id=job_id,
                lease_id=payload.lease_id,
                attempt_id=payload.attempt_id,
                batch_type="metrics",
                sequences=sequences,
            )
            await session.rollback()
            await mlflow.sync_after_commit(
                metric_event_key(payload.attempt_id, payload.idempotency_key)
            )
            return OperationAck(duplicate=True)
        raise HTTPException(status_code=409, detail="telemetry ingest conflicted") from exc
    await mlflow.sync_after_commit(mlflow_event_key)
    return OperationAck(accepted=len(new_entries))


@router.post("/jobs/{job_id}/artifacts", response_model=OperationAck)
async def ingest_artifacts(
    job_id: str,
    payload: ArtifactBatch,
    worker: WorkerDep,
    session: SessionDep,
    settings: SettingsDep,
    mlflow: MlflowDep,
) -> OperationAck:
    lease = await require_active_lease(
        session, worker_id=worker.id, job_id=job_id, lease_id=payload.lease_id
    )
    if lease.attempt_id != payload.attempt_id:
        raise HTTPException(status_code=409, detail="batch attempt does not match lease")
    attempt = await session.get(JobAttempt, lease.attempt_id)
    allow_legacy_fake = (
        attempt is not None
        and attempt.engine_mode == "fake"
        and settings.allow_fake_workers
        and settings.environment != "production"
    )
    if not allow_legacy_fake:
        raise HTTPException(
            status_code=403,
            detail="metadata-only artifacts are restricted to fake test workers",
        )
    if any(
        urlparse(item.storage_uri).scheme != "file" or item.metadata.get("fake") is not True
        for item in payload.artifacts
    ):
        raise HTTPException(
            status_code=422,
            detail="fake metadata artifacts require file:// URIs and fake=true metadata",
        )
    if await _is_duplicate_batch(
        session,
        attempt_id=lease.attempt_id,
        batch_type="artifacts",
        idempotency_key=payload.idempotency_key,
    ):
        existing_artifacts = list(
            (
                await session.scalars(
                    select(Artifact).where(
                        Artifact.attempt_id == lease.attempt_id,
                        Artifact.sha256.in_([item.sha256 for item in payload.artifacts]),
                    )
                )
            ).all()
        )
        for existing_artifact in existing_artifacts:
            await mlflow.sync_after_commit(artifact_event_key(existing_artifact.id))
        return OperationAck(duplicate=True)
    existing = set(
        (
            await session.execute(
                select(Artifact.artifact_type, Artifact.sha256).where(
                    Artifact.attempt_id == lease.attempt_id
                )
            )
        ).all()
    )
    new_items = [
        item
        for item in payload.artifacts
        if (item.artifact_type.value, item.sha256) not in existing
    ]
    artifacts = [
        Artifact(
            job_id=job_id,
            attempt_id=lease.attempt_id,
            artifact_type=item.artifact_type.value,
            filename=item.filename,
            storage_uri=item.storage_uri,
            size_bytes=item.size_bytes,
            sha256=item.sha256,
            mime_type=item.mime_type,
            metadata_json=item.metadata,
        )
        for item in new_items
    ]
    session.add_all(artifacts)
    session.add(
        IngestBatch(
            job_id=job_id,
            attempt_id=lease.attempt_id,
            batch_type="artifacts",
            idempotency_key=payload.idempotency_key,
            item_count=len(new_items),
        )
    )
    await session.flush()
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=409, detail="job is no longer available")
    mlflow_event_keys = [
        await mlflow.enqueue_artifact(session, job=job, artifact=artifact) for artifact in artifacts
    ]
    await session.commit()
    for mlflow_event_key in mlflow_event_keys:
        await mlflow.sync_after_commit(mlflow_event_key)
    return OperationAck(accepted=len(new_items))


# Keep the single-segment dynamic route last so fixed Worker protocol paths such
# as /me and /next-job can never be shadowed by the Manager detail endpoint.
@router.get("/{worker_id}", response_model=WorkerRead)
async def get_worker(
    worker_id: str,
    session: SessionDep,
    settings: SettingsDep,
    _admin: AdminUserDep,
) -> WorkerRead:
    worker = await session.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="worker not found")
    return _worker_to_read(worker, settings)
