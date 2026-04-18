from __future__ import annotations

import hashlib
import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from rvc_orchestrator_contracts import (
    TERMINAL_JOB_STATUSES,
    DatasetTransfer,
    InferencePresetConfig,
    JobClaim,
    JobConfig,
    JobStatus,
    TestSetTransfer,
    TestSetTransferItem,
    WorkerCapabilities,
    WorkerEngineMode,
    utc_now,
    validate_job_transition,
)

from ..audit import add_audit_event
from ..config import Settings
from ..models import (
    Dataset,
    DatasetUploadSession,
    Job,
    JobAttempt,
    JobLease,
    JobStatusEvent,
    TestSet,
    TestSetItem,
    TestSetItemUploadSession,
    Worker,
    new_id,
)
from .datasets import dataset_ready_for_training
from .test_sets import (
    build_sample_plan_document,
    build_test_set_manifest_document,
    canonical_json,
    canonical_sha256,
    test_set_item_object_key,
    test_set_manifest_object_key,
)


class ClaimStoragePort(Protocol):
    """Narrow storage view used while proving claim-time transfer snapshots."""

    backend: Literal["local", "s3"]
    namespace_fingerprint: str

    def storage_uri(self, object_key: str) -> str: ...

    def stream_object(
        self,
        object_key: str,
        *,
        chunk_size: int,
        max_bytes: int,
    ) -> AsyncIterator[bytes]: ...


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def lease_expiry(settings: Settings, *, now: datetime | None = None) -> datetime:
    return (now or utc_now()) + timedelta(seconds=settings.lease_seconds)


