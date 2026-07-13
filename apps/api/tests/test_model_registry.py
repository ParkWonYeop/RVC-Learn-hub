from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select

from rvc_manager_api.models import (
    Artifact,
    ArtifactUploadSession,
    AuditEvent,
    Experiment,
    ExperimentModelRegistry,
    Job,
    JobAttempt,
    JobLease,
    ModelRegistryOperation,
    new_id,
)
from rvc_manager_api.services import model_registry as model_registry_service
from rvc_manager_api.services.artifacts import canonical_object_key
from rvc_manager_api.services.workers import worker_can_run
from rvc_manager_api.storage import StorageError
from rvc_orchestrator_contracts import (
    RVC_REVIEWED_COMMIT,
    JobConfig,
    WorkerCapabilities,
    utc_now,
)

IMAGE_DIGEST = f"sha256:{'1' * 64}"
ASSET_SHA256 = "2" * 64


async def _register_real_worker(client: AsyncClient) -> str:
    worker_id, _ = await _register_real_worker_credentials(client)
    return worker_id


async def _register_real_worker_credentials(client: AsyncClient) -> tuple[str, str]:
    response = await client.post(
        "/api/v1/workers/register",
        headers={"X-Worker-Bootstrap-Token": "test-bootstrap-token"},
        json={
            "name": f"registry-worker-{new_id()}",
            "capabilities": {
                "engine_mode": "rvc_webui",
                "worker_version": "0.2.0",
                "rvc_commit_hash": RVC_REVIEWED_COMMIT,
                "supported_rvc_versions": ["v2"],
                "supported_training_f0_methods": ["rmvpe"],
                "gpus": [
                    {
                        "index": 0,
                        "uuid": f"GPU-{new_id()}",
                        "name": "Registry Test GPU",
                        "total_vram_mb": 24 * 1024,
                        "free_vram_mb": 20 * 1024,
                        "utilization_percent": 0,
                    }
                ],
                "disk_free_bytes": 500_000_000_000,
                "rvc_assets_ready": True,
                "runtime_image_digest": IMAGE_DIGEST,
                "runtime_asset_manifest_sha256": ASSET_SHA256,
            },
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["worker_id"]), str(response.json()["worker_token"])


async def _create_experiment_job(
    client: AsyncClient,
    headers: dict[str, str],
) -> tuple[str, str]:
    prefix = new_id()
    dataset = await client.post(
        "/api/v1/datasets",
        headers=headers,
        json={
            "name": f"registry-dataset-{prefix}",
            "storage_uri": f"local:///datasets/{prefix}.zip",
            "flat_storage_uri": f"local:///datasets/{prefix}-flat.zip",
        },
    )
    assert dataset.status_code == 201, dataset.text
    experiment = await client.post(
        "/api/v1/experiments",
        headers=headers,
        json={
            "name": f"registry-experiment-{prefix}",
            "dataset_id": dataset.json()["id"],
        },
    )
    assert experiment.status_code == 201, experiment.text
    job = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_name": f"registry-job-{prefix}",
            "experiment_id": experiment.json()["id"],
            "dataset_id": dataset.json()["id"],
            "model": {"version": "v2", "sample_rate": "40k"},
            "training": {"epochs": 10},
        },
    )
    assert job.status_code == 201, job.text
    return str(experiment.json()["id"]), str(job.json()["id"])


async def _create_additional_job(
    app: FastAPI,
    client: AsyncClient,
    headers: dict[str, str],
    experiment_id: str,
) -> str:
    async with app.state.database.session_factory() as session:
        experiment = await session.get(Experiment, experiment_id)
        assert experiment is not None
        dataset_id = experiment.dataset_id
    job = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_name": f"registry-job-{new_id()}",
            "experiment_id": experiment_id,
            "dataset_id": dataset_id,
            "model": {"version": "v2", "sample_rate": "40k"},
            "training": {"epochs": 10},
        },
    )
    assert job.status_code == 201, job.text
    return str(job.json()["id"])


