from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from rvc_manager_api.config import Settings
from rvc_manager_api.models import (
    Artifact,
    ArtifactUploadSession,
    AuditEvent,
    Job,
    JobLease,
    User,
)
from rvc_manager_api.routers.artifacts import _expire_upload
from rvc_manager_api.security import hash_password
from rvc_manager_api.services.artifact_cleanup import (
    ArtifactCleanupReconciler,
    reconcile_artifact_upload_cleanup,
)
from rvc_manager_api.services.artifacts import (
    ArtifactSpoolError,
    effective_artifact_upload_ttl_seconds,
    remove_spool_file,
    verify_object_to_spool,
)
from rvc_manager_api.services.storage_adoption import adopt_storage_sessions
from rvc_manager_api.storage import (
    UNBOUND_STORAGE_NAMESPACE_SHA256,
    InvalidObjectKey,
    LocalStorageAdapter,
    S3StorageAdapter,
    StorageError,
)
from rvc_orchestrator_contracts import utc_now

USER_PASSWORD = "artifact-owner-password-1234"


def _verification_spool_files(directory: Path) -> tuple[Path, ...]:
    return tuple(directory.glob("rvc-artifact-verify-*"))


def worker_capabilities() -> dict[str, object]:
    return {
        "engine_mode": "rvc_webui",
        "worker_version": "0.2.0",
        "rvc_commit_hash": "0123456789abcdef",
        "supported_rvc_versions": ["v2"],
        "supported_training_f0_methods": ["rmvpe"],
        "gpus": [
            {
                "index": 0,
                "name": "Artifact Test GPU",
                "total_vram_mb": 24 * 1024,
                "free_vram_mb": 20 * 1024,
            }
        ],
        "disk_free_bytes": 500_000_000_000,
        "rvc_assets_ready": True,
    }


async def seed_user(app: FastAPI, email: str) -> User:
    password_hash = await run_in_threadpool(hash_password, USER_PASSWORD)
    async with app.state.database.session_factory() as session:
        user = User(
            email=email,
            password_hash=password_hash,
            role="user",
            disabled=False,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def login(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": USER_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def register_worker(client: AsyncClient, name: str) -> str:
    response = await client.post(
        "/api/v1/workers/register",
        headers={"X-Worker-Bootstrap-Token": "test-bootstrap-token"},
        json={"name": name, "capabilities": worker_capabilities()},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["worker_token"])


async def create_claim(
    client: AsyncClient,
    owner_headers: dict[str, str],
    worker_token: str,
    *,
    suffix: str,
) -> tuple[str, dict[str, Any]]:
    dataset = await client.post(
        "/api/v1/datasets",
        headers=owner_headers,
        json={
            "name": f"artifact-dataset-{suffix}",
            "storage_uri": f"local:///dataset/{suffix}",
            "flat_storage_uri": f"local:///dataset/{suffix}/flat",
        },
    )
    assert dataset.status_code == 201, dataset.text
    experiment = await client.post(
        "/api/v1/experiments",
        headers=owner_headers,
        json={
            "name": f"artifact-experiment-{suffix}",
            "dataset_id": dataset.json()["id"],
        },
    )
    assert experiment.status_code == 201, experiment.text
    job = await client.post(
        "/api/v1/jobs",
        headers=owner_headers,
        json={
            "job_name": f"artifact-job-{suffix}",
            "experiment_id": experiment.json()["id"],
            "dataset_id": dataset.json()["id"],
            "model": {"version": "v2", "sample_rate": "40k"},
        },
    )
    assert job.status_code == 201, job.text
    claim = await client.post(
        "/api/v1/workers/jobs/claim",
        headers={"Authorization": f"Bearer {worker_token}"},
        json={"max_wait_seconds": 0},
    )
    assert claim.status_code == 200, claim.text
    return str(job.json()["id"]), claim.json()


def init_payload(claim: dict[str, Any], content: bytes) -> dict[str, Any]:
    return {
        "lease_id": claim["lease_id"],
        "attempt_id": claim["attempt_id"],
        "idempotency_key": "artifact-upload-idempotency-0001",
        "artifact_type": "final_small_model",
        "filename": "voice-model.pth",
        "content_type": "application/octet-stream",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "metadata": {"rvc_version": "v2"},
    }


async def test_artifact_namespace_mismatch_preserves_staging_and_blocks_download(
    app: FastAPI,
    client: AsyncClient,
    tmp_path: Path,
) -> None:
    owner = await seed_user(app, "artifact-namespace-owner@example.test")
    owner_headers = await login(client, owner.email)
    worker_token = await register_worker(client, "artifact-namespace-worker")
    worker_headers = {"Authorization": f"Bearer {worker_token}"}
    job_id, claim = await create_claim(
        client,
        owner_headers,
        worker_token,
        suffix="namespace",
    )
    content = b"namespace-bound-artifact" * 32
    payload = init_payload(claim, content)
    initialized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=worker_headers,
        json=payload,
    )
    assert initialized.status_code == 201, initialized.text
    target = initialized.json()
    uploaded = await client.put(
        target["upload_url"],
        headers=target["upload_headers"],
        content=content,
    )
    assert uploaded.status_code == 204, uploaded.text

    original_storage = cast(LocalStorageAdapter, app.state.storage)
    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        assert upload is not None
        staging_path = original_storage._path(upload.temporary_object_key)
        assert upload.storage_namespace_sha256 == original_storage.namespace_fingerprint
        assert staging_path.is_file()

    alternate_storage = LocalStorageAdapter(tmp_path / "alternate-artifact-objects")
    assert alternate_storage.backend == original_storage.backend
    assert alternate_storage.namespace_fingerprint != original_storage.namespace_fingerprint
    app.state.storage = alternate_storage
    try:
        replay = await client.post(
            f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
            headers=worker_headers,
            json=payload,
        )
        assert replay.status_code == 503
        overwritten = await client.put(
            target["upload_url"],
            headers=target["upload_headers"],
            content=content,
        )
        assert overwritten.status_code == 503
        blocked_finalize = await client.post(
            f"/api/v1/workers/jobs/{job_id}/artifact-uploads/"
            f"{target['upload_session_id']}/finalize",
            headers=worker_headers,
            json={"lease_id": claim["lease_id"], "attempt_id": claim["attempt_id"]},
        )
        assert blocked_finalize.status_code == 503
    finally:
        app.state.storage = original_storage

    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        assert upload is not None
        assert upload.status == "pending"
        assert upload.storage_namespace_sha256 == original_storage.namespace_fingerprint
    assert staging_path.is_file()

    finalized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/{target['upload_session_id']}/finalize",
        headers=worker_headers,
        json={"lease_id": claim["lease_id"], "attempt_id": claim["attempt_id"]},
    )
    assert finalized.status_code == 200, finalized.text
    artifact_id = finalized.json()["id"]

    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        assert upload is not None
        upload.storage_namespace_sha256 = UNBOUND_STORAGE_NAMESPACE_SHA256
        await session.commit()
    wrong_target = await adopt_storage_sessions(
        app.state.database,
        alternate_storage,
        kind="artifact",
        session_ids=(target["upload_session_id"],),
        dry_run=True,
    )
    assert wrong_target.rejected == 1
    assert wrong_target.items[0].code in {
        "artifact_storage_uri_mismatch",
        "object_not_found",
    }
    preview = await adopt_storage_sessions(
        app.state.database,
        original_storage,
        kind="artifact",
        session_ids=(target["upload_session_id"],),
        dry_run=True,
    )
    assert preview.verified == 1
    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        assert upload is not None
        assert upload.storage_namespace_sha256 == UNBOUND_STORAGE_NAMESPACE_SHA256
    applied = await adopt_storage_sessions(
        app.state.database,
        original_storage,
        kind="artifact",
        session_ids=(target["upload_session_id"],),
        dry_run=False,
    )
    assert applied.adopted == 1
    replayed = await adopt_storage_sessions(
        app.state.database,
        original_storage,
        kind="artifact",
        session_ids=(target["upload_session_id"],),
        dry_run=False,
    )
    assert replayed.rejected == 0
    assert replayed.verified == 1
    assert replayed.items[0].code == "already_bound"
    async with app.state.database.session_factory() as session:
        audit = await session.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.resource_id == target["upload_session_id"],
                AuditEvent.action == "storage_namespace.adopted",
            )
            .order_by(AuditEvent.occurred_at.desc())
        )
        assert audit is not None
        assert audit.details_json["target_storage_backend"] == "local"
        assert (
            audit.details_json["target_storage_namespace_sha256"]
            == original_storage.namespace_fingerprint
        )
    app.state.storage = alternate_storage
    try:
        blocked_download = await client.get(
            f"/api/v1/artifacts/{artifact_id}/download",
            headers=owner_headers,
        )
        assert blocked_download.status_code == 503
    finally:
        app.state.storage = original_storage


