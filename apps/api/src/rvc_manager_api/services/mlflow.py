from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Protocol, cast

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rvc_orchestrator_contracts import JobStatus, MetricEntry, utc_now

from ..config import Settings
from ..database import Database
from ..models import Artifact, Experiment, Job, MlflowSyncEvent

LOGGER = logging.getLogger("rvc_manager_api.mlflow")

EXPERIMENT_CREATED = "experiment.created"
JOB_CREATED = "job.created"
METRIC_BATCH = "metric.batch"
ARTIFACT_VERIFIED = "artifact.verified"
JOB_TERMINAL = "job.terminal"

_SENSITIVE_SEGMENTS = frozenset(
    {
        "authorization",
        "credential",
        "credentials",
        "d_path",
        "g_path",
        "password",
        "path",
        "secret",
        "token",
        "uri",
        "url",
    }
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~+/-]+=*|(?:password|secret|token|credential)\s*[:=])"
)
_MLFLOW_STATUS = {
    JobStatus.COMPLETED.value: "FINISHED",
    JobStatus.FAILED.value: "FAILED",
    JobStatus.CANCELLED.value: "KILLED",
}


class MlflowUnavailable(RuntimeError):
    """A deliberately detail-free error that cannot leak tracking credentials or URIs."""

    def __init__(self, code: str = "backend_unavailable") -> None:
        self.code = code
        super().__init__(code)


class MlflowProjectionRequired(RuntimeError):
    """Fail-closed signal raised only after the ledger and outbox are durable."""

    def __init__(self, event: ProjectionEvent) -> None:
        self.event_key = event.event_key
        self.aggregate_type = event.aggregate_type
        self.aggregate_id = event.aggregate_id
        super().__init__("MLflow projection is required but currently unavailable")


class MlflowAdapter(Protocol):
    async def health(self) -> None: ...

    async def project(self, event: ProjectionEvent) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProjectionEvent:
    id: str
    event_key: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any]
    attempt_count: int


def _safe_utf8(value: object, *, max_bytes: int = 250) -> str:
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (str, int, float)):
        text = str(value)
    else:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if "://" in text or _SENSITIVE_TEXT.search(text):
        return "[REDACTED]"
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[: max_bytes - 3].decode("utf-8", errors="ignore") + "..."


def _flatten_params(
    value: object,
    *,
    prefix: str = "",
    result: dict[str, str] | None = None,
) -> dict[str, str]:
    flattened = {} if result is None else result
    if isinstance(value, Mapping):
        for raw_key, child in sorted(value.items(), key=lambda item: str(item[0])):
            key = str(raw_key)
            if key.lower() in _SENSITIVE_SEGMENTS:
                continue
            child_prefix = f"{prefix}.{key}" if prefix else key
            _flatten_params(child, prefix=child_prefix, result=flattened)
    elif value is not None and prefix:
        flattened[_safe_utf8(prefix)] = _safe_utf8(value)
    return flattened


def _timestamp_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _event_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def metric_event_key(attempt_id: str, idempotency_key: str) -> str:
    return f"metric:{attempt_id}:{_event_digest(idempotency_key)}"


def artifact_event_key(artifact_id: str) -> str:
    return f"artifact:{artifact_id}"


def terminal_event_key(attempt_id: str, job_status: str) -> str:
    return f"terminal:{attempt_id}:{job_status}"


def _job_context(job: Job, experiment: Experiment) -> dict[str, Any]:
    return {
        "manager_experiment_id": experiment.id,
        "experiment_name": _safe_utf8(experiment.name),
        "job_id": job.id,
        "job_name": _safe_utf8(job.job_name),
        "dataset_id": job.dataset_id,
        "start_time_ms": _timestamp_ms(job.created_at),
        "params": _flatten_params(job.config_json),
    }


async def _experiment_for_job(session: AsyncSession, job: Job) -> Experiment:
    experiment = await session.get(Experiment, job.experiment_id)
    if experiment is None:
        raise RuntimeError("job references a missing experiment")
    return experiment


