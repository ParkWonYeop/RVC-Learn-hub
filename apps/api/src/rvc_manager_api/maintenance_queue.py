from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

import anyio
from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from rq import Queue, Retry
from rq.exceptions import NoSuchJobError
from rq.job import Job, JobStatus
from rq.registry import (
    CanceledJobRegistry,
    DeferredJobRegistry,
    FailedJobRegistry,
    FinishedJobRegistry,
    ScheduledJobRegistry,
)
from rq.serializers import JSONSerializer

from .config import Settings

LOGGER = logging.getLogger("rvc_manager_api.maintenance_queue")

DATASET_CLEANUP_TASK_PATH = "rvc_manager_api.maintenance_tasks.execute_dataset_staging_cleanup"
TEST_SET_CLEANUP_TASK_PATH = "rvc_manager_api.maintenance_tasks.execute_test_set_staging_cleanup"
MAINTENANCE_JOB_DESCRIPTION = "allowlisted Dataset staging cleanup"
TEST_SET_MAINTENANCE_JOB_DESCRIPTION = "allowlisted TestSet staging cleanup"
_TASK_ENVELOPES = {
    "dataset_staging_cleanup": (
        DATASET_CLEANUP_TASK_PATH,
        MAINTENANCE_JOB_DESCRIPTION,
    ),
    "test_set_staging_cleanup": (
        TEST_SET_CLEANUP_TASK_PATH,
        TEST_SET_MAINTENANCE_JOB_DESCRIPTION,
    ),
}
MAINTENANCE_RESULT_TTL_SECONDS = 86_400
MAINTENANCE_FAILURE_TTL_SECONDS = 604_800
MAINTENANCE_JOB_TTL_SECONDS = 86_400
MAINTENANCE_QUARANTINE_MAX_WIP_ENTRIES = 10_000
_JOB_ID = re.compile(r"^rvc-maintenance-[0-9a-f]{64}$")
_QUARANTINE_JOB_LUA = """
local status = redis.call('HGET', KEYS[1], 'status')
if status == 'started' then
  return 'active'
end
if redis.call('LPOS', KEYS[5], ARGV[1]) then
  return 'active'
end
if redis.call('EXISTS', KEYS[12]) == 1 or redis.call('EXISTS', KEYS[13]) == 1 then
  return 'execution_material'
end
local wip_count = redis.call('ZCARD', KEYS[11])
if wip_count > tonumber(ARGV[2]) then
  return 'execution_material'
end
local execution_prefix = ARGV[1] .. ':'
local wip_entries = redis.call('ZRANGE', KEYS[11], 0, -1)
for _, entry in ipairs(wip_entries) do
  if string.sub(entry, 1, string.len(execution_prefix)) == execution_prefix then
    return 'execution_material'
  end
end
redis.call('LREM', KEYS[4], 0, ARGV[1])
redis.call('LREM', KEYS[5], 0, ARGV[1])
for index = 6, 10 do
  redis.call('ZREM', KEYS[index], ARGV[1])
end
redis.call('DEL', KEYS[1], KEYS[2], KEYS[3])
return status or 'missing'
"""


class MaintenanceQueueUnavailable(RuntimeError):
    pass


