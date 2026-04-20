from __future__ import annotations

import asyncio
import json
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from rvc_orchestrator_contracts import (
    GPUCapability,
    JobStatus,
    LogBatch,
    LogEntry,
    MetricBatch,
    MetricEntry,
    RVCVersion,
    TrainingF0Method,
    WorkerCapabilities,
    WorkerEngineMode,
)
from rvc_worker.client import ManagerClientError
from rvc_worker.telemetry import (
    AttemptTelemetrySession,
    TelemetrySpool,
    TelemetrySpoolError,
    sanitize_telemetry_message,
)
from rvc_worker.training_metrics import ParsedTrainingMetric


class RecordingManager:
    def __init__(self, *, failure_status: int | None = None) -> None:
        self.failure_status = failure_status
        self.logs: list[tuple[str, LogBatch]] = []
        self.metrics: list[tuple[str, MetricBatch]] = []
        self.statuses = []
        self.actions: list[str] = []

    async def send_logs(self, job_id: str, batch: LogBatch) -> None:
        if self.failure_status is not None:
            raise ManagerClientError("test rejection", status_code=self.failure_status)
        self.logs.append((job_id, batch))
        self.actions.append("logs")

    async def send_metrics(self, job_id: str, batch: MetricBatch) -> None:
        if self.failure_status is not None:
            raise ManagerClientError("test rejection", status_code=self.failure_status)
        self.metrics.append((job_id, batch))
        self.actions.append("metrics")

    async def update_status(self, job_id: str, update: object) -> None:
        del job_id
        self.statuses.append(update)
        self.actions.append("status")


class BlockingManager(RecordingManager):
    def __init__(self) -> None:
        super().__init__()
        self.delivery_started = asyncio.Event()
        self.release_delivery = asyncio.Event()

    async def send_logs(self, job_id: str, batch: LogBatch) -> None:
        self.delivery_started.set()
        await self.release_delivery.wait()
        await super().send_logs(job_id, batch)


class MetricFailingSpool(TelemetrySpool):
    async def enqueue_metric(self, job_id: str, batch: MetricBatch) -> Path:
        del job_id, batch
        raise TelemetrySpoolError("injected metric persistence failure")


class BlockingMetricEnqueueSpool(TelemetrySpool):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.enqueue_started = asyncio.Event()
        self.release_enqueue = asyncio.Event()

    async def enqueue_metric(self, job_id: str, batch: MetricBatch) -> Path:
        self.enqueue_started.set()
        await self.release_enqueue.wait()
        return await super().enqueue_metric(job_id, batch)


class TelemetrySpoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_failure_survives_process_restart_and_replays(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            first_process = TelemetrySpool(root)
            await first_process.enqueue_log("job-1", _log_batch("log-key-0001"))
            await first_process.enqueue_metric("job-1", _metric_batch("metric-key-0001"))

            deferred = await first_process.flush(RecordingManager(failure_status=503))
            self.assertEqual(deferred.delivered, 0)
            self.assertEqual(deferred.deferred, 2)
            self.assertEqual(len(list(first_process.pending.glob("*.json"))), 2)

            second_process = TelemetrySpool(root)
            manager = RecordingManager()
            delivered = await second_process.flush(manager)

            self.assertEqual(delivered.delivered, 2)
            self.assertEqual(delivered.deferred, 0)
            self.assertEqual(len(manager.logs), 1)
            self.assertEqual(len(manager.metrics), 1)
            self.assertFalse(list(second_process.pending.glob("*.json")))

    async def test_enqueue_is_idempotent_but_rejects_payload_collision(self) -> None:
        with TemporaryDirectory() as temporary:
            spool = TelemetrySpool(Path(temporary) / "telemetry")
            batch = _log_batch("same-key-0001")
            first = await spool.enqueue_log("job-1", batch)
            second = await spool.enqueue_log("job-1", batch)

            self.assertEqual(first, second)
            self.assertEqual(len(list(spool.pending.glob("*.json"))), 1)
            self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)

            changed = _log_batch("same-key-0001", message="different payload")
            with self.assertRaises(TelemetrySpoolError):
                await spool.enqueue_log("job-1", changed)

    async def test_permanent_rejection_is_preserved_as_dead_letter(self) -> None:
        with TemporaryDirectory() as temporary:
            spool = TelemetrySpool(Path(temporary) / "telemetry")
            await spool.enqueue_log("job-1", _log_batch("rejected-key"))

            report = await spool.flush(RecordingManager(failure_status=422))

            self.assertEqual(report.dead_lettered, 1)
            self.assertFalse(list(spool.pending.glob("*.json")))
            dead_letters = list(spool.dead_letter.glob("*--http-422.json"))
            self.assertEqual(len(dead_letters), 1)
            self.assertIn('"job_id":"job-1"', dead_letters[0].read_text(encoding="utf-8"))

    async def test_corrupt_record_is_preserved_and_does_not_block_valid_record(self) -> None:
        with TemporaryDirectory() as temporary:
            spool = TelemetrySpool(Path(temporary) / "telemetry")
            corrupt = spool.pending / "log-corrupt.json"
            corrupt.write_text("{not-json", encoding="utf-8")
            await spool.enqueue_metric("job-1", _metric_batch("valid-key-0001"))
            manager = RecordingManager()

            report = await spool.flush(manager)

            self.assertEqual(report.delivered, 1)
            self.assertEqual(report.dead_lettered, 1)
            self.assertEqual(len(manager.metrics), 1)
            self.assertTrue(list(spool.dead_letter.glob("log-corrupt--corrupt.json")))

    async def test_full_spool_refuses_to_silently_drop_records(self) -> None:
        with TemporaryDirectory() as temporary:
            spool = TelemetrySpool(
                Path(temporary) / "telemetry", max_bytes=700, max_record_bytes=700
            )
            await spool.enqueue_log("job-1", _log_batch("first-key-0001", message="x" * 120))

            with self.assertRaisesRegex(TelemetrySpoolError, "spool is full"):
                await spool.enqueue_log("job-1", _log_batch("second-key-0001", message="y" * 120))
            self.assertEqual(len(list(spool.pending.glob("*.json"))), 1)

    async def test_unsafe_job_id_and_symlink_root_are_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            spool = TelemetrySpool(base / "telemetry")
            with self.assertRaises(TelemetrySpoolError):
                await spool.enqueue_log("../job", _log_batch("unsafe-key-0001"))

            target = base / "target"
            target.mkdir()
            link = base / "linked-spool"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(TelemetrySpoolError):
                TelemetrySpool(link)

    async def test_crash_temporary_is_recovered_to_dead_letter(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            spool = TelemetrySpool(root)
            incomplete = spool.pending / ".record-crash"
            incomplete.write_text("partial", encoding="utf-8")

            recovered = TelemetrySpool(root)

            self.assertFalse(incomplete.exists())
            self.assertTrue(list(recovered.dead_letter.glob("record-crash--incomplete.json")))

    async def test_attempt_allocator_is_shared_and_cross_source_duplicates_are_removed(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            spool = TelemetrySpool(Path(temporary) / "telemetry")
            manager = RecordingManager()
            session = AttemptTelemetrySession(
                job_id="job-1",
                attempt_id="attempt-1",
                lease_id="lease-1",
                spool=spool,
                manager=manager,  # type: ignore[arg-type]
                status_callback=manager.update_status,
            )
            metric = ParsedTrainingMetric(
                key="current_epoch",
                value=2.0,
                epoch=2,
                source="stdout",
            )
            await session.record_training_event(
                source="stdout",
                event_key="stdout:0",
                message="INFO Train Epoch: 2",
                metrics=(metric,),
                channel="stdout",
            )
            await session.record_training_event(
                source="train_log",
                event_key="train-log:0",
                message="INFO Train Epoch: 2",
                metrics=(metric,),
                channel="train_log",
            )
            await session.record_stage_completed(
                stage=JobStatus.TRAINING,
                stage_ordinal=6,
                created_path_count=3,
            )

            documents = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in spool.pending.glob("*.json")
            ]
            log_sequences = sorted(
                entry["sequence"]
                for document in documents
                if document["kind"] == "log"
                for entry in document["batch"]["entries"]
            )
            metric_sequences = sorted(
                entry["sequence"]
                for document in documents
                if document["kind"] == "metric"
                for entry in document["batch"]["entries"]
            )
            self.assertEqual(log_sequences, [0, 1])
            self.assertEqual(metric_sequences, [0, 1])
            watermarks = await session.watermarks()
            self.assertEqual((watermarks.log_count, watermarks.metric_count), (2, 2))
            self.assertEqual(await session.watermarks(), watermarks)
            with self.assertRaisesRegex(TelemetrySpoolError, "terminal watermarks"):
                await session.record_training_event(
                    source="stdout",
                    event_key="after-terminal",
                    message="must not allocate",
                )
            with self.assertRaisesRegex(TelemetrySpoolError, "terminal watermarks"):
                await session.record_stage_completed(
                    stage=JobStatus.SAVING_CHECKPOINT,
                    stage_ordinal=7,
                    created_path_count=2,
                )

    async def test_watermarks_count_only_successfully_persisted_sequences(self) -> None:
        with TemporaryDirectory() as temporary:
            spool = MetricFailingSpool(Path(temporary) / "telemetry")
            manager = RecordingManager()
            session = AttemptTelemetrySession(
                job_id="job-1",
                attempt_id="attempt-1",
                lease_id="lease-1",
                spool=spool,
                manager=manager,  # type: ignore[arg-type]
                status_callback=manager.update_status,
            )

            with self.assertRaisesRegex(TelemetrySpoolError, "metric persistence"):
                await session.record_stage_completed(
                    stage=JobStatus.TRAINING,
                    stage_ordinal=6,
                    created_path_count=3,
                )

            watermarks = await session.watermarks()
            self.assertEqual((watermarks.log_count, watermarks.metric_count), (1, 0))
            self.assertEqual(len(manager.logs), 1)
            self.assertFalse(list(spool.pending.glob("log-*.json")))

    async def test_system_snapshots_keep_repeated_values_in_attempt_sequence(self) -> None:
        with TemporaryDirectory() as temporary:
            spool = TelemetrySpool(Path(temporary) / "telemetry")
            manager = RecordingManager()
            session = AttemptTelemetrySession(
                job_id="job-1",
                attempt_id="attempt-1",
                lease_id="lease-1",
                spool=spool,
                manager=manager,  # type: ignore[arg-type]
                status_callback=manager.update_status,
            )
            capabilities = _system_capabilities()

            self.assertTrue(
                await session.record_system_snapshot(
                    capabilities,
                    gpu_telemetry_available=True,
                )
            )
            self.assertTrue(
                await session.record_system_snapshot(
                    capabilities,
                    gpu_telemetry_available=True,
                )
            )
            await spool.flush(manager)  # type: ignore[arg-type]

            entries = [entry for _, batch in manager.metrics for entry in batch.entries]
            self.assertEqual(
                [entry.sequence for entry in entries],
                list(range(14)),
            )
            expected = {
                "system.gpu.count": 1.0,
                "system.gpu.telemetry_available": 1.0,
                "system.disk_free_bytes": 10_000.0,
                "system.gpu.0.vram_used_mb": 6_144.0,
                "system.gpu.0.vram_total_mb": 24_576.0,
                "system.gpu.0.utilization_percent": 37.5,
                "system.gpu.0.temperature_c": 61.0,
            }
            for key, value in expected.items():
                matching = [entry for entry in entries if entry.key == key]
                self.assertEqual(len(matching), 2)
                self.assertTrue(all(entry.value == value for entry in matching))
            self.assertEqual(len({entry.occurred_at for entry in entries[:7]}), 1)
            self.assertEqual(len({entry.occurred_at for entry in entries[7:]}), 1)

            watermarks = await session.watermarks()
            self.assertEqual((watermarks.log_count, watermarks.metric_count), (0, 14))
            self.assertFalse(
                await session.record_system_snapshot(
                    capabilities,
                    gpu_telemetry_available=True,
                )
            )
            self.assertFalse(list(spool.pending.glob("*.json")))

    async def test_terminal_watermark_waits_for_inflight_system_snapshot(self) -> None:
        with TemporaryDirectory() as temporary:
            spool = BlockingMetricEnqueueSpool(Path(temporary) / "telemetry")
            manager = RecordingManager()
            session = AttemptTelemetrySession(
                job_id="job-1",
                attempt_id="attempt-1",
                lease_id="lease-1",
                spool=spool,
                manager=manager,  # type: ignore[arg-type]
                status_callback=manager.update_status,
            )

            snapshot = asyncio.create_task(
                session.record_system_snapshot(
                    _system_capabilities(),
                    gpu_telemetry_available=True,
                )
            )
            await asyncio.wait_for(spool.enqueue_started.wait(), timeout=1)
            watermarks = asyncio.create_task(session.watermarks())
            await asyncio.sleep(0)
            self.assertFalse(watermarks.done())

            spool.release_enqueue.set()
            self.assertTrue(await asyncio.wait_for(snapshot, timeout=1))
            sealed = await asyncio.wait_for(watermarks, timeout=1)
            self.assertEqual((sealed.log_count, sealed.metric_count), (0, 7))
            self.assertFalse(
                await session.record_system_snapshot(
                    _system_capabilities(),
                    gpu_telemetry_available=True,
                )
            )

    async def test_terminal_watermark_final_flushes_the_sealed_durable_prefix(self) -> None:
        with TemporaryDirectory() as temporary:
            spool = TelemetrySpool(Path(temporary) / "telemetry")
            manager = RecordingManager()
            session = AttemptTelemetrySession(
                job_id="job-1",
                attempt_id="attempt-1",
                lease_id="lease-1",
                spool=spool,
                manager=manager,  # type: ignore[arg-type]
                status_callback=manager.update_status,
            )
            self.assertTrue(
                await session.record_system_snapshot(
                    _system_capabilities(),
                    gpu_telemetry_available=True,
                )
            )
            self.assertEqual(len(list(spool.pending.glob("*.json"))), 1)

            sealed = await session.watermarks()

            self.assertEqual((sealed.log_count, sealed.metric_count), (0, 7))
            self.assertEqual(
                sum(len(batch.entries) for _, batch in manager.metrics),
                sealed.metric_count,
            )
            self.assertFalse(list(spool.pending.glob("*.json")))

    async def test_terminal_watermark_keeps_deferred_prefix_for_late_replay(self) -> None:
        with TemporaryDirectory() as temporary:
            spool = TelemetrySpool(Path(temporary) / "telemetry")
            manager = RecordingManager(failure_status=503)
            session = AttemptTelemetrySession(
                job_id="job-1",
                attempt_id="attempt-1",
                lease_id="lease-1",
                spool=spool,
                manager=manager,  # type: ignore[arg-type]
                status_callback=manager.update_status,
            )
            self.assertTrue(
                await session.record_system_snapshot(
                    _system_capabilities(),
                    gpu_telemetry_available=False,
                )
            )

            sealed = await session.watermarks()

            self.assertEqual((sealed.log_count, sealed.metric_count), (0, 7))
            self.assertEqual(len(list(spool.pending.glob("*.json"))), 1)
            self.assertFalse(manager.metrics)

    async def test_durable_epoch_metric_is_delivered_before_best_effort_status(self) -> None:
        with TemporaryDirectory() as temporary:
            spool = TelemetrySpool(Path(temporary) / "telemetry")
            manager = RecordingManager()
            session = AttemptTelemetrySession(
                job_id="job-1",
                attempt_id="attempt-1",
                lease_id="lease-1",
                spool=spool,
                manager=manager,  # type: ignore[arg-type]
                status_callback=manager.update_status,
            )
            await session.record_training_event(
                source="stdout",
                event_key="epoch-3",
                message=None,
                metrics=(
                    ParsedTrainingMetric(
                        key="current_epoch",
                        value=3.0,
                        epoch=3,
                        source="stdout",
                    ),
                ),
            )

            await session.finish_training()

            self.assertEqual(manager.actions, ["metrics", "status"])
            self.assertEqual(manager.statuses[0].status, JobStatus.TRAINING)
            self.assertEqual(manager.statuses[0].current_epoch, 3)

    async def test_epoch_status_waits_when_the_durable_metric_is_deferred(self) -> None:
        with TemporaryDirectory() as temporary:
            spool = TelemetrySpool(Path(temporary) / "telemetry")
            manager = RecordingManager(failure_status=503)
            session = AttemptTelemetrySession(
                job_id="job-1",
                attempt_id="attempt-1",
                lease_id="lease-1",
                spool=spool,
                manager=manager,  # type: ignore[arg-type]
                status_callback=manager.update_status,
            )
            await session.record_training_event(
                source="stdout",
                event_key="epoch-4",
                message=None,
                metrics=(
                    ParsedTrainingMetric(
                        key="current_epoch",
                        value=4.0,
                        epoch=4,
                        source="stdout",
                    ),
                ),
            )

            await session.finish_training()

            self.assertFalse(manager.statuses)
            self.assertEqual(len(list(spool.pending.glob("*.json"))), 1)

    async def test_network_flush_does_not_hold_the_spool_enqueue_lock(self) -> None:
        with TemporaryDirectory() as temporary:
            spool = TelemetrySpool(Path(temporary) / "telemetry")
            manager = BlockingManager()
            session = AttemptTelemetrySession(
                job_id="job-1",
                attempt_id="attempt-1",
                lease_id="lease-1",
                spool=spool,
                manager=manager,  # type: ignore[arg-type]
                status_callback=manager.update_status,
                delivery_interval_seconds=0.05,
            )
            session.start()
            await session.record_training_event(
                source="stdout",
                event_key="line-1",
                message="first line",
            )
            await asyncio.wait_for(manager.delivery_started.wait(), timeout=1)

            await asyncio.wait_for(
                session.record_training_event(
                    source="stdout",
                    event_key="line-2",
                    message="second line",
                ),
                timeout=0.2,
            )

            await session.close(cancelled=True)
            self.assertEqual(len(list(spool.pending.glob("*.json"))), 2)

    def test_log_sanitization_is_bounded_and_removes_secrets_controls_and_paths(
        self,
    ) -> None:
        root = "/private/jobs/attempt"
        sanitized = sanitize_telemetry_message(
            "\x1b[31m Authorization: Bearer top-secret "
            "token=\"secret value\" --password 'hunter two' "
            '--token "cli secret" '
            "api_key=api-secret access-key='access secret' "
            'private_key="private secret" '
            "aaaabbbb.ccccdddd.eeeeffff "
            "https://manager.test/path?token=query "
            f"{root}/train.log " + "가" * 20_000,
            redacted_values=(root,),
        )

        self.assertNotIn("top-secret", sanitized)
        self.assertNotIn("secret value", sanitized)
        self.assertNotIn("hunter two", sanitized)
        self.assertNotIn("cli secret", sanitized)
        self.assertNotIn("api-secret", sanitized)
        self.assertNotIn("access secret", sanitized)
        self.assertNotIn("private secret", sanitized)
        self.assertNotIn("aaaabbbb.ccccdddd.eeeeffff", sanitized)
        self.assertNotIn("token=query", sanitized)
        self.assertNotIn(root, sanitized)
        self.assertNotIn("\x1b", sanitized)
        self.assertLessEqual(len(sanitized.encode("utf-8")), 16 * 1024)
        self.assertTrue(sanitized.endswith("...[truncated]"))


def _log_batch(key: str, *, message: str = "stage complete") -> LogBatch:
    return LogBatch(
        lease_id="lease-1",
        attempt_id="attempt-1",
        idempotency_key=key,
        entries=[LogEntry(sequence=1, message=message)],
    )


def _metric_batch(key: str) -> MetricBatch:
    return MetricBatch(
        lease_id="lease-1",
        attempt_id="attempt-1",
        idempotency_key=key,
        entries=[MetricEntry(sequence=1, key="worker.stage_completed", value=1.0)],
    )


def _system_capabilities() -> WorkerCapabilities:
    return WorkerCapabilities(
        engine_mode=WorkerEngineMode.FAKE,
        worker_version="test",
        rvc_commit_hash="fake-runner",
        supported_rvc_versions=[RVCVersion.V1, RVCVersion.V2],
        supported_training_f0_methods=list(TrainingF0Method),
        gpus=[
            GPUCapability(
                index=0,
                uuid="GPU-0",
                name="Fixture GPU",
                total_vram_mb=24_576,
                free_vram_mb=18_432,
                utilization_percent=37.5,
                temperature_c=61,
            )
        ],
        disk_free_bytes=10_000,
    )


if __name__ == "__main__":
    unittest.main()