class MlflowRestAdapter:
    """Small MLflow REST 2.0 client with deterministic Manager identity tags."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if settings.mlflow_tracking_uri is None:
            raise ValueError("MLflow tracking URI is required")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if settings.mlflow_tracking_token is not None:
            headers["Authorization"] = f"Bearer {settings.mlflow_tracking_token.get_secret_value()}"
        self._base_url = settings.mlflow_tracking_uri.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=settings.mlflow_request_timeout_seconds,
            headers=headers,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
        allowed_statuses: frozenset[int] = frozenset(),
    ) -> tuple[int, dict[str, Any]]:
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}/api/2.0/mlflow/{path}",
                params=params,
                json=payload,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise MlflowUnavailable("request_failed") from exc
        if response.status_code in allowed_statuses:
            return response.status_code, {}
        if response.is_redirect:
            raise MlflowUnavailable("redirect_rejected")
        if not response.is_success:
            raise MlflowUnavailable(
                "backend_unavailable" if response.status_code >= 500 else "projection_rejected"
            )
        if not response.content:
            return response.status_code, {}
        try:
            decoded = response.json()
        except ValueError as exc:
            raise MlflowUnavailable("invalid_response") from exc
        if not isinstance(decoded, dict):
            raise MlflowUnavailable("invalid_response")
        return response.status_code, cast(dict[str, Any], decoded)

    async def health(self) -> None:
        await self._request(
            "POST",
            "experiments/search",
            payload={"max_results": 1},
        )

    async def _get_experiment_id(self, name: str) -> str | None:
        status_code, response = await self._request(
            "GET",
            "experiments/get-by-name",
            params={"experiment_name": name},
            allowed_statuses=frozenset({404}),
        )
        if status_code == 404:
            return None
        experiment = response.get("experiment")
        if not isinstance(experiment, dict) or not isinstance(experiment.get("experiment_id"), str):
            raise MlflowUnavailable("invalid_response")
        return cast(str, experiment["experiment_id"])

    async def _ensure_experiment(self, payload: Mapping[str, Any]) -> str:
        manager_id = _required_string(payload, "manager_experiment_id")
        name = f"rvc-manager-{manager_id}"
        experiment_id = await self._get_experiment_id(name)
        if experiment_id is None:
            try:
                _, response = await self._request(
                    "POST",
                    "experiments/create",
                    payload={
                        "name": name,
                        "tags": [
                            {"key": "rvc_manager_experiment_id", "value": manager_id},
                            {
                                "key": "rvc_manager_display_name",
                                "value": _safe_utf8(payload.get("experiment_name", "")),
                            },
                        ],
                    },
                )
                experiment_id_value = response.get("experiment_id")
                if not isinstance(experiment_id_value, str):
                    raise MlflowUnavailable("invalid_response")
                experiment_id = experiment_id_value
            except MlflowUnavailable as exc:
                # A concurrent replica may have created the deterministic name.
                experiment_id = await self._get_experiment_id(name)
                if experiment_id is None:
                    raise exc
        return experiment_id

    async def _find_run_id(self, experiment_id: str, job_id: str) -> str | None:
        _, response = await self._request(
            "POST",
            "runs/search",
            payload={
                "experiment_ids": [experiment_id],
                "filter": f"tags.rvc_manager_job_id = '{job_id}'",
                "max_results": 1,
                "run_view_type": "ALL",
            },
        )
        runs = response.get("runs", [])
        if not isinstance(runs, list) or not runs:
            return None
        first = runs[0]
        if not isinstance(first, dict) or not isinstance(first.get("info"), dict):
            raise MlflowUnavailable("invalid_response")
        run_id = first["info"].get("run_id")
        if not isinstance(run_id, str):
            raise MlflowUnavailable("invalid_response")
        return run_id

    async def _ensure_run(self, payload: Mapping[str, Any]) -> str:
        experiment_id = await self._ensure_experiment(payload)
        job_id = _required_string(payload, "job_id")
        run_id = await self._find_run_id(experiment_id, job_id)
        if run_id is not None:
            return run_id
        try:
            _, response = await self._request(
                "POST",
                "runs/create",
                payload={
                    "experiment_id": experiment_id,
                    "run_name": _safe_utf8(payload.get("job_name", job_id)),
                    "start_time": _required_int(payload, "start_time_ms"),
                    "tags": [
                        {"key": "rvc_manager_job_id", "value": job_id},
                        {
                            "key": "rvc_manager_experiment_id",
                            "value": _required_string(payload, "manager_experiment_id"),
                        },
                        {
                            "key": "rvc_manager_dataset_id",
                            "value": _required_string(payload, "dataset_id"),
                        },
                    ],
                },
            )
            run = response.get("run")
            if not isinstance(run, dict) or not isinstance(run.get("info"), dict):
                raise MlflowUnavailable("invalid_response")
            run_id_value = run["info"].get("run_id")
            if not isinstance(run_id_value, str):
                raise MlflowUnavailable("invalid_response")
            return run_id_value
        except MlflowUnavailable as exc:
            # A create response may be lost or a second API replica may win the race.
            run_id = await self._find_run_id(experiment_id, job_id)
            if run_id is None:
                raise exc
            return run_id

    async def _run_tags(self, run_id: str) -> dict[str, str]:
        _, response = await self._request("GET", "runs/get", params={"run_id": run_id})
        run = response.get("run")
        if not isinstance(run, dict):
            raise MlflowUnavailable("invalid_response")
        data = run.get("data", {})
        if not isinstance(data, dict):
            raise MlflowUnavailable("invalid_response")
        raw_tags = data.get("tags", [])
        if not isinstance(raw_tags, list):
            raise MlflowUnavailable("invalid_response")
        tags: dict[str, str] = {}
        for raw_tag in raw_tags:
            if not isinstance(raw_tag, dict):
                continue
            key, value = raw_tag.get("key"), raw_tag.get("value")
            if isinstance(key, str) and isinstance(value, str):
                tags[key] = value
        return tags

    async def _log_batch_once(
        self,
        run_id: str,
        *,
        marker_source: str,
        metrics: Sequence[Mapping[str, object]] = (),
        params: Sequence[Mapping[str, str]] = (),
        tags: Sequence[Mapping[str, str]] = (),
    ) -> None:
        marker_key = f"rvc_sync_{_event_digest(marker_source)[:32]}"
        if (await self._run_tags(run_id)).get(marker_key) == "1":
            return
        payload_tags = [*tags, {"key": marker_key, "value": "1"}]
        if len(metrics) + len(params) + len(payload_tags) > 1_000:
            raise MlflowUnavailable("projection_batch_too_large")
        await self._request(
            "POST",
            "runs/log-batch",
            payload={
                "run_id": run_id,
                "metrics": list(metrics),
                "params": list(params),
                "tags": payload_tags,
            },
        )

    async def _project_experiment(self, event: ProjectionEvent) -> None:
        await self._ensure_experiment(event.payload)

    async def _project_job(self, event: ProjectionEvent) -> None:
        run_id = await self._ensure_run(event.payload)
        raw_params = event.payload.get("params", {})
        if not isinstance(raw_params, dict):
            raise MlflowUnavailable("invalid_event")
        params = [
            {"key": _safe_utf8(key), "value": _safe_utf8(value)}
            for key, value in sorted(raw_params.items())
        ]
        chunks = [params[index : index + 99] for index in range(0, len(params), 99)] or [[]]
        for index, chunk in enumerate(chunks):
            await self._log_batch_once(
                run_id,
                marker_source=f"{event.event_key}:params:{index}",
                params=chunk,
            )

    async def _project_metrics(self, event: ProjectionEvent) -> None:
        run_id = await self._ensure_run(event.payload)
        attempt_number = _required_int(event.payload, "attempt_number")
        raw_metrics = event.payload.get("metrics")
        if not isinstance(raw_metrics, list):
            raise MlflowUnavailable("invalid_event")
        metrics: list[dict[str, object]] = []
        for raw_metric in raw_metrics:
            if not isinstance(raw_metric, dict):
                raise MlflowUnavailable("invalid_event")
            metrics.append(
                {
                    "key": _safe_utf8(
                        f"attempt_{attempt_number}.{_required_string(raw_metric, 'key')}"
                    ),
                    "value": _required_float(raw_metric, "value"),
                    "timestamp": _required_int(raw_metric, "timestamp"),
                    "step": _required_int(raw_metric, "step"),
                }
            )
        for index in range(0, len(metrics), 999):
            await self._log_batch_once(
                run_id,
                marker_source=f"{event.event_key}:metrics:{index // 999}",
                metrics=metrics[index : index + 999],
            )

    async def _project_artifact(self, event: ProjectionEvent) -> None:
        run_id = await self._ensure_run(event.payload)
        artifact = event.payload.get("artifact")
        if not isinstance(artifact, dict):
            raise MlflowUnavailable("invalid_event")
        artifact_id = _required_string(artifact, "id")
        key_prefix = (
            "rvc_artifact_"
            f"{_safe_utf8(artifact.get('type', 'unknown'), max_bytes=40)}_"
            f"{_required_string(artifact, 'sha256')[:12]}"
        )
        tags = [
            {"key": f"{key_prefix}_id", "value": artifact_id},
            {
                "key": f"{key_prefix}_filename",
                "value": _safe_utf8(artifact.get("filename", "")),
            },
            {
                "key": f"{key_prefix}_sha256",
                "value": _required_string(artifact, "sha256"),
            },
            {
                "key": f"{key_prefix}_size_bytes",
                "value": str(_required_int(artifact, "size_bytes")),
            },
            {
                "key": f"{key_prefix}_manager_path",
                "value": f"/api/v1/artifacts/{artifact_id}/download",
            },
        ]
        await self._log_batch_once(
            run_id,
            marker_source=event.event_key,
            tags=tags,
        )

    async def _project_terminal(self, event: ProjectionEvent) -> None:
        run_id = await self._ensure_run(event.payload)
        status = _required_string(event.payload, "status")
        mlflow_status = _MLFLOW_STATUS.get(status)
        if mlflow_status is None:
            raise MlflowUnavailable("invalid_event")
        marker_key = f"rvc_sync_{_event_digest(event.event_key)[:32]}"
        if (await self._run_tags(run_id)).get(marker_key) == "1":
            return
        await self._request(
            "POST",
            "runs/update",
            payload={
                "run_id": run_id,
                "status": mlflow_status,
                "end_time": _required_int(event.payload, "end_time_ms"),
            },
        )
        await self._request(
            "POST",
            "runs/set-tag",
            payload={"run_id": run_id, "key": marker_key, "value": "1"},
        )

    async def project(self, event: ProjectionEvent) -> None:
        if event.event_type == EXPERIMENT_CREATED:
            await self._project_experiment(event)
        elif event.event_type == JOB_CREATED:
            await self._project_job(event)
        elif event.event_type == METRIC_BATCH:
            await self._project_metrics(event)
        elif event.event_type == ARTIFACT_VERIFIED:
            await self._project_artifact(event)
        elif event.event_type == JOB_TERMINAL:
            await self._project_terminal(event)
        else:
            raise MlflowUnavailable("invalid_event")


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise MlflowUnavailable("invalid_event")
    return value


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise MlflowUnavailable("invalid_event")
    return value


def _required_float(payload: Mapping[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MlflowUnavailable("invalid_event")
    return float(value)


class MlflowCoordinator:
    """Writes a transactional outbox and projects it without owning source-of-truth state."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        adapter: MlflowAdapter | None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.adapter = adapter
        self._stop = asyncio.Event()
        self._projection_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.settings.mlflow_enabled and self.adapter is not None

    def _enqueue(
        self,
        session: AsyncSession,
        *,
        event_key: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> str | None:
        if not self.enabled:
            return None
        session.add(
            MlflowSyncEvent(
                event_key=event_key,
                event_type=event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                payload_json=payload,
                status="pending",
                attempt_count=0,
            )
        )
        return event_key

    def enqueue_experiment_created(
        self,
        session: AsyncSession,
        experiment: Experiment,
    ) -> str | None:
        return self._enqueue(
            session,
            event_key=f"experiment:{experiment.id}",
            event_type=EXPERIMENT_CREATED,
            aggregate_type="experiment",
            aggregate_id=experiment.id,
            payload={
                "manager_experiment_id": experiment.id,
                "experiment_name": _safe_utf8(experiment.name),
                "dataset_id": experiment.dataset_id,
            },
        )

    async def enqueue_job_created(self, session: AsyncSession, job: Job) -> str | None:
        if not self.enabled:
            return None
        experiment = await _experiment_for_job(session, job)
        return self._enqueue(
            session,
            event_key=f"job:{job.id}",
            event_type=JOB_CREATED,
            aggregate_type="job",
            aggregate_id=job.id,
            payload=_job_context(job, experiment),
        )

    async def enqueue_metric_batch(
        self,
        session: AsyncSession,
        *,
        job: Job,
        attempt_id: str,
        attempt_number: int,
        idempotency_key: str,
        entries: Sequence[MetricEntry],
    ) -> str | None:
        if not self.enabled:
            return None
        experiment = await _experiment_for_job(session, job)
        payload = _job_context(job, experiment)
        payload.update(
            {
                "attempt_id": attempt_id,
                "attempt_number": attempt_number,
                "metrics": [
                    {
                        "key": entry.key,
                        "value": entry.value,
                        "timestamp": _timestamp_ms(entry.occurred_at),
                        "step": entry.step
                        if entry.step is not None
                        else (entry.epoch if entry.epoch is not None else entry.sequence),
                    }
                    for entry in entries
                ],
            }
        )
        return self._enqueue(
            session,
            event_key=metric_event_key(attempt_id, idempotency_key),
            event_type=METRIC_BATCH,
            aggregate_type="job",
            aggregate_id=job.id,
            payload=payload,
        )

    async def enqueue_artifact(
        self,
        session: AsyncSession,
        *,
        job: Job,
        artifact: Artifact,
        event_key: str | None = None,
    ) -> str | None:
        if not self.enabled:
            return None
        experiment = await _experiment_for_job(session, job)
        payload = _job_context(job, experiment)
        payload["artifact"] = {
            "id": artifact.id,
            "type": artifact.artifact_type,
            "filename": _safe_utf8(artifact.filename),
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
            "mime_type": _safe_utf8(artifact.mime_type or "application/octet-stream"),
        }
        return self._enqueue(
            session,
            event_key=event_key or artifact_event_key(artifact.id),
            event_type=ARTIFACT_VERIFIED,
            aggregate_type="artifact",
            aggregate_id=artifact.id,
            payload=payload,
        )

    async def enqueue_terminal_status(
        self,
        session: AsyncSession,
        *,
        job: Job,
        attempt_id: str,
        status: str,
        ended_at: datetime,
    ) -> str | None:
        if not self.enabled:
            return None
        experiment = await _experiment_for_job(session, job)
        payload = _job_context(job, experiment)
        payload.update(
            {
                "attempt_id": attempt_id,
                "status": status,
                "end_time_ms": _timestamp_ms(ended_at),
            }
        )
        return self._enqueue(
            session,
            event_key=terminal_event_key(attempt_id, status),
            event_type=JOB_TERMINAL,
            aggregate_type="job",
            aggregate_id=job.id,
            payload=payload,
        )

    async def readiness(self) -> tuple[str, bool]:
        if not self.settings.mlflow_enabled:
            return "disabled", True
        if self.adapter is None:
            return "not_configured", not self.settings.mlflow_fail_closed
        try:
            async with asyncio.timeout(self.settings.mlflow_readiness_timeout_seconds):
                await self.adapter.health()
        except Exception:
            return "unavailable", not self.settings.mlflow_fail_closed
        return "ok", True

    async def _recover_stale_processing(self, session: AsyncSession, now: datetime) -> None:
        stale_before = now - timedelta(seconds=self.settings.mlflow_processing_stale_seconds)
        await session.execute(
            update(MlflowSyncEvent)
            .where(
                MlflowSyncEvent.status == "processing",
                MlflowSyncEvent.locked_at.is_not(None),
                MlflowSyncEvent.locked_at <= stale_before,
            )
            .values(status="pending", locked_at=None, next_attempt_at=now)
        )

    async def _claim(self, event_key: str | None = None) -> ProjectionEvent | None:
        now = utc_now()
        async with self.database.session_factory() as session:
            await self._recover_stale_processing(session, now)
            filters = [MlflowSyncEvent.status == "pending"]
            if event_key is None:
                filters.append(
                    or_(
                        MlflowSyncEvent.next_attempt_at.is_(None),
                        MlflowSyncEvent.next_attempt_at <= now,
                    )
                )
            else:
                filters.append(MlflowSyncEvent.event_key == event_key)
            candidate_id = await session.scalar(
                select(MlflowSyncEvent.id)
                .where(*filters)
                .order_by(MlflowSyncEvent.created_at.asc(), MlflowSyncEvent.id.asc())
                .limit(1)
            )
            if candidate_id is None:
                await session.commit()
                return None
            claimed = await session.scalar(
                update(MlflowSyncEvent)
                .where(
                    MlflowSyncEvent.id == candidate_id,
                    MlflowSyncEvent.status == "pending",
                )
                .values(status="processing", locked_at=now, next_attempt_at=None)
                .returning(MlflowSyncEvent.id)
            )
            if claimed is None:
                await session.rollback()
                return None
            event = await session.get(MlflowSyncEvent, claimed)
            if event is None:
                await session.rollback()
                return None
            projection = ProjectionEvent(
                id=event.id,
                event_key=event.event_key,
                event_type=event.event_type,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                payload=dict(event.payload_json),
                attempt_count=event.attempt_count,
            )
            await session.commit()
            return projection

    async def _mark_synced(self, event: ProjectionEvent) -> None:
        now = utc_now()
        async with self.database.session_factory() as session:
            await session.execute(
                update(MlflowSyncEvent)
                .where(
                    MlflowSyncEvent.id == event.id,
                    MlflowSyncEvent.status == "processing",
                )
                .values(
                    status="synced",
                    attempt_count=event.attempt_count + 1,
                    locked_at=None,
                    next_attempt_at=None,
                    last_error_code=None,
                    synced_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

    async def _mark_retry(self, event: ProjectionEvent, error: MlflowUnavailable) -> None:
        now = utc_now()
        attempts = event.attempt_count + 1
        delay = min(2 ** min(attempts, 8), self.settings.mlflow_retry_max_seconds)
        async with self.database.session_factory() as session:
            await session.execute(
                update(MlflowSyncEvent)
                .where(
                    MlflowSyncEvent.id == event.id,
                    MlflowSyncEvent.status == "processing",
                )
                .values(
                    status="pending",
                    attempt_count=attempts,
                    locked_at=None,
                    next_attempt_at=now + timedelta(seconds=delay),
                    last_error_code=error.code,
                    updated_at=now,
                )
            )
            await session.commit()

    async def _project_one_locked(
        self,
        event_key: str | None = None,
    ) -> ProjectionEvent | None:
        if not self.enabled or self.adapter is None:
            return None
        event = await self._claim(event_key)
        if event is None:
            return None
        try:
            await self.adapter.project(event)
        except Exception as raw_error:
            exc = (
                raw_error
                if isinstance(raw_error, MlflowUnavailable)
                else MlflowUnavailable("projection_failed")
            )
            await self._mark_retry(event, exc)
            LOGGER.warning(
                "MLflow projection deferred",
                extra={
                    "event_type": event.event_type,
                    "aggregate_type": event.aggregate_type,
                    "aggregate_id": event.aggregate_id,
                    "error_code": exc.code,
                },
            )
            if self.settings.mlflow_fail_closed:
                raise MlflowProjectionRequired(event) from raw_error
            return event
        await self._mark_synced(event)
        return event

    async def _project_one(self, event_key: str | None = None) -> ProjectionEvent | None:
        # Serialize the request fast-path and background loop inside one API process.
        # Conditional outbox claims remain the cross-process ownership boundary.
        async with self._projection_lock:
            return await self._project_one_locked(event_key)

    async def sync_after_commit(self, event_key: str | None) -> None:
        if event_key is None:
            return
        await self._project_one(event_key)
        if not self.settings.mlflow_fail_closed:
            return
        async with self.database.session_factory() as session:
            event = await session.scalar(
                select(MlflowSyncEvent).where(MlflowSyncEvent.event_key == event_key)
            )
            if event is None or event.status == "synced":
                return
            raise MlflowProjectionRequired(
                ProjectionEvent(
                    id=event.id,
                    event_key=event.event_key,
                    event_type=event.event_type,
                    aggregate_type=event.aggregate_type,
                    aggregate_id=event.aggregate_id,
                    payload=dict(event.payload_json),
                    attempt_count=event.attempt_count,
                )
            )

    async def process_pending_once(self) -> int:
        processed = 0
        for _ in range(self.settings.mlflow_sync_batch_size):
            event = await self._project_one()
            if event is None:
                break
            processed += 1
        return processed

    async def run(self) -> None:
        if not self.enabled:
            return
        while not self._stop.is_set():
            try:
                await self.process_pending_once()
            except MlflowProjectionRequired:
                # Fail-closed changes API/readiness behavior, not the retry worker's lifetime.
                pass
            except Exception:
                LOGGER.exception("MLflow outbox worker failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.settings.mlflow_sync_interval_seconds,
                )
            except TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()

    async def close(self) -> None:
        if self.adapter is not None:
            await self.adapter.close()


def create_mlflow_coordinator(settings: Settings, database: Database) -> MlflowCoordinator:
    adapter: MlflowAdapter | None = None
    if settings.mlflow_enabled:
        adapter = MlflowRestAdapter(settings)
    return MlflowCoordinator(settings, database, adapter)
