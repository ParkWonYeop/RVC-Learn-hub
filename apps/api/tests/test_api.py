from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import rvc_manager_api.routers.workers as worker_routes
from rvc_manager_api.config import Settings
from rvc_manager_api.models import (
    Artifact,
    IngestBatch,
    Job,
    JobAttempt,
    JobLease,
    JobLog,
    JobStatusEvent,
    Metric,
    Worker,
)
from rvc_manager_api.services.workers import worker_can_run
from rvc_orchestrator_contracts import JobConfig, WorkerCapabilities, utc_now


def capabilities(
    *,
    tags: list[str] | None = None,
    engine_mode: str = "rvc_webui",
) -> dict[str, object]:
    return {
        "engine_mode": engine_mode,
        "worker_version": "0.1.0",
        "rvc_commit_hash": "0123456789abcdef",
        "supported_rvc_versions": ["v1", "v2"],
        "supported_training_f0_methods": [
            "pm",
            "harvest",
            "dio",
            "rmvpe",
            "rmvpe_gpu",
        ],
        "gpus": [
            {
                "index": 0,
                "uuid": "GPU-test",
                "name": "Test GPU",
                "total_vram_mb": 24 * 1024,
                "free_vram_mb": 20 * 1024,
                "utilization_percent": 0,
            }
        ],
        "disk_free_bytes": 500_000_000_000,
        "tags": tags or ["nvidia", "24gb"],
        "rvc_assets_ready": True,
        "max_concurrent_jobs": 1,
    }


