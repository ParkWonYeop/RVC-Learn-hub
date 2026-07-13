from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select

from rvc_manager_api.config import Settings
from rvc_manager_api.models import Experiment, MlflowSyncEvent
from rvc_manager_api.services.mlflow import (
    ARTIFACT_VERIFIED,
    JOB_CREATED,
    JOB_TERMINAL,
    METRIC_BATCH,
    MlflowProjectionRequired,
    MlflowRestAdapter,
    MlflowUnavailable,
    ProjectionEvent,
)


class RecordingAdapter:
    def __init__(self, *, failing: bool = False) -> None:
        self.failing = failing
        self.events: list[ProjectionEvent] = []

    async def health(self) -> None:
        if self.failing:
            raise MlflowUnavailable("injected_failure")

    async def project(self, event: ProjectionEvent) -> None:
        self.events.append(event)
        if self.failing:
            raise MlflowUnavailable("injected_failure")

    async def close(self) -> None:
        return None


class SlowHealthAdapter(RecordingAdapter):
    async def health(self) -> None:
        await asyncio.sleep(1)


def _capabilities() -> dict[str, object]:
    return {
        "engine_mode": "rvc_webui",
        "worker_version": "0.1.0",
        "rvc_commit_hash": "0123456789abcdef",
        "supported_rvc_versions": ["v2"],
        "supported_training_f0_methods": ["rmvpe"],
        "gpus": [
            {
                "index": 0,
                "name": "MLflow Test GPU",
                "total_vram_mb": 24 * 1024,
                "free_vram_mb": 20 * 1024,
            }
        ],
        "disk_free_bytes": 500_000_000_000,
        "tags": ["nvidia"],
        "rvc_assets_ready": True,
    }


async def _create_dataset(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    suffix: str,
) -> str:
    response = await client.post(
        "/api/v1/datasets",
        headers=headers,
        json={
            "name": f"mlflow-dataset-{suffix}",
            "storage_uri": f"s3://private/raw/{suffix}.zip",
            "flat_storage_uri": f"s3://private/flat/{suffix}/",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _create_experiment(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    dataset_id: str,
    suffix: str,
) -> httpx.Response:
    return await client.post(
        "/api/v1/experiments",
        headers=headers,
        json={"name": f"mlflow-experiment-{suffix}", "dataset_id": dataset_id},
    )


async def _create_job(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    dataset_id: str,
    experiment_id: str,
    suffix: str,
) -> httpx.Response:
    return await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_name": f"mlflow-job-{suffix}",
            "experiment_id": experiment_id,
            "dataset_id": dataset_id,
            "model": {"version": "v2", "sample_rate": "40k", "use_f0": True},
            "pretrained": {
                "mode": "custom",
                "g_path": "private/G.pth",
                "d_path": "private/D.pth",
                "allow_custom_override": True,
            },
            "f0_extraction": {"training_f0_method": "rmvpe"},
            "resource": {"preferred_worker_tags": ["nvidia"]},
        },
    )


def _enable_mlflow(app: FastAPI, adapter: RecordingAdapter, *, fail_closed: bool) -> None:
    app.state.settings.mlflow_enabled = True
    app.state.settings.mlflow_fail_closed = fail_closed
    app.state.mlflow.adapter = adapter


