"""Durable at-least-once delivery for Worker log and metric batches."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import tempfile
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

from rvc_orchestrator_contracts import (
    JobStatus,
    JobStatusUpdate,
    LogBatch,
    LogEntry,
    LogLevel,
    MetricBatch,
    MetricEntry,
    WorkerCapabilities,
    utc_now,
)

from .client import ManagerClient, ManagerClientError
from .training_metrics import ParsedTrainingMetric

TelemetryKind = Literal["log", "metric"]

_FORMAT_VERSION = 1
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_SANITIZED_LOG_BYTES = 16 * 1024
_BEARER_SECRET = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_NAME = (
    r"(?:authorization|token|password|secret|credential|"
    r"api[_-]?key|access[_-]?key|private[_-]?key)"
)
_NAMED_SECRET = re.compile(
    rf"(?i)\b({_SECRET_NAME})"
    r"(\s*(?::|=)\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_CLI_SECRET = re.compile(
    rf"(?i)(--{_SECRET_NAME}(?:=|\s+))"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_RVC_WORKER_TOKEN = re.compile(r"\brvcw_[A-Za-z0-9_-]{8,}\b")
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"(?![A-Za-z0-9_-])"
)
_URL_QUERY = re.compile(r"(?i)(https?://[^\s?#]+)\?[^\s#]*")
_FILE_URI = re.compile(r"(?i)\bfile:///[^\s'\";,]+")
_ABSOLUTE_PATH = re.compile(r"(?<![:/A-Za-z0-9])/(?:[^\s'\";,]+)")
_CONTROL_REPLACEMENT = " "


class TelemetrySpoolError(RuntimeError):
    """Raised when telemetry cannot be persisted without risking data loss."""


@dataclass(frozen=True, slots=True)
class FlushReport:
    delivered: int = 0
    deferred: int = 0
    dead_lettered: int = 0


@dataclass(frozen=True, slots=True)
class AttemptTelemetryWatermarks:
    log_count: int
    metric_count: int


@dataclass(frozen=True, slots=True)
class _DecodedRecord:
    kind: TelemetryKind
    job_id: str
    log_batch: LogBatch | None = None
    metric_batch: MetricBatch | None = None


class TelemetrySpool:
    """Disk-backed queue which never puts credentials in its record format.

    A batch is written atomically before its first network attempt. Successful
    delivery removes the record. Definitively invalid 4xx responses and corrupt
    local records are preserved under ``dead-letter`` for operator inspection.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = 256 * 1024**2,
        max_record_bytes: int = 2 * 1024**2,
    ) -> None:
        if max_bytes <= 0 or max_record_bytes <= 0 or max_record_bytes > max_bytes:
            raise TelemetrySpoolError("invalid telemetry spool size limits")
        self.root = root.expanduser().absolute()
        self.pending = self.root / "pending"
        self.dead_letter = self.root / "dead-letter"
        self.max_bytes = max_bytes
        self.max_record_bytes = max_record_bytes
        # Network delivery must never hold the lock required by a subprocess
        # output callback to durably enqueue its next record.  The two locks
        # deliberately serialize only their own file/network operation class.
        self._write_lock = asyncio.Lock()
        self._flush_lock = asyncio.Lock()
        _prepare_private_directory(self.root)
        _prepare_private_directory(self.pending)
        _prepare_private_directory(self.dead_letter)
        self._recover_incomplete_records()

    async def enqueue_log(self, job_id: str, batch: LogBatch) -> Path:
        return await self._enqueue("log", job_id, batch.idempotency_key, batch)

    async def enqueue_metric(self, job_id: str, batch: MetricBatch) -> Path:
        return await self._enqueue("metric", job_id, batch.idempotency_key, batch)

    async def _enqueue(
        self,
        kind: TelemetryKind,
        job_id: str,
        idempotency_key: str,
        batch: LogBatch | MetricBatch,
    ) -> Path:
        _validate_job_id(job_id)
        payload = _encode_record(kind, job_id, batch)
        if len(payload) > self.max_record_bytes:
            raise TelemetrySpoolError("telemetry record exceeds the per-record size limit")
        digest = sha256(f"{kind}\x00{job_id}\x00{idempotency_key}".encode()).hexdigest()
        destination = self.pending / f"{kind}-{digest}.json"
        async with self._write_lock:
            await asyncio.to_thread(self._write_record, destination, payload)
        return destination

    async def flush(self, manager: ManagerClient) -> FlushReport:
        """Deliver pending records once, stopping on a retryable transport error."""

        async with self._flush_lock:
            return await self._flush_locked(manager)

    async def _flush_locked(self, manager: ManagerClient) -> FlushReport:
        delivered = 0
        dead_lettered = 0
        records = await asyncio.to_thread(self._list_pending)
        for position, path in enumerate(records):
            try:
                decoded = await asyncio.to_thread(self._read_record, path)
            except TelemetrySpoolError:
                async with self._write_lock:
                    await asyncio.to_thread(self._move_to_dead_letter, path, "corrupt")
                dead_lettered += 1
                continue

            try:
                if decoded.kind == "log":
                    assert decoded.log_batch is not None
                    await manager.send_logs(decoded.job_id, decoded.log_batch)
                else:
                    assert decoded.metric_batch is not None
                    await manager.send_metrics(decoded.job_id, decoded.metric_batch)
            except ManagerClientError as exc:
                if not exc.retryable:
                    suffix = f"http-{exc.status_code}" if exc.status_code else "rejected"
                    async with self._write_lock:
                        await asyncio.to_thread(self._move_to_dead_letter, path, suffix)
                    dead_lettered += 1
                    continue
                return FlushReport(
                    delivered=delivered,
                    deferred=len(records) - position,
                    dead_lettered=dead_lettered,
                )
            except (OSError, TimeoutError):
                return FlushReport(
                    delivered=delivered,
                    deferred=len(records) - position,
                    dead_lettered=dead_lettered,
                )

            async with self._write_lock:
                await asyncio.to_thread(_remove_record, path)
            delivered += 1

        return FlushReport(delivered=delivered, dead_lettered=dead_lettered)

    def _write_record(self, destination: Path, payload: bytes) -> None:
        if destination.is_symlink():
            raise TelemetrySpoolError("telemetry record path is a symbolic link")
        if destination.exists():
            try:
                existing = destination.read_bytes()
            except OSError as exc:
                raise TelemetrySpoolError("cannot verify an existing telemetry record") from exc
            if existing != payload:
                raise TelemetrySpoolError(
                    "telemetry idempotency key was reused with a different payload"
                )
            return

        used_bytes = sum(
            item.stat().st_size for item in self._list_pending() if item != destination
        )
        if used_bytes + len(payload) > self.max_bytes:
            raise TelemetrySpoolError(
                "telemetry spool is full; refusing to discard log or metric data"
            )

        descriptor, temporary_name = tempfile.mkstemp(prefix=".record-", dir=self.pending)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            _fsync_directory(self.pending)
        except OSError as exc:
            raise TelemetrySpoolError("cannot persist telemetry record") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _list_pending(self) -> list[Path]:
        records: list[Path] = []
        try:
            candidates = list(self.pending.iterdir())
        except OSError as exc:
            raise TelemetrySpoolError("cannot list telemetry spool") from exc
        for path in candidates:
            if path.name.startswith(".record-"):
                continue
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                raise TelemetrySpoolError("unexpected entry in telemetry spool")
            try:
                path.chmod(0o600)
            except OSError as exc:
                raise TelemetrySpoolError("cannot secure telemetry record") from exc
            records.append(path)
        return sorted(records, key=lambda item: item.name)

    def _recover_incomplete_records(self) -> None:
        try:
            candidates = list(self.pending.glob(".record-*"))
        except OSError as exc:
            raise TelemetrySpoolError("cannot inspect incomplete telemetry records") from exc
        for path in candidates:
            if path.is_symlink() or not path.is_file():
                raise TelemetrySpoolError("unsafe incomplete telemetry record")
            self._move_to_dead_letter(path, "incomplete")

    def _read_record(self, path: Path) -> _DecodedRecord:
        if path.parent != self.pending or path.is_symlink() or not path.is_file():
            raise TelemetrySpoolError("unsafe telemetry record path")
        try:
            if path.stat().st_size > self.max_record_bytes:
                raise TelemetrySpoolError("telemetry record exceeds its size limit")
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise TelemetrySpoolError("cannot decode telemetry record") from exc
        if not isinstance(raw, dict) or raw.get("version") != _FORMAT_VERSION:
            raise TelemetrySpoolError("unsupported telemetry record format")
        kind = raw.get("kind")
        job_id = raw.get("job_id")
        batch = raw.get("batch")
        if kind not in {"log", "metric"} or not isinstance(job_id, str):
            raise TelemetrySpoolError("invalid telemetry envelope")
        _validate_job_id(job_id)
        try:
            if kind == "log":
                return _DecodedRecord(
                    kind="log", job_id=job_id, log_batch=LogBatch.model_validate(batch)
                )
            return _DecodedRecord(
                kind="metric", job_id=job_id, metric_batch=MetricBatch.model_validate(batch)
            )
        except ValueError as exc:
            raise TelemetrySpoolError("invalid telemetry batch") from exc

    def _move_to_dead_letter(self, source: Path, reason: str) -> None:
        if source.parent != self.pending or source.is_symlink() or not source.is_file():
            raise TelemetrySpoolError("unsafe telemetry dead-letter source")
        record_name = source.stem.lstrip(".") or "record"
        destination = self.dead_letter / f"{record_name}--{reason}.json"
        if destination.exists():
            try:
                if destination.read_bytes() == source.read_bytes():
                    source.unlink()
                    _fsync_directory(self.pending)
                    return
                content_suffix = sha256(source.read_bytes()).hexdigest()[:12]
            except OSError as exc:
                raise TelemetrySpoolError("cannot compare telemetry dead letters") from exc
            destination = self.dead_letter / f"{record_name}--{reason}-{content_suffix}.json"
        try:
            os.replace(source, destination)
            os.chmod(destination, 0o600)
            _fsync_directory(self.pending)
            _fsync_directory(self.dead_letter)
        except OSError as exc:
            raise TelemetrySpoolError("cannot preserve telemetry dead letter") from exc