async def test_local_upload_finalize_idempotency_and_owner_download(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await seed_user(app, "artifact-owner@example.test")
    await seed_user(app, "artifact-other@example.test")
    owner_headers = await login(client, owner.email)
    other_headers = await login(client, "artifact-other@example.test")
    worker_token = await register_worker(client, "artifact-worker")
    job_id, claim = await create_claim(
        client,
        owner_headers,
        worker_token,
        suffix="owner",
    )
    worker_headers = {"Authorization": f"Bearer {worker_token}"}
    content = b"verified-rvc-small-model\x00" * 64
    payload = init_payload(claim, content)

    initialized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=worker_headers,
        json=payload,
    )
    assert initialized.status_code == 201, initialized.text
    target = initialized.json()
    assert target["status"] == "pending"
    assert target["method"] == "PUT"
    assert target["upload_url"].endswith(target["upload_session_id"])
    assert payload["filename"] not in target["upload_url"]
    assert initialized.headers["Cache-Control"] == "no-store"

    duplicate = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=worker_headers,
        json=payload,
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["upload_session_id"] == target["upload_session_id"]
    assert duplicate.json()["upload_headers"] == target["upload_headers"]

    conflicting = dict(payload)
    conflicting["filename"] = "different-model.pth"
    conflict = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=worker_headers,
        json=conflicting,
    )
    assert conflict.status_code == 409

    wrong_type_headers = dict(target["upload_headers"])
    wrong_type_headers["Content-Type"] = "application/json"
    wrong_type = await client.put(
        target["upload_url"],
        headers=wrong_type_headers,
        content=content,
    )
    assert wrong_type.status_code == 422
    uploaded = await client.put(
        target["upload_url"],
        headers=target["upload_headers"],
        content=content,
    )
    assert uploaded.status_code == 204, uploaded.text

    finalized = await client.post(
        (f"/api/v1/workers/jobs/{job_id}/artifact-uploads/{target['upload_session_id']}/finalize"),
        headers=worker_headers,
        json={"lease_id": claim["lease_id"], "attempt_id": claim["attempt_id"]},
    )
    assert finalized.status_code == 200, finalized.text
    artifact = finalized.json()
    assert artifact["sha256"] == payload["sha256"]
    assert artifact["filename"] == payload["filename"]
    assert "storage_uri" not in artifact
    assert artifact["metadata_json"]["manager_verification"]["bounded_stream"] is True

    finalized_again = await client.post(
        (f"/api/v1/workers/jobs/{job_id}/artifact-uploads/{target['upload_session_id']}/finalize"),
        headers=worker_headers,
        json={"lease_id": claim["lease_id"], "attempt_id": claim["attempt_id"]},
    )
    assert finalized_again.status_code == 200
    assert finalized_again.json()["id"] == artifact["id"]

    same_artifact = dict(payload)
    same_artifact["idempotency_key"] = "artifact-upload-idempotency-0002"
    deduplicated = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=worker_headers,
        json=same_artifact,
    )
    assert deduplicated.status_code == 201
    assert deduplicated.json()["upload_session_id"] == target["upload_session_id"]
    assert deduplicated.json()["artifact"]["id"] == artifact["id"]
    assert deduplicated.json()["upload_url"] is None

    owner_list = await client.get(
        f"/api/v1/jobs/{job_id}/artifacts",
        headers=owner_headers,
    )
    assert owner_list.status_code == 200
    assert owner_list.json()["total"] == 1
    assert owner_list.json()["items"][0]["id"] == artifact["id"]
    assert "storage_uri" not in owner_list.text
    assert (
        await client.get(
            f"/api/v1/jobs/{job_id}/artifacts",
            headers=other_headers,
        )
    ).status_code == 404
    assert (
        await client.get(
            f"/api/v1/jobs/{job_id}/artifacts",
            headers=admin_headers,
        )
    ).json()["total"] == 1

    async def same_origin_presign(*args: Any, **kwargs: Any) -> str:
        del args, kwargs
        return "http://test/object-store/presigned?signature=secret"

    monkeypatch.setattr(app.state.storage, "create_download_url", same_origin_presign)
    owner_download = await client.get(
        f"/api/v1/artifacts/{artifact['id']}/download",
        headers=owner_headers,
    )
    assert owner_download.status_code == 200
    assert not owner_download.history
    assert owner_download.content == content
    assert "voice-model.pth" in owner_download.headers["Content-Disposition"]
    assert owner_download.headers["X-Content-Type-Options"] == "nosniff"
    assert (
        await client.get(
            f"/api/v1/artifacts/{artifact['id']}/download",
            headers=other_headers,
        )
    ).status_code == 404
    admin_download = await client.get(
        f"/api/v1/artifacts/{artifact['id']}/download",
        headers=admin_headers,
    )
    assert admin_download.status_code == 200
    assert admin_download.content == content

    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        assert upload is not None
        assert upload.status == "completed"
        assert upload.filename not in upload.temporary_object_key
        assert upload.filename not in upload.canonical_object_key
        assert target["upload_headers"]["X-RVC-Upload-Token"] not in (
            upload.upload_token_hash or ""
        )
        assert not (app.state.storage.root / upload.temporary_object_key).exists()
        assert (app.state.storage.root / upload.canonical_object_key).is_file()
        stored_artifact = await session.get(Artifact, artifact["id"])
        assert stored_artifact is not None
        assert stored_artifact.storage_uri.startswith("local:///artifacts/verified/")
        events = list(
            (
                await session.scalars(
                    select(AuditEvent).where(AuditEvent.action == "artifact.download_requested")
                )
            ).all()
        )
        assert len(events) == 2
        persisted = " ".join(str(event.details_json) for event in events)
        assert "X-RVC-Upload-Token" not in persisted
        assert "upload_url" not in persisted


