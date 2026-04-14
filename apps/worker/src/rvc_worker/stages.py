"""Validated JobStatus plan and per-stage execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from rvc_orchestrator_contracts import (
    JobClaim,
    JobStatus,
    JobStatusUpdate,
    validate_job_transition,
)

from .artifacts import ArtifactDiscoveryError
from .client import (
    ArtifactTransferCancelled,
    DatasetTransferCancelled,
    DatasetTransferError,
    ManagerClientError,
    TestSetTransferCancelled,
    TestSetTransferError,
)
from .datasets import DatasetMaterializationError
from .index_builder import IndexBuildError
from .pretrained import PretrainedResolutionError
from .process import (
    ProcessCancelled,
    ProcessFailed,
    ProcessRunnerError,
    ProcessTimedOut,
)
from .runner import (
    RvcConfigurationError,
    RvcRunContext,
    RvcRunner,
    RvcRunnerError,
    RvcRuntimeIntegrityError,
    StageResult,
)
from .rvc_commands import RvcCommandError
from .small_model import SmallModelExtractionError
from .telemetry import TelemetrySpoolError
from .test_sets import (
    TestSetMaterializationCancelled,
    TestSetMaterializationError,
    TestSetMaterializationTimeout,
)
from .training_inputs import TrainingInputError
from .training_metrics import MetricParserError
from .workspace import JobWorkspace, WorkspaceError


class StageErrorCategory(StrEnum):
    """Stable operator-facing failure categories with no exception text."""

    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    PROCESS = "process"
    TRANSIENT = "transient"
    INTEGRITY = "integrity"
    CONFIGURATION = "configuration"
    TELEMETRY = "telemetry"
    INTERNAL = "internal"


class StageFailureCause(StrEnum):
    """Sanitized causes safe to retain in memory, logs, and tests."""

    CANCELLATION = "cancellation"
    PROCESS_TIMEOUT = "process_timeout"
    TEST_SET_TIMEOUT = "test_set_timeout"
    PROCESS_NONZERO = "process_nonzero"
    PROCESS_RUNTIME = "process_runtime"
    TRANSIENT_EXHAUSTED = "transient_exhausted"
    DATASET_INTEGRITY = "dataset_integrity"
    TEST_SET_INTEGRITY = "test_set_integrity"
    ARTIFACT_INTEGRITY = "artifact_integrity"
    WORKSPACE_INTEGRITY = "workspace_integrity"
    RUNTIME_INTEGRITY = "runtime_integrity"
    WORKER_RUNTIME_UNREADY = "worker_runtime_unready"
    REMOTE_REJECTED = "remote_rejected"
    CONFIGURATION = "configuration"
    TELEMETRY_PERSISTENCE = "telemetry_persistence"
    UNKNOWN = "unknown"


class InternalRetryScope(StrEnum):
    """Atomic/idempotent operations allowed to retry inside a single stage call."""

    DATASET_TRANSFER = "dataset_transfer"
    TEST_SET_TRANSFER = "test_set_transfer"
    ARTIFACT_TRANSFER = "artifact_transfer"


@dataclass(frozen=True, slots=True)
class StageExecutionPolicy:
    """Explicitly forbids executor-level replay and names internal retry exceptions."""

    executor_max_attempts: int = 1
    internal_retry_scopes: tuple[InternalRetryScope, ...] = ()


STAGE_EXECUTION_POLICIES: dict[JobStatus, StageExecutionPolicy] = {
    JobStatus.ASSIGNED: StageExecutionPolicy(),
    JobStatus.DOWNLOADING_DATASET: StageExecutionPolicy(
        internal_retry_scopes=(
            InternalRetryScope.DATASET_TRANSFER,
            InternalRetryScope.TEST_SET_TRANSFER,
        )
    ),
    JobStatus.VALIDATING_DATASET: StageExecutionPolicy(),
    JobStatus.PREPARING_FLAT_DATASET: StageExecutionPolicy(),
    JobStatus.PREPROCESSING: StageExecutionPolicy(),
    JobStatus.EXTRACTING_F0: StageExecutionPolicy(),
    JobStatus.EXTRACTING_FEATURES: StageExecutionPolicy(),
    JobStatus.TRAINING: StageExecutionPolicy(),
    JobStatus.SAVING_CHECKPOINT: StageExecutionPolicy(),
    JobStatus.BUILDING_INDEX: StageExecutionPolicy(),
    JobStatus.COLLECTING_SMALL_MODEL: StageExecutionPolicy(),
    JobStatus.GENERATING_SAMPLES: StageExecutionPolicy(),
    JobStatus.EVALUATING: StageExecutionPolicy(),
    JobStatus.UPLOADING_ARTIFACTS: StageExecutionPolicy(
        internal_retry_scopes=(InternalRetryScope.ARTIFACT_TRANSFER,)
    ),
    JobStatus.COMPLETED: StageExecutionPolicy(),
}


class StageExecutionError(RuntimeError):
    """Typed stage failure whose public fields never contain raw exception text."""

    def __init__(
        self,
        stage: JobStatus,
        *,
        error_code: str,
        category: StageErrorCategory,
        retryable: bool,
        cause: StageFailureCause,
        safe_message: str,
    ) -> None:
        if stage not in STAGE_EXECUTION_POLICIES:
            raise ValueError("stage has no explicit execution policy")
        super().__init__(safe_message)
        self.stage = stage
        self.error_code = error_code
        self.category = category
        self.retryable = retryable
        self.cause = cause
        self.safe_message = safe_message


class StageExecutionCancelled(StageExecutionError):
    """Raised when a stage observes cancellation or lease loss."""

    def __init__(self, stage: JobStatus) -> None:
        super().__init__(
            stage,
            error_code="stage_cancelled",
            category=StageErrorCategory.CANCELLED,
            retryable=False,
            cause=StageFailureCause.CANCELLATION,
            safe_message="Worker execution was cancelled.",
        )


StatusCallback = Callable[[str, JobStatusUpdate], Awaitable[None]]
StageCallback = Callable[[JobClaim, JobWorkspace, JobStatus, StageResult, int], Awaitable[None]]
ExceptionT = TypeVar("ExceptionT", bound=BaseException)


@dataclass(frozen=True, slots=True)
class ExecutionSummary:
    final_status: JobStatus
    stage_results: dict[JobStatus, StageResult]


def build_stage_plan(claim: JobClaim) -> tuple[JobStatus, ...]:
    config = claim.config
    stages = [
        JobStatus.DOWNLOADING_DATASET,
        JobStatus.VALIDATING_DATASET,
        JobStatus.PREPARING_FLAT_DATASET,
        JobStatus.PREPROCESSING,
    ]
    if config.model.use_f0:
        stages.append(JobStatus.EXTRACTING_F0)
    stages.extend(
        [
            JobStatus.EXTRACTING_FEATURES,
            JobStatus.TRAINING,
            JobStatus.SAVING_CHECKPOINT,
        ]
    )
    if config.index.build_index:
        stages.append(JobStatus.BUILDING_INDEX)
    stages.append(JobStatus.COLLECTING_SMALL_MODEL)
    if config.auto_inference_samples.enabled:
        stages.append(JobStatus.GENERATING_SAMPLES)
    stages.extend([JobStatus.EVALUATING, JobStatus.UPLOADING_ARTIFACTS])
    return tuple(stages)


class StageExecutor:
    def __init__(
        self,
        runner: RvcRunner,
        status_callback: StatusCallback,
        stage_callback: StageCallback | None = None,
    ) -> None:
        self.runner = runner
        self.status_callback = status_callback
        self.stage_callback = stage_callback

    async def execute(
        self,
        claim: JobClaim,
        workspace: JobWorkspace,
        cancellation: asyncio.Event,
    ) -> ExecutionSummary:
        context = RvcRunContext(claim, workspace)
        current = JobStatus.ASSIGNED
        results: dict[JobStatus, StageResult] = {}
        for sequence, stage in enumerate(build_stage_plan(claim)):
            if cancellation.is_set():
                raise StageExecutionCancelled(stage)
            try:
                validate_job_transition(current, stage, allow_same=False)
                await self.status_callback(
                    claim.job_id, JobStatusUpdate(lease_id=claim.lease_id, status=stage)
                )
                result = await self.runner.run_stage(stage, context, cancellation)
                if cancellation.is_set():
                    raise StageExecutionCancelled(stage)
                if self.stage_callback is not None:
                    await self.stage_callback(claim, workspace, stage, result, sequence)
            except asyncio.CancelledError:
                raise StageExecutionCancelled(stage) from None
            except Exception as exc:
                classified = classify_stage_exception(stage, exc, cancellation)
                if classified is exc:
                    raise
                raise classified from None
            results[stage] = result
            current = stage

        if cancellation.is_set():
            raise StageExecutionCancelled(JobStatus.COMPLETED)
        try:
            validate_job_transition(current, JobStatus.COMPLETED, allow_same=False)
            await self.status_callback(
                claim.job_id,
                JobStatusUpdate(lease_id=claim.lease_id, status=JobStatus.COMPLETED),
            )
        except asyncio.CancelledError:
            raise StageExecutionCancelled(JobStatus.COMPLETED) from None
        except Exception as exc:
            classified = classify_stage_exception(JobStatus.COMPLETED, exc, cancellation)
            if classified is exc:
                raise
            raise classified from None
        return ExecutionSummary(JobStatus.COMPLETED, results)


def classify_stage_exception(
    stage: JobStatus,
    exc: BaseException,
    cancellation: asyncio.Event | None = None,
) -> StageExecutionError:
    """Map an internal exception to a stable, non-sensitive stage failure."""

    if (
        (cancellation is not None and cancellation.is_set())
        or isinstance(
            exc,
            (
                asyncio.CancelledError,
                ProcessCancelled,
                ArtifactTransferCancelled,
                DatasetTransferCancelled,
                TestSetTransferCancelled,
                TestSetMaterializationCancelled,
                StageExecutionCancelled,
            ),
        )
    ):
        return StageExecutionCancelled(stage)
    if isinstance(exc, StageExecutionError):
        return exc

    if isinstance(exc, TestSetMaterializationTimeout):
        return _stage_error(
            stage,
            error_code="stage_timeout",
            category=StageErrorCategory.TIMEOUT,
            retryable=False,
            cause=StageFailureCause.TEST_SET_TIMEOUT,
            action="timed out",
        )

    process_timeout = _find_cause(exc, ProcessTimedOut)
    if process_timeout is not None:
        return _stage_error(
            stage,
            error_code="stage_timeout",
            category=StageErrorCategory.TIMEOUT,
            retryable=False,
            cause=StageFailureCause.PROCESS_TIMEOUT,
            action="timed out",
        )
    process_failed = _find_cause(exc, ProcessFailed)
    if process_failed is not None:
        return _stage_error(
            stage,
            error_code="stage_process_failed",
            category=StageErrorCategory.PROCESS,
            retryable=False,
            cause=StageFailureCause.PROCESS_NONZERO,
            action="failed in its isolated process",
        )
    if isinstance(exc, ProcessRunnerError):
        return _stage_error(
            stage,
            error_code="stage_process_failed",
            category=StageErrorCategory.PROCESS,
            retryable=False,
            cause=StageFailureCause.PROCESS_RUNTIME,
            action="could not run its isolated process",
        )

    manager_error = _find_cause(exc, ManagerClientError)
    if manager_error is not None and manager_error.retryable:
        return _stage_error(
            stage,
            error_code="exhausted_transient",
            category=StageErrorCategory.TRANSIENT,
            retryable=True,
            cause=StageFailureCause.TRANSIENT_EXHAUSTED,
            action="exhausted its bounded transient operation",
        )
    if manager_error is not None and manager_error.category == "integrity":
        return _stage_error(
            stage,
            error_code="stage_integrity_failed",
            category=StageErrorCategory.INTEGRITY,
            retryable=False,
            cause=StageFailureCause.ARTIFACT_INTEGRITY,
            action="failed an integrity check",
        )
    if isinstance(exc, DatasetTransferError) or isinstance(
        exc, DatasetMaterializationError
    ):
        return _stage_error(
            stage,
            error_code="stage_integrity_failed",
            category=StageErrorCategory.INTEGRITY,
            retryable=False,
            cause=StageFailureCause.DATASET_INTEGRITY,
            action="failed an integrity check",
        )
    if isinstance(exc, (TestSetTransferError, TestSetMaterializationError)):
        return _stage_error(
            stage,
            error_code="stage_integrity_failed",
            category=StageErrorCategory.INTEGRITY,
            retryable=False,
            cause=StageFailureCause.TEST_SET_INTEGRITY,
            action="failed an integrity check",
        )
    if isinstance(exc, WorkspaceError):
        return _stage_error(
            stage,
            error_code="stage_integrity_failed",
            category=StageErrorCategory.INTEGRITY,
            retryable=False,
            cause=StageFailureCause.WORKSPACE_INTEGRITY,
            action="failed an integrity check",
        )
    if isinstance(exc, TelemetrySpoolError):
        return _stage_error(
            stage,
            error_code="telemetry_persistence_failed",
            category=StageErrorCategory.TELEMETRY,
            retryable=False,
            cause=StageFailureCause.TELEMETRY_PERSISTENCE,
            action="could not persist required telemetry",
        )
    if isinstance(exc, ManagerClientError):
        return _stage_error(
            stage,
            error_code="stage_remote_rejected",
            category=StageErrorCategory.CONFIGURATION,
            retryable=False,
            cause=StageFailureCause.REMOTE_REJECTED,
            action="was rejected by the Manager",
        )
    if isinstance(exc, RvcRuntimeIntegrityError):
        return StageExecutionError(
            stage,
            error_code="worker_runtime_unready",
            category=StageErrorCategory.INTEGRITY,
            retryable=False,
            cause=StageFailureCause.WORKER_RUNTIME_UNREADY,
            safe_message="Worker runtime is not ready for assigned execution.",
        )
    if isinstance(
        exc,
        (PretrainedResolutionError, RvcCommandError, RvcConfigurationError),
    ):
        return _stage_error(
            stage,
            error_code="stage_configuration_invalid",
            category=StageErrorCategory.CONFIGURATION,
            retryable=False,
            cause=StageFailureCause.CONFIGURATION,
            action="has an invalid configuration",
        )
    if isinstance(
        exc,
        (
            ArtifactDiscoveryError,
            IndexBuildError,
            MetricParserError,
            RvcRunnerError,
            SmallModelExtractionError,
            TrainingInputError,
        ),
    ):
        return _stage_error(
            stage,
            error_code="stage_integrity_failed",
            category=StageErrorCategory.INTEGRITY,
            retryable=False,
            cause=StageFailureCause.RUNTIME_INTEGRITY,
            action="failed an integrity check",
        )
    return _stage_error(
        stage,
        error_code="stage_internal_error",
        category=StageErrorCategory.INTERNAL,
        retryable=False,
        cause=StageFailureCause.UNKNOWN,
        action="failed unexpectedly",
    )


def _stage_error(
    stage: JobStatus,
    *,
    error_code: str,
    category: StageErrorCategory,
    retryable: bool,
    cause: StageFailureCause,
    action: str,
) -> StageExecutionError:
    return StageExecutionError(
        stage,
        error_code=error_code,
        category=category,
        retryable=retryable,
        cause=cause,
        safe_message=f"Worker stage {stage.value} {action}.",
    )


def _find_cause(
    exc: BaseException,
    exception_type: type[ExceptionT],
) -> ExceptionT | None:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if isinstance(current, exception_type):
            return current
        visited.add(id(current))
        current = current.__cause__
    return None