async def _create_managed_user_headers(
    client: AsyncClient,
    admin_headers: dict[str, str],
    email: str,
) -> dict[str, str]:
    password = "registry-managed-password-12345!"
    created = await client.post(
        "/api/v1/admin/users",
        headers={
            **admin_headers,
            "Idempotency-Key": f"create-{hashlib.sha256(email.encode()).hexdigest()[:16]}",
        },
        json={
            "email": email,
            "password": password,
            "role": "user",
            "active": True,
        },
    )
    assert created.status_code == 201, created.text
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _store_bytes(app: FastAPI, tmp_path: Path, object_key: str, data: bytes) -> str:
    source = tmp_path / f"source-{new_id()}"
    source.write_bytes(data)
    sha256 = hashlib.sha256(data).hexdigest()
    await app.state.storage.store_verified_file(
        object_key,
        source,
        content_type="application/octet-stream",
        sha256=sha256,
    )
    return sha256


async def _seed_completed_attempt(
    app: FastAPI,
    client: AsyncClient,
    tmp_path: Path,
    job_id: str,
    *,
    include_index: bool = True,
) -> tuple[str, str, str | None]:
    worker_id = await _register_real_worker(client)
    now = utc_now().replace(microsecond=0)
    async with app.state.database.session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        attempt = JobAttempt(
            job_id=job.id,
            worker_id=worker_id,
            attempt_number=1,
            engine_mode="rvc_webui",
            rvc_commit_hash=RVC_REVIEWED_COMMIT,
            execution_provenance_version="worker-claim-v1",
            runtime_image_digest=IMAGE_DIGEST,
            runtime_asset_manifest_sha256=ASSET_SHA256,
            status="completed",
            started_at=now - timedelta(minutes=2),
            finished_at=now - timedelta(minutes=1),
        )


        session.add(attempt)
        await session.flush()
        lease = JobLease(
            job_id=job.id,
            attempt_id=attempt.id,
            worker_id=worker_id,
            expires_at=now,
            last_renewed_at=now - timedelta(minutes=1),
            released_at=now - timedelta(minutes=1),
            active=False,
        )
        session.add(lease)
        await session.flush()
        job.worker_id = worker_id
        job.current_attempt_id = attempt.id
        job.attempt_count = 1
        job.status = "completed"
        job.current_epoch = job.total_epoch
        job.started_at = attempt.started_at
        job.completed_at = attempt.finished_at

        artifact_ids: dict[str, str] = {}
        for artifact_type, filename, data in (
            ("final_small_model", "registry-model.pth", b"reviewed-model-bytes"),
            ("final_index", "final.index", b"reviewed-index-bytes"),
        ):
            if artifact_type == "final_index" and not include_index:
                continue
            upload_id = new_id()
            object_key = canonical_object_key(job.id, attempt.id, artifact_type, upload_id)
            sha256 = await _store_bytes(app, tmp_path, object_key, data)
            artifact = Artifact(
                job_id=job.id,
                attempt_id=attempt.id,
                artifact_type=artifact_type,
                filename=filename,
                storage_uri=app.state.storage.storage_uri(object_key),
                size_bytes=len(data),
                sha256=sha256,
                mime_type="application/octet-stream",
                metadata_json={
                    "manager_verification": {
                        "algorithm": "sha256",
                        "bounded_stream": True,
                        "upload_session_id": upload_id,
                        "storage_backend": "local",
                    }
                },
            )
            session.add(artifact)
            await session.flush()
            session.add(
                ArtifactUploadSession(
                    id=upload_id,
                    job_id=job.id,
                    attempt_id=attempt.id,
                    lease_id=lease.id,
                    worker_id=worker_id,
                    artifact_id=artifact.id,
                    artifact_type=artifact_type,
                    filename=filename,
                    content_type="application/octet-stream",
                    expected_size_bytes=len(data),
                    expected_sha256=sha256,
                    metadata_json={},
                    idempotency_key=f"registry-{upload_id}",
                    generation=1,
                    request_fingerprint="f" * 64,
                    temporary_object_key=f"artifacts/staging/{upload_id}",
                    canonical_object_key=object_key,
                    storage_backend="local",
                    storage_namespace_sha256=app.state.storage.namespace_fingerprint,
                    status="completed",
                    expires_at=now + timedelta(hours=1),
                    uploaded_at=now,
                    finalized_at=now,
                )
            )
            artifact_ids[artifact_type] = artifact.id
        await session.commit()
        return (
            attempt.id,
            artifact_ids["final_small_model"],
            artifact_ids.get("final_index"),
        )


