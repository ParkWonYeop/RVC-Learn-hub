from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Literal, cast

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from rvc_orchestrator_contracts import (
    RVC_REVIEWED_COMMIT,
    ArtifactType,
    JobConfig,
    JobStatus,
    utc_now,
)

from ..audit import add_audit_event
from ..config import Settings
from ..models import (
    Artifact,
    Experiment,
    ExperimentModelRegistry,
    Job,
    JobAttempt,
    ModelRegistryEntry,
    ModelRegistryOperation,
    User,
)
from ..schemas import (
    ModelRegistryArtifactRead,
    ModelRegistryCandidateCreate,
    ModelRegistryEntryPromote,
    ModelRegistryEntryRead,
    ModelRegistryEntryRevoke,
    ModelRegistryMutationRead,
    ModelRegistryRead,
)
from ..storage import StorageAdapter
from .samples import (
    SampleCompletionUnavailable,
    SampleStorageUnavailable,
    VerifiedArtifactBinding,
    verified_artifact_binding,
    verify_current_artifact_bytes,
)
from .test_sets import canonical_sha256

RegistryOperationType = Literal["candidate", "promote", "revoke"]


class ModelRegistryNotFound(LookupError):
    pass


class ModelRegistryConflict(ValueError):
    pass


class ModelRegistryUnavailable(RuntimeError):
    pass