async def test_finalize_revalidates_config_after_canonical_publish_and_cleans_up(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_token = await register_worker(client, "artifact-config-fence-worker")
    job_id, claim = await create_claim(
        client,
        admin_headers,
        worker_token,
        suffix="config-fence",
    )
    auth = {"Authorization": f"Bearer {worker_token}"}
    content = b"artifact config fence" * 64
    payload = init_payload(claim, content)
    initialized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=payload,
    )
    assert initialized.status_code == 201, initialized.text
    target = initialized.json()
    assert (
        await client.put(
            target["upload_url"],
            headers=target["upload_headers"],
            content=content,
        )
    ).status_code == 204

    original_store = app.state.storage.store_verified_file

    async def store_then_tamper(*args: object, **kwargs: object) -> None:
        await original_store(*args, **kwargs)
        async with app.state.database.session_factory() as other_session:
            job = await other_session.get(Job, job_id)
            assert job is not None
            document = dict(job.config_json)
            artifacts = dict(document["artifacts"])
            artifacts["collect_logs"] = not artifacts["collect_logs"]
            document["artifacts"] = artifacts
            job.config_json = document
            await other_session.commit()

    monkeypatch.setattr(app.state.storage, "store_verified_file", store_then_tamper)
    finalized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/{target['upload_session_id']}/finalize",
        headers=auth,
        json={"lease_id": claim["lease_id"], "attempt_id": claim["attempt_id"]},
    )
    assert finalized.status_code == 409
    assert finalized.json()["detail"] == "job configuration integrity check failed"

    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        artifacts = list(
            (await session.scalars(select(Artifact).where(Artifact.job_id == job_id))).all()
        )
        assert upload is not None
        assert upload.status == "failed"
        assert upload.failure_code == "job_config_integrity_failed"
        assert upload.finalization_token is None
        assert artifacts == []
        canonical_path = app.state.storage.root / upload.canonical_object_key
        assert canonical_path.is_file()
        assert upload.canonical_cleanup_first_deleted_at is None
        assert upload.canonical_cleanup_completed_at is None

        upload.finalized_at = utc_now() - timedelta(
            seconds=app.state.settings.artifact_finalizing_stale_seconds + 1
        )
        await session.commit()

    first_cleanup = await reconcile_artifact_upload_cleanup(
        app.state.database,
        app.state.storage,
        app.state.settings,
        upload_ids=(target["upload_session_id"],),
    )
    assert first_cleanup.first_deletes == 1
    assert not canonical_path.exists()

    # A stale publisher that lost its DB token may finish after the first
    # delete. Recreate the exact key and prove the confirmation pass removes it.
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_bytes(content)
    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        assert upload is not None
        assert upload.canonical_cleanup_first_deleted_at is not None
        assert upload.canonical_cleanup_completed_at is None
        upload.canonical_cleanup_first_deleted_at = utc_now() - timedelta(
            seconds=app.state.settings.artifact_cleanup_confirmation_grace_seconds + 1
        )
        await session.commit()

    confirmed_cleanup = await reconcile_artifact_upload_cleanup(
        app.state.database,
        app.state.storage,
        app.state.settings,
        upload_ids=(target["upload_session_id"],),
    )
    assert confirmed_cleanup.confirmed_deletes == 1
    assert not canonical_path.exists()
    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        assert upload is not None
        assert upload.canonical_cleanup_completed_at is not None


async def test_stale_finalizer_never_deletes_replacement_tokens_canonical_object(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_token = await register_worker(client, "artifact-token-fence-worker")
    job_id, claim = await create_claim(
        client,
        admin_headers,
        worker_token,
        suffix="token-fence",
    )
    auth = {"Authorization": f"Bearer {worker_token}"}
    content = b"artifact token ownership fence" * 64
    initialized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=init_payload(claim, content),
    )
    assert initialized.status_code == 201, initialized.text
    target = initialized.json()
    upload_id = target["upload_session_id"]
    assert (
        await client.put(
            target["upload_url"],
            headers=target["upload_headers"],
            content=content,
        )
    ).status_code == 204

    replacement_token = str(uuid.uuid4())
    original_store = app.state.storage.store_verified_file

    async def publish_then_replace_owner(*args: object, **kwargs: object) -> None:
        await original_store(*args, **kwargs)
        async with app.state.database.session_factory() as other_session:
            upload = await other_session.get(ArtifactUploadSession, upload_id)
            assert upload is not None
            assert upload.status == "finalizing"
            upload.finalization_token = replacement_token
            upload.updated_at = utc_now()
            await other_session.commit()

    monkeypatch.setattr(
        app.state.storage,
        "store_verified_file",
        publish_then_replace_owner,
    )
    finalized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/{upload_id}/finalize",
        headers=auth,
        json={"lease_id": claim["lease_id"], "attempt_id": claim["attempt_id"]},
    )
    assert finalized.status_code == 409
    assert finalized.json()["detail"] == "upload finalization ownership changed"

    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, upload_id)
        assert upload is not None
        assert upload.status == "finalizing"
        assert upload.finalization_token == replacement_token
        assert (app.state.storage.root / upload.canonical_object_key).is_file()
        assert (app.state.storage.root / upload.temporary_object_key).is_file()