class MaintenanceQueueEnvelopeConflict(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__("maintenance queue envelope requires operator review")
        self.code = code


@dataclass(frozen=True, slots=True)
class EnqueuedMaintenanceTask:
    job_id: str
    existing: bool
    job_state: str = "queued"
    repaired: bool = False
    repair_code: str | None = None


class MaintenanceQueuePort(Protocol):
    async def enqueue_dataset_cleanup(
        self,
        *,
        run_id: str,
        job_id: str,
        max_attempts: int,
        create_if_missing: bool = True,
    ) -> EnqueuedMaintenanceTask: ...

    async def enqueue_test_set_cleanup(
        self,
        *,
        run_id: str,
        job_id: str,
        max_attempts: int,
        create_if_missing: bool = True,
    ) -> EnqueuedMaintenanceTask: ...

    async def close(self) -> None: ...


def retry_intervals(settings: Settings, max_attempts: int) -> list[int]:
    intervals: list[int] = []
    value = settings.maintenance_retry_backoff_seconds
    for _ in range(max(0, max_attempts - 1)):
        intervals.append(min(value, settings.maintenance_retry_backoff_max_seconds))
        value = min(value * 2, settings.maintenance_retry_backoff_max_seconds)
    return intervals


def _canonical_run_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def maintenance_job_is_allowlisted(
    job: Job,
    queue: Queue,
    settings: Settings,
    *,
    expected_run_id: str | None = None,
    expected_job_id: str | None = None,
    expected_max_attempts: int | None = None,
    expected_task_name: str | None = None,
) -> bool:
    """Validate Redis-controlled job data without resolving a callable or callback."""

    try:
        if job.serializer is not JSONSerializer or queue.serializer is not JSONSerializer:
            return False
        args = job.args
        kwargs = job.kwargs
        actual_intervals = job.retry_intervals or []
        if expected_max_attempts is None:
            retry_policy_valid = bool(
                job.number_of_retries is None
                and (
                    (not actual_intervals and job.retries_left is None)
                    or (
                        isinstance(job.retry_intervals, list)
                        and 0 < len(actual_intervals) < settings.maintenance_task_max_attempts
                        and all(type(interval) is int for interval in actual_intervals)
                        and actual_intervals == retry_intervals(settings, len(actual_intervals) + 1)
                        and type(job.retries_left) is int
                        and 0 <= job.retries_left <= len(actual_intervals)
                    )
                )
            )
        else:
            expected_intervals = retry_intervals(settings, expected_max_attempts)
            retry_policy_valid = bool(
                job.number_of_retries is None
                and actual_intervals == expected_intervals
                and all(type(interval) is int for interval in actual_intervals)
                and (
                    (not expected_intervals and job.retries_left is None)
                    or (
                        expected_intervals
                        and type(job.retries_left) is int
                        and 0 <= job.retries_left <= len(expected_intervals)
                    )
                )
            )
        callbacks_absent = all(
            getattr(job, field_name, None) is None
            for field_name in (
                "_success_callback_name",
                "_failure_callback_name",
                "_stopped_callback_name",
                "_success_callback_timeout",
                "_failure_callback_timeout",
                "_stopped_callback_timeout",
            )
        )
        dependency_sets_empty = (
            cast(int, job.connection.scard(job.dependencies_key)) == 0
            and cast(int, job.connection.scard(job.dependents_key)) == 0
        )
        expected_envelope = (
            _TASK_ENVELOPES.get(expected_task_name) if expected_task_name is not None else None
        )
        envelope = (job.func_name, job.description)
        task_envelope_valid = envelope in _TASK_ENVELOPES.values()
        if expected_task_name is not None:
            task_envelope_valid = expected_envelope is not None and envelope == expected_envelope
        return bool(
            queue.name == settings.rq_queue_name
            and job.origin == settings.rq_queue_name
            and _JOB_ID.fullmatch(job.id or "")
            and (expected_job_id is None or job.id == expected_job_id)
            and task_envelope_valid
            and job.instance is None
            and isinstance(args, (list, tuple))
            and len(args) == 1
            and _canonical_run_id(args[0])
            and (expected_run_id is None or args[0] == expected_run_id)
            and kwargs == {}
            and job.timeout == settings.maintenance_task_timeout_seconds
            and job.result_ttl == MAINTENANCE_RESULT_TTL_SECONDS
            and job.failure_ttl == MAINTENANCE_FAILURE_TTL_SECONDS
            and job.ttl == MAINTENANCE_JOB_TTL_SECONDS
            and retry_policy_valid
            and not job._dependency_ids
            and dependency_sets_empty
            and job.meta == {}
            and job.group_id is None
            and job.allow_dependency_failures is None
            and job.enqueue_at_front is None
            and job.repeats_left is None
            and job.repeat_intervals is None
            and callbacks_absent
        )
    except Exception:
        return False


class RqMaintenanceQueue:
    """Allowlisted RQ adapter; callers cannot choose a callable or task arguments."""

    def __init__(self, settings: Settings, *, connection: Redis | None = None) -> None:
        if not settings.redis_url:
            raise ValueError("REDIS_URL is required for the RQ maintenance queue")
        self.settings = settings
        self.connection = connection or Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=settings.rq_readiness_timeout_seconds,
            socket_timeout=settings.rq_readiness_timeout_seconds,
        )
        self.queue = Queue(
            settings.rq_queue_name,
            connection=self.connection,
            serializer=JSONSerializer,
        )

    def _job_status(self, job: Job) -> JobStatus:
        raw_status: object = self.connection.hget(job.key, "status")
        if isinstance(raw_status, bytes):
            raw_status = raw_status.decode("ascii", errors="strict")
        if not isinstance(raw_status, str):
            raise MaintenanceQueueEnvelopeConflict("maintenance_queue_job_state_invalid")
        try:
            return JobStatus(raw_status)
        except ValueError as exc:
            raise MaintenanceQueueEnvelopeConflict("maintenance_queue_job_state_invalid") from exc

    def _active_job_state(self, job: Job, status: JobStatus) -> str | None:
        if status == JobStatus.STARTED:
            return "started"
        if status == JobStatus.QUEUED:
            if self.connection.lpos(self.queue.intermediate_queue_key, job.id) is not None:
                return "started"
            if self.connection.lpos(self.queue.key, job.id) is not None:
                return "queued"
            return None
        if status == JobStatus.SCHEDULED:
            registry = ScheduledJobRegistry(  # type: ignore[no-untyped-call]
                self.queue.name,
                connection=self.connection,
                serializer=JSONSerializer,
            )
            if self.connection.zscore(registry.key, job.id) is not None:
                return "scheduled"
        return None

    def _quarantine_job(self, job: Job) -> None:
        registry_keys = [
            ScheduledJobRegistry(  # type: ignore[no-untyped-call]
                self.queue.name,
                connection=self.connection,
                serializer=JSONSerializer,
            ).key,
            DeferredJobRegistry(
                self.queue.name,
                connection=self.connection,
                serializer=JSONSerializer,
            ).key,
            FinishedJobRegistry(
                self.queue.name,
                connection=self.connection,
                serializer=JSONSerializer,
            ).key,
            FailedJobRegistry(
                self.queue.name,
                connection=self.connection,
                serializer=JSONSerializer,
            ).key,
            CanceledJobRegistry(
                self.queue.name,
                connection=self.connection,
                serializer=JSONSerializer,
            ).key,
        ]
        execution_material_keys = [
            f"rq:wip:{self.queue.name}",
            f"rq:executions:{job.id}",
            f"rq:results:{job.id}",
        ]
        result = self.connection.eval(
            _QUARANTINE_JOB_LUA,
            13,
            job.key,
            job.dependencies_key,
            job.dependents_key,
            self.queue.key,
            self.queue.intermediate_queue_key,
            *registry_keys,
            *execution_material_keys,
            job.id,
            str(MAINTENANCE_QUARANTINE_MAX_WIP_ENTRIES),
        )
        if isinstance(result, bytes):
            result = result.decode("ascii", errors="strict")
        if result == "active":
            raise MaintenanceQueueEnvelopeConflict("maintenance_queue_poisoned_active")
        if result == "execution_material":
            raise MaintenanceQueueEnvelopeConflict("maintenance_queue_poisoned_execution")

    def _existing_job(
        self,
        *,
        task_name: str,
        run_id: str,
        job_id: str,
        max_attempts: int,
    ) -> tuple[EnqueuedMaintenanceTask | None, str | None]:
        try:
            job = Job.fetch(
                job_id,
                connection=self.connection,
                serializer=JSONSerializer,
            )
        except NoSuchJobError:
            return None, None
        status = self._job_status(job)
        active_state = self._active_job_state(job, status)
        envelope_valid = maintenance_job_is_allowlisted(
            job,
            self.queue,
            self.settings,
            expected_run_id=run_id,
            expected_job_id=job_id,
            expected_max_attempts=max_attempts,
            expected_task_name=task_name,
        )
        if envelope_valid and active_state is not None:
            return (
                EnqueuedMaintenanceTask(
                    job_id=job_id,
                    existing=True,
                    job_state=active_state,
                ),
                None,
            )
        if active_state == "started":
            raise MaintenanceQueueEnvelopeConflict("maintenance_queue_poisoned_active")
        repair_code = (
            "maintenance_queue_envelope_mismatch"
            if not envelope_valid
            else "maintenance_queue_inactive_job"
        )
        self._quarantine_job(job)
        return None, repair_code

    def _enqueue_exact(
        self,
        *,
        task_name: str,
        run_id: str,
        job_id: str,
        max_attempts: int,
    ) -> None:
        try:
            task_path, description = _TASK_ENVELOPES[task_name]
        except KeyError as exc:
            raise MaintenanceQueueEnvelopeConflict("maintenance_queue_task_unsupported") from exc
        intervals = retry_intervals(self.settings, max_attempts)
        retry = Retry(max=len(intervals), interval=intervals) if intervals else None
        self.queue.enqueue_call(
            func=task_path,
            args=(run_id,),
            kwargs=None,
            timeout=self.settings.maintenance_task_timeout_seconds,
            result_ttl=MAINTENANCE_RESULT_TTL_SECONDS,
            failure_ttl=MAINTENANCE_FAILURE_TTL_SECONDS,
            ttl=MAINTENANCE_JOB_TTL_SECONDS,
            description=description,
            depends_on=None,
            job_id=job_id,
            at_front=False,
            meta=None,
            retry=retry,
            repeat=None,
            on_success=None,
            on_failure=None,
            on_stopped=None,
        )

    async def _enqueue_cleanup(
        self,
        *,
        task_name: str,
        run_id: str,
        job_id: str,
        max_attempts: int,
        create_if_missing: bool = True,
    ) -> EnqueuedMaintenanceTask:
        def enqueue() -> EnqueuedMaintenanceTask:
            if not _canonical_run_id(run_id) or not _JOB_ID.fullmatch(job_id):
                raise MaintenanceQueueEnvelopeConflict("maintenance_queue_identity_invalid")
            if max_attempts < 1 or max_attempts > self.settings.maintenance_task_max_attempts:
                raise MaintenanceQueueEnvelopeConflict("maintenance_queue_retry_policy_unsupported")
            queue_lock = self.connection.lock(
                f"rvc:maintenance:enqueue:{job_id}",
                timeout=max(30.0, self.settings.rq_readiness_timeout_seconds * 12),
                blocking_timeout=self.settings.rq_readiness_timeout_seconds,
            )
            acquired = queue_lock.acquire(blocking=True)
            if not acquired:
                raise RuntimeError("maintenance queue coordination is busy")
            try:
                existing, repair_code = self._existing_job(
                    task_name=task_name,
                    run_id=run_id,
                    job_id=job_id,
                    max_attempts=max_attempts,
                )
                if existing is not None:
                    return existing
                if not create_if_missing:
                    return EnqueuedMaintenanceTask(
                        job_id=job_id,
                        existing=False,
                        job_state="missing",
                        repaired=repair_code is not None,
                        repair_code=repair_code,
                    )
                try:
                    self._enqueue_exact(
                        task_name=task_name,
                        run_id=run_id,
                        job_id=job_id,
                        max_attempts=max_attempts,
                    )
                except Exception:
                    raced, _ = self._existing_job(
                        task_name=task_name,
                        run_id=run_id,
                        job_id=job_id,
                        max_attempts=max_attempts,
                    )
                    if raced is not None:
                        return raced
                    raise
                return EnqueuedMaintenanceTask(
                    job_id=job_id,
                    existing=False,
                    job_state="queued",
                    repaired=repair_code is not None,
                    repair_code=repair_code,
                )
            finally:
                try:
                    queue_lock.release()
                except Exception:
                    LOGGER.warning("maintenance queue coordination lock release failed")

        try:
            return await anyio.to_thread.run_sync(enqueue)
        except MaintenanceQueueEnvelopeConflict:
            raise
        except Exception as exc:
            raise MaintenanceQueueUnavailable("maintenance queue is unavailable") from exc

    async def enqueue_dataset_cleanup(
        self,
        *,
        run_id: str,
        job_id: str,
        max_attempts: int,
        create_if_missing: bool = True,
    ) -> EnqueuedMaintenanceTask:
        return await self._enqueue_cleanup(
            task_name="dataset_staging_cleanup",
            run_id=run_id,
            job_id=job_id,
            max_attempts=max_attempts,
            create_if_missing=create_if_missing,
        )

    async def enqueue_test_set_cleanup(
        self,
        *,
        run_id: str,
        job_id: str,
        max_attempts: int,
        create_if_missing: bool = True,
    ) -> EnqueuedMaintenanceTask:
        return await self._enqueue_cleanup(
            task_name="test_set_staging_cleanup",
            run_id=run_id,
            job_id=job_id,
            max_attempts=max_attempts,
            create_if_missing=create_if_missing,
        )

    async def close(self) -> None:
        await anyio.to_thread.run_sync(self.connection.close)