async def register_worker(
    client: AsyncClient,
    name: str,
    *,
    engine_mode: str = "rvc_webui",
) -> tuple[str, str]:
    response = await client.post(
        "/api/v1/workers/register",
        headers={"X-Worker-Bootstrap-Token": "test-bootstrap-token"},
        json={"name": name, "capabilities": capabilities(engine_mode=engine_mode)},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["worker_id"], body["worker_token"]


async def create_job(
    client: AsyncClient,
    user_headers: dict[str, str],
    *,
    job_overrides: dict[str, object] | None = None,
) -> tuple[str, str, str]:
    dataset_response = await client.post(
        "/api/v1/datasets",
        headers=user_headers,
        json={
            "name": "speaker-a",
            "storage_uri": "s3://datasets/raw/dataset.zip",
            "flat_storage_uri": "s3://datasets/flat/",
        },
    )
    assert dataset_response.status_code == 201, dataset_response.text
    dataset_id = dataset_response.json()["id"]
    experiment_response = await client.post(
        "/api/v1/experiments",
        headers=user_headers,
        json={"name": "speaker-a-comparison", "dataset_id": dataset_id},
    )
    assert experiment_response.status_code == 201, experiment_response.text
    experiment_id = experiment_response.json()["id"]
    job_payload: dict[str, object] = {
        "job_name": "speaker-a-v2-40k",
        "experiment_id": experiment_id,
        "dataset_id": dataset_id,
        "model": {"version": "v2", "sample_rate": "40k", "use_f0": True},
        "f0_extraction": {"training_f0_method": "rmvpe"},
        "resource": {
            "min_vram_gb": 12,
            "preferred_worker_tags": ["nvidia"],
            "priority": 7,
        },
    }
    if job_overrides:
        job_payload.update(job_overrides)
    job_response = await client.post(
        "/api/v1/jobs",
        headers=user_headers,
        json=job_payload,
    )
    assert job_response.status_code == 201, job_response.text
    assert job_response.json()["current_attempt_engine_mode"] is None
    return dataset_id, experiment_id, job_response.json()["id"]


async def claim(client: AsyncClient, token: str):
    return await client.post(
        "/api/v1/workers/jobs/claim",
        headers={"Authorization": f"Bearer {token}"},
        json={"max_wait_seconds": 0},
    )


async def test_health_and_readiness(client: AsyncClient) -> None:
    health = await client.get("/health")
    ready = await client.get("/ready")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert ready.status_code == 200
    assert ready.json()["checks"]["database"] == "ok"
    assert ready.headers["X-Request-ID"]


async def test_job_api_rejects_ambiguous_low_inference_resample_rate(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    dataset_id, experiment_id, _ = await create_job(client, admin_headers)

    response = await client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={
            "job_name": "speaker-a-invalid-resample",
            "experiment_id": experiment_id,
            "dataset_id": dataset_id,
            "model": {"version": "v2", "sample_rate": "40k", "use_f0": True},
            "f0_extraction": {"training_f0_method": "rmvpe"},
            "auto_inference_samples": {
                "enabled": False,
                "test_set_id": None,
                "resample_sr": 15_999,
            },
        },
    )

    assert response.status_code == 422


async def test_job_api_rejects_coerced_inference_resample_rate(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    dataset_id, experiment_id, _ = await create_job(client, admin_headers)

    for sequence, resample_sr in enumerate((False, "16000", 16_000.0), start=1):
        response = await client.post(
            "/api/v1/jobs",
            headers=admin_headers,
            json={
                "job_name": f"speaker-a-coerced-resample-{sequence}",
                "experiment_id": experiment_id,
                "dataset_id": dataset_id,
                "model": {"version": "v2", "sample_rate": "40k", "use_f0": True},
                "f0_extraction": {"training_f0_method": "rmvpe"},
                "auto_inference_samples": {
                    "enabled": False,
                    "test_set_id": None,
                    "resample_sr": resample_sr,
                },
            },
        )

        assert response.status_code == 422


def test_fake_workers_require_explicit_non_production_opt_in() -> None:
    config = JobConfig.model_validate(
        {
            "job_name": "fake-job",
            "experiment_id": "experiment-1",
            "dataset_id": "dataset-1",
            "model": {"version": "v2", "sample_rate": "40k"},
        }
    )
    fake = WorkerCapabilities.model_validate(
        {
            "engine_mode": "fake",
            "worker_version": "0.1.0",
            "rvc_commit_hash": "fake000",
            "supported_rvc_versions": ["v2"],
            "supported_training_f0_methods": ["rmvpe"],
            "gpus": [],
            "disk_free_bytes": 1_000_000,
            "rvc_assets_ready": False,
        }
    )
    assert worker_can_run(config, fake) is False
    assert worker_can_run(config, fake, allow_fake_workers=True) is True
    try:
        Settings(
            environment="production",
            database_url="postgresql+asyncpg://manager:test@db/manager",
            worker_bootstrap_token="bootstrap",
            worker_token_pepper="production-pepper",
            jwt_secret="production-jwt-secret-with-at-least-thirty-two-characters",
            allow_fake_workers=True,
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("production settings must reject fake workers")


async def test_worker_registration_hashes_token_and_heartbeat(
    app: FastAPI, client: AsyncClient
) -> None:
    worker_id, token = await register_worker(client, "gpu-01")
    async with app.state.database.session_factory() as session:
        worker = await session.get(Worker, worker_id)
        assert worker is not None
        assert worker.token_hash != token
        assert token not in worker.token_hash
        assert len(worker.token_hash) == 64

    session_response = await client.get(
        "/api/v1/workers/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert session_response.status_code == 200
    assert session_response.json()["worker_id"] == worker_id
    assert "worker_token" not in session_response.json()

    heartbeat = await client.post(
        "/api/v1/workers/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "idle", "capabilities": capabilities()},
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["cancel_job_ids"] == []

    denied = await client.post(
        "/api/v1/workers/heartbeat",
        headers={"Authorization": "Bearer invalid"},
        json={"status": "idle", "capabilities": capabilities()},
    )
    assert denied.status_code == 401


async def test_atomic_claim_creates_single_attempt_and_lease(
    app: FastAPI, client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    _, token_one = await register_worker(client, "gpu-01")
    _, token_two = await register_worker(client, "gpu-02")
    _, _, job_id = await create_job(client, admin_headers)

    first, second = await asyncio.gather(claim(client, token_one), claim(client, token_two))
    assert sorted([first.status_code, second.status_code]) == [200, 204]
    winner = first if first.status_code == 200 else second
    body = winner.json()
    assert body["job_id"] == job_id
    assert body["attempt_number"] == 1
    assert body["config"]["model"]["version"] == "v2"

    async with app.state.database.session_factory() as session:
        attempts = list((await session.scalars(select(JobAttempt))).all())
        leases = list((await session.scalars(select(JobLease))).all())
        assert len(attempts) == 1
        assert len(leases) == 1
        assert leases[0].active is True


async def test_job_read_uses_current_attempt_engine_mode_through_terminal_and_retry(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    _, token = await register_worker(client, "fake-engine-ledger", engine_mode="fake")
    _, experiment_id, job_id = await create_job(client, admin_headers)

    created = await client.get(f"/api/v1/jobs/{job_id}", headers=admin_headers)
    assert created.status_code == 200, created.text
    assert created.json()["current_attempt_id"] is None
    assert created.json()["current_attempt_engine_mode"] is None

    assignment_response = await claim(client, token)
    assert assignment_response.status_code == 200, assignment_response.text
    assignment = assignment_response.json()

    assigned = await client.get(f"/api/v1/jobs/{job_id}", headers=admin_headers)
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["current_attempt_id"] == assignment["attempt_id"]
    assert assigned.json()["current_attempt_engine_mode"] == "fake"
    assert assigned.json()["config"]["rvc_backend"]["backend_type"] == "rvc_webui"

    listed = await client.get(
        "/api/v1/jobs",
        headers=admin_headers,
        params={"experiment_id": experiment_id},
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["current_attempt_engine_mode"] == "fake"

    failed = await client.post(
        f"/api/v1/workers/jobs/{job_id}/status",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "lease_id": assignment["lease_id"],
            "status": "failed",
            "error_code": "RVC_PROCESS_EXITED",
            "error_message": "training process exited",
        },
    )
    assert failed.status_code == 200, failed.text

    terminal = await client.get(f"/api/v1/jobs/{job_id}", headers=admin_headers)
    assert terminal.status_code == 200, terminal.text
    assert terminal.json()["status"] == "failed"
    assert terminal.json()["current_attempt_id"] == assignment["attempt_id"]
    assert terminal.json()["current_attempt_engine_mode"] == "fake"

    terminal_list = await client.get(
        "/api/v1/jobs",
        headers=admin_headers,
        params={"experiment_id": experiment_id},
    )
    assert terminal_list.status_code == 200, terminal_list.text
    assert terminal_list.json()["items"][0]["current_attempt_engine_mode"] == "fake"

    retried = await client.post(f"/api/v1/jobs/{job_id}/retry", headers=admin_headers)
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "queued"
    assert retried.json()["current_attempt_id"] is None
    assert retried.json()["current_attempt_engine_mode"] is None

    retried_get = await client.get(f"/api/v1/jobs/{job_id}", headers=admin_headers)
    assert retried_get.status_code == 200, retried_get.text
    assert retried_get.json()["current_attempt_engine_mode"] is None

    retried_list = await client.get(
        "/api/v1/jobs",
        headers=admin_headers,
        params={"experiment_id": experiment_id},
    )
    assert retried_list.status_code == 200, retried_list.text
    assert retried_list.json()["items"][0]["current_attempt_engine_mode"] is None


async def test_lease_renewal_does_not_contend_with_versioned_worker_heartbeat(
    app: FastAPI, client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    worker_id, token = await register_worker(client, "gpu-renew", engine_mode="fake")
    _, _, job_id = await create_job(client, admin_headers)
    assignment = (await claim(client, token)).json()

    async with app.state.database.session_factory() as session:
        worker = await session.get(Worker, worker_id)
        assert worker is not None
        heartbeat_before = worker.last_heartbeat_at
        version_before = worker.row_version

    renew = await client.post(
        f"/api/v1/workers/jobs/{job_id}/lease/renew",
        headers={"Authorization": f"Bearer {token}"},
        json={"lease_id": assignment["lease_id"]},
    )
    assert renew.status_code == 200, renew.text

    async with app.state.database.session_factory() as session:
        worker = await session.get(Worker, worker_id)
        lease = await session.get(JobLease, assignment["lease_id"])
        assert worker is not None
        assert lease is not None
        assert worker.last_heartbeat_at == heartbeat_before
        assert worker.row_version == version_before
        assert lease.last_renewed_at is not None


async def test_terminal_status_reloads_after_worker_heartbeat_cas_conflict(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch,
) -> None:
    worker_id, token = await register_worker(
        client,
        "gpu-terminal-heartbeat-race",
        engine_mode="fake",
    )
    _, _, job_id = await create_job(client, admin_headers)
    assignment = (await claim(client, token)).json()
    original_boundary = worker_routes._lock_current_status_claim
    original_write_fence = worker_routes._acquire_status_write_fence
    original_commit = AsyncSession.commit
    boundary_calls = 0
    heartbeat_injected = False
    heartbeat_at = utc_now()

    async def mark_status_boundary(session, **kwargs):
        nonlocal boundary_calls
        boundary = await original_boundary(session, **kwargs)
        boundary_calls += 1
        return boundary

    async def fence_after_competing_heartbeat(session, **kwargs):
        nonlocal heartbeat_injected
        if not heartbeat_injected:
            async with app.state.database.session_factory() as heartbeat_session:
                competing_worker = await heartbeat_session.get(Worker, worker_id)
                assert competing_worker is not None
                assert competing_worker.current_job_id == job_id
                competing_worker.last_heartbeat_at = heartbeat_at
                await original_commit(heartbeat_session)
            heartbeat_injected = True
        return await original_write_fence(session, **kwargs)

    monkeypatch.setattr(
        worker_routes,
        "_lock_current_status_claim",
        mark_status_boundary,
    )
    monkeypatch.setattr(
        worker_routes,
        "_acquire_status_write_fence",
        fence_after_competing_heartbeat,
    )
    failed = await client.post(
        f"/api/v1/workers/jobs/{job_id}/status",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "lease_id": assignment["lease_id"],
            "status": "failed",
            "error_code": "RVC_PROCESS_EXITED",
            "error_message": "training process exited with code 1",
        },
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "failed"
    assert heartbeat_injected is True
    assert boundary_calls >= 2

    async with app.state.database.session_factory() as session:
        job = await session.get(Job, job_id)
        attempt = await session.get(JobAttempt, assignment["attempt_id"])
        lease = await session.get(JobLease, assignment["lease_id"])
        stored_worker = await session.get(Worker, worker_id)
        events = list(
            (
                await session.scalars(select(JobStatusEvent).where(JobStatusEvent.job_id == job_id))
            ).all()
        )
    assert job is not None and job.status == "failed"
    assert attempt is not None and attempt.status == "failed"
    assert attempt.finished_at is not None
    assert lease is not None and lease.active is False
    assert lease.released_at is not None
    assert stored_worker is not None and stored_worker.current_job_id is None
    assert stored_worker.status == "idle"
    assert stored_worker.last_heartbeat_at is not None
    assert [event.status for event in events].count("failed") == 1


async def test_lease_state_validation_and_idempotent_batches(
    app: FastAPI, client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    _, token = await register_worker(client, "gpu-01", engine_mode="fake")
    _, _, job_id = await create_job(client, admin_headers)
    claim_response = await claim(client, token)
    assert claim_response.status_code == 200
    assignment = claim_response.json()
    auth = {"Authorization": f"Bearer {token}"}

    invalid_transition = await client.post(
        f"/api/v1/workers/jobs/{job_id}/status",
        headers=auth,
        json={"lease_id": assignment["lease_id"], "status": "training"},
    )
    assert invalid_transition.status_code == 409

    valid_transition = await client.post(
        f"/api/v1/workers/jobs/{job_id}/status",
        headers=auth,
        json={"lease_id": assignment["lease_id"], "status": "downloading_dataset"},
    )
    assert valid_transition.status_code == 200, valid_transition.text

    renew = await client.post(
        f"/api/v1/workers/jobs/{job_id}/lease/renew",
        headers=auth,
        json={"lease_id": assignment["lease_id"]},
    )
    assert renew.status_code == 200, renew.text

    log_payload = {
        "lease_id": assignment["lease_id"],
        "attempt_id": assignment["attempt_id"],
        "idempotency_key": "logs-batch-0001",
        "entries": [
            {
                "sequence": 1,
                "level": "info",
                "message": (
                    "download https://objects.example.test/model?X-Amz-Signature=db-secret "
                    "Authorization: Bearer eyJdatabase.payload.signature password=db-password"
                ),
                "fields": {
                    "token": "db-worker-token",
                    "nested": {"api_key": "db-api-key", "safe": "preserved"},
                },
            }
        ],
    }
    logs = await client.post(f"/api/v1/workers/jobs/{job_id}/logs", headers=auth, json=log_payload)
    duplicate_logs = await client.post(
        f"/api/v1/workers/jobs/{job_id}/logs", headers=auth, json=log_payload
    )
    assert logs.json() == {"accepted": 1, "duplicate": False}
    assert duplicate_logs.json() == {"accepted": 0, "duplicate": True}
    sequence_conflict = await client.post(
        f"/api/v1/workers/jobs/{job_id}/logs",
        headers=auth,
        json={
            **log_payload,
            "idempotency_key": "logs-batch-0002",
            "entries": [
                {"sequence": 1, "message": "different logical event"},
                {"sequence": 2, "message": "must roll back with the conflict"},
            ],
        },
    )
    assert sequence_conflict.status_code == 409
    key_conflict = await client.post(
        f"/api/v1/workers/jobs/{job_id}/logs",
        headers=auth,
        json={
            **log_payload,
            "entries": [{"sequence": 1, "message": "reused key with different content"}],
        },
    )
    assert key_conflict.status_code == 409

    metrics = await client.post(
        f"/api/v1/workers/jobs/{job_id}/metrics",
        headers=auth,
        json={
            "lease_id": assignment["lease_id"],
            "attempt_id": assignment["attempt_id"],
            "idempotency_key": "metric-batch-01",
            "entries": [{"sequence": 1, "key": "loss_g_total", "value": 1.25, "epoch": 1}],
        },
    )
    assert metrics.status_code == 200, metrics.text
    assert metrics.json()["accepted"] == 1

    artifacts = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifacts",
        headers=auth,
        json={
            "lease_id": assignment["lease_id"],
            "attempt_id": assignment["attempt_id"],
            "idempotency_key": "artifact-batch-1",
            "artifacts": [
                {
                    "artifact_type": "final_small_model",
                    "filename": "final_small_model.pth",
                    "storage_uri": "file:///tmp/fake-final-small-model.pth",
                    "size_bytes": 64_000_000,
                    "sha256": "a" * 64,
                    "mime_type": "application/octet-stream",
                    "metadata": {"fake": True},
                }
            ],
        },
    )
    assert artifacts.status_code == 200, artifacts.text
    assert artifacts.json()["accepted"] == 1

    async with app.state.database.session_factory() as session:
        stored_logs = list((await session.scalars(select(JobLog))).all())
        assert len(stored_logs) == 1
        persisted_log = f"{stored_logs[0].message} {stored_logs[0].fields_json}"
        for secret in (
            "db-secret",
            "eyJdatabase.payload.signature",
            "db-password",
            "db-worker-token",
            "db-api-key",
        ):
            assert secret not in persisted_log
        assert "[REDACTED]" in persisted_log
        assert stored_logs[0].fields_json["nested"]["safe"] == "preserved"
        assert len(list((await session.scalars(select(Metric))).all())) == 1
        assert len(list((await session.scalars(select(Artifact))).all())) == 1

        job = await session.get(Job, job_id)
        assert job is not None
        job.current_attempt_id = None
        await session.commit()

    stale_attempt = await client.post(
        f"/api/v1/workers/jobs/{job_id}/logs",
        headers=auth,
        json={
            **log_payload,
            "idempotency_key": "logs-stale-attempt",
            "entries": [{"sequence": 3, "message": "stale attempt"}],
        },
    )
    assert stale_attempt.status_code == 409


async def test_training_epoch_status_is_bounded_monotonic_and_not_cleared(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    _, token = await register_worker(client, "gpu-epoch-status", engine_mode="fake")
    _, _, job_id = await create_job(
        client,
        admin_headers,
        job_overrides={"training": {"epochs": 5}},
    )
    assignment = (await claim(client, token)).json()
    auth = {"Authorization": f"Bearer {token}"}
    for status_name in (
        "downloading_dataset",
        "validating_dataset",
        "preprocessing",
        "extracting_f0",
        "extracting_features",
        "training",
    ):
        response = await client.post(
            f"/api/v1/workers/jobs/{job_id}/status",
            headers=auth,
            json={"lease_id": assignment["lease_id"], "status": status_name},
        )
        assert response.status_code == 200, response.text

    advanced = await client.post(
        f"/api/v1/workers/jobs/{job_id}/status",
        headers=auth,
        json={
            "lease_id": assignment["lease_id"],
            "status": "training",
            "current_epoch": 3,
        },
    )
    assert advanced.status_code == 200, advanced.text
    regressed = await client.post(
        f"/api/v1/workers/jobs/{job_id}/status",
        headers=auth,
        json={
            "lease_id": assignment["lease_id"],
            "status": "training",
            "current_epoch": 2,
        },
    )
    assert regressed.status_code == 409
    oversized = await client.post(
        f"/api/v1/workers/jobs/{job_id}/status",
        headers=auth,
        json={
            "lease_id": assignment["lease_id"],
            "status": "training",
            "current_epoch": 6,
        },
    )
    assert oversized.status_code == 422
    next_stage = await client.post(
        f"/api/v1/workers/jobs/{job_id}/status",
        headers=auth,
        json={"lease_id": assignment["lease_id"], "status": "saving_checkpoint"},
    )
    assert next_stage.status_code == 200, next_stage.text

    async with app.state.database.session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.current_epoch == 3


async def test_metric_epoch_projection_replay_conflicts_and_batch_atomicity(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    _, token = await register_worker(client, "gpu-metric-ledger", engine_mode="fake")
    _, _, job_id = await create_job(
        client,
        admin_headers,
        job_overrides={"training": {"epochs": 5}},
    )
    assignment = (await claim(client, token)).json()
    auth = {"Authorization": f"Bearer {token}"}
    url = f"/api/v1/workers/jobs/{job_id}/metrics"
    payload = {
        "lease_id": assignment["lease_id"],
        "attempt_id": assignment["attempt_id"],
        "idempotency_key": "metric-epoch-batch-01",
        "entries": [
            {"sequence": 10, "key": "loss_g_total", "value": 1.5, "epoch": 2},
            {"sequence": 11, "key": "current_epoch", "value": 3.0, "epoch": 3},
        ],
    }
    accepted = await client.post(url, headers=auth, json=payload)
    replayed = await client.post(url, headers=auth, json=payload)
    assert accepted.json() == {"accepted": 2, "duplicate": False}
    assert replayed.json() == {"accepted": 0, "duplicate": True}

    key_conflict = await client.post(
        url,
        headers=auth,
        json={
            **payload,
            "entries": [{"sequence": 10, "key": "loss_g_total", "value": 9.0, "epoch": 2}],
        },
    )
    assert key_conflict.status_code == 409
    sequence_conflict = await client.post(
        url,
        headers=auth,
        json={
            **payload,
            "idempotency_key": "metric-epoch-batch-02",
            "entries": [
                {"sequence": 11, "key": "current_epoch", "value": 4.0, "epoch": 4},
                {"sequence": 12, "key": "loss_d_total", "value": 2.0, "epoch": 4},
            ],
        },
    )
    assert sequence_conflict.status_code == 409
    invalid_batch = await client.post(
        url,
        headers=auth,
        json={
            **payload,
            "idempotency_key": "metric-epoch-batch-03",
            "entries": [
                {"sequence": 12, "key": "loss_d_total", "value": 2.0, "epoch": 4},
                {"sequence": 13, "key": "loss_mel", "value": 1.0, "epoch": 6},
            ],
        },
    )
    assert invalid_batch.status_code == 422

    async with app.state.database.session_factory() as session:
        job = await session.get(Job, job_id)
        metrics = list((await session.scalars(select(Metric).order_by(Metric.sequence))).all())
        batches = list((await session.scalars(select(IngestBatch))).all())
        assert job is not None and job.current_epoch == 3
        assert [metric.sequence for metric in metrics] == [10, 11]
        assert len(batches) == 1
        assert batches[0].payload_fingerprint is not None


async def test_telemetry_schema_body_limit_and_concurrent_exact_replay(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    _, token = await register_worker(client, "gpu-telemetry-bound", engine_mode="fake")
    _, _, job_id = await create_job(client, admin_headers)
    assignment = (await claim(client, token)).json()
    auth = {"Authorization": f"Bearer {token}"}
    metric_url = f"/api/v1/workers/jobs/{job_id}/metrics"
    common = {
        "lease_id": assignment["lease_id"],
        "attempt_id": assignment["attempt_id"],
        "idempotency_key": "concurrent-metric-batch",
        "entries": [{"sequence": 21, "key": "loss_g_total", "value": 1.0}],
    }
    first, second = await asyncio.gather(
        client.post(metric_url, headers=auth, json=common),
        client.post(metric_url, headers=auth, json=common),
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert sorted((first.json()["accepted"], second.json()["accepted"])) == [0, 1]
    assert sorted((first.json()["duplicate"], second.json()["duplicate"])) == [False, True]

    invalid_key = await client.post(
        metric_url,
        headers=auth,
        json={
            **common,
            "idempotency_key": "invalid-key-batch",
            "entries": [{"sequence": 22, "key": "bad key", "value": 1.0}],
        },
    )
    assert invalid_key.status_code == 422
    non_finite = await client.post(
        metric_url,
        headers={**auth, "Content-Type": "application/json"},
        content=json.dumps(
            {
                **common,
                "idempotency_key": "non-finite-batch",
                "entries": [{"sequence": 22, "key": "loss_mel", "value": None}],
            }
        ).replace("null", "NaN"),
    )
    assert non_finite.status_code == 422

    log_url = f"/api/v1/workers/jobs/{job_id}/logs"
    too_large = await client.post(
        log_url,
        headers=auth,
        json={
            "lease_id": assignment["lease_id"],
            "attempt_id": assignment["attempt_id"],
            "idempotency_key": "oversized-log-batch",
            "entries": [
                {
                    "sequence": 1,
                    "message": "bounded",
                    "fields": {"padding": "x" * (2 * 1024**2)},
                }
            ],
        },
    )
    assert too_large.status_code == 413
    assert too_large.json()["detail"] == "worker telemetry request body is too large"


async def test_terminal_watermarks_allow_bounded_late_telemetry_without_epoch_projection(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    _, token = await register_worker(client, "gpu-terminal-telemetry", engine_mode="fake")
    _, _, job_id = await create_job(
        client,
        admin_headers,
        job_overrides={"training": {"epochs": 5}},
    )
    assignment = (await claim(client, token)).json()
    auth = {"Authorization": f"Bearer {token}"}
    metric_url = f"/api/v1/workers/jobs/{job_id}/metrics"
    log_url = f"/api/v1/workers/jobs/{job_id}/logs"
    status_url = f"/api/v1/workers/jobs/{job_id}/status"

    active_metric = await client.post(
        metric_url,
        headers=auth,
        json={
            "lease_id": assignment["lease_id"],
            "attempt_id": assignment["attempt_id"],
            "idempotency_key": "active-terminal-metric",
            "entries": [{"sequence": 0, "key": "current_epoch", "value": 3, "epoch": 3}],
        },
    )
    assert active_metric.status_code == 200, active_metric.text
    terminal = await client.post(
        status_url,
        headers=auth,
        json={
            "lease_id": assignment["lease_id"],
            "status": "failed",
            "error_code": "RVC_PROCESS_EXITED",
            "error_message": "training process exited",
            "telemetry_log_count": 2,
            "telemetry_metric_count": 4,
        },
    )
    assert terminal.status_code == 200, terminal.text

    late_log_payload = {
        "lease_id": assignment["lease_id"],
        "attempt_id": assignment["attempt_id"],
        "idempotency_key": "late-terminal-log",
        "entries": [{"sequence": 1, "message": "durable late log"}],
    }
    late_metric_payload = {
        "lease_id": assignment["lease_id"],
        "attempt_id": assignment["attempt_id"],
        "idempotency_key": "late-terminal-metric",
        "entries": [
            {"sequence": 1, "key": "current_epoch", "value": 4, "epoch": 4},
            {"sequence": 2, "key": "loss_g_total", "value": 1.0, "epoch": 4},
        ],
    }
    late_log = await client.post(log_url, headers=auth, json=late_log_payload)
    late_metric = await client.post(metric_url, headers=auth, json=late_metric_payload)
    replayed_log = await client.post(log_url, headers=auth, json=late_log_payload)
    replayed_metric = await client.post(metric_url, headers=auth, json=late_metric_payload)
    assert late_log.json() == {"accepted": 1, "duplicate": False}
    assert late_metric.json() == {"accepted": 2, "duplicate": False}
    assert replayed_log.json() == {"accepted": 0, "duplicate": True}
    assert replayed_metric.json() == {"accepted": 0, "duplicate": True}

    over_log = await client.post(
        log_url,
        headers=auth,
        json={
            **late_log_payload,
            "idempotency_key": "late-log-over-watermark",
            "entries": [{"sequence": 2, "message": "outside watermark"}],
        },
    )
    over_metric = await client.post(
        metric_url,
        headers=auth,
        json={
            **late_metric_payload,
            "idempotency_key": "late-metric-over-watermark",
            "entries": [{"sequence": 4, "key": "loss_mel", "value": 1.0}],
        },
    )
    sequence_conflict = await client.post(
        metric_url,
        headers=auth,
        json={
            **late_metric_payload,
            "idempotency_key": "late-metric-sequence-conflict",
            "entries": [{"sequence": 1, "key": "current_epoch", "value": 5, "epoch": 5}],
        },
    )
    assert over_log.status_code == 409
    assert over_metric.status_code == 409
    assert sequence_conflict.status_code == 409

    _, other_token = await register_worker(client, "gpu-terminal-cross-worker", engine_mode="fake")
    cross_worker = await client.post(
        log_url,
        headers={"Authorization": f"Bearer {other_token}"},
        json={
            **late_log_payload,
            "idempotency_key": "late-cross-worker",
            "entries": [{"sequence": 0, "message": "wrong worker"}],
        },
    )
    assert cross_worker.status_code == 409

    async with app.state.database.session_factory() as session:
        job = await session.get(Job, job_id)
        attempt = await session.get(JobAttempt, assignment["attempt_id"])
        assert job is not None and job.current_epoch == 3
        assert attempt is not None
        assert attempt.telemetry_log_count == 2
        assert attempt.telemetry_metric_count == 4

    retried = await client.post(f"/api/v1/jobs/{job_id}/retry", headers=admin_headers)
    assert retried.status_code == 200, retried.text
    old_attempt_metric = await client.post(
        metric_url,
        headers=auth,
        json={
            **late_metric_payload,
            "idempotency_key": "late-old-attempt-metric",
            "entries": [{"sequence": 3, "key": "current_epoch", "value": 5, "epoch": 5}],
        },
    )
    assert old_attempt_metric.status_code == 200, old_attempt_metric.text
    async with app.state.database.session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status == "queued"
        assert job.current_attempt_id is None
        assert job.current_epoch is None


async def test_terminal_telemetry_legacy_and_low_watermarks_fail_closed(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    _, token = await register_worker(client, "gpu-terminal-legacy", engine_mode="fake")
    _, _, job_id = await create_job(client, admin_headers)
    assignment = (await claim(client, token)).json()
    auth = {"Authorization": f"Bearer {token}"}
    metric_url = f"/api/v1/workers/jobs/{job_id}/metrics"
    status_url = f"/api/v1/workers/jobs/{job_id}/status"
    metric_payload = {
        "lease_id": assignment["lease_id"],
        "attempt_id": assignment["attempt_id"],
        "idempotency_key": "watermark-existing-sequence",
        "entries": [{"sequence": 2, "key": "loss_g_total", "value": 1.0}],
    }
    assert (await client.post(metric_url, headers=auth, json=metric_payload)).status_code == 200
    too_low = await client.post(
        status_url,
        headers=auth,
        json={
            "lease_id": assignment["lease_id"],
            "status": "failed",
            "error_message": "failed",
            "telemetry_log_count": 0,
            "telemetry_metric_count": 2,
        },
    )
    assert too_low.status_code == 409
    terminal = await client.post(
        status_url,
        headers=auth,
        json={
            "lease_id": assignment["lease_id"],
            "status": "failed",
            "error_message": "failed",
        },
    )
    assert terminal.status_code == 200, terminal.text
    legacy_late = await client.post(
        metric_url,
        headers=auth,
        json={
            **metric_payload,
            "idempotency_key": "legacy-terminal-late",
            "entries": [{"sequence": 1, "key": "loss_mel", "value": 1.0}],
        },
    )
    assert legacy_late.status_code == 409
    assert legacy_late.json()["detail"] == "terminal attempt has no telemetry watermarks"

    async with app.state.database.session_factory() as session:
        attempt = await session.get(JobAttempt, assignment["attempt_id"])
        assert attempt is not None
        assert attempt.telemetry_log_count is None
        assert attempt.telemetry_metric_count is None


async def test_terminal_commit_wins_over_active_ingest_then_late_retry_succeeds(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch,
) -> None:
    _, token = await register_worker(client, "gpu-terminal-ingest-race", engine_mode="fake")
    _, _, job_id = await create_job(client, admin_headers)
    assignment = (await claim(client, token)).json()
    auth = {"Authorization": f"Bearer {token}"}
    log_url = f"/api/v1/workers/jobs/{job_id}/logs"
    payload = {
        "lease_id": assignment["lease_id"],
        "attempt_id": assignment["attempt_id"],
        "idempotency_key": "terminal-race-late-log",
        "entries": [{"sequence": 0, "message": "pending before terminal"}],
    }
    original_fence = worker_routes._acquire_active_telemetry_write_fence
    ingest_at_fence = asyncio.Event()
    release_ingest = asyncio.Event()

    async def delayed_ingest_fence(session, **kwargs):
        ingest_at_fence.set()
        await release_ingest.wait()
        return await original_fence(session, **kwargs)

    monkeypatch.setattr(
        worker_routes,
        "_acquire_active_telemetry_write_fence",
        delayed_ingest_fence,
    )
    ingest_task = asyncio.create_task(client.post(log_url, headers=auth, json=payload))
    await asyncio.wait_for(ingest_at_fence.wait(), timeout=2)
    terminal = await client.post(
        f"/api/v1/workers/jobs/{job_id}/status",
        headers=auth,
        json={
            "lease_id": assignment["lease_id"],
            "status": "failed",
            "error_message": "failed",
            "telemetry_log_count": 1,
            "telemetry_metric_count": 0,
        },
    )
    assert terminal.status_code == 200, terminal.text
    release_ingest.set()
    raced_ingest = await asyncio.wait_for(ingest_task, timeout=2)
    assert raced_ingest.status_code == 503
    assert raced_ingest.headers["Retry-After"] == "1"

    monkeypatch.setattr(
        worker_routes,
        "_acquire_active_telemetry_write_fence",
        original_fence,
    )
    late_retry = await client.post(log_url, headers=auth, json=payload)
    assert late_retry.status_code == 200, late_retry.text
    assert late_retry.json() == {"accepted": 1, "duplicate": False}
    async with app.state.database.session_factory() as session:
        logs = list((await session.scalars(select(JobLog))).all())
        assert [log.sequence for log in logs] == [0]


async def test_cancel_request_fences_active_telemetry_until_terminal_watermark(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    _, token = await register_worker(client, "gpu-cancel-telemetry", engine_mode="fake")
    _, _, job_id = await create_job(client, admin_headers)
    assignment = (await claim(client, token)).json()
    auth = {"Authorization": f"Bearer {token}"}
    payload = {
        "lease_id": assignment["lease_id"],
        "attempt_id": assignment["attempt_id"],
        "idempotency_key": "cancel-fenced-log",
        "entries": [{"sequence": 0, "message": "pending cancellation log"}],
    }
    cancelled = await client.post(f"/api/v1/jobs/{job_id}/cancel", headers=admin_headers)
    assert cancelled.status_code == 200, cancelled.text
    fenced = await client.post(
        f"/api/v1/workers/jobs/{job_id}/logs",
        headers=auth,
        json=payload,
    )
    assert fenced.status_code == 503
    terminal = await client.post(
        f"/api/v1/workers/jobs/{job_id}/status",
        headers=auth,
        json={
            "lease_id": assignment["lease_id"],
            "status": "cancelled",
            "telemetry_log_count": 1,
            "telemetry_metric_count": 0,
        },
    )
    assert terminal.status_code == 200, terminal.text
    late = await client.post(
        f"/api/v1/workers/jobs/{job_id}/logs",
        headers=auth,
        json=payload,
    )
    assert late.status_code == 200, late.text
    assert late.json() == {"accepted": 1, "duplicate": False}


async def test_expired_lease_is_rejected(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    _, token = await register_worker(client, "gpu-01", engine_mode="fake")
    _, _, job_id = await create_job(client, admin_headers)
    assignment = (await claim(client, token)).json()
    async with app.state.database.session_factory() as session:
        lease = await session.get(JobLease, assignment["lease_id"])
        assert lease is not None
        lease.expires_at = utc_now() - timedelta(seconds=1)
        await session.commit()

    response = await client.post(
        f"/api/v1/workers/jobs/{job_id}/lease/renew",
        headers={"Authorization": f"Bearer {token}"},
        json={"lease_id": assignment["lease_id"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "job lease expired"

    async with app.state.database.session_factory() as session:
        # Expiry rejection must not hide the abandoned lease from the recovery
        # reaper before the Worker offline grace window has elapsed.
        lease = await session.get(JobLease, assignment["lease_id"])
        assert lease is not None
        assert lease.active is True


async def test_offline_expired_lease_is_failed_and_atomically_reassigned(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    first_worker_id, first_token = await register_worker(client, "lease-lost-gpu-01")
    _, second_token = await register_worker(client, "lease-recovery-gpu-02")
    _, _, job_id = await create_job(client, admin_headers)
    first = (await claim(client, first_token)).json()

    async with app.state.database.session_factory() as session:
        lease = await session.get(JobLease, first["lease_id"])
        worker = await session.get(Worker, first_worker_id)
        assert lease is not None and worker is not None
        lease.expires_at = utc_now() - timedelta(seconds=1)
        worker.last_heartbeat_at = utc_now() - timedelta(
            seconds=app.state.settings.worker_offline_seconds + 1
        )
        await session.commit()

    reassigned = await claim(client, second_token)
    assert reassigned.status_code == 200, reassigned.text
    second = reassigned.json()
    assert second["job_id"] == job_id
    assert second["attempt_number"] == 2
    assert second["attempt_id"] != first["attempt_id"]

    async with app.state.database.session_factory() as session:
        old_attempt = await session.get(JobAttempt, first["attempt_id"])
        old_lease = await session.get(JobLease, first["lease_id"])
        old_worker = await session.get(Worker, first_worker_id)
        assert old_attempt is not None and old_lease is not None and old_worker is not None
        assert old_attempt.status == "failed"
        assert old_attempt.error_code == "worker_lease_expired"
        assert old_attempt.finished_at is not None
        assert old_lease.active is False
        assert old_worker.current_job_id is None
        assert old_worker.status == "idle"
        statuses = list(
            (
                await session.scalars(
                    select(JobStatusEvent.status).where(JobStatusEvent.job_id == job_id)
                )
            ).all()
        )
        assert "failed" in statuses
        assert "retrying" in statuses
        assert statuses.count("assigned") == 2


async def test_expired_lease_waits_for_offline_grace(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    _, first_token = await register_worker(client, "lease-grace-gpu-01")
    _, second_token = await register_worker(client, "lease-grace-gpu-02")
    _, _, job_id = await create_job(client, admin_headers)
    first = (await claim(client, first_token)).json()
    async with app.state.database.session_factory() as session:
        lease = await session.get(JobLease, first["lease_id"])
        assert lease is not None
        lease.expires_at = utc_now() - timedelta(seconds=1)
        await session.commit()

    blocked = await claim(client, second_token)
    assert blocked.status_code == 204
    async with app.state.database.session_factory() as session:
        job = await session.get(Job, job_id)
        lease = await session.get(JobLease, first["lease_id"])
        assert job is not None and lease is not None
        assert job.current_attempt_id == first["attempt_id"]
        assert job.status == "assigned"
        assert lease.active is True


async def test_cancelled_abandoned_job_is_not_requeued(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    worker_id, first_token = await register_worker(client, "cancel-lost-gpu-01")
    _, second_token = await register_worker(client, "cancel-recovery-gpu-02")
    _, _, job_id = await create_job(client, admin_headers)
    first = (await claim(client, first_token)).json()
    cancelled = await client.post(f"/api/v1/jobs/{job_id}/cancel", headers=admin_headers)
    assert cancelled.status_code == 200
    async with app.state.database.session_factory() as session:
        lease = await session.get(JobLease, first["lease_id"])
        worker = await session.get(Worker, worker_id)
        assert lease is not None and worker is not None
        lease.expires_at = utc_now() - timedelta(seconds=1)
        worker.last_heartbeat_at = utc_now() - timedelta(
            seconds=app.state.settings.worker_offline_seconds + 1
        )
        await session.commit()

    assert (await claim(client, second_token)).status_code == 204
    async with app.state.database.session_factory() as session:
        job = await session.get(Job, job_id)
        attempt = await session.get(JobAttempt, first["attempt_id"])
        assert job is not None and attempt is not None
        assert job.status == "cancelled"
        assert attempt.status == "cancelled"


async def test_lease_recovery_attempt_cap_fails_closed(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    app.state.settings.lease_recovery_max_attempts = 1
    worker_id, first_token = await register_worker(client, "capped-lost-gpu-01")
    _, second_token = await register_worker(client, "capped-recovery-gpu-02")
    _, _, job_id = await create_job(client, admin_headers)
    first = (await claim(client, first_token)).json()
    async with app.state.database.session_factory() as session:
        lease = await session.get(JobLease, first["lease_id"])
        worker = await session.get(Worker, worker_id)
        assert lease is not None and worker is not None
        lease.expires_at = utc_now() - timedelta(seconds=1)
        worker.last_heartbeat_at = utc_now() - timedelta(
            seconds=app.state.settings.worker_offline_seconds + 1
        )
        await session.commit()

    assert (await claim(client, second_token)).status_code == 204
    async with app.state.database.session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.current_attempt_id == first["attempt_id"]
        assert job.error_code == "worker_lease_expired"


async def test_failed_job_retry_preserves_attempt_history(
    app: FastAPI, client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    _, token = await register_worker(client, "gpu-01")
    _, _, job_id = await create_job(client, admin_headers)
    first_claim = (await claim(client, token)).json()
    failed = await client.post(
        f"/api/v1/workers/jobs/{job_id}/status",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "lease_id": first_claim["lease_id"],
            "status": "failed",
            "error_code": "RVC_PROCESS_EXITED",
            "error_message": "training process exited with code 1",
        },
    )
    assert failed.status_code == 200, failed.text
    retried = await client.post(
        f"/api/v1/jobs/{job_id}/retry",
        headers=admin_headers,
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "queued"
    assert retried.json()["attempt_count"] == 1

    second_claim_response = await claim(client, token)
    assert second_claim_response.status_code == 200, second_claim_response.text
    assert second_claim_response.json()["attempt_number"] == 2
    async with app.state.database.session_factory() as session:
        attempts = list(
            (await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id))).all()
        )
        assert [attempt.attempt_number for attempt in attempts] == [1, 2]


async def test_completion_requires_model_and_index_artifacts(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    _, token = await register_worker(client, "gpu-01", engine_mode="fake")
    _, _, job_id = await create_job(client, admin_headers)
    assignment = (await claim(client, token)).json()
    auth = {"Authorization": f"Bearer {token}"}
    for target in (
        "downloading_dataset",
        "validating_dataset",
        "preparing_flat_dataset",
        "preprocessing",
        "extracting_f0",
        "extracting_features",
        "training",
        "saving_checkpoint",
        "building_index",
        "collecting_small_model",
        "uploading_artifacts",
    ):
        response = await client.post(
            f"/api/v1/workers/jobs/{job_id}/status",
            headers=auth,
            json={"lease_id": assignment["lease_id"], "status": target},
        )
        assert response.status_code == 200, response.text

    model_batch = {
        "lease_id": assignment["lease_id"],
        "attempt_id": assignment["attempt_id"],
        "idempotency_key": "final-model-batch",
        "artifacts": [
            {
                "artifact_type": "final_small_model",
                "filename": "final_small_model.pth",
                "storage_uri": "file:///tmp/fake-final-small-model.pth",
                "size_bytes": 64_000_000,
                "sha256": "a" * 64,
                "metadata": {"fake": True},
            }
        ],
    }
    assert (
        await client.post(
            f"/api/v1/workers/jobs/{job_id}/artifacts",
            headers=auth,
            json=model_batch,
        )
    ).status_code == 200
    missing_index = await client.post(
        f"/api/v1/workers/jobs/{job_id}/status",
        headers=auth,
        json={"lease_id": assignment["lease_id"], "status": "completed"},
    )
    assert missing_index.status_code == 409
    assert missing_index.json()["detail"] == "required artifacts are not registered"

    index_batch = {
        "lease_id": assignment["lease_id"],
        "attempt_id": assignment["attempt_id"],
        "idempotency_key": "final-index-batch",
        "artifacts": [
            {
                "artifact_type": "final_index",
                "filename": "final.index",
                "storage_uri": "file:///tmp/fake-final.index",
                "size_bytes": 1_000_000,
                "sha256": "b" * 64,
                "metadata": {
                    "original_filename": "added_IVF123_Flat.index",
                    "fake": True,
                },
            }
        ],
    }
    assert (
        await client.post(
            f"/api/v1/workers/jobs/{job_id}/artifacts",
            headers=auth,
            json=index_batch,
        )
    ).status_code == 200
    completed = await client.post(
        f"/api/v1/workers/jobs/{job_id}/status",
        headers=auth,
        json={"lease_id": assignment["lease_id"], "status": "completed"},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "completed"


async def test_completion_does_not_require_index_when_added_index_collection_is_disabled(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    _, token = await register_worker(client, "gpu-index-disabled", engine_mode="fake")
    _, _, job_id = await create_job(
        client,
        admin_headers,
        job_overrides={
            "index": {
                "build_index": True,
                "collect_total_fea": True,
                "collect_added_index": False,
            }
        },
    )
    assignment = (await claim(client, token)).json()
    auth = {"Authorization": f"Bearer {token}"}
    for target in (
        "downloading_dataset",
        "validating_dataset",
        "preparing_flat_dataset",
        "preprocessing",
        "extracting_f0",
        "extracting_features",
        "training",
        "saving_checkpoint",
        "building_index",
        "collecting_small_model",
        "uploading_artifacts",
    ):
        response = await client.post(
            f"/api/v1/workers/jobs/{job_id}/status",
            headers=auth,
            json={"lease_id": assignment["lease_id"], "status": target},
        )
        assert response.status_code == 200, response.text

    model = await client.post(
        f"/api/v1/workers/jobs/{job_id}/artifacts",
        headers=auth,
        json={
            "lease_id": assignment["lease_id"],
            "attempt_id": assignment["attempt_id"],
            "idempotency_key": "model-with-index-disabled",
            "artifacts": [
                {
                    "artifact_type": "final_small_model",
                    "filename": "final_small_model.pth",
                    "storage_uri": "file:///tmp/fake-model-index-disabled.pth",
                    "size_bytes": 1024,
                    "sha256": "c" * 64,
                    "metadata": {"fake": True},
                }
            ],
        },
    )
    assert model.status_code == 200, model.text
    completed = await client.post(
        f"/api/v1/workers/jobs/{job_id}/status",
        headers=auth,
        json={"lease_id": assignment["lease_id"], "status": "completed"},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "completed"
