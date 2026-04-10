from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from rvc_orchestrator_contracts import (
    TERMINAL_JOB_STATUSES,
    ArtifactType,
    JobConfig,
    JobStatus,
    SampleMetricsEvidence,
    WorkerEngineMode,
)

from ..models import Artifact, ArtifactUploadSession, Job, JobAttempt, Metric, Sample
from ..schemas import (
    ExperimentComparisonArtifactRead,
    ExperimentComparisonAttemptRead,
    ExperimentComparisonAvailability,
    ExperimentComparisonJobRead,
    ExperimentComparisonMetricPoint,
    ExperimentComparisonMetricSeries,
    ExperimentComparisonSampleRead,
)
from ..storage import StorageAdapter
from .samples import (
    SampleStorageUnavailable,
    VerifiedArtifactBinding,
    _verified_binding_from_rows,
    artifact_provenance_matches,
)

METRIC_POINT_LIMIT_PER_KEY = 200

_TRAINING_METRIC_KEYS = frozenset(
    {
        "current_epoch",
        "epoch_completed",
        "epoch_progress_percent",
        "grad_norm_d",
        "grad_norm_g",
        "learning_rate",
        "loss_d_total",
        "loss_fm",
        "loss_g_adversarial",
        "loss_g_total",
        "loss_kl",
        "loss_mel",
        "step",
        "total_epoch",
    }
)
_SYSTEM_METRIC_KEYS = frozenset(
    {
        "system.disk_free_bytes",
        "system.gpu.count",
        "system.gpu.telemetry_available",
        *(
            f"system.gpu.{gpu_index}.{suffix}"
            for gpu_index in range(64)
            for suffix in (
                "temperature_c",
                "utilization_percent",
                "vram_total_mb",
                "vram_used_mb",
            )
        ),
    }
)
ALLOWED_COMPARISON_METRIC_KEYS = tuple(sorted(_TRAINING_METRIC_KEYS | _SYSTEM_METRIC_KEYS))
_COMPARISON_ARTIFACT_TYPES = (
    ArtifactType.FINAL_SMALL_MODEL.value,
    ArtifactType.FINAL_INDEX.value,
    ArtifactType.SAMPLE.value,
)


class InvalidExperimentComparisonLedger(ValueError):
    """A selected Job's immutable/current ledger cannot be projected safely."""


class ExperimentComparisonUnavailable(RuntimeError):
    """The configured artifact namespace cannot verify comparison availability."""


def _invalid() -> InvalidExperimentComparisonLedger:
    return InvalidExperimentComparisonLedger("experiment comparison ledger is inconsistent")


def _contains_non_finite(value: object) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_contains_non_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_non_finite(item) for item in value)
    return False


def _job_fingerprint(job: Job) -> tuple[object, ...]:
    return (
        job.row_version,
        job.status,
        job.worker_id,
        job.current_attempt_id,
        job.current_epoch,
        job.total_epoch,
        job.attempt_count,
    )


def _attempt_fingerprint(attempt: JobAttempt) -> tuple[object, ...]:
    return (
        attempt.job_id,
        attempt.worker_id,
        attempt.attempt_number,
        attempt.engine_mode,
        attempt.rvc_commit_hash,
        attempt.execution_provenance_version,
        attempt.runtime_image_digest,
        attempt.runtime_asset_manifest_sha256,
        attempt.status,
        attempt.started_at,
        attempt.finished_at,
    )


def _validated_job_config(job: Job) -> JobConfig:
    if _contains_non_finite(job.config_json):
        raise _invalid()
    try:
        config = JobConfig.model_validate(job.config_json)
    except (TypeError, ValueError, ValidationError) as exc:
        raise _invalid() from exc
    if (
        config.job_name != job.job_name
        or config.experiment_id != job.experiment_id
        or config.dataset_id != job.dataset_id
        or config.training.epochs != job.total_epoch
        or config.resource.priority != job.priority
    ):
        raise _invalid()
    samples = config.auto_inference_samples
    if samples.enabled:
        if job.test_set_id is None or samples.test_set_id != job.test_set_id:
            raise _invalid()
    elif job.test_set_id is not None:
        raise _invalid()
    if job.total_epoch < 1 or (
        job.current_epoch is not None and not 0 <= job.current_epoch <= job.total_epoch
    ):
        raise _invalid()
    return config


