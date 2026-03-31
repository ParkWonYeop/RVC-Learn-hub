from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

import rvc_manager_api.routers.workers as worker_routes
from rvc_manager_api.models import AuditEvent, Job, JobAttempt, JobLease, JobStatusEvent, Worker
from rvc_manager_api.security import hash_worker_token
from rvc_manager_api.services.workers import as_utc
from rvc_orchestrator_contracts import utc_now


def _capabilities() -> dict[str, object]:
    return {
        "engine_mode": "fake",
        "worker_version": "rotation-test",
        "rvc_commit_hash": "0123456789abcdef",
        "supported_rvc_versions": ["v2"],
        "supported_training_f0_methods": ["rmvpe"],
        "gpus": [],
        "disk_free_bytes": 100 * 1024**3,
        "rvc_assets_ready": False,
    }


async def _register(client: AsyncClient, name: str) -> tuple[str, str, str]:
    response = await client.post(
        "/api/v1/workers/register",
        headers={"X-Worker-Bootstrap-Token": "test-bootstrap-token"},
        json={"name": name, "capabilities": _capabilities()},
    )
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"
    return (
        response.json()["worker_id"],
        response.json()["worker_token"],
        response.json()["issued_at"],
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_job(
    client: AsyncClient,
    admin_headers: dict[str, str],
    suffix: str,
) -> str:
    dataset = await client.post(
        "/api/v1/datasets",
        headers=admin_headers,
        json={
            "name": f"rotation-dataset-{suffix}",
            "storage_uri": f"local:///legacy/{suffix}.zip",
            "flat_storage_uri": f"local:///legacy/{suffix}-flat.zip",
        },
    )
    assert dataset.status_code == 201, dataset.text
    experiment = await client.post(
        "/api/v1/experiments",
        headers=admin_headers,
        json={
            "name": f"rotation-experiment-{suffix}",
            "dataset_id": dataset.json()["id"],
        },
    )
    assert experiment.status_code == 201, experiment.text
    job = await client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={
            "job_name": f"rotation-job-{suffix}",
            "experiment_id": experiment.json()["id"],
            "dataset_id": dataset.json()["id"],
            "model": {"version": "v2", "sample_rate": "40k"},
            "f0_extraction": {"training_f0_method": "rmvpe"},
        },
    )
    assert job.status_code == 201, job.text
    return job.json()["id"]


async def _create_and_claim_job(
    client: AsyncClient,
    admin_headers: dict[str, str],
    worker_token: str,
    suffix: str,
) -> tuple[str, dict[str, object]]:
    job_id = await _create_job(client, admin_headers, suffix)
    claimed = await client.post(
        "/api/v1/workers/jobs/claim",
        headers=_auth(worker_token),
        json={"max_wait_seconds": 0},
    )
    assert claimed.status_code == 200, claimed.text
    return job_id, claimed.json()