class RqReadinessProbe:
    def __init__(
        self,
        settings: Settings,
        *,
        connection: AsyncRedis | None = None,
    ) -> None:
        if not settings.redis_url:
            raise ValueError("REDIS_URL is required for RQ readiness")
        self.settings = settings
        self.connection = connection or AsyncRedis.from_url(
            settings.redis_url,
            decode_responses=True,
        )

    async def readiness(self) -> tuple[str, bool]:
        try:
            async with asyncio.timeout(self.settings.rq_readiness_timeout_seconds):
                await self.connection.ping()
                worker_keys = await cast(
                    Awaitable[set[str]],
                    self.connection.smembers(f"rq:workers:{self.settings.rq_queue_name}"),
                )
                if not worker_keys:
                    return "no_worker", False
                heartbeats = await asyncio.gather(
                    *(
                        cast(
                            Awaitable[str | None],
                            self.connection.hget(str(worker_key), "last_heartbeat"),
                        )
                        for worker_key in worker_keys
                    )
                )
        except Exception:
            return "unavailable", False

        now = datetime.now(UTC)
        max_age = timedelta(seconds=self.settings.rq_worker_heartbeat_max_age_seconds)
        for raw in heartbeats:
            parsed = _parse_rq_heartbeat(raw)
            if parsed is None:
                continue
            if now - max_age <= parsed <= now + max_age:
                return "ok", True
        return "stale", False

    async def close(self) -> None:
        await self.connection.aclose()


def _parse_rq_heartbeat(raw: object) -> datetime | None:
    if not isinstance(raw, (str, bytes)):
        return None
    value = raw.decode("utf-8", errors="strict") if isinstance(raw, bytes) else raw
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