def _current_pair_conditions(
    jobs: Sequence[Job],
    attempts_by_job: dict[str, JobAttempt],
) -> list[Any]:
    return [
        and_(Metric.job_id == job.id, Metric.attempt_id == attempt.id)
        for job in jobs
        if (attempt := attempts_by_job.get(job.id)) is not None
    ]


async def _validated_current_attempts(
    session: AsyncSession,
    jobs: Sequence[Job],
) -> tuple[dict[str, JobAttempt], dict[str, ExperimentComparisonAttemptRead | None]]:
    current_ids = tuple(
        job.current_attempt_id for job in jobs if job.current_attempt_id is not None
    )
    attempts = (
        list(
            (await session.scalars(select(JobAttempt).where(JobAttempt.id.in_(current_ids)))).all()
        )
        if current_ids
        else []
    )
    attempts_by_id = {attempt.id: attempt for attempt in attempts}
    attempts_by_job: dict[str, JobAttempt] = {}
    reads_by_job: dict[str, ExperimentComparisonAttemptRead | None] = {}
    terminal_values = {item.value for item in TERMINAL_JOB_STATUSES}
    for job in jobs:
        try:
            job_status = JobStatus(job.status)
        except ValueError as exc:
            raise _invalid() from exc
        if job.current_attempt_id is None:
            if job.status not in {JobStatus.QUEUED.value, JobStatus.CANCELLED.value}:
                raise _invalid()
            if job.worker_id is not None:
                raise _invalid()
            reads_by_job[job.id] = None
            continue
        attempt = attempts_by_id.get(job.current_attempt_id)
        if (
            attempt is None
            or attempt.job_id != job.id
            or attempt.worker_id != job.worker_id
            or attempt.attempt_number != job.attempt_count
            or attempt.attempt_number < 1
            or attempt.status != job.status
        ):
            raise _invalid()
        if attempt.status in terminal_values:
            if attempt.finished_at is None or attempt.finished_at < attempt.started_at:
                raise _invalid()
        elif attempt.finished_at is not None:
            raise _invalid()
        try:
            engine_mode = WorkerEngineMode(attempt.engine_mode)
            attempt_status = JobStatus(attempt.status)
        except ValueError as exc:
            raise _invalid() from exc
        attempts_by_job[job.id] = attempt
        reads_by_job[job.id] = ExperimentComparisonAttemptRead(
            id=attempt.id,
            attempt_number=attempt.attempt_number,
            engine_mode=engine_mode,
            status=attempt_status,
            started_at=attempt.started_at,
            finished_at=attempt.finished_at,
        )
        if attempt_status is not job_status:
            raise _invalid()
    return attempts_by_job, reads_by_job