async def _create_candidate(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    experiment_id: str,
    registry_version: int,
    job_id: str,
    attempt_id: str,
    model_id: str,
    key: str,
) -> dict[str, object]:
    response = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/candidates",
        headers={**headers, "Idempotency-Key": key},
        json={
            "expected_registry_row_version": registry_version,
            "source_job_id": job_id,
            "source_attempt_id": attempt_id,
            "model_artifact_id": model_id,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_model_registry_candidate_promote_replay_and_revoke(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    app.state.settings.sample_approved_runtime_bundles = (
        f"{IMAGE_DIGEST}@{ASSET_SHA256}"
    )
    experiment_id, job_id = await _create_experiment_job(client, admin_headers)
    attempt_id, model_id, index_id = await _seed_completed_attempt(
        app,
        client,
        tmp_path,
        job_id,
    )

    initial = await client.get(
        f"/api/v1/experiments/{experiment_id}/model-registry",
        headers=admin_headers,
    )
    assert initial.status_code == 200, initial.text
    assert initial.json() == {
        "experiment_id": experiment_id,
        "registry_row_version": 0,
        "active_entry_id": None,
        "can_manage": True,
        "items": [],
        "total": 0,
        "offset": 0,
        "limit": 50,
    }
    assert initial.headers["cache-control"] == "private, no-store"

    candidate_body = {
        "expected_registry_row_version": 0,
        "source_job_id": job_id,
        "source_attempt_id": attempt_id,
        "model_artifact_id": model_id,
    }
    identity = await client.get("/api/v1/auth/me", headers=admin_headers)
    assert identity.status_code == 200, identity.text
    actor_id = str(identity.json()["id"])
    changed_actor = new_id()
    assert changed_actor != actor_id
    actor_mismatch = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/candidates",
        headers={
            **admin_headers,
            "Idempotency-Key": "registry-actor-mismatch",
            "X-RVC-Expected-Actor-ID": changed_actor,
        },
        json=candidate_body,
    )
    assert actor_mismatch.status_code == 409, actor_mismatch.text
    unchanged = await client.get(
        f"/api/v1/experiments/{experiment_id}/model-registry",
        headers=admin_headers,
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["registry_row_version"] == 0
    assert unchanged.json()["items"] == []
    async with app.state.database.session_factory() as session:
        operation_count = await session.scalar(
            select(func.count()).select_from(ModelRegistryOperation)
        )
        registry_audit_count = await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action.like("model_registry.%"))
        )
        assert operation_count == 0
        assert registry_audit_count == 0

    candidate_headers = {
        **admin_headers,
        "Idempotency-Key": "registry-candidate-1",
        "X-RVC-Expected-Actor-ID": actor_id,
    }
    candidate = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/candidates",
        headers=candidate_headers,
        json=candidate_body,
    )
    assert candidate.status_code == 201, candidate.text
    candidate_json = candidate.json()
    assert candidate_json["registry_row_version"] == 1
    assert candidate_json["active_entry_id"] is None
    assert candidate_json["entry"]["status"] == "candidate"
    assert candidate_json["entry"]["row_version"] == 1
    assert candidate_json["entry"]["model"]["id"] == model_id
    assert candidate_json["entry"]["index"]["id"] == index_id
    assert "storage_uri" not in str(candidate_json)
    assert "execution_provenance_version" not in str(candidate_json)

    replay = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/candidates",
        headers=candidate_headers,
        json=candidate_body,
    )
    assert replay.status_code == 201, replay.text
    assert replay.json() == candidate_json
    assert replay.headers["idempotency-replayed"] == "true"

    conflict_body = {**candidate_body, "expected_registry_row_version": 1}
    conflict = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/candidates",
        headers=candidate_headers,
        json=conflict_body,
    )
    assert conflict.status_code == 409

    entry_id = candidate_json["entry"]["id"]
    cross_operation_key = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/entries/{entry_id}/promote",
        headers=candidate_headers,
        json={
            "expected_registry_row_version": 1,
            "expected_entry_row_version": 1,
        },
    )
    assert cross_operation_key.status_code == 409
    stale_registry = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/entries/{entry_id}/promote",
        headers={**admin_headers, "Idempotency-Key": "registry-stale-promote"},
        json={
            "expected_registry_row_version": 2,
            "expected_entry_row_version": 1,
        },
    )
    assert stale_registry.status_code == 409
    stale_entry = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/entries/{entry_id}/promote",
        headers={**admin_headers, "Idempotency-Key": "registry-stale-entry"},
        json={
            "expected_registry_row_version": 1,
            "expected_entry_row_version": 2,
        },
    )
    assert stale_entry.status_code == 409

    # Promotion is bound to the frozen completed Attempt, not to a later Job
    # current-attempt pointer. This keeps reviewed candidates promotable after
    # an operator-authorized retry changes the Job's live assignment state.
    async with app.state.database.session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.status = "queued"
        job.current_attempt_id = None
        job.worker_id = None
        job.attempt_count = 2
        await session.commit()

    promoted = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/entries/{entry_id}/promote",
        headers={**admin_headers, "Idempotency-Key": "registry-promote-1"},
        json={
            "expected_registry_row_version": 1,
            "expected_entry_row_version": 1,
        },
    )
    assert promoted.status_code == 200, promoted.text
    promoted_json = promoted.json()
    assert promoted_json["registry_row_version"] == 2
    assert promoted_json["active_entry_id"] == entry_id
    assert promoted_json["entry"]["status"] == "approved"
    assert promoted_json["entry"]["row_version"] == 2

    revoked = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/entries/{entry_id}/revoke",
        headers={**admin_headers, "Idempotency-Key": "registry-revoke-1"},
        json={
            "expected_registry_row_version": 2,
            "expected_entry_row_version": 2,
            "reason_code": "operator_request",
        },
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["registry_row_version"] == 3
    assert revoked.json()["active_entry_id"] is None
    assert revoked.json()["entry"]["status"] == "revoked"
    assert revoked.json()["entry"]["revoke_reason"] == "operator_request"

    async with app.state.database.session_factory() as session:
        operation = await session.scalar(select(ModelRegistryOperation))
        assert operation is not None
        assert operation.idempotency_key_hash != "registry-candidate-1"
        assert "registry-candidate-1" not in str(operation.response_json)