StatusCallback = Callable[[str, JobStatusUpdate], Awaitable[object]]


class AttemptTelemetrySession:
    """Attempt-scoped live telemetry with durable-first, monotonic sequencing.

    Subprocess callbacks call :meth:`record_training_event`, which only sanitizes
    and atomically enqueues local spool records.  A single owned delivery task is
    responsible for all Manager I/O, so a slow Manager cannot backpressure the
    subprocess pipe through the spool write path.
    """

    def __init__(
        self,
        *,
        job_id: str,
        attempt_id: str,
        lease_id: str,
        spool: TelemetrySpool,
        manager: ManagerClient,
        status_callback: StatusCallback,
        redacted_roots: tuple[Path, ...] = (),
        delivery_interval_seconds: float = 1.0,
        max_source_events: int = 250_000,
    ) -> None:
        _validate_job_id(job_id)
        if not attempt_id or not lease_id:
            raise TelemetrySpoolError("attempt telemetry identity is incomplete")
        if not math.isfinite(delivery_interval_seconds) or delivery_interval_seconds <= 0:
            raise TelemetrySpoolError("telemetry delivery interval is invalid")
        if isinstance(max_source_events, bool) or not 1 <= max_source_events <= 1_000_000:
            raise TelemetrySpoolError("telemetry source event limit is invalid")
        self.job_id = job_id
        self.attempt_id = attempt_id
        self.lease_id = lease_id
        self.spool = spool
        self.manager = manager
        self.status_callback = status_callback
        self.delivery_interval_seconds = delivery_interval_seconds
        self.max_source_events = max_source_events
        self._redacted_values = tuple(
            sorted(
                {str(path.expanduser().absolute()) for path in redacted_roots if str(path)},
                key=len,
                reverse=True,
            )
        )
        self._state_lock = asyncio.Lock()
        self._delivery_lock = asyncio.Lock()
        self._wake_delivery = asyncio.Event()
        self._closing = asyncio.Event()
        self._delivery_task: asyncio.Task[None] | None = None
        self._next_log_sequence = 0
        self._next_metric_sequence = 0
        self._system_snapshot_ordinal = 0
        self._source_event_keys: set[str] = set()
        self._log_sources_by_semantic_key: dict[str, set[str]] = {}
        self._metric_sources_by_semantic_key: dict[str, set[str]] = {}
        self._pending_epoch: int | None = None
        self._reported_epoch: int | None = None
        self._training_active = True
        self._terminal_watermarks: AttemptTelemetryWatermarks | None = None

    def start(self) -> None:
        if self._delivery_task is not None:
            raise TelemetrySpoolError("attempt telemetry delivery already started")
        self._delivery_task = asyncio.create_task(
            self._delivery_loop(),
            name=f"telemetry-delivery-{self.job_id}-{self.attempt_id}",
        )

    async def record_training_event(
        self,
        *,
        source: str,
        event_key: str,
        message: str | None,
        metrics: tuple[ParsedTrainingMetric, ...] = (),
        channel: str | None = None,
    ) -> None:
        """Persist one deterministic source event without performing network I/O."""

        normalized_source = _normalize_source(source)
        normalized_event_key = _source_event_key(normalized_source, event_key)
        sanitized_message = (
            sanitize_telemetry_message(message, redacted_values=self._redacted_values)
            if message is not None
            else None
        )
        async with self._state_lock:
            if self._terminal_watermarks is not None:
                raise TelemetrySpoolError("training telemetry arrived after terminal watermarks")
            if not self._training_active:
                raise TelemetrySpoolError("training telemetry arrived after the stage closed")
            if normalized_event_key in self._source_event_keys:
                return
            if len(self._source_event_keys) >= self.max_source_events:
                raise TelemetrySpoolError("training telemetry source event limit was exceeded")
            self._source_event_keys.add(normalized_event_key)

            log_entry: LogEntry | None = None
            if sanitized_message:
                semantic_key = sha256(sanitized_message.encode("utf-8")).hexdigest()
                seen_sources = self._log_sources_by_semantic_key.setdefault(semantic_key, set())
                if not seen_sources or normalized_source in seen_sources:
                    log_entry = LogEntry(
                        sequence=self._next_log_sequence,
                        level=(LogLevel.ERROR if channel == "stderr" else LogLevel.INFO),
                        message=sanitized_message,
                        fields={
                            "source": normalized_source,
                            "channel": channel or normalized_source,
                            "source_event_key": normalized_event_key,
                        },
                    )
                seen_sources.add(normalized_source)

            metric_entries: list[MetricEntry] = []
            metric_event_keys: list[str] = []
            pending_epoch = self._pending_epoch
            for position, metric in enumerate(metrics):
                semantic_key = _metric_semantic_key(metric)
                seen_sources = self._metric_sources_by_semantic_key.setdefault(semantic_key, set())
                if seen_sources:
                    seen_sources.add(normalized_source)
                    continue
                seen_sources.add(normalized_source)
                metric_entries.append(
                    MetricEntry(
                        sequence=self._next_metric_sequence + len(metric_entries),
                        key=metric.key,
                        value=metric.value,
                        epoch=metric.epoch,
                        step=metric.step,
                    )
                )
                metric_event_keys.append(f"{normalized_event_key}:{position}:{semantic_key}")
                if metric.key in {"current_epoch", "epoch_completed"}:
                    epoch = metric.epoch
                    if epoch is None:
                        epoch = int(metric.value)
                    if epoch >= 0 and (pending_epoch is None or epoch > pending_epoch):
                        pending_epoch = epoch

            if log_entry is not None:
                log_batch = LogBatch(
                    lease_id=self.lease_id,
                    attempt_id=self.attempt_id,
                    idempotency_key=_batch_idempotency_key(
                        "log", self.attempt_id, (normalized_event_key,)
                    ),
                    entries=[log_entry],
                )
                await self.spool.enqueue_log(self.job_id, log_batch)
                self._next_log_sequence += 1
            if metric_entries:
                metric_batch = MetricBatch(
                    lease_id=self.lease_id,
                    attempt_id=self.attempt_id,
                    idempotency_key=_batch_idempotency_key(
                        "metric", self.attempt_id, tuple(metric_event_keys)
                    ),
                    entries=metric_entries,
                )
                await self.spool.enqueue_metric(self.job_id, metric_batch)
                self._next_metric_sequence += len(metric_entries)
                self._pending_epoch = pending_epoch
        if log_entry is not None or metric_entries:
            self._wake_delivery.set()

    async def record_stage_completed(
        self,
        *,
        stage: JobStatus,
        stage_ordinal: int,
        created_path_count: int,
    ) -> None:
        """Persist stage telemetry through the same attempt-wide allocators."""

        event_key = f"stage:{stage_ordinal}:{stage.value}"
        async with self._state_lock:
            if self._terminal_watermarks is not None:
                raise TelemetrySpoolError("stage telemetry arrived after terminal watermarks")
            if event_key in self._source_event_keys:
                return
            if len(self._source_event_keys) >= self.max_source_events:
                raise TelemetrySpoolError("attempt telemetry source event limit was exceeded")
            self._source_event_keys.add(event_key)
            log_entry = LogEntry(
                sequence=self._next_log_sequence,
                level=LogLevel.INFO,
                message=f"worker stage completed: {stage.value}",
                fields={
                    "stage": stage.value,
                    "created_path_count": created_path_count,
                },
            )
            metric_entry = MetricEntry(
                sequence=self._next_metric_sequence,
                key="worker.stage_completed",
                value=1.0,
            )
            await self.spool.enqueue_log(
                self.job_id,
                LogBatch(
                    lease_id=self.lease_id,
                    attempt_id=self.attempt_id,
                    idempotency_key=_batch_idempotency_key("log", self.attempt_id, (event_key,)),
                    entries=[log_entry],
                ),
            )
            self._next_log_sequence += 1
            await self.spool.enqueue_metric(
                self.job_id,
                MetricBatch(
                    lease_id=self.lease_id,
                    attempt_id=self.attempt_id,
                    idempotency_key=_batch_idempotency_key("metric", self.attempt_id, (event_key,)),
                    entries=[metric_entry],
                ),
            )
            self._next_metric_sequence += 1
        self._wake_delivery.set()

    async def record_system_snapshot(
        self,
        capabilities: WorkerCapabilities,
        *,
        gpu_telemetry_available: bool,
    ) -> bool:
        """Persist one Job-bound GPU/disk snapshot through the metric spool.

        System samples deliberately do not use semantic deduplication: an
        unchanged utilization value at two heartbeat times is still meaningful
        time-series evidence.  A heartbeat which raced behind terminal sealing
        is ignored instead of turning a successfully completed Job into a
        telemetry failure.
        """

        observed_at = utc_now()
        gpu_indices = [gpu.index for gpu in capabilities.gpus]
        if len(set(gpu_indices)) != len(gpu_indices):
            raise TelemetrySpoolError("system telemetry GPU indexes are not unique")
        values: list[tuple[str, float]] = [
            ("system.gpu.count", _finite_metric_value(len(capabilities.gpus))),
            (
                "system.gpu.telemetry_available",
                1.0 if gpu_telemetry_available else 0.0,
            ),
            (
                "system.disk_free_bytes",
                _finite_metric_value(capabilities.disk_free_bytes),
            ),
        ]
        for gpu in sorted(capabilities.gpus, key=lambda item: item.index):
            prefix = f"system.gpu.{gpu.index}"
            values.extend(
                (
                    (
                        f"{prefix}.vram_used_mb",
                        _finite_metric_value(gpu.total_vram_mb - gpu.free_vram_mb),
                    ),
                    (
                        f"{prefix}.vram_total_mb",
                        _finite_metric_value(gpu.total_vram_mb),
                    ),
                )
            )
            if gpu.utilization_percent is not None:
                values.append(
                    (
                        f"{prefix}.utilization_percent",
                        _finite_metric_value(gpu.utilization_percent),
                    )
                )
            if gpu.temperature_c is not None:
                values.append(
                    (
                        f"{prefix}.temperature_c",
                        _finite_metric_value(gpu.temperature_c),
                    )
                )

        async with self._state_lock:
            if self._terminal_watermarks is not None:
                return False
            if self._system_snapshot_ordinal >= self.max_source_events:
                raise TelemetrySpoolError("system telemetry snapshot limit was exceeded")
            ordinal = self._system_snapshot_ordinal
            entries = [
                MetricEntry(
                    sequence=self._next_metric_sequence + position,
                    key=key,
                    value=value,
                    occurred_at=observed_at,
                )
                for position, (key, value) in enumerate(values)
            ]
            event_key = f"system:{ordinal}"
            await self.spool.enqueue_metric(
                self.job_id,
                MetricBatch(
                    lease_id=self.lease_id,
                    attempt_id=self.attempt_id,
                    idempotency_key=_batch_idempotency_key(
                        "metric",
                        self.attempt_id,
                        (event_key,),
                    ),
                    entries=entries,
                ),
            )
            self._next_metric_sequence += len(entries)
            self._system_snapshot_ordinal += 1
        self._wake_delivery.set()
        return True

    async def finish_training(self) -> None:
        """Drain epoch projection while the Job is still in TRAINING."""

        async with self._delivery_lock:
            await self._deliver_once(include_epoch=True)
            async with self._state_lock:
                self._training_active = False

    async def watermarks(self) -> AttemptTelemetryWatermarks:
        """Seal producers and atomically snapshot durable attempt counts."""

        # Acquire the delivery boundary first, then seal under the same state
        # lock used by every producer.  Once sealed, a final best-effort flush
        # sees the complete durable prefix and no heartbeat can append behind
        # its pending-file snapshot.  A Manager outage may still leave records
        # for bounded late replay; the terminal counts remain authoritative.
        async with self._delivery_lock:
            async with self._state_lock:
                if self._terminal_watermarks is None:
                    self._training_active = False
                    self._terminal_watermarks = AttemptTelemetryWatermarks(
                        log_count=self._next_log_sequence,
                        metric_count=self._next_metric_sequence,
                    )
                watermarks = self._terminal_watermarks
            await self._deliver_once(include_epoch=False)
            return watermarks

    async def close(self, *, cancelled: bool) -> None:
        del cancelled
        task = self._delivery_task
        if task is None:
            return
        self._closing.set()
        # Do not perform Manager I/O after StageExecutor may have released the
        # lease with a terminal status.  Every unsent record is already durable.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._delivery_task = None

    async def _delivery_loop(self) -> None:
        try:
            while True:
                try:
                    await asyncio.wait_for(
                        self._wake_delivery.wait(),
                        timeout=self.delivery_interval_seconds,
                    )
                except TimeoutError:
                    pass
                self._wake_delivery.clear()
                async with self._delivery_lock:
                    await self._deliver_once(include_epoch=self._training_active)
                if self._closing.is_set():
                    return
        except asyncio.CancelledError:
            raise

    async def _deliver_once(self, *, include_epoch: bool) -> None:
        try:
            report = await self.spool.flush(self.manager)
        except TelemetrySpoolError:
            # The durable record remains authoritative.  A later heartbeat or
            # process restart will retry it through the same spool.
            return
        if report.deferred or report.dead_lettered:
            return
        if not include_epoch:
            return
        async with self._state_lock:
            epoch = self._pending_epoch
            if epoch is None or (
                self._reported_epoch is not None and epoch <= self._reported_epoch
            ):
                return
        try:
            await self.status_callback(
                self.job_id,
                JobStatusUpdate(
                    lease_id=self.lease_id,
                    status=JobStatus.TRAINING,
                    current_epoch=epoch,
                ),
            )
        except (ManagerClientError, OSError, TimeoutError):
            return
        async with self._state_lock:
            if self._reported_epoch is None or epoch > self._reported_epoch:
                self._reported_epoch = epoch


