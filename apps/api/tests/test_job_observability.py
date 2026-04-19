from __future__ import annotations

import asyncio
from datetime import timedelta
from time import monotonic
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from rvc_manager_api.models import Artifact, JobAttempt, JobLog, Metric, User
from rvc_manager_api.routers import job_observability as observability_router
from rvc_manager_api.security import hash_password
from rvc_orchestrator_contracts import utc_now

USER_PASSWORD = "job-observability-password-1234"


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


async def register_worker(client: AsyncClient) -> str:
    response = await client.post(
        "/api/v1/workers/register",
        headers={"X-Worker-Bootstrap-Token": "test-bootstrap-token"},
        json={
            "name": "observability-worker",
            "capabilities": {
                "engine_mode": "rvc_webui",
                "worker_version": "0.2.0",
                "rvc_commit_hash": "0123456789abcdef",
                "supported_rvc_versions": ["v2"],
                "supported_training_f0_methods": ["rmvpe"],
                "gpus": [
                    {
                        "index": 0,
                        "name": "Observability GPU",
                        "total_vram_mb": 24 * 1024,
                        "free_vram_mb": 20 * 1024,
                    }
                ],
                "disk_free_bytes": 500_000_000_000,
                "rvc_assets_ready": True,
            },
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["worker_id"])


async def seed_job_data(
    app: FastAPI,
    client: AsyncClient,
    owner_headers: dict[str, str],
) -> dict[str, Any]:
    dataset = await client.post(
        "/api/v1/datasets",
        headers=owner_headers,
        json={
            "name": "observability-dataset",
            "storage_uri": "local:///datasets/observability.zip",
            "flat_storage_uri": "local:///datasets/observability-flat",
        },
    )
    assert dataset.status_code == 201, dataset.text
    experiment = await client.post(
        "/api/v1/experiments",
        headers=owner_headers,
        json={
            "name": "observability-experiment",
            "dataset_id": dataset.json()["id"],
        },
    )
    assert experiment.status_code == 201, experiment.text
    job = await client.post(
        "/api/v1/jobs",
        headers=owner_headers,
        json={
            "job_name": "observability-job",
            "experiment_id": experiment.json()["id"],
            "dataset_id": dataset.json()["id"],
            "model": {"version": "v2", "sample_rate": "40k"},
        },
    )
    assert job.status_code == 201, job.text
    worker_id = await register_worker(client)
    now = utc_now().replace(microsecond=0) - timedelta(minutes=5)
    async with app.state.database.session_factory() as session:
        first_attempt = JobAttempt(
            job_id=job.json()["id"],
            worker_id=worker_id,
            attempt_number=1,
            engine_mode="rvc_webui",
            status="failed",
            started_at=now,
            finished_at=now + timedelta(minutes=1),
        )
        second_attempt = JobAttempt(
            job_id=job.json()["id"],
            worker_id=worker_id,
            attempt_number=2,
            engine_mode="rvc_webui",
            status="training",
            started_at=now + timedelta(minutes=2),
        )
        session.add_all([first_attempt, second_attempt])
        await session.flush()
        session.add_all(
            [
                JobLog(
                    job_id=job.json()["id"],
                    attempt_id=first_attempt.id,
                    sequence=2,
                    level="warning",
                    message=(
                        "upload https://objects.example.test/model"
                        "?X-Amz-Signature=secret-signature "
                        "Authorization: Bearer eyJsecret.payload.signature password=hunter2"
                    ),
                    fields_json={
                        "token": "worker-secret-token",
                        "nested": {"api_key": "secret-api-key", "safe": "visible"},
                        "url": "https://objects.example.test/model?signature=field-secret",
                    },
                    occurred_at=now + timedelta(seconds=30),
                ),
                JobLog(
                    job_id=job.json()["id"],
                    attempt_id=first_attempt.id,
                    sequence=0,
                    level="info",
                    message="attempt one started",
                    fields_json={"phase": "start"},
                    occurred_at=now + timedelta(seconds=10),
                ),
                JobLog(
                    job_id=job.json()["id"],
                    attempt_id=first_attempt.id,
                    sequence=1,
                    level="info",
                    message="attempt one training",
                    fields_json={"epoch": 1},
                    occurred_at=now + timedelta(seconds=20),
                ),
                JobLog(
                    job_id=job.json()["id"],
                    attempt_id=second_attempt.id,
                    sequence=0,
                    level="info",
                    message="attempt two started",
                    fields_json={},
                    occurred_at=now + timedelta(minutes=2, seconds=10),
                ),
                Metric(
                    job_id=job.json()["id"],
                    attempt_id=first_attempt.id,
                    sequence=2,
                    epoch=2,
                    step=20,
                    key="train.loss",
                    value=0.8,
                    occurred_at=now + timedelta(seconds=30),
                ),
                Metric(
                    job_id=job.json()["id"],
                    attempt_id=first_attempt.id,
                    sequence=0,
                    epoch=1,
                    step=10,
                    key="train.loss",
                    value=1.2,
                    occurred_at=now + timedelta(seconds=10),
                ),
                Metric(
                    job_id=job.json()["id"],
                    attempt_id=first_attempt.id,
                    sequence=1,
                    epoch=1,
                    step=10,
                    key="system.gpu.0.utilization_percent",
                    value=75.0,
                    occurred_at=now + timedelta(seconds=20),
                ),
                Metric(
                    job_id=job.json()["id"],
                    attempt_id=second_attempt.id,
                    sequence=0,
                    epoch=1,
                    step=1,
                    key="train.loss",
                    value=1.0,
                    occurred_at=now + timedelta(minutes=2, seconds=10),
                ),
                Artifact(
                    job_id=job.json()["id"],
                    attempt_id=first_attempt.id,
                    artifact_type="final_small_model",
                    filename="voice.pth",
                    storage_uri="s3://private-bucket/secret/model",
                    size_bytes=1024,
                    sha256="a" * 64,
                    mime_type="application/octet-stream",
                    metadata_json={"source": "attempt-one"},
                    created_at=now + timedelta(minutes=1),
                ),
                Artifact(
                    job_id=job.json()["id"],
                    attempt_id=second_attempt.id,
                    artifact_type="final_index",
                    filename="final.index",
                    storage_uri="s3://private-bucket/secret/index",
                    size_bytes=2048,
                    sha256="b" * 64,
                    mime_type="application/octet-stream",
                    metadata_json={"source": "attempt-two"},
                    created_at=now + timedelta(minutes=3),
                ),
            ]
        )
        await session.commit()
        return {
            "job_id": job.json()["id"],
            "first_attempt_id": first_attempt.id,
            "second_attempt_id": second_attempt.id,
            "now": now,
        }


async def test_log_read_rbac_cursor_tail_ranges_and_redaction(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    owner = await seed_user(app, "log-owner@example.test")
    await seed_user(app, "log-other@example.test")
    owner_headers = await login(client, owner.email)
    other_headers = await login(client, "log-other@example.test")
    seeded = await seed_job_data(app, client, owner_headers)
    url = f"/api/v1/jobs/{seeded['job_id']}/logs"

    first = await client.get(url, headers=owner_headers, params={"limit": 2})
    assert first.status_code == 200, first.text
    assert first.json()["total"] == 4
    assert first.json()["has_more"] is True
    assert [item["sequence"] for item in first.json()["items"]] == [0, 1]
    assert [item["attempt_number"] for item in first.json()["items"]] == [1, 1]
    assert first.json()["next_cursor"]
    assert first.headers["Cache-Control"] == "private, no-store"
    assert first.headers["Vary"] == "Authorization"

    second = await client.get(
        url,
        headers=owner_headers,
        params={"limit": 2, "after": first.json()["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    assert [(item["attempt_number"], item["sequence"]) for item in second.json()["items"]] == [
        (1, 2),
        (2, 0),
    ]
    assert second.json()["has_more"] is False
    serialized = second.text
    for secret in (
        "secret-signature",
        "eyJsecret.payload.signature",
        "hunter2",
        "worker-secret-token",
        "secret-api-key",
        "field-secret",
    ):
        assert secret not in serialized
    assert "[REDACTED]" in serialized
    assert second.json()["items"][0]["fields"]["nested"]["safe"] == "visible"

    tail = await client.get(url, headers=owner_headers, params={"tail": True, "limit": 2})
    assert [(item["attempt_number"], item["sequence"]) for item in tail.json()["items"]] == [
        (1, 2),
        (2, 0),
    ]
    sequence_range = await client.get(
        url,
        headers=owner_headers,
        params={"sequence_gte": 1, "sequence_lte": 2},
    )
    assert [item["sequence"] for item in sequence_range.json()["items"]] == [1, 2]
    time_range = await client.get(
        url,
        headers=owner_headers,
        params={
            "occurred_at_gte": (seeded["now"] + timedelta(seconds=20)).isoformat(),
            "occurred_at_lte": (seeded["now"] + timedelta(seconds=30)).isoformat(),
        },
    )
    assert [item["sequence"] for item in time_range.json()["items"]] == [1, 2]
    attempt_only = await client.get(
        url,
        headers=owner_headers,
        params={"attempt_id": seeded["second_attempt_id"]},
    )
    assert len(attempt_only.json()["items"]) == 1
    assert attempt_only.json()["items"][0]["attempt_number"] == 2

    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, headers=other_headers)).status_code == 404
    assert (await client.get(url, headers=admin_headers)).status_code == 200
    assert (
        await client.get(
            url,
            headers=owner_headers,
            params={"sequence_gte": 2, "sequence_lte": 1},
        )
    ).status_code == 422
    assert (
        await client.get(url, headers=owner_headers, params={"after": "not-a-cursor"})
    ).status_code == 422
    assert (
        await client.get(
            url,
            headers=owner_headers,
            params={"tail": True, "after": first.json()["next_cursor"]},
        )
    ).status_code == 422
    assert (
        await client.get(url, headers=owner_headers, params={"limit": 501})
    ).status_code == 422


async def test_metric_and_artifact_reads_filter_paginate_and_hide_storage_uri(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    owner = await seed_user(app, "telemetry-owner@example.test")
    await seed_user(app, "telemetry-other@example.test")
    owner_headers = await login(client, owner.email)
    other_headers = await login(client, "telemetry-other@example.test")
    seeded = await seed_job_data(app, client, owner_headers)
    metrics_url = f"/api/v1/jobs/{seeded['job_id']}/metrics"
    artifacts_url = f"/api/v1/jobs/{seeded['job_id']}/artifacts"

    metrics = await client.get(
        metrics_url,
        headers=owner_headers,
        params={"offset": 1, "limit": 2},
    )
    assert metrics.status_code == 200, metrics.text
    assert metrics.json()["total"] == 4
    assert [(item["attempt_number"], item["sequence"]) for item in metrics.json()["items"]] == [
        (1, 1),
        (1, 2),
    ]
    tail_metrics = await client.get(
        metrics_url,
        headers=owner_headers,
        params={"tail": True, "limit": 2},
    )
    assert tail_metrics.status_code == 200, tail_metrics.text
    assert tail_metrics.json()["total"] == 4
    assert tail_metrics.json()["offset"] == 2
    assert [
        (item["attempt_number"], item["sequence"])
        for item in tail_metrics.json()["items"]
    ] == [(1, 2), (2, 0)]
    filtered = await client.get(
        metrics_url,
        headers=owner_headers,
        params={"key": "train.loss", "epoch": 1, "step": 10},
    )
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["value"] == 1.2
    gpu_metrics = await client.get(
        metrics_url,
        headers=owner_headers,
        params={"key": "system.gpu.0.utilization_percent"},
    )
    assert gpu_metrics.status_code == 200, gpu_metrics.text
    assert gpu_metrics.json()["total"] == 1
    assert gpu_metrics.json()["items"][0]["value"] == 75.0
    attempt_metrics = await client.get(
        metrics_url,
        headers=owner_headers,
        params={"attempt_id": seeded["second_attempt_id"]},
    )
    assert {item["attempt_number"] for item in attempt_metrics.json()["items"]} == {2}
    assert (
        await client.get(metrics_url, headers=owner_headers, params={"key": "bad key"})
    ).status_code == 422
    assert (
        await client.get(metrics_url, headers=owner_headers, params={"limit": 501})
    ).status_code == 422
    assert (
        await client.get(
            metrics_url,
            headers=owner_headers,
            params={"tail": True, "offset": 1},
        )
    ).status_code == 422

    artifacts = await client.get(
        artifacts_url,
        headers=owner_headers,
        params={"artifact_type": "final_index"},
    )
    assert artifacts.status_code == 200, artifacts.text
    assert artifacts.json()["total"] == 1
    assert artifacts.json()["items"][0]["artifact_type"] == "final_index"
    assert "storage_uri" not in artifacts.text
    assert "private-bucket" not in artifacts.text
    assert artifacts.headers["Cache-Control"] == "private, no-store"
    assert (
        await client.get(
            artifacts_url,
            headers=owner_headers,
            params={"artifact_type": "not-an-artifact"},
        )
    ).status_code == 422
    assert (
        await client.get(artifacts_url, headers=owner_headers, params={"limit": 201})
    ).status_code == 422

    for url in (metrics_url, artifacts_url):
        assert (await client.get(url)).status_code == 401
        assert (await client.get(url, headers=other_headers)).status_code == 404
        assert (await client.get(url, headers=admin_headers)).status_code == 200


async def test_log_sse_is_bounded_resumable_redacted_and_not_cacheable(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del admin_headers
    owner = await seed_user(app, "stream-owner@example.test")
    await seed_user(app, "stream-other@example.test")
    owner_headers = await login(client, owner.email)
    other_headers = await login(client, "stream-other@example.test")
    seeded = await seed_job_data(app, client, owner_headers)
    url = f"/api/v1/jobs/{seeded['job_id']}/logs/stream"
    app.state.settings.log_stream_poll_interval_seconds = 0.005
    app.state.settings.log_stream_heartbeat_seconds = 0.01
    app.state.settings.log_stream_max_connection_seconds = 0.05
    app.state.settings.log_stream_batch_limit = 2

    closed_sessions: list[AsyncSession] = []
    original_close = AsyncSession.close
    original_fetch = observability_router.fetch_job_logs

    async def tracked_close(session: AsyncSession) -> None:
        await original_close(session)
        if not closed_sessions:
            # Keep the closed object alive so CPython cannot reuse its id for
            # the polling session and turn this identity assertion flaky.
            closed_sessions.append(session)

    async def fetch_after_initial_close(*args: Any, **kwargs: Any) -> Any:
        polling_session = args[0]
        assert closed_sessions
        assert all(polling_session is not closed for closed in closed_sessions)
        return await original_fetch(*args, **kwargs)

    monkeypatch.setattr(AsyncSession, "close", tracked_close)
    monkeypatch.setattr(observability_router, "fetch_job_logs", fetch_after_initial_close)

    started = monotonic()
    streamed = await client.get(url, headers=owner_headers)
    elapsed = monotonic() - started
    assert streamed.status_code == 200, streamed.text
    assert elapsed < 1
    assert streamed.text.count("event: log") == 4
    assert ": heartbeat" in streamed.text
    assert "secret-signature" not in streamed.text
    assert "hunter2" not in streamed.text
    assert "worker-secret-token" not in streamed.text
    assert "[REDACTED]" in streamed.text
    assert "no-store" in streamed.headers["Cache-Control"]
    assert streamed.headers["X-Accel-Buffering"] == "no"
    assert streamed.headers["Content-Type"].startswith("text/event-stream")
    event_ids = [
        line.removeprefix("id: ")
        for line in streamed.text.splitlines()
        if line.startswith("id: ")
    ]
    assert len(event_ids) == 4

    resumed = await client.get(
        url,
        headers={**owner_headers, "Last-Event-ID": event_ids[-1]},
    )
    assert resumed.status_code == 200
    assert "event: log" not in resumed.text
    assert ": heartbeat" in resumed.text
    assert (
        await client.get(
            url,
            headers={**owner_headers, "Last-Event-ID": event_ids[-1]},
            params={"after": event_ids[0]},
        )
    ).status_code == 422
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, headers=other_headers)).status_code == 404

    app.state.settings.log_stream_max_connection_seconds = 0.5
    revoked_started = monotonic()
    revoked_stream = asyncio.create_task(
        client.get(
            url,
            headers=owner_headers,
            params={"after": event_ids[-1]},
        )
    )
    await asyncio.sleep(0.03)
    logout = await client.post("/api/v1/auth/logout", headers=owner_headers)
    assert logout.status_code == 204
    revoked_response = await asyncio.wait_for(revoked_stream, timeout=1)
    assert revoked_response.status_code == 200
    assert monotonic() - revoked_started < 0.4
    assert (await client.get(url, headers=owner_headers)).status_code == 401
