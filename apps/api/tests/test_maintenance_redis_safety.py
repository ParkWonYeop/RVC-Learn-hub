from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from redis import Redis
from rq import Queue, Retry, Worker
from rq.group import Group
from rq.job import Job
from rq.registry import DeferredJobRegistry, StartedJobRegistry
from rq.serializers import JSONSerializer

from rvc_manager_api.config import Settings
from rvc_manager_api.maintenance_queue import (
    _QUARANTINE_JOB_LUA,
    DATASET_CLEANUP_TASK_PATH,
    MAINTENANCE_QUARANTINE_MAX_WIP_ENTRIES,
    MaintenanceQueueEnvelopeConflict,
    RqMaintenanceQueue,
)
from rvc_manager_api.rq_worker import AllowlistedMaintenanceWorker


def _settings() -> Settings:
    return Settings(
        process_role="maintenance",
        rq_enabled=True,
        redis_url="redis://127.0.0.1:6379/0",
    )


def _job() -> tuple[Job, Queue, Mock]:
    connection = Mock(spec=Redis)
    connection.scard.return_value = 0
    queue = Queue("rvc-maintenance", connection=connection, serializer=JSONSerializer)
    job = queue.create_job(
        DATASET_CLEANUP_TASK_PATH,
        args=("40000000-0000-4000-8000-000000000001",),
        kwargs=None,
        result_ttl=86_400,
        ttl=86_400,
        description="allowlisted Dataset staging cleanup",
        timeout=300,
        job_id=f"rvc-maintenance-{'a' * 64}",
        failure_ttl=604_800,
        retry=Retry(max=2, interval=[30, 60]),
    )
    return job, queue, connection


def test_worker_periodic_cleanup_never_enters_rq_callable_loading_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden = Mock(side_effect=AssertionError("generic RQ cleanup must not run"))
    monkeypatch.setattr(Worker, "clean_registries", forbidden)
    monkeypatch.setattr(Group, "clean_registries", forbidden)
    monkeypatch.setattr(StartedJobRegistry, "cleanup", forbidden)
    monkeypatch.setattr(DeferredJobRegistry, "cleanup", forbidden)

    terminal_cleanup_calls: list[str] = []

    class SafeFinishedRegistry:
        def __init__(self, name: str, **_kwargs: object) -> None:
            self.name = name

        def cleanup(self) -> None:
            terminal_cleanup_calls.append(f"finished:{self.name}")

    class SafeFailedRegistry:
        def __init__(self, name: str, **_kwargs: object) -> None:
            self.name = name

        def cleanup(self) -> None:
            terminal_cleanup_calls.append(f"failed:{self.name}")

    monkeypatch.setattr("rvc_manager_api.rq_worker.FinishedJobRegistry", SafeFinishedRegistry)
    monkeypatch.setattr("rvc_manager_api.rq_worker.FailedJobRegistry", SafeFailedRegistry)

    worker = object.__new__(AllowlistedMaintenanceWorker)
    worker.queues = [SimpleNamespace(name="rvc-maintenance")]
    worker.connection = Mock(spec=Redis)
    worker.job_class = Job
    worker.serializer = JSONSerializer
    worker.last_cleaned_at = None
    worker.scheduler = None

    worker.run_maintenance_tasks()

    assert terminal_cleanup_calls == [
        "finished:rvc-maintenance",
        "failed:rvc-maintenance",
    ]
    assert worker.last_cleaned_at is not None
    forbidden.assert_not_called()


def test_worker_periodic_cleanup_reacquires_dead_retry_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_registry = Mock()
    monkeypatch.setattr("rvc_manager_api.rq_worker.FinishedJobRegistry", terminal_registry)
    monkeypatch.setattr("rvc_manager_api.rq_worker.FailedJobRegistry", terminal_registry)

    worker = object.__new__(AllowlistedMaintenanceWorker)
    worker.queues = []
    worker.last_cleaned_at = object()
    process = Mock()
    process.is_alive.return_value = False
    worker.scheduler = SimpleNamespace(process=process, _process=process, acquire_locks=Mock())

    worker.run_maintenance_tasks()

    worker.scheduler.acquire_locks.assert_called_once_with(auto_start=True)
    terminal_registry.assert_not_called()


def test_worker_disables_redis_pubsub_and_global_suspension() -> None:
    worker = object.__new__(AllowlistedMaintenanceWorker)
    connection = Mock(spec=Redis)
    connection.pubsub.side_effect = AssertionError("pubsub must stay disabled")
    worker.connection = connection
    worker.pubsub = object()
    worker.pubsub_thread = object()

    worker.subscribe()
    worker.check_for_suspension(burst=False)
    worker.unsubscribe()

    assert worker.pubsub is None
    assert worker.pubsub_thread is None
    connection.pubsub.assert_not_called()


@pytest.mark.asyncio
async def test_quarantine_refuses_inactive_job_with_execution_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, _, connection = _job()
    connection.hget.return_value = b"finished"
    connection.eval.return_value = b"execution_material"
    monkeypatch.setattr(Job, "fetch", lambda *_args, **_kwargs: job)
    adapter = RqMaintenanceQueue(_settings(), connection=connection)
    enqueue_call = Mock()
    monkeypatch.setattr(adapter.queue, "enqueue_call", enqueue_call)

    with pytest.raises(MaintenanceQueueEnvelopeConflict) as exc_info:
        await adapter.enqueue_dataset_cleanup(
            run_id="40000000-0000-4000-8000-000000000001",
            job_id=job.id,
            max_attempts=3,
        )

    assert exc_info.value.code == "maintenance_queue_poisoned_execution"
    enqueue_call.assert_not_called()


def test_quarantine_atomically_checks_bounded_wip_execution_and_results() -> None:
    job, _, connection = _job()
    connection.eval.return_value = b"finished"
    adapter = RqMaintenanceQueue(_settings(), connection=connection)

    adapter._quarantine_job(job)

    call = connection.eval.call_args.args
    assert call[1] == 13
    assert call[-5:] == (
        "rq:wip:rvc-maintenance",
        f"rq:executions:{job.id}",
        f"rq:results:{job.id}",
        job.id,
        str(MAINTENANCE_QUARANTINE_MAX_WIP_ENTRIES),
    )
    assert "ZCARD" in _QUARANTINE_JOB_LUA
    assert "ZRANGE" in _QUARANTINE_JOB_LUA
    assert "EXISTS" in _QUARANTINE_JOB_LUA
