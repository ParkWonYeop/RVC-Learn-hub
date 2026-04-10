from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Annotated, Any, cast

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from rvc_orchestrator_contracts import (
    TERMINAL_JOB_STATUSES,
    InferencePresetConfig,
    JobConfig,
    JobStatus,
    WorkerEngineMode,
    utc_now,
    validate_job_transition,
)

from ..audit import add_audit_event
from ..dependencies import CurrentUserDep, MlflowDep, SessionDep, SettingsDep
from ..models import (
    Dataset,
    Experiment,
    Job,
    JobAttempt,
    JobStatusEvent,
    MlflowSyncEvent,
    TestSet,
    TestSetItem,
    User,
)
from ..schemas import (
    ExperimentComparisonRead,
    ExperimentCreate,
    ExperimentList,
    ExperimentRead,
    ExperimentUpdate,
    JobList,
    JobRead,
)
from ..services.datasets import dataset_ready_for_training
from ..services.experiment_comparison import (
    ExperimentComparisonUnavailable,
    InvalidExperimentComparisonLedger,
    build_experiment_comparison_jobs,
)
from ..services.test_sets import (
    build_sample_plan_document,
    build_test_set_manifest_document,
    canonical_sha256,
)
from ..storage import StorageAdapter

router = APIRouter(tags=["manager"])

_CREATE_EXPERIMENT_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"description": "Dataset not found or hidden by ownership"},
    409: {"description": "Dataset readiness or owner/name conflict"},
    413: {"description": "Experiment JSON body exceeds the configured bound"},
    422: {"description": "Invalid or unknown request field"},
}
_UPDATE_EXPERIMENT_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"description": "Experiment not found or hidden by ownership"},
    409: {"description": "Optimistic row version conflict"},
    413: {"description": "Experiment JSON body exceeds the configured bound"},
    422: {"description": "Invalid, unknown, or immutable request field"},
}
_DELETE_EXPERIMENT_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"description": "Experiment not found or hidden by ownership"},
    409: {"description": "Version conflict or referenced/projection-bound Experiment"},
    422: {"description": "Missing or invalid expected_row_version"},
}
_COMPARISON_JOB_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def require_owner_or_admin(user: User, owner_id: str | None, resource: str) -> None:
    if user.role != "admin" and owner_id != user.id:
        # Ownership is intentionally concealed to prevent cross-user resource
        # enumeration through different 403/404 responses.
        raise HTTPException(status_code=404, detail=f"{resource} not found")