async def test_local_upload_writer_is_single_owner_and_sealed_before_finalize(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_token = await register_worker(client, "artifact-write-fence-worker")
    job_id, claim = await create_claim(
        client,
        admin_headers,
        worker_token,
        suffix="write-fence",
    )
    auth = {"Authorization": f"Bearer {worker_token}"}
    content = b"single owner upload body" * 64
    initialized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=init_payload(claim, content),
    )
    assert initialized.status_code == 201, initialized.text
    target = initialized.json()

    unsealed = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/{target['upload_session_id']}/finalize",
        headers=auth,
        json={"lease_id": claim["lease_id"], "attempt_id": claim["attempt_id"]},
    )
    assert unsealed.status_code == 409
    assert unsealed.json()["detail"] == "local upload is not sealed"

    write_started = asyncio.Event()
    release_write = asyncio.Event()
    original_write = app.state.storage.write_upload_stream

    async def delayed_write(*args: object, **kwargs: object) -> None:
        write_started.set()
        await release_write.wait()
        await original_write(*args, **kwargs)

    monkeypatch.setattr(app.state.storage, "write_upload_stream", delayed_write)
    first_write = asyncio.create_task(
        client.put(
            target["upload_url"],
            headers=target["upload_headers"],
            content=content,
        )
    )
    await asyncio.wait_for(write_started.wait(), timeout=2)

    concurrent_write = await client.put(
        target["upload_url"],
        headers=target["upload_headers"],
        content=content,
    )
    assert concurrent_write.status_code == 409
    assert concurrent_write.json()["detail"] == "upload session write is active"
    concurrent_finalize = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/{target['upload_session_id']}/finalize",
        headers=auth,
        json={"lease_id": claim["lease_id"], "attempt_id": claim["attempt_id"]},
    )
    assert concurrent_finalize.status_code == 409
    assert concurrent_finalize.json()["detail"] == "upload session write is active"

    release_write.set()
    assert (await first_write).status_code == 204
    replay_write = await client.put(
        target["upload_url"],
        headers=target["upload_headers"],
        content=content,
    )
    assert replay_write.status_code == 409
    assert replay_write.json()["detail"] == "upload session is already sealed"

    finalized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/{target['upload_session_id']}/finalize",
        headers=auth,
        json={"lease_id": claim["lease_id"], "attempt_id": claim["attempt_id"]},
    )
    assert finalized.status_code == 200, finalized.text
    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        assert upload is not None
        assert upload.status == "completed"
        assert upload.upload_write_token is None
        assert upload.upload_heartbeat_at is None
        assert upload.staging_cleanup_completed_at is not None


async def test_expiry_cas_never_overwrites_a_replacement_finalizer(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    worker_token = await register_worker(client, "artifact-expiry-fence-worker")
    job_id, claim = await create_claim(
        client,
        admin_headers,
        worker_token,
        suffix="expiry-fence",
    )
    content = b"expiry fence staging body"
    initialized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers={"Authorization": f"Bearer {worker_token}"},
        json=init_payload(claim, content),
    )
    target = initialized.json()
    assert (
        await client.put(
            target["upload_url"],
            headers=target["upload_headers"],
            content=content,
        )
    ).status_code == 204

    replacement_token = str(uuid.uuid4())
    async with app.state.database.session_factory() as expiry_session:
        stale_upload = await expiry_session.get(
            ArtifactUploadSession,
            target["upload_session_id"],
        )
        assert stale_upload is not None
        stale_upload.expires_at = utc_now() - timedelta(seconds=1)
        await expiry_session.commit()
        expiry_session.expunge(stale_upload)

        async with app.state.database.session_factory() as finalizer_session:
            replacement = await finalizer_session.get(
                ArtifactUploadSession,
                target["upload_session_id"],
            )
            assert replacement is not None
            replacement.status = "finalizing"
            replacement.finalization_token = replacement_token
            await finalizer_session.commit()

        expired = await _expire_upload(
            stale_upload,
            database=app.state.database,
            storage=app.state.storage,
            settings=app.state.settings,
            session=expiry_session,
        )
        assert expired is False

    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        assert upload is not None
        assert upload.status == "finalizing"
        assert upload.finalization_token == replacement_token
        assert (app.state.storage.root / upload.temporary_object_key).is_file()


def test_upload_ttl_scales_to_single_put_size_and_respects_operator_cap() -> None:
    settings = Settings(
        environment="test",
        jwt_secret="test-jwt-secret-with-at-least-thirty-two-characters",
        artifact_upload_ttl_seconds=3600,
    )

    assert effective_artifact_upload_ttl_seconds(1, settings) == 301
    assert effective_artifact_upload_ttl_seconds(5 * 1024**3, settings) == 2860

    capped = settings.model_copy(update={"artifact_upload_ttl_seconds": 1800})
    assert effective_artifact_upload_ttl_seconds(5 * 1024**3, capped) == 1800


async def test_spool_os_errors_are_typed_and_cleanup_is_never_raw(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        del args, kwargs
        raise OSError(errno.ENOSPC, "test spool is full")

    monkeypatch.setattr(
        "rvc_manager_api.services.artifacts.tempfile.mkstemp",
        fail_mkstemp,
    )
    with pytest.raises(ArtifactSpoolError) as raised:
        await verify_object_to_spool(
            app.state.storage,
            "artifacts/staging/attempt/session",
            expected_size=1,
            expected_sha256="a" * 64,
            settings=app.state.settings,
        )
    assert raised.value.failure_code == "verification_spool_full"

    path = tmp_path / "cleanup.bin"
    path.write_bytes(b"x")

    def fail_unlink(self: Path, missing_ok: bool = False) -> None:
        del self, missing_ok
        raise OSError(errno.EIO, "test cleanup failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "unlink", fail_unlink)
        with pytest.raises(ArtifactSpoolError) as cleanup:
            await remove_spool_file(path)
    assert cleanup.value.failure_code == "verification_spool_cleanup_failed"


async def test_spool_cancellation_closes_handle_and_removes_partial_file(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stream_waiting = asyncio.Event()
    keep_stream_open = asyncio.Event()

    async def stalled_stream(
        object_key: str,
        *,
        chunk_size: int,
        max_bytes: int,
    ) -> Any:
        del object_key, chunk_size, max_bytes
        yield b"partial"
        stream_waiting.set()
        await keep_stream_open.wait()

    monkeypatch.setattr(app.state.storage, "stream_object", stalled_stream)
    settings = app.state.settings.model_copy(update={"artifact_verification_spool_dir": tmp_path})
    verification = asyncio.create_task(
        verify_object_to_spool(
            app.state.storage,
            "artifacts/verified/cancellation-test",
            expected_size=64,
            expected_sha256="a" * 64,
            settings=settings,
        )
    )
    await asyncio.wait_for(stream_waiting.wait(), timeout=2)
    assert len(_verification_spool_files(tmp_path)) == 1

    verification.cancel()
    with pytest.raises(asyncio.CancelledError):
        await verification

    assert _verification_spool_files(tmp_path) == ()


async def test_spool_failure_recovers_finalizing_to_retryable_pending(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_token = await register_worker(client, "spool-recovery-worker")
    job_id, claim = await create_claim(
        client,
        admin_headers,
        worker_token,
        suffix="spool-recovery",
    )
    auth = {"Authorization": f"Bearer {worker_token}"}
    content = b"spool recovery artifact"
    payload = init_payload(claim, content)
    initialized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=payload,
    )
    target = initialized.json()
    assert (
        await client.put(
            target["upload_url"],
            headers=target["upload_headers"],
            content=content,
        )
    ).status_code == 204

    async def fail_spool(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise ArtifactSpoolError("verification_spool_full")

    monkeypatch.setattr(
        "rvc_manager_api.routers.artifacts.verify_object_to_spool",
        fail_spool,
    )
    finalized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/{target['upload_session_id']}/finalize",
        headers=auth,
        json={"lease_id": claim["lease_id"], "attempt_id": claim["attempt_id"]},
    )
    assert finalized.status_code == 503

    repeated = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=payload,
    )
    assert repeated.status_code == 201
    assert repeated.json()["upload_session_id"] == target["upload_session_id"]
    assert repeated.json()["status"] == "pending"
    assert repeated.json()["failure_code"] == "verification_spool_full"
    assert repeated.json()["retryable"] is True
    assert repeated.json()["retry_after_seconds"] == 5
    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        assert upload is not None
        assert upload.status == "pending"
        assert upload.failure_code == "verification_spool_full"


async def test_stale_finalizing_is_recovered_and_nonstale_response_has_retry_semantics(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    worker_token = await register_worker(client, "stale-finalizing-worker")
    job_id, claim = await create_claim(
        client,
        admin_headers,
        worker_token,
        suffix="stale-finalizing",
    )
    auth = {"Authorization": f"Bearer {worker_token}"}
    content = b"stale finalizing recovery"
    payload = init_payload(claim, content)
    initialized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=payload,
    )
    target = initialized.json()
    assert (
        await client.put(
            target["upload_url"],
            headers=target["upload_headers"],
            content=content,
        )
    ).status_code == 204
    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        assert upload is not None
        upload.status = "finalizing"
        upload.updated_at = utc_now()
        await session.commit()

    in_progress = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=payload,
    )
    assert in_progress.status_code == 201
    assert in_progress.json()["status"] == "finalizing"
    assert in_progress.json()["retryable"] is True
    assert in_progress.json()["retry_after_seconds"] == 5
    assert in_progress.json()["upload_url"] is None

    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        assert upload is not None
        upload.updated_at = utc_now() - timedelta(
            seconds=app.state.settings.artifact_finalizing_stale_seconds + 1
        )
        await session.commit()
    finalized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/{target['upload_session_id']}/finalize",
        headers=auth,
        json={"lease_id": claim["lease_id"], "attempt_id": claim["attempt_id"]},
    )
    assert finalized.status_code == 200, finalized.text
    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        assert upload is not None
        assert upload.status == "completed"
        assert upload.failure_code is None


