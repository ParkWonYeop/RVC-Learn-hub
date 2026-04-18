from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from rvc_orchestrator_contracts import (
    JobClaimRequest,
    JobStatus,
    JobStatusUpdate,
    LeaseRenewResponse,
    WorkerHeartbeatResponse,
    WorkerRegisterResponse,
    WorkerSessionResponse,
    WorkerStatus,
    utc_now,
)
from rvc_worker.agent import (
    ActiveJob,
    WorkerAgent,
    required_claim_input_disk_bytes,
)
from rvc_worker.client import ManagerClientError
from rvc_worker.credentials import CredentialStore
from rvc_worker.gpu import GpuCollection, GpuSnapshot
from rvc_worker.runner import FakeRvcRunner, RvcRunContext, StageResult
from rvc_worker.rvc_commands import RVC_REVIEWED_COMMIT
from rvc_worker.settings import WorkerSettings
from rvc_worker.stages import StageExecutor, build_stage_plan
from rvc_worker.telemetry import (
    AttemptTelemetrySession,
    TelemetrySpool,
    TelemetrySpoolError,
)
from rvc_worker.training_metrics import ParsedTrainingMetric
from rvc_worker.workspace import WorkspaceManager

from .helpers import make_claim


class FakeGpuCollector:
    def collect(self) -> GpuCollection:
        return GpuCollection((), False, "test has no GPU")


class VisibleGpuCollector:
    def __init__(self, index: int, *, utilization_percent: float = 0) -> None:
        self.index = index
        self.utilization_percent = utilization_percent

    def collect(self) -> GpuCollection:
        return GpuCollection(
            (
                GpuSnapshot(
                    index=self.index,
                    uuid=f"GPU-{self.index}",
                    name="fixture GPU",
                    memory_total_mb=24_576,
                    memory_used_mb=1_024,
                    utilization_percent=self.utilization_percent,
                    temperature_celsius=30,
                ),
            ),
            True,
        )


class ZeroGpuCollector:
    def collect(self) -> GpuCollection:
        return GpuCollection((), True)


class MutableGpuCollector(VisibleGpuCollector):
    pass


class InvalidGpuCollector:
    def collect(self) -> GpuCollection:
        duplicate = GpuSnapshot(
            index=0,
            uuid="GPU-duplicate",
            name="invalid fixture GPU",
            memory_total_mb=24_576,
            memory_used_mb=1_024,
            utilization_percent=10,
            temperature_celsius=30,
        )
        return GpuCollection((duplicate, duplicate), True)


class FailSecondSystemSnapshotSpool(TelemetrySpool):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.system_snapshots = 0

    async def enqueue_metric(self, job_id, batch):
        if any(entry.key == "system.gpu.count" for entry in batch.entries):
            self.system_snapshots += 1
            if self.system_snapshots == 2:
                raise TelemetrySpoolError("injected periodic system telemetry failure")
        return await super().enqueue_metric(job_id, batch)