async def _metric_series_by_job(
    session: AsyncSession,
    jobs: Sequence[Job],
    attempts_by_job: dict[str, JobAttempt],
) -> dict[str, list[ExperimentComparisonMetricSeries]]:
    pairs = _current_pair_conditions(jobs, attempts_by_job)
    result: dict[str, list[ExperimentComparisonMetricSeries]] = {job.id: [] for job in jobs}
    if not pairs:
        return result
    relevant = (
        or_(*pairs),
        Metric.key.in_(ALLOWED_COMPARISON_METRIC_KEYS),
    )
    invalid_metric = await session.scalar(
        select(Metric.id)
        .where(
            *relevant,
            or_(
                Metric.sequence < 0,
                Metric.epoch < 0,
                Metric.step < 0,
                Metric.value > sys.float_info.max,
                Metric.value < -sys.float_info.max,
            ),
        )
        .limit(1)
    )
    if invalid_metric is not None:
        raise _invalid()
    ranked = (
        select(
            Metric.job_id.label("job_id"),
            Metric.attempt_id.label("attempt_id"),
            Metric.sequence.label("sequence"),
            Metric.epoch.label("epoch"),
            Metric.step.label("step"),
            Metric.key.label("key"),
            Metric.value.label("value"),
            Metric.occurred_at.label("occurred_at"),
            func.count().over(partition_by=(Metric.job_id, Metric.key)).label("series_total"),
            func.row_number()
            .over(
                partition_by=(Metric.job_id, Metric.key),
                order_by=(Metric.sequence.desc(), Metric.id.desc()),
            )
            .label("series_rank"),
        )
        .where(*relevant)
        .subquery()
    )
    rows = list(
        (
            await session.execute(
                select(ranked)
                .where(ranked.c.series_rank <= METRIC_POINT_LIMIT_PER_KEY)
                .order_by(ranked.c.job_id.asc(), ranked.c.key.asc(), ranked.c.sequence.asc())
            )
        ).mappings()
    )
    points_by_job_key: dict[tuple[str, str], list[ExperimentComparisonMetricPoint]] = {}
    totals_by_job_key: dict[tuple[str, str], int] = {}
    for row in rows:
        job_id = str(row["job_id"])
        attempt = attempts_by_job.get(job_id)
        value = float(row["value"])
        if attempt is None or row["attempt_id"] != attempt.id or not math.isfinite(value):
            raise _invalid()
        key = str(row["key"])
        series_key = (job_id, key)
        total = int(row["series_total"])
        previous_total = totals_by_job_key.setdefault(series_key, total)
        if total < 1 or previous_total != total:
            raise _invalid()
        points_by_job_key.setdefault(series_key, []).append(
            ExperimentComparisonMetricPoint(
                sequence=int(row["sequence"]),
                epoch=row["epoch"],
                step=row["step"],
                value=value,
                occurred_at=row["occurred_at"],
            )
        )
    for job in jobs:
        result[job.id] = [
            ExperimentComparisonMetricSeries(
                key=key,
                total_points=totals_by_job_key[(job_id, key)],
                truncated=totals_by_job_key[(job_id, key)] > len(points),
                points=points,
            )
            for (job_id, key), points in sorted(points_by_job_key.items())
            if job_id == job.id
        ]
    return result


def _artifact_to_read(binding: VerifiedArtifactBinding) -> ExperimentComparisonArtifactRead:
    artifact = binding.artifact
    if (
        not artifact.filename
        or artifact.filename in {".", ".."}
        or "/" in artifact.filename
        or "\\" in artifact.filename
        or any(ord(character) < 32 or ord(character) == 127 for character in artifact.filename)
        or len(artifact.sha256) != 64
        or artifact.sha256.lower() != artifact.sha256
        or any(character not in "0123456789abcdef" for character in artifact.sha256)
    ):
        raise _invalid()
    return ExperimentComparisonArtifactRead(
        id=artifact.id,
        filename=artifact.filename,
        size_bytes=artifact.size_bytes,
        sha256=artifact.sha256,
    )