async def test_api_projects_creation_and_metric_batch_without_sensitive_paths(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    adapter = RecordingAdapter()
    _enable_mlflow(app, adapter, fail_closed=False)
    dataset_id = await _create_dataset(client, admin_headers, suffix="metric")
    experiment = await _create_experiment(
        client,
        admin_headers,
        dataset_id=dataset_id,
        suffix="metric",
    )
    assert experiment.status_code == 201, experiment.text
    job = await _create_job(
        client,
        admin_headers,
        dataset_id=dataset_id,
        experiment_id=experiment.json()["id"],
        suffix="metric",
    )
    assert job.status_code == 201, job.text

    registration = await client.post(
        "/api/v1/workers/register",
        headers={"X-Worker-Bootstrap-Token": "test-bootstrap-token"},
        json={"name": "mlflow-gpu", "capabilities": _capabilities()},
    )
    assert registration.status_code == 201, registration.text
    worker_headers = {"Authorization": f"Bearer {registration.json()['worker_token']}"}
    claim = await client.post(
        "/api/v1/workers/jobs/claim",
        headers=worker_headers,
        json={"max_wait_seconds": 0},
    )
    assert claim.status_code == 200, claim.text
    metric_payload = {
        "lease_id": claim.json()["lease_id"],
        "attempt_id": claim.json()["attempt_id"],
        "idempotency_key": "metric-mlflow-0001",
        "entries": [{"sequence": 1, "key": "loss.g", "value": 0.42, "epoch": 1, "step": 10}],
    }
    metrics = await client.post(
        f"/api/v1/workers/jobs/{job.json()['id']}/metrics",
        headers=worker_headers,
        json=metric_payload,
    )
    assert metrics.status_code == 200, metrics.text
    duplicate = await client.post(
        f"/api/v1/workers/jobs/{job.json()['id']}/metrics",
        headers=worker_headers,
        json=metric_payload,
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    terminal = await client.post(
        f"/api/v1/workers/jobs/{job.json()['id']}/status",
        headers=worker_headers,
        json={
            "lease_id": claim.json()["lease_id"],
            "status": "failed",
            "error_message": "training failed",
            "telemetry_log_count": 0,
            "telemetry_metric_count": 3,
        },
    )
    assert terminal.status_code == 200, terminal.text
    late_metrics = await client.post(
        f"/api/v1/workers/jobs/{job.json()['id']}/metrics",
        headers=worker_headers,
        json={
            **metric_payload,
            "idempotency_key": "metric-mlflow-late-0002",
            "entries": [{"sequence": 2, "key": "loss.g", "value": 0.4, "epoch": 1, "step": 11}],
        },
    )
    assert late_metrics.status_code == 200, late_metrics.text

    async with app.state.database.session_factory() as session:
        events = list(
            (
                await session.scalars(select(MlflowSyncEvent).order_by(MlflowSyncEvent.created_at))
            ).all()
        )
    assert [event.event_type for event in events] == [
        "experiment.created",
        JOB_CREATED,
        METRIC_BATCH,
        JOB_TERMINAL,
        METRIC_BATCH,
    ]
    assert all(event.status == "synced" for event in events)
    assert all(
        event.payload_json["config_sha256"] == job.json()["config_sha256"] for event in events[1:]
    )
    job_payload = events[1].payload_json
    serialized = json.dumps(job_payload, sort_keys=True)
    assert "g_path" not in serialized
    assert "d_path" not in serialized
    assert "private/G.pth" not in serialized
    assert "storage_uri" not in serialized
    assert len([event for event in adapter.events if event.event_type == METRIC_BATCH]) == 2


@pytest.mark.parametrize(
    ("fail_closed", "expected_status", "ready_status"),
    [(False, 201, 200), (True, 503, 503)],
)
async def test_mlflow_failure_policy_preserves_ledger_and_outbox(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    fail_closed: bool,
    expected_status: int,
    ready_status: int,
) -> None:
    adapter = RecordingAdapter(failing=True)
    _enable_mlflow(app, adapter, fail_closed=fail_closed)
    dataset_id = await _create_dataset(
        client,
        admin_headers,
        suffix=f"failure-{fail_closed}",
    )
    response = await _create_experiment(
        client,
        admin_headers,
        dataset_id=dataset_id,
        suffix=f"failure-{fail_closed}",
    )
    assert response.status_code == expected_status, response.text
    if fail_closed:
        assert response.headers["Cache-Control"] == "no-store"
        assert int(response.headers["Retry-After"]) >= 1
        assert response.json()["detail"]["ledger_committed"] is True
        aggregate_id = response.json()["detail"]["resource_id"]
    else:
        aggregate_id = response.json()["id"]

    ready = await client.get("/ready")
    assert ready.status_code == ready_status
    assert ready.json()["checks"]["mlflow"] == "unavailable"
    async with app.state.database.session_factory() as session:
        assert await session.get(Experiment, aggregate_id) is not None
        event = await session.scalar(
            select(MlflowSyncEvent).where(MlflowSyncEvent.aggregate_id == aggregate_id)
        )
        assert event is not None
        assert event.status == "pending"
        assert event.attempt_count == 1
        assert event.last_error_code == "injected_failure"
        event_key = event.event_key

    adapter.failing = False
    await app.state.mlflow.sync_after_commit(event_key)
    async with app.state.database.session_factory() as session:
        recovered = await session.scalar(
            select(MlflowSyncEvent).where(MlflowSyncEvent.event_key == event_key)
        )
        assert recovered is not None
        assert recovered.status == "synced"
        assert recovered.attempt_count == 2


async def test_fail_closed_does_not_treat_another_projector_claim_as_synced(
    app: FastAPI,
) -> None:
    adapter = RecordingAdapter()
    _enable_mlflow(app, adapter, fail_closed=True)
    async with app.state.database.session_factory() as session:
        session.add(
            MlflowSyncEvent(
                event_key="experiment:claimed-elsewhere",
                event_type="experiment.created",
                aggregate_type="experiment",
                aggregate_id="claimed-elsewhere",
                payload_json={
                    "manager_experiment_id": "claimed-elsewhere",
                    "experiment_name": "claimed elsewhere",
                    "dataset_id": "dataset-elsewhere",
                },
                status="processing",
                attempt_count=0,
            )
        )
        await session.commit()

    with pytest.raises(MlflowProjectionRequired):
        await app.state.mlflow.sync_after_commit("experiment:claimed-elsewhere")
    assert adapter.events == []


async def test_fail_open_readiness_bounds_a_stalled_mlflow_probe(app: FastAPI) -> None:
    _enable_mlflow(app, SlowHealthAdapter(), fail_closed=False)
    app.state.settings.mlflow_readiness_timeout_seconds = 0.01
    status, ready = await app.state.mlflow.readiness()
    assert status == "unavailable"
    assert ready is True


def test_mlflow_settings_require_safe_uri_and_support_token_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(mlflow_enabled=True)
    with pytest.raises(ValidationError):
        Settings(
            mlflow_enabled=True,
            mlflow_tracking_uri="http://user:password@mlflow:5000?token=secret",
        )
    token_file = tmp_path / "mlflow-token"
    token_file.write_text("tracking-token-value\n", encoding="utf-8")
    settings = Settings(
        mlflow_enabled=True,
        mlflow_tracking_uri="http://mlflow:5000",
        mlflow_tracking_token_file=token_file,
    )
    assert settings.mlflow_tracking_token is not None
    assert settings.mlflow_tracking_token.get_secret_value() == "tracking-token-value"
    assert "tracking-token-value" not in repr(settings)


class FakeMlflowServer:
    def __init__(self) -> None:
        self.experiment_id: str | None = None
        self.run_id: str | None = None
        self.tags: dict[str, str] = {}
        self.log_batches: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.authorization_headers: list[str] = []

    def _json(self, request: httpx.Request) -> dict[str, Any]:
        if not request.content:
            return {}
        decoded = json.loads(request.content)
        assert isinstance(decoded, dict)
        return decoded

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("Authorization")
        if authorization:
            self.authorization_headers.append(authorization)
        path = request.url.path
        payload = self._json(request)
        if path.endswith("/experiments/search"):
            return httpx.Response(200, json={"experiments": []})
        if path.endswith("/experiments/get-by-name"):
            if self.experiment_id is None:
                return httpx.Response(404, json={"error_code": "RESOURCE_DOES_NOT_EXIST"})
            return httpx.Response(
                200,
                json={"experiment": {"experiment_id": self.experiment_id}},
            )
        if path.endswith("/experiments/create"):
            self.experiment_id = "10"
            return httpx.Response(200, json={"experiment_id": self.experiment_id})
        if path.endswith("/runs/search"):
            runs: list[dict[str, object]] = []
            if self.run_id is not None:
                runs = [{"info": {"run_id": self.run_id}}]
            return httpx.Response(200, json={"runs": runs})
        if path.endswith("/runs/create"):
            self.run_id = "run-001"
            for tag in payload.get("tags", []):
                self.tags[str(tag["key"])] = str(tag["value"])
            return httpx.Response(200, json={"run": {"info": {"run_id": self.run_id}}})
        if path.endswith("/runs/get"):
            return httpx.Response(
                200,
                json={
                    "run": {
                        "info": {"run_id": self.run_id},
                        "data": {
                            "tags": [
                                {"key": key, "value": value} for key, value in self.tags.items()
                            ]
                        },
                    }
                },
            )
        if path.endswith("/runs/log-batch"):
            self.log_batches.append(payload)
            for tag in payload.get("tags", []):
                self.tags[str(tag["key"])] = str(tag["value"])
            return httpx.Response(200, json={})
        if path.endswith("/runs/update"):
            self.updates.append(payload)
            return httpx.Response(200, json={"run_info": {"run_id": self.run_id}})
        if path.endswith("/runs/set-tag"):
            self.tags[str(payload["key"])] = str(payload["value"])
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error_code": "UNKNOWN_ENDPOINT"})


def _event(
    event_type: str,
    event_key: str,
    payload: dict[str, Any],
    *,
    aggregate_type: str = "job",
    aggregate_id: str = "job-001",
) -> ProjectionEvent:
    return ProjectionEvent(
        id=f"event-{event_key}",
        event_key=event_key,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload,
        attempt_count=0,
    )


async def test_rest_adapter_projects_all_event_types_and_skips_replayed_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://untrusted-proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted-proxy.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "http://untrusted-proxy.invalid:8080")
    server = FakeMlflowServer()
    settings = Settings(
        mlflow_enabled=True,
        mlflow_tracking_uri="http://mlflow.internal:5000",
        mlflow_tracking_token="super-secret-tracking-token",
    )
    adapter = MlflowRestAdapter(settings, transport=httpx.MockTransport(server))
    assert adapter._client._trust_env is False
    common: dict[str, Any] = {
        "manager_experiment_id": "exp-001",
        "experiment_name": "speaker comparison",
        "job_id": "job-001",
        "job_name": "job-one",
        "dataset_id": "dataset-001",
        "start_time_ms": 1_700_000_000_000,
        "params": {"model.version": "v2", "training.epochs": "80"},
    }
    job_event = _event(JOB_CREATED, "job:job-001", common)
    metric_event = _event(
        METRIC_BATCH,
        "metric:attempt-001:digest",
        {
            **common,
            "attempt_number": 1,
            "metrics": [{"key": "loss.g", "value": 0.5, "timestamp": 1_700_000_001_000, "step": 1}],
        },
    )
    artifact_event = _event(
        ARTIFACT_VERIFIED,
        "artifact:artifact-001",
        {
            **common,
            "artifact": {
                "id": "artifact-001",
                "type": "final_small_model",
                "filename": "voice.pth",
                "size_bytes": 1234,
                "sha256": "a" * 64,
                "mime_type": "application/octet-stream",
            },
        },
        aggregate_type="artifact",
        aggregate_id="artifact-001",
    )
    terminal_event = _event(
        JOB_TERMINAL,
        "terminal:attempt-001:completed",
        {**common, "status": "completed", "end_time_ms": 1_700_000_010_000},
    )

    await adapter.health()
    await adapter.project(job_event)
    await adapter.project(metric_event)
    metric_batch_count = len(server.log_batches)
    await adapter.project(metric_event)
    assert len(server.log_batches) == metric_batch_count
    await adapter.project(artifact_event)
    await adapter.project(terminal_event)
    await adapter.close()

    assert server.experiment_id == "10"
    assert server.run_id == "run-001"
    assert any(
        metric["key"] == "attempt_1.loss.g"
        for batch in server.log_batches
        for metric in batch.get("metrics", [])
    )
    assert any(
        tag["value"] == "/api/v1/artifacts/artifact-001/download"
        for batch in server.log_batches
        for tag in batch.get("tags", [])
    )
    assert server.updates[-1]["status"] == "FINISHED"
    assert server.authorization_headers
    assert all(
        header == "Bearer super-secret-tracking-token" for header in server.authorization_headers
    )
    serialized_batches = json.dumps(server.log_batches, sort_keys=True)
    assert "mlflow.internal" not in serialized_batches
    assert "super-secret-tracking-token" not in serialized_batches


async def test_rest_adapter_errors_do_not_expose_uri_token_or_response_body() -> None:
    async def failure(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer do-not-leak"
        return httpx.Response(500, text="password=server-side-secret")

    settings = Settings(
        mlflow_enabled=True,
        mlflow_tracking_uri="http://private-mlflow.internal:5000",
        mlflow_tracking_token="do-not-leak",
    )
    adapter = MlflowRestAdapter(settings, transport=httpx.MockTransport(failure))
    with pytest.raises(MlflowUnavailable) as raised:
        await adapter.health()
    rendered = repr(raised.value)
    assert "do-not-leak" not in rendered
    assert "private-mlflow" not in rendered
    assert "server-side-secret" not in rendered
    await adapter.close()