async def recover_expired_leases(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
    batch_limit: int = 100,
) -> int:
    """Close abandoned attempts and safely make eligible Jobs claimable again.

    A lease expiry alone is not enough to reassign work: a partitioned Worker can
    still be terminating its process group. Recovery therefore waits until the
    Worker is also beyond the configured offline window. Locked attempt rows make
    the operation idempotent when several Workers poll concurrently.
    """

    recovered_at = now or utc_now()
    offline_before = recovered_at - timedelta(seconds=settings.worker_offline_seconds)
    recoverable_statuses = [
        item.value
        for item in JobStatus
        if item
        not in {
            JobStatus.QUEUED,
            JobStatus.RETRYING,
            *TERMINAL_JOB_STATUSES,
        }
    ]
    leases = list(
        (
            await session.scalars(
                select(JobLease)
                .join(Job, Job.current_attempt_id == JobLease.attempt_id)
                .join(JobAttempt, JobAttempt.id == JobLease.attempt_id)
                .join(Worker, Worker.id == JobLease.worker_id)
                .where(
                    JobLease.expires_at <= recovered_at,
                    JobAttempt.finished_at.is_(None),
                    Job.status.in_(recoverable_statuses),
                )
                .order_by(JobLease.expires_at.asc(), JobLease.id.asc())
                .limit(batch_limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    recovered = 0
    for lease in leases:
        worker = await session.get(Worker, lease.worker_id)
        job = await session.get(Job, lease.job_id)
        attempt = await session.get(JobAttempt, lease.attempt_id)
        if worker is None or job is None or attempt is None:
            continue
        if job.current_attempt_id != attempt.id or attempt.finished_at is not None:
            continue
        if worker.last_heartbeat_at is not None and as_utc(worker.last_heartbeat_at) > as_utc(
            offline_before
        ):
            continue

        previous = JobStatus(job.status)
        cancelled = job.cancel_requested_at is not None
        terminal = JobStatus.CANCELLED if cancelled else JobStatus.FAILED
        # Every leased execution state is required to admit failure/cancellation.
        # Validation here also fails closed if persisted state was corrupted.
        validate_job_transition(previous, terminal)
        lease.active = False
        lease.released_at = recovered_at
        attempt.status = terminal.value
        attempt.finished_at = recovered_at
        if not cancelled:
            attempt.error_code = "worker_lease_expired"
            attempt.error_message = "Worker became offline and its Job lease expired"
        session.add(
            JobStatusEvent(
                job_id=job.id,
                attempt_id=attempt.id,
                previous_status=previous.value,
                status=terminal.value,
                occurred_at=recovered_at,
                source="manager",
            )
        )
        job.status = terminal.value
        if cancelled:
            job.error_code = None
            job.error_message = None
        else:
            job.error_code = attempt.error_code
            job.error_message = attempt.error_message

        auto_requeue = (
            not cancelled
            and settings.lease_recovery_max_attempts > 0
            and job.attempt_count < settings.lease_recovery_max_attempts
        )
        if auto_requeue:
            session.add_all(
                [
                    JobStatusEvent(
                        job_id=job.id,
                        attempt_id=attempt.id,
                        previous_status=JobStatus.FAILED.value,
                        status=JobStatus.RETRYING.value,
                        occurred_at=recovered_at,
                        source="manager",
                    ),
                    JobStatusEvent(
                        job_id=job.id,
                        attempt_id=attempt.id,
                        previous_status=JobStatus.RETRYING.value,
                        status=JobStatus.QUEUED.value,
                        occurred_at=recovered_at,
                        source="manager",
                    ),
                ]
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
        if worker.current_job_id == job.id:
            worker.current_job_id = None
            worker.status = "idle"
        add_audit_event(
            session,
            actor_type="system",
            action="job.lease_recovered",
            resource_type="job",
            resource_id=job.id,
            details={
                "attempt_id": attempt.id,
                "worker_id": worker.id,
                "outcome": job.status,
                "automatic_requeue": auto_requeue,
            },
        )
        recovered += 1

    if recovered:
        await session.commit()
    return recovered


def worker_can_run(
    config: JobConfig,
    capabilities: WorkerCapabilities,
    *,
    allow_fake_workers: bool = False,
    approved_runtime_bundles: frozenset[tuple[str, str]] = frozenset(),
) -> bool:
    is_fake = capabilities.engine_mode is WorkerEngineMode.FAKE
    if is_fake and not allow_fake_workers:
        return False
    if (
        not is_fake
        and config.rvc_backend.rvc_commit_hash is not None
        and capabilities.rvc_commit_hash != config.rvc_backend.rvc_commit_hash
    ):
        return False
    if not is_fake and not capabilities.rvc_assets_ready:
        return False
    if config.model.version not in capabilities.supported_rvc_versions:
        return False
    method = config.f0_extraction.training_f0_method
    if method is not None and method not in capabilities.supported_training_f0_methods:
        return False
    sample = config.auto_inference_samples
    if sample.enabled:
        if (
            is_fake
            or not capabilities.fixed_test_set_inference_ready
            or sample.inference_f0_method not in capabilities.supported_inference_f0_methods
            or capabilities.runtime_image_digest is None
            or capabilities.runtime_asset_manifest_sha256 is None
            or (
                capabilities.runtime_image_digest,
                capabilities.runtime_asset_manifest_sha256,
            )
            not in approved_runtime_bundles
        ):
            return False
    worker_tags = set(capabilities.tags)
    if not set(config.resource.preferred_worker_tags).issubset(worker_tags):
        return False
    if config.resource.min_vram_gb and not is_fake:
        required_mb = config.resource.min_vram_gb * 1024
        if not any(gpu.total_vram_mb >= required_mb for gpu in capabilities.gpus):
            return False
    requested_gpu_count = len(config.training.gpu_ids)
    if not is_fake and len(capabilities.gpus) < requested_gpu_count:
        return False
    return True


async def _object_matches_exact_bytes(
    storage: ClaimStoragePort,
    object_key: str,
    *,
    expected_bytes: bytes,
    chunk_size: int,
) -> bool:
    digest = hashlib.sha256()
    total = 0
    try:
        async for chunk in storage.stream_object(
            object_key,
            chunk_size=chunk_size,
            max_bytes=len(expected_bytes) + 1,
        ):
            total += len(chunk)
            digest.update(chunk)
    except Exception:
        return False
    return (
        total == len(expected_bytes)
        and digest.hexdigest() == hashlib.sha256(expected_bytes).hexdigest()
    )


def _resolved_inference_config(config: JobConfig) -> InferencePresetConfig:
    return InferencePresetConfig.model_validate(
        config.auto_inference_samples.model_dump(
            mode="json",
            exclude={"enabled", "test_set_id"},
        )
    )


def _upload_matches_test_set_item(
    upload: TestSetItemUploadSession,
    item: TestSetItem,
    *,
    storage: ClaimStoragePort,
) -> bool:
    expected_key = test_set_item_object_key(item.test_set_id, upload.id)
    try:
        expected_uri = storage.storage_uri(expected_key)
    except Exception:
        return False
    return bool(
        upload.status == "completed"
        and upload.test_set_id == item.test_set_id
        and upload.item_key == item.item_key
        and upload.display_name == item.display_name
        and upload.sort_order == item.sort_order
        and upload.filename == item.original_filename
        and upload.content_type == item.mime_type == "audio/wav"
        and upload.expected_size_bytes == item.size_bytes
        and upload.expected_sha256 == item.sha256
        and upload.license_reference == item.license_reference
        and upload.provenance_reference == item.provenance_reference
        and upload.canonical_object_key == expected_key
        and upload.storage_backend == storage.backend
        and upload.storage_namespace_sha256 == storage.namespace_fingerprint
        and item.storage_uri == expected_uri
    )


async def verified_test_set_transfer(
    session: AsyncSession,
    job: Job,
    config: JobConfig,
    *,
    storage: ClaimStoragePort,
    settings: Settings,
) -> TestSetTransfer | None:
    sample = config.auto_inference_samples
    if (
        not sample.enabled
        or sample.test_set_id is None
        or job.test_set_id != sample.test_set_id
        or job.sample_plan_json is None
        or job.sample_plan_sha256 is None
    ):
        return None
    test_set = await session.get(TestSet, job.test_set_id)
    if (
        test_set is None
        or test_set.status != "ready"
        or test_set.manifest_sha256 is None
        or test_set.manifest_storage_uri is None
        or test_set.item_count < 1
    ):
        return None
    items = list(
        (
            await session.scalars(
                select(TestSetItem)
                .where(TestSetItem.test_set_id == test_set.id)
                .order_by(
                    TestSetItem.sort_order.asc(),
                    TestSetItem.item_key.asc(),
                    TestSetItem.id.asc(),
                )
            )
        ).all()
    )
    if (
        len(items) != test_set.item_count
        or len(items) > settings.test_set_max_items
        or sum(item.size_bytes for item in items) > settings.test_set_max_total_bytes
        or math.fsum(item.duration_seconds for item in items)
        > settings.test_set_max_total_duration_seconds
    ):
        return None
    inference = _resolved_inference_config(config)
    manifest_document = build_test_set_manifest_document(test_set, items)
    manifest_bytes = canonical_json(manifest_document)
    if canonical_sha256(manifest_document) != test_set.manifest_sha256:
        return None
    manifest_key = test_set_manifest_object_key(test_set.id)
    try:
        if test_set.manifest_storage_uri != storage.storage_uri(manifest_key):
            return None
    except Exception:
        return None
    if not await _object_matches_exact_bytes(
        storage,
        manifest_key,
        expected_bytes=manifest_bytes,
        chunk_size=settings.artifact_stream_chunk_bytes,
    ):
        return None
    plan = build_sample_plan_document(test_set, items, inference)
    plan_sha256 = canonical_sha256(plan)
    if plan != job.sample_plan_json or plan_sha256 != job.sample_plan_sha256:
        return None

    uploads = list(
        (
            await session.scalars(
                select(TestSetItemUploadSession)
                .where(
                    TestSetItemUploadSession.test_set_id == test_set.id,
                    TestSetItemUploadSession.status == "completed",
                )
                .order_by(TestSetItemUploadSession.finalized_at.desc())
            )
        ).all()
    )
    if len(uploads) != len(items):
        return None
    uploads_by_item_key: dict[str, TestSetItemUploadSession] = {}
    for upload in uploads:
        if upload.item_key in uploads_by_item_key:
            return None
        uploads_by_item_key[upload.item_key] = upload
    descriptors: list[TestSetTransferItem] = []
    for item in items:
        selected_upload = uploads_by_item_key.get(item.item_key)
        if selected_upload is None or not _upload_matches_test_set_item(
            selected_upload,
            item,
            storage=storage,
        ):
            return None
        descriptors.append(
            TestSetTransferItem(
                test_set_item_id=item.id,
                item_key=item.item_key,
                sort_order=item.sort_order,
                download_path=(f"/api/v1/workers/jobs/{job.id}/test-set/items/{item.id}"),
                filename=f"{item.id}.wav",
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                sample_rate_hz=item.sample_rate_hz,
                channels=item.channels,
                duration_seconds=item.duration_seconds,
            )
        )
    return TestSetTransfer(
        test_set_id=test_set.id,
        family_id=test_set.family_id,
        revision=test_set.revision,
        manifest_sha256=test_set.manifest_sha256,
        sample_plan_sha256=job.sample_plan_sha256,
        inference_config=inference,
        inference_config_sha256=canonical_sha256(inference.model_dump(mode="json")),
        items=descriptors,
    )


async def _verified_dataset_transfer(
    session: AsyncSession,
    dataset: Dataset,
    *,
    job_id: str,
    settings: Settings,
    storage: ClaimStoragePort,
) -> DatasetTransfer | None:
    if (
        dataset.status != "ready"
        or not dataset.is_usable
        or dataset.prepared_flat_size_bytes is None
        or dataset.prepared_flat_size_bytes <= 0
        or dataset.prepared_flat_sha256 is None
    ):
        return None
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
    if (
        upload is None
        or upload.storage_backend != settings.resolved_storage_backend
        or upload.storage_backend != storage.backend
        or upload.storage_namespace_sha256 != storage.namespace_fingerprint
    ):
        return None
    try:
        if dataset.flat_storage_uri != storage.storage_uri(upload.prepared_flat_object_key):
            return None
    except Exception:
        return None
    return DatasetTransfer(
        dataset_id=dataset.id,
        download_path=f"/api/v1/workers/jobs/{job_id}/dataset",
        size_bytes=dataset.prepared_flat_size_bytes,
        sha256=dataset.prepared_flat_sha256,
    )


async def claim_job(
    session: AsyncSession,
    worker: Worker,
    capabilities: WorkerCapabilities,
    settings: Settings,
    *,
    storage: ClaimStoragePort,
) -> JobClaim | None:
    await recover_expired_leases(session, settings)
    locked_worker = await session.scalar(
        select(Worker)
        .where(Worker.id == worker.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_worker is None or not locked_worker.is_active:
        raise HTTPException(status_code=401, detail="invalid worker token")
    worker = locked_worker
    if worker.pending_token_hash is not None:
        raise HTTPException(status_code=409, detail="worker token rotation is pending")
    if worker.status == "draining":
        return None
    if worker.current_job_id is not None:
        raise HTTPException(status_code=409, detail="worker already has an active job")

    candidates = list(
        (
            await session.scalars(
                select(Job)
                .where(Job.status == JobStatus.QUEUED.value)
                .order_by(Job.priority.desc(), Job.created_at.asc(), Job.id.asc())
                .limit(50)
            )
        ).all()
    )
    now = utc_now()
    for job in candidates:
        config = JobConfig.model_validate(job.config_json)
        if not worker_can_run(
            config,
            capabilities,
            allow_fake_workers=settings.allow_fake_workers,
            approved_runtime_bundles=settings.approved_sample_runtime_bundles,
        ):
            continue
        dataset = await session.scalar(
            select(Dataset).where(Dataset.id == job.dataset_id).with_for_update(skip_locked=True)
        )
        if dataset is None or not dataset_ready_for_training(dataset):
            continue
        # URI-seeded legacy fixtures exist only in the isolated test policy.
        # They never receive a transfer path and cannot enter production.
        test_legacy_compatibility = (
            settings.environment == "test" and dataset.status == "legacy_imported"
        )
        dataset_transfer = await _verified_dataset_transfer(
            session,
            dataset,
            job_id=job.id,
            settings=settings,
            storage=storage,
        )
        if dataset_transfer is None and not test_legacy_compatibility:
            continue
        test_set_transfer = None
        if config.auto_inference_samples.enabled:
            test_set_transfer = await verified_test_set_transfer(
                session,
                job,
                config,
                storage=storage,
                settings=settings,
            )
            if test_set_transfer is None:
                continue
        attempt_id = new_id()
        lease_id = new_id()
        attempt_number = job.attempt_count + 1
        claimed = await session.execute(
            update(Job)
            .where(
                Job.id == job.id,
                Job.status == JobStatus.QUEUED.value,
                Job.row_version == job.row_version,
            )
            .values(
                status=JobStatus.ASSIGNED.value,
                worker_id=worker.id,
                attempt_count=attempt_number,
                current_attempt_id=attempt_id,
                current_epoch=None,
                started_at=now,
                updated_at=now,
                row_version=job.row_version + 1,
            )
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            continue

        expires_at = lease_expiry(settings, now=now)
        session.add_all(
            [
                JobAttempt(
                    id=attempt_id,
                    job_id=job.id,
                    worker_id=worker.id,
                    attempt_number=attempt_number,
                    engine_mode=capabilities.engine_mode.value,
                    rvc_commit_hash=capabilities.rvc_commit_hash,
                    execution_provenance_version="worker-claim-v1",
                    runtime_image_digest=capabilities.runtime_image_digest,
                    runtime_asset_manifest_sha256=capabilities.runtime_asset_manifest_sha256,
                    status=JobStatus.ASSIGNED.value,
                    started_at=now,
                ),
                JobLease(
                    id=lease_id,
                    job_id=job.id,
                    attempt_id=attempt_id,
                    worker_id=worker.id,
                    expires_at=expires_at,
                    last_renewed_at=now,
                    active=True,
                ),
                JobStatusEvent(
                    job_id=job.id,
                    attempt_id=attempt_id,
                    previous_status=JobStatus.QUEUED.value,
                    status=JobStatus.ASSIGNED.value,
                    occurred_at=now,
                    source="manager",
                ),
            ]
        )
        worker.status = "busy"
        worker.current_job_id = job.id
        worker.capabilities_json = capabilities.model_dump(mode="json")
        worker.last_heartbeat_at = now
        try:
            response = JobClaim(
                job_id=job.id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                lease_id=lease_id,
                lease_expires_at=expires_at,
                config=config,
                dataset_transfer=dataset_transfer,
                test_set_transfer=test_set_transfer,
            )
        except ValidationError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=503,
                detail="job claim transfer validation failed",
            ) from exc
        try:
            await session.commit()
        except StaleDataError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=409,
                detail="worker or job changed during claim commit",
            ) from exc
        return response
    await session.rollback()
    return None


async def require_active_lease(
    session: AsyncSession,
    *,
    worker_id: str,
    job_id: str,
    lease_id: str,
    for_update: bool = False,
) -> JobLease:
    statement = select(JobLease).where(
        JobLease.id == lease_id,
        JobLease.job_id == job_id,
        JobLease.worker_id == worker_id,
        JobLease.active.is_(True),
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    lease = await session.scalar(statement)
    if lease is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="invalid job lease")
    if as_utc(lease.expires_at) <= utc_now():
        # Keep the abandoned assignment discoverable until the Worker has also
        # crossed the offline grace window. The recovery reaper then closes the
        # attempt and clears both sides of the assignment atomically.
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="job lease expired")
    return lease