async def _verified_bindings_by_job(
    session: AsyncSession,
    storage: StorageAdapter,
    jobs: Sequence[Job],
    attempts_by_job: dict[str, JobAttempt],
) -> tuple[
    dict[str, ExperimentComparisonArtifactRead | None],
    dict[str, ExperimentComparisonArtifactRead | None],
    dict[str, dict[str, VerifiedArtifactBinding]],
]:
    model_by_job: dict[str, ExperimentComparisonArtifactRead | None] = {
        job.id: None for job in jobs
    }
    index_by_job: dict[str, ExperimentComparisonArtifactRead | None] = {
        job.id: None for job in jobs
    }
    bindings_by_job: dict[str, dict[str, VerifiedArtifactBinding]] = {job.id: {} for job in jobs}
    pairs = [
        and_(Artifact.job_id == job.id, Artifact.attempt_id == attempt.id)
        for job in jobs
        if (attempt := attempts_by_job.get(job.id)) is not None
    ]
    if not pairs:
        return model_by_job, index_by_job, bindings_by_job
    rows = list(
        (
            await session.execute(
                select(Artifact, ArtifactUploadSession)
                .join(ArtifactUploadSession, ArtifactUploadSession.artifact_id == Artifact.id)
                .where(
                    or_(*pairs),
                    Artifact.artifact_type.in_(_COMPARISON_ARTIFACT_TYPES),
                    ArtifactUploadSession.status == "completed",
                )
                .order_by(Artifact.job_id.asc(), Artifact.artifact_type.asc(), Artifact.id.asc())
            )
        ).all()
    )
    selected_by_job_type: dict[tuple[str, str], list[VerifiedArtifactBinding]] = {}
    for artifact, upload in rows:
        attempt = attempts_by_job.get(artifact.job_id)
        if attempt is None:
            raise _invalid()
        try:
            artifact_type = ArtifactType(artifact.artifact_type)
            binding = _verified_binding_from_rows(
                artifact,
                upload,
                storage,
                job_id=artifact.job_id,
                attempt_id=attempt.id,
                artifact_type=artifact_type,
            )
        except SampleStorageUnavailable as exc:
            raise ExperimentComparisonUnavailable(
                "experiment comparison artifact namespace is unavailable"
            ) from exc
        except ValueError as exc:
            raise _invalid() from exc
        if binding is None:
            raise _invalid()
        if artifact.id in bindings_by_job[artifact.job_id]:
            raise _invalid()
        bindings_by_job[artifact.job_id][artifact.id] = binding
        selected_by_job_type.setdefault((artifact.job_id, artifact.artifact_type), []).append(
            binding
        )
    for job in jobs:
        models = selected_by_job_type.get((job.id, ArtifactType.FINAL_SMALL_MODEL.value), [])
        indexes = selected_by_job_type.get((job.id, ArtifactType.FINAL_INDEX.value), [])
        if len(models) > 1 or len(indexes) > 1:
            raise _invalid()
        if models:
            model_by_job[job.id] = _artifact_to_read(models[0])
        if indexes:
            index_by_job[job.id] = _artifact_to_read(indexes[0])
    return model_by_job, index_by_job, bindings_by_job


def _sample_provenance_is_valid(
    sample: Sample,
    *,
    attempt: JobAttempt,
    output_binding: VerifiedArtifactBinding,
    model_binding: VerifiedArtifactBinding,
    index_binding: VerifiedArtifactBinding | None,
) -> bool:
    provenance = {
        "rvc_commit_hash": sample.rvc_commit_hash,
        "runtime_image_digest": sample.runtime_image_digest,
        "runtime_asset_manifest_sha256": sample.runtime_asset_manifest_sha256,
        "native_inference_manifest_sha256": sample.native_inference_manifest_sha256,
        "native_inference_request_sha256": sample.native_inference_request_sha256,
    }
    return bool(
        sample.runtime_image_digest == attempt.runtime_image_digest
        and sample.runtime_asset_manifest_sha256 == attempt.runtime_asset_manifest_sha256
        and artifact_provenance_matches(
            output_binding,
            **provenance,
            native_sample_role="sample_output",
        )
        and artifact_provenance_matches(
            model_binding,
            **provenance,
            native_sample_role="sample_model",
        )
        and (
            index_binding is None
            or artifact_provenance_matches(
                index_binding,
                **provenance,
                native_sample_role="sample_index",
            )
        )
    )


