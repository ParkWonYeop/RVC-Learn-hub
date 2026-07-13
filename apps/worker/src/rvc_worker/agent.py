"""Heartbeat, claim/lease, cancellation, and single-job worker lifecycle."""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol, runtime_checkable

from rvc_orchestrator_contracts import (
    TERMINAL_JOB_STATUSES,
    GPUCapability,
    InferenceF0Method,
    JobClaim,
    JobClaimRequest,
    JobStatus,
    JobStatusUpdate,
    LeaseRenewRequest,
    LogBatch,
    LogEntry,
    LogLevel,
    MetricBatch,
    MetricEntry,
    RVCVersion,
    SampleRead,
    SampleRegistrationRequest,
    TrainingF0Method,
    WorkerCapabilities,
    WorkerEngineMode,
    WorkerHeartbeatRequest,
    WorkerRegisterRequest,
    WorkerStatus,
    job_config_sha256,
)

from . import __version__
from .client import ArtifactTransferCancelled, ManagerClient, ManagerClientError
from .credentials import CredentialStore, WorkerCredential
from .datasets import (
    DatasetMaterializationLimits,
    DatasetMaterializer,
    DatasetStageRunner,
)
from .gpu import GpuCollection, NvidiaSmiCollector
from .native_inference import NativeInferencePublication
from .native_runner import (
    NativeSampleInferenceRuntimeEvidence,
    NativeTrainingTelemetrySink,
)
from .runner import (
    RvcConfigurationError,
    RvcRunContext,
    RvcRunner,
    RvcRuntimeIntegrityError,
    StageResult,
)
from .rvc_commands import RvcCommandError, validate_gpu_ids
from .sample_publication import (
    NativeSamplePublicationPlan,
    build_sample_registration_requests,
    expand_finalized_artifacts,
    prepare_native_sample_publication,
    validate_finalized_artifact,
    validate_registered_sample,
)
from .settings import WorkerSettings
from .stages import (
    StageExecutionCancelled,
    StageExecutor,
    classify_stage_exception,
)
from .telemetry import (
    AttemptTelemetrySession,
    TelemetrySpool,
    TelemetrySpoolError,
)
from .test_sets import (
    TestSetMaterializationLimits,
    TestSetMaterializer,
    TestSetStageRunner,
)
from .uploads import ArtifactUploadCandidate, PublishedArtifact, collect_artifact_candidates
from .workspace import JobWorkspace, WorkspaceManager

LOGGER = logging.getLogger(__name__)


@runtime_checkable
class ClaimValidatingRunner(Protocol):
    def validate_claim(
        self,
        claim: JobClaim,
        available_gpu_ids: tuple[int, ...],
    ) -> None: ...


@runtime_checkable
class NativePublicationLoader(Protocol):
    def load_publication(self, context: RvcRunContext) -> NativeInferencePublication: ...


@runtime_checkable
class NativeSampleRuntimeProvider(Protocol):
    @property
    def sample_inference_runtime_evidence(
        self,
    ) -> NativeSampleInferenceRuntimeEvidence | None: ...