async def test_two_phase_rotation_is_one_time_fenced_and_immediately_revokes_old_token(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    worker_id, old_token, issued_at = await _register(client, "rotation-worker")
    async with app.state.database.session_factory() as session:
        registered_worker = await session.get(Worker, worker_id)
        assert registered_worker is not None
        assert as_utc(registered_worker.token_issued_at) == datetime.fromisoformat(issued_at)
    rotation_id = str(uuid.uuid4())
    prepared = await client.post(
        "/api/v1/workers/token-rotation/prepare",
        headers=_auth(old_token),
        json={"rotation_id": rotation_id},
    )
    assert prepared.status_code == 201, prepared.text
    assert prepared.headers["cache-control"] == "private, no-store"
    pending_token = prepared.json()["worker_token"]
    assert pending_token != old_token

    assert (
        await client.get("/api/v1/workers/me", headers=_auth(old_token))
    ).status_code == 200
    assert (
        await client.get("/api/v1/workers/me", headers=_auth(pending_token))
    ).status_code == 401
    status = await client.get(
        "/api/v1/workers/token-rotation",
        headers=_auth(old_token),
    )
    assert status.status_code == 200
    assert status.json()["pending"] is True
    assert status.json()["rotation_id"] == rotation_id
    assert "worker_token" not in status.text
    admin_detail = await client.get(
        f"/api/v1/workers/{worker_id}",
        headers=admin_headers,
    )
    assert admin_detail.status_code == 200
    assert admin_detail.json()["token_rotation_pending"] is True
    assert admin_detail.json()["token_rotation_expires_at"] is not None
    assert "token_hash" not in admin_detail.text
    assert "pending_token_hash" not in admin_detail.text
    queued_job_id = await _create_job(client, admin_headers, "pending-rotation")
    blocked_claim = await client.post(
        "/api/v1/workers/jobs/claim",
        headers=_auth(old_token),
        json={"max_wait_seconds": 0},
    )
    assert blocked_claim.status_code == 409
    async with app.state.database.session_factory() as session:
        queued_job = await session.get(Job, queued_job_id)
        assert queued_job is not None and queued_job.status == "queued"

    replay = await client.post(
        "/api/v1/workers/token-rotation/prepare",
        headers=_auth(old_token),
        json={"rotation_id": rotation_id},
    )
    assert replay.status_code == 409
    assert pending_token not in replay.text
    assert (
        await client.post(
            "/api/v1/workers/token-rotation/prepare",
            headers=admin_headers,
            json={"rotation_id": str(uuid.uuid4())},
        )
    ).status_code == 401
    wrong_pending = await client.post(
        "/api/v1/workers/token-rotation/activate",
        headers={**_auth(old_token), "X-RVC-Pending-Worker-Token": "rvcw_" + "x" * 43},
        json={"rotation_id": rotation_id},
    )
    assert wrong_pending.status_code == 409

    activated = await client.post(
        "/api/v1/workers/token-rotation/activate",
        headers={
            **_auth(old_token),
            "X-RVC-Pending-Worker-Token": pending_token,
        },
        json={"rotation_id": rotation_id},
    )
    assert activated.status_code == 200, activated.text
    assert (
        await client.get("/api/v1/workers/me", headers=_auth(old_token))
    ).status_code == 401
    assert (
        await client.get("/api/v1/workers/me", headers=_auth(pending_token))
    ).status_code == 200

    async with app.state.database.session_factory() as session:
        worker = await session.get(Worker, worker_id)
        assert worker is not None
        assert as_utc(worker.token_issued_at) == datetime.fromisoformat(
            activated.json()["token_issued_at"]
        )
        assert worker.token_hash == hash_worker_token(pending_token, app.state.settings)
        assert worker.pending_token_hash is None
        serialized = repr(worker.__dict__)
        assert old_token not in serialized
        assert pending_token not in serialized
        events = list(
            (
                await session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.resource_id == worker_id)
                    .order_by(AuditEvent.occurred_at.asc(), AuditEvent.id.asc())
                )
            ).all()
        )
        assert [event.action for event in events] == [
            "worker.token_rotation_prepared",
            "worker.token_rotated",
        ]
        audit_text = " ".join(repr(event.details_json) for event in events)
        assert old_token not in audit_text
        assert pending_token not in audit_text


