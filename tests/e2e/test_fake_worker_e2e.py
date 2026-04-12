from __future__ import annotations

import asyncio
import hashlib
import io
import json
import socket
import sqlite3
import stat
import struct
import threading
import time
import wave
import zipfile
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import urlopen

import httpx
import pytest
import uvicorn
from pydantic import ValidationError

from rvc_manager_api.app import create_app
from rvc_manager_api.bootstrap import ensure_admin_user
from rvc_manager_api.config import Settings
from rvc_manager_api.database import Database
from rvc_orchestrator_contracts import (
    JobClaim,
    JobClaimRequest,
    JobConfig,
    JobStatus,
    JobStatusUpdate,
    LeaseRenewRequest,
    LeaseRenewResponse,
    LogBatch,
    MetricBatch,
    RVCVersion,
    TrainingF0Method,
    WorkerCapabilities,
    WorkerEngineMode,
    WorkerHeartbeatRequest,
    WorkerHeartbeatResponse,
    WorkerRegisterRequest,
    WorkerRegisterResponse,
)
from rvc_worker.agent import WorkerAgent
from rvc_worker.client import HttpManagerClient
from rvc_worker.credentials import CredentialStore
from rvc_worker.gpu import GpuCollection, GpuSnapshot
from rvc_worker.runner import FakeRvcRunner, RvcRunContext, StageResult
from rvc_worker.settings import WorkerSettings
from rvc_worker.stages import build_stage_plan
from rvc_worker.training_metrics import ParsedTrainingMetric
from rvc_worker.uploads import ArtifactUploadInitRequest, PublishedArtifact

BOOTSTRAP_TOKEN = "e2e-bootstrap-token"
TOKEN_PEPPER = "e2e-worker-token-pepper"
JWT_SECRET = "e2e-jwt-signing-secret-that-is-longer-than-32-characters"
ADMIN_EMAIL = "e2e-admin@example.test"
ADMIN_PASSWORD = "e2e-manager-password-strong"

pytestmark = pytest.mark.e2e


@dataclass(slots=True)
class RunningManager:
    base_url: str
    database_path: Path
    storage_root: Path
    server: uvicorn.Server
    thread: threading.Thread
    listener: socket.socket
    admin_headers: dict[str, str] = field(default_factory=dict)
    errors: list[BaseException] = field(default_factory=list)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            self.server.force_exit = True
            self.thread.join(timeout=5)
        self.listener.close()
        if self.thread.is_alive():
            raise RuntimeError("localhost Uvicorn test server did not stop")
        if self.errors:
            raise RuntimeError("localhost Uvicorn test server failed") from self.errors[0]


@pytest.fixture
def manager_server_factory(
    tmp_path: Path,
) -> Iterator[Callable[..., RunningManager]]:
    running: list[RunningManager] = []

    def start(*, allow_fake_workers: bool) -> RunningManager:
        database_path = tmp_path / f"manager-{len(running)}.sqlite3"
        storage_root = tmp_path / f"manager-{len(running)}-objects"
        settings = Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            worker_bootstrap_token=BOOTSTRAP_TOKEN,
            worker_token_pepper=TOKEN_PEPPER,
            jwt_secret=JWT_SECRET,
            lease_seconds=15,
            allow_fake_workers=allow_fake_workers,
            auto_create_schema=True,
            storage_backend="local",
            local_storage_root=storage_root,
            dataset_ingestion_root=tmp_path / f"manager-{len(running)}-dataset-ingestion",
        )
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        host, port = listener.getsockname()
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(settings),
                host=host,
                port=port,
                loop="asyncio",
                http="h11",
                lifespan="on",
                access_log=False,
                log_config=None,
            )
        )
        errors: list[BaseException] = []

        def run_server() -> None:
            try:
                server.run(sockets=[listener])
            except BaseException as exc:  # pragma: no cover - surfaced in fixture teardown
                errors.append(exc)

        thread = threading.Thread(
            target=run_server,
            name=f"e2e-manager-{port}",
            daemon=True,
        )
        instance = RunningManager(
            base_url=f"http://{host}:{port}",
            database_path=database_path,
            storage_root=storage_root,
            server=server,
            thread=thread,
            listener=listener,
            errors=errors,
        )
        running.append(instance)
        thread.start()

        deadline = time.monotonic() + 10
        while not server.started:
            if errors or not thread.is_alive():
                instance.stop()
                raise RuntimeError("localhost Uvicorn test server failed during startup")
            if time.monotonic() >= deadline:
                instance.stop()
                raise TimeoutError("localhost Uvicorn test server did not become ready")
            time.sleep(0.01)
        with urlopen(f"{instance.base_url}/health", timeout=2) as response:
            assert response.status == 200
        # The factory is called by async tests, so bootstrap on a dedicated
        # thread instead of nesting asyncio.run() in pytest's running loop.
        with ThreadPoolExecutor(max_workers=1) as executor:
            instance.admin_headers = executor.submit(
                lambda: asyncio.run(_bootstrap_and_login_admin(instance, settings))
            ).result(timeout=10)
        return instance

    yield start

    for instance in reversed(running):
        instance.stop()


@dataclass(frozen=True, slots=True)
class SeededJob:
    dataset_id: str
    experiment_id: str
    job_id: str
    prepared_flat_size_bytes: int
    prepared_flat_sha256: str
    config: JobConfig


class NoGpuCollector:
    def collect(self) -> GpuCollection:
        return GpuCollection((), False, "E2E intentionally has no GPU")