@runtime_checkable
class NativeTrainingTelemetryBinder(Protocol):
    def bind_training_telemetry(
        self,
        claim: JobClaim | None,
        sink: NativeTrainingTelemetrySink | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _CapabilityObservation:
    capabilities: WorkerCapabilities
    gpu_telemetry_available: bool


@dataclass(slots=True)
class ActiveJob:
    claim: JobClaim
    cancellation: asyncio.Event
    lease_expires_at: datetime
    lease_deadline: float
    lease_changed: asyncio.Event
    claim_runtime_image_digest: str | None = None
    claim_runtime_asset_manifest_sha256: str | None = None
    cancellation_reason: str | None = None
    telemetry_session: AttemptTelemetrySession | None = None
    current_stage: JobStatus = JobStatus.ASSIGNED
    fatal_error: Exception | None = None
    next_system_telemetry_at: float | None = None

    def cancel(self, reason: str) -> None:
        # Explicit cancellation/lease loss takes precedence over a concurrent
        # internal failure, matching the Worker terminal-status contract.
        self.fatal_error = None
        if not self.cancellation.is_set():
            self.cancellation_reason = reason
            self.cancellation.set()
        else:
            self.cancellation_reason = reason

    def fail(self, error: Exception, reason: str) -> None:
        """Stop execution while preserving a typed non-cancellation cause."""

        if not self.cancellation.is_set():
            self.fatal_error = error
            self.cancellation_reason = reason
            self.cancellation.set()

    def accept_lease_expiry(
        self,
        value: datetime,
        *,
        source: str,
        request_baseline: datetime | None = None,
    ) -> bool:
        candidate = _as_utc(value)
        current = _as_utc(self.lease_expires_at)
        now = _utc_now()
        baseline = _as_utc(request_baseline) if request_baseline is not None else None
        if current <= now or candidate <= now or (baseline is not None and candidate <= baseline):
            self.cancel(f"{source} returned an invalid lease expiry")
            return False
        if baseline is not None and candidate < current:
            # A heartbeat and explicit renewal may be in flight together. A
            # successful response can extend the lease relative to its own
            # request baseline yet arrive after the other response advanced it
            # further. Keep the newer Manager-issued deadline.
            return True
        if candidate < current:
            self.cancel(f"{source} returned an invalid lease expiry")
            return False
        if candidate > current:
            self.lease_expires_at = candidate
            self.lease_deadline = (
                asyncio.get_running_loop().time() + (candidate - now).total_seconds()
            )
            self.lease_changed.set()
        return True


class WorkerAgent:
    def __init__(
        self,
        settings: WorkerSettings,
        manager: ManagerClient,
        runner: RvcRunner,
        *,
        gpu_collector: NvidiaSmiCollector | None = None,
        workspace_manager: WorkspaceManager | None = None,
        credential_store: CredentialStore | None = None,
        telemetry_spool: TelemetrySpool | None = None,
    ) -> None:
        self.settings = settings
        self.manager = manager
        self.runner = (
            runner
            if settings.runner_mode == "fake"
            else TestSetStageRunner(
                DatasetStageRunner(
                    runner,
                    DatasetMaterializer(
                        manager,
                        limits=DatasetMaterializationLimits(
                            max_archive_bytes=settings.dataset_max_archive_bytes,
                            max_entries=settings.dataset_max_entries,
                            max_file_bytes=settings.dataset_max_file_bytes,
                            max_total_bytes=settings.dataset_max_total_bytes,
                            max_compression_ratio=settings.dataset_max_compression_ratio,
                            download_attempts=settings.dataset_download_max_attempts,
                        ),
                    ),
                ),
                TestSetMaterializer(
                    manager,
                    limits=TestSetMaterializationLimits(
                        max_items=settings.test_set_max_items,
                        max_item_bytes=settings.test_set_max_item_bytes,
                        max_total_bytes=settings.test_set_max_total_bytes,
                        max_duration_seconds=settings.test_set_max_duration_seconds,
                        max_total_duration_seconds=(settings.test_set_max_total_duration_seconds),
                        materialization_timeout_seconds=(
                            settings.test_set_materialization_timeout_seconds
                        ),
                        min_sample_rate_hz=settings.test_set_min_sample_rate_hz,
                        max_sample_rate_hz=settings.test_set_max_sample_rate_hz,
                        max_channels=settings.test_set_max_channels,
                        duration_tolerance_seconds=(settings.test_set_duration_tolerance_seconds),
                        download_attempts=settings.test_set_download_max_attempts,
                    ),
                ),
            )
        )
        self.gpu_collector = gpu_collector or NvidiaSmiCollector(
            timeout_seconds=settings.gpu_query_timeout_seconds
        )
        self.workspace_manager = workspace_manager or WorkspaceManager(
            settings.data_root / "jobs", min_free_bytes=settings.min_free_disk_bytes
        )
        self.credential_store = credential_store
        self.telemetry_spool = telemetry_spool or TelemetrySpool(
            settings.data_root / "telemetry-spool",
            max_bytes=settings.telemetry_spool_max_bytes,
        )
        self.shutdown_requested = asyncio.Event()
        self.active_job: ActiveJob | None = None
        self.worker_id: str | None = None

    def request_shutdown(self) -> None:
        self.shutdown_requested.set()
        if self.active_job is not None:
            self.active_job.cancel("worker shutdown")

    async def run(self, *, max_jobs: int | None = None) -> int:
        await self._establish_session()
        await self._flush_telemetry()
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="worker-heartbeat")
        completed_jobs = 0
        try:
            while not self.shutdown_requested.is_set():
                capabilities = await self._capabilities()
                try:
                    claim = await self.manager.claim_job(
                        JobClaimRequest(
                            capabilities=capabilities,
                            max_wait_seconds=min(
                                30, max(0, int(self.settings.poll_interval_seconds))
                            ),
                        )
                    )
                except ManagerClientError as exc:
                    LOGGER.warning(
                        "job claim failed (manager_error=%s)",
                        _manager_error_label(exc),
                    )
                    await _wait_event(self.shutdown_requested, self.settings.poll_interval_seconds)
                    continue
                if claim is None:
                    await _wait_event(self.shutdown_requested, self.settings.poll_interval_seconds)
                    continue
                await self._execute_claim(claim, claim_capabilities=capabilities)
                completed_jobs += 1
                if max_jobs is not None and completed_jobs >= max_jobs:
                    break
        finally:
            self.shutdown_requested.set()
            if self.active_job is not None:
                self.active_job.cancel("worker shutdown")
            await self._best_effort_draining_heartbeat()
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        return completed_jobs

    async def _establish_session(self) -> None:
        session = await self.manager.get_session()
        if session is not None:
            if session.name != self.settings.worker_name:
                raise ManagerClientError("stored Worker token belongs to a different Worker")
            if session.current_job_id is not None:
                LOGGER.warning(
                    "Worker restart found active job %s; waiting for Manager lease recovery",
                    session.current_job_id,
                )
            self.worker_id = session.worker_id
            return
        await self._register_with_retry()

    async def _register_with_retry(self) -> None:
        delay = 1.0
        while not self.shutdown_requested.is_set():
            try:
                response = await self.manager.register(
                    WorkerRegisterRequest(
                        name=self.settings.worker_name,
                        capabilities=await self._capabilities(),
                    )
                )
                self.worker_id = response.worker_id
                if self.credential_store is not None:
                    self.credential_store.save(
                        WorkerCredential(
                            manager_url=self.settings.manager_url,
                            worker_id=response.worker_id,
                            worker_name=self.settings.worker_name,
                            worker_token=response.worker_token,
                        )
                    )
                return
            except ManagerClientError as exc:
                if exc.status_code in {400, 401, 403, 409, 422}:
                    raise
                LOGGER.warning(
                    "worker registration failed; retrying (manager_error=%s)",
                    _manager_error_label(exc),
                )
                await _wait_event(self.shutdown_requested, delay)
                delay = min(delay * 2, 30.0)
        raise asyncio.CancelledError

    async def _heartbeat_loop(self) -> None:
        while not self.shutdown_requested.is_set():
            try:
                observation = await self._capability_observation()
                capabilities = observation.capabilities
                active = self.active_job
                status = WorkerStatus.BUSY if active else WorkerStatus.IDLE
                expiry_before_request = active.lease_expires_at if active else None
                now = asyncio.get_running_loop().time()
                if (
                    active is not None
                    and self.active_job is active
                    and not active.cancellation.is_set()
                    and active.telemetry_session is not None
                    and active.next_system_telemetry_at is not None
                    and now >= active.next_system_telemetry_at
                ):
                    try:
                        recorded = await active.telemetry_session.record_system_snapshot(
                            capabilities,
                            gpu_telemetry_available=(observation.gpu_telemetry_available),
                        )
                        if recorded:
                            active.next_system_telemetry_at = (
                                asyncio.get_running_loop().time()
                                + self.settings.system_telemetry_interval_seconds
                            )
                    except TelemetrySpoolError as exc:
                        LOGGER.error(
                            "job %s system telemetry could not be persisted",
                            active.claim.job_id,
                        )
                        active.fail(exc, "system telemetry spool failed")
                response = await self.manager.heartbeat(
                    WorkerHeartbeatRequest(
                        status=status,
                        capabilities=capabilities,
                        current_job_id=active.claim.job_id if active else None,
                        current_lease_id=active.claim.lease_id if active else None,
                    )
                )
                if active and active.claim.job_id in response.cancel_job_ids:
                    active.cancel("manager cancellation")
                if (
                    active
                    and not active.cancellation.is_set()
                    and response.lease_expires_at is not None
                ):
                    active.accept_lease_expiry(
                        response.lease_expires_at,
                        source="Manager heartbeat",
                        request_baseline=expiry_before_request,
                    )
                await self._flush_telemetry()
            except ManagerClientError as exc:
                LOGGER.warning(
                    "heartbeat failed (manager_error=%s)",
                    _manager_error_label(exc),
                )
            except Exception:
                # A malformed local capability observation must not silently
                # terminate the long-lived heartbeat supervisor.
                LOGGER.error("heartbeat capability observation failed")
            await _wait_event(self.shutdown_requested, self.settings.heartbeat_interval_seconds)

    async def _execute_claim(
        self,
        claim: JobClaim,
        *,
        claim_capabilities: WorkerCapabilities | None = None,
    ) -> None:
        if claim_capabilities is None:
            claim_capabilities = await self._capabilities()
        lease_expires_at = _as_utc(claim.lease_expires_at)
        lease_remaining = (lease_expires_at - _utc_now()).total_seconds()
        active = ActiveJob(
            claim=claim,
            cancellation=asyncio.Event(),
            lease_expires_at=lease_expires_at,
            lease_deadline=(asyncio.get_running_loop().time() + max(0.0, lease_remaining)),
            lease_changed=asyncio.Event(),
            claim_runtime_image_digest=claim_capabilities.runtime_image_digest,
            claim_runtime_asset_manifest_sha256=(claim_capabilities.runtime_asset_manifest_sha256),
        )
        self.active_job = active
        finished = asyncio.Event()
        lease_tasks: tuple[asyncio.Task[None], ...] = ()
        telemetry_runner: NativeTrainingTelemetryBinder | None = None
        try:
            if lease_remaining <= 0:
                active.cancel("claim lease expired before execution")
                raise StageExecutionCancelled(JobStatus.ASSIGNED)
            lease_tasks = (
                asyncio.create_task(
                    self._lease_loop(active, finished),
                    name=f"lease-renew-{claim.job_id}",
                ),
                asyncio.create_task(
                    self._lease_deadline_loop(active, finished),
                    name=f"lease-deadline-{claim.job_id}",
                ),
            )
            await self._validate_claim_runtime(claim)
            await self._admit_claim_input_disk(claim)
            workspace = self.workspace_manager.prepare(claim.job_id, claim.attempt_id)
            active.telemetry_session = AttemptTelemetrySession(
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                lease_id=claim.lease_id,
                spool=self.telemetry_spool,
                manager=self.manager,
                status_callback=self._update_status,
                redacted_roots=(self.settings.data_root, workspace.root),
                delivery_interval_seconds=min(
                    1.0,
                    self.settings.heartbeat_interval_seconds,
                ),
            )
            active.telemetry_session.start()
            initial_observation = await self._capability_observation()
            await active.telemetry_session.record_system_snapshot(
                initial_observation.capabilities,
                gpu_telemetry_available=initial_observation.gpu_telemetry_available,
            )
            active.next_system_telemetry_at = (
                asyncio.get_running_loop().time() + self.settings.system_telemetry_interval_seconds
            )
            base_runner = _base_runner(self.runner)
            if isinstance(base_runner, NativeTrainingTelemetryBinder):
                base_runner.bind_training_telemetry(
                    claim,
                    active.telemetry_session,
                )
                telemetry_runner = base_runner
            executor = StageExecutor(self.runner, self._update_status, self._report_stage)
            await executor.execute(claim, workspace, active.cancellation)
        except Exception as exc:
            fatal_error = active.fatal_error
            failure = classify_stage_exception(
                active.current_stage,
                fatal_error if fatal_error is not None else exc,
                None if fatal_error is not None else active.cancellation,
            )
            if isinstance(failure, StageExecutionCancelled):
                await self._best_effort_terminal_status(claim, JobStatus.CANCELLED)
            else:
                LOGGER.error(
                    "job %s failed at stage %s (%s/%s; retryable=%s)",
                    claim.job_id,
                    failure.stage.value,
                    failure.error_code,
                    failure.category.value,
                    failure.retryable,
                )
                await self._best_effort_terminal_status(
                    claim,
                    JobStatus.FAILED,
                    error_code=failure.error_code,
                    error_message=failure.safe_message,
                )
        finally:
            finished.set()
            active.lease_changed.set()
            if telemetry_runner is not None:
                try:
                    telemetry_runner.bind_training_telemetry(None, None)
                except Exception:
                    LOGGER.error("native training telemetry binding could not be cleared")
            if active.telemetry_session is not None:
                await active.telemetry_session.close(
                    cancelled=active.cancellation.is_set(),
                )
            if lease_tasks:
                await asyncio.gather(*lease_tasks, return_exceptions=True)
            self.active_job = None

    async def _validate_claim_runtime(self, claim: JobClaim) -> None:
        try:
            current_config_sha256 = job_config_sha256(claim.config)
        except (TypeError, ValueError) as exc:
            raise RvcRuntimeIntegrityError(
                "Job configuration changed after the Manager claim"
            ) from exc
        if current_config_sha256 != claim.config_sha256:
            raise RvcRuntimeIntegrityError("Job configuration changed after the Manager claim")
        if claim.config.auto_inference_samples.enabled:
            sample_capabilities = await self._capabilities()
            if not sample_capabilities.fixed_test_set_inference_ready:
                raise RvcRuntimeIntegrityError(
                    "Worker fixed TestSet inference capability is not ready"
                )
            active = self.active_job
            if (
                active is None
                or active.claim.job_id != claim.job_id
                or active.claim.attempt_id != claim.attempt_id
                or active.claim_runtime_image_digest is None
                or active.claim_runtime_asset_manifest_sha256 is None
                or sample_capabilities.runtime_image_digest != active.claim_runtime_image_digest
                or sample_capabilities.runtime_asset_manifest_sha256
                != active.claim_runtime_asset_manifest_sha256
            ):
                raise RvcRuntimeIntegrityError("Worker sample runtime changed after the Job claim")
        if self.settings.runner_mode == "fake":
            return
        capabilities = await self._capabilities()
        if not capabilities.rvc_assets_ready:
            raise RvcRuntimeIntegrityError("Worker RVC assets are not ready for a real Job")
        requested_commit = claim.config.rvc_backend.rvc_commit_hash
        if (
            requested_commit is not None
            and requested_commit.lower() != capabilities.rvc_commit_hash.lower()
        ):
            raise RvcConfigurationError("Job RVC commit does not match current Worker capabilities")
        visible_gpu_ids = tuple(gpu.index for gpu in capabilities.gpus)
        try:
            validate_gpu_ids(claim.config.training.gpu_ids, visible_gpu_ids)
            if claim.config.f0_extraction.training_f0_method is TrainingF0Method.RMVPE_GPU:
                validate_gpu_ids(
                    claim.config.f0_extraction.rmvpe_gpu_ids or [],
                    visible_gpu_ids,
                )
        except RvcCommandError as exc:
            raise RvcConfigurationError("Job requests a GPU that is not visible") from exc
        runner = _base_runner(self.runner)
        if isinstance(runner, ClaimValidatingRunner):
            await asyncio.to_thread(runner.validate_claim, claim, visible_gpu_ids)

    async def _admit_claim_input_disk(self, claim: JobClaim) -> None:
        if self.settings.runner_mode == "fake":
            return
        required = required_claim_input_disk_bytes(
            claim,
            min_free_disk_bytes=self.settings.min_free_disk_bytes,
            dataset_max_total_bytes=self.settings.dataset_max_total_bytes,
        )
        free = await asyncio.to_thread(self.workspace_manager.check_disk)
        if free < required:
            raise RvcRuntimeIntegrityError(
                "Worker has insufficient free disk for the claimed inputs"
            )

    async def _lease_loop(self, active: ActiveJob, finished: asyncio.Event) -> None:
        while not finished.is_set() and not active.cancellation.is_set():
            remaining = _remaining_lease_seconds(active)
            if remaining <= 0:
                active.cancel("lease expired")
                return
            await _wait_any(
                (finished, active.cancellation),
                min(self.settings.lease_renew_interval_seconds, remaining / 2),
            )
            if finished.is_set() or active.cancellation.is_set():
                return
            remaining = _remaining_lease_seconds(active)
            if remaining <= 0:
                active.cancel("lease expired")
                return
            expiry_before_request = _as_utc(active.lease_expires_at)
            try:
                response = await asyncio.wait_for(
                    self.manager.renew_lease(
                        active.claim.job_id,
                        LeaseRenewRequest(lease_id=active.claim.lease_id),
                    ),
                    timeout=remaining,
                )
                if response.lease_id != active.claim.lease_id:
                    active.cancel("Manager returned a mismatched lease")
                    return
                if _as_utc(response.lease_expires_at) <= expiry_before_request:
                    active.cancel("Manager renewal did not extend the lease")
                    return
                if not active.accept_lease_expiry(
                    response.lease_expires_at,
                    source="Manager renewal",
                    request_baseline=expiry_before_request,
                ):
                    return
            except TimeoutError:
                if (
                    _remaining_lease_seconds(active) <= 0
                    or _as_utc(active.lease_expires_at) <= expiry_before_request
                ):
                    active.cancel("lease renewal exceeded the active lease deadline")
                    return
            except ManagerClientError as exc:
                LOGGER.warning(
                    "lease renewal failed for job %s (manager_error=%s)",
                    active.claim.job_id,
                    _manager_error_label(exc),
                )
                if exc.status_code in {404, 409, 410} or _utc_now() >= _as_utc(
                    active.lease_expires_at
                ):
                    active.cancel("lease expired")
                    return

    async def _lease_deadline_loop(
        self,
        active: ActiveJob,
        finished: asyncio.Event,
    ) -> None:
        while not finished.is_set() and not active.cancellation.is_set():
            active.lease_changed.clear()
            remaining = _remaining_lease_seconds(active)
            if remaining <= 0:
                active.cancel("lease expired")
                return
            await _wait_any(
                (finished, active.cancellation, active.lease_changed),
                remaining,
            )
            if finished.is_set() or active.cancellation.is_set():
                return
            if _remaining_lease_seconds(active) <= 0:
                active.cancel("lease expired")
                return

    async def _best_effort_draining_heartbeat(self) -> None:
        active = self.active_job
        try:
            await self.manager.heartbeat(
                WorkerHeartbeatRequest(
                    status=WorkerStatus.DRAINING,
                    capabilities=await self._capabilities(),
                    current_job_id=active.claim.job_id if active else None,
                    current_lease_id=active.claim.lease_id if active else None,
                )
            )
        except (ManagerClientError, OSError):
            return

    async def _flush_telemetry(self) -> None:
        try:
            report = await self.telemetry_spool.flush(self.manager)
        except TelemetrySpoolError:
            LOGGER.error("telemetry spool flush failed")
            return
        if report.dead_lettered:
            LOGGER.error(
                "%s telemetry record(s) moved to dead-letter storage",
                report.dead_lettered,
            )

    async def _best_effort_terminal_status(
        self,
        claim: JobClaim,
        status: JobStatus,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        try:
            await self._update_status(
                claim.job_id,
                JobStatusUpdate(
                    lease_id=claim.lease_id,
                    status=status,
                    error_code=error_code,
                    error_message=error_message,
                ),
            )
        except ManagerClientError as exc:
            LOGGER.warning(
                "terminal status could not be reported for %s (manager_error=%s)",
                claim.job_id,
                _manager_error_label(exc),
            )
        except TelemetrySpoolError:
            LOGGER.error("terminal telemetry watermarks could not be reported")

    async def _update_status(
        self,
        job_id: str,
        update: JobStatusUpdate,
    ) -> None:
        """Fence status identity and attach sealed terminal telemetry counts."""

        active = self.active_job
        if (
            active is None
            or active.claim.job_id != job_id
            or active.claim.lease_id != update.lease_id
        ):
            raise TelemetrySpoolError("status update does not match the active claim")
        session = active.telemetry_session
        if session is not None and (
            session.job_id != active.claim.job_id
            or session.attempt_id != active.claim.attempt_id
            or session.lease_id != active.claim.lease_id
        ):
            raise TelemetrySpoolError("telemetry session does not match the active Job attempt")
        if update.status in TERMINAL_JOB_STATUSES:
            if session is None:
                log_count = 0
                metric_count = 0
            else:
                watermarks = await session.watermarks()
                log_count = watermarks.log_count
                metric_count = watermarks.metric_count
            update = update.model_copy(
                update={
                    "telemetry_log_count": log_count,
                    "telemetry_metric_count": metric_count,
                }
            )
        await self.manager.update_status(job_id, update)
        if update.status not in TERMINAL_JOB_STATUSES:
            active.current_stage = update.status

    async def _report_stage(
        self,
        claim: JobClaim,
        workspace: JobWorkspace,
        stage: JobStatus,
        result: StageResult,
        sequence: int,
    ) -> None:
        active = self.active_job
        telemetry_session = (
            active.telemetry_session
            if active is not None
            and active.claim.job_id == claim.job_id
            and active.claim.attempt_id == claim.attempt_id
            else None
        )
        if telemetry_session is not None:
            await telemetry_session.record_stage_completed(
                stage=stage,
                stage_ordinal=sequence,
                created_path_count=len(result.created_paths),
            )
        else:
            # Direct stage-callback tests and compatibility callers which do not
            # own an Agent execution session retain deterministic legacy batches.
            log_batch = LogBatch(
                lease_id=claim.lease_id,
                attempt_id=claim.attempt_id,
                idempotency_key=_idempotency_key(
                    "log", claim.attempt_id, stage.value, str(sequence)
                ),
                entries=[
                    LogEntry(
                        sequence=sequence,
                        level=LogLevel.INFO,
                        message=f"worker stage completed: {stage.value}",
                        fields={
                            "stage": stage.value,
                            "created_path_count": len(result.created_paths),
                        },
                    )
                ],
            )
            metric_batch = MetricBatch(
                lease_id=claim.lease_id,
                attempt_id=claim.attempt_id,
                idempotency_key=_idempotency_key(
                    "metric", claim.attempt_id, stage.value, str(sequence)
                ),
                entries=[
                    MetricEntry(
                        sequence=sequence,
                        key="worker.stage_completed",
                        value=1.0,
                    )
                ],
            )
            await self.telemetry_spool.enqueue_log(claim.job_id, log_batch)
            await self.telemetry_spool.enqueue_metric(claim.job_id, metric_batch)
        await self._flush_telemetry()
        if stage is not JobStatus.UPLOADING_ARTIFACTS:
            return
        publication_plan: NativeSamplePublicationPlan | None = None
        publication: NativeInferencePublication | None = None
        if claim.config.auto_inference_samples.enabled:
            publication = await self._load_native_sample_publication(claim, workspace)
        candidates = collect_artifact_candidates(
            claim,
            workspace,
            is_fake=self.settings.runner_mode == "fake",
            max_object_bytes=self.settings.artifact_max_object_bytes,
            max_files=self.settings.artifact_max_files_per_attempt,
            max_total_bytes=self.settings.artifact_max_total_bytes_per_attempt,
            checkpoint_retention=self.settings.artifact_checkpoint_retention,
        )
        if publication is not None:
            publication_plan = prepare_native_sample_publication(
                claim,
                workspace,
                publication,
                candidates,
            )
            candidates = publication_plan.upload_candidates
        finalized_uploads: dict[str, PublishedArtifact] = {}
        for candidate in candidates:
            artifact = await self._publish_artifact_with_retry(
                claim,
                candidate,
                stage=stage,
            )
            if publication_plan is not None:
                validate_finalized_artifact(claim, candidate, artifact)
                finalized_uploads[candidate.relative_path] = artifact
        if publication_plan is None:
            return
        finalized = expand_finalized_artifacts(
            claim,
            publication_plan,
            finalized_uploads,
        )
        requests = build_sample_registration_requests(
            claim,
            publication_plan,
            finalized,
        )
        for request in requests:
            response = await self._register_sample_with_retry(
                claim,
                request,
                stage=stage,
            )
            validate_registered_sample(claim, request, response)

    async def _load_native_sample_publication(
        self,
        claim: JobClaim,
        workspace: JobWorkspace,
    ) -> NativeInferencePublication:
        if self.settings.runner_mode != "native":
            raise RvcRuntimeIntegrityError(
                "fixed TestSet publication requires the guarded native runner"
            )
        runner = _base_runner(self.runner)
        dependency = getattr(runner, "sample_inference_dependency", None)
        if not isinstance(dependency, NativePublicationLoader):
            raise RvcRuntimeIntegrityError("native sample publication loader is unavailable")
        context = RvcRunContext(claim, workspace)
        publication = await asyncio.to_thread(dependency.load_publication, context)
        active = self._require_active_transfer(JobStatus.UPLOADING_ARTIFACTS)
        if (
            active.claim.job_id != claim.job_id
            or active.claim.attempt_id != claim.attempt_id
            or active.claim_runtime_image_digest is None
            or active.claim_runtime_asset_manifest_sha256 is None
            or publication.runtime_image_digest != active.claim_runtime_image_digest
            or publication.runtime_asset_manifest_sha256
            != active.claim_runtime_asset_manifest_sha256
        ):
            raise RvcRuntimeIntegrityError(
                "native sample publication does not match the claim-time runtime"
            )
        return publication

    async def _publish_artifact_with_retry(
        self,
        claim: JobClaim,
        candidate: ArtifactUploadCandidate,
        *,
        stage: JobStatus,
    ) -> PublishedArtifact:
        request = candidate.init_request(claim)
        delay = 1.0
        for attempt_number in range(1, self.settings.artifact_upload_max_attempts + 1):
            active = self._require_active_transfer(stage)
            try:
                artifact = await self.manager.publish_artifact(
                    claim.job_id,
                    request,
                    candidate.path,
                    cancellation=active.cancellation,
                )
                self._require_active_transfer(stage)
                return artifact
            except ArtifactTransferCancelled:
                raise StageExecutionCancelled(stage) from None
            except ManagerClientError as exc:
                active = self._require_active_transfer(stage)
                if (
                    not _retryable_manager_error(exc)
                    or attempt_number >= self.settings.artifact_upload_max_attempts
                ):
                    raise
                LOGGER.warning(
                    "artifact upload attempt %s/%s failed for %s; retrying",
                    attempt_number,
                    self.settings.artifact_upload_max_attempts,
                    candidate.artifact_type.value,
                )
                await self._wait_transfer_retry(active, stage, delay)
                delay = min(delay * 2, 30.0)
        raise AssertionError("bounded artifact retry loop did not return")

    async def _register_sample_with_retry(
        self,
        claim: JobClaim,
        request: SampleRegistrationRequest,
        *,
        stage: JobStatus,
    ) -> SampleRead:
        delay = 1.0
        for attempt_number in range(1, self.settings.artifact_upload_max_attempts + 1):
            active = self._require_active_transfer(stage)
            try:
                response = await self.manager.register_sample(
                    claim.job_id,
                    request,
                    cancellation=active.cancellation,
                )
                self._require_active_transfer(stage)
                return response
            except ArtifactTransferCancelled:
                raise StageExecutionCancelled(stage) from None
            except ManagerClientError as exc:
                active = self._require_active_transfer(stage)
                if (
                    not _retryable_manager_error(exc)
                    or attempt_number >= self.settings.artifact_upload_max_attempts
                ):
                    raise
                LOGGER.warning(
                    "Sample registration attempt %s/%s failed; retrying",
                    attempt_number,
                    self.settings.artifact_upload_max_attempts,
                )
                await self._wait_transfer_retry(active, stage, delay)
                delay = min(delay * 2, 30.0)
        raise AssertionError("bounded Sample registration retry loop did not return")

    def _require_active_transfer(self, stage: JobStatus) -> ActiveJob:
        active = self.active_job
        if active is None or active.cancellation.is_set() or self.shutdown_requested.is_set():
            raise StageExecutionCancelled(stage)
        return active

    async def _wait_transfer_retry(
        self,
        active: ActiveJob,
        stage: JobStatus,
        delay: float,
    ) -> None:
        await _wait_any((active.cancellation, self.shutdown_requested), delay)
        self._require_active_transfer(stage)

    async def registration_capabilities(self) -> WorkerCapabilities:
        """Collect the same bounded snapshot used for register and heartbeat."""

        return await self._capabilities()

    async def _capabilities(self) -> WorkerCapabilities:
        return (await self._capability_observation()).capabilities

    async def _capability_observation(self) -> _CapabilityObservation:
        try:
            collection = await asyncio.to_thread(self.gpu_collector.collect)
        except Exception:
            collection = GpuCollection((), False, "GPU telemetry collector failed")
        free_disk = await asyncio.to_thread(_disk_free, self.settings.data_root)
        runtime_evidence = _sample_runtime_evidence(self.settings, self.runner)
        sample_ready = runtime_evidence is not None
        try:
            gpu_capabilities = _gpu_capabilities(collection)
        except (OverflowError, TypeError, ValueError):
            collection = GpuCollection((), False, "invalid GPU telemetry snapshot")
            gpu_capabilities = []
        capabilities = WorkerCapabilities(
            worker_version=__version__,
            engine_mode=(
                WorkerEngineMode.FAKE
                if self.settings.runner_mode == "fake"
                else WorkerEngineMode.RVC_WEBUI
            ),
            rvc_commit_hash=_rvc_revision(self.settings, self.runner),
            supported_rvc_versions=[RVCVersion.V1, RVCVersion.V2],
            supported_training_f0_methods=list(TrainingF0Method),
            supported_inference_f0_methods=(list(InferenceF0Method) if sample_ready else []),
            fixed_test_set_inference_ready=sample_ready,
            gpus=gpu_capabilities,
            disk_free_bytes=free_disk,
            tags=list(self.settings.worker_tags),
            rvc_assets_ready=_rvc_assets_ready(self.settings, self.runner),
            runtime_image_digest=(
                runtime_evidence.runtime_image_digest if runtime_evidence else None
            ),
            runtime_asset_manifest_sha256=(
                runtime_evidence.runtime_asset_manifest_sha256 if runtime_evidence else None
            ),
            max_concurrent_jobs=1,
        )
        return _CapabilityObservation(
            capabilities=capabilities,
            gpu_telemetry_available=collection.available,
        )


def _gpu_capabilities(collection: GpuCollection) -> list[GPUCapability]:
    if not collection.available:
        return []
    if len(collection.gpus) > 64:
        raise ValueError("GPU inventory exceeds the supported limit")
    indices = [gpu.index for gpu in collection.gpus]
    if len(set(indices)) != len(indices):
        raise ValueError("GPU indexes are not unique")
    uuids = [gpu.uuid for gpu in collection.gpus if gpu.uuid is not None]
    if len(set(uuids)) != len(uuids):
        raise ValueError("GPU UUIDs are not unique")
    return [
        GPUCapability(
            index=gpu.index,
            uuid=gpu.uuid,
            name=gpu.name,
            total_vram_mb=gpu.memory_total_mb,
            free_vram_mb=max(0, gpu.memory_total_mb - gpu.memory_used_mb),
            utilization_percent=gpu.utilization_percent,
            temperature_c=gpu.temperature_celsius,
        )
        for gpu in collection.gpus
    ]


def required_claim_input_disk_bytes(
    claim: JobClaim,
    *,
    min_free_disk_bytes: int,
    dataset_max_total_bytes: int,
) -> int:
    dataset_archive_bytes = (
        claim.dataset_transfer.size_bytes if claim.dataset_transfer is not None else 0
    )
    test_set_bytes = (
        sum(item.size_bytes for item in claim.test_set_transfer.items)
        if claim.test_set_transfer is not None
        else 0
    )
    return min_free_disk_bytes + dataset_archive_bytes + dataset_max_total_bytes + test_set_bytes


def _rvc_revision(settings: WorkerSettings, runner: RvcRunner) -> str:
    if settings.runner_mode == "fake":
        return "fake-runner"
    verified = getattr(_base_runner(runner), "verified_commit_hash", None)
    if isinstance(verified, str) and len(verified) >= 7:
        return verified
    return "profile-pinned"


def _rvc_assets_ready(settings: WorkerSettings, runner: RvcRunner) -> bool:
    if settings.runner_mode == "fake":
        return False
    if settings.runner_mode == "profile":
        return True
    return getattr(_base_runner(runner), "assets_ready", False) is True


def _base_runner(runner: RvcRunner) -> RvcRunner:
    current = runner
    while isinstance(current, (DatasetStageRunner, TestSetStageRunner)):
        current = current.runner
    return current


def _sample_runtime_evidence(
    settings: WorkerSettings,
    runner: RvcRunner,
) -> NativeSampleInferenceRuntimeEvidence | None:
    if settings.runner_mode != "native":
        return None
    current = _base_runner(runner)
    if not isinstance(current, NativeSampleRuntimeProvider):
        return None
    return current.sample_inference_runtime_evidence


def _disk_free(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


async def _wait_event(event: asyncio.Event, delay_seconds: float) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout=delay_seconds)
    except TimeoutError:
        return


async def _wait_any(events: tuple[asyncio.Event, ...], delay_seconds: float) -> None:
    tasks = [asyncio.create_task(event.wait()) for event in events]
    try:
        await asyncio.wait(tasks, timeout=delay_seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _remaining_lease_seconds(active: ActiveJob) -> float:
    wall_remaining = (_as_utc(active.lease_expires_at) - _utc_now()).total_seconds()
    monotonic_remaining = active.lease_deadline - asyncio.get_running_loop().time()
    return min(wall_remaining, monotonic_remaining)


def _idempotency_key(*parts: str) -> str:
    return sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _retryable_manager_error(exc: ManagerClientError) -> bool:
    return exc.retryable


def _manager_error_label(exc: ManagerClientError) -> str:
    if exc.status_code is not None:
        return f"http-{exc.status_code}"
    return "transient-transport" if exc.retryable else exc.category
