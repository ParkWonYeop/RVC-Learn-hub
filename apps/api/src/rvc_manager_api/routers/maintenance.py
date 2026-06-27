from __future__ import annotations

import hashlib
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from rvc_orchestrator_contracts import utc_now

from ..audit import add_audit_event
from ..dependencies import AdminUserDep, SessionDep, SettingsDep
from ..maintenance_queue import (
    MaintenanceQueueEnvelopeConflict,
    MaintenanceQueuePort,
    MaintenanceQueueUnavailable,
)
from ..models import MaintenanceTaskRun
from ..schemas import MaintenanceEnqueueRequest, MaintenanceRunRead

router = APIRouter(prefix="/admin/maintenance", tags=["maintenance"])
MaintenanceTaskName = Literal["dataset_staging_cleanup", "test_set_staging_cleanup"]


def get_maintenance_queue(request: Request) -> MaintenanceQueuePort | None:
    return cast(MaintenanceQueuePort | None, request.app.state.maintenance_queue)


MaintenanceQueueDep = Annotated[
    MaintenanceQueuePort | None,
    Depends(get_maintenance_queue),
]
IdempotencyKeyHeader = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
    ),
]


def _run_read(run: MaintenanceTaskRun) -> MaintenanceRunRead:
    return MaintenanceRunRead(
        id=run.id,
        task_name=run.task_name,  # type: ignore[arg-type]
        job_id=run.job_id,
        dry_run=run.dry_run,
        status=run.status,  # type: ignore[arg-type]
        attempt_count=run.attempt_count,
        max_attempts=run.max_attempts,
        result=run.result_json,
        last_error_code=run.last_error_code,
        queued_at=run.queued_at,
        started_at=run.started_at,
        heartbeat_at=run.heartbeat_at,
        completed_at=run.completed_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _job_identity(
    *,
    task_name: MaintenanceTaskName = "dataset_staging_cleanup",
    actor_id: str,
    idempotency_key: str,
    dry_run: bool,
) -> tuple[str, str]:
    digest = hashlib.sha256(
        (f"{task_name}\x1f{actor_id}\x1f{int(dry_run)}\x1f{idempotency_key}").encode()
    ).hexdigest()
    return f"rvc-maintenance-{digest}", digest


async def _enqueue_staging_cleanup(
    task_name: MaintenanceTaskName,
    payload: MaintenanceEnqueueRequest,
    response: Response,
    user: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
    queue: MaintenanceQueueDep,
    idempotency_key: IdempotencyKeyHeader,
) -> MaintenanceRunRead:
    if not settings.rq_enabled or queue is None:
        raise HTTPException(status_code=503, detail="maintenance queue is disabled")
    audit_prefix = f"maintenance.{task_name}"
    job_id, key_hash = _job_identity(
        task_name=task_name,
        actor_id=user.id,
        idempotency_key=idempotency_key,
        dry_run=payload.dry_run,
    )
    run = await session.scalar(
        select(MaintenanceTaskRun)
        .where(MaintenanceTaskRun.idempotency_key_hash == key_hash)
        .with_for_update()
    )
    existing = run is not None
    if run is None:
        run = MaintenanceTaskRun(
            task_name=task_name,
            job_id=job_id,
            idempotency_key_hash=key_hash,
            dry_run=payload.dry_run,
            status="queued",
            attempt_count=0,
            max_attempts=settings.maintenance_task_max_attempts,
            result_json={},
            created_by=user.id,
        )
        session.add(run)
        add_audit_event(
            session,
            actor_type="user",
            actor_id=user.id,
            action=f"{audit_prefix}.requested",
            resource_type="maintenance_task_run",
            resource_id=run.id,
            details={"dry_run": payload.dry_run, "job_id": job_id},
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            run = await session.scalar(
                select(MaintenanceTaskRun).where(
                    MaintenanceTaskRun.idempotency_key_hash == key_hash
                )
            )
            if run is None:
                raise
            existing = True
    if existing and run.status in {"running", "completed", "failed"}:
        response.status_code = 200
        return _run_read(run)
    if run.attempt_count >= run.max_attempts:
        run.status = "failed"
        run.last_error_code = "maintenance_attempts_exhausted"
        run.completed_at = utc_now()
        add_audit_event(
            session,
            actor_type="system",
            action=f"{audit_prefix}.reconcile_failed",
            resource_type="maintenance_task_run",
            resource_id=run.id,
            details={"failure_code": "maintenance_attempts_exhausted"},
        )
        await session.commit()
        response.status_code = 200
        return _run_read(run)
    try:
        if task_name == "dataset_staging_cleanup":
            enqueued = await queue.enqueue_dataset_cleanup(
                run_id=run.id,
                job_id=run.job_id,
                max_attempts=run.max_attempts,
            )
        else:
            enqueued = await queue.enqueue_test_set_cleanup(
                run_id=run.id,
                job_id=run.job_id,
                max_attempts=run.max_attempts,
            )
    except MaintenanceQueueEnvelopeConflict as exc:
        run.status = "failed"
        run.last_error_code = exc.code
        run.completed_at = utc_now()
        add_audit_event(
            session,
            actor_type="system",
            action=f"{audit_prefix}.reconcile_failed",
            resource_type="maintenance_task_run",
            resource_id=run.id,
            details={"failure_code": exc.code},
        )
        await session.commit()
        raise HTTPException(
            status_code=503,
            detail={
                "code": exc.code,
                "ledger_committed": True,
                "run_id": run.id,
            },
        ) from exc
    except MaintenanceQueueUnavailable as exc:
        run.status = "enqueue_failed"
        run.last_error_code = "maintenance_queue_unavailable"
        add_audit_event(
            session,
            actor_type="system",
            action=f"{audit_prefix}.enqueue_failed",
            resource_type="maintenance_task_run",
            resource_id=run.id,
            details={"failure_code": "maintenance_queue_unavailable"},
        )
        await session.commit()
        raise HTTPException(
            status_code=503,
            detail={
                "code": "maintenance_queue_unavailable",
                "ledger_committed": True,
                "run_id": run.id,
            },
        ) from exc
    run.status = "queued"
    run.queued_at = utc_now()
    run.last_error_code = None
    add_audit_event(
        session,
        actor_type="system",
        action=f"{audit_prefix}.enqueued",
        resource_type="maintenance_task_run",
        resource_id=run.id,
        details={
            "job_id": run.job_id,
            "existing": enqueued.existing,
            "job_state": enqueued.job_state,
            "repaired": enqueued.repaired,
            "repair_code": enqueued.repair_code,
        },
    )
    await session.commit()
    await session.refresh(run)
    if existing:
        response.status_code = 200
    return _run_read(run)


@router.post(
    "/dataset-staging-cleanup",
    response_model=MaintenanceRunRead,
    status_code=202,
)
async def enqueue_dataset_staging_cleanup(
    payload: MaintenanceEnqueueRequest,
    response: Response,
    user: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
    queue: MaintenanceQueueDep,
    idempotency_key: IdempotencyKeyHeader,
) -> MaintenanceRunRead:
    return await _enqueue_staging_cleanup(
        "dataset_staging_cleanup",
        payload,
        response,
        user,
        session,
        settings,
        queue,
        idempotency_key,
    )


@router.post(
    "/test-set-staging-cleanup",
    response_model=MaintenanceRunRead,
    status_code=202,
)
async def enqueue_test_set_staging_cleanup(
    payload: MaintenanceEnqueueRequest,
    response: Response,
    user: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
    queue: MaintenanceQueueDep,
    idempotency_key: IdempotencyKeyHeader,
) -> MaintenanceRunRead:
    return await _enqueue_staging_cleanup(
        "test_set_staging_cleanup",
        payload,
        response,
        user,
        session,
        settings,
        queue,
        idempotency_key,
    )


@router.get("/{run_id}", response_model=MaintenanceRunRead)
async def get_maintenance_run(
    run_id: str,
    user: AdminUserDep,
    session: SessionDep,
) -> MaintenanceRunRead:
    del user
    run = await session.get(MaintenanceTaskRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="maintenance run not found")
    return _run_read(run)