def sanitize_telemetry_message(
    value: str,
    *,
    redacted_values: tuple[str, ...] = (),
) -> str:
    """Return bounded, single-line log text without common secret forms."""

    normalized = unicodedata.normalize("NFC", value)
    normalized = "".join(
        _CONTROL_REPLACEMENT if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in normalized
    )
    normalized = _URL_QUERY.sub(r"\1?[REDACTED]", normalized)
    normalized = _BEARER_SECRET.sub("Bearer [REDACTED]", normalized)
    normalized = _RVC_WORKER_TOKEN.sub("[REDACTED]", normalized)
    normalized = _JWT.sub("[REDACTED JWT]", normalized)
    normalized = _NAMED_SECRET.sub(r"\1\2[REDACTED]", normalized)
    normalized = _CLI_SECRET.sub(r"\1[REDACTED]", normalized)
    normalized = _FILE_URI.sub("[LOCAL_PATH]", normalized)
    for secret in redacted_values:
        if secret:
            normalized = normalized.replace(secret, "[LOCAL_PATH]")
    normalized = _ABSOLUTE_PATH.sub("[LOCAL_PATH]", normalized)
    normalized = " ".join(normalized.split())
    encoded = normalized.encode("utf-8")
    if len(encoded) <= _MAX_SANITIZED_LOG_BYTES:
        return normalized
    suffix = "...[truncated]"
    budget = _MAX_SANITIZED_LOG_BYTES - len(suffix.encode("ascii"))
    shortened = encoded[:budget]
    while shortened:
        try:
            return shortened.decode("utf-8") + suffix
        except UnicodeDecodeError:
            shortened = shortened[:-1]
    return suffix