class GuardedNativeRunner:
    verified_commit_hash = RVC_REVIEWED_COMMIT
    assets_ready = True

    def __init__(self) -> None:
        self.claim_validations = 0

    def validate_claim(self, claim, available_gpu_ids) -> None:
        del claim, available_gpu_ids
        self.claim_validations += 1

    async def run_stage(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        del stage, context, cancellation
        raise AssertionError("invalid claim must fail before any native stage")


class UnreadyNativeRunner(GuardedNativeRunner):
    assets_ready = False


class FixedFreeWorkspaceManager(WorkspaceManager):
    def __init__(self, root: Path, free_bytes: int) -> None:
        super().__init__(root, min_free_bytes=0)
        self.free_bytes = free_bytes
        self.checks = 0
        self.prepare_calls = 0

    def check_disk(self) -> int:
        self.checks += 1
        return self.free_bytes

    def prepare(self, job_id: str, attempt_id: str):
        del job_id, attempt_id
        self.prepare_calls += 1
        raise AssertionError("claim disk admission must run before workspace preparation")


class SecretFailingRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run_stage(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        del stage, context, cancellation
        self.calls += 1
        raise RuntimeError(
            "Bearer bootstrap-secret at /private/jobs/attempt; argv=['python', '--token', 'secret']"
        )


class BindableFakeRunner(FakeRvcRunner):
    def __init__(self) -> None:
        super().__init__()
        self.bound_sink = None
        self.bind_calls: list[tuple[str | None, str | None]] = []

    def bind_training_telemetry(self, claim, sink) -> None:
        self.bind_calls.append(
            (
                claim.job_id if claim is not None else None,
                claim.attempt_id if claim is not None else None,
            )
        )
        self.bound_sink = sink

    async def run_stage(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        if stage is JobStatus.TRAINING:
            assert self.bound_sink is not None
            await self.bound_sink.record_training_event(
                source="stdout",
                event_key="fixture-live-epoch",
                message=(f"Authorization: Bearer worker-secret {context.workspace.root}/train.log"),
                metrics=(
                    ParsedTrainingMetric(
                        key="current_epoch",
                        value=2.0,
                        epoch=2,
                        source="stdout",
                    ),
                ),
                channel="stdout",
            )
            await self.bound_sink.finish_training()
        return await super().run_stage(stage, context, cancellation)


class FakeManager:
    def __init__(
        self,
        *,
        cancel_on_heartbeat: bool = False,
        existing_session: WorkerSessionResponse | None = None,
        artifact_failure_status: int | None = None,
        telemetry_failure_status: int | None = None,
        terminal_failure_status: int | None = None,
    ) -> None:
        self.claim = make_claim(samples=False)
        self.cancel_on_heartbeat = cancel_on_heartbeat
        self.existing_session = existing_session
        self.artifact_failure_status = artifact_failure_status
        self.telemetry_failure_status = telemetry_failure_status
        self.terminal_failure_status = terminal_failure_status
        self.registered = False
        self.register_request = None
        self.claimed = False
        self.renewals = 0
        self.statuses: list[JobStatus] = []
        self.status_updates = []
        self.log_batches = []
        self.metric_batches = []
        self.artifact_uploads = []
        self.artifact_attempts = 0
        self.telemetry_attempts = 0
        self.heartbeat_requests = []

    async def get_session(self):
        return self.existing_session

    async def register(self, request):
        self.registered = True
        self.register_request = request
        return WorkerRegisterResponse(
            worker_id="worker-1",
            worker_token="rvcw_" + "i" * 43,
        )

    async def heartbeat(self, request):
        self.heartbeat_requests.append(request)
        cancel = [self.claim.job_id] if self.cancel_on_heartbeat and request.current_job_id else []
        return WorkerHeartbeatResponse(cancel_job_ids=cancel)

    async def claim_job(self, request: JobClaimRequest):
        if self.claimed:
            return None
        self.claimed = True
        return self.claim

    async def renew_lease(self, job_id, request):
        self.renewals += 1
        self.claim.lease_expires_at += timedelta(minutes=5)
        return LeaseRenewResponse(
            lease_id=request.lease_id,
            lease_expires_at=self.claim.lease_expires_at,
        )

    async def update_status(self, job_id, update):
        self.status_updates.append(update)
        self.statuses.append(update.status)
        if update.status is JobStatus.FAILED and self.terminal_failure_status is not None:
            raise ManagerClientError(
                "terminal URL https://manager.example/private?token=secret-token",
                status_code=self.terminal_failure_status,
            )

    async def send_logs(self, job_id, batch):
        self.telemetry_attempts += 1
        if self.telemetry_failure_status is not None:
            raise ManagerClientError(
                "telemetry URL contains /private/path?token=secret",
                status_code=self.telemetry_failure_status,
            )
        self.log_batches.append(batch)

    async def send_metrics(self, job_id, batch):
        self.telemetry_attempts += 1
        if self.telemetry_failure_status is not None:
            raise ManagerClientError(
                "telemetry URL contains /private/path?token=secret",
                status_code=self.telemetry_failure_status,
            )
        self.metric_batches.append(batch)

    async def publish_artifact(self, job_id, request, source, *, cancellation=None):
        del cancellation
        self.artifact_attempts += 1
        if self.artifact_failure_status is not None:
            raise ManagerClientError(
                "artifact URL contains /private/path?token=secret",
                status_code=self.artifact_failure_status,
            )
        self.artifact_uploads.append((job_id, request, source.read_bytes()))


class InvalidRenewalManager(FakeManager):
    def __init__(self, expiry_kind: str) -> None:
        super().__init__()
        self.expiry_kind = expiry_kind

    async def renew_lease(self, job_id, request):
        del job_id
        self.renewals += 1
        if self.expiry_kind == "past":
            expires_at = utc_now() - timedelta(seconds=1)
        elif self.expiry_kind == "regressive":
            expires_at = self.claim.lease_expires_at - timedelta(seconds=1)
        else:
            expires_at = self.claim.lease_expires_at
        return LeaseRenewResponse(
            lease_id=request.lease_id,
            lease_expires_at=expires_at,
        )


class HangingRenewalManager(FakeManager):
    def __init__(self) -> None:
        super().__init__()
        self.renewal_started = asyncio.Event()

    async def renew_lease(self, job_id, request):
        del job_id, request
        self.renewals += 1
        self.renewal_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class InvalidHeartbeatExpiryManager(FakeManager):
    def __init__(self, expiry_kind: str) -> None:
        super().__init__()
        self.expiry_kind = expiry_kind

    async def heartbeat(self, request):
        if request.current_job_id is None:
            return WorkerHeartbeatResponse()
        expires_at = (
            utc_now() - timedelta(seconds=1)
            if self.expiry_kind == "past"
            else self.claim.lease_expires_at - timedelta(seconds=1)
        )
        return WorkerHeartbeatResponse(lease_expires_at=expires_at)


class ConcurrentSameExpiryManager(FakeManager):
    def __init__(self) -> None:
        super().__init__()
        self.renewal_started = asyncio.Event()
        self.release_response = asyncio.Event()
        self.candidate = self.claim.lease_expires_at + timedelta(minutes=5)

    async def renew_lease(self, job_id, request):
        del job_id
        self.renewals += 1
        self.renewal_started.set()
        await self.release_response.wait()
        return LeaseRenewResponse(
            lease_id=request.lease_id,
            lease_expires_at=self.candidate,
        )


class StageFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_fake_runner_builds_full_vertical_artifacts(self) -> None:
        with TemporaryDirectory() as temporary:
            claim = make_claim(samples=True)
            workspace = WorkspaceManager(Path(temporary)).prepare(claim.job_id, claim.attempt_id)
            statuses = []

            async def update(job_id, status):
                statuses.append(status.status)

            summary = await StageExecutor(FakeRvcRunner(), update).execute(
                claim, workspace, asyncio.Event()
            )
            self.assertEqual(summary.final_status, JobStatus.COMPLETED)
            self.assertEqual(statuses[-1], JobStatus.COMPLETED)
            self.assertTrue((workspace.outputs / "model/final_small_model.pth").is_file())
            self.assertTrue((workspace.outputs / "index/final.index").is_file())
            self.assertTrue((workspace.outputs / "samples/fixed_test_converted.wav").is_file())
            self.assertTrue((workspace.outputs / "artifact_manifest.json").is_file())

    def test_optional_stages_are_omitted(self) -> None:
        plan = build_stage_plan(make_claim(use_f0=False, build_index=False, samples=False))
        self.assertNotIn(JobStatus.EXTRACTING_F0, plan)
        self.assertNotIn(JobStatus.BUILDING_INDEX, plan)
        self.assertNotIn(JobStatus.GENERATING_SAMPLES, plan)


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_registers_renews_and_completes(self) -> None:
        with TemporaryDirectory() as temporary:
            manager = FakeManager()
            settings = _settings(Path(temporary))
            agent = WorkerAgent(
                settings,
                manager,
                FakeRvcRunner(stage_delay_seconds=0.01),
                gpu_collector=FakeGpuCollector(),
                credential_store=CredentialStore(Path(temporary) / "credential.json"),
            )
            completed = await asyncio.wait_for(agent.run(max_jobs=1), timeout=5)
            self.assertEqual(completed, 1)
            self.assertTrue(manager.registered)
            self.assertEqual(manager.register_request.capabilities.engine_mode, "fake")
            self.assertFalse(manager.register_request.capabilities.fixed_test_set_inference_ready)
            self.assertEqual(
                manager.register_request.capabilities.supported_inference_f0_methods,
                [],
            )
            stored = CredentialStore(Path(temporary) / "credential.json").load(
                manager_url=settings.manager_url, worker_name=settings.worker_name
            )
            self.assertEqual(stored.worker_token, "rvcw_" + "i" * 43)
            self.assertGreater(manager.renewals, 0)
            self.assertEqual(manager.statuses[-1], JobStatus.COMPLETED)
            self.assertTrue(manager.log_batches)
            self.assertTrue(manager.metric_batches)
            terminal = manager.status_updates[-1]
            self.assertEqual(
                terminal.telemetry_log_count,
                sum(len(batch.entries) for batch in manager.log_batches),
            )
            self.assertEqual(
                terminal.telemetry_metric_count,
                sum(len(batch.entries) for batch in manager.metric_batches),
            )
            artifact_types = {request.artifact_type for _, request, _ in manager.artifact_uploads}
            self.assertIn("final_small_model", artifact_types)
            self.assertIn("final_index", artifact_types)

    async def test_existing_bearer_session_skips_duplicate_registration(self) -> None:
        with TemporaryDirectory() as temporary:
            manager = FakeManager(
                existing_session=WorkerSessionResponse(
                    worker_id="worker-1",
                    name="gpu-01",
                    status=WorkerStatus.IDLE,
                )
            )
            agent = WorkerAgent(
                _settings(Path(temporary)),
                manager,
                FakeRvcRunner(),
                gpu_collector=FakeGpuCollector(),
            )
            await asyncio.wait_for(agent.run(max_jobs=1), timeout=5)
            self.assertFalse(manager.registered)

    async def test_agent_records_job_bound_gpu_and_disk_time_series(self) -> None:
        with TemporaryDirectory() as temporary:
            manager = FakeManager()
            agent = WorkerAgent(
                _settings(Path(temporary)),
                manager,
                FakeRvcRunner(stage_delay_seconds=0.02),
                gpu_collector=VisibleGpuCollector(0),
            )

            await asyncio.wait_for(agent.run(max_jobs=1), timeout=5)

            entries = [entry for batch in manager.metric_batches for entry in batch.entries]
            keys = {entry.key for entry in entries}
            self.assertTrue(
                {
                    "system.gpu.count",
                    "system.gpu.telemetry_available",
                    "system.disk_free_bytes",
                    "system.gpu.0.vram_used_mb",
                    "system.gpu.0.vram_total_mb",
                    "system.gpu.0.utilization_percent",
                    "system.gpu.0.temperature_c",
                }.issubset(keys)
            )
            self.assertTrue(
                all(
                    entry.value == 1.0
                    for entry in entries
                    if entry.key == "system.gpu.count"
                )
            )
            self.assertTrue(
                all(
                    entry.value == 1_024.0
                    for entry in entries
                    if entry.key == "system.gpu.0.vram_used_mb"
                )
            )
            self.assertEqual(
                sorted(entry.sequence for entry in entries),
                list(range(len(entries))),
            )
            self.assertEqual(
                sum(entry.key == "system.gpu.count" for entry in entries),
                1,
            )
            self.assertGreater(
                sum(request.current_job_id is not None for request in manager.heartbeat_requests),
                1,
            )
            terminal = manager.status_updates[-1]
            self.assertEqual(terminal.status, JobStatus.COMPLETED)
            self.assertEqual(terminal.telemetry_metric_count, len(entries))

    async def test_initial_system_snapshot_uses_fresh_post_claim_observation(self) -> None:
        with TemporaryDirectory() as temporary:
            manager = FakeManager()
            collector = MutableGpuCollector(0, utilization_percent=5)
            agent = WorkerAgent(
                _settings(Path(temporary)),
                manager,
                FakeRvcRunner(),
                gpu_collector=collector,
            )
            claim_capabilities = await agent._capabilities()  # noqa: SLF001
            collector.utilization_percent = 73

            await agent._execute_claim(  # noqa: SLF001
                manager.claim,
                claim_capabilities=claim_capabilities,
            )

            utilization = [
                entry.value
                for batch in manager.metric_batches
                for entry in batch.entries
                if entry.key == "system.gpu.0.utilization_percent"
            ]
            self.assertEqual(utilization, [73.0])

    async def test_system_snapshot_distinguishes_zero_gpu_from_query_failure(self) -> None:
        observed: dict[str, tuple[float, float]] = {}
        for label, collector in (
            ("zero", ZeroGpuCollector()),
            ("unavailable", FakeGpuCollector()),
        ):
            with self.subTest(label=label), TemporaryDirectory() as temporary:
                manager = FakeManager()
                agent = WorkerAgent(
                    _settings(Path(temporary)),
                    manager,
                    FakeRvcRunner(),
                    gpu_collector=collector,
                )

                await agent._execute_claim(manager.claim)  # noqa: SLF001

                metrics = {
                    entry.key: entry.value
                    for batch in manager.metric_batches
                    for entry in batch.entries
                    if entry.key.startswith("system.gpu.")
                }
                observed[label] = (
                    metrics["system.gpu.count"],
                    metrics["system.gpu.telemetry_available"],
                )
        self.assertEqual(observed["zero"], (0.0, 1.0))
        self.assertEqual(observed["unavailable"], (0.0, 0.0))

    async def test_invalid_gpu_observation_does_not_terminate_heartbeat_supervisor(self) -> None:
        with TemporaryDirectory() as temporary:
            manager = FakeManager()
            agent = WorkerAgent(
                _settings(Path(temporary)),
                manager,
                FakeRvcRunner(),
                gpu_collector=InvalidGpuCollector(),
            )
            task = asyncio.create_task(agent._heartbeat_loop())  # noqa: SLF001
            try:
                for _ in range(100):
                    if len(manager.heartbeat_requests) >= 2:
                        break
                    await asyncio.sleep(0.005)
                self.assertGreaterEqual(len(manager.heartbeat_requests), 2)
                self.assertTrue(
                    all(not request.capabilities.gpus for request in manager.heartbeat_requests)
                )
            finally:
                agent.shutdown_requested.set()
                await asyncio.wait_for(task, timeout=1)

    async def test_periodic_system_spool_failure_is_typed_failed_not_cancelled(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = FakeManager()
            spool = FailSecondSystemSnapshotSpool(root / "telemetry-spool")
            agent = WorkerAgent(
                _settings(root, system_telemetry_interval_seconds=0.01),
                manager,
                FakeRvcRunner(stage_delay_seconds=0.1),
                gpu_collector=VisibleGpuCollector(0),
                telemetry_spool=spool,
            )

            await asyncio.wait_for(agent.run(max_jobs=1), timeout=5)

            terminal = manager.status_updates[-1]
            self.assertEqual(spool.system_snapshots, 2)
            self.assertEqual(terminal.status, JobStatus.FAILED)
            self.assertEqual(terminal.error_code, "telemetry_persistence_failed")
            self.assertEqual(
                terminal.error_message,
                "Worker stage downloading_dataset could not persist required telemetry.",
            )
            self.assertNotIn(JobStatus.CANCELLED, manager.statuses)

    async def test_live_training_telemetry_shares_sequences_and_unbinds_after_claim(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            manager = FakeManager()
            runner = BindableFakeRunner()
            agent = WorkerAgent(
                _settings(Path(temporary)),
                manager,
                runner,
                gpu_collector=FakeGpuCollector(),
            )

            await asyncio.wait_for(agent.run(max_jobs=1), timeout=5)

            log_entries = [entry for batch in manager.log_batches for entry in batch.entries]
            metric_entries = [entry for batch in manager.metric_batches for entry in batch.entries]
            self.assertEqual(
                sorted(entry.sequence for entry in log_entries),
                list(range(len(log_entries))),
            )
            self.assertEqual(
                sorted(entry.sequence for entry in metric_entries),
                list(range(len(metric_entries))),
            )
            self.assertTrue(
                any(entry.key == "current_epoch" and entry.epoch == 2 for entry in metric_entries)
            )
            self.assertTrue(any(update.current_epoch == 2 for update in manager.status_updates))
            rendered_logs = "\n".join(entry.message for entry in log_entries)
            self.assertNotIn("worker-secret", rendered_logs)
            self.assertNotIn(str(Path(temporary)), rendered_logs)
            self.assertIsNone(runner.bound_sink)
            self.assertEqual(runner.bind_calls[-1], (None, None))
            terminal = manager.status_updates[-1]
            self.assertEqual(terminal.status, JobStatus.COMPLETED)
            self.assertEqual(terminal.telemetry_log_count, len(log_entries))
            self.assertEqual(terminal.telemetry_metric_count, len(metric_entries))

    async def test_restart_with_active_assignment_waits_for_manager_recovery(self) -> None:
        with TemporaryDirectory() as temporary:
            manager = FakeManager(
                existing_session=WorkerSessionResponse(
                    worker_id="worker-1",
                    name="gpu-01",
                    status=WorkerStatus.BUSY,
                    current_job_id="abandoned-job",
                )
            )
            agent = WorkerAgent(
                _settings(Path(temporary)),
                manager,
                FakeRvcRunner(),
                gpu_collector=FakeGpuCollector(),
            )
            await agent._establish_session()
            self.assertEqual(agent.worker_id, "worker-1")
            self.assertFalse(manager.registered)

    async def test_manager_cancellation_reaches_stage_executor(self) -> None:
        with TemporaryDirectory() as temporary:
            manager = FakeManager(cancel_on_heartbeat=True)
            settings = _settings(Path(temporary))
            agent = WorkerAgent(
                settings,
                manager,
                FakeRvcRunner(stage_delay_seconds=0.05),
                gpu_collector=FakeGpuCollector(),
            )
            await asyncio.wait_for(agent.run(max_jobs=1), timeout=5)
            self.assertEqual(manager.statuses[-1], JobStatus.CANCELLED)
            terminal = manager.status_updates[-1]
            self.assertIsNotNone(terminal.telemetry_log_count)
            self.assertIsNotNone(terminal.telemetry_metric_count)

    async def test_initially_expired_lease_is_rejected_before_workspace(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = FakeManager()
            manager.claim.lease_expires_at = utc_now() - timedelta(seconds=1)
            runner = SecretFailingRunner()
            agent = WorkerAgent(
                _settings(root),
                manager,
                runner,
                gpu_collector=FakeGpuCollector(),
            )

            await agent._execute_claim(manager.claim)

            self.assertEqual(manager.statuses[-1], JobStatus.CANCELLED)
            self.assertEqual(manager.status_updates[-1].telemetry_log_count, 0)
            self.assertEqual(manager.status_updates[-1].telemetry_metric_count, 0)
            self.assertEqual(manager.renewals, 0)
            self.assertEqual(runner.calls, 0)
            self.assertFalse((root / "jobs").exists())

    async def test_hanging_renewal_is_cancelled_at_hard_lease_deadline(self) -> None:
        with TemporaryDirectory() as temporary:
            manager = HangingRenewalManager()
            manager.claim.lease_expires_at = utc_now() + timedelta(seconds=0.08)
            agent = WorkerAgent(
                _settings(Path(temporary)),
                manager,
                FakeRvcRunner(stage_delay_seconds=0.5),
                gpu_collector=FakeGpuCollector(),
            )

            await asyncio.wait_for(agent._execute_claim(manager.claim), timeout=1)

            self.assertTrue(manager.renewal_started.is_set())
            self.assertEqual(manager.renewals, 1)
            self.assertEqual(manager.statuses[-1], JobStatus.CANCELLED)

    async def test_non_increasing_or_expired_renewal_cancels_without_busy_loop(self) -> None:
        for expiry_kind in ("past", "regressive", "equal"):
            with self.subTest(expiry_kind=expiry_kind), TemporaryDirectory() as temporary:
                manager = InvalidRenewalManager(expiry_kind)
                agent = WorkerAgent(
                    _settings(Path(temporary)),
                    manager,
                    FakeRvcRunner(stage_delay_seconds=0.5),
                    gpu_collector=FakeGpuCollector(),
                )

                await asyncio.wait_for(agent._execute_claim(manager.claim), timeout=1)

                self.assertEqual(manager.renewals, 1)
                self.assertEqual(manager.statuses[-1], JobStatus.CANCELLED)

    async def test_renewal_accepts_expiry_already_applied_by_concurrent_heartbeat(self) -> None:
        with TemporaryDirectory() as temporary:
            manager = ConcurrentSameExpiryManager()
            settings = _settings(Path(temporary))
            initial_expiry = manager.claim.lease_expires_at
            active = ActiveJob(
                claim=manager.claim,
                cancellation=asyncio.Event(),
                lease_expires_at=initial_expiry,
                lease_deadline=(
                    asyncio.get_running_loop().time() + (initial_expiry - utc_now()).total_seconds()
                ),
                lease_changed=asyncio.Event(),
            )
            agent = WorkerAgent(
                settings,
                manager,
                FakeRvcRunner(),
                gpu_collector=FakeGpuCollector(),
            )
            finished = asyncio.Event()
            renewal = asyncio.create_task(agent._lease_loop(active, finished))

            await asyncio.wait_for(manager.renewal_started.wait(), timeout=1)
            self.assertTrue(
                active.accept_lease_expiry(
                    manager.candidate,
                    source="Manager heartbeat",
                )
            )
            manager.release_response.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            finished.set()
            active.lease_changed.set()
            await asyncio.wait_for(renewal, timeout=1)

            self.assertEqual(manager.renewals, 1)
            self.assertFalse(active.cancellation.is_set())
            self.assertEqual(active.lease_expires_at, manager.candidate)

    async def test_delayed_success_response_keeps_newer_concurrent_lease_expiry(self) -> None:
        manager = FakeManager()
        initial_expiry = manager.claim.lease_expires_at
        active = ActiveJob(
            claim=manager.claim,
            cancellation=asyncio.Event(),
            lease_expires_at=initial_expiry,
            lease_deadline=(
                asyncio.get_running_loop().time() + (initial_expiry - utc_now()).total_seconds()
            ),
            lease_changed=asyncio.Event(),
        )
        newer_expiry = initial_expiry + timedelta(minutes=10)
        delayed_expiry = initial_expiry + timedelta(minutes=5)

        self.assertTrue(active.accept_lease_expiry(newer_expiry, source="Manager heartbeat"))
        self.assertTrue(
            active.accept_lease_expiry(
                delayed_expiry,
                source="Manager renewal",
                request_baseline=initial_expiry,
            )
        )

        self.assertFalse(active.cancellation.is_set())
        self.assertEqual(active.lease_expires_at, newer_expiry)

    async def test_request_scoped_lease_response_requires_a_live_extended_baseline(self) -> None:
        manager = FakeManager()
        initial_expiry = manager.claim.lease_expires_at
        unchanged = ActiveJob(
            claim=manager.claim,
            cancellation=asyncio.Event(),
            lease_expires_at=initial_expiry,
            lease_deadline=(
                asyncio.get_running_loop().time() + (initial_expiry - utc_now()).total_seconds()
            ),
            lease_changed=asyncio.Event(),
        )
        self.assertFalse(
            unchanged.accept_lease_expiry(
                initial_expiry,
                source="Manager heartbeat",
                request_baseline=initial_expiry,
            )
        )
        self.assertTrue(unchanged.cancellation.is_set())

        expired_current = utc_now() - timedelta(seconds=1)
        expired = ActiveJob(
            claim=manager.claim,
            cancellation=asyncio.Event(),
            lease_expires_at=expired_current,
            lease_deadline=asyncio.get_running_loop().time() - 1,
            lease_changed=asyncio.Event(),
        )
        self.assertFalse(
            expired.accept_lease_expiry(
                expired_current - timedelta(milliseconds=100),
                source="Manager renewal",
                request_baseline=expired_current - timedelta(seconds=1),
            )
        )
        self.assertTrue(expired.cancellation.is_set())

    async def test_past_or_regressive_heartbeat_expiry_cancels_active_job(self) -> None:
        for expiry_kind in ("past", "regressive"):
            with self.subTest(expiry_kind=expiry_kind), TemporaryDirectory() as temporary:
                manager = InvalidHeartbeatExpiryManager(expiry_kind)
                agent = WorkerAgent(
                    _settings(
                        Path(temporary),
                        lease_renew_interval_seconds=1,
                    ),
                    manager,
                    FakeRvcRunner(stage_delay_seconds=0.5),
                    gpu_collector=FakeGpuCollector(),
                )

                await asyncio.wait_for(agent.run(max_jobs=1), timeout=1)

                self.assertEqual(manager.renewals, 0)
                self.assertEqual(manager.statuses[-1], JobStatus.CANCELLED)

    async def test_native_claim_rechecks_current_gpu_and_unwrapped_capabilities(self) -> None:
        with TemporaryDirectory() as temporary:
            manager = FakeManager()
            manager.claim = make_claim(samples=False)
            runner = GuardedNativeRunner()
            settings = _settings(Path(temporary), runner_mode="native")
            agent = WorkerAgent(
                settings,
                manager,
                runner,
                gpu_collector=VisibleGpuCollector(1),
            )

            capabilities = await agent._capabilities()
            self.assertEqual(capabilities.rvc_commit_hash, RVC_REVIEWED_COMMIT)
            self.assertTrue(capabilities.rvc_assets_ready)
            await asyncio.wait_for(agent.run(max_jobs=1), timeout=5)

            self.assertEqual(manager.statuses[-1], JobStatus.FAILED)
            self.assertEqual(
                manager.status_updates[-1].error_code,
                "stage_configuration_invalid",
            )
            self.assertEqual(runner.claim_validations, 0)

    async def test_claim_input_disk_requirement_and_below_boundary_fail_before_workspace(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = FakeManager()
            claim = manager.claim
            settings = _settings(root, runner_mode="native")
            required = required_claim_input_disk_bytes(
                claim,
                min_free_disk_bytes=settings.min_free_disk_bytes,
                dataset_max_total_bytes=settings.dataset_max_total_bytes,
            )
            self.assertEqual(
                required,
                settings.min_free_disk_bytes
                + claim.dataset_transfer.size_bytes  # type: ignore[union-attr]
                + settings.dataset_max_total_bytes,
            )
            sample_claim = make_claim(samples=True)
            sample_required = required_claim_input_disk_bytes(
                sample_claim,
                min_free_disk_bytes=settings.min_free_disk_bytes,
                dataset_max_total_bytes=settings.dataset_max_total_bytes,
            )
            self.assertEqual(
                sample_required,
                required
                + sum(
                    item.size_bytes
                    for item in sample_claim.test_set_transfer.items  # type: ignore[union-attr]
                ),
            )
            workspace = FixedFreeWorkspaceManager(root / "jobs", required - 1)
            runner = GuardedNativeRunner()
            agent = WorkerAgent(
                settings,
                manager,
                runner,
                gpu_collector=VisibleGpuCollector(0),
                workspace_manager=workspace,
            )

            await agent._execute_claim(claim)

            self.assertEqual(workspace.checks, 1)
            self.assertEqual(workspace.prepare_calls, 0)
            self.assertEqual(runner.claim_validations, 1)
            self.assertEqual(manager.statuses[-1], JobStatus.FAILED)
            self.assertEqual(
                manager.status_updates[-1].error_code,
                "worker_runtime_unready",
            )

    async def test_claim_input_disk_exact_boundary_is_admitted(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = FakeManager()
            settings = _settings(root, runner_mode="native")
            required = required_claim_input_disk_bytes(
                manager.claim,
                min_free_disk_bytes=settings.min_free_disk_bytes,
                dataset_max_total_bytes=settings.dataset_max_total_bytes,
            )
            workspace = FixedFreeWorkspaceManager(root / "jobs", required)
            agent = WorkerAgent(
                settings,
                manager,
                GuardedNativeRunner(),
                gpu_collector=VisibleGpuCollector(0),
                workspace_manager=workspace,
            )

            await agent._admit_claim_input_disk(manager.claim)

            self.assertEqual(workspace.checks, 1)
            self.assertEqual(workspace.prepare_calls, 0)

    async def test_sample_claim_is_rejected_before_workspace_when_gate_is_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = FakeManager()
            manager.claim = make_claim(samples=True)
            runner = SecretFailingRunner()
            agent = WorkerAgent(
                _settings(root),
                manager,
                runner,
                gpu_collector=FakeGpuCollector(),
            )

            await asyncio.wait_for(agent.run(max_jobs=1), timeout=5)

            terminal = manager.status_updates[-1]
            self.assertEqual(terminal.status, JobStatus.FAILED)
            self.assertEqual(terminal.error_code, "worker_runtime_unready")
            self.assertEqual(runner.calls, 0)
            self.assertFalse((root / "jobs").exists())

    async def test_native_claim_rejects_manager_commit_mismatch_before_stage(self) -> None:
        with TemporaryDirectory() as temporary:
            manager = FakeManager()
            manager.claim = make_claim(samples=False)
            manager.claim.config.rvc_backend.rvc_commit_hash = "f" * 40
            runner = GuardedNativeRunner()
            agent = WorkerAgent(
                _settings(Path(temporary), runner_mode="native"),
                manager,
                runner,
                gpu_collector=VisibleGpuCollector(0),
            )

            await asyncio.wait_for(agent.run(max_jobs=1), timeout=5)

            self.assertEqual(manager.statuses[-1], JobStatus.FAILED)
            self.assertEqual(
                manager.status_updates[-1].error_code,
                "stage_configuration_invalid",
            )
            self.assertEqual(runner.claim_validations, 0)

    async def test_native_unready_assets_have_distinct_runtime_error(self) -> None:
        with TemporaryDirectory() as temporary:
            manager = FakeManager()
            manager.claim = make_claim(samples=False)
            runner = UnreadyNativeRunner()
            agent = WorkerAgent(
                _settings(Path(temporary), runner_mode="native"),
                manager,
                runner,
                gpu_collector=VisibleGpuCollector(0),
            )

            await asyncio.wait_for(agent.run(max_jobs=1), timeout=5)

            terminal = manager.status_updates[-1]
            self.assertEqual(terminal.status, JobStatus.FAILED)
            self.assertEqual(terminal.error_code, "worker_runtime_unready")
            self.assertEqual(
                terminal.error_message,
                "Worker runtime is not ready for assigned execution.",
            )
            self.assertEqual(runner.claim_validations, 0)

    async def test_terminal_error_uses_typed_code_and_never_leaks_raw_exception(self) -> None:
        with TemporaryDirectory() as temporary:
            manager = FakeManager()
            runner = SecretFailingRunner()
            agent = WorkerAgent(
                _settings(Path(temporary)),
                manager,
                runner,
                gpu_collector=FakeGpuCollector(),
            )

            await asyncio.wait_for(agent.run(max_jobs=1), timeout=5)

            terminal = manager.status_updates[-1]
            self.assertEqual(terminal.status, JobStatus.FAILED)
            self.assertEqual(terminal.error_code, "stage_internal_error")
            self.assertEqual(
                terminal.error_message,
                "Worker stage downloading_dataset failed unexpectedly.",
            )
            self.assertNotIn("bootstrap-secret", terminal.error_message)
            self.assertNotIn("/private", terminal.error_message)
            self.assertNotIn("argv", terminal.error_message)
            self.assertEqual(runner.calls, 1)
            self.assertEqual(terminal.telemetry_log_count, 0)
            metric_entries = [
                entry for batch in manager.metric_batches for entry in batch.entries
            ]
            self.assertEqual(terminal.telemetry_metric_count, len(metric_entries))
            self.assertEqual(
                {entry.key for entry in metric_entries},
                {
                    "system.gpu.count",
                    "system.gpu.telemetry_available",
                    "system.disk_free_bytes",
                },
            )

    async def test_status_wrapper_rejects_claim_and_attempt_identity_mismatch(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = FakeManager()
            agent = WorkerAgent(
                _settings(root),
                manager,
                FakeRvcRunner(),
                gpu_collector=FakeGpuCollector(),
            )
            claim = manager.claim
            active = ActiveJob(
                claim=claim,
                cancellation=asyncio.Event(),
                lease_expires_at=claim.lease_expires_at,
                lease_deadline=(
                    asyncio.get_running_loop().time()
                    + (claim.lease_expires_at - utc_now()).total_seconds()
                ),
                lease_changed=asyncio.Event(),
            )
            agent.active_job = active
            update = JobStatusUpdate(
                lease_id=claim.lease_id,
                status=JobStatus.ASSIGNED,
            )

            with self.assertRaisesRegex(TelemetrySpoolError, "active claim"):
                await agent._update_status("other-job", update)  # noqa: SLF001

            active.telemetry_session = AttemptTelemetrySession(
                job_id=claim.job_id,
                attempt_id="other-attempt",
                lease_id=claim.lease_id,
                spool=TelemetrySpool(root / "mismatched-spool"),
                manager=manager,  # type: ignore[arg-type]
                status_callback=manager.update_status,
            )
            with self.assertRaisesRegex(TelemetrySpoolError, "Job attempt"):
                await agent._update_status(claim.job_id, update)  # noqa: SLF001
            self.assertFalse(manager.status_updates)

    async def test_artifact_transport_retry_exhaustion_is_typed_and_stage_is_not_replayed(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            manager = FakeManager(artifact_failure_status=503)
            settings = _settings(Path(temporary), artifact_upload_max_attempts=2)
            agent = WorkerAgent(
                settings,
                manager,
                FakeRvcRunner(),
                gpu_collector=FakeGpuCollector(),
            )

            async def no_backoff(*args: object) -> None:
                del args
                await asyncio.sleep(0)

            with patch("rvc_worker.agent._wait_any", new=no_backoff):
                await asyncio.wait_for(agent.run(max_jobs=1), timeout=5)

            terminal = manager.status_updates[-1]
            self.assertEqual(terminal.status, JobStatus.FAILED)
            self.assertEqual(terminal.error_code, "exhausted_transient")
            self.assertEqual(
                terminal.error_message,
                "Worker stage uploading_artifacts exhausted its bounded transient operation.",
            )
            self.assertEqual(manager.artifact_attempts, 2)
            self.assertEqual(manager.statuses.count(JobStatus.UPLOADING_ARTIFACTS), 1)
            self.assertNotIn("private", terminal.error_message)
            self.assertNotIn("secret", terminal.error_message)

    async def test_terminal_reporting_failure_log_never_includes_manager_error_text(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            manager = FakeManager(terminal_failure_status=503)
            agent = WorkerAgent(
                _settings(Path(temporary)),
                manager,
                SecretFailingRunner(),
                gpu_collector=FakeGpuCollector(),
            )

            with self.assertLogs("rvc_worker.agent") as captured:
                await asyncio.wait_for(agent.run(max_jobs=1), timeout=5)

            messages = "\n".join(captured.output)
            self.assertIn("manager_error=http-503", messages)
            self.assertNotIn("manager.example", messages)
            self.assertNotIn("/private", messages)
            self.assertNotIn("secret-token", messages)
            self.assertNotIn("argv", messages)

    async def test_telemetry_transient_exhaustion_is_deferred_without_stage_replay(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            manager = FakeManager(telemetry_failure_status=503)
            agent = WorkerAgent(
                _settings(Path(temporary)),
                manager,
                FakeRvcRunner(),
                gpu_collector=FakeGpuCollector(),
            )

            await asyncio.wait_for(agent.run(max_jobs=1), timeout=5)

            self.assertEqual(manager.statuses[-1], JobStatus.COMPLETED)
            self.assertGreater(manager.telemetry_attempts, 0)
            self.assertTrue(list(agent.telemetry_spool.pending.glob("*.json")))
            for stage in build_stage_plan(manager.claim):
                self.assertEqual(manager.statuses.count(stage), 1)


def _settings(
    root: Path,
    *,
    runner_mode: str = "fake",
    artifact_upload_max_attempts: int = 3,
    lease_renew_interval_seconds: float = 0.01,
    system_telemetry_interval_seconds: float = 60.0,
) -> WorkerSettings:
    return WorkerSettings(
        manager_url="https://manager.example",
        worker_name="gpu-01",
        worker_token="bootstrap-secret",
        data_root=root,
        runner_mode=runner_mode,
        heartbeat_interval_seconds=0.01,
        system_telemetry_interval_seconds=system_telemetry_interval_seconds,
        poll_interval_seconds=0.01,
        lease_renew_interval_seconds=lease_renew_interval_seconds,
        request_timeout_seconds=1,
        shutdown_grace_seconds=0.2,
        gpu_query_timeout_seconds=0.1,
        min_free_disk_bytes=0,
        artifact_upload_max_attempts=artifact_upload_max_attempts,
    )


if __name__ == "__main__":
    unittest.main()
