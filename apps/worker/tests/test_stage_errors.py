from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from rvc_orchestrator_contracts import JobStatus
from rvc_worker.client import ManagerClientError
from rvc_worker.process import ProcessFailed, ProcessTimedOut
from rvc_worker.runner import RvcRunContext, StageResult
from rvc_worker.stages import (
    STAGE_EXECUTION_POLICIES,
    InternalRetryScope,
    StageErrorCategory,
    StageExecutionCancelled,
    StageExecutionError,
    StageExecutor,
    StageFailureCause,
    build_stage_plan,
    classify_stage_exception,
)
from rvc_worker.telemetry import TelemetrySpoolError
from rvc_worker.test_sets import (
    TestSetMaterializationTimeout as MaterializationTimeout,
)
from rvc_worker.workspace import WorkspaceManager

from .helpers import make_claim


class FailingRunner:
    def __init__(
        self,
        target: JobStatus,
        failure: Callable[[asyncio.Event], BaseException],
    ) -> None:
        self.target = target
        self.failure = failure
        self.calls: list[JobStatus] = []

    async def run_stage(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        del context
        self.calls.append(stage)
        if stage is self.target:
            raise self.failure(cancellation)
        return StageResult()


async def _update_status(job_id: str, update: object) -> None:
    del job_id, update


def _workspace(root: Path):
    claim = make_claim(samples=True)
    return claim, WorkspaceManager(root).prepare(claim.job_id, claim.attempt_id)


_ALL_STAGES = build_stage_plan(make_claim(samples=True))


def test_every_stage_has_no_replay_with_only_explicit_transfer_retry_scopes() -> None:
    assert set(_ALL_STAGES).issubset(STAGE_EXECUTION_POLICIES)
    assert all(policy.executor_max_attempts == 1 for policy in STAGE_EXECUTION_POLICIES.values())
    assert STAGE_EXECUTION_POLICIES[
        JobStatus.DOWNLOADING_DATASET
    ].internal_retry_scopes == (
        InternalRetryScope.DATASET_TRANSFER,
        InternalRetryScope.TEST_SET_TRANSFER,
    )
    assert STAGE_EXECUTION_POLICIES[
        JobStatus.UPLOADING_ARTIFACTS
    ].internal_retry_scopes == (InternalRetryScope.ARTIFACT_TRANSFER,)
    assert all(
        not policy.internal_retry_scopes
        for stage, policy in STAGE_EXECUTION_POLICIES.items()
        if stage
        not in {JobStatus.DOWNLOADING_DATASET, JobStatus.UPLOADING_ARTIFACTS}
    )


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(400, False), (409, False), (429, True), (500, True), (503, True)],
)
def test_manager_status_has_stable_transient_metadata(
    status_code: int,
    retryable: bool,
) -> None:
    failure = ManagerClientError("raw remote response must stay internal", status_code=status_code)
    assert failure.retryable is retryable
    assert failure.category == ("transport" if retryable else "protocol")


def test_network_failure_requires_explicit_transport_metadata() -> None:
    failure = ManagerClientError(
        "ConnectError for https://manager.example/private?token=secret",
        retryable=True,
        category="transport",
    )
    assert failure.status_code is None
    assert failure.retryable is True
    assert failure.category == "transport"


def test_whole_test_set_deadline_is_typed_as_timeout() -> None:
    failure = classify_stage_exception(
        JobStatus.DOWNLOADING_DATASET,
        MaterializationTimeout("private path must not escape"),
    )

    assert failure.error_code == "stage_timeout"
    assert failure.category is StageErrorCategory.TIMEOUT
    assert failure.cause is StageFailureCause.TEST_SET_TIMEOUT
    assert "private path" not in failure.safe_message