async def test_registry_rejects_legacy_provenance_and_oversized_body(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    app.state.settings.sample_approved_runtime_bundles = (
        f"{IMAGE_DIGEST}@{ASSET_SHA256}"
    )
    experiment_id, job_id = await _create_experiment_job(client, admin_headers)
    attempt_id, model_id, _ = await _seed_completed_attempt(
        app,
        client,
        tmp_path,
        job_id,
        include_index=False,
    )
    async with app.state.database.session_factory() as session:
        attempt = await session.get(JobAttempt, attempt_id)
        assert attempt is not None
        attempt.execution_provenance_version = None
        await session.commit()

    rejected = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/candidates",
        headers={**admin_headers, "Idempotency-Key": "registry-legacy-1"},
        json={
            "expected_registry_row_version": 0,
            "source_job_id": job_id,
            "source_attempt_id": attempt_id,
            "model_artifact_id": model_id,
        },
    )
    assert rejected.status_code == 409, rejected.text

    oversized = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/candidates",
        headers={**admin_headers, "Idempotency-Key": "registry-oversized-1"},
        content=b'{"padding":"' + b"x" * 20_000 + b'"}',
    )
    assert oversized.status_code == 413
    assert oversized.headers["cache-control"] == "private, no-store"
    assert oversized.headers["vary"] == "Authorization"