async def test_expired_idempotent_upload_creates_a_new_generation(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    worker_token = await register_worker(client, "expired-upload-worker")
    job_id, claim = await create_claim(
        client,
        admin_headers,
        worker_token,
        suffix="expired-upload",
    )
    auth = {"Authorization": f"Bearer {worker_token}"}
    payload = init_payload(claim, b"retry-safe artifact")
    first_response = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=payload,
    )
    assert first_response.status_code == 201, first_response.text
    first = first_response.json()

    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, first["upload_session_id"])
        assert upload is not None
        upload.expires_at = utc_now() - timedelta(seconds=1)
        await session.commit()

    retried_response = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=payload,
    )
    assert retried_response.status_code == 201, retried_response.text
    retried = retried_response.json()
    assert retried["status"] == "pending"
    assert retried["upload_session_id"] != first["upload_session_id"]
    assert retried["upload_url"] != first["upload_url"]
    assert retried["upload_headers"] != first["upload_headers"]
    repeated_response = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=payload,
    )
    assert repeated_response.status_code == 201
    assert repeated_response.json()["upload_session_id"] == retried["upload_session_id"]

    async with app.state.database.session_factory() as session:
        uploads = list(
            (
                await session.scalars(
                    select(ArtifactUploadSession)
                    .where(ArtifactUploadSession.attempt_id == claim["attempt_id"])
                    .order_by(ArtifactUploadSession.generation)
                )
            ).all()
        )
        assert [(upload.generation, upload.status) for upload in uploads] == [
            (1, "expired"),
            (2, "pending"),
        ]
        assert uploads[0].idempotency_key == uploads[1].idempotency_key
        assert uploads[0].dedupe_key is None
        lease = await session.get(JobLease, claim["lease_id"])
        assert lease is not None
        assert uploads[1].expires_at > lease.expires_at


async def test_attempt_session_quota_counts_failed_sessions_until_cleanup_completes(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    app.state.settings.artifact_attempt_max_sessions = 1
    worker_token = await register_worker(client, "session-quota-worker")
    job_id, claim = await create_claim(
        client,
        admin_headers,
        worker_token,
        suffix="session-quota",
    )
    auth = {"Authorization": f"Bearer {worker_token}"}
    first_payload = init_payload(claim, b"first quota artifact")
    first = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=first_payload,
    )
    assert first.status_code == 201
    duplicate = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=first_payload,
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["upload_session_id"] == first.json()["upload_session_id"]

    second_payload = init_payload(claim, b"second quota artifact")
    second_payload.update(
        {
            "idempotency_key": "artifact-upload-session-quota-0002",
            "artifact_type": "final_index",
            "filename": "final.index",
        }
    )
    rejected = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=second_payload,
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "artifact session quota exceeded"

    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, first.json()["upload_session_id"])
        assert upload is not None
        upload.status = "failed"
        upload.failure_code = "test_terminal_failure"
        upload.dedupe_key = None
        await session.commit()
    cleanup_pending = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=second_payload,
    )
    assert cleanup_pending.status_code == 409
    assert cleanup_pending.json()["detail"] == "artifact session quota exceeded"
    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, first.json()["upload_session_id"])
        assert upload is not None
        cleaned_at = utc_now()
        upload.staging_cleanup_completed_at = cleaned_at
        upload.canonical_cleanup_completed_at = cleaned_at
        await session.commit()
    accepted = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=second_payload,
    )
    assert accepted.status_code == 201, accepted.text


async def test_attempt_byte_quota_counts_expired_sessions_until_cleanup_completes(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    first_content = b"123456"
    second_content = b"abcdef"
    app.state.settings.artifact_attempt_max_bytes = len(first_content) + len(second_content) - 1
    worker_token = await register_worker(client, "byte-quota-worker")
    job_id, claim = await create_claim(
        client,
        admin_headers,
        worker_token,
        suffix="byte-quota",
    )
    auth = {"Authorization": f"Bearer {worker_token}"}
    first_payload = init_payload(claim, first_content)
    first = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=first_payload,
    )
    assert first.status_code == 201

    second_payload = init_payload(claim, second_content)
    second_payload.update(
        {
            "idempotency_key": "artifact-upload-byte-quota-0002",
            "artifact_type": "final_index",
            "filename": "final.index",
        }
    )
    rejected = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=second_payload,
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "artifact byte quota exceeded"

    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, first.json()["upload_session_id"])
        assert upload is not None
        upload.status = "expired"
        upload.failure_code = "upload_expired"
        upload.dedupe_key = None
        await session.commit()
    cleanup_pending = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=second_payload,
    )
    assert cleanup_pending.status_code == 409
    assert cleanup_pending.json()["detail"] == "artifact byte quota exceeded"
    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, first.json()["upload_session_id"])
        assert upload is not None
        cleaned_at = utc_now()
        upload.staging_cleanup_completed_at = cleaned_at
        upload.canonical_cleanup_completed_at = cleaned_at
        await session.commit()
    accepted = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=second_payload,
    )
    assert accepted.status_code == 201, accepted.text