def job_to_schema(
    job: Job,
    *,
    current_attempt_engine_mode: WorkerEngineMode | None,
) -> JobRead:
    return JobRead(
        id=job.id,
        experiment_id=job.experiment_id,
        dataset_id=job.dataset_id,
        worker_id=job.worker_id,
        job_name=job.job_name,
        status=JobStatus(job.status),
        config=JobConfig.model_validate(job.config_json),
        test_set_id=job.test_set_id,
        preset_id=job.preset_id,
        sample_plan_sha256=job.sample_plan_sha256,
        priority=job.priority,
        current_epoch=job.current_epoch,
        total_epoch=job.total_epoch,
        attempt_count=job.attempt_count,
        current_attempt_id=job.current_attempt_id,
        current_attempt_engine_mode=current_attempt_engine_mode,
        cancel_requested_at=job.cancel_requested_at,
        error_code=job.error_code,
        error_message=job.error_message,
        started_at=job.started_at,
        completed_at=job.completed_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


async def _jobs_to_schema(session: SessionDep, jobs: Sequence[Job]) -> list[JobRead]:
    """Project Jobs with the engine recorded by their exact current attempts.

    The requested RVC backend is configuration, not evidence of which engine
    executed an attempt. Fetch all referenced attempts in one query and require
    both the attempt and Job identities to match before exposing the mode.
    """

    expected_jobs_by_attempt = {
        job.current_attempt_id: job.id for job in jobs if job.current_attempt_id is not None
    }
    modes_by_job: dict[str, WorkerEngineMode] = {}
    if expected_jobs_by_attempt:
        rows = (
            await session.execute(
                select(JobAttempt.id, JobAttempt.job_id, JobAttempt.engine_mode).where(
                    JobAttempt.id.in_(tuple(expected_jobs_by_attempt))
                )
            )
        ).all()
        for attempt_id, attempt_job_id, raw_engine_mode in rows:
            if expected_jobs_by_attempt.get(attempt_id) != attempt_job_id:
                continue
            modes_by_job[attempt_job_id] = WorkerEngineMode(raw_engine_mode)

    return [
        job_to_schema(
            job,
            current_attempt_engine_mode=modes_by_job.get(job.id),
        )
        for job in jobs
    ]


@router.post(
    "/experiments",
    response_model=ExperimentRead,
    status_code=status.HTTP_201_CREATED,
    responses=_CREATE_EXPERIMENT_RESPONSES,
)
async def create_experiment(
    payload: ExperimentCreate,
    session: SessionDep,
    user: CurrentUserDep,
    mlflow: MlflowDep,
) -> Experiment:
    dataset = await session.scalar(
        select(Dataset).where(Dataset.id == payload.dataset_id).with_for_update()
    )
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset not found")
    require_owner_or_admin(user, dataset.created_by, "dataset")
    if not dataset_ready_for_training(dataset):
        raise HTTPException(status_code=409, detail="dataset is not ready for experiments")
    existing_name = await session.scalar(
        select(Experiment.id)
        .where(
            Experiment.created_by == user.id,
            Experiment.name == payload.name,
        )
        .limit(1)
    )
    if existing_name is not None:
        # This also catches pre-migration duplicate groups whose conflict key
        # remains deliberately NULL to preserve historical IDs and Job links.
        raise HTTPException(
            status_code=409,
            detail="experiment name already exists for owner",
        )
    experiment = Experiment(
        **payload.model_dump(),
        name_conflict_key=payload.name,
        created_by=user.id,
    )
    session.add(experiment)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="experiment name already exists for owner",
        ) from exc
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="experiment.created",
        resource_type="experiment",
        resource_id=experiment.id,
    )
    mlflow_event_key = mlflow.enqueue_experiment_created(session, experiment)
    await session.commit()
    await session.refresh(experiment)
    await mlflow.sync_after_commit(mlflow_event_key)
    return experiment