async def test_registry_owner_admin_visibility_conceals_other_users(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    owner_headers = await _create_managed_user_headers(
        client, admin_headers, "registry-owner@example.test"
    )
    other_headers = await _create_managed_user_headers(
        client, admin_headers, "registry-other@example.test"
    )
    experiment_id, _ = await _create_experiment_job(client, owner_headers)
    owner = await client.get(
        f"/api/v1/experiments/{experiment_id}/model-registry",
        headers=owner_headers,
    )
    admin = await client.get(
        f"/api/v1/experiments/{experiment_id}/model-registry",
        headers=admin_headers,
    )
    hidden = await client.get(
        f"/api/v1/experiments/{experiment_id}/model-registry",
        headers=other_headers,
    )
    hidden_mutation = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/candidates",
        headers={**other_headers, "Idempotency-Key": "hidden-registry-candidate"},
        json={
            "expected_registry_row_version": 0,
            "source_job_id": new_id(),
            "source_attempt_id": new_id(),
            "model_artifact_id": new_id(),
        },
    )
    assert owner.status_code == 200
    assert admin.status_code == 200
    assert hidden.status_code == 404
    assert hidden_mutation.status_code == 404
    unauthenticated = await client.get(
        f"/api/v1/experiments/{experiment_id}/model-registry"
    )
    invalid = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/candidates",
        headers={**owner_headers, "Idempotency-Key": "invalid-registry-body"},
        json={
            "expected_registry_row_version": 0,
            "source_job_id": new_id(),
            "source_attempt_id": new_id(),
            "model_artifact_id": new_id(),
            "unexpected": True,
        },
    )
    assert unauthenticated.status_code == 401
    assert invalid.status_code == 422
    for response in (hidden, hidden_mutation, unauthenticated, invalid):
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["vary"] == "Authorization"


@pytest.mark.parametrize(
    "variant",
    ["fake", "running", "stale_current", "unapproved_runtime", "duplicate_model", "wrong_commit"],
)
async def test_registry_rejects_ineligible_candidate_ledgers(
    variant: str,
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    app.state.settings.sample_approved_runtime_bundles = (
        f"{IMAGE_DIGEST}@{ASSET_SHA256}"
    )
    experiment_id, job_id = await _create_experiment_job(client, admin_headers)
    attempt_id, model_id, _ = await _seed_completed_attempt(
        app, client, tmp_path, job_id, include_index=False
    )
    async with app.state.database.session_factory() as session:
        job = await session.get(Job, job_id)
        attempt = await session.get(JobAttempt, attempt_id)
        assert job is not None and attempt is not None
        if variant == "fake":
            attempt.engine_mode = "fake"
        elif variant == "running":
            attempt.status = "training"
            attempt.finished_at = None
        elif variant == "stale_current":
            job.current_attempt_id = None
        elif variant == "unapproved_runtime":
            app.state.settings.sample_approved_runtime_bundles = ""
        elif variant == "duplicate_model":
            session.add(
                Artifact(
                    job_id=job.id,
                    attempt_id=attempt.id,
                    artifact_type="final_small_model",
                    filename="duplicate.pth",
                    storage_uri="local:///duplicate-model",
                    size_bytes=1,
                    sha256="e" * 64,
                    mime_type="application/octet-stream",
                    metadata_json={},
                )
            )
        elif variant == "wrong_commit":
            document = dict(job.config_json)
            backend = dict(document["rvc_backend"])
            backend["rvc_commit_hash"] = "0" * 40
            document["rvc_backend"] = backend
            job.config_json = document
        await session.commit()
    response = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/candidates",
        headers={**admin_headers, "Idempotency-Key": f"registry-ineligible-{variant}"},
        json={
            "expected_registry_row_version": 0,
            "source_job_id": job_id,
            "source_attempt_id": attempt_id,
            "model_artifact_id": model_id,
        },
    )
    assert response.status_code == 409, response.text