async def test_checksum_mismatch_cleans_temporary_object_and_never_creates_artifact(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    worker_token = await register_worker(client, "mismatch-worker")
    job_id, claim = await create_claim(
        client,
        admin_headers,
        worker_token,
        suffix="mismatch",
    )
    auth = {"Authorization": f"Bearer {worker_token}"}
    expected = b"expected artifact bytes"
    uploaded_bytes = b"tampered artifact bytes"
    assert len(expected) == len(uploaded_bytes)
    payload = init_payload(claim, expected)
    initialized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=payload,
    )
    target = initialized.json()
    assert (
        await client.put(
            target["upload_url"],
            headers=target["upload_headers"],
            content=uploaded_bytes,
        )
    ).status_code == 204
    finalized = await client.post(
        (f"/api/v1/workers/jobs/{job_id}/artifact-uploads/{target['upload_session_id']}/finalize"),
        headers=auth,
        json={"lease_id": claim["lease_id"], "attempt_id": claim["attempt_id"]},
    )
    assert finalized.status_code == 422
    assert "SHA-256" in finalized.json()["detail"]

    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        assert upload is not None
        assert upload.status == "failed"
        assert upload.failure_code == "sha256_mismatch"
        assert upload.dedupe_key is None
        assert not (app.state.storage.root / upload.temporary_object_key).exists()
        assert not (app.state.storage.root / upload.canonical_object_key).exists()
        count = await session.scalar(
            select(func.count()).select_from(Artifact).where(Artifact.job_id == job_id)
        )
        assert count == 0

    repeated = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=payload,
    )
    assert repeated.status_code == 201
    assert repeated.json()["status"] == "failed"
    assert repeated.json()["failure_code"] == "sha256_mismatch"
    assert repeated.json()["retryable"] is False
    assert repeated.json()["retry_after_seconds"] is None
    retry_payload = dict(payload)
    retry_payload["idempotency_key"] = "artifact-upload-idempotency-retry"
    retried = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=retry_payload,
    )
    assert retried.status_code == 201
    assert retried.json()["status"] == "pending"
    assert retried.json()["upload_session_id"] != target["upload_session_id"]


async def test_cleanup_reconciler_retries_local_failure_and_releases_quota(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.state.settings.artifact_attempt_max_sessions = 1
    worker_token = await register_worker(client, "artifact-cleanup-retry-worker")
    job_id, claim = await create_claim(
        client,
        admin_headers,
        worker_token,
        suffix="cleanup-retry",
    )
    auth = {"Authorization": f"Bearer {worker_token}"}
    content = b"cleanup retry staging object"
    initialized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=init_payload(claim, content),
    )
    assert initialized.status_code == 201, initialized.text
    target = initialized.json()
    assert (
        await client.put(
            target["upload_url"],
            headers=target["upload_headers"],
            content=content,
        )
    ).status_code == 204

    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        assert upload is not None
        upload.status = "failed"
        upload.failure_code = "test_terminal_failure"
        upload.dedupe_key = None
        upload.finalized_at = utc_now()
        upload.canonical_cleanup_completed_at = utc_now()
        await session.commit()

    second_payload = init_payload(claim, b"second cleanup artifact")
    second_payload.update(
        {
            "idempotency_key": "artifact-cleanup-retry-0002",
            "artifact_type": "final_index",
            "filename": "final.index",
        }
    )
    blocked = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=second_payload,
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "artifact session quota exceeded"

    original_delete = app.state.storage.delete_object

    async def fail_delete(_object_key: str) -> None:
        raise StorageError("injected cleanup outage")

    monkeypatch.setattr(app.state.storage, "delete_object", fail_delete)
    failed_cleanup = await reconcile_artifact_upload_cleanup(
        app.state.database,
        app.state.storage,
        app.state.settings,
        upload_ids=(target["upload_session_id"],),
    )
    assert failed_cleanup.failed == 1
    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        assert upload is not None
        assert upload.cleanup_token is None
        assert upload.staging_cleanup_completed_at is None
        assert upload.failure_code == "cleanup_failed"

    monkeypatch.setattr(app.state.storage, "delete_object", original_delete)
    retried_cleanup = await reconcile_artifact_upload_cleanup(
        app.state.database,
        app.state.storage,
        app.state.settings,
        upload_ids=(target["upload_session_id"],),
    )
    assert retried_cleanup.confirmed_deletes == 1
    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, target["upload_session_id"])
        assert upload is not None
        assert upload.staging_cleanup_first_deleted_at is not None
        assert upload.staging_cleanup_completed_at is not None

    accepted = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=second_payload,
    )
    assert accepted.status_code == 201, accepted.text


@pytest.mark.parametrize(
    ("tampered_field", "completed_field"),
    [
        ("temporary_object_key", "canonical_cleanup_completed_at"),
        ("canonical_object_key", "staging_cleanup_completed_at"),
    ],
)
async def test_cleanup_reconciler_rejects_reconstructed_object_key_mismatch(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    tampered_field: str,
    completed_field: str,
) -> None:
    worker_token = await register_worker(client, f"artifact-cleanup-key-{tampered_field}")
    job_id, claim = await create_claim(
        client,
        admin_headers,
        worker_token,
        suffix=f"cleanup-key-{tampered_field}",
    )
    auth = {"Authorization": f"Bearer {worker_token}"}
    content = b"cleanup key integrity object"
    initialized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers=auth,
        json=init_payload(claim, content),
    )
    assert initialized.status_code == 201, initialized.text
    target = initialized.json()
    assert (
        await client.put(
            target["upload_url"],
            headers=target["upload_headers"],
            content=content,
        )
    ).status_code == 204

    upload_id = target["upload_session_id"]
    foreign_key = f"artifacts/staging/{claim['attempt_id']}/foreign-{upload_id}"
    if tampered_field == "canonical_object_key":
        foreign_key = (
            f"artifacts/verified/{job_id}/{claim['attempt_id']}/"
            f"final_small_model/foreign-{upload_id}"
        )
    foreign_path = app.state.storage.root / foreign_key
    foreign_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    foreign_path.write_bytes(b"must not be deleted")

    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, upload_id)
        assert upload is not None
        upload.status = "failed"
        upload.failure_code = "test_terminal_failure"
        upload.dedupe_key = None
        upload.finalized_at = utc_now() - timedelta(
            seconds=app.state.settings.artifact_finalizing_stale_seconds + 1
        )
        setattr(upload, tampered_field, foreign_key)
        setattr(upload, completed_field, utc_now())
        await session.commit()

    result = await reconcile_artifact_upload_cleanup(
        app.state.database,
        app.state.storage,
        app.state.settings,
        upload_ids=(upload_id,),
    )
    assert result.failed == 1
    assert result.object_deletes == 0
    assert foreign_path.read_bytes() == b"must not be deleted"
    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, upload_id)
        assert upload is not None
        assert upload.failure_code == "cleanup_key_mismatch"
        assert upload.cleanup_token is None
        if tampered_field == "temporary_object_key":
            assert upload.staging_cleanup_completed_at is None
        else:
            assert upload.canonical_cleanup_completed_at is None


