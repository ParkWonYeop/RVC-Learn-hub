from __future__ import annotations

import hashlib
import logging
import os
import socket
from typing import Any, cast

from redis import Redis
from rq import Queue, Worker
from rq.job import Job, JobStatus
from rq.registry import FailedJobRegistry, FinishedJobRegistry, StartedJobRegistry
from rq.serializers import JSONSerializer
from rq.utils import now
from rq.worker import WorkerStatus

from .config import Settings
from .logging_config import configure_logging
from .maintenance_queue import (
    MAINTENANCE_FAILURE_TTL_SECONDS,
    maintenance_job_is_allowlisted,
)

LOGGER = logging.getLogger("rvc_manager_api.maintenance_worker")
_POLICY_FAILURE = "Maintenance job rejected by execution policy"


class AllowlistedMaintenanceWorker(Worker):
    """RQ worker that treats Redis as untrusted task-envelope storage."""

    def __init__(self, queues: list[Queue], settings: Settings, **kwargs: Any) -> None:
        self.settings = settings
        # RQ's default initialization runs CLIENT SETNAME then CLIENT LIST only
        # to discover its own address. Do not grant connection-enumeration ACLs
        # to the maintenance identity; preserve non-secret local provenance.
        kwargs["prepare_for_work"] = False
        super().__init__(queues, **kwargs)
        self.hostname = socket.gethostname()
        self.pid = os.getpid()
        self.ip_address = "unknown"

    def _job_reference(self, job: Job) -> str:
        value = job.id if isinstance(job.id, str) else "invalid"
        return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]

    def subscribe(self) -> None:
        """Disable Redis pub/sub commands for this non-interactive worker."""

        self.pubsub = None
        self.pubsub_thread = None

    def unsubscribe(self) -> None:
        """Match the disabled subscription without touching Redis channels."""

    def check_for_suspension(self, burst: bool) -> None:
        """Do not accept Redis-controlled global suspension commands."""

        del burst

    def run_maintenance_tasks(self) -> None:
        """Expire inert terminal indexes without resolving Redis job callables.

        RQ's generic maintenance path loads expired started/deferred jobs and may
        execute failure callbacks or enqueue dependents. Redis is an untrusted
        envelope store here, so abandoned execution repair belongs to the
        PostgreSQL-ledger reconciler and operator review. Finished/failed
        cleanup only removes expired sorted-set members and never fetches a Job.
        """

        last_cleaned_at = cast(Any, self.last_cleaned_at)
        scheduler = cast(Any, self.scheduler)
        if last_cleaned_at and scheduler and (
            not scheduler._process or not scheduler._process.is_alive()
        ):
            scheduler.acquire_locks(auto_start=True)
        for queue in self.queues:
            FinishedJobRegistry(
                queue.name,
                connection=self.connection,
                job_class=self.job_class,
                serializer=self.serializer,
            ).cleanup()
            FailedJobRegistry(
                queue.name,
                connection=self.connection,
                job_class=self.job_class,
                serializer=self.serializer,
            ).cleanup()
        self.last_cleaned_at = cast(Any, now())

    def _reject_job(self, job: Job, queue: Queue, *, execution_prepared: bool) -> None:
        reference = self._job_reference(job)
        LOGGER.warning("maintenance job rejected", extra={"job_reference": reference})
        try:
            rejected_at = now()
            job.origin = self.settings.rq_queue_name
            job.started_at = job.started_at or rejected_at
            job.ended_at = rejected_at
            job.retries_left = 0
            job.failure_ttl = MAINTENANCE_FAILURE_TTL_SECONDS
            with self.connection.pipeline() as pipeline:
                job.set_status(JobStatus.FAILED, pipeline=pipeline)
                pipeline.lrem(queue.intermediate_queue_key, 0, job.id)
                if execution_prepared:
                    self.cleanup_execution(job, pipeline=pipeline)
                else:
                    self.set_current_job_id(None, pipeline=pipeline)
                job._handle_failure(
                    _POLICY_FAILURE,
                    pipeline=pipeline,
                    worker_name=self.name,
                )
                self.increment_failed_job_count(pipeline=pipeline)
                pipeline.execute()
        except Exception:
            LOGGER.exception(
                "maintenance job rejection could not be persisted",
                extra={"job_reference": reference},
            )

    def execute_job(self, job: Job, queue: Queue) -> None:
        if not maintenance_job_is_allowlisted(job, queue, self.settings):
            self._reject_job(job, queue, execution_prepared=False)
            try:
                self.set_state(WorkerStatus.IDLE)
            except Exception:
                LOGGER.exception("maintenance worker could not restore idle state")
            return
        super().execute_job(job, queue)

    def perform_job(self, job: Job, queue: Queue) -> bool:
        if not maintenance_job_is_allowlisted(job, queue, self.settings):
            self._reject_job(job, queue, execution_prepared=True)
            return False
        return super().perform_job(job, queue)

    def handle_job_success(
        self,
        job: Job,
        queue: Queue,
        started_job_registry: StartedJobRegistry,
    ) -> None:
        """Persist success without honoring Redis-injected dependents or repeats."""

        del queue, started_job_registry
        with self.connection.pipeline() as pipeline:
            self.increment_successful_job_count(pipeline=pipeline)
            if job.started_at is not None and job.ended_at is not None:
                self.increment_total_working_time(job.ended_at - job.started_at, pipeline)
            result_ttl = job.get_result_ttl(self.default_result_ttl)
            if result_ttl != 0:
                job._handle_success(result_ttl, pipeline=pipeline, worker_name=self.name)
            job.cleanup(result_ttl, pipeline=pipeline, remove_from_queue=False)
            self.cleanup_execution(job, pipeline=pipeline)
            pipeline.execute()

    def handle_job_failure(
        self,
        job: Job,
        queue: Queue,
        started_job_registry: StartedJobRegistry | None = None,
        exc_string: str = "",
    ) -> None:
        """Persist failure/retry without honoring Redis-injected dependents."""

        del started_job_registry
        with self.connection.pipeline() as pipeline:
            job_is_stopped = self._stopped_job_id == job.id
            retry = job.should_retry and not job_is_stopped
            if job_is_stopped:
                job.set_status(JobStatus.STOPPED, pipeline=pipeline)
                self._stopped_job_id = None
            elif not retry:
                job.set_status(JobStatus.FAILED, pipeline=pipeline)

            self.cleanup_execution(job, pipeline=pipeline)
            if not self.disable_default_exception_handler and not retry:
                job._handle_failure(exc_string, pipeline=pipeline, worker_name=self.name)
            self.increment_failed_job_count(pipeline=pipeline)
            if job.started_at is not None and job.ended_at is not None:
                self.increment_total_working_time(job.ended_at - job.started_at, pipeline)
            if retry:
                job.retry(queue, pipeline)
            pipeline.execute()


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    if settings.process_role != "maintenance":
        raise SystemExit("RQ worker requires PROCESS_ROLE=maintenance")
    if not settings.rq_enabled or not settings.redis_url:
        raise SystemExit("RQ_ENABLED=true and REDIS_URL are required")
    connection = Redis.from_url(settings.redis_url)
    queue = Queue(
        settings.rq_queue_name,
        connection=connection,
        serializer=JSONSerializer,
    )
    hostname_digest = hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()
    worker = AllowlistedMaintenanceWorker(
        [queue],
        settings,
        name=f"rvc-maintenance-{hostname_digest}",
        connection=connection,
        worker_ttl=settings.rq_worker_ttl_seconds,
        job_monitoring_interval=min(30, settings.rq_worker_ttl_seconds - 15),
        serializer=JSONSerializer,
        log_job_description=False,
    )
    # The scheduler only promotes due bounded retries. New maintenance runs are
    # still created exclusively through the admin API, and every promoted job
    # passes the dequeue/perform execution policy again.
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