async def test_registry_active_replacement_rollback_and_candidate_revoke(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    app.state.settings.sample_approved_runtime_bundles = (
        f"{IMAGE_DIGEST}@{ASSET_SHA256}"
    )
    experiment_id, first_job_id = await _create_experiment_job(client, admin_headers)
    second_job_id = await _create_additional_job(
        app,
        client,
        admin_headers,
        experiment_id,
    )
    first_attempt, first_model, _ = await _seed_completed_attempt(
        app, client, tmp_path, first_job_id, include_index=False
    )
    second_attempt, second_model, _ = await _seed_completed_attempt(
        app, client, tmp_path, second_job_id, include_index=False
    )
    first = await _create_candidate(
        client,
        admin_headers,
        experiment_id=experiment_id,
        registry_version=0,
        job_id=first_job_id,
        attempt_id=first_attempt,
        model_id=first_model,
        key="registry-first-candidate",
    )
    second = await _create_candidate(
        client,
        admin_headers,
        experiment_id=experiment_id,
        registry_version=1,
        job_id=second_job_id,
        attempt_id=second_attempt,
        model_id=second_model,
        key="registry-second-candidate",
    )
    first_entry = first["entry"]
    second_entry = second["entry"]
    assert isinstance(first_entry, dict) and isinstance(second_entry, dict)

    promote_first = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/entries/"
        f"{first_entry['id']}/promote",
        headers={**admin_headers, "Idempotency-Key": "registry-promote-first"},
        json={
            "expected_registry_row_version": 2,
            "expected_entry_row_version": 1,
        },
    )
    assert promote_first.status_code == 200, promote_first.text
    promote_second = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/entries/"
        f"{second_entry['id']}/promote",
        headers={**admin_headers, "Idempotency-Key": "registry-promote-second"},
        json={
            "expected_registry_row_version": 3,
            "expected_entry_row_version": 1,
        },
    )
    assert promote_second.status_code == 200, promote_second.text
    assert promote_second.json()["active_entry_id"] == second_entry["id"]

    history = await client.get(
        f"/api/v1/experiments/{experiment_id}/model-registry",
        headers=admin_headers,
    )
    by_id = {item["id"]: item for item in history.json()["items"]}
    assert by_id[first_entry["id"]]["status"] == "approved"
    assert by_id[first_entry["id"]]["is_active"] is False
    assert by_id[first_entry["id"]]["row_version"] == 3

    rollback = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/entries/"
        f"{first_entry['id']}/promote",
        headers={**admin_headers, "Idempotency-Key": "registry-rollback-first"},
        json={
            "expected_registry_row_version": 4,
            "expected_entry_row_version": 3,
        },
    )
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()["active_entry_id"] == first_entry["id"]
    assert rollback.json()["entry"]["approved_at"].removesuffix("Z") == (
        promote_first.json()["entry"]["approved_at"].removesuffix("Z")
    )

    revoke_active = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/entries/"
        f"{first_entry['id']}/revoke",
        headers={**admin_headers, "Idempotency-Key": "registry-revoke-active"},
        json={
            "expected_registry_row_version": 5,
            "expected_entry_row_version": 4,
            "reason_code": "quality_rejected",
        },
    )
    assert revoke_active.status_code == 200, revoke_active.text
    assert revoke_active.json()["active_entry_id"] is None

    third_job_id = await _create_additional_job(
        app,
        client,
        admin_headers,
        experiment_id,
    )
    third_attempt, third_model, _ = await _seed_completed_attempt(
        app, client, tmp_path, third_job_id, include_index=False
    )
    third = await _create_candidate(
        client,
        admin_headers,
        experiment_id=experiment_id,
        registry_version=6,
        job_id=third_job_id,
        attempt_id=third_attempt,
        model_id=third_model,
        key="registry-third-candidate",
    )
    third_entry = third["entry"]
    assert isinstance(third_entry, dict)
    revoke_candidate = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/entries/"
        f"{third_entry['id']}/revoke",
        headers={**admin_headers, "Idempotency-Key": "registry-revoke-candidate"},
        json={
            "expected_registry_row_version": 7,
            "expected_entry_row_version": 1,
            "reason_code": "operator_request",
        },
    )
    assert revoke_candidate.status_code == 200, revoke_candidate.text
    assert revoke_candidate.json()["entry"]["approved_at"] is None
    assert revoke_candidate.json()["entry"]["status"] == "revoked"