async def test_production_readiness_fails_when_artifact_cleanup_is_disabled(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    app.state.settings.environment = "production"
    app.state.settings.artifact_cleanup_reconcile_enabled = False
    response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["artifact_cleanup_reconciler"] == "disabled"


async def test_cleanup_reconciler_background_loop_reports_readiness(app: FastAPI) -> None:
    app.state.settings.artifact_cleanup_reconcile_interval_seconds = 0.01
    app.state.settings.artifact_cleanup_reconcile_stale_seconds = 1
    reconciler = ArtifactCleanupReconciler(
        app.state.database,
        app.state.storage,
        app.state.settings,
    )
    task = asyncio.create_task(reconciler.run())
    try:
        await asyncio.sleep(0.05)
        assert reconciler.last_completed_at is not None
        assert reconciler.readiness() == ("ok", True)
    finally:
        reconciler.stop()
        await asyncio.wait_for(task, timeout=1)
    assert reconciler.readiness() == ("stopped", False)


async def test_upload_rejects_foreign_and_expired_lease_and_unsafe_filename(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    owner_token = await register_worker(client, "lease-owner-worker")
    foreign_token = await register_worker(client, "foreign-worker")
    job_id, claim = await create_claim(
        client,
        admin_headers,
        owner_token,
        suffix="lease",
    )
    payload = init_payload(claim, b"lease-bound artifact")
    unsafe = dict(payload)
    unsafe["filename"] = "../escape.pth"
    assert (
        await client.post(
            f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
            headers={"Authorization": f"Bearer {owner_token}"},
            json=unsafe,
        )
    ).status_code == 422
    foreign = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers={"Authorization": f"Bearer {foreign_token}"},
        json=payload,
    )
    assert foreign.status_code == 409

    async with app.state.database.session_factory() as session:
        lease = await session.get(JobLease, claim["lease_id"])
        assert lease is not None
        lease.expires_at = utc_now()
        await session.commit()
    stale = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers={"Authorization": f"Bearer {owner_token}"},
        json=payload,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == "job lease expired"


class FakeStreamingBody:
    def __init__(self, content: bytes) -> None:
        self.value = io.BytesIO(content)
        self.closed = False

    def read(self, size: int) -> bytes:
        return self.value.read(size)

    def close(self) -> None:
        self.closed = True
        self.value.close()


class FakeS3Client:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.presigns: list[tuple[str, dict[str, Any]]] = []
        self.uploads: list[tuple[str, str, bytes, dict[str, Any]]] = []
        self.deletes: list[str] = []
        self.body = FakeStreamingBody(content)

    def generate_presigned_url(self, operation: str, **kwargs: Any) -> str:
        self.presigns.append((operation, kwargs))
        return f"https://objects.example.test/signed?operation={operation}&signature=secret"

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        del Bucket, Key
        self.body = FakeStreamingBody(self.content)
        return {"Body": self.body}

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: Any,
        **kwargs: Any,
    ) -> None:
        self.uploads.append((Bucket, Key, Body.read(), kwargs))

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        del Bucket
        self.deletes.append(Key)

    def close(self) -> None:
        return None


async def test_s3_staging_cleanup_requires_confirmed_second_delete(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    worker_token = await register_worker(client, "artifact-s3-cleanup-worker")
    job_id, claim = await create_claim(
        client,
        admin_headers,
        worker_token,
        suffix="s3-cleanup",
    )
    content = b"s3 cleanup fence"
    initialized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifact-uploads/init",
        headers={"Authorization": f"Bearer {worker_token}"},
        json=init_payload(claim, content),
    )
    assert initialized.status_code == 201, initialized.text
    upload_id = initialized.json()["upload_session_id"]

    s3_settings = Settings(
        environment="test",
        storage_backend="s3",
        s3_endpoint_url="https://minio.example.test",
        s3_access_key_id="access-key",
        s3_secret_access_key="secret-key",
        s3_bucket="artifact-cleanup-bucket",
        jwt_secret="test-jwt-secret-with-at-least-thirty-two-characters",
    )
    fake_client = FakeS3Client(content)
    adapter = S3StorageAdapter(s3_settings, client=fake_client)
    try:
        async with app.state.database.session_factory() as session:
            upload = await session.get(ArtifactUploadSession, upload_id)
            assert upload is not None
            temporary_object_key = upload.temporary_object_key
            upload.status = "completed"
            upload.storage_backend = adapter.backend
            upload.storage_namespace_sha256 = adapter.namespace_fingerprint
            upload.expires_at = utc_now()
            upload.finalized_at = utc_now()
            await session.commit()

        deferred = await reconcile_artifact_upload_cleanup(
            app.state.database,
            adapter,
            app.state.settings,
            upload_ids=(upload_id,),
        )
        assert deferred.examined == 0
        assert fake_client.deletes == []
        async with app.state.database.session_factory() as session:
            upload = await session.get(ArtifactUploadSession, upload_id)
            assert upload is not None
            upload.expires_at = utc_now() - timedelta(
                seconds=app.state.settings.artifact_staging_cleanup_grace_seconds + 1
            )
            await session.commit()

        first = await reconcile_artifact_upload_cleanup(
            app.state.database,
            adapter,
            app.state.settings,
            upload_ids=(upload_id,),
        )
        assert first.first_deletes == 1
        assert first.confirmed_deletes == 0
        assert fake_client.deletes == [temporary_object_key]
        async with app.state.database.session_factory() as session:
            upload = await session.get(ArtifactUploadSession, upload_id)
            assert upload is not None
            assert upload.staging_cleanup_first_deleted_at is not None
            assert upload.staging_cleanup_completed_at is None
            upload.staging_cleanup_first_deleted_at = utc_now() - timedelta(
                seconds=app.state.settings.artifact_cleanup_confirmation_grace_seconds + 1
            )
            await session.commit()

        # A PUT authorized before URL expiry may finish after the first delete.
        # The confirmation pass deletes that possible resurrection as well.
        confirmed = await reconcile_artifact_upload_cleanup(
            app.state.database,
            adapter,
            app.state.settings,
            upload_ids=(upload_id,),
        )
        assert confirmed.confirmed_deletes == 1
        assert fake_client.deletes == [temporary_object_key, temporary_object_key]
        async with app.state.database.session_factory() as session:
            upload = await session.get(ArtifactUploadSession, upload_id)
            assert upload is not None
            assert upload.staging_cleanup_completed_at is not None
            assert upload.cleanup_token is None
    finally:
        await adapter.close()


