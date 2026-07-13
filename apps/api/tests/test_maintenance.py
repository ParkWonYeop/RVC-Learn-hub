from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import ValidationError
from redis import Redis
from rq import Queue, Retry, Worker
from rq.exceptions import NoSuchJobError
from rq.job import UNEVALUATED, Job, JobStatus
from rq.scheduler import RQScheduler
from rq.serializers import DefaultSerializer, JSONSerializer
from sqlalchemy import func, select

from rvc_manager_api.app import create_app
from rvc_manager_api.config import Settings
from rvc_manager_api.maintenance_queue import (
    DATASET_CLEANUP_TASK_PATH,
    TEST_SET_CLEANUP_TASK_PATH,
    EnqueuedMaintenanceTask,
    MaintenanceQueueEnvelopeConflict,
    MaintenanceQueueUnavailable,
    RqMaintenanceQueue,
)
from rvc_manager_api.models import (
    AuditEvent,
    Dataset,
    DatasetUploadSession,
    MaintenanceTaskRun,
    User,
)
from rvc_manager_api.models import (
    TestSet as LedgerTestSet,
)
from rvc_manager_api.models import (
    TestSetItemUploadSession as LedgerTestSetItemUploadSession,
)
from rvc_manager_api.rq_worker import (
    AllowlistedMaintenanceWorker,
    maintenance_job_is_allowlisted,
)
from rvc_manager_api.security import hash_password
from rvc_manager_api.services.maintenance import (
    DatasetCleanupResult,
    MaintenanceReconciler,
    MaintenanceRunNotExecutable,
    _finish_run,
    _RunHeartbeat,
    reconcile_maintenance_runs,
    run_dataset_staging_cleanup,
    run_test_set_staging_cleanup,
)
from rvc_manager_api.storage import (
    UNBOUND_STORAGE_NAMESPACE_SHA256,
    LocalStorageAdapter,
    StorageError,
)
from rvc_orchestrator_contracts import utc_now


def _make_upload(
    dataset: Dataset,
    *,
    upload_id: str,
    status: str,
    expires_at: Any,
    storage_namespace_sha256: str,
    failure_code: str | None = None,
) -> DatasetUploadSession:
    prefix = f"datasets/verified/{dataset.id}"
    return DatasetUploadSession(
        id=upload_id,
        dataset_id=dataset.id,
        owner_id=dataset.created_by,
        idempotency_key=f"key-{upload_id}",
        generation=1,
        request_fingerprint="a" * 64,
        filename="dataset.zip",
        content_type="application/zip",
        expected_size_bytes=4,
        expected_sha256="b" * 64,
        temporary_object_key=f"datasets/staging/{dataset.id}/{upload_id}",
        original_object_key=f"{prefix}/original.zip",
        prepared_flat_object_key=f"{prefix}/prepared_flat.zip",
        manifest_object_key=f"{prefix}/manifest.json",
        quality_report_object_key=f"{prefix}/quality_report.json",
        storage_backend="local",
        storage_namespace_sha256=storage_namespace_sha256,
        status=status,
        expires_at=expires_at,
        failure_code=failure_code,
    )


def _make_run(*, run_id: str, user_id: str, dry_run: bool = False) -> MaintenanceTaskRun:
    identity_hash = run_id.replace("-", "").ljust(64, "0")[:64]
    return MaintenanceTaskRun(
        id=run_id,
        task_name="dataset_staging_cleanup",
        job_id=f"rvc-maintenance-{identity_hash}",
        idempotency_key_hash=identity_hash,
        dry_run=dry_run,
        status="queued",
        attempt_count=0,
        max_attempts=3,
        result_json={},
        created_by=user_id,
    )


def _make_test_set_upload(
    test_set: LedgerTestSet,
    *,
    upload_id: str,
    status: str,
    expires_at: Any,
    storage_namespace_sha256: str,
) -> LedgerTestSetItemUploadSession:
    return LedgerTestSetItemUploadSession(
        id=upload_id,
        test_set_id=test_set.id,
        owner_id=test_set.created_by,
        idempotency_key=f"test-set-key-{upload_id}",
        generation=1,
        request_fingerprint="c" * 64,
        item_key=f"item-{upload_id[-4:]}",
        display_name="Cleanup fixture",
        sort_order=int(upload_id[-2:], 16),
        filename="fixture.wav",
        content_type="audio/wav",
        expected_size_bytes=4,
        expected_sha256="d" * 64,
        license_reference="license:test-cleanup",
        provenance_reference="consent:test-cleanup",
        temporary_object_key=f"test-sets/staging/{test_set.id}/{upload_id}",
        canonical_object_key=f"test-sets/verified/{test_set.id}/items/{upload_id}.wav",
        storage_backend="local",
        storage_namespace_sha256=storage_namespace_sha256,
        status=status,
        expires_at=expires_at,
    )


def _make_test_set_run(
    *,
    run_id: str,
    user_id: str,
    dry_run: bool = False,
) -> MaintenanceTaskRun:
    identity_hash = run_id.replace("-", "").ljust(64, "0")[:64]
    return MaintenanceTaskRun(
        id=run_id,
        task_name="test_set_staging_cleanup",
        job_id=f"rvc-maintenance-{identity_hash}",
        idempotency_key_hash=identity_hash,
        dry_run=dry_run,
        status="queued",
        attempt_count=0,
        max_attempts=3,
        result_json={},
        created_by=user_id,
    )