def test_real_worker_commit_must_match_requested_job_commit() -> None:
    config = JobConfig.model_validate(
        {
            "job_name": "commit-match-job",
            "experiment_id": "commit-match-experiment",
            "dataset_id": "commit-match-dataset",
            "rvc_backend": {"rvc_commit_hash": RVC_REVIEWED_COMMIT},
            "model": {"version": "v2", "sample_rate": "40k"},
        }
    )
    capabilities = WorkerCapabilities.model_validate(
        {
            "engine_mode": "rvc_webui",
            "worker_version": "0.2.0",
            "rvc_commit_hash": "0" * 40,
            "supported_rvc_versions": ["v2"],
            "supported_training_f0_methods": ["rmvpe"],
            "gpus": [],
            "disk_free_bytes": 1_000_000_000,
            "rvc_assets_ready": True,
        }
    )
    assert worker_can_run(config, capabilities) is False


async def test_concurrent_promotion_has_one_winner_and_claim_snapshots_provenance(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    app.state.settings.sample_approved_runtime_bundles = (
        f"{IMAGE_DIGEST}@{ASSET_SHA256}"
    )
    experiment_id, job_id = await _create_experiment_job(client, admin_headers)
    attempt_id, model_id, _ = await _seed_completed_attempt(
        app, client, tmp_path, job_id, include_index=False
    )
    candidate = await _create_candidate(
        client,
        admin_headers,
        experiment_id=experiment_id,
        registry_version=0,
        job_id=job_id,
        attempt_id=attempt_id,
        model_id=model_id,
        key="registry-concurrent-candidate",
    )
    entry = candidate["entry"]
    assert isinstance(entry, dict)
    url = (
        f"/api/v1/experiments/{experiment_id}/model-registry/entries/"
        f"{entry['id']}/promote"
    )
    first, second = await asyncio.gather(
        client.post(
            url,
            headers={**admin_headers, "Idempotency-Key": "registry-concurrent-a"},
            json={
                "expected_registry_row_version": 1,
                "expected_entry_row_version": 1,
            },
        ),
        client.post(
            url,
            headers={**admin_headers, "Idempotency-Key": "registry-concurrent-b"},
            json={
                "expected_registry_row_version": 1,
                "expected_entry_row_version": 1,
            },
        ),
    )
    assert sorted((first.status_code, second.status_code)) == [200, 409]

    claim_experiment, claim_job_id = await _create_experiment_job(client, admin_headers)
    assert claim_experiment
    _, token = await _register_real_worker_credentials(client)
    claim = await client.post(
        "/api/v1/workers/jobs/claim",
        headers={"Authorization": f"Bearer {token}"},
        json={"max_wait_seconds": 0},
    )
    assert claim.status_code == 200, claim.text
    assert claim.json()["job_id"] == claim_job_id
    async with app.state.database.session_factory() as session:
        attempt = await session.get(JobAttempt, claim.json()["attempt_id"])
        assert attempt is not None
        assert attempt.rvc_commit_hash == RVC_REVIEWED_COMMIT
        assert attempt.execution_provenance_version == "worker-claim-v1"
        assert attempt.runtime_image_digest == IMAGE_DIGEST
        assert attempt.runtime_asset_manifest_sha256 == ASSET_SHA256


async def test_registry_canonical_tamper_and_storage_outage_fail_closed(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.state.settings.sample_approved_runtime_bundles = (
        f"{IMAGE_DIGEST}@{ASSET_SHA256}"
    )
    experiment_id, job_id = await _create_experiment_job(client, admin_headers)
    attempt_id, model_id, _ = await _seed_completed_attempt(
        app, client, tmp_path, job_id, include_index=False
    )
    async with app.state.database.session_factory() as session:
        upload = await session.scalar(
            select(ArtifactUploadSession).where(
                ArtifactUploadSession.artifact_id == model_id
            )
        )
        assert upload is not None
        canonical_key = upload.canonical_object_key
    canonical_path = Path(app.state.storage.root) / canonical_key
    canonical_path.chmod(0o600)
    try:
        canonical_path.write_bytes(b"tampered-model-bytes")
    finally:
        canonical_path.chmod(0o444)
    tampered = await client.post(
        f"/api/v1/experiments/{experiment_id}/model-registry/candidates",
        headers={**admin_headers, "Idempotency-Key": "registry-tampered"},
        json={
            "expected_registry_row_version": 0,
            "source_job_id": job_id,
            "source_attempt_id": attempt_id,
            "model_artifact_id": model_id,
        },
    )
    assert tampered.status_code == 409, tampered.text

    slot_experiment, slot_job = await _create_experiment_job(client, admin_headers)
    slot_attempt, slot_model, _ = await _seed_completed_attempt(
        app, client, tmp_path, slot_job, include_index=False
    )
    original_semaphore = app.state.sample_verification_semaphore
    original_timeout = app.state.settings.sample_verification_timeout_seconds
    app.state.sample_verification_semaphore = asyncio.Semaphore(0)
    app.state.settings.sample_verification_timeout_seconds = 0.01
    try:
        slot_unavailable = await client.post(
            f"/api/v1/experiments/{slot_experiment}/model-registry/candidates",
            headers={**admin_headers, "Idempotency-Key": "registry-slot-unavailable"},
            json={
                "expected_registry_row_version": 0,
                "source_job_id": slot_job,
                "source_attempt_id": slot_attempt,
                "model_artifact_id": slot_model,
            },
        )
    finally:
        app.state.sample_verification_semaphore = original_semaphore
        app.state.settings.sample_verification_timeout_seconds = original_timeout
    assert slot_unavailable.status_code == 503, slot_unavailable.text
    assert slot_unavailable.headers["retry-after"] == "1"

    outage_experiment, outage_job = await _create_experiment_job(client, admin_headers)
    outage_attempt, outage_model, _ = await _seed_completed_attempt(
        app, client, tmp_path, outage_job, include_index=False
    )

    def unavailable_stream(*_args: object, **_kwargs: object) -> object:
        async def generate() -> AsyncIterator[bytes]:
            raise StorageError("injected registry storage outage")
            yield b""  # pragma: no cover

        return generate()

    monkeypatch.setattr(app.state.storage, "stream_object", unavailable_stream)
    unavailable = await client.post(
        f"/api/v1/experiments/{outage_experiment}/model-registry/candidates",
        headers={**admin_headers, "Idempotency-Key": "registry-outage"},
        json={
            "expected_registry_row_version": 0,
            "source_job_id": outage_job,
            "source_attempt_id": outage_attempt,
            "model_artifact_id": outage_model,
        },
    )
    assert unavailable.status_code == 503, unavailable.text
    assert unavailable.headers["retry-after"] == "1"


async def test_registry_get_rejects_mixed_version_projection(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.state.settings.sample_approved_runtime_bundles = (
        f"{IMAGE_DIGEST}@{ASSET_SHA256}"
    )
    experiment_id, job_id = await _create_experiment_job(client, admin_headers)
    attempt_id, model_id, _ = await _seed_completed_attempt(
        app, client, tmp_path, job_id, include_index=False
    )
    await _create_candidate(
        client,
        admin_headers,
        experiment_id=experiment_id,
        registry_version=0,
        job_id=job_id,
        attempt_id=attempt_id,
        model_id=model_id,
        key="registry-read-race-candidate",
    )
    original_active_entry = model_registry_service._active_entry

    async def racing_active_entry(
        session: object,
        requested_experiment_id: str,
        *,
        lock: bool = False,
    ) -> object:
        entry = await original_active_entry(  # type: ignore[arg-type]
            session,
            requested_experiment_id,
            lock=lock,
        )
        if not lock:
            registry = await session.get(  # type: ignore[attr-defined]
                ExperimentModelRegistry,
                requested_experiment_id,
            )
            assert registry is not None
            registry.updated_at = utc_now()
            await session.flush()  # type: ignore[attr-defined]
        return entry

    monkeypatch.setattr(model_registry_service, "_active_entry", racing_active_entry)
    response = await client.get(
        f"/api/v1/experiments/{experiment_id}/model-registry",
        headers=admin_headers,
    )
    assert response.status_code == 409, response.text
    assert response.headers["cache-control"] == "private, no-store"