async def _samples_by_job(
    session: AsyncSession,
    jobs: Sequence[Job],
    configs_by_job: dict[str, JobConfig],
    attempts_by_job: dict[str, JobAttempt],
    bindings_by_job: dict[str, dict[str, VerifiedArtifactBinding]],
) -> dict[str, list[ExperimentComparisonSampleRead]]:
    result: dict[str, list[ExperimentComparisonSampleRead]] = {job.id: [] for job in jobs}
    pairs = [
        and_(Sample.job_id == job.id, Sample.attempt_id == attempt.id)
        for job in jobs
        if (attempt := attempts_by_job.get(job.id)) is not None
    ]
    if not pairs:
        return result
    samples = list(
        (
            await session.scalars(
                select(Sample)
                .where(or_(*pairs))
                .order_by(Sample.job_id.asc(), Sample.created_at.asc(), Sample.id.asc())
            )
        ).all()
    )
    seen_items: dict[str, set[str]] = {job.id: set() for job in jobs}
    for sample in samples:
        attempt = attempts_by_job.get(sample.job_id)
        config = configs_by_job.get(sample.job_id)
        job = next((item for item in jobs if item.id == sample.job_id), None)
        bindings = bindings_by_job.get(sample.job_id, {})
        output_binding = bindings.get(sample.artifact_id)
        model_bindings = [
            binding
            for binding in bindings.values()
            if binding.artifact.artifact_type == ArtifactType.FINAL_SMALL_MODEL.value
            and binding.artifact.sha256 == sample.model_sha256
        ]
        index_bindings = [
            binding
            for binding in bindings.values()
            if binding.artifact.artifact_type == ArtifactType.FINAL_INDEX.value
            and binding.artifact.sha256 == sample.index_sha256
        ]
        if (
            attempt is None
            or config is None
            or job is None
            or not config.auto_inference_samples.enabled
            or sample.test_set_id != job.test_set_id
            or sample.test_set_item_id in seen_items[sample.job_id]
            or output_binding is None
            or output_binding.artifact.artifact_type != ArtifactType.SAMPLE.value
            or output_binding.artifact.sha256 != sample.output_sha256
            or output_binding.artifact.size_bytes != sample.output_size_bytes
            or output_binding.artifact.mime_type != "audio/wav"
            or len(model_bindings) != 1
            or len(index_bindings) > 1
            or not math.isfinite(sample.output_duration_seconds)
            or sample.output_duration_seconds <= 0
            or sample.output_size_bytes <= 0
            or sample.output_sample_rate_hz <= 0
            or sample.output_channels <= 0
        ):
            raise _invalid()
        expected_index = config.auto_inference_samples.index_rate > 0
        if expected_index != (sample.index_sha256 is not None) or expected_index != bool(
            index_bindings
        ):
            raise _invalid()
        try:
            SampleMetricsEvidence.model_validate(sample.metrics_json)
        except (TypeError, ValueError, ValidationError) as exc:
            raise _invalid() from exc
        index_binding = index_bindings[0] if index_bindings else None
        if not _sample_provenance_is_valid(
            sample,
            attempt=attempt,
            output_binding=output_binding,
            model_binding=model_bindings[0],
            index_binding=index_binding,
        ):
            raise _invalid()
        seen_items[sample.job_id].add(sample.test_set_item_id)
        result[sample.job_id].append(
            ExperimentComparisonSampleRead(
                id=sample.id,
                test_set_item_id=sample.test_set_item_id,
                output_size_bytes=sample.output_size_bytes,
                output_sha256=sample.output_sha256,
                output_sample_rate_hz=sample.output_sample_rate_hz,
                output_channels=sample.output_channels,
                output_duration_seconds=sample.output_duration_seconds,
                created_at=sample.created_at,
            )
        )
    return result