def _normalize_source(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"stdout", "train_log", "tensorboard"}:
        raise TelemetrySpoolError("unsupported training telemetry source")
    return normalized


def _source_event_key(source: str, value: str) -> str:
    if not value or len(value.encode("utf-8")) > 4_096:
        raise TelemetrySpoolError("training telemetry event key is invalid")
    return sha256(f"{source}\x00{value}".encode()).hexdigest()


def _metric_semantic_key(metric: ParsedTrainingMetric) -> str:
    if not math.isfinite(metric.value):
        raise TelemetrySpoolError("training telemetry metric is non-finite")
    return sha256(
        json.dumps(
            {
                "key": metric.key,
                "value": metric.value.hex(),
                "epoch": metric.epoch,
                "step": metric.step,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _finite_metric_value(value: int | float) -> float:
    try:
        rendered = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise TelemetrySpoolError("system telemetry metric is not numeric") from exc
    if not math.isfinite(rendered):
        raise TelemetrySpoolError("system telemetry metric is non-finite")
    return rendered


def _batch_idempotency_key(
    kind: TelemetryKind,
    attempt_id: str,
    event_keys: tuple[str, ...],
) -> str:
    return sha256("\x1f".join((kind, attempt_id, *event_keys)).encode("utf-8")).hexdigest()


def _encode_record(kind: TelemetryKind, job_id: str, batch: LogBatch | MetricBatch) -> bytes:
    try:
        serialized = json.dumps(
            {
                "version": _FORMAT_VERSION,
                "kind": kind,
                "job_id": job_id,
                "batch": batch.model_dump(mode="json"),
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise TelemetrySpoolError("telemetry batch is not JSON serializable") from exc
    return f"{serialized}\n".encode()


def _validate_job_id(job_id: str) -> None:
    if job_id in {".", ".."} or not _SAFE_JOB_ID.fullmatch(job_id):
        raise TelemetrySpoolError("unsafe telemetry job identifier")


def _prepare_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise TelemetrySpoolError("telemetry spool directory cannot be a symbolic link")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise TelemetrySpoolError("telemetry spool path is not a directory")
        path.chmod(0o700)
    except OSError as exc:
        raise TelemetrySpoolError("cannot prepare telemetry spool directory") from exc


def _remove_record(path: Path) -> None:
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except OSError as exc:
        raise TelemetrySpoolError("cannot acknowledge telemetry record") from exc


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