def _put(root: Path, key: str, content: bytes = b"data") -> Path:
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.mark.asyncio
async def test_test_set_cleanup_two_phase_removes_late_put_and_never_deletes_canonical(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = app.state.settings
    settings.maintenance_cleanup_grace_seconds = 0
    settings.test_set_cleanup_late_writer_grace_seconds = 0
    settings.test_set_cleanup_confirmation_grace_seconds = 0
    settings.test_set_upload_write_stale_seconds = 1
    storage = app.state.storage
    assert isinstance(storage, LocalStorageAdapter)
    now = utc_now()
    async with app.state.database.session_factory() as session:
        admin = await session.scalar(select(User).where(User.role == "admin"))
        assert admin is not None
        test_set = LedgerTestSet(
            id="31000000-0000-4000-8000-000000000001",
            family_id="31000000-0000-4000-8000-000000000002",
            name="test-set-cleanup",
            revision=1,
            status="draft",
            item_count=0,
            created_by=admin.id,
        )
        target = _make_test_set_upload(
            test_set,
            upload_id="31000000-0000-4000-8000-000000000011",
            status="expired",
            expires_at=now - timedelta(days=1),
            storage_namespace_sha256=storage.namespace_fingerprint,
        )
        target.updated_at = now - timedelta(days=1)
        active_writer = _make_test_set_upload(
            test_set,
            upload_id="31000000-0000-4000-8000-000000000012",
            status="expired",
            expires_at=now - timedelta(days=1),
            storage_namespace_sha256=storage.namespace_fingerprint,
        )
        active_writer.updated_at = now - timedelta(days=1)
        active_writer.upload_write_token = "31000000-0000-4000-8000-000000000099"
        active_writer.upload_heartbeat_at = now
        finalizing = _make_test_set_upload(
            test_set,
            upload_id="31000000-0000-4000-8000-000000000013",
            status="finalizing",
            expires_at=now - timedelta(days=1),
            storage_namespace_sha256=storage.namespace_fingerprint,
        )
        finalizing.finalization_token = "31000000-0000-4000-8000-000000000098"
        completed = _make_test_set_upload(
            test_set,
            upload_id="31000000-0000-4000-8000-000000000014",
            status="completed",
            expires_at=now - timedelta(days=1),
            storage_namespace_sha256=storage.namespace_fingerprint,
        )
        run = _make_test_set_run(
            run_id="31000000-0000-4000-8000-000000000090",
            user_id=admin.id,
        )
        session.add_all([test_set, target, active_writer, finalizing, completed, run])
        await session.commit()

    for upload in (target, active_writer, finalizing, completed):
        _put(storage.root, upload.temporary_object_key)
    canonical = _put(storage.root, target.canonical_object_key, b"preserve-canonical")
    original_delete = storage.delete_object
    target_delete_count = 0

    async def delete_then_simulate_late_put(object_key: str) -> None:
        nonlocal target_delete_count
        await original_delete(object_key)
        if object_key == target.temporary_object_key:
            target_delete_count += 1
            if target_delete_count == 1:
                # Models an S3 PUT that began before URL expiry but becomes
                # visible only after the first cleanup deletion.
                _put(storage.root, object_key, b"late-put")

    monkeypatch.setattr(storage, "delete_object", delete_then_simulate_late_put)
    execution = await run_test_set_staging_cleanup(
        app.state.database,
        storage,
        settings,
        run_id=run.id,
    )

    assert execution.retry_required is False
    assert execution.result.deleted == 1
    assert execution.result.failed == 0
    assert target_delete_count == 2
    assert not (storage.root / target.temporary_object_key).exists()
    assert canonical.read_bytes() == b"preserve-canonical"
    assert (storage.root / active_writer.temporary_object_key).is_file()
    assert (storage.root / finalizing.temporary_object_key).is_file()
    assert (storage.root / completed.temporary_object_key).is_file()
    async with app.state.database.session_factory() as session:
        saved = await session.get(LedgerTestSetItemUploadSession, target.id)
        assert saved is not None
        assert saved.cleanup_claim_generation == saved.generation
        assert saved.cleanup_first_deleted_at is not None
        assert saved.cleanup_completed_at is not None
        assert saved.failure_code == "staging_cleanup_complete"
        actions = set(await session.scalars(select(AuditEvent.action)))
        assert "maintenance.test_set_staging_cleanup.first_deleted" in actions
        assert "maintenance.test_set_staging_cleanup.completed" in actions


@pytest.mark.asyncio
async def test_test_set_cleanup_dry_run_audits_without_claim_or_delete(
    app: FastAPI,
) -> None:
    settings = app.state.settings
    settings.maintenance_cleanup_grace_seconds = 0
    settings.test_set_cleanup_late_writer_grace_seconds = 0
    storage = app.state.storage
    assert isinstance(storage, LocalStorageAdapter)
    now = utc_now()
    async with app.state.database.session_factory() as session:
        admin = await session.scalar(select(User).where(User.role == "admin"))
        assert admin is not None
        test_set = LedgerTestSet(
            id="32000000-0000-4000-8000-000000000001",
            family_id="32000000-0000-4000-8000-000000000002",
            name="test-set-cleanup-preview",
            revision=1,
            status="draft",
            item_count=0,
            created_by=admin.id,
        )
        upload = _make_test_set_upload(
            test_set,
            upload_id="32000000-0000-4000-8000-000000000011",
            status="expired",
            expires_at=now - timedelta(days=1),
            storage_namespace_sha256=storage.namespace_fingerprint,
        )
        upload.updated_at = now - timedelta(days=1)
        run = _make_test_set_run(
            run_id="32000000-0000-4000-8000-000000000090",
            user_id=admin.id,
            dry_run=True,
        )
        session.add_all([test_set, upload, run])
        await session.commit()
    staging = _put(storage.root, upload.temporary_object_key)

    execution = await run_test_set_staging_cleanup(
        app.state.database,
        storage,
        settings,
        run_id=run.id,
    )
    assert execution.result.eligible == 1
    assert execution.result.deleted == 0
    assert staging.is_file()
    async with app.state.database.session_factory() as session:
        saved = await session.get(LedgerTestSetItemUploadSession, upload.id)
        assert saved is not None
        assert saved.cleanup_claim_run_id is None
        assert saved.cleanup_first_deleted_at is None
        preview = await session.scalar(
            select(AuditEvent.id).where(
                AuditEvent.action == "maintenance.test_set_staging_cleanup.previewed"
            )
        )
        assert preview is not None


@pytest.mark.asyncio
async def test_test_set_cleanup_namespace_mismatch_is_typed_and_retryable(
    app: FastAPI,
) -> None:
    settings = app.state.settings
    settings.maintenance_cleanup_grace_seconds = 0
    settings.test_set_cleanup_late_writer_grace_seconds = 0
    settings.test_set_cleanup_confirmation_grace_seconds = 0
    storage = app.state.storage
    assert isinstance(storage, LocalStorageAdapter)
    now = utc_now()
    async with app.state.database.session_factory() as session:
        admin = await session.scalar(select(User).where(User.role == "admin"))
        assert admin is not None
        test_set = LedgerTestSet(
            id="33000000-0000-4000-8000-000000000001",
            family_id="33000000-0000-4000-8000-000000000002",
            name="test-set-cleanup-namespace",
            revision=1,
            status="draft",
            item_count=0,
            created_by=admin.id,
        )
        upload = _make_test_set_upload(
            test_set,
            upload_id="33000000-0000-4000-8000-000000000011",
            status="expired",
            expires_at=now - timedelta(days=1),
            storage_namespace_sha256="0" * 64,
        )
        upload.updated_at = now - timedelta(days=1)
        run = _make_test_set_run(
            run_id="33000000-0000-4000-8000-000000000090",
            user_id=admin.id,
        )
        session.add_all([test_set, upload, run])
        await session.commit()
    staging = _put(storage.root, upload.temporary_object_key)

    execution = await run_test_set_staging_cleanup(
        app.state.database,
        storage,
        settings,
        run_id=run.id,
    )
    assert execution.retry_required is True
    assert execution.result.failure_codes == ["storage_namespace_mismatch"]
    assert staging.is_file()
    async with app.state.database.session_factory() as session:
        saved = await session.get(LedgerTestSetItemUploadSession, upload.id)
        assert saved is not None
        assert saved.cleanup_completed_at is None
        assert saved.failure_code == "staging_cleanup_pending"


@pytest.mark.asyncio
async def test_dataset_cleanup_two_phase_removes_late_put_and_preserves_canonical(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = app.state.settings
    settings.maintenance_cleanup_grace_seconds = 0
    settings.dataset_cleanup_late_writer_grace_seconds = 0
    settings.dataset_cleanup_confirmation_grace_seconds = 0
    settings.dataset_upload_write_stale_seconds = 1
    storage = app.state.storage
    assert isinstance(storage, LocalStorageAdapter)
    now = utc_now()
    async with app.state.database.session_factory() as session:
        admin = await session.scalar(select(User).where(User.role == "admin"))
        assert admin is not None
        dataset = Dataset(
            id="35000000-0000-4000-8000-000000000001",
            name="dataset-two-phase-cleanup",
            storage_uri="local:///pending",
            status="upload_pending",
            is_usable=False,
            created_by=admin.id,
        )
        target = _make_upload(
            dataset,
            upload_id="35000000-0000-4000-8000-000000000011",
            status="expired",
            expires_at=now - timedelta(days=1),
            storage_namespace_sha256=storage.namespace_fingerprint,
        )
        target.updated_at = now - timedelta(days=1)
        active_writer = _make_upload(
            dataset,
            upload_id="35000000-0000-4000-8000-000000000012",
            status="expired",
            expires_at=now - timedelta(days=1),
            storage_namespace_sha256=storage.namespace_fingerprint,
        )
        active_writer.updated_at = now - timedelta(days=1)
        active_writer.upload_write_token = "35000000-0000-4000-8000-000000000098"
        active_writer.upload_heartbeat_at = now
        run = _make_run(
            run_id="35000000-0000-4000-8000-000000000090",
            user_id=admin.id,
        )
        session.add_all([dataset, target, active_writer, run])
        await session.commit()
    target_staging = _put(storage.root, target.temporary_object_key)
    active_staging = _put(storage.root, active_writer.temporary_object_key)
    canonical_paths = [
        _put(storage.root, target.original_object_key, b"canonical-original"),
        _put(storage.root, target.prepared_flat_object_key, b"canonical-flat"),
        _put(storage.root, target.manifest_object_key, b"canonical-manifest"),
        _put(storage.root, target.quality_report_object_key, b"canonical-report"),
    ]
    original_delete = storage.delete_object
    deleted_keys: list[str] = []

    async def delete_then_late_publish(object_key: str) -> None:
        deleted_keys.append(object_key)
        await original_delete(object_key)
        if object_key == target.temporary_object_key and deleted_keys.count(object_key) == 1:
            _put(storage.root, object_key, b"late-presigned-put")

    monkeypatch.setattr(storage, "delete_object", delete_then_late_publish)
    execution = await run_dataset_staging_cleanup(
        app.state.database,
        storage,
        settings,
        run_id=run.id,
    )

    assert execution.retry_required is False
    assert execution.result.deleted == 1
    assert deleted_keys == [target.temporary_object_key, target.temporary_object_key]
    assert not target_staging.exists()
    assert active_staging.is_file()
    assert [path.read_bytes() for path in canonical_paths] == [
        b"canonical-original",
        b"canonical-flat",
        b"canonical-manifest",
        b"canonical-report",
    ]
    async with app.state.database.session_factory() as session:
        saved = await session.get(DatasetUploadSession, target.id)
        active = await session.get(DatasetUploadSession, active_writer.id)
        assert saved is not None and active is not None
        assert saved.cleanup_claim_generation == saved.generation
        assert saved.cleanup_first_deleted_at is not None
        assert saved.cleanup_completed_at is not None
        assert active.cleanup_claim_run_id is None


@pytest.mark.asyncio
async def test_cleanup_deletes_only_grace_eligible_staging_and_protects_active_canonical(
    app: FastAPI,
) -> None:
    settings = app.state.settings
    settings.maintenance_cleanup_grace_seconds = 60
    settings.dataset_cleanup_late_writer_grace_seconds = 60
    settings.dataset_cleanup_confirmation_grace_seconds = 0
    storage = app.state.storage
    assert isinstance(storage, LocalStorageAdapter)
    now = utc_now()
    async with app.state.database.session_factory() as session:
        admin = await session.scalar(select(User).where(User.role == "admin"))
        assert admin is not None
        dataset = Dataset(
            id="10000000-0000-4000-8000-000000000001",
            name="cleanup-target",
            storage_uri="local:///datasets/verified/target/original.zip",
            status="ready",
            flat_storage_uri="local:///datasets/verified/target/prepared_flat.zip",
            is_usable=True,
            created_by=admin.id,
        )
        old_pending = _make_upload(
            dataset,
            upload_id="10000000-0000-4000-8000-000000000011",
            status="pending",
            expires_at=now - timedelta(seconds=121),
            storage_namespace_sha256=storage.namespace_fingerprint,
        )
        old_failed = _make_upload(
            dataset,
            upload_id="10000000-0000-4000-8000-000000000012",
            status="failed",
            expires_at=now - timedelta(seconds=121),
            storage_namespace_sha256=storage.namespace_fingerprint,
            failure_code="staging_cleanup_pending",
        )
        old_failed.updated_at = now - timedelta(seconds=121)
        active_pending = _make_upload(
            dataset,
            upload_id="10000000-0000-4000-8000-000000000013",
            status="pending",
            expires_at=now + timedelta(hours=1),
            storage_namespace_sha256=storage.namespace_fingerprint,
        )
        active_finalizing = _make_upload(
            dataset,
            upload_id="10000000-0000-4000-8000-000000000014",
            status="finalizing",
            expires_at=now - timedelta(days=1),
            storage_namespace_sha256=storage.namespace_fingerprint,
        )
        completed = _make_upload(
            dataset,
            upload_id="10000000-0000-4000-8000-000000000015",
            status="completed",
            expires_at=now - timedelta(days=1),
            storage_namespace_sha256=storage.namespace_fingerprint,
        )
        run = _make_run(
            run_id="10000000-0000-4000-8000-000000000099",
            user_id=admin.id,
        )
        session.add_all(
            [
                dataset,
                old_pending,
                old_failed,
                active_pending,
                active_finalizing,
                completed,
                run,
            ]
        )
        await session.commit()

    for upload in (
        old_pending,
        old_failed,
        active_pending,
        active_finalizing,
        completed,
    ):
        _put(storage.root, upload.temporary_object_key)
    canonical_paths = [
        _put(storage.root, completed.original_object_key),
        _put(storage.root, completed.prepared_flat_object_key),
        _put(storage.root, completed.manifest_object_key),
        _put(storage.root, completed.quality_report_object_key),
    ]

    execution = await run_dataset_staging_cleanup(
        app.state.database,
        storage,
        settings,
        run_id=run.id,
    )

    assert execution.retry_required is False
    assert execution.result.deleted == 2
    assert not (storage.root / old_pending.temporary_object_key).exists()
    assert not (storage.root / old_failed.temporary_object_key).exists()
    for upload in (active_pending, active_finalizing, completed):
        assert (storage.root / upload.temporary_object_key).exists()
    assert all(path.exists() for path in canonical_paths)
    async with app.state.database.session_factory() as session:
        refreshed = await session.get(DatasetUploadSession, old_pending.id)
        assert refreshed is not None
        assert refreshed.status == "expired"
        assert refreshed.failure_code == "staging_cleanup_complete"
        assert refreshed.cleanup_completed_at is not None
        saved_run = await session.get(MaintenanceTaskRun, run.id)
        assert saved_run is not None
        assert saved_run.status == "completed"
        actions = set(
            await session.scalars(
                select(AuditEvent.action).where(AuditEvent.resource_id == run.id)
            )
        )
        assert "maintenance.dataset_staging_cleanup.finished" in actions


@pytest.mark.asyncio
async def test_cleanup_dry_run_and_competing_runs_are_idempotent(app: FastAPI) -> None:
    settings = app.state.settings
    settings.maintenance_cleanup_grace_seconds = 0
    settings.dataset_cleanup_late_writer_grace_seconds = 0
    settings.dataset_cleanup_confirmation_grace_seconds = 0
    storage = app.state.storage
    assert isinstance(storage, LocalStorageAdapter)
    now = utc_now()
    async with app.state.database.session_factory() as session:
        admin = await session.scalar(select(User).where(User.role == "admin"))
        assert admin is not None
        dataset = Dataset(
            id="20000000-0000-4000-8000-000000000001",
            name="dry-run-target",
            storage_uri="local:///pending",
            status="upload_pending",
            is_usable=False,
            created_by=admin.id,
        )
        upload = _make_upload(
            dataset,
            upload_id="20000000-0000-4000-8000-000000000011",
            status="expired",
            expires_at=now - timedelta(days=1),
            storage_namespace_sha256=storage.namespace_fingerprint,
        )
        upload.updated_at = now - timedelta(days=1)
        dry_run = _make_run(
            run_id="20000000-0000-4000-8000-000000000091",
            user_id=admin.id,
            dry_run=True,
        )
        first = _make_run(
            run_id="20000000-0000-4000-8000-000000000092",
            user_id=admin.id,
        )
        second = _make_run(
            run_id="20000000-0000-4000-8000-000000000093",
            user_id=admin.id,
        )
        session.add_all([dataset, upload, dry_run, first, second])
        await session.commit()
    staged = _put(storage.root, upload.temporary_object_key)

    preview = await run_dataset_staging_cleanup(
        app.state.database,
        storage,
        settings,
        run_id=dry_run.id,
    )
    assert preview.result.eligible == 1
    assert preview.result.deleted == 0
    assert staged.exists()
    async with app.state.database.session_factory() as session:
        unchanged = await session.get(DatasetUploadSession, upload.id)
        assert unchanged is not None
        assert unchanged.cleanup_claim_run_id is None

    first_execution = await run_dataset_staging_cleanup(
        app.state.database,
        storage,
        settings,
        run_id=first.id,
    )
    second_execution = await run_dataset_staging_cleanup(
        app.state.database,
        storage,
        settings,
        run_id=second.id,
    )
    replay = await run_dataset_staging_cleanup(
        app.state.database,
        storage,
        settings,
        run_id=first.id,
    )
    assert first_execution.result.deleted == 1
    assert second_execution.result.deleted == 0
    assert replay.result.deleted == 1
    assert not staged.exists()


@pytest.mark.asyncio
async def test_cleanup_storage_failure_is_typed_and_retried(app: FastAPI) -> None:
    settings = app.state.settings
    settings.maintenance_cleanup_grace_seconds = 0
    settings.dataset_cleanup_late_writer_grace_seconds = 0
    settings.dataset_cleanup_confirmation_grace_seconds = 0
    storage = app.state.storage
    assert isinstance(storage, LocalStorageAdapter)
    now = utc_now()
    async with app.state.database.session_factory() as session:
        admin = await session.scalar(select(User).where(User.role == "admin"))
        assert admin is not None
        dataset = Dataset(
            id="30000000-0000-4000-8000-000000000001",
            name="retry-target",
            storage_uri="local:///pending",
            status="upload_pending",
            is_usable=False,
            created_by=admin.id,
        )
        upload = _make_upload(
            dataset,
            upload_id="30000000-0000-4000-8000-000000000011",
            status="expired",
            expires_at=now - timedelta(days=1),
            storage_namespace_sha256=storage.namespace_fingerprint,
        )
        upload.updated_at = now - timedelta(days=1)
        run = _make_run(
            run_id="30000000-0000-4000-8000-000000000099",
            user_id=admin.id,
        )
        session.add_all([dataset, upload, run])
        await session.commit()
    staged = _put(storage.root, upload.temporary_object_key)
    real_delete = storage.delete_object
    calls = 0

    async def fail_once(key: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StorageError("secret backend detail")
        await real_delete(key)

    storage.delete_object = fail_once  # type: ignore[method-assign]
    deferred = await run_dataset_staging_cleanup(
        app.state.database,
        storage,
        settings,
        run_id=run.id,
    )
    assert deferred.retry_required is True
    assert deferred.result.failure_codes == ["staging_cleanup_failed"]
    assert staged.exists()
    async with app.state.database.session_factory() as session:
        pending = await session.get(MaintenanceTaskRun, run.id)
        assert pending is not None
        assert pending.status == "retrying"
        assert pending.last_error_code == "staging_cleanup_deferred"

    completed = await run_dataset_staging_cleanup(
        app.state.database,
        storage,
        settings,
        run_id=run.id,
    )
    assert completed.retry_required is False
    assert completed.result.deleted == 1
    assert not staged.exists()


@pytest.mark.asyncio
async def test_cleanup_namespace_mismatch_never_deletes_or_completes_claim(
    app: FastAPI,
) -> None:
    settings = app.state.settings
    settings.maintenance_cleanup_grace_seconds = 0
    settings.dataset_cleanup_late_writer_grace_seconds = 0
    settings.dataset_cleanup_confirmation_grace_seconds = 0
    storage = app.state.storage
    assert isinstance(storage, LocalStorageAdapter)
    now = utc_now()
    async with app.state.database.session_factory() as session:
        admin = await session.scalar(select(User).where(User.role == "admin"))
        assert admin is not None
        dataset = Dataset(
            id="31000000-0000-4000-8000-000000000001",
            name="unbound-cleanup-target",
            storage_uri="local:///pending",
            status="upload_pending",
            is_usable=False,
            created_by=admin.id,
        )
        upload = _make_upload(
            dataset,
            upload_id="31000000-0000-4000-8000-000000000011",
            status="expired",
            expires_at=now - timedelta(days=1),
            storage_namespace_sha256=UNBOUND_STORAGE_NAMESPACE_SHA256,
        )
        upload.updated_at = now - timedelta(days=1)
        run = _make_run(
            run_id="31000000-0000-4000-8000-000000000099",
            user_id=admin.id,
        )
        session.add_all([dataset, upload, run])
        await session.commit()
    staged = _put(storage.root, upload.temporary_object_key)

    execution = await run_dataset_staging_cleanup(
        app.state.database,
        storage,
        settings,
        run_id=run.id,
    )

    assert execution.retry_required is True
    assert execution.result.deleted == 0
    assert execution.result.failure_codes == ["storage_namespace_mismatch"]
    assert staged.exists()
    async with app.state.database.session_factory() as session:
        deferred = await session.get(DatasetUploadSession, upload.id)
        assert deferred is not None
        assert deferred.cleanup_completed_at is None
        assert deferred.failure_code == "staging_cleanup_pending"


@pytest.mark.asyncio
async def test_cleanup_completion_cannot_overwrite_reconciler_terminal_fence(
    app: FastAPI,
) -> None:
    run_id = "32000000-0000-4000-8000-000000000099"
    async with app.state.database.session_factory() as session:
        admin = await session.scalar(select(User).where(User.role == "admin"))
        assert admin is not None
        run = _make_run(run_id=run_id, user_id=admin.id)
        run.status = "failed"
        run.attempt_count = 1
        run.completed_at = utc_now()
        session.add(run)
        await session.commit()

    result = DatasetCleanupResult(
        run_id=run_id,
        dry_run=False,
        attempt=1,
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
    async with app.state.database.session_factory() as session:
        with pytest.raises(MaintenanceRunNotExecutable, match="lost execution ownership"):
            await _finish_run(session, result=result)
    async with app.state.database.session_factory() as session:
        fenced = await session.get(MaintenanceTaskRun, run_id)
        assert fenced is not None
        assert fenced.status == "failed"


@pytest.mark.asyncio
async def test_maintenance_run_heartbeat_is_exact_attempt_status_cas(app: FastAPI) -> None:
    database = app.state.database
    async with database.session_factory() as session:
        admin = await session.scalar(select(User).where(User.role == "admin"))
        assert admin is not None
        run = _make_run(
            run_id="00000000-0000-4000-8000-0000000008a1",
            user_id=admin.id,
        )
        run.status = "running"
        run.attempt_count = 2
        session.add(run)
        await session.commit()

    heartbeat = _RunHeartbeat(
        database,
        app.state.settings,
        run_id=run.id,
        attempt=2,
    )
    await heartbeat.pulse()
    async with database.session_factory() as session:
        saved = await session.get(MaintenanceTaskRun, run.id)
        assert saved is not None and saved.heartbeat_at is not None
        saved.status = "failed"
        saved.last_error_code = "reconciler_terminal_fence"
        await session.commit()

    with pytest.raises(MaintenanceRunNotExecutable, match="lost execution ownership"):
        await heartbeat.pulse()
    stale_attempt = _RunHeartbeat(
        database,
        app.state.settings,
        run_id=run.id,
        attempt=1,
    )
    with pytest.raises(MaintenanceRunNotExecutable, match="lost execution ownership"):
        await stale_attempt.pulse()
    async with database.session_factory() as session:
        saved = await session.get(MaintenanceTaskRun, run.id)
        assert saved is not None
        assert saved.status == "failed"
        assert saved.last_error_code == "reconciler_terminal_fence"


@pytest.mark.asyncio
async def test_maintenance_guard_heartbeats_long_operation_and_cancels_on_fence(
    app: FastAPI,
) -> None:
    database = app.state.database
    app.state.settings.maintenance_task_heartbeat_seconds = 1
    run_id = "00000000-0000-4000-8000-0000000008a2"
    async with database.session_factory() as session:
        admin = await session.scalar(select(User).where(User.role == "admin"))
        assert admin is not None
        run = _make_run(run_id=run_id, user_id=admin.id)
        run.status = "running"
        run.attempt_count = 1
        run.heartbeat_at = utc_now() - timedelta(seconds=10)
        session.add(run)
        await session.commit()
        initial_heartbeat = run.heartbeat_at

    started = asyncio.Event()
    release = asyncio.Event()

    async def long_operation() -> str:
        started.set()
        await release.wait()
        return "finished"

    heartbeat = _RunHeartbeat(
        database,
        app.state.settings,
        run_id=run_id,
        attempt=1,
    )
    guarded = asyncio.create_task(heartbeat.guard(long_operation()))
    await started.wait()
    await asyncio.sleep(1.1)
    async with database.session_factory() as session:
        saved = await session.get(MaintenanceTaskRun, run_id)
        assert saved is not None and saved.heartbeat_at is not None
        assert initial_heartbeat is not None
        observed_heartbeat = saved.heartbeat_at
        if observed_heartbeat.tzinfo is None:
            observed_heartbeat = observed_heartbeat.replace(tzinfo=initial_heartbeat.tzinfo)
        assert observed_heartbeat > initial_heartbeat
    release.set()
    assert await asyncio.wait_for(guarded, timeout=2) == "finished"

    async with database.session_factory() as session:
        saved = await session.get(MaintenanceTaskRun, run_id)
        assert saved is not None
        saved.status = "running"
        saved.attempt_count = 2
        await session.commit()

    blocked = asyncio.Event()
    cancelled = asyncio.Event()

    async def fenced_operation() -> None:
        blocked.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    fenced_heartbeat = _RunHeartbeat(
        database,
        app.state.settings,
        run_id=run_id,
        attempt=2,
    )
    fenced_guard = asyncio.create_task(fenced_heartbeat.guard(fenced_operation()))
    await blocked.wait()
    async with database.session_factory() as session:
        saved = await session.get(MaintenanceTaskRun, run_id)
        assert saved is not None
        saved.status = "failed"
        saved.last_error_code = "reconciler_terminal_fence"
        await session.commit()
    with pytest.raises(MaintenanceRunNotExecutable, match="lost execution ownership"):
        await asyncio.wait_for(fenced_guard, timeout=2)
    assert cancelled.is_set()


class _FakeMaintenanceQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.test_set_calls: list[tuple[str, str, int]] = []

    async def enqueue_dataset_cleanup(
        self,
        *,
        run_id: str,
        job_id: str,
        max_attempts: int,
        create_if_missing: bool = True,
    ) -> EnqueuedMaintenanceTask:
        assert create_if_missing is True
        existing = (run_id, job_id, max_attempts) in self.calls
        self.calls.append((run_id, job_id, max_attempts))
        return EnqueuedMaintenanceTask(job_id=job_id, existing=existing)

    async def enqueue_test_set_cleanup(
        self,
        *,
        run_id: str,
        job_id: str,
        max_attempts: int,
        create_if_missing: bool = True,
    ) -> EnqueuedMaintenanceTask:
        assert create_if_missing is True
        existing = (run_id, job_id, max_attempts) in self.test_set_calls
        self.test_set_calls.append((run_id, job_id, max_attempts))
        return EnqueuedMaintenanceTask(job_id=job_id, existing=existing)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_admin_only_enqueue_is_deterministic_and_accepts_no_callable(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    app.state.settings.rq_enabled = True
    queue = _FakeMaintenanceQueue()
    app.state.maintenance_queue = queue
    unauthenticated = await client.post(
        "/api/v1/admin/maintenance/dataset-staging-cleanup",
        headers={"Idempotency-Key": "cleanup-20260711"},
        json={"dry_run": False},
    )
    assert unauthenticated.status_code == 401

    async with app.state.database.session_factory() as session:
        user = User(
            email="user@example.test",
            password_hash=hash_password("ordinary-user-password"),
            role="user",
        )
        session.add(user)
        await session.commit()
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "user@example.test", "password": "ordinary-user-password"},
    )
    user_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    forbidden = await client.post(
        "/api/v1/admin/maintenance/dataset-staging-cleanup",
        headers={**user_headers, "Idempotency-Key": "cleanup-20260711"},
        json={"dry_run": False},
    )
    assert forbidden.status_code == 403

    headers = {**admin_headers, "Idempotency-Key": "cleanup-20260711"}
    first = await client.post(
        "/api/v1/admin/maintenance/dataset-staging-cleanup",
        headers=headers,
        json={"dry_run": False},
    )
    second = await client.post(
        "/api/v1/admin/maintenance/dataset-staging-cleanup",
        headers=headers,
        json={"dry_run": False},
    )
    assert first.status_code == 202
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["job_id"] == second.json()["job_id"]
    assert len(queue.calls) == 2
    status = await client.get(
        f"/api/v1/admin/maintenance/{first.json()['id']}",
        headers=admin_headers,
    )
    assert status.status_code == 200
    assert status.json()["status"] == "queued"

    test_set = await client.post(
        "/api/v1/admin/maintenance/test-set-staging-cleanup",
        headers=headers,
        json={"dry_run": False},
    )
    assert test_set.status_code == 202
    assert test_set.json()["task_name"] == "test_set_staging_cleanup"
    assert test_set.json()["id"] != first.json()["id"]
    assert test_set.json()["job_id"] != first.json()["job_id"]
    assert len(queue.test_set_calls) == 1
    test_set_status = await client.get(
        f"/api/v1/admin/maintenance/{test_set.json()['id']}",
        headers=admin_headers,
    )
    assert test_set_status.json()["task_name"] == "test_set_staging_cleanup"


class _ReconcileQueue:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.test_set_calls: list[str] = []
        self.create_requests: list[tuple[str, bool]] = []
        self.failures: dict[str, Exception] = {}
        self.existing_states: dict[str, str] = {}

    async def enqueue_dataset_cleanup(
        self,
        *,
        run_id: str,
        job_id: str,
        max_attempts: int,
        create_if_missing: bool = True,
    ) -> EnqueuedMaintenanceTask:
        del max_attempts
        self.calls.append(run_id)
        self.create_requests.append((run_id, create_if_missing))
        failure = self.failures.get(run_id)
        if failure is not None:
            raise failure
        state = self.existing_states.get(run_id)
        return EnqueuedMaintenanceTask(
            job_id=job_id,
            existing=state is not None,
            job_state=state or "queued",
        )

    async def enqueue_test_set_cleanup(
        self,
        *,
        run_id: str,
        job_id: str,
        max_attempts: int,
        create_if_missing: bool = True,
    ) -> EnqueuedMaintenanceTask:
        del max_attempts
        self.test_set_calls.append(run_id)
        self.create_requests.append((run_id, create_if_missing))
        failure = self.failures.get(run_id)
        if failure is not None:
            raise failure
        state = self.existing_states.get(run_id)
        return EnqueuedMaintenanceTask(
            job_id=job_id,
            existing=state is not None,
            job_state=state or "queued",
        )

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_reconciler_routes_test_set_lost_poisoned_and_final_started_jobs(
    app: FastAPI,
) -> None:
    settings = app.state.settings
    settings.maintenance_reconcile_interval_seconds = 0.25
    settings.maintenance_task_timeout_seconds = 30
    now = utc_now()
    async with app.state.database.session_factory() as session:
        admin = await session.scalar(select(User).where(User.role == "admin"))
        assert admin is not None
        lost = _make_test_set_run(
            run_id="34000000-0000-4000-8000-000000000091",
            user_id=admin.id,
        )
        poisoned = _make_test_set_run(
            run_id="34000000-0000-4000-8000-000000000092",
            user_id=admin.id,
        )
        final_started = _make_test_set_run(
            run_id="34000000-0000-4000-8000-000000000093",
            user_id=admin.id,
        )
        final_started.status = "running"
        final_started.attempt_count = final_started.max_attempts
        final_started.started_at = now - timedelta(minutes=10)
        final_started.heartbeat_at = now - timedelta(minutes=10)
        for run in (lost, poisoned, final_started):
            run.updated_at = now - timedelta(minutes=10)
        session.add_all([lost, poisoned, final_started])
        await session.commit()

    queue = _ReconcileQueue()
    queue.failures[poisoned.id] = MaintenanceQueueEnvelopeConflict(
        "maintenance_queue_envelope_mismatch"
    )
    queue.existing_states[final_started.id] = "started"
    result = await reconcile_maintenance_runs(
        app.state.database,
        queue,
        settings,
    )

    assert result.enqueued == 1
    assert result.terminal_failed == 1
    assert result.existing == 1
    assert set(queue.test_set_calls) == {lost.id, poisoned.id, final_started.id}
    assert queue.calls == []
    assert (lost.id, True) in queue.create_requests
    assert (poisoned.id, True) in queue.create_requests
    assert (final_started.id, False) in queue.create_requests
    async with app.state.database.session_factory() as session:
        saved_lost = await session.get(MaintenanceTaskRun, lost.id)
        saved_poisoned = await session.get(MaintenanceTaskRun, poisoned.id)
        saved_started = await session.get(MaintenanceTaskRun, final_started.id)
        assert saved_lost is not None and saved_lost.status == "queued"
        assert saved_poisoned is not None and saved_poisoned.status == "failed"
        assert saved_poisoned.last_error_code == "maintenance_queue_envelope_mismatch"
        assert saved_started is not None and saved_started.status == "running"
        failed_action = await session.scalar(
            select(AuditEvent.id).where(
                AuditEvent.resource_id == poisoned.id,
                AuditEvent.action
                == "maintenance.test_set_staging_cleanup.reconcile_failed",
            )
        )
        assert failed_action is not None


@pytest.mark.asyncio
async def test_admin_enqueue_persists_unavailable_and_poisoned_queue_states(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    class FailingQueue(_ReconcileQueue):
        def __init__(self, failure: Exception) -> None:
            super().__init__()
            self.failure = failure

        async def enqueue_dataset_cleanup(
            self,
            *,
            run_id: str,
            job_id: str,
            max_attempts: int,
            create_if_missing: bool = True,
        ) -> EnqueuedMaintenanceTask:
            del run_id, job_id, max_attempts, create_if_missing
            raise self.failure

    app.state.settings.rq_enabled = True
    app.state.maintenance_queue = FailingQueue(
        MaintenanceQueueUnavailable("secret redis detail")
    )
    unavailable = await client.post(
        "/api/v1/admin/maintenance/dataset-staging-cleanup",
        headers={**admin_headers, "Idempotency-Key": "cleanup-unavailable-20260711"},
        json={"dry_run": False},
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "maintenance_queue_unavailable"
    unavailable_id = unavailable.json()["detail"]["run_id"]

    app.state.maintenance_queue = FailingQueue(
        MaintenanceQueueEnvelopeConflict("maintenance_queue_poisoned_active")
    )
    poisoned = await client.post(
        "/api/v1/admin/maintenance/dataset-staging-cleanup",
        headers={**admin_headers, "Idempotency-Key": "cleanup-poisoned-20260711"},
        json={"dry_run": False},
    )
    assert poisoned.status_code == 503
    assert poisoned.json()["detail"]["code"] == "maintenance_queue_poisoned_active"
    poisoned_id = poisoned.json()["detail"]["run_id"]

    async with app.state.database.session_factory() as session:
        unavailable_run = await session.get(MaintenanceTaskRun, unavailable_id)
        poisoned_run = await session.get(MaintenanceTaskRun, poisoned_id)
        assert unavailable_run is not None
        assert unavailable_run.status == "enqueue_failed"
        assert unavailable_run.last_error_code == "maintenance_queue_unavailable"
        assert poisoned_run is not None
        assert poisoned_run.status == "failed"
        assert poisoned_run.last_error_code == "maintenance_queue_poisoned_active"
        assert poisoned_run.completed_at is not None


@pytest.mark.asyncio
async def test_reconciler_recovers_only_nonterminal_due_runs_with_attempt_fence(
    app: FastAPI,
) -> None:
    settings = app.state.settings
    settings.maintenance_reconcile_interval_seconds = 0.25
    settings.maintenance_reconcile_batch_size = 20
    settings.maintenance_task_timeout_seconds = 30
    now = utc_now()
    queue = _ReconcileQueue()
    ids = {
        "queued": "41000000-0000-4000-8000-000000000001",
        "retrying": "41000000-0000-4000-8000-000000000002",
        "enqueue_failed": "41000000-0000-4000-8000-000000000003",
        "stale_running": "41000000-0000-4000-8000-000000000004",
        "fresh_running": "41000000-0000-4000-8000-000000000005",
        "completed": "41000000-0000-4000-8000-000000000006",
        "failed": "41000000-0000-4000-8000-000000000007",
        "exhausted": "41000000-0000-4000-8000-000000000008",
        "final_running": "41000000-0000-4000-8000-000000000009",
    }
    async with app.state.database.session_factory() as session:
        admin = await session.scalar(select(User).where(User.role == "admin"))
        assert admin is not None
        runs = {
            name: _make_run(run_id=run_id, user_id=admin.id)
            for name, run_id in ids.items()
        }
        for run in runs.values():
            run.updated_at = now - timedelta(minutes=10)
        runs["retrying"].status = "retrying"
        runs["retrying"].attempt_count = 1
        runs["enqueue_failed"].status = "enqueue_failed"
        runs["enqueue_failed"].last_error_code = "maintenance_queue_unavailable"
        runs["stale_running"].status = "running"
        runs["stale_running"].attempt_count = 1
        runs["stale_running"].started_at = now - timedelta(minutes=10)
        runs["stale_running"].heartbeat_at = now - timedelta(minutes=10)
        runs["fresh_running"].status = "running"
        runs["fresh_running"].attempt_count = 1
        runs["fresh_running"].started_at = now
        runs["fresh_running"].heartbeat_at = now
        runs["completed"].status = "completed"
        runs["completed"].completed_at = now
        runs["failed"].status = "failed"
        runs["failed"].completed_at = now
        runs["exhausted"].attempt_count = runs["exhausted"].max_attempts
        runs["final_running"].status = "running"
        runs["final_running"].attempt_count = runs["final_running"].max_attempts
        runs["final_running"].started_at = now - timedelta(minutes=10)
        runs["final_running"].heartbeat_at = now - timedelta(minutes=10)
        queue.existing_states[ids["final_running"]] = "started"
        session.add_all(runs.values())
        await session.commit()

    result = await reconcile_maintenance_runs(
        app.state.database,
        queue,
        settings,
    )

    assert result.leader_acquired is True
    assert result.examined == 6
    assert result.enqueued == 4
    assert result.existing == 1
    assert result.terminal_failed == 1
    assert set(queue.calls) == {
        ids["queued"],
        ids["retrying"],
        ids["enqueue_failed"],
        ids["stale_running"],
        ids["exhausted"],
        ids["final_running"],
    }
    assert (ids["exhausted"], False) in queue.create_requests
    assert (ids["final_running"], False) in queue.create_requests
    async with app.state.database.session_factory() as session:
        saved = {
            name: await session.get(MaintenanceTaskRun, run_id)
            for name, run_id in ids.items()
        }
        assert saved["queued"] is not None and saved["queued"].status == "queued"
        assert saved["retrying"] is not None and saved["retrying"].status == "retrying"
        assert (
            saved["enqueue_failed"] is not None
            and saved["enqueue_failed"].status == "queued"
        )
        assert (
            saved["stale_running"] is not None
            and saved["stale_running"].status == "running"
        )
        assert (
            saved["fresh_running"] is not None
            and saved["fresh_running"].status == "running"
        )
        assert saved["completed"] is not None and saved["completed"].status == "completed"
        assert saved["failed"] is not None and saved["failed"].status == "failed"
        assert (
            saved["exhausted"] is not None
            and saved["exhausted"].status == "failed"
            and saved["exhausted"].last_error_code
            == "maintenance_attempts_exhausted"
        )
        assert (
            saved["final_running"] is not None
            and saved["final_running"].status == "running"
            and saved["final_running"].attempt_count
            == saved["final_running"].max_attempts
        )
        count = await session.scalar(select(func.count()).select_from(MaintenanceTaskRun))
        assert count == len(ids)


@pytest.mark.asyncio
async def test_reconciler_persists_redis_failure_and_active_poison_as_typed_states(
    app: FastAPI,
) -> None:
    settings = app.state.settings
    settings.maintenance_reconcile_interval_seconds = 0.25
    now = utc_now()
    unavailable_id = "42000000-0000-4000-8000-000000000001"
    poisoned_id = "42000000-0000-4000-8000-000000000002"
    queue = _ReconcileQueue()
    queue.failures[unavailable_id] = MaintenanceQueueUnavailable("unavailable")
    queue.failures[poisoned_id] = MaintenanceQueueEnvelopeConflict(
        "maintenance_queue_poisoned_active"
    )
    async with app.state.database.session_factory() as session:
        admin = await session.scalar(select(User).where(User.role == "admin"))
        assert admin is not None
        unavailable = _make_run(run_id=unavailable_id, user_id=admin.id)
        poisoned = _make_run(run_id=poisoned_id, user_id=admin.id)
        unavailable.updated_at = now - timedelta(minutes=1)
        poisoned.updated_at = now - timedelta(minutes=1)
        session.add_all([unavailable, poisoned])
        await session.commit()

    unavailable_result = await reconcile_maintenance_runs(
        app.state.database,
        queue,
        settings,
    )
    queue.failures.pop(unavailable_id)
    poisoned_result = await reconcile_maintenance_runs(
        app.state.database,
        queue,
        settings,
    )

    assert unavailable_result.enqueue_failed == 1
    assert poisoned_result.terminal_failed == 1
    async with app.state.database.session_factory() as session:
        unavailable = await session.get(MaintenanceTaskRun, unavailable_id)
        poisoned = await session.get(MaintenanceTaskRun, poisoned_id)
        assert unavailable is not None
        assert unavailable.status == "enqueue_failed"
        assert unavailable.last_error_code == "maintenance_queue_unavailable"
        assert poisoned is not None
        assert poisoned.status == "failed"
        assert poisoned.last_error_code == "maintenance_queue_poisoned_active"
        assert poisoned.completed_at is not None


@pytest.mark.asyncio
async def test_reconciler_concurrent_cycles_have_one_local_leader(
    app: FastAPI,
) -> None:
    settings = app.state.settings
    settings.maintenance_reconcile_interval_seconds = 0.25
    run_id = "43000000-0000-4000-8000-000000000001"
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowQueue(_ReconcileQueue):
        async def enqueue_dataset_cleanup(
            self,
            *,
            run_id: str,
            job_id: str,
            max_attempts: int,
            create_if_missing: bool = True,
        ) -> EnqueuedMaintenanceTask:
            del create_if_missing, max_attempts
            self.calls.append(run_id)
            started.set()
            await release.wait()
            return EnqueuedMaintenanceTask(job_id=job_id, existing=False)

    queue = SlowQueue()
    async with app.state.database.session_factory() as session:
        admin = await session.scalar(select(User).where(User.role == "admin"))
        assert admin is not None
        run = _make_run(run_id=run_id, user_id=admin.id)
        run.updated_at = utc_now() - timedelta(minutes=1)
        session.add(run)
        await session.commit()

    leader_task = asyncio.create_task(
        reconcile_maintenance_runs(app.state.database, queue, settings)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    standby = await reconcile_maintenance_runs(app.state.database, queue, settings)
    release.set()
    leader = await asyncio.wait_for(leader_task, timeout=2)

    assert leader.leader_acquired is True
    assert standby.leader_acquired is False
    assert queue.calls == [run_id]


@pytest.mark.asyncio
async def test_periodic_reconciler_reports_readiness_and_stops_promptly(
    app: FastAPI,
) -> None:
    settings = app.state.settings
    settings.maintenance_reconcile_interval_seconds = 0.25
    settings.maintenance_reconcile_stale_seconds = 5
    reconciler = MaintenanceReconciler(
        app.state.database,
        _ReconcileQueue(),
        settings,
    )
    task = asyncio.create_task(reconciler.run())
    for _ in range(100):
        if reconciler.last_completed_at is not None:
            break
        await asyncio.sleep(0.01)
    assert reconciler.readiness() == ("ok", True)
    reconciler.last_completed_at = utc_now() - timedelta(seconds=6)
    assert reconciler.readiness() == ("stale", False)

    reconciler.stop()
    await asyncio.wait_for(task, timeout=2)
    assert reconciler.readiness() == ("stopped", False)


@pytest.mark.asyncio
async def test_readiness_fails_closed_for_missing_or_stale_rq_worker(
    app: FastAPI,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRedis:
        async def ping(self) -> bool:
            return True

        async def aclose(self) -> None:
            return None

    class FakeProbe:
        def __init__(self, result: tuple[str, bool]) -> None:
            self.result = result

        async def readiness(self) -> tuple[str, bool]:
            return self.result

    class FakeReconciler:
        def readiness(self) -> tuple[str, bool]:
            return "ok", True

    monkeypatch.setattr(
        "redis.asyncio.Redis.from_url",
        lambda _url: FakeRedis(),
    )
    app.state.settings.redis_url = "redis://internal.invalid/0"
    app.state.settings.readiness_check_redis = True
    app.state.settings.rq_enabled = True
    app.state.rq_readiness = FakeProbe(("stale", False))
    app.state.maintenance_reconciler = FakeReconciler()
    stale = await client.get("/ready")
    assert stale.status_code == 503
    assert stale.json()["checks"] == {
        "database": "ok",
        "redis": "ok",
        "rq_worker": "stale",
        "maintenance_reconciler": "ok",
        "artifact_cleanup_reconciler": "stopped",
        "mlflow": "disabled",
    }
    app.state.rq_readiness = FakeProbe(("ok", True))
    ready = await client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"]["rq_worker"] == "ok"


@pytest.mark.asyncio
async def test_rq_adapter_uses_fixed_json_task_and_bounded_retry(
    monkeypatch: pytest.MonkeyPatch,
    app: FastAPI,
) -> None:
    settings = app.state.settings
    settings.redis_url = "redis://127.0.0.1:1/0"
    settings.maintenance_task_max_attempts = 3
    connection = Redis.from_url(settings.redis_url)
    adapter = RqMaintenanceQueue(settings, connection=connection)
    captured: dict[str, Any] = {}

    class FakeLock:
        def acquire(self, *, blocking: bool) -> bool:
            assert blocking is True
            return True

        def release(self) -> None:
            return None

    def missing_job(*_args: Any, **_kwargs: Any) -> Job:
        raise NoSuchJobError

    def enqueue_call(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(Job, "fetch", missing_job)
    monkeypatch.setattr(adapter.queue, "enqueue_call", enqueue_call)
    monkeypatch.setattr(connection, "lock", lambda *_args, **_kwargs: FakeLock())
    result = await adapter.enqueue_dataset_cleanup(
        run_id="40000000-0000-4000-8000-000000000001",
        job_id=f"rvc-maintenance-{'f' * 64}",
        max_attempts=3,
    )
    assert result.existing is False
    assert captured["func"] == DATASET_CLEANUP_TASK_PATH
    assert captured["args"] == ("40000000-0000-4000-8000-000000000001",)
    assert captured["kwargs"] is None
    assert captured["timeout"] == settings.maintenance_task_timeout_seconds
    assert captured["retry"].intervals == [30, 60]
    assert captured["at_front"] is False
    assert captured["depends_on"] is None
    assert captured["meta"] is None
    assert captured["repeat"] is None
    assert captured["on_success"] is None
    assert captured["on_failure"] is None
    assert captured["on_stopped"] is None
    captured.clear()
    test_set_result = await adapter.enqueue_test_set_cleanup(
        run_id="40000000-0000-4000-8000-000000000002",
        job_id=f"rvc-maintenance-{'e' * 64}",
        max_attempts=3,
    )
    assert test_set_result.existing is False
    assert captured["func"] == TEST_SET_CLEANUP_TASK_PATH
    assert captured["description"] == "allowlisted TestSet staging cleanup"
    assert captured["args"] == ("40000000-0000-4000-8000-000000000002",)
    await adapter.close()


def _maintenance_worker_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "process_role": "maintenance",
        "rq_enabled": True,
        "redis_url": "redis://127.0.0.1:6379/0",
    }
    values.update(overrides)
    return Settings(**values)


def _rq_job(
    func: str = DATASET_CLEANUP_TASK_PATH,
    *,
    serializer: type[JSONSerializer] | type[DefaultSerializer] = JSONSerializer,
    description: str | None = None,
) -> tuple[Job, Queue]:
    connection = Mock(spec=Redis)
    connection.scard.return_value = 0
    queue = Queue("rvc-maintenance", connection=connection, serializer=serializer)
    job = queue.create_job(
        func,
        args=("40000000-0000-4000-8000-000000000001",),
        kwargs=None,
        result_ttl=86_400,
        ttl=86_400,
        description=(
            description
            or (
                "allowlisted TestSet staging cleanup"
                if func == TEST_SET_CLEANUP_TASK_PATH
                else "allowlisted Dataset staging cleanup"
            )
        ),
        timeout=300,
        job_id=f"rvc-maintenance-{'a' * 64}",
        failure_ttl=604_800,
        retry=Retry(max=2, interval=[30, 60]),
    )
    return job, queue


@pytest.mark.asyncio
async def test_rq_adapter_accepts_only_exact_active_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _maintenance_worker_settings()
    job, _ = _rq_job()
    connection = job.connection
    connection.hget.return_value = b"queued"
    connection.lpos.side_effect = [None, 0]
    adapter = RqMaintenanceQueue(settings, connection=connection)
    monkeypatch.setattr(Job, "fetch", lambda *_args, **_kwargs: job)

    result = await adapter.enqueue_dataset_cleanup(
        run_id="40000000-0000-4000-8000-000000000001",
        job_id=job.id,
        max_attempts=3,
    )

    assert result == EnqueuedMaintenanceTask(
        job_id=job.id,
        existing=True,
        job_state="queued",
    )
    connection.eval.assert_not_called()


@pytest.mark.asyncio
async def test_rq_adapter_quarantines_poison_without_resolving_callback_or_dependents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _maintenance_worker_settings()
    job, _ = _rq_job()
    connection = job.connection
    connection.hget.return_value = b"queued"
    connection.lpos.side_effect = [None, 0]
    connection.eval.return_value = b"queued"
    connection.scard.side_effect = [0, 1]
    job._success_callback_name = "os.getenv"
    invoked = False

    def forbidden(*_args: object, **_kwargs: object) -> str:
        nonlocal invoked
        invoked = True
        return "forbidden"

    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(Job, "fetch", lambda *_args, **_kwargs: job)
    adapter = RqMaintenanceQueue(settings, connection=connection)
    enqueued: list[dict[str, Any]] = []
    monkeypatch.setattr(
        adapter.queue,
        "enqueue_call",
        lambda **kwargs: enqueued.append(kwargs),
    )

    result = await adapter.enqueue_dataset_cleanup(
        run_id="40000000-0000-4000-8000-000000000001",
        job_id=job.id,
        max_attempts=3,
    )

    assert result.existing is False
    assert result.repaired is True
    assert result.repair_code == "maintenance_queue_envelope_mismatch"
    assert len(enqueued) == 1
    assert invoked is False
    connection.eval.assert_called_once()


@pytest.mark.asyncio
async def test_rq_adapter_never_replaces_poisoned_started_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _maintenance_worker_settings()
    job, _ = _rq_job("os.getenv")
    connection = job.connection
    connection.hget.return_value = b"started"
    monkeypatch.setattr(Job, "fetch", lambda *_args, **_kwargs: job)
    adapter = RqMaintenanceQueue(settings, connection=connection)
    enqueue_call = Mock()
    monkeypatch.setattr(adapter.queue, "enqueue_call", enqueue_call)

    with pytest.raises(MaintenanceQueueEnvelopeConflict) as exc_info:
        await adapter.enqueue_dataset_cleanup(
            run_id="40000000-0000-4000-8000-000000000001",
            job_id=job.id,
            max_attempts=3,
        )

    assert exc_info.value.code == "maintenance_queue_poisoned_active"
    connection.eval.assert_not_called()
    enqueue_call.assert_not_called()


@pytest.mark.asyncio
async def test_rq_adapter_inspect_only_preserves_exact_started_and_never_creates_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _maintenance_worker_settings()
    job, _ = _rq_job()
    connection = job.connection
    connection.hget.return_value = b"started"
    adapter = RqMaintenanceQueue(settings, connection=connection)
    monkeypatch.setattr(Job, "fetch", lambda *_args, **_kwargs: job)
    enqueue_call = Mock()
    monkeypatch.setattr(adapter.queue, "enqueue_call", enqueue_call)

    active = await adapter.enqueue_dataset_cleanup(
        run_id="40000000-0000-4000-8000-000000000001",
        job_id=job.id,
        max_attempts=3,
        create_if_missing=False,
    )
    assert active.existing is True
    assert active.job_state == "started"

    monkeypatch.setattr(
        Job,
        "fetch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(NoSuchJobError),
    )
    missing = await adapter.enqueue_dataset_cleanup(
        run_id="40000000-0000-4000-8000-000000000001",
        job_id=job.id,
        max_attempts=3,
        create_if_missing=False,
    )
    assert missing.existing is False
    assert missing.job_state == "missing"
    enqueue_call.assert_not_called()


@pytest.mark.asyncio
async def test_rq_adapter_recreates_exact_terminal_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _maintenance_worker_settings()
    job, _ = _rq_job()
    connection = job.connection
    connection.hget.return_value = b"finished"
    connection.eval.return_value = b"finished"
    monkeypatch.setattr(Job, "fetch", lambda *_args, **_kwargs: job)
    adapter = RqMaintenanceQueue(settings, connection=connection)
    enqueue_call = Mock(return_value=job)
    monkeypatch.setattr(adapter.queue, "enqueue_call", enqueue_call)

    result = await adapter.enqueue_dataset_cleanup(
        run_id="40000000-0000-4000-8000-000000000001",
        job_id=job.id,
        max_attempts=3,
    )

    assert result.repaired is True
    assert result.repair_code == "maintenance_queue_inactive_job"
    enqueue_call.assert_called_once()


def test_shared_queue_policy_covers_every_non_execution_surface() -> None:
    settings = _maintenance_worker_settings()

    def assert_rejected(mutate: Any) -> None:
        job, queue = _rq_job()
        mutate(job, queue)
        assert maintenance_job_is_allowlisted(job, queue, settings) is False

    mutations = (
        lambda job, _queue: setattr(job, "origin", "other-queue"),
        lambda job, _queue: setattr(job, "description", "other-description"),
        lambda job, _queue: setattr(job, "timeout", 301),
        lambda job, _queue: setattr(job, "result_ttl", 1),
        lambda job, _queue: setattr(job, "failure_ttl", 1),
        lambda job, _queue: setattr(job, "ttl", 1),
        lambda job, _queue: setattr(job, "group_id", "group"),
        lambda job, _queue: setattr(job, "allow_dependency_failures", True),
        lambda job, _queue: setattr(job, "enqueue_at_front", True),
        lambda job, _queue: setattr(job, "repeats_left", 1),
        lambda job, _queue: setattr(job, "repeat_intervals", [1]),
        lambda job, _queue: setattr(job, "meta", {"poison": True}),
        lambda job, _queue: setattr(job, "_dependency_ids", ["dependency"]),
        lambda job, _queue: setattr(job, "_failure_callback_name", "os.getenv"),
    )
    for mutate in mutations:
        assert_rejected(mutate)


def _uninitialized_worker(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AllowlistedMaintenanceWorker, list[bool]]:
    worker = object.__new__(AllowlistedMaintenanceWorker)
    worker.settings = settings
    rejected: list[bool] = []

    def reject(
        _job: Job,
        _queue: Queue,
        *,
        execution_prepared: bool,
    ) -> None:
        rejected.append(execution_prepared)

    monkeypatch.setattr(worker, "_reject_job", reject)
    monkeypatch.setattr(worker, "set_state", lambda _state: None)
    return worker, rejected


def test_rq_worker_policy_accepts_only_fixed_json_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _maintenance_worker_settings()
    job, queue = _rq_job()
    assert maintenance_job_is_allowlisted(job, queue, settings) is True
    test_set_job, test_set_queue = _rq_job(TEST_SET_CLEANUP_TASK_PATH)
    assert maintenance_job_is_allowlisted(test_set_job, test_set_queue, settings) is True
    crossed_job, crossed_queue = _rq_job(
        TEST_SET_CLEANUP_TASK_PATH,
        description="allowlisted Dataset staging cleanup",
    )
    assert maintenance_job_is_allowlisted(crossed_job, crossed_queue, settings) is False

    pickle_job, pickle_queue = _rq_job(serializer=DefaultSerializer)
    pickle_job._args = UNEVALUATED
    pickle_job._kwargs = UNEVALUATED
    pickle_deserialized = False

    def forbidden_pickle_loads(_value: object) -> object:
        nonlocal pickle_deserialized
        pickle_deserialized = True
        raise AssertionError("pickle payload must not be deserialized")

    monkeypatch.setattr(DefaultSerializer, "loads", forbidden_pickle_loads)
    assert maintenance_job_is_allowlisted(pickle_job, pickle_queue, settings) is False
    assert pickle_deserialized is False
    job.meta = {"unexpected": "surface"}
    assert maintenance_job_is_allowlisted(job, queue, settings) is False
    non_integer_retry, retry_queue = _rq_job()
    non_integer_retry.retry_intervals = [30.0, 60]
    assert maintenance_job_is_allowlisted(non_integer_retry, retry_queue, settings) is False
    boolean_retry, boolean_queue = _rq_job()
    boolean_retry.retries_left = True
    assert maintenance_job_is_allowlisted(boolean_retry, boolean_queue, settings) is False


@pytest.mark.parametrize("func", ["os.getenv", "os.path.basename"])
def test_rq_worker_rejects_redis_selected_callable_without_executing_it(
    func: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoked = False

    def forbidden(*_args: object, **_kwargs: object) -> str:
        nonlocal invoked
        invoked = True
        return "forbidden"

    if func == "os.getenv":
        monkeypatch.setattr(os, "getenv", forbidden)
    else:
        monkeypatch.setattr(os.path, "basename", forbidden)
    settings = _maintenance_worker_settings()
    job, queue = _rq_job(func)
    worker, rejected = _uninitialized_worker(settings, monkeypatch)

    def base_execute(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("base RQ execution must not be reached")

    monkeypatch.setattr(Worker, "execute_job", base_execute)
    worker.execute_job(job, queue)
    assert rejected == [False]
    assert invoked is False


def test_scheduler_promoted_arbitrary_envelope_is_rejected_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _maintenance_worker_settings()
    job, queue = _rq_job("os.getenv")
    scheduler_connection = Mock()
    pipeline = Mock()
    pipeline_context = Mock()
    pipeline_context.__enter__ = Mock(return_value=pipeline)
    pipeline_context.__exit__ = Mock(return_value=False)
    scheduler_connection.pipeline.return_value = pipeline_context
    promoted: list[Job] = []

    class FakeRegistry:
        name = "rvc-maintenance"

        def get_jobs_to_schedule(self, _timestamp: int) -> list[str]:
            return [job.id]

        def remove(self, job_id: str, *, pipeline: object) -> None:
            assert job_id == job.id

    class FakeQueue:
        def __init__(
            self,
            name: str,
            *,
            connection: object,
            serializer: object,
        ) -> None:
            assert name == "rvc-maintenance"
            assert connection is scheduler_connection
            assert serializer is JSONSerializer

        def _enqueue_job(
            self,
            candidate: Job,
            *,
            pipeline: object,
            at_front: bool,
        ) -> None:
            assert at_front is False
            candidate._status = JobStatus.QUEUED
            promoted.append(candidate)

    scheduler = object.__new__(RQScheduler)
    scheduler._status = RQScheduler.Status.STOPPED
    scheduler._scheduled_job_registries = [FakeRegistry()]  # type: ignore[list-item]
    scheduler._acquired_locks = {"rvc-maintenance"}
    scheduler._connection = scheduler_connection
    scheduler.serializer = JSONSerializer
    monkeypatch.setattr(
        "rq.scheduler.Job.fetch_many",
        lambda _ids, *, connection, serializer: [job],
    )
    monkeypatch.setattr("rq.scheduler.Queue", FakeQueue)

    scheduler.enqueue_scheduled_jobs()

    assert promoted == [job]
    worker, rejected = _uninitialized_worker(settings, monkeypatch)

    def base_execute(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("scheduler-promoted arbitrary callable must not execute")

    monkeypatch.setattr(Worker, "execute_job", base_execute)
    worker.execute_job(promoted[0], queue)

    assert rejected == [False]


@pytest.mark.parametrize(
    "callback_field",
    ["_success_callback_name", "_failure_callback_name", "_stopped_callback_name"],
)
def test_rq_worker_rejects_callback_without_importing_or_executing_it(
    callback_field: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoked = False

    def forbidden(*_args: object, **_kwargs: object) -> str:
        nonlocal invoked
        invoked = True
        return "forbidden"

    monkeypatch.setattr(os, "getenv", forbidden)
    settings = _maintenance_worker_settings()
    job, queue = _rq_job()
    setattr(job, callback_field, "os.getenv")
    worker, rejected = _uninitialized_worker(settings, monkeypatch)

    def base_perform(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("base RQ perform must not be reached")

    monkeypatch.setattr(Worker, "perform_job", base_perform)
    assert worker.perform_job(job, queue) is False
    assert rejected == [True]
    assert invoked is False


def test_rq_worker_persists_policy_rejection_as_generic_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _maintenance_worker_settings()
    job, queue = _rq_job("os.getenv")
    pipeline = Mock()
    pipeline_context = Mock()
    pipeline_context.__enter__ = Mock(return_value=pipeline)
    pipeline_context.__exit__ = Mock(return_value=False)
    job.connection.pipeline.return_value = pipeline_context
    failure_handler = Mock()
    monkeypatch.setattr(job, "_handle_failure", failure_handler)
    worker = object.__new__(AllowlistedMaintenanceWorker)
    worker.settings = settings
    worker.connection = job.connection
    worker.name = "test-maintenance-worker"
    worker.execution = None

    worker._reject_job(job, queue, execution_prepared=False)

    assert job.retries_left == 0
    assert job._status == JobStatus.FAILED
    failure_handler.assert_called_once_with(
        "Maintenance job rejected by execution policy",
        pipeline=pipeline,
        worker_name="test-maintenance-worker",
    )
    pipeline.execute.assert_called_once_with()


def test_maintenance_role_has_no_auth_secret_or_http_app_escape() -> None:
    settings = _maintenance_worker_settings()
    with pytest.raises(ValueError, match="PROCESS_ROLE=api"):
        create_app(settings)
    with pytest.raises(ValidationError, match="must not receive unrelated auth secrets"):
        _maintenance_worker_settings(jwt_secret="x" * 64)
    with pytest.raises(ValidationError, match="must not receive unrelated auth secrets"):
        _maintenance_worker_settings(worker_bootstrap_token="bootstrap-secret")
    with pytest.raises(ValidationError, match="must not receive unrelated auth secrets"):
        _maintenance_worker_settings(mlflow_tracking_token="mlflow-secret")


def test_production_maintenance_role_needs_no_api_auth_or_presign_secret() -> None:
    settings = _maintenance_worker_settings(
        environment="production",
        database_url="postgresql+asyncpg://maintenance@postgres/rvc",
        storage_backend="s3",
        s3_endpoint_url="http://minio:9000",
        s3_access_key_id="maintenance-key",
        s3_secret_access_key="maintenance-secret",
    )
    assert settings.process_role == "maintenance"
    assert settings.worker_bootstrap_token is None
    assert settings.jwt_secret_file is None
    assert settings.s3_presign_endpoint_url is None


def test_cleanup_grace_cannot_be_shorter_than_upload_ttl() -> None:
    with pytest.raises(ValidationError, match="MAINTENANCE_CLEANUP_GRACE_SECONDS"):
        Settings(
            dataset_upload_ttl_seconds=600,
            maintenance_cleanup_grace_seconds=599,
        )


def test_test_set_cleanup_and_heartbeat_windows_are_fail_closed() -> None:
    with pytest.raises(ValidationError, match="TEST_SET_CLEANUP_LATE_WRITER_GRACE_SECONDS"):
        Settings(
            test_set_upload_ttl_seconds=600,
            test_set_cleanup_late_writer_grace_seconds=599,
        )
    with pytest.raises(ValidationError, match="TEST_SET_UPLOAD_WRITE_HEARTBEAT_SECONDS"):
        Settings(
            test_set_upload_write_heartbeat_seconds=10,
            test_set_upload_write_stale_seconds=30,
        )
    with pytest.raises(ValidationError, match="TEST_SET_FINALIZING_HEARTBEAT_SECONDS"):
        Settings(
            test_set_finalizing_heartbeat_seconds=20,
            test_set_finalizing_stale_seconds=60,
        )
    with pytest.raises(
        ValidationError,
        match="TEST_SET_CLEANUP_CONFIRMATION_GRACE_SECONDS",
    ):
        Settings(
            maintenance_task_timeout_seconds=180,
            dataset_cleanup_confirmation_grace_seconds=59,
            test_set_cleanup_confirmation_grace_seconds=60,
        )


def test_dataset_cleanup_and_heartbeat_windows_are_fail_closed() -> None:
    with pytest.raises(ValidationError, match="DATASET_CLEANUP_LATE_WRITER_GRACE_SECONDS"):
        Settings(
            dataset_upload_ttl_seconds=600,
            dataset_cleanup_late_writer_grace_seconds=599,
        )
    with pytest.raises(ValidationError, match="DATASET_UPLOAD_WRITE_HEARTBEAT_SECONDS"):
        Settings(
            dataset_upload_write_heartbeat_seconds=10,
            dataset_upload_write_stale_seconds=30,
        )
    with pytest.raises(ValidationError, match="DATASET_FINALIZING_HEARTBEAT_SECONDS"):
        Settings(
            dataset_finalizing_heartbeat_seconds=20,
            dataset_finalizing_stale_seconds=60,
        )
    with pytest.raises(
        ValidationError,
        match="DATASET_CLEANUP_CONFIRMATION_GRACE_SECONDS",
    ):
        Settings(
            maintenance_task_timeout_seconds=180,
            dataset_cleanup_confirmation_grace_seconds=60,
        )


def test_reconciler_stale_threshold_must_exceed_two_intervals() -> None:
    with pytest.raises(ValidationError, match="MAINTENANCE_RECONCILE_STALE_SECONDS"):
        Settings(
            maintenance_reconcile_interval_seconds=10,
            maintenance_reconcile_stale_seconds=20,
        )


def test_rq_worker_entrypoint_rejects_api_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rvc_manager_api import rq_worker

    monkeypatch.setattr(rq_worker, "Settings", lambda: Settings(process_role="api"))
    with pytest.raises(SystemExit, match="PROCESS_ROLE=maintenance"):
        rq_worker.main()


def test_rq_worker_enables_scheduler_only_to_deliver_bounded_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rvc_manager_api import rq_worker

    settings = _maintenance_worker_settings()
    connection = object()
    queue = object()
    work_calls: list[bool] = []

    class FakeWorker:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        def work(self, *, with_scheduler: bool) -> None:
            work_calls.append(with_scheduler)

    monkeypatch.setattr(rq_worker, "Settings", lambda: settings)
    monkeypatch.setattr(rq_worker.Redis, "from_url", lambda _url: connection)
    monkeypatch.setattr(rq_worker, "Queue", lambda *_args, **_kwargs: queue)
    monkeypatch.setattr(rq_worker, "AllowlistedMaintenanceWorker", FakeWorker)

    rq_worker.main()

    assert work_calls == [True]


def test_rq_scheduler_uses_one_distributed_lock_and_separate_worker_heartbeat_key() -> None:
    class FakeConnectionPool:
        connection_kwargs: dict[str, object] = {}
        connection_class = object

    class FakeRedis:
        def __init__(self) -> None:
            self.connection_pool = FakeConnectionPool()
            self.values: dict[str, object] = {}

        def set(self, key: str, value: object, *, nx: bool, ex: int) -> bool:
            assert nx is True
            assert ex > 0
            if key in self.values:
                return False
            self.values[key] = value
            return True

    connection = FakeRedis()
    first = RQScheduler(
        ["rvc-maintenance"],
        connection=connection,  # type: ignore[arg-type]
        serializer=JSONSerializer,
    )
    second = RQScheduler(
        ["rvc-maintenance"],
        connection=connection,  # type: ignore[arg-type]
        serializer=JSONSerializer,
    )
    first._connection = connection
    second._connection = connection

    assert first.acquire_locks() == {"rvc-maintenance"}
    assert second.acquire_locks() == set()
    assert RQScheduler.get_locking_key("rvc-maintenance") == (
        "rq:scheduler-lock:rvc-maintenance"
    )
    assert RQScheduler.get_locking_key("rvc-maintenance") != (
        "rq:workers:rvc-maintenance"
    )


@pytest.mark.asyncio
async def test_maintenance_task_rejects_api_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rvc_manager_api import maintenance_tasks

    monkeypatch.setattr(maintenance_tasks, "Settings", lambda: Settings(process_role="api"))
    with pytest.raises(RuntimeError, match="PROCESS_ROLE=maintenance"):
        await maintenance_tasks._execute_dataset_staging_cleanup(
            "40000000-0000-4000-8000-000000000001"
        )
    with pytest.raises(RuntimeError, match="PROCESS_ROLE=maintenance"):
        await maintenance_tasks._execute_test_set_staging_cleanup(
            "40000000-0000-4000-8000-000000000002"
        )