@router.get("/experiments", response_model=ExperimentList)
async def list_experiments(
    session: SessionDep,
    user: CurrentUserDep,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> ExperimentList:
    filters = [] if user.role == "admin" else [Experiment.created_by == user.id]
    total = await session.scalar(select(func.count()).select_from(Experiment).where(*filters)) or 0
    items = list(
        (
            await session.scalars(
                select(Experiment)
                .where(*filters)
                .order_by(Experiment.created_at.desc(), Experiment.id.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    return ExperimentList(
        items=[ExperimentRead.model_validate(item) for item in items],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/experiments/{experiment_id}",
    response_model=ExperimentRead,
    responses={404: {"description": "Experiment not found or hidden by ownership"}},
)
async def get_experiment(
    experiment_id: str,
    session: SessionDep,
    user: CurrentUserDep,
) -> Experiment:
    experiment = await session.get(Experiment, experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    require_owner_or_admin(user, experiment.created_by, "experiment")
    return experiment


@router.get(
    "/experiments/{experiment_id}/comparison",
    response_model=ExperimentComparisonRead,
    summary="Compare explicitly selected Jobs in one Experiment",
    description=(
        "Select 2-16 Jobs by repeating job_ids. Only each Job's exact current attempt is "
        "projected; allowlisted metric series contain the latest 200 points in chronological "
        "order and report whether older points were truncated."
    ),
    responses={
        404: {"description": "Experiment or selected Job is not visible in this Experiment"},
        409: {"description": "Selected current-attempt ledger is inconsistent"},
        422: {"description": "Two to sixteen distinct explicit Job IDs are required"},
        503: {"description": "Artifact namespace cannot currently be verified"},
    },
)
async def compare_experiment_jobs(
    experiment_id: str,
    request: Request,
    session: SessionDep,
    user: CurrentUserDep,
    response: Response,
    job_ids: Annotated[
        list[str],
        Query(
            min_length=2,
            max_length=16,
            description="Repeat job_ids for each of the 2-16 Jobs to compare.",
        ),
    ],
) -> ExperimentComparisonRead:
    if len(set(job_ids)) != len(job_ids) or any(
        _COMPARISON_JOB_ID.fullmatch(job_id) is None for job_id in job_ids
    ):
        raise HTTPException(
            status_code=422,
            detail="job_ids must contain 2-16 distinct canonical Job IDs",
        )
    experiment = await session.get(Experiment, experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    require_owner_or_admin(user, experiment.created_by, "experiment")
    selected = list((await session.scalars(select(Job).where(Job.id.in_(tuple(job_ids))))).all())
    selected_by_id = {job.id: job for job in selected}
    if len(selected_by_id) != len(job_ids) or any(
        selected_by_id[job_id].experiment_id != experiment.id
        for job_id in job_ids
        if job_id in selected_by_id
    ):
        # Missing and cross-Experiment Job IDs deliberately share one concealed
        # response so this endpoint cannot enumerate another owner's Jobs.
        raise HTTPException(status_code=404, detail="selected jobs not found in experiment")
    ordered_jobs = [selected_by_id[job_id] for job_id in job_ids]
    storage = cast(StorageAdapter, request.app.state.storage)
    try:
        projected = await build_experiment_comparison_jobs(session, storage, ordered_jobs)
    except InvalidExperimentComparisonLedger as exc:
        raise HTTPException(
            status_code=409,
            detail="selected job comparison ledger is inconsistent",
        ) from exc
    except ExperimentComparisonUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="comparison artifact namespace is unavailable",
        ) from exc
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return ExperimentComparisonRead(
        experiment=ExperimentRead.model_validate(experiment),
        jobs=projected,
    )


async def _locked_owned_experiment(
    experiment_id: str,
    *,
    session: SessionDep,
    user: CurrentUserDep,
) -> Experiment:
    filters = [Experiment.id == experiment_id]
    if user.role != "admin":
        filters.append(Experiment.created_by == user.id)
    experiment = await session.scalar(select(Experiment).where(*filters).with_for_update())
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return experiment


@router.patch(
    "/experiments/{experiment_id}",
    response_model=ExperimentRead,
    responses=_UPDATE_EXPERIMENT_RESPONSES,
)
async def update_experiment(
    experiment_id: str,
    payload: ExperimentUpdate,
    session: SessionDep,
    user: CurrentUserDep,
) -> Experiment:
    experiment = await _locked_owned_experiment(
        experiment_id,
        session=session,
        user=user,
    )
    if experiment.row_version != payload.expected_row_version:
        raise HTTPException(
            status_code=409,
            detail="experiment changed; refresh and retry",
        )
    # Dataset and name are intentionally absent from ExperimentUpdate. Both
    # are immutable snapshots: Jobs bind the Dataset, while MLflow projects
    # the Experiment name at creation time and has no safe rename operation.
    if experiment.description == payload.description:
        return experiment
    previous_row_version = experiment.row_version
    experiment.description = payload.description
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="experiment.updated",
        resource_type="experiment",
        resource_id=experiment.id,
        details={
            "changed_fields": ["description"],
            "previous_row_version": previous_row_version,
            "new_row_version": previous_row_version + 1,
        },
    )
    try:
        await session.commit()
    except StaleDataError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="experiment changed; refresh and retry",
        ) from exc
    await session.refresh(experiment)
    return experiment


@router.delete(
    "/experiments/{experiment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    responses=_DELETE_EXPERIMENT_RESPONSES,
)
async def delete_experiment(
    experiment_id: str,
    session: SessionDep,
    user: CurrentUserDep,
    mlflow: MlflowDep,
    expected_row_version: Annotated[int, Query(ge=1, le=2_147_483_647)],
) -> Response:
    experiment = await _locked_owned_experiment(
        experiment_id,
        session=session,
        user=user,
    )
    if experiment.row_version != expected_row_version:
        raise HTTPException(
            status_code=409,
            detail="experiment changed; refresh and retry",
        )
    job_exists = await session.scalar(
        select(Job.id).where(Job.experiment_id == experiment.id).limit(1)
    )
    if job_exists is not None:
        raise HTTPException(
            status_code=409,
            detail="experiment with jobs cannot be deleted",
        )
    projection_exists = await session.scalar(
        select(MlflowSyncEvent.id)
        .where(
            MlflowSyncEvent.aggregate_type == "experiment",
            MlflowSyncEvent.aggregate_id == experiment.id,
        )
        .limit(1)
    )
    if mlflow.enabled or projection_exists is not None:
        raise HTTPException(
            status_code=409,
            detail="experiment with MLflow projection cannot be deleted",
        )
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="experiment.deleted",
        resource_type="experiment",
        resource_id=experiment.id,
        details={
            "dataset_id": experiment.dataset_id,
            "row_version": experiment.row_version,
        },
    )
    await session.delete(experiment)
    try:
        await session.commit()
    except StaleDataError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="experiment changed; refresh and retry",
        ) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="experiment became referenced and cannot be deleted",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/jobs", response_model=JobRead, status_code=status.HTTP_201_CREATED)
async def create_job(
    config: JobConfig,
    session: SessionDep,
    user: CurrentUserDep,
    mlflow: MlflowDep,
    settings: SettingsDep,
) -> JobRead:
    experiment = await session.get(Experiment, config.experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    require_owner_or_admin(user, experiment.created_by, "experiment")
    if experiment.dataset_id != config.dataset_id:
        raise HTTPException(
            status_code=409,
            detail="job dataset does not match experiment dataset",
        )
    dataset = await session.scalar(
        select(Dataset).where(Dataset.id == experiment.dataset_id).with_for_update()
    )
    if dataset is None or not dataset_ready_for_training(dataset):
        raise HTTPException(status_code=409, detail="dataset is not ready for jobs")
    test_set_id: str | None = None
    sample_plan: dict[str, object] | None = None
    sample_plan_sha256: str | None = None
    auto_samples = config.auto_inference_samples
    if auto_samples.enabled:
        if not settings.auto_sample_jobs_enabled:
            raise HTTPException(
                status_code=409,
                detail="automatic sample jobs are disabled until the runtime gate is verified",
            )
        if not config.artifacts.collect_samples:
            raise HTTPException(
                status_code=409,
                detail="automatic samples require collect_samples=true",
            )
        if auto_samples.index_rate > 0 and not config.index.build_index:
            raise HTTPException(
                status_code=409,
                detail="sample index_rate requires index.build_index=true",
            )
        assert auto_samples.test_set_id is not None
        test_set = await session.scalar(
            select(TestSet).where(TestSet.id == auto_samples.test_set_id).with_for_update()
        )
        if test_set is None:
            raise HTTPException(status_code=404, detail="test set not found")
        require_owner_or_admin(user, test_set.created_by, "test set")
        if (
            test_set.status != "ready"
            or test_set.manifest_storage_uri is None
            or test_set.manifest_sha256 is None
            or test_set.item_count < 1
        ):
            raise HTTPException(status_code=409, detail="test set is not ready for jobs")
        test_set_items = list(
            (
                await session.scalars(
                    select(TestSetItem)
                    .where(TestSetItem.test_set_id == test_set.id)
                    .order_by(TestSetItem.sort_order.asc(), TestSetItem.item_key.asc())
                )
            ).all()
        )
        current_manifest_sha256 = canonical_sha256(
            build_test_set_manifest_document(test_set, test_set_items)
        )
        if (
            len(test_set_items) != test_set.item_count
            or current_manifest_sha256 != test_set.manifest_sha256
        ):
            raise HTTPException(
                status_code=409,
                detail="test set manifest no longer matches its immutable ledger",
            )
        inference_config = InferencePresetConfig.model_validate(
            auto_samples.model_dump(
                mode="json",
                exclude={"enabled", "test_set_id"},
            )
        )
        sample_plan = build_sample_plan_document(
            test_set,
            test_set_items,
            inference_config,
        )
        sample_plan_sha256 = canonical_sha256(sample_plan)
        test_set_id = test_set.id
    job = Job(
        experiment_id=experiment.id,
        dataset_id=experiment.dataset_id,
        job_name=config.job_name,
        status=JobStatus.QUEUED.value,
        config_json=config.model_dump(mode="json"),
        test_set_id=test_set_id,
        preset_id=None,
        sample_plan_json=sample_plan,
        sample_plan_sha256=sample_plan_sha256,
        priority=config.resource.priority,
        total_epoch=config.training.epochs,
    )
    session.add(job)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="job name already exists in experiment"
        ) from exc
    session.add(
        JobStatusEvent(
            job_id=job.id,
            previous_status=None,
            status=JobStatus.QUEUED.value,
            occurred_at=utc_now(),
            source="manager",
        )
    )
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="job.created",
        resource_type="job",
        resource_id=job.id,
        details={
            "test_set_id": test_set_id,
            "sample_plan_sha256": sample_plan_sha256,
        },
    )
    mlflow_event_key = await mlflow.enqueue_job_created(session, job)
    await session.commit()
    await session.refresh(job)
    await mlflow.sync_after_commit(mlflow_event_key)
    return (await _jobs_to_schema(session, [job]))[0]


@router.get("/jobs", response_model=JobList)
async def list_jobs(
    session: SessionDep,
    user: CurrentUserDep,
    experiment_id: str | None = None,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> JobList:
    filters = []
    if experiment_id:
        filters.append(Job.experiment_id == experiment_id)
    if job_status:
        filters.append(Job.status == job_status.value)
    if user.role != "admin":
        filters.append(Experiment.created_by == user.id)
    total = (
        await session.scalar(
            select(func.count())
            .select_from(Job)
            .join(Experiment, Job.experiment_id == Experiment.id)
            .where(*filters)
        )
        or 0
    )
    jobs = list(
        (
            await session.scalars(
                select(Job)
                .join(Experiment, Job.experiment_id == Experiment.id)
                .where(*filters)
                .order_by(Job.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    projected_jobs = await _jobs_to_schema(session, jobs)
    return JobList(
        items=projected_jobs,
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/jobs/{job_id}", response_model=JobRead)
async def get_job(job_id: str, session: SessionDep, user: CurrentUserDep) -> JobRead:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    experiment = await session.get(Experiment, job.experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="job not found")
    require_owner_or_admin(user, experiment.created_by, "job")
    return (await _jobs_to_schema(session, [job]))[0]


@router.post("/jobs/{job_id}/cancel", response_model=JobRead)
async def cancel_job(job_id: str, session: SessionDep, user: CurrentUserDep) -> JobRead:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    experiment = await session.get(Experiment, job.experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="job not found")
    require_owner_or_admin(user, experiment.created_by, "job")
    current = JobStatus(job.status)
    if current in TERMINAL_JOB_STATUSES:
        raise HTTPException(status_code=409, detail="terminal job cannot be cancelled")
    now = utc_now()
    if current is JobStatus.QUEUED:
        validate_job_transition(current, JobStatus.CANCELLED)
        job.status = JobStatus.CANCELLED.value
        session.add(
            JobStatusEvent(
                job_id=job.id,
                previous_status=current.value,
                status=JobStatus.CANCELLED.value,
                occurred_at=now,
                source="manager",
            )
        )
    else:
        job.cancel_requested_at = now
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="job.cancel_requested",
        resource_type="job",
        resource_id=job.id,
        details={"previous_status": current.value},
    )
    await session.commit()
    await session.refresh(job)
    return (await _jobs_to_schema(session, [job]))[0]


@router.post("/jobs/{job_id}/retry", response_model=JobRead)
async def retry_job(job_id: str, session: SessionDep, user: CurrentUserDep) -> JobRead:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    experiment = await session.get(Experiment, job.experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="job not found")
    require_owner_or_admin(user, experiment.created_by, "job")
    if JobStatus(job.status) is not JobStatus.FAILED:
        raise HTTPException(status_code=409, detail="only failed jobs can be retried")
    now = utc_now()
    validate_job_transition(JobStatus.FAILED, JobStatus.RETRYING)
    session.add(
        JobStatusEvent(
            job_id=job.id,
            attempt_id=job.current_attempt_id,
            previous_status=JobStatus.FAILED.value,
            status=JobStatus.RETRYING.value,
            occurred_at=now,
            source="manager",
        )
    )
    validate_job_transition(JobStatus.RETRYING, JobStatus.QUEUED)
    session.add(
        JobStatusEvent(
            job_id=job.id,
            attempt_id=job.current_attempt_id,
            previous_status=JobStatus.RETRYING.value,
            status=JobStatus.QUEUED.value,
            occurred_at=now,
            source="manager",
        )
    )
    job.status = JobStatus.QUEUED.value
    job.worker_id = None
    job.current_attempt_id = None
    job.cancel_requested_at = None
    job.error_code = None
    job.error_message = None
    job.started_at = None
    job.completed_at = None
    job.current_epoch = None
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="job.retried",
        resource_type="job",
        resource_id=job.id,
        details={"attempt_count": job.attempt_count},
    )
    await session.commit()
    await session.refresh(job)
    return (await _jobs_to_schema(session, [job]))[0]