async def _assert_selection_is_current(
    session: AsyncSession,
    jobs: Sequence[Job],
    attempts_by_job: dict[str, JobAttempt],
    job_fingerprints: dict[str, tuple[object, ...]],
    attempt_fingerprints: dict[str, tuple[object, ...]],
) -> None:
    job_rows = list(
        (
            await session.execute(
                select(
                    Job.id,
                    Job.row_version,
                    Job.status,
                    Job.worker_id,
                    Job.current_attempt_id,
                    Job.current_epoch,
                    Job.total_epoch,
                    Job.attempt_count,
                ).where(Job.id.in_(tuple(job.id for job in jobs)))
            )
        ).all()
    )
    current_jobs = {
        row.id: (
            row.row_version,
            row.status,
            row.worker_id,
            row.current_attempt_id,
            row.current_epoch,
            row.total_epoch,
            row.attempt_count,
        )
        for row in job_rows
    }
    if current_jobs != job_fingerprints:
        raise _invalid()
    attempt_ids = tuple(attempt.id for attempt in attempts_by_job.values())
    if not attempt_ids:
        if attempt_fingerprints:
            raise _invalid()
        return
    attempt_rows = list(
        (
            await session.execute(
                select(
                    JobAttempt.id,
                    JobAttempt.job_id,
                    JobAttempt.worker_id,
                    JobAttempt.attempt_number,
                    JobAttempt.engine_mode,
                    JobAttempt.rvc_commit_hash,
                    JobAttempt.execution_provenance_version,
                    JobAttempt.runtime_image_digest,
                    JobAttempt.runtime_asset_manifest_sha256,
                    JobAttempt.status,
                    JobAttempt.started_at,
                    JobAttempt.finished_at,
                ).where(JobAttempt.id.in_(attempt_ids))
            )
        ).all()
    )
    current_attempts = {
        row.id: (
            row.job_id,
            row.worker_id,
            row.attempt_number,
            row.engine_mode,
            row.rvc_commit_hash,
            row.execution_provenance_version,
            row.runtime_image_digest,
            row.runtime_asset_manifest_sha256,
            row.status,
            row.started_at,
            row.finished_at,
        )
        for row in attempt_rows
    }
    if current_attempts != attempt_fingerprints:
        raise _invalid()


async def build_experiment_comparison_jobs(
    session: AsyncSession,
    storage: StorageAdapter,
    jobs: Sequence[Job],
) -> list[ExperimentComparisonJobRead]:
    # READ COMMITTED can observe a status/assignment transition between the
    # projection queries. Preserve the first ledger identities and reject the
    # whole response if a final read shows that the selected current attempt
    # changed while it was being assembled.
    job_fingerprints = {job.id: _job_fingerprint(job) for job in jobs}
    configs_by_job = {job.id: _validated_job_config(job) for job in jobs}
    attempts_by_job, attempt_reads = await _validated_current_attempts(session, jobs)
    attempt_fingerprints = {
        attempt.id: _attempt_fingerprint(attempt) for attempt in attempts_by_job.values()
    }
    metrics_by_job = await _metric_series_by_job(session, jobs, attempts_by_job)
    model_by_job, index_by_job, bindings_by_job = await _verified_bindings_by_job(
        session,
        storage,
        jobs,
        attempts_by_job,
    )
    samples_by_job = await _samples_by_job(
        session,
        jobs,
        configs_by_job,
        attempts_by_job,
        bindings_by_job,
    )
    result: list[ExperimentComparisonJobRead] = []
    for job in jobs:
        try:
            job_status = JobStatus(job.status)
        except ValueError as exc:
            raise _invalid() from exc
        result.append(
            ExperimentComparisonJobRead(
                id=job.id,
                job_name=job.job_name,
                status=job_status,
                config=configs_by_job[job.id],
                current_epoch=job.current_epoch,
                total_epoch=job.total_epoch,
                current_attempt=attempt_reads[job.id],
                metrics=metrics_by_job[job.id],
                availability=ExperimentComparisonAvailability(
                    final_model=model_by_job[job.id],
                    final_index=index_by_job[job.id],
                    samples=samples_by_job[job.id],
                ),
            )
        )
    await _assert_selection_is_current(
        session,
        jobs,
        attempts_by_job,
        job_fingerprints,
        attempt_fingerprints,
    )
    return result