async def test_rotation_abort_and_expiry_preserve_old_token(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    worker_id, old_token, _issued_at = await _register(client, "rotation-abort-worker")
    first_id = str(uuid.uuid4())
    first = await client.post(
        "/api/v1/workers/token-rotation/prepare",
        headers=_auth(old_token),
        json={"rotation_id": first_id},
    )
    first_pending = first.json()["worker_token"]
    aborted = await client.post(
        "/api/v1/workers/token-rotation/abort",
        headers=_auth(old_token),
        json={"rotation_id": first_id},
    )
    assert aborted.status_code == 200
    assert aborted.json()["pending"] is False
    assert (
        await client.get("/api/v1/workers/me", headers=_auth(old_token))
    ).status_code == 200
    assert (
        await client.get("/api/v1/workers/me", headers=_auth(first_pending))
    ).status_code == 401

    second_id = str(uuid.uuid4())
    second = await client.post(
        "/api/v1/workers/token-rotation/prepare",
        headers=_auth(old_token),
        json={"rotation_id": second_id},
    )
    async with app.state.database.session_factory() as session:
        worker = await session.get(Worker, worker_id)
        assert worker is not None
        worker.token_rotation_expires_at = utc_now() - timedelta(seconds=1)
        await session.commit()
    expired = await client.post(
        "/api/v1/workers/token-rotation/activate",
        headers={
            **_auth(old_token),
            "X-RVC-Pending-Worker-Token": second.json()["worker_token"],
        },
        json={"rotation_id": second_id},
    )
    assert expired.status_code == 409
    replacement = await client.post(
        "/api/v1/workers/token-rotation/prepare",
        headers=_auth(old_token),
        json={"rotation_id": str(uuid.uuid4())},
    )
    assert replacement.status_code == 201
    assert (
        await client.get("/api/v1/workers/me", headers=_auth(old_token))
    ).status_code == 200


async def test_admin_force_revoke_cancels_ledger_and_bootstrap_reenrolls_same_worker(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    worker_name = "emergency-revoke-worker"
    worker_id, old_token, _issued_at = await _register(client, worker_name)
    job_id, claim = await _create_and_claim_job(
        client,
        admin_headers,
        old_token,
        "emergency",
    )
    rotation_while_busy = await client.post(
        "/api/v1/workers/token-rotation/prepare",
        headers=_auth(old_token),
        json={"rotation_id": str(uuid.uuid4())},
    )
    assert rotation_while_busy.status_code == 409

    revoke_path = f"/api/v1/workers/{worker_id}/token/revoke"
    payload = {
        "expected_worker_name": worker_name,
        "reason_code": "confirmed_compromise",
        "force_cancel_active": False,
    }
    assert (
        await client.post(revoke_path, headers=_auth(old_token), json=payload)
    ).status_code == 401
    refused = await client.post(revoke_path, headers=admin_headers, json=payload)
    assert refused.status_code == 409
    assert (
        await client.get("/api/v1/workers/me", headers=_auth(old_token))
    ).status_code == 200

    revoked = await client.post(
        revoke_path,
        headers=admin_headers,
        json={**payload, "force_cancel_active": True},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["is_active"] is False
    assert revoked.json()["status"] == "draining"
    assert (
        await client.get("/api/v1/workers/me", headers=_auth(old_token))
    ).status_code == 401

    async with app.state.database.session_factory() as session:
        job = await session.get(Job, job_id)
        attempt = await session.get(JobAttempt, claim["attempt_id"])
        lease = await session.get(JobLease, claim["lease_id"])
        worker = await session.get(Worker, worker_id)
        assert job is not None and job.status == "cancelled"
        assert job.error_code == "worker_token_emergency_revoked"
        assert attempt is not None and attempt.status == "cancelled"
        assert attempt.finished_at is not None
        assert lease is not None and lease.active is False and lease.released_at is not None
        assert worker is not None and worker.current_job_id is None
        event = await session.scalar(
            select(JobStatusEvent).where(
                JobStatusEvent.job_id == job_id,
                JobStatusEvent.status == "cancelled",
            )
        )
        assert event is not None and event.source == "manager"

    re_enroll_body = {
        "worker_id": worker_id,
        "name": worker_name,
        "capabilities": _capabilities(),
    }
    assert (
        await client.post(
            "/api/v1/workers/re-enroll",
            headers={"X-Worker-Bootstrap-Token": "wrong"},
            json=re_enroll_body,
        )
    ).status_code == 401
    mismatched = await client.post(
        "/api/v1/workers/re-enroll",
        headers={"X-Worker-Bootstrap-Token": "test-bootstrap-token"},
        json={**re_enroll_body, "name": "different-worker"},
    )
    assert mismatched.status_code == 409
    enrolled = await client.post(
        "/api/v1/workers/re-enroll",
        headers={"X-Worker-Bootstrap-Token": "test-bootstrap-token"},
        json=re_enroll_body,
    )
    assert enrolled.status_code == 201, enrolled.text
    assert enrolled.headers["cache-control"] == "private, no-store"
    assert enrolled.headers["pragma"] == "no-cache"
    assert enrolled.json()["worker_id"] == worker_id
    new_token = enrolled.json()["worker_token"]
    assert new_token != old_token
    assert (
        await client.get("/api/v1/workers/me", headers=_auth(new_token))
    ).status_code == 200
    assert (
        await client.post(
            "/api/v1/workers/re-enroll",
            headers={"X-Worker-Bootstrap-Token": "test-bootstrap-token"},
            json=re_enroll_body,
        )
    ).status_code == 409


async def test_rotation_openapi_declares_one_time_and_operational_responses(
    client: AsyncClient,
) -> None:
    schema = (await client.get("/openapi.json")).json()
    paths = schema["paths"]
    expected_success = {
        "/api/v1/workers/token-rotation/prepare": "201",
        "/api/v1/workers/token-rotation/activate": "200",
        "/api/v1/workers/token-rotation/abort": "200",
        "/api/v1/workers/{worker_id}/token/revoke": "200",
        "/api/v1/workers/re-enroll": "201",
    }
    for path, success_status in expected_success.items():
        responses = paths[path]["post"]["responses"]
        assert success_status in responses
        assert {"409", "429", "503"}.issubset(responses)
    activate_parameters = paths["/api/v1/workers/token-rotation/activate"]["post"][
        "parameters"
    ]
    assert any(
        parameter["in"] == "header"
        and parameter["name"] == "X-RVC-Pending-Worker-Token"
        for parameter in activate_parameters
    )


async def test_force_revoke_serializes_with_status_commit_and_claim(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    worker_name = "revoke-status-race-worker"
    worker_id, token, _issued_at = await _register(client, worker_name)
    job_id, claim = await _create_and_claim_job(
        client,
        admin_headers,
        token,
        "status-race",
    )
    revoke_payload = {
        "expected_worker_name": worker_name,
        "reason_code": "suspected_compromise",
        "force_cancel_active": True,
    }
    status_result, revoke_result = await asyncio.gather(
        client.post(
            f"/api/v1/workers/jobs/{job_id}/status",
            headers=_auth(token),
            json={
                "lease_id": claim["lease_id"],
                "status": "downloading_dataset",
            },
        ),
        client.post(
            f"/api/v1/workers/{worker_id}/token/revoke",
            headers=admin_headers,
            json=revoke_payload,
        ),
    )
    assert revoke_result.status_code == 200, revoke_result.text
    assert status_result.status_code in {200, 401, 409}
    async with app.state.database.session_factory() as session:
        job = await session.get(Job, job_id)
        worker = await session.get(Worker, worker_id)
        lease = await session.get(JobLease, claim["lease_id"])
        assert job is not None and job.status == "cancelled"
        assert worker is not None and worker.is_active is False
        assert worker.current_job_id is None
        assert lease is not None and lease.active is False

    second_name = "revoke-claim-race-worker"
    second_id, second_token, _second_issued_at = await _register(client, second_name)
    queued_job_id = await _create_job(client, admin_headers, "claim-race")
    claim_result, second_revoke = await asyncio.gather(
        client.post(
            "/api/v1/workers/jobs/claim",
            headers=_auth(second_token),
            json={"max_wait_seconds": 0},
        ),
        client.post(
            f"/api/v1/workers/{second_id}/token/revoke",
            headers=admin_headers,
            json={
                "expected_worker_name": second_name,
                "reason_code": "confirmed_compromise",
                "force_cancel_active": True,
            },
        ),
    )
    assert second_revoke.status_code == 200, second_revoke.text
    assert claim_result.status_code in {200, 401, 409}
    async with app.state.database.session_factory() as session:
        queued_job = await session.get(Job, queued_job_id)
        worker = await session.get(Worker, second_id)
        active_lease = await session.scalar(
            select(JobLease).where(
                JobLease.worker_id == second_id,
                JobLease.active.is_(True),
            )
        )
        assert queued_job is not None and queued_job.status in {"queued", "cancelled"}
        assert worker is not None and worker.is_active is False
        assert worker.current_job_id is None
        assert active_lease is None


async def test_emergency_revoke_reloads_once_after_optimistic_commit_conflict(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch,
) -> None:
    worker_name = "bounded-revoke-retry-worker"
    worker_id, old_token, _issued_at = await _register(client, worker_name)
    original_boundary = worker_routes._lock_worker_revocation_boundary
    original_commit = AsyncSession.commit
    injected = False

    async def marked_boundary(session, worker_id):
        nonlocal injected
        result = await original_boundary(session, worker_id)
        if not injected:
            injected = True
            session.info["inject_revoke_stale_commit"] = True
        return result

    async def stale_once(session):
        if session.info.pop("inject_revoke_stale_commit", False):
            raise StaleDataError("injected optimistic conflict")
        await original_commit(session)

    monkeypatch.setattr(worker_routes, "_lock_worker_revocation_boundary", marked_boundary)
    monkeypatch.setattr(AsyncSession, "commit", stale_once)
    revoked = await client.post(
        f"/api/v1/workers/{worker_id}/token/revoke",
        headers=admin_headers,
        json={
            "expected_worker_name": worker_name,
            "reason_code": "suspected_compromise",
            "force_cancel_active": False,
        },
    )
    assert revoked.status_code == 200, revoked.text
    assert injected is True
    assert (
        await client.get("/api/v1/workers/me", headers=_auth(old_token))
    ).status_code == 401
    async with app.state.database.session_factory() as session:
        events = list(
            (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.resource_id == worker_id,
                        AuditEvent.action == "worker.token_revoked",
                    )
                )
            ).all()
        )
        assert len(events) == 1