class VisibleGpuCollector:
    def collect(self) -> GpuCollection:
        return GpuCollection(
            (
                GpuSnapshot(
                    index=0,
                    uuid="GPU-e2e-visible-0",
                    name="E2E Visible GPU",
                    memory_total_mb=24_576,
                    memory_used_mb=4_096,
                    utilization_percent=62.5,
                    temperature_celsius=55.0,
                ),
            ),
            True,
        )


class LiveTelemetryFakeRunner(FakeRvcRunner):
    """Hold a Fake Job in TRAINING after publishing one native-shaped event."""

    def __init__(self) -> None:
        super().__init__(stage_delay_seconds=0.01)
        self.sink = None
        self.live_event_committed = asyncio.Event()
        self.release_training = asyncio.Event()

    def bind_training_telemetry(self, claim: JobClaim | None, sink: object | None) -> None:
        del claim
        self.sink = sink

    async def run_stage(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        if stage is JobStatus.TRAINING:
            assert self.sink is not None
            await self.sink.record_training_event(
                source="stdout",
                event_key="e2e-live-epoch-2",
                message=(
                    "INFO Train Epoch: 2 [50%] "
                    f"Authorization: Bearer e2e-live-secret {context.workspace.root}/train.log"
                ),
                metrics=(
                    ParsedTrainingMetric(
                        key="current_epoch",
                        value=2.0,
                        epoch=2,
                        source="stdout",
                    ),
                    ParsedTrainingMetric(
                        key="loss_g_total",
                        value=14.0,
                        epoch=2,
                        step=10,
                        source="stdout",
                    ),
                ),
                channel="stdout",
            )
            await self.sink.finish_training()
            self.live_event_committed.set()
            # The test body owns the bounded wait and always releases this gate
            # from ``finally``.  A shorter runner-local timeout made a slow CI
            # host fail the stage before the HTTP assertions could run.
            await self.release_training.wait()
        return await super().run_stage(stage, context, cancellation)


async def _bootstrap_and_login_admin(manager: RunningManager, settings: Settings) -> dict[str, str]:
    database = Database(settings)
    try:
        async with database.session_factory() as session:
            _, created = await ensure_admin_user(
                session,
                email=ADMIN_EMAIL,
                password=ADMIN_PASSWORD,
            )
            assert created is True
    finally:
        await database.dispose()

    async with httpx.AsyncClient(
        base_url=manager.base_url,
        timeout=3,
        trust_env=False,
    ) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class ProtocolProbeClient(HttpManagerClient):
    """Real HTTP client with observations around the public protocol calls."""

    def __init__(self, base_url: str, *, dataset_probe_path: Path | None = None) -> None:
        super().__init__(base_url, BOOTSTRAP_TOKEN, timeout_seconds=3)
        self.dataset_probe_path = dataset_probe_path
        self.downloaded_dataset: Path | None = None
        self.issued_token: str | None = None
        self.claims: list[JobClaim] = []
        self.successful_statuses: list[JobStatus] = []
        self.log_batches: list[LogBatch] = []
        self.metric_batches: list[MetricBatch] = []
        self.published_artifacts: list[PublishedArtifact] = []
        self.heartbeat_count = 0
        self.lease_renewals: list[LeaseRenewResponse] = []
        self.completion_gate_status: int | None = None
        self.completion_gate_detail: str | None = None

    async def register(self, request: WorkerRegisterRequest) -> WorkerRegisterResponse:
        response = await super().register(request)
        self.issued_token = response.worker_token
        return response

    async def heartbeat(self, request: WorkerHeartbeatRequest) -> WorkerHeartbeatResponse:
        response = await super().heartbeat(request)
        self.heartbeat_count += 1
        return response

    async def claim_job(self, request: JobClaimRequest) -> JobClaim | None:
        claim = await super().claim_job(request)
        if claim is not None:
            self.claims.append(claim)
            if self.dataset_probe_path is not None:
                self.downloaded_dataset = await super().download_dataset(
                    claim,
                    self.dataset_probe_path,
                )
        return claim

    async def renew_lease(self, job_id: str, request: LeaseRenewRequest) -> LeaseRenewResponse:
        response = await super().renew_lease(job_id, request)
        self.lease_renewals.append(response)
        return response

    async def update_status(self, job_id: str, update: JobStatusUpdate) -> None:
        await super().update_status(job_id, update)
        self.successful_statuses.append(update.status)

    async def send_logs(self, job_id: str, batch: LogBatch) -> None:
        await super().send_logs(job_id, batch)
        self.log_batches.append(batch)

    async def send_metrics(self, job_id: str, batch: MetricBatch) -> None:
        await super().send_metrics(job_id, batch)
        self.metric_batches.append(batch)

    async def publish_artifact(
        self,
        job_id: str,
        request: ArtifactUploadInitRequest,
        source: Path,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> PublishedArtifact:
        if self.completion_gate_status is None:
            assert self.issued_token is not None
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=3,
                trust_env=False,
            ) as client:
                premature = await client.post(
                    f"/api/v1/workers/jobs/{job_id}/status",
                    headers={"Authorization": f"Bearer {self.issued_token}"},
                    json={"lease_id": request.lease_id, "status": "completed"},
                )
            self.completion_gate_status = premature.status_code
            self.completion_gate_detail = premature.json().get("detail")
        artifact = await super().publish_artifact(
            job_id,
            request,
            source,
            cancellation=cancellation,
        )
        self.published_artifacts.append(artifact)
        return artifact


class ClaimStartBarrier:
    """Release a bounded group only after every Worker reaches its first claim."""

    def __init__(self, parties: int) -> None:
        if parties < 1:
            raise ValueError("claim barrier requires at least one party")
        self.parties = parties
        self.arrivals = 0
        self._lock = asyncio.Lock()
        self._released = asyncio.Event()

    async def wait(self, *, timeout_seconds: float) -> None:
        async with self._lock:
            self.arrivals += 1
            if self.arrivals > self.parties:
                raise RuntimeError("claim barrier received too many arrivals")
            if self.arrivals == self.parties:
                self._released.set()
        await asyncio.wait_for(self._released.wait(), timeout=timeout_seconds)


class ConcurrentProtocolProbeClient(ProtocolProbeClient):
    """Protocol probe whose first claim participates in a shared start barrier."""

    def __init__(
        self,
        base_url: str,
        *,
        claim_barrier: ClaimStartBarrier,
        dataset_probe_path: Path,
    ) -> None:
        super().__init__(base_url, dataset_probe_path=dataset_probe_path)
        self.claim_barrier = claim_barrier
        self._first_claim_started = False

    async def claim_job(self, request: JobClaimRequest) -> JobClaim | None:
        if not self._first_claim_started:
            self._first_claim_started = True
            await self.claim_barrier.wait(timeout_seconds=5)
        return await super().claim_job(request)


def _fake_capabilities() -> WorkerCapabilities:
    return WorkerCapabilities(
        engine_mode=WorkerEngineMode.FAKE,
        worker_version="0.1.0-e2e",
        rvc_commit_hash="fake-runner",
        supported_rvc_versions=[RVCVersion.V1, RVCVersion.V2],
        supported_training_f0_methods=list(TrainingF0Method),
        gpus=[],
        disk_free_bytes=1_000_000_000,
        tags=["e2e"],
        rvc_assets_ready=False,
        max_concurrent_jobs=1,
    )


async def _seed_training_jobs(
    manager: RunningManager,
    *,
    count: int,
) -> tuple[SeededJob, ...]:
    if count < 1:
        raise ValueError("at least one E2E Job is required")
    source = _pcm_wav_bytes([0, 1_000, -1_000, 0])
    async with httpx.AsyncClient(
        base_url=manager.base_url,
        timeout=3,
        trust_env=False,
        headers=manager.admin_headers,
    ) as client:
        initialized = await client.post(
            "/api/v1/datasets/uploads/init",
            json={
                "name": "e2e-speaker-dataset",
                "filename": "e2e-speaker.wav",
                "content_type": "audio/wav",
                "size_bytes": len(source),
                "sha256": hashlib.sha256(source).hexdigest(),
                "idempotency_key": "e2e-dataset-upload-0001",
            },
        )
        assert initialized.status_code == 201, initialized.text
        target = initialized.json()
        async with httpx.AsyncClient(timeout=3, trust_env=False) as upload_client:
            uploaded = await upload_client.put(
                target["upload_url"],
                headers=target["upload_headers"],
                content=source,
            )
        assert uploaded.status_code == 204, uploaded.text
        dataset_response = await client.post(
            f"/api/v1/datasets/uploads/{target['upload_session_id']}/finalize"
        )
        assert dataset_response.status_code == 200, dataset_response.text
        dataset = dataset_response.json()
        assert dataset["status"] == "ready"
        assert dataset["is_usable"] is True

        experiment_response = await client.post(
            "/api/v1/experiments",
            json={
                "name": "e2e-v2-comparison",
                "dataset_id": dataset["id"],
                "description": "localhost FakeRvcRunner E2E",
            },
        )
        assert experiment_response.status_code == 201, experiment_response.text
        experiment = experiment_response.json()

        seeded: list[SeededJob] = []
        comparison_f0_methods = ("pm", "harvest", "rmvpe")
        for index in range(count):
            job_name = "e2e-v2-full-flow" if count == 1 else f"e2e-v2-parallel-{index + 1:02d}"
            config = JobConfig.model_validate(
                {
                    "job_name": job_name,
                    "experiment_id": experiment["id"],
                    "dataset_id": dataset["id"],
                    "model": {
                        "version": "v2",
                        "sample_rate": "40k",
                        "use_f0": True,
                    },
                    "training": {
                        "epochs": 2,
                        "batch_size_per_gpu": 1,
                        "gpu_ids": [0],
                    },
                    "f0_extraction": {
                        "training_f0_method": (
                            "rmvpe"
                            if count == 1
                            else comparison_f0_methods[index % len(comparison_f0_methods)]
                        )
                    },
                    "index": {"build_index": True},
                    "auto_inference_samples": {"enabled": False},
                    "resource": {
                        "min_vram_gb": 24,
                        "preferred_worker_tags": ["e2e"],
                        "priority": 8,
                    },
                }
            )
            job_response = await client.post(
                "/api/v1/jobs",
                json=config.model_dump(mode="json"),
            )
            assert job_response.status_code == 201, job_response.text
            job = job_response.json()
            assert job["status"] == "queued"
            assert job["config"] == config.model_dump(mode="json")
            seeded.append(
                SeededJob(
                    dataset_id=dataset["id"],
                    experiment_id=experiment["id"],
                    job_id=job["id"],
                    prepared_flat_size_bytes=dataset["prepared_flat_size_bytes"],
                    prepared_flat_sha256=dataset["prepared_flat_sha256"],
                    config=config,
                )
            )

    return tuple(seeded)


async def _seed_training_job(manager: RunningManager) -> SeededJob:
    return (await _seed_training_jobs(manager, count=1))[0]


def _pcm_wav_bytes(samples: list[int], *, sample_rate: int = 8_000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, mode="wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return output.getvalue()


def _worker_settings(
    manager: RunningManager,
    data_root: Path,
    *,
    worker_name: str = "fake-worker-e2e",
) -> WorkerSettings:
    return WorkerSettings(
        manager_url=manager.base_url,
        worker_name=worker_name,
        worker_token=BOOTSTRAP_TOKEN,
        data_root=data_root,
        credential_path=data_root / "credentials" / "worker.json",
        runner_mode="fake",
        heartbeat_interval_seconds=0.01,
        poll_interval_seconds=0.01,
        lease_renew_interval_seconds=0.01,
        request_timeout_seconds=3,
        shutdown_grace_seconds=1,
        gpu_query_timeout_seconds=0.1,
        min_free_disk_bytes=0,
        worker_tags=("e2e",),
    )


def _read_ledger(database_path: Path, job_id: str) -> dict[str, object]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        status_events = [
            row["status"]
            for row in connection.execute(
                "SELECT status FROM job_status_events WHERE job_id = ? ORDER BY rowid",
                (job_id,),
            )
        ]
        logs = list(
            connection.execute(
                "SELECT sequence, message FROM job_logs WHERE job_id = ? ORDER BY sequence",
                (job_id,),
            )
        )
        metrics = list(
            connection.execute(
                "SELECT sequence, key, value FROM metrics WHERE job_id = ? ORDER BY sequence",
                (job_id,),
            )
        )
        artifacts = list(
            connection.execute(
                """
                SELECT artifact_type, storage_uri, size_bytes, sha256, metadata_json
                FROM artifacts
                WHERE job_id = ?
                ORDER BY artifact_type, filename
                """,
                (job_id,),
            )
        )
        attempt = connection.execute(
            """
            SELECT engine_mode, status, attempt_number
            FROM job_attempts
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        lease = connection.execute(
            """
            SELECT active, released_at
            FROM job_leases
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        worker = connection.execute("SELECT token_hash, current_job_id FROM workers").fetchone()
        worker_count = connection.execute("SELECT COUNT(*) FROM workers").fetchone()[0]
        completed_upload_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM artifact_upload_sessions
            WHERE job_id = ? AND status = 'completed'
            """,
            (job_id,),
        ).fetchone()[0]
    return {
        "status_events": status_events,
        "logs": logs,
        "metrics": metrics,
        "artifacts": artifacts,
        "attempt": attempt,
        "lease": lease,
        "worker": worker,
        "worker_count": worker_count,
        "completed_upload_count": completed_upload_count,
    }


def _read_parallel_ledger(database_path: Path) -> dict[str, list[sqlite3.Row]]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        jobs = list(
            connection.execute(
                """
                SELECT id, worker_id, status, current_attempt_id, attempt_count
                FROM jobs
                ORDER BY id
                """
            )
        )
        attempts = list(
            connection.execute(
                """
                SELECT id, job_id, worker_id, attempt_number, engine_mode, status,
                       started_at, finished_at
                FROM job_attempts
                ORDER BY job_id, attempt_number
                """
            )
        )
        leases = list(
            connection.execute(
                """
                SELECT id, job_id, attempt_id, worker_id, active, released_at
                FROM job_leases
                ORDER BY job_id, id
                """
            )
        )
        artifacts = list(
            connection.execute(
                """
                SELECT id, job_id, attempt_id, artifact_type, filename, storage_uri,
                       size_bytes, sha256, metadata_json
                FROM artifacts
                ORDER BY job_id, artifact_type, filename
                """
            )
        )
        uploads = list(
            connection.execute(
                """
                SELECT job_id, attempt_id, lease_id, worker_id, artifact_id, status
                FROM artifact_upload_sessions
                ORDER BY job_id, id
                """
            )
        )
        status_events = list(
            connection.execute(
                """
                SELECT job_id, attempt_id, status
                FROM job_status_events
                ORDER BY rowid
                """
            )
        )
        logs = list(
            connection.execute(
                """
                SELECT job_id, attempt_id, sequence
                FROM job_logs
                ORDER BY job_id, sequence
                """
            )
        )
        metrics = list(
            connection.execute(
                """
                SELECT job_id, attempt_id, sequence, key
                FROM metrics
                ORDER BY job_id, sequence
                """
            )
        )
        workers = list(
            connection.execute(
                """
                SELECT id, name, token_hash, current_job_id
                FROM workers
                ORDER BY name
                """
            )
        )
    return {
        "jobs": jobs,
        "attempts": attempts,
        "leases": leases,
        "artifacts": artifacts,
        "uploads": uploads,
        "status_events": status_events,
        "logs": logs,
        "metrics": metrics,
        "workers": workers,
    }


@pytest.mark.asyncio
async def test_fake_worker_requires_opt_in_and_production_rejects_it(
    manager_server_factory: Callable[..., RunningManager],
) -> None:
    with pytest.raises(ValidationError, match="ALLOW_FAKE_WORKERS"):
        Settings(
            environment="production",
            database_url="postgresql+asyncpg://manager:test@database/manager",
            worker_bootstrap_token=BOOTSTRAP_TOKEN,
            worker_token_pepper="explicit-production-pepper",
            jwt_secret="explicit-production-jwt-secret-longer-than-32-characters",
            allow_fake_workers=True,
        )

    manager = manager_server_factory(allow_fake_workers=False)
    seeded = await _seed_training_job(manager)
    worker_client = HttpManagerClient(
        manager.base_url,
        BOOTSTRAP_TOKEN,
        timeout_seconds=3,
    )
    registration = await worker_client.register(
        WorkerRegisterRequest(
            name="fake-worker-not-allowed",
            capabilities=_fake_capabilities(),
        )
    )
    assert registration.worker_token.startswith("rvcw_")
    claim = await worker_client.claim_job(
        JobClaimRequest(capabilities=_fake_capabilities(), max_wait_seconds=0)
    )
    assert claim is None

    async with httpx.AsyncClient(
        base_url=manager.base_url,
        timeout=3,
        trust_env=False,
        headers=manager.admin_headers,
    ) as client:
        job_response = await client.get(f"/api/v1/jobs/{seeded.job_id}")
    assert job_response.status_code == 200
    assert job_response.json()["status"] == "queued"
    assert job_response.json()["attempt_count"] == 0
    assert job_response.json()["current_attempt_engine_mode"] is None

    async with httpx.AsyncClient(
        base_url=manager.base_url,
        timeout=3,
        trust_env=False,
    ) as client:
        user_token_on_worker_api = await client.get(
            "/api/v1/workers/me",
            headers=manager.admin_headers,
        )
        worker_token_on_user_api = await client.get(
            f"/api/v1/jobs/{seeded.job_id}",
            headers={"Authorization": f"Bearer {registration.worker_token}"},
        )
    assert user_token_on_worker_api.status_code == 401
    assert worker_token_on_user_api.status_code == 401


@pytest.mark.asyncio
async def test_live_training_telemetry_is_visible_before_terminal_over_http(
    manager_server_factory: Callable[..., RunningManager],
    tmp_path: Path,
) -> None:
    manager = manager_server_factory(allow_fake_workers=True)
    seeded = await _seed_training_job(manager)
    data_root = tmp_path / "live-telemetry-worker"
    protocol = ProtocolProbeClient(manager.base_url)
    runner = LiveTelemetryFakeRunner()
    agent = WorkerAgent(
        _worker_settings(manager, data_root, worker_name="live-telemetry-e2e"),
        protocol,
        runner,
        gpu_collector=VisibleGpuCollector(),
    )

    execution = asyncio.create_task(agent.run(max_jobs=1))
    completed_jobs: int | None = None
    try:
        await asyncio.wait_for(runner.live_event_committed.wait(), timeout=10)
        async with httpx.AsyncClient(
            base_url=manager.base_url,
            timeout=3,
            trust_env=False,
            headers=manager.admin_headers,
        ) as client:
            (
                job_response,
                metric_response,
                loss_response,
                gpu_response,
                gpu_availability_response,
                system_response,
                log_response,
            ) = await asyncio.gather(
                client.get(f"/api/v1/jobs/{seeded.job_id}"),
                client.get(
                    f"/api/v1/jobs/{seeded.job_id}/metrics",
                    params={"key": "current_epoch", "tail": "true"},
                ),
                client.get(
                    f"/api/v1/jobs/{seeded.job_id}/metrics",
                    params={"key": "loss_g_total", "tail": "true"},
                ),
                client.get(
                    f"/api/v1/jobs/{seeded.job_id}/metrics",
                    params={
                        "key": "system.gpu.0.utilization_percent",
                        "tail": "true",
                    },
                ),
                client.get(
                    f"/api/v1/jobs/{seeded.job_id}/metrics",
                    params={"key": "system.gpu.telemetry_available", "tail": "true"},
                ),
                client.get(
                    f"/api/v1/jobs/{seeded.job_id}/metrics",
                    params={"key": "system.disk_free_bytes", "tail": "true"},
                ),
                client.get(
                    f"/api/v1/jobs/{seeded.job_id}/logs",
                    params={"tail": "true"},
                ),
            )
        assert job_response.status_code == 200, job_response.text
        assert job_response.json()["status"] == "training"
        assert job_response.json()["current_epoch"] == 2
        assert job_response.json()["current_attempt_engine_mode"] == "fake"
        assert metric_response.status_code == 200, metric_response.text
        assert [
            (item["key"], item["value"], item["epoch"], item["step"])
            for item in metric_response.json()["items"]
        ] == [("current_epoch", 2.0, 2, None)]
        assert loss_response.status_code == 200, loss_response.text
        assert [
            (item["key"], item["value"], item["epoch"], item["step"])
            for item in loss_response.json()["items"]
        ] == [("loss_g_total", 14.0, 2, 10)]
        assert gpu_response.status_code == 200, gpu_response.text
        assert [item["value"] for item in gpu_response.json()["items"]] == [62.5]
        assert gpu_availability_response.status_code == 200, gpu_availability_response.text
        assert [item["value"] for item in gpu_availability_response.json()["items"]] == [1.0]
        assert system_response.status_code == 200, system_response.text
        system_metrics = system_response.json()["items"]
        assert system_metrics
        assert all(item["key"] == "system.disk_free_bytes" for item in system_metrics)
        assert all(item["value"] > 0 for item in system_metrics)
        assert log_response.status_code == 200, log_response.text
        live_messages = "\n".join(item["message"] for item in log_response.json()["items"])
        assert "Train Epoch: 2" in live_messages
        assert "[REDACTED]" in live_messages
        assert "e2e-live-secret" not in live_messages
        assert str(data_root) not in live_messages
    finally:
        runner.release_training.set()
        completed_jobs = await asyncio.wait_for(execution, timeout=10)

    assert completed_jobs == 1
    assert list(agent.telemetry_spool.pending.iterdir()) == []
    assert list(agent.telemetry_spool.dead_letter.iterdir()) == []
    with sqlite3.connect(manager.database_path) as connection:
        stored = connection.execute(
            """
            SELECT telemetry_log_count, telemetry_metric_count
            FROM job_attempts
            WHERE job_id = ?
            """,
            (seeded.job_id,),
        ).fetchone()
        log_count = connection.execute(
            "SELECT COUNT(*) FROM job_logs WHERE job_id = ?",
            (seeded.job_id,),
        ).fetchone()[0]
        metric_count = connection.execute(
            "SELECT COUNT(*) FROM metrics WHERE job_id = ?",
            (seeded.job_id,),
        ).fetchone()[0]
    assert stored == (log_count, metric_count)


@pytest.mark.asyncio
async def test_localhost_manager_and_fake_worker_complete_full_protocol(
    manager_server_factory: Callable[..., RunningManager],
    tmp_path: Path,
) -> None:
    manager = manager_server_factory(allow_fake_workers=True)
    seeded = await _seed_training_job(manager)
    data_root = tmp_path / "worker-data"
    settings = _worker_settings(manager, data_root)
    credential_path = settings.credential_path
    assert credential_path is not None
    credential_store = CredentialStore(credential_path)
    protocol = ProtocolProbeClient(
        manager.base_url,
        dataset_probe_path=data_root / "protocol-probe" / "prepared_flat.zip",
    )
    agent = WorkerAgent(
        settings,
        protocol,
        FakeRvcRunner(stage_delay_seconds=0.01),
        gpu_collector=NoGpuCollector(),
        credential_store=credential_store,
    )

    completed_jobs = await asyncio.wait_for(agent.run(max_jobs=1), timeout=15)

    assert completed_jobs == 1
    assert len(protocol.claims) == 1
    claim = protocol.claims[0]
    assert claim.job_id == seeded.job_id
    assert claim.attempt_number == 1
    assert claim.dataset_transfer is not None
    assert claim.dataset_transfer.dataset_id == seeded.dataset_id
    assert claim.dataset_transfer.size_bytes == seeded.prepared_flat_size_bytes
    assert claim.dataset_transfer.sha256 == seeded.prepared_flat_sha256
    assert protocol.downloaded_dataset is not None
    assert protocol.downloaded_dataset.is_file()
    downloaded_dataset = protocol.downloaded_dataset.read_bytes()
    assert len(downloaded_dataset) == seeded.prepared_flat_size_bytes
    assert hashlib.sha256(downloaded_dataset).hexdigest() == seeded.prepared_flat_sha256
    with zipfile.ZipFile(io.BytesIO(downloaded_dataset)) as archive:
        assert archive.namelist() == ["prepared_flat/000001.wav"]
    assert claim.config == seeded.config
    expected_stages = list(build_stage_plan(claim))
    assert protocol.successful_statuses == [*expected_stages, JobStatus.COMPLETED]
    assert protocol.heartbeat_count > 0
    assert protocol.lease_renewals
    assert {renewal.lease_id for renewal in protocol.lease_renewals} == {claim.lease_id}

    assert protocol.completion_gate_status == 409
    assert protocol.completion_gate_detail == "required artifacts are not registered"
    assert len(protocol.log_batches) == len(expected_stages)
    protocol_metrics = [
        entry for batch in protocol.metric_batches for entry in batch.entries
    ]
    assert sum(entry.key == "worker.stage_completed" for entry in protocol_metrics) == len(
        expected_stages
    )
    assert {"system.gpu.count", "system.disk_free_bytes"}.issubset(
        {entry.key for entry in protocol_metrics}
    )
    assert protocol.published_artifacts

    stored = credential_store.load(
        manager_url=settings.manager_url,
        worker_name=settings.worker_name,
    )
    assert stored is not None
    assert protocol.issued_token is not None
    assert stored.worker_token == protocol.issued_token
    assert stat.S_IMODE(credential_path.stat().st_mode) == 0o600

    restarted_client = HttpManagerClient(
        manager.base_url,
        BOOTSTRAP_TOKEN,
        worker_token=stored.worker_token,
        timeout_seconds=3,
    )
    restarted_session = await restarted_client.get_session()
    assert restarted_session is not None
    assert restarted_session.worker_id == stored.worker_id
    assert restarted_session.name == settings.worker_name
    assert restarted_session.current_job_id is None

    async with httpx.AsyncClient(
        base_url=manager.base_url,
        timeout=3,
        trust_env=False,
        headers=manager.admin_headers,
    ) as client:
        job_response = await client.get(f"/api/v1/jobs/{seeded.job_id}")
    assert job_response.status_code == 200
    job = job_response.json()
    assert job["status"] == "completed"
    assert job["current_attempt_engine_mode"] == "fake"
    assert job["worker_id"] == stored.worker_id
    assert job["attempt_count"] == 1
    assert job["completed_at"] is not None

    ledger = _read_ledger(manager.database_path, seeded.job_id)
    assert ledger["status_events"] == [
        JobStatus.QUEUED.value,
        JobStatus.ASSIGNED.value,
        *(stage.value for stage in expected_stages),
        JobStatus.COMPLETED.value,
    ]
    assert len(ledger["logs"]) == len(expected_stages)
    assert sum(
        row["key"] == "worker.stage_completed" for row in ledger["metrics"]
    ) == len(expected_stages)
    assert {
        "worker.stage_completed",
        "system.gpu.count",
        "system.disk_free_bytes",
    }.issubset({row["key"] for row in ledger["metrics"]})

    artifacts = ledger["artifacts"]
    artifact_types = {row["artifact_type"] for row in artifacts}
    assert {
        "config",
        "dataset_report",
        "discriminator_checkpoint",
        "environment",
        "final_index",
        "final_small_model",
        "generator_checkpoint",
        "total_features",
        "train_log",
    }.issubset(artifact_types)
    assert all(row["storage_uri"].startswith("local:///") for row in artifacts)
    assert all(len(row["sha256"]) == 64 for row in artifacts)
    assert all(json.loads(row["metadata_json"])["runner_fake"] is True for row in artifacts)
    assert all(
        json.loads(row["metadata_json"])["manager_verification"]["algorithm"] == "sha256"
        for row in artifacts
    )
    assert ledger["completed_upload_count"] == len(artifacts)
    assert len(protocol.published_artifacts) == len(artifacts)
    for row in artifacts:
        object_key = row["storage_uri"].removeprefix("local:///")
        stored_object = manager.storage_root.joinpath(*object_key.split("/"))
        assert stored_object.is_file()
        assert stored_object.stat().st_size == row["size_bytes"]

    attempt = ledger["attempt"]
    assert attempt is not None
    assert tuple(attempt) == ("fake", "completed", 1)
    lease = ledger["lease"]
    assert lease is not None
    assert lease["active"] == 0
    assert lease["released_at"] is not None
    worker = ledger["worker"]
    assert worker is not None
    assert ledger["worker_count"] == 1
    assert worker["token_hash"] != stored.worker_token
    assert stored.worker_token not in worker["token_hash"]
    assert worker["current_job_id"] is None


@pytest.mark.asyncio
async def test_three_fake_workers_complete_isolated_jobs_concurrently_over_http(
    manager_server_factory: Callable[..., RunningManager],
    tmp_path: Path,
) -> None:
    manager = manager_server_factory(allow_fake_workers=True)
    seeded_jobs = await _seed_training_jobs(manager, count=3)
    expected_job_ids = {seeded.job_id for seeded in seeded_jobs}
    assert len(expected_job_ids) == 3
    assert len({seeded.dataset_id for seeded in seeded_jobs}) == 1
    assert len({seeded.experiment_id for seeded in seeded_jobs}) == 1

    claim_barrier = ClaimStartBarrier(parties=3)
    agents: list[WorkerAgent] = []
    protocols: list[ConcurrentProtocolProbeClient] = []
    stores: list[CredentialStore] = []
    settings_by_worker: list[WorkerSettings] = []
    for index in range(3):
        data_root = tmp_path / f"parallel-worker-{index + 1}"
        settings = _worker_settings(
            manager,
            data_root,
            worker_name=f"fake-worker-e2e-{index + 1}",
        )
        assert settings.credential_path is not None
        protocol = ConcurrentProtocolProbeClient(
            manager.base_url,
            claim_barrier=claim_barrier,
            dataset_probe_path=(data_root / "protocol-probe" / "prepared_flat.zip"),
        )
        store = CredentialStore(settings.credential_path)
        agents.append(
            WorkerAgent(
                settings,
                protocol,
                FakeRvcRunner(stage_delay_seconds=0.02),
                gpu_collector=NoGpuCollector(),
                credential_store=store,
            )
        )
        protocols.append(protocol)
        stores.append(store)
        settings_by_worker.append(settings)

    tasks = [
        asyncio.create_task(agent.run(max_jobs=1), name=f"parallel-e2e-worker-{index + 1}")
        for index, agent in enumerate(agents)
    ]
    try:
        completed = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)
    finally:
        for agent in agents:
            agent.request_shutdown()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert completed == [1, 1, 1]
    assert claim_barrier.arrivals == 3
    assert all(len(protocol.claims) == 1 for protocol in protocols)
    claims = [protocol.claims[0] for protocol in protocols]
    assert {claim.job_id for claim in claims} == expected_job_ids
    assert {
        claim.config.f0_extraction.training_f0_method.value for claim in claims
    } == {"pm", "harvest", "rmvpe"}
    assert len({claim.attempt_id for claim in claims}) == 3
    assert len({claim.lease_id for claim in claims}) == 3
    assert {claim.attempt_number for claim in claims} == {1}

    worker_id_by_job: dict[str, str] = {}
    attempt_id_by_job: dict[str, str] = {}
    lease_id_by_job: dict[str, str] = {}
    issued_tokens: set[str] = set()
    for settings, protocol, store, claim in zip(
        settings_by_worker,
        protocols,
        stores,
        claims,
        strict=True,
    ):
        stored = store.load(
            manager_url=settings.manager_url,
            worker_name=settings.worker_name,
        )
        assert stored is not None
        assert protocol.issued_token is not None
        assert stored.worker_token == protocol.issued_token
        assert settings.credential_path is not None
        assert stat.S_IMODE(settings.credential_path.stat().st_mode) == 0o600
        issued_tokens.add(stored.worker_token)
        worker_id_by_job[claim.job_id] = stored.worker_id
        attempt_id_by_job[claim.job_id] = claim.attempt_id
        lease_id_by_job[claim.job_id] = claim.lease_id

        assert protocol.downloaded_dataset is not None
        downloaded = protocol.downloaded_dataset.read_bytes()
        seeded = next(item for item in seeded_jobs if item.job_id == claim.job_id)
        assert len(downloaded) == seeded.prepared_flat_size_bytes
        assert hashlib.sha256(downloaded).hexdigest() == seeded.prepared_flat_sha256
        assert protocol.completion_gate_status == 409
        assert protocol.completion_gate_detail == "required artifacts are not registered"
        expected_stages = list(build_stage_plan(claim))
        assert protocol.successful_statuses == [*expected_stages, JobStatus.COMPLETED]
        assert len(protocol.log_batches) == len(expected_stages)
        protocol_metrics = [
            entry for batch in protocol.metric_batches for entry in batch.entries
        ]
        assert sum(entry.key == "worker.stage_completed" for entry in protocol_metrics) == len(
            expected_stages
        )
        assert {"system.gpu.count", "system.disk_free_bytes"}.issubset(
            {entry.key for entry in protocol_metrics}
        )
        assert protocol.published_artifacts
        assert protocol.heartbeat_count > 0
        assert protocol.lease_renewals
        assert {renewal.lease_id for renewal in protocol.lease_renewals} == {claim.lease_id}

    assert len(issued_tokens) == 3
    assert len(set(worker_id_by_job.values())) == 3

    async with httpx.AsyncClient(
        base_url=manager.base_url,
        timeout=3,
        trust_env=False,
        headers=manager.admin_headers,
    ) as client:
        job_responses = [
            await client.get(f"/api/v1/jobs/{seeded.job_id}") for seeded in seeded_jobs
        ]
    assert all(response.status_code == 200 for response in job_responses)
    for response in job_responses:
        job = response.json()
        assert job["status"] == JobStatus.COMPLETED.value
        assert job["current_attempt_engine_mode"] == "fake"
        assert job["attempt_count"] == 1
        assert job["completed_at"] is not None
        assert job["worker_id"] == worker_id_by_job[job["id"]]

    ledger = _read_parallel_ledger(manager.database_path)
    jobs = {row["id"]: row for row in ledger["jobs"]}
    attempts = {row["id"]: row for row in ledger["attempts"]}
    leases = {row["id"]: row for row in ledger["leases"]}
    artifacts = {row["id"]: row for row in ledger["artifacts"]}
    assert set(jobs) == expected_job_ids
    assert set(attempts) == set(attempt_id_by_job.values())
    assert set(leases) == set(lease_id_by_job.values())
    assert len(ledger["workers"]) == 3

    expected_artifact_types = {
        "config",
        "dataset_report",
        "discriminator_checkpoint",
        "environment",
        "final_index",
        "final_small_model",
        "generator_checkpoint",
        "total_features",
        "train_log",
    }
    for job_id in expected_job_ids:
        worker_id = worker_id_by_job[job_id]
        attempt_id = attempt_id_by_job[job_id]
        lease_id = lease_id_by_job[job_id]
        job = jobs[job_id]
        assert tuple(
            job[key] for key in ("worker_id", "status", "current_attempt_id", "attempt_count")
        ) == (worker_id, JobStatus.COMPLETED.value, attempt_id, 1)

        attempt = attempts[attempt_id]
        assert tuple(
            attempt[key]
            for key in (
                "job_id",
                "worker_id",
                "attempt_number",
                "engine_mode",
                "status",
            )
        ) == (job_id, worker_id, 1, WorkerEngineMode.FAKE.value, "completed")
        assert attempt["finished_at"] is not None

        lease = leases[lease_id]
        assert tuple(lease[key] for key in ("job_id", "attempt_id", "worker_id", "active")) == (
            job_id,
            attempt_id,
            worker_id,
            0,
        )
        assert lease["released_at"] is not None

        job_artifacts = [row for row in ledger["artifacts"] if row["job_id"] == job_id]
        assert expected_artifact_types.issubset({row["artifact_type"] for row in job_artifacts})
        assert all(row["attempt_id"] == attempt_id for row in job_artifacts)
        for row in job_artifacts:
            metadata = json.loads(row["metadata_json"])
            assert metadata["runner_fake"] is True
            assert metadata["manager_verification"]["algorithm"] == "sha256"
            object_key = row["storage_uri"].removeprefix("local:///")
            stored_object = manager.storage_root.joinpath(*object_key.split("/"))
            assert stored_object.is_file()
            content = stored_object.read_bytes()
            assert len(content) == row["size_bytes"]
            assert hashlib.sha256(content).hexdigest() == row["sha256"]

        job_uploads = [row for row in ledger["uploads"] if row["job_id"] == job_id]
        assert len(job_uploads) == len(job_artifacts)
        for upload in job_uploads:
            assert tuple(
                upload[key] for key in ("attempt_id", "lease_id", "worker_id", "status")
            ) == (attempt_id, lease_id, worker_id, "completed")
            assert upload["artifact_id"] in artifacts
            artifact = artifacts[upload["artifact_id"]]
            assert artifact["job_id"] == job_id
            assert artifact["attempt_id"] == attempt_id

        expected_stages = list(
            build_stage_plan(next(claim for claim in claims if claim.job_id == job_id))
        )
        status_events = [row for row in ledger["status_events"] if row["job_id"] == job_id]
        assert [row["status"] for row in status_events] == [
            JobStatus.QUEUED.value,
            JobStatus.ASSIGNED.value,
            *(stage.value for stage in expected_stages),
            JobStatus.COMPLETED.value,
        ]
        assert all(row["attempt_id"] in {None, attempt_id} for row in status_events)
        job_logs = [row for row in ledger["logs"] if row["job_id"] == job_id]
        job_metrics = [row for row in ledger["metrics"] if row["job_id"] == job_id]
        assert len(job_logs) == len(expected_stages)
        assert sum(row["key"] == "worker.stage_completed" for row in job_metrics) == len(
            expected_stages
        )
        assert all(row["attempt_id"] == attempt_id for row in job_logs)
        assert all(row["attempt_id"] == attempt_id for row in job_metrics)
        assert {
            "worker.stage_completed",
            "system.gpu.count",
            "system.disk_free_bytes",
        }.issubset({row["key"] for row in job_metrics})

    # All three leases were live at the same time, proving that this exercised
    # concurrent ownership rather than three serial Agent runs.
    assert max(row["started_at"] for row in ledger["attempts"]) < min(
        row["finished_at"] for row in ledger["attempts"]
    )
    workers_by_id = {row["id"]: row for row in ledger["workers"]}
    assert set(workers_by_id) == set(worker_id_by_job.values())
    for worker_id in worker_id_by_job.values():
        worker = workers_by_id[worker_id]
        assert worker["current_job_id"] is None
        assert all(token not in worker["token_hash"] for token in issued_tokens)