async def test_s3_namespace_ignores_credentials_but_tracks_object_namespace() -> None:
    base = {
        "environment": "test",
        "storage_backend": "s3",
        "s3_endpoint_url": "https://minio.example.test",
        "s3_bucket": "artifact-bucket",
        "s3_region": "ap-northeast-2",
        "s3_addressing_style": "path",
        "jwt_secret": "test-jwt-secret-with-at-least-thirty-two-characters",
    }
    first = S3StorageAdapter(
        Settings(
            **base,
            s3_access_key_id="first-access-key",
            s3_secret_access_key="first-secret-key",
        ),
        client=FakeS3Client(b""),
    )
    rotated = S3StorageAdapter(
        Settings(
            **base,
            s3_access_key_id="rotated-access-key",
            s3_secret_access_key="rotated-secret-key",
        ),
        client=FakeS3Client(b""),
    )
    other_bucket = S3StorageAdapter(
        Settings(
            **{**base, "s3_bucket": "other-bucket"},
            s3_access_key_id="first-access-key",
            s3_secret_access_key="first-secret-key",
        ),
        client=FakeS3Client(b""),
    )
    try:
        assert first.namespace_fingerprint == rotated.namespace_fingerprint
        assert first.namespace_fingerprint != other_bucket.namespace_fingerprint
    finally:
        await first.close()
        await rotated.close()
        await other_bucket.close()


async def test_s3_adapter_presigns_bound_headers_and_streams_without_external_s3(
    tmp_path: Path,
) -> None:
    content = b"bounded-s3-content"
    sha256 = hashlib.sha256(content).hexdigest()
    settings = Settings(
        environment="test",
        storage_backend="s3",
        s3_endpoint_url="https://minio.example.test",
        s3_access_key_id="access-key",
        s3_secret_access_key="secret-key",
        s3_bucket="artifact-bucket",
        jwt_secret="test-jwt-secret-with-at-least-thirty-two-characters",
        s3_presign_bind_checksum=True,
    )
    fake_client = FakeS3Client(content)
    adapter = S3StorageAdapter(settings, client=fake_client)
    target = await adapter.create_upload_target(
        session_id="00000000-0000-4000-8000-000000000001",
        object_key="artifacts/staging/attempt/session",
        public_api_base_url="https://manager.example.test",
        content_type="application/octet-stream",
        content_length=len(content),
        sha256=sha256,
        expires_at=utc_now().replace(microsecond=0),
        local_upload_token=None,
    )
    assert "signature=secret" in target.url
    assert target.headers["Content-Length"] == str(len(content))
    assert target.headers["If-None-Match"] == "*"
    assert target.headers["x-amz-meta-sha256"] == sha256
    assert "x-amz-checksum-sha256" in target.headers
    operation, arguments = fake_client.presigns[0]
    assert operation == "put_object"
    assert arguments["Params"]["Key"] == "artifacts/staging/attempt/session"
    assert arguments["Params"]["ContentLength"] == len(content)
    assert arguments["Params"]["IfNoneMatch"] == "*"
    assert arguments["Params"]["Metadata"] == {"sha256": sha256}

    streamed = b"".join(
        [
            chunk
            async for chunk in adapter.stream_object(
                "artifacts/staging/attempt/session",
                chunk_size=4,
                max_bytes=len(content),
            )
        ]
    )
    assert streamed == content
    assert fake_client.body.closed is True

    verified = tmp_path / "verified.bin"
    verified.write_bytes(content)
    await adapter.store_verified_file(
        "artifacts/verified/job/attempt/type/session",
        verified,
        content_type="application/octet-stream",
        sha256=sha256,
    )
    assert fake_client.uploads[0][2] == content
    assert fake_client.uploads[0][3]["Metadata"]["verified"] == "true"
    assert fake_client.uploads[0][3]["IfNoneMatch"] == "*"
    assert fake_client.uploads[0][3]["ContentLength"] == len(content)
    await adapter.delete_object("artifacts/staging/attempt/session")
    assert fake_client.deletes == ["artifacts/staging/attempt/session"]
    download_url = await adapter.create_download_url(
        "artifacts/verified/job/attempt/type/session",
        content_disposition='attachment; filename="model.pth"',
        expires_in_seconds=60,
    )
    assert download_url is not None and "signature=secret" in download_url


async def test_s3_presign_uses_worker_reachable_endpoint_without_network() -> None:
    settings = Settings(
        environment="test",
        storage_backend="s3",
        s3_endpoint_url="http://minio:9000",
        s3_presign_endpoint_url="https://objects.example.test",
        s3_access_key_id="access-key",
        s3_secret_access_key="secret-key",
        s3_bucket="artifact-bucket",
        jwt_secret="test-jwt-secret-with-at-least-thirty-two-characters",
    )
    adapter = S3StorageAdapter(settings)
    target = await adapter.create_upload_target(
        session_id="00000000-0000-4000-8000-000000000001",
        object_key="artifacts/staging/attempt/session",
        public_api_base_url="https://manager.example.test",
        content_type="application/octet-stream",
        content_length=3,
        sha256="a" * 64,
        expires_at=utc_now(),
        local_upload_token=None,
    )
    assert target.url.startswith("https://objects.example.test/artifact-bucket/artifacts/staging/")
    assert "minio:9000" not in target.url
    signed_headers = parse_qs(urlparse(target.url).query)["X-Amz-SignedHeaders"][0]
    assert {"content-length", "content-type", "x-amz-meta-sha256"}.issubset(
        set(signed_headers.split(";"))
    )
    await adapter.close()


def test_local_adapter_rejects_path_escape(tmp_path: Path) -> None:
    adapter = LocalStorageAdapter(tmp_path / "objects")
    with pytest.raises(InvalidObjectKey):
        adapter.storage_uri("../outside")


@pytest.mark.asyncio
async def test_local_verified_publish_is_write_once_and_read_only(tmp_path: Path) -> None:
    adapter = LocalStorageAdapter(tmp_path / "objects")
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"first-canonical-bytes")
    second.write_bytes(b"replacement-bytes")
    object_key = "artifacts/verified/job/attempt/sample/session"

    await adapter.store_verified_file(
        object_key,
        first,
        content_type="application/octet-stream",
        sha256=hashlib.sha256(first.read_bytes()).hexdigest(),
    )
    target = adapter._path(object_key)
    assert target.read_bytes() == first.read_bytes()
    assert target.stat().st_mode & 0o222 == 0

    with pytest.raises(StorageError):
        await adapter.store_verified_file(
            object_key,
            second,
            content_type="application/octet-stream",
            sha256=hashlib.sha256(second.read_bytes()).hexdigest(),
        )
    assert target.read_bytes() == first.read_bytes()