@pytest.mark.asyncio
@pytest.mark.parametrize("target", _ALL_STAGES)
async def test_process_timeout_maps_for_every_stage_without_raw_details(
    tmp_path: Path,
    target: JobStatus,
) -> None:
    claim, workspace = _workspace(tmp_path / target.value)
    runner = FailingRunner(
        target,
        lambda _: ProcessTimedOut(
            "Bearer secret-token /private/job/path argv=python --credential hidden"
        ),
    )

    with pytest.raises(StageExecutionError) as raised:
        await StageExecutor(runner, _update_status).execute(
            claim,
            workspace,
            asyncio.Event(),
        )

    failure = raised.value
    assert failure.stage is target
    assert failure.error_code == "stage_timeout"
    assert failure.category is StageErrorCategory.TIMEOUT
    assert failure.retryable is False
    assert failure.cause is StageFailureCause.PROCESS_TIMEOUT
    assert runner.calls.count(target) == 1
    assert "secret-token" not in failure.safe_message
    assert "/private/job/path" not in failure.safe_message
    assert "argv" not in failure.safe_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [JobStatus.TRAINING, JobStatus.SAVING_CHECKPOINT, JobStatus.BUILDING_INDEX],
)
async def test_nonzero_training_checkpoint_and_index_are_never_replayed(
    tmp_path: Path,
    target: JobStatus,
) -> None:
    claim, workspace = _workspace(tmp_path / target.value)
    runner = FailingRunner(
        target,
        lambda _: ProcessFailed(
            ("/private/runtime/python", "--token", "secret-token"),
            17,
        ),
    )

    with pytest.raises(StageExecutionError) as raised:
        await StageExecutor(runner, _update_status).execute(
            claim,
            workspace,
            asyncio.Event(),
        )

    failure = raised.value
    assert failure.error_code == "stage_process_failed"
    assert failure.category is StageErrorCategory.PROCESS
    assert failure.cause is StageFailureCause.PROCESS_NONZERO
    assert runner.calls.count(target) == 1
    assert "secret-token" not in failure.safe_message
    assert "/private/runtime" not in failure.safe_message


@pytest.mark.asyncio
async def test_unknown_exception_uses_safe_nonretryable_fallback(tmp_path: Path) -> None:
    claim, workspace = _workspace(tmp_path)
    runner = FailingRunner(
        JobStatus.PREPROCESSING,
        lambda _: RuntimeError(
            "Bearer secret-token in /private/workspace; argv=['python', '--password']"
        ),
    )

    with pytest.raises(StageExecutionError) as raised:
        await StageExecutor(runner, _update_status).execute(
            claim,
            workspace,
            asyncio.Event(),
        )

    failure = raised.value
    assert failure.error_code == "stage_internal_error"
    assert failure.category is StageErrorCategory.INTERNAL
    assert failure.retryable is False
    assert failure.cause is StageFailureCause.UNKNOWN
    assert failure.safe_message == "Worker stage preprocessing failed unexpectedly."


@pytest.mark.asyncio
async def test_cancellation_has_precedence_over_simultaneous_process_failure(
    tmp_path: Path,
) -> None:
    claim, workspace = _workspace(tmp_path)

    def cancel_then_fail(cancellation: asyncio.Event) -> BaseException:
        cancellation.set()
        return ProcessFailed(("/private/runtime/python",), 9)

    runner = FailingRunner(JobStatus.TRAINING, cancel_then_fail)
    cancellation = asyncio.Event()

    with pytest.raises(StageExecutionCancelled) as raised:
        await StageExecutor(runner, _update_status).execute(
            claim,
            workspace,
            cancellation,
        )

    assert raised.value.stage is JobStatus.TRAINING
    assert raised.value.error_code == "stage_cancelled"
    assert runner.calls.count(JobStatus.TRAINING) == 1


@pytest.mark.asyncio
async def test_transient_manager_failure_is_typed_without_remote_message(
    tmp_path: Path,
) -> None:
    claim, workspace = _workspace(tmp_path)
    runner = FailingRunner(
        JobStatus.DOWNLOADING_DATASET,
        lambda _: ManagerClientError(
            "GET https://manager.example/private/path?token=secret-token failed",
            status_code=503,
        ),
    )

    with pytest.raises(StageExecutionError) as raised:
        await StageExecutor(runner, _update_status).execute(
            claim,
            workspace,
            asyncio.Event(),
        )

    failure = raised.value
    assert failure.error_code == "exhausted_transient"
    assert failure.category is StageErrorCategory.TRANSIENT
    assert failure.retryable is True
    assert "manager.example" not in failure.safe_message
    assert "secret-token" not in failure.safe_message


@pytest.mark.asyncio
async def test_telemetry_persistence_failure_is_not_retried_or_leaked(
    tmp_path: Path,
) -> None:
    claim, workspace = _workspace(tmp_path)
    runner = FailingRunner(JobStatus.COMPLETED, lambda _: AssertionError())
    callback_calls = 0

    async def fail_telemetry(*args: object) -> None:
        nonlocal callback_calls
        del args
        callback_calls += 1
        raise TelemetrySpoolError(
            "spool /private/workspace contains credential secret-token"
        )

    with pytest.raises(StageExecutionError) as raised:
        await StageExecutor(runner, _update_status, fail_telemetry).execute(
            claim,
            workspace,
            asyncio.Event(),
        )

    failure = raised.value
    assert failure.error_code == "telemetry_persistence_failed"
    assert failure.category is StageErrorCategory.TELEMETRY
    assert failure.retryable is False
    assert callback_calls == 1
    assert runner.calls.count(JobStatus.DOWNLOADING_DATASET) == 1
    assert "private" not in failure.safe_message
    assert "secret-token" not in failure.safe_message