class ModelRegistryAuthenticationChanged(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class CandidateContext:
    job: Job
    attempt: JobAttempt
    model: VerifiedArtifactBinding
    index: VerifiedArtifactBinding | None
    job_config_sha256: str


def entry_to_read(entry: ModelRegistryEntry) -> ModelRegistryEntryRead:
    index = None
    if entry.index_artifact_id is not None:
        assert entry.index_filename is not None
        assert entry.index_size_bytes is not None
        assert entry.index_sha256 is not None
        index = ModelRegistryArtifactRead(
            id=entry.index_artifact_id,
            filename=entry.index_filename,
            size_bytes=entry.index_size_bytes,
            sha256=entry.index_sha256,
        )
    return ModelRegistryEntryRead(
        id=entry.id,
        experiment_id=entry.experiment_id,
        row_version=entry.row_version,
        status=cast(Literal["candidate", "approved", "revoked"], entry.status),
        is_active=entry.active_slot == 1,
        source_job_id=entry.source_job_id,
        source_attempt_id=entry.source_attempt_id,
        source_job_name=entry.source_job_name,
        source_attempt_number=entry.source_attempt_number,
        engine_mode="rvc_webui",
        job_config_sha256=entry.job_config_sha256,
        rvc_commit_hash=entry.rvc_commit_hash,
        runtime_image_digest=entry.runtime_image_digest,
        runtime_asset_manifest_sha256=entry.runtime_asset_manifest_sha256,
        model=ModelRegistryArtifactRead(
            id=entry.model_artifact_id,
            filename=entry.model_filename,
            size_bytes=entry.model_size_bytes,
            sha256=entry.model_sha256,
        ),
        index=index,
        created_at=entry.created_at,
        approved_at=entry.approved_at,
        revoked_at=entry.revoked_at,
        revoke_reason=cast(
            Literal["quality_rejected", "security_issue", "operator_request"] | None,
            entry.revoke_reason,
        ),
    )


def _active_entry_id(entry: ModelRegistryEntry | None) -> str | None:
    return entry.id if entry is not None else None


def _actor_key_hash(actor_id: str, idempotency_key: str) -> str:
    return hashlib.sha256(f"{actor_id}\x1f{idempotency_key}".encode()).hexdigest()


def request_fingerprint(
    settings: Settings,
    *,
    operation_type: RegistryOperationType,
    path: str,
    document: dict[str, object],
) -> str:
    canonical = json.dumps(
        {
            "method": "POST",
            "operation_type": operation_type,
            "path": path,
            "document": document,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hmac.new(
        settings.jwt_secret.get_secret_value().encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()


def _require_visible_experiment(experiment: Experiment | None, actor: User) -> Experiment:
    if experiment is None or (actor.role != "admin" and experiment.created_by != actor.id):
        raise ModelRegistryNotFound("experiment not found")
    return experiment


async def _visible_experiment(
    session: AsyncSession,
    *,
    experiment_id: str,
    actor: User,
) -> Experiment:
    return _require_visible_experiment(await session.get(Experiment, experiment_id), actor)


async def _lock_experiment_fence(
    session: AsyncSession,
    *,
    experiment_id: str,
    actor_id: str,
    actor_token_version: int,
) -> tuple[Experiment, User]:
    # A no-op value assignment still performs a real UPDATE. PostgreSQL takes
    # the Experiment row lock and SQLite takes its write lock, serializing the
    # absent-registry first mutation as well as all later registry writes.
    fenced = await session.execute(
        update(Experiment)
        .where(Experiment.id == experiment_id)
        .values(updated_at=Experiment.updated_at)
        .execution_options(synchronize_session=False)
    )
    if fenced.rowcount != 1:  # type: ignore[attr-defined]
        raise ModelRegistryNotFound("experiment not found")
    experiment = await session.scalar(
        select(Experiment)
        .where(Experiment.id == experiment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    actor = await session.scalar(
        select(User)
        .where(User.id == actor_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        actor is None
        or actor.disabled
        or actor.access_token_version != actor_token_version
    ):
        raise ModelRegistryAuthenticationChanged("authentication state changed")
    assert experiment is not None
    _require_visible_experiment(experiment, actor)
    return experiment, actor


async def _replay_or_none(
    session: AsyncSession,
    *,
    actor_id: str,
    key_hash: str,
    operation_type: RegistryOperationType,
    fingerprint: str,
) -> ModelRegistryMutationRead | None:
    operation = await session.scalar(
        select(ModelRegistryOperation).where(
            ModelRegistryOperation.actor_id == actor_id,
            ModelRegistryOperation.idempotency_key_hash == key_hash,
        )
    )
    if operation is None:
        return None
    if (
        operation.operation_type != operation_type
        or not hmac.compare_digest(operation.request_fingerprint, fingerprint)
    ):
        raise ModelRegistryConflict(
            "idempotency key conflicts with a prior model registry request"
        )
    return ModelRegistryMutationRead.model_validate(operation.response_json)


def _record_operation(
    session: AsyncSession,
    *,
    actor_id: str,
    key_hash: str,
    fingerprint: str,
    operation_type: RegistryOperationType,
    result: ModelRegistryMutationRead,
) -> None:
    session.add(
        ModelRegistryOperation(
            actor_id=actor_id,
            idempotency_key_hash=key_hash,
            request_fingerprint=fingerprint,
            operation_type=operation_type,
            experiment_id=result.experiment_id,
            entry_id=result.entry.id,
            response_json=result.model_dump(mode="json"),
        )
    )


async def _registry_for_update(
    session: AsyncSession,
    experiment_id: str,
) -> ExperimentModelRegistry | None:
    return cast(
        ExperimentModelRegistry | None,
        await session.scalar(
            select(ExperimentModelRegistry)
            .where(ExperimentModelRegistry.experiment_id == experiment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ),
    )


async def _active_entry(
    session: AsyncSession,
    experiment_id: str,
    *,
    lock: bool = False,
) -> ModelRegistryEntry | None:
    statement = select(ModelRegistryEntry).where(
        ModelRegistryEntry.experiment_id == experiment_id,
        ModelRegistryEntry.active_slot == 1,
    )
    if lock:
        statement = statement.with_for_update()
    return cast(
        ModelRegistryEntry | None,
        await session.scalar(statement.execution_options(populate_existing=True)),
    )


def _require_registry_version(
    registry: ExperimentModelRegistry | None,
    expected: int,
) -> None:
    current = 0 if registry is None else registry.row_version
    if current != expected:
        raise ModelRegistryConflict("model registry changed; refresh and retry")


async def _flush_or_conflict(session: AsyncSession) -> None:
    try:
        await session.flush()
    except (IntegrityError, StaleDataError) as exc:
        await session.rollback()
        raise ModelRegistryConflict("model registry changed; refresh and retry") from exc


async def _advance_registry(
    session: AsyncSession,
    registry: ExperimentModelRegistry | None,
    *,
    experiment_id: str,
) -> ExperimentModelRegistry:
    if registry is None:
        registry = ExperimentModelRegistry(
            experiment_id=experiment_id,
            row_version=1,
        )
        session.add(registry)
    else:
        registry.updated_at = utc_now()
    await _flush_or_conflict(session)
    return registry


async def _binding(
    session: AsyncSession,
    storage: StorageAdapter,
    *,
    artifact_id: str,
    job_id: str,
    attempt_id: str,
    artifact_type: ArtifactType,
    worker_id: str,
) -> VerifiedArtifactBinding:
    try:
        binding = await verified_artifact_binding(
            session,
            storage,
            artifact_id=artifact_id,
            job_id=job_id,
            attempt_id=attempt_id,
            artifact_type=artifact_type,
            worker_id=worker_id,
        )
    except SampleStorageUnavailable as exc:
        raise ModelRegistryUnavailable("artifact storage namespace is unavailable") from exc
    if binding is None:
        raise ModelRegistryConflict("model registry artifact ledger is inconsistent")
    return binding


async def _candidate_context(
    session: AsyncSession,
    storage: StorageAdapter,
    settings: Settings,
    *,
    experiment_id: str,
    source_job_id: str,
    source_attempt_id: str,
    model_artifact_id: str,
    require_current: bool,
    lock: bool,
) -> CandidateContext:
    job_statement = select(Job).where(Job.id == source_job_id)
    attempt_statement = select(JobAttempt).where(JobAttempt.id == source_attempt_id)
    if lock:
        job_statement = job_statement.with_for_update()
        attempt_statement = attempt_statement.with_for_update()
    job = await session.scalar(job_statement.execution_options(populate_existing=True))
    attempt = await session.scalar(attempt_statement.execution_options(populate_existing=True))
    try:
        config = None if job is None else JobConfig.model_validate(job.config_json)
    except ValueError as exc:
        raise ModelRegistryConflict("model registry Job configuration is invalid") from exc
    config_matches = bool(
        job is not None
        and config is not None
        and config.job_name == job.job_name
        and config.experiment_id == job.experiment_id
        and config.dataset_id == job.dataset_id
        and config.training.epochs == job.total_epoch
        and config.resource.priority == job.priority
        and (config.rvc_backend.rvc_commit_hash or RVC_REVIEWED_COMMIT)
        == RVC_REVIEWED_COMMIT
    )
    current_matches = bool(
        not require_current
        or (
            job is not None
            and attempt is not None
            and job.current_attempt_id == attempt.id
            and job.status == JobStatus.COMPLETED.value
            and job.worker_id == attempt.worker_id
            and job.attempt_count == attempt.attempt_number
        )
    )
    if (
        job is None
        or attempt is None
        or job.experiment_id != experiment_id
        or attempt.job_id != job.id
        or not config_matches
        or not current_matches
        or attempt.status != JobStatus.COMPLETED.value
        or attempt.finished_at is None
        or attempt.finished_at < attempt.started_at
        or attempt.engine_mode != "rvc_webui"
        or attempt.execution_provenance_version != "worker-claim-v1"
        or attempt.rvc_commit_hash != RVC_REVIEWED_COMMIT
        or attempt.runtime_image_digest is None
        or attempt.runtime_asset_manifest_sha256 is None
        or (
            attempt.runtime_image_digest,
            attempt.runtime_asset_manifest_sha256,
        )
        not in settings.approved_sample_runtime_bundles
    ):
        raise ModelRegistryConflict("model registry candidate is not eligible")
    assert config is not None

    model_statement = select(Artifact.id).where(
        Artifact.job_id == job.id,
        Artifact.attempt_id == attempt.id,
        Artifact.artifact_type == ArtifactType.FINAL_SMALL_MODEL.value,
    )
    index_statement = select(Artifact.id).where(
        Artifact.job_id == job.id,
        Artifact.attempt_id == attempt.id,
        Artifact.artifact_type == ArtifactType.FINAL_INDEX.value,
    )
    if lock:
        model_statement = model_statement.with_for_update()
        index_statement = index_statement.with_for_update()
    model_ids = list((await session.scalars(model_statement)).all())
    if len(model_ids) != 1 or model_ids[0] != model_artifact_id:
        raise ModelRegistryConflict("model registry final model ledger is ambiguous")
    model = await _binding(
        session,
        storage,
        artifact_id=model_artifact_id,
        job_id=job.id,
        attempt_id=attempt.id,
        artifact_type=ArtifactType.FINAL_SMALL_MODEL,
        worker_id=attempt.worker_id,
    )
    index_ids = list((await session.scalars(index_statement)).all())
    if len(index_ids) > 1:
        raise ModelRegistryConflict("model registry final index ledger is ambiguous")
    index = None
    if index_ids:
        index = await _binding(
            session,
            storage,
            artifact_id=index_ids[0],
            job_id=job.id,
            attempt_id=attempt.id,
            artifact_type=ArtifactType.FINAL_INDEX,
            worker_id=attempt.worker_id,
        )
    return CandidateContext(
        job=job,
        attempt=attempt,
        model=model,
        index=index,
        job_config_sha256=canonical_sha256(config.model_dump(mode="json")),
    )


def _context_fingerprint(context: CandidateContext) -> str:
    bindings = [context.model] + ([context.index] if context.index is not None else [])
    return canonical_sha256(
        {
            "job": {
                "id": context.job.id,
                "experiment_id": context.job.experiment_id,
                "name": context.job.job_name,
                "status": context.job.status,
                "current_attempt_id": context.job.current_attempt_id,
                "config_sha256": context.job_config_sha256,
            },
            "attempt": {
                "id": context.attempt.id,
                "job_id": context.attempt.job_id,
                "number": context.attempt.attempt_number,
                "status": context.attempt.status,
                "engine": context.attempt.engine_mode,
                "rvc_commit": context.attempt.rvc_commit_hash,
                "provenance_version": context.attempt.execution_provenance_version,
                "runtime_image": context.attempt.runtime_image_digest,
                "runtime_assets": context.attempt.runtime_asset_manifest_sha256,
            },
            "artifacts": [
                {
                    "id": binding.artifact.id,
                    "job_id": binding.artifact.job_id,
                    "attempt_id": binding.artifact.attempt_id,
                    "type": binding.artifact.artifact_type,
                    "filename": binding.artifact.filename,
                    "size": binding.artifact.size_bytes,
                    "sha256": binding.artifact.sha256,
                    "uri": binding.artifact.storage_uri,
                    "metadata": binding.artifact.metadata_json,
                    "upload_id": binding.upload.id,
                    "upload_status": binding.upload.status,
                    "canonical_key": binding.upload.canonical_object_key,
                    "namespace": binding.upload.storage_namespace_sha256,
                    "expected_size": binding.upload.expected_size_bytes,
                    "expected_sha256": binding.upload.expected_sha256,
                }
                for binding in bindings
            ],
        }
    )


async def _verify_context_bytes(
    context: CandidateContext,
    storage: StorageAdapter,
    settings: Settings,
    semaphore: asyncio.Semaphore,
) -> None:
    deadline = time.monotonic() + settings.sample_verification_timeout_seconds
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ModelRegistryUnavailable("model registry artifact verification timed out")
    acquired = False
    try:
        try:
            async with asyncio.timeout(remaining):
                await semaphore.acquire()
            acquired = True
        except TimeoutError as exc:
            raise ModelRegistryUnavailable(
                "model registry artifact verification slot is unavailable"
            ) from exc
        for binding in (context.model, context.index):
            if binding is None:
                continue
            if deadline <= time.monotonic():
                raise ModelRegistryUnavailable("model registry artifact verification timed out")
            try:
                verified = await verify_current_artifact_bytes(
                    binding,
                    storage,
                    settings,
                    deadline_monotonic=deadline,
                )
            except SampleCompletionUnavailable as exc:
                raise ModelRegistryUnavailable(
                    "model registry artifact verification is unavailable"
                ) from exc
            if not verified:
                raise ModelRegistryConflict("model registry artifact bytes do not match ledger")
    finally:
        if acquired:
            semaphore.release()


async def _preflight_context(
    session: AsyncSession,
    storage: StorageAdapter,
    settings: Settings,
    semaphore: asyncio.Semaphore,
    *,
    experiment_id: str,
    source_job_id: str,
    source_attempt_id: str,
    model_artifact_id: str,
    require_current: bool,
) -> tuple[CandidateContext, str]:
    context = await _candidate_context(
        session,
        storage,
        settings,
        experiment_id=experiment_id,
        source_job_id=source_job_id,
        source_attempt_id=source_attempt_id,
        model_artifact_id=model_artifact_id,
        require_current=require_current,
        lock=False,
    )
    fingerprint = _context_fingerprint(context)
    await _verify_context_bytes(context, storage, settings, semaphore)
    return context, fingerprint


def _context_matches_entry(context: CandidateContext, entry: ModelRegistryEntry) -> bool:
    index = context.index.artifact if context.index is not None else None
    return bool(
        context.job.id == entry.source_job_id
        and context.attempt.id == entry.source_attempt_id
        and context.job.job_name == entry.source_job_name
        and context.attempt.attempt_number == entry.source_attempt_number
        and context.attempt.engine_mode == entry.engine_mode
        and context.job_config_sha256 == entry.job_config_sha256
        and context.attempt.rvc_commit_hash == entry.rvc_commit_hash
        and context.attempt.execution_provenance_version
        == entry.execution_provenance_version
        and context.attempt.runtime_image_digest == entry.runtime_image_digest
        and context.attempt.runtime_asset_manifest_sha256
        == entry.runtime_asset_manifest_sha256
        and context.model.artifact.id == entry.model_artifact_id
        and context.model.artifact.filename == entry.model_filename
        and context.model.artifact.size_bytes == entry.model_size_bytes
        and context.model.artifact.sha256 == entry.model_sha256
        and (None if index is None else index.id) == entry.index_artifact_id
        and (None if index is None else index.filename) == entry.index_filename
        and (None if index is None else index.size_bytes) == entry.index_size_bytes
        and (None if index is None else index.sha256) == entry.index_sha256
    )


async def get_registry(
    session: AsyncSession,
    *,
    experiment_id: str,
    actor: User,
    offset: int,
    limit: int,
) -> ModelRegistryRead:
    await _visible_experiment(session, experiment_id=experiment_id, actor=actor)
    registry = await session.get(ExperimentModelRegistry, experiment_id)
    if registry is None:
        if await session.scalar(
            select(ExperimentModelRegistry.row_version).where(
                ExperimentModelRegistry.experiment_id == experiment_id
            )
        ) is not None:
            raise ModelRegistryConflict("model registry changed while reading; retry")
        return ModelRegistryRead(
            experiment_id=experiment_id,
            registry_row_version=0,
            active_entry_id=None,
            can_manage=True,
            items=[],
            total=0,
            offset=offset,
            limit=limit,
        )
    initial_registry_version = registry.row_version
    active = await _active_entry(session, experiment_id)
    total = (
        await session.scalar(
            select(func.count())
            .select_from(ModelRegistryEntry)
            .where(ModelRegistryEntry.experiment_id == experiment_id)
        )
        or 0
    )
    entries = list(
        (
            await session.scalars(
                select(ModelRegistryEntry)
                .where(ModelRegistryEntry.experiment_id == experiment_id)
                .order_by(ModelRegistryEntry.created_at.desc(), ModelRegistryEntry.id.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    final_registry_version = await session.scalar(
        select(ExperimentModelRegistry.row_version).where(
            ExperimentModelRegistry.experiment_id == experiment_id
        )
    )
    final_active = (
        await session.execute(
            select(
                ModelRegistryEntry.id,
                ModelRegistryEntry.status,
                ModelRegistryEntry.active_slot,
            ).where(
                ModelRegistryEntry.experiment_id == experiment_id,
                ModelRegistryEntry.active_slot == 1,
            )
        )
    ).one_or_none()
    final_active_id = None if final_active is None else str(final_active.id)
    if (
        final_registry_version != initial_registry_version
        or final_active_id != _active_entry_id(active)
        or (
            final_active is not None
            and (final_active.status != "approved" or final_active.active_slot != 1)
        )
    ):
        raise ModelRegistryConflict("model registry changed while reading; retry")
    return ModelRegistryRead(
        experiment_id=experiment_id,
        registry_row_version=initial_registry_version,
        active_entry_id=_active_entry_id(active),
        can_manage=True,
        items=[entry_to_read(entry) for entry in entries],
        total=total,
        offset=offset,
        limit=limit,
    )


async def create_candidate(
    session: AsyncSession,
    storage: StorageAdapter,
    settings: Settings,
    semaphore: asyncio.Semaphore,
    *,
    experiment_id: str,
    actor: User,
    actor_token_version: int,
    payload: ModelRegistryCandidateCreate,
    idempotency_key: str,
    path: str,
) -> tuple[ModelRegistryMutationRead, bool]:
    await _visible_experiment(session, experiment_id=experiment_id, actor=actor)
    actor_id = actor.id
    key_hash = _actor_key_hash(actor_id, idempotency_key)
    fingerprint = request_fingerprint(
        settings,
        operation_type="candidate",
        path=path,
        document=payload.model_dump(mode="json"),
    )
    replay = await _replay_or_none(
        session,
        actor_id=actor_id,
        key_hash=key_hash,
        operation_type="candidate",
        fingerprint=fingerprint,
    )
    if replay is not None:
        return replay, True
    preflight_fingerprint: str | None = None
    _require_registry_version(
        await session.get(ExperimentModelRegistry, experiment_id),
        payload.expected_registry_row_version,
    )
    existing = await session.scalar(
        select(ModelRegistryEntry.id).where(
            ModelRegistryEntry.model_artifact_id == str(payload.model_artifact_id)
        )
    )
    if existing is not None:
        raise ModelRegistryConflict("model artifact already has a registry entry")
    _, preflight_fingerprint = await _preflight_context(
        session,
        storage,
        settings,
        semaphore,
        experiment_id=experiment_id,
        source_job_id=str(payload.source_job_id),
        source_attempt_id=str(payload.source_attempt_id),
        model_artifact_id=str(payload.model_artifact_id),
        require_current=True,
    )
    await session.rollback()
    _, locked_actor = await _lock_experiment_fence(
        session,
        experiment_id=experiment_id,
        actor_id=actor_id,
        actor_token_version=actor_token_version,
    )
    replay = await _replay_or_none(
        session,
        actor_id=locked_actor.id,
        key_hash=key_hash,
        operation_type="candidate",
        fingerprint=fingerprint,
    )
    if replay is not None:
        await session.rollback()
        return replay, True
    registry = await _registry_for_update(session, experiment_id)
    _require_registry_version(registry, payload.expected_registry_row_version)
    existing = await session.scalar(
        select(ModelRegistryEntry.id).where(
            ModelRegistryEntry.model_artifact_id == str(payload.model_artifact_id)
        )
    )
    if existing is not None:
        raise ModelRegistryConflict("model artifact already has a registry entry")
    if preflight_fingerprint is None:
        raise ModelRegistryConflict("model registry preflight result is unavailable")
    context = await _candidate_context(
        session,
        storage,
        settings,
        experiment_id=experiment_id,
        source_job_id=str(payload.source_job_id),
        source_attempt_id=str(payload.source_attempt_id),
        model_artifact_id=str(payload.model_artifact_id),
        require_current=True,
        lock=True,
    )
    if not hmac.compare_digest(preflight_fingerprint, _context_fingerprint(context)):
        raise ModelRegistryConflict("model registry artifact ledger changed during verification")
    registry = await _advance_registry(
        session,
        registry,
        experiment_id=experiment_id,
    )
    model = context.model.artifact
    index = context.index.artifact if context.index is not None else None
    assert context.attempt.rvc_commit_hash is not None
    assert context.attempt.execution_provenance_version is not None
    assert context.attempt.runtime_image_digest is not None
    assert context.attempt.runtime_asset_manifest_sha256 is not None
    entry = ModelRegistryEntry(
        experiment_id=experiment_id,
        status="candidate",
        source_job_id=context.job.id,
        source_attempt_id=context.attempt.id,
        source_job_name=context.job.job_name,
        source_attempt_number=context.attempt.attempt_number,
        engine_mode="rvc_webui",
        job_config_sha256=context.job_config_sha256,
        rvc_commit_hash=context.attempt.rvc_commit_hash,
        execution_provenance_version=context.attempt.execution_provenance_version,
        runtime_image_digest=context.attempt.runtime_image_digest,
        runtime_asset_manifest_sha256=context.attempt.runtime_asset_manifest_sha256,
        model_artifact_id=model.id,
        model_filename=model.filename,
        model_size_bytes=model.size_bytes,
        model_sha256=model.sha256,
        index_artifact_id=None if index is None else index.id,
        index_filename=None if index is None else index.filename,
        index_size_bytes=None if index is None else index.size_bytes,
        index_sha256=None if index is None else index.sha256,
        created_by=locked_actor.id,
    )
    session.add(entry)
    await _flush_or_conflict(session)
    result = ModelRegistryMutationRead(
        experiment_id=experiment_id,
        registry_row_version=registry.row_version,
        active_entry_id=_active_entry_id(await _active_entry(session, experiment_id)),
        entry=entry_to_read(entry),
    )
    _record_operation(
        session,
        actor_id=locked_actor.id,
        key_hash=key_hash,
        fingerprint=fingerprint,
        operation_type="candidate",
        result=result,
    )
    add_audit_event(
        session,
        actor_type="user",
        actor_id=locked_actor.id,
        action="model_registry.candidate_created",
        resource_type="model_registry_entry",
        resource_id=entry.id,
        details={
            "experiment_id": experiment_id,
            "source_job_id": entry.source_job_id,
            "source_attempt_id": entry.source_attempt_id,
            "registry_row_version": registry.row_version,
            "entry_row_version": entry.row_version,
            "model_artifact_id": entry.model_artifact_id,
            "model_sha256": entry.model_sha256,
            "index_artifact_id": entry.index_artifact_id,
            "index_sha256": entry.index_sha256,
            "runtime_image_digest": entry.runtime_image_digest,
            "runtime_asset_manifest_sha256": entry.runtime_asset_manifest_sha256,
        },
    )
    try:
        await session.commit()
    except (IntegrityError, StaleDataError) as exc:
        await session.rollback()
        replay = await _replay_or_none(
            session,
            actor_id=actor_id,
            key_hash=key_hash,
            operation_type="candidate",
            fingerprint=fingerprint,
        )
        if replay is not None:
            return replay, True
        raise ModelRegistryConflict("model registry changed; refresh and retry") from exc
    return result, False


async def promote_entry(
    session: AsyncSession,
    storage: StorageAdapter,
    settings: Settings,
    semaphore: asyncio.Semaphore,
    *,
    experiment_id: str,
    entry_id: str,
    actor: User,
    actor_token_version: int,
    payload: ModelRegistryEntryPromote,
    idempotency_key: str,
    path: str,
) -> tuple[ModelRegistryMutationRead, bool]:
    await _visible_experiment(session, experiment_id=experiment_id, actor=actor)
    actor_id = actor.id
    key_hash = _actor_key_hash(actor_id, idempotency_key)
    fingerprint = request_fingerprint(
        settings,
        operation_type="promote",
        path=path,
        document=payload.model_dump(mode="json"),
    )
    replay = await _replay_or_none(
        session,
        actor_id=actor_id,
        key_hash=key_hash,
        operation_type="promote",
        fingerprint=fingerprint,
    )
    if replay is not None:
        return replay, True
    preflight_fingerprint: str | None = None
    _require_registry_version(
        await session.get(ExperimentModelRegistry, experiment_id),
        payload.expected_registry_row_version,
    )
    preflight_entry = await session.scalar(
        select(ModelRegistryEntry).where(
            ModelRegistryEntry.id == entry_id,
            ModelRegistryEntry.experiment_id == experiment_id,
        )
    )
    if preflight_entry is None:
        raise ModelRegistryNotFound("model registry entry not found")
    if preflight_entry.row_version != payload.expected_entry_row_version:
        raise ModelRegistryConflict("model registry entry changed; refresh and retry")
    if preflight_entry.status == "revoked":
        raise ModelRegistryConflict("revoked model registry entry cannot be promoted")
    if preflight_entry.active_slot == 1:
        raise ModelRegistryConflict("model registry entry is already active")
    preflight_context, preflight_fingerprint = await _preflight_context(
        session,
        storage,
        settings,
        semaphore,
        experiment_id=experiment_id,
        source_job_id=preflight_entry.source_job_id,
        source_attempt_id=preflight_entry.source_attempt_id,
        model_artifact_id=preflight_entry.model_artifact_id,
        require_current=False,
    )
    if not _context_matches_entry(preflight_context, preflight_entry):
        raise ModelRegistryConflict("model registry frozen snapshot is inconsistent")
    await session.rollback()
    _, locked_actor = await _lock_experiment_fence(
        session,
        experiment_id=experiment_id,
        actor_id=actor_id,
        actor_token_version=actor_token_version,
    )
    replay = await _replay_or_none(
        session,
        actor_id=locked_actor.id,
        key_hash=key_hash,
        operation_type="promote",
        fingerprint=fingerprint,
    )
    if replay is not None:
        await session.rollback()
        return replay, True
    registry = await _registry_for_update(session, experiment_id)
    _require_registry_version(registry, payload.expected_registry_row_version)
    if registry is None:
        raise ModelRegistryConflict("model registry changed; refresh and retry")
    entry = await session.scalar(
        select(ModelRegistryEntry)
        .where(
            ModelRegistryEntry.id == entry_id,
            ModelRegistryEntry.experiment_id == experiment_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if entry is None:
        raise ModelRegistryNotFound("model registry entry not found")
    if entry.row_version != payload.expected_entry_row_version:
        raise ModelRegistryConflict("model registry entry changed; refresh and retry")
    if entry.status == "revoked":
        raise ModelRegistryConflict("revoked model registry entry cannot be promoted")
    if entry.active_slot == 1:
        raise ModelRegistryConflict("model registry entry is already active")
    if preflight_fingerprint is None:
        raise ModelRegistryConflict("model registry preflight result is unavailable")
    context = await _candidate_context(
        session,
        storage,
        settings,
        experiment_id=experiment_id,
        source_job_id=entry.source_job_id,
        source_attempt_id=entry.source_attempt_id,
        model_artifact_id=entry.model_artifact_id,
        require_current=False,
        lock=True,
    )
    if (
        not hmac.compare_digest(preflight_fingerprint, _context_fingerprint(context))
        or not _context_matches_entry(context, entry)
    ):
        raise ModelRegistryConflict("model registry frozen snapshot is inconsistent")
    previous = await _active_entry(session, experiment_id, lock=True)
    now = utc_now()
    if previous is not None:
        previous.active_slot = None
        previous.updated_at = now
        await _flush_or_conflict(session)
    entry.status = "approved"
    entry.active_slot = 1
    if entry.approved_at is None:
        entry.approved_at = now
        entry.approved_by = locked_actor.id
    entry.updated_at = now
    await _flush_or_conflict(session)
    registry = await _advance_registry(
        session,
        registry,
        experiment_id=experiment_id,
    )
    result = ModelRegistryMutationRead(
        experiment_id=experiment_id,
        registry_row_version=registry.row_version,
        active_entry_id=entry.id,
        entry=entry_to_read(entry),
    )
    _record_operation(
        session,
        actor_id=locked_actor.id,
        key_hash=key_hash,
        fingerprint=fingerprint,
        operation_type="promote",
        result=result,
    )
    add_audit_event(
        session,
        actor_type="user",
        actor_id=locked_actor.id,
        action="model_registry.entry_promoted",
        resource_type="model_registry_entry",
        resource_id=entry.id,
        details={
            "experiment_id": experiment_id,
            "registry_row_version": registry.row_version,
            "entry_row_version": entry.row_version,
            "replaced_active_entry": previous is not None,
            "model_artifact_id": entry.model_artifact_id,
            "model_sha256": entry.model_sha256,
            "index_artifact_id": entry.index_artifact_id,
            "index_sha256": entry.index_sha256,
            "runtime_image_digest": entry.runtime_image_digest,
            "runtime_asset_manifest_sha256": entry.runtime_asset_manifest_sha256,
        },
    )
    try:
        await session.commit()
    except (IntegrityError, StaleDataError) as exc:
        await session.rollback()
        replay = await _replay_or_none(
            session,
            actor_id=actor_id,
            key_hash=key_hash,
            operation_type="promote",
            fingerprint=fingerprint,
        )
        if replay is not None:
            return replay, True
        raise ModelRegistryConflict("model registry changed; refresh and retry") from exc
    return result, False


async def revoke_entry(
    session: AsyncSession,
    settings: Settings,
    *,
    experiment_id: str,
    entry_id: str,
    actor: User,
    actor_token_version: int,
    payload: ModelRegistryEntryRevoke,
    idempotency_key: str,
    path: str,
) -> tuple[ModelRegistryMutationRead, bool]:
    await _visible_experiment(session, experiment_id=experiment_id, actor=actor)
    actor_id = actor.id
    key_hash = _actor_key_hash(actor_id, idempotency_key)
    fingerprint = request_fingerprint(
        settings,
        operation_type="revoke",
        path=path,
        document=payload.model_dump(mode="json"),
    )
    replay = await _replay_or_none(
        session,
        actor_id=actor_id,
        key_hash=key_hash,
        operation_type="revoke",
        fingerprint=fingerprint,
    )
    if replay is not None:
        return replay, True
    await session.rollback()
    _, locked_actor = await _lock_experiment_fence(
        session,
        experiment_id=experiment_id,
        actor_id=actor_id,
        actor_token_version=actor_token_version,
    )
    replay = await _replay_or_none(
        session,
        actor_id=locked_actor.id,
        key_hash=key_hash,
        operation_type="revoke",
        fingerprint=fingerprint,
    )
    if replay is not None:
        await session.rollback()
        return replay, True
    registry = await _registry_for_update(session, experiment_id)
    _require_registry_version(registry, payload.expected_registry_row_version)
    if registry is None:
        raise ModelRegistryConflict("model registry changed; refresh and retry")
    entry = await session.scalar(
        select(ModelRegistryEntry)
        .where(
            ModelRegistryEntry.id == entry_id,
            ModelRegistryEntry.experiment_id == experiment_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if entry is None:
        raise ModelRegistryNotFound("model registry entry not found")
    if entry.row_version != payload.expected_entry_row_version:
        raise ModelRegistryConflict("model registry entry changed; refresh and retry")
    if entry.status == "revoked":
        raise ModelRegistryConflict("model registry entry is already revoked")
    entry.status = "revoked"
    entry.active_slot = None
    entry.revoked_by = locked_actor.id
    entry.revoked_at = utc_now()
    entry.revoke_reason = payload.reason_code
    entry.updated_at = entry.revoked_at
    await _flush_or_conflict(session)
    registry = await _advance_registry(
        session,
        registry,
        experiment_id=experiment_id,
    )
    active = await _active_entry(session, experiment_id)
    result = ModelRegistryMutationRead(
        experiment_id=experiment_id,
        registry_row_version=registry.row_version,
        active_entry_id=_active_entry_id(active),
        entry=entry_to_read(entry),
    )
    _record_operation(
        session,
        actor_id=locked_actor.id,
        key_hash=key_hash,
        fingerprint=fingerprint,
        operation_type="revoke",
        result=result,
    )
    add_audit_event(
        session,
        actor_type="user",
        actor_id=locked_actor.id,
        action="model_registry.entry_revoked",
        resource_type="model_registry_entry",
        resource_id=entry.id,
        details={
            "experiment_id": experiment_id,
            "registry_row_version": registry.row_version,
            "reason_code": payload.reason_code,
        },
    )
    try:
        await session.commit()
    except (IntegrityError, StaleDataError) as exc:
        await session.rollback()
        replay = await _replay_or_none(
            session,
            actor_id=actor_id,
            key_hash=key_hash,
            operation_type="revoke",
            fingerprint=fingerprint,
        )
        if replay is not None:
            return replay, True
        raise ModelRegistryConflict("model registry changed; refresh and retry") from exc
    return result, False
