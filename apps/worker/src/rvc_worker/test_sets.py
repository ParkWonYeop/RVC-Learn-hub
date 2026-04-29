"""Lease-bound fixed TestSet transfer and workspace materialization."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import stat
import uuid
import wave
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from rvc_orchestrator_contracts import JobStatus, TestSetTransfer, TestSetTransferItem

from .client import (
    ManagerClient,
    TestSetTransferCancelled,
    TestSetTransferError,
    _critical_to_thread,
)
from .runner import RvcRunContext, RvcRunner, RvcRunnerError, StageResult
from .workspace import WorkspaceError


class TestSetMaterializationError(RvcRunnerError):
    """A claimed TestSet snapshot could not be proven safe and immutable."""


class TestSetMaterializationCancelled(TestSetMaterializationError):
    """TestSet inspection observed cooperative cancellation."""


class TestSetMaterializationTimeout(TestSetMaterializationError):
    """The absolute whole-TestSet materialization deadline elapsed."""


@dataclass(frozen=True, slots=True)
class TestSetMaterializationLimits:
    max_items: int = 128
    max_item_bytes: int = 256 * 1024**2
    max_total_bytes: int = 2 * 1024**3
    max_duration_seconds: float = 600.0
    max_total_duration_seconds: float = 3_600.0
    materialization_timeout_seconds: float = 7_200.0
    min_sample_rate_hz: int = 8_000
    max_sample_rate_hz: int = 192_000
    max_channels: int = 2
    duration_tolerance_seconds: float = 0.000001
    chunk_bytes: int = 1024**2
    download_attempts: int = 3

    def __post_init__(self) -> None:
        if (
            not 1 <= self.max_items <= 128
            or not 44 <= self.max_item_bytes <= 2 * 1024**3
            or not self.max_item_bytes <= self.max_total_bytes <= 100 * 1024**3
            or not math.isfinite(self.max_duration_seconds)
            or not 0 < self.max_duration_seconds <= 86_400
            or not math.isfinite(self.max_total_duration_seconds)
            or not self.max_duration_seconds <= self.max_total_duration_seconds <= 86_400
            or not math.isfinite(self.materialization_timeout_seconds)
            or self.materialization_timeout_seconds <= 0
            or not 1 <= self.min_sample_rate_hz <= self.max_sample_rate_hz <= 384_000
            or not 1 <= self.max_channels <= 32
            or not math.isfinite(self.duration_tolerance_seconds)
            or not 0 < self.duration_tolerance_seconds <= 1
            or self.chunk_bytes <= 0
            or not 1 <= self.download_attempts <= 10
        ):
            raise ValueError("invalid TestSet materialization limits")


@dataclass(frozen=True, slots=True)
class PcmWaveInspection:
    size_bytes: int
    sha256: str
    sample_rate_hz: int
    channels: int
    duration_seconds: float


class TestSetMaterializer:
    """Download an ordered immutable TestSet into one attempt workspace."""

    def __init__(
        self,
        manager: ManagerClient,
        *,
        limits: TestSetMaterializationLimits | None = None,
    ) -> None:
        self.manager = manager
        self.limits = limits or TestSetMaterializationLimits()

    async def materialize(
        self,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        transfer = context.claim.test_set_transfer
        if transfer is None:
            raise TestSetMaterializationError("Job claim has no verified TestSet transfer")
        self.preflight(transfer)
        if cancellation.is_set():
            raise asyncio.CancelledError
        effective_cancellation = asyncio.Event()
        operation = asyncio.create_task(self._materialize_once(context, effective_cancellation))
        external_cancellation = asyncio.create_task(_wait_for_cancellation(cancellation))
        timeout = asyncio.create_task(asyncio.sleep(self.limits.materialization_timeout_seconds))
        try:
            done, _ = await asyncio.wait(
                (operation, external_cancellation, timeout),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation in done:
                return await operation
            effective_cancellation.set()
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            if cancellation.is_set():
                raise asyncio.CancelledError
            raise TestSetMaterializationTimeout("TestSet materialization timed out")
        except asyncio.CancelledError:
            effective_cancellation.set()
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise
        finally:
            external_cancellation.cancel()
            timeout.cancel()
            await asyncio.gather(
                external_cancellation,
                timeout,
                return_exceptions=True,
            )

    def preflight(self, transfer: TestSetTransfer) -> None:
        _preflight_transfer(transfer, self.limits)

    async def _materialize_once(
        self,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        transfer = context.claim.test_set_transfer
        assert transfer is not None
        destination = context.workspace.inputs / "test_set"
        marker = context.workspace.outputs / "test_set_transfer.json"
        try:
            context.workspace.assert_path(destination)
            context.workspace.assert_path(marker)
        except WorkspaceError as exc:
            raise TestSetMaterializationError("TestSet path escapes the workspace") from exc
        await _critical_to_thread(
            _assert_safe_parent_and_no_stale_partials,
            context.workspace.inputs,
        )

        if await _critical_to_thread(_path_exists_or_symlink, destination):
            try:
                files = await _critical_to_thread(
                    _validate_materialization,
                    destination,
                    transfer,
                    self.limits,
                    cancellation,
                )
            except TestSetMaterializationCancelled as exc:
                raise asyncio.CancelledError from exc
        else:
            files = await self._download_and_publish(
                context,
                transfer,
                destination,
                cancellation,
            )
        if cancellation.is_set():
            raise asyncio.CancelledError
        await _critical_to_thread(
            _write_transfer_marker,
            marker,
            transfer,
        )
        try:
            for path in (*files, marker):
                context.workspace.assert_path(path)
        except WorkspaceError as exc:
            raise TestSetMaterializationError("TestSet path escapes the workspace") from exc
        return StageResult(
            (*files, marker),
            {
                "test_set_id": transfer.test_set_id,
                "family_id": transfer.family_id,
                "revision": transfer.revision,
                "item_count": len(files),
                "total_size_bytes": sum(item.size_bytes for item in transfer.items),
                "manifest_sha256": transfer.manifest_sha256,
                "sample_plan_sha256": transfer.sample_plan_sha256,
                "inference_config_sha256": transfer.inference_config_sha256,
            },
        )

    async def _download_and_publish(
        self,
        context: RvcRunContext,
        transfer: TestSetTransfer,
        destination: Path,
        cancellation: asyncio.Event,
    ) -> tuple[Path, ...]:
        staging = destination.with_name(f".test_set.{uuid.uuid4().hex}.partial")
        try:
            await _critical_to_thread(os.mkdir, staging, 0o700)
            await _critical_to_thread(_secure_directory_mode, staging)
            for item in transfer.items:
                if cancellation.is_set():
                    raise asyncio.CancelledError
                target = staging / item.filename
                await self._download_item(context, item, target, cancellation)
                try:
                    await _critical_to_thread(
                        partial(
                            inspect_pcm_wave,
                            target,
                            item=item,
                            limits=self.limits,
                            cancellation=cancellation,
                        )
                    )
                except TestSetMaterializationCancelled as exc:
                    raise asyncio.CancelledError from exc

            try:
                await _critical_to_thread(
                    _validate_materialization,
                    staging,
                    transfer,
                    self.limits,
                    cancellation,
                )
            except TestSetMaterializationCancelled as exc:
                raise asyncio.CancelledError from exc
            if await _critical_to_thread(_path_exists_or_symlink, destination):
                try:
                    files = await _critical_to_thread(
                        _validate_materialization,
                        destination,
                        transfer,
                        self.limits,
                        cancellation,
                    )
                except TestSetMaterializationCancelled as exc:
                    raise asyncio.CancelledError from exc
                await _critical_to_thread(_remove_owned_staging, staging)
                return files
            await _critical_to_thread(os.rename, staging, destination)
            await _critical_to_thread(_fsync_directory, destination.parent)
            return tuple(destination / item.filename for item in transfer.items)
        except asyncio.CancelledError:
            raise
        except TestSetMaterializationError:
            raise
        except OSError as exc:
            raise TestSetMaterializationError(
                "TestSet workspace snapshot could not be published"
            ) from exc
        finally:
            await _critical_to_thread(_remove_owned_staging, staging)

    async def _download_item(
        self,
        context: RvcRunContext,
        item: TestSetTransferItem,
        destination: Path,
        cancellation: asyncio.Event,
    ) -> None:
        delay = 1.0
        for attempt in range(1, self.limits.download_attempts + 1):
            if cancellation.is_set():
                raise asyncio.CancelledError
            try:
                downloaded = await self.manager.download_test_set_item(
                    context.claim,
                    item,
                    destination,
                    cancellation=cancellation,
                )
                if downloaded != destination:
                    raise TestSetMaterializationError(
                        "TestSet client returned an unexpected destination"
                    )
                return
            except TestSetTransferCancelled as exc:
                raise asyncio.CancelledError from exc
            except TestSetTransferError as exc:
                if not exc.retryable or attempt >= self.limits.download_attempts:
                    raise TestSetMaterializationError(
                        "verified TestSet item download failed"
                    ) from exc
                try:
                    await asyncio.wait_for(cancellation.wait(), timeout=delay)
                except TimeoutError:
                    delay = min(delay * 2, 10.0)
                    continue
                raise asyncio.CancelledError from exc
        raise TestSetMaterializationError("verified TestSet item download failed")


class TestSetStageRunner:
    """Materialize a claim's TestSet beside Dataset receipt, then delegate."""

    def __init__(self, runner: RvcRunner, materializer: TestSetMaterializer) -> None:
        self.runner = runner
        self.materializer = materializer

    async def run_stage(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        transfer = context.claim.test_set_transfer
        if stage is JobStatus.DOWNLOADING_DATASET and transfer is not None:
            self.materializer.preflight(transfer)
        result = await self.runner.run_stage(stage, context, cancellation)
        if stage is not JobStatus.DOWNLOADING_DATASET or transfer is None:
            return result
        materialized = await self.materializer.materialize(context, cancellation)
        metadata = dict(result.metadata or {})
        metadata["test_set"] = dict(materialized.metadata or {})
        return StageResult(
            (*result.created_paths, *materialized.created_paths),
            metadata,
        )


def inspect_pcm_wave(
    path: Path,
    *,
    item: TestSetTransferItem,
    limits: TestSetMaterializationLimits,
    cancellation: asyncio.Event | None = None,
) -> PcmWaveInspection:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise TestSetMaterializationError("TestSet item cannot be opened safely") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size != item.size_bytes
        ):
            raise TestSetMaterializationError("TestSet item metadata is invalid")
        prefix = os.read(descriptor, 12)
        if len(prefix) != 12 or prefix[:4] != b"RIFF" or prefix[8:] != b"WAVE":
            raise TestSetMaterializationError("TestSet item is not a RIFF/WAVE file")
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            _check_cancelled(cancellation)
            chunk = os.read(descriptor, limits.chunk_bytes)
            if not chunk:
                break
            digest.update(chunk)
        if digest.hexdigest() != item.sha256:
            raise TestSetMaterializationError("TestSet item checksum is invalid")

        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            with os.fdopen(os.dup(descriptor), "rb") as source:
                with wave.open(source, "rb") as audio:
                    channels = audio.getnchannels()
                    sample_rate = audio.getframerate()
                    sample_width = audio.getsampwidth()
                    frame_count = audio.getnframes()
                    compression = audio.getcomptype()
                    if (
                        compression != "NONE"
                        or channels <= 0
                        or sample_rate <= 0
                        or not 1 <= sample_width <= 4
                    ):
                        raise TestSetMaterializationError("TestSet item must use uncompressed PCM")
                    if frame_count <= 0:
                        raise TestSetMaterializationError("TestSet item has no PCM frames")
                    frame_width = channels * sample_width
                    decoded_frames = 0
                    while decoded_frames < frame_count:
                        _check_cancelled(cancellation)
                        requested = min(65_536, frame_count - decoded_frames)
                        frame_bytes = audio.readframes(requested)
                        if not frame_bytes or len(frame_bytes) % frame_width:
                            raise TestSetMaterializationError("TestSet PCM data is truncated")
                        decoded_frames += len(frame_bytes) // frame_width
                        if decoded_frames > frame_count:
                            raise TestSetMaterializationError(
                                "TestSet PCM frame count is inconsistent"
                            )
                    if decoded_frames != frame_count:
                        raise TestSetMaterializationError("TestSet PCM data is truncated")
                    if audio.readframes(1):
                        raise TestSetMaterializationError("TestSet PCM frame count is inconsistent")
        except (EOFError, wave.Error) as exc:
            raise TestSetMaterializationError("TestSet WAV structure is invalid") from exc

        duration = frame_count / sample_rate
        tolerance = max(limits.duration_tolerance_seconds, 1 / sample_rate)
        if (
            sample_rate != item.sample_rate_hz
            or channels != item.channels
            or not limits.min_sample_rate_hz <= sample_rate <= limits.max_sample_rate_hz
            or channels > limits.max_channels
            or duration > limits.max_duration_seconds
            or abs(duration - item.duration_seconds) > tolerance
        ):
            raise TestSetMaterializationError(
                "TestSet PCM metadata does not match the verified descriptor"
            )
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise TestSetMaterializationError("TestSet item changed during validation")
        return PcmWaveInspection(
            size_bytes=before.st_size,
            sha256=digest.hexdigest(),
            sample_rate_hz=sample_rate,
            channels=channels,
            duration_seconds=duration,
        )
    except OSError as exc:
        raise TestSetMaterializationError("TestSet item could not be verified") from exc
    finally:
        os.close(descriptor)


def _preflight_transfer(
    transfer: TestSetTransfer,
    limits: TestSetMaterializationLimits,
) -> None:
    if len(transfer.items) > limits.max_items:
        raise TestSetMaterializationError("TestSet item count exceeds the Worker limit")
    total = 0
    total_duration = 0.0
    for item in transfer.items:
        if item.size_bytes > limits.max_item_bytes:
            raise TestSetMaterializationError("TestSet item exceeds the Worker byte limit")
        total += item.size_bytes
        if total > limits.max_total_bytes:
            raise TestSetMaterializationError("TestSet exceeds the Worker total-byte limit")
        if not math.isfinite(item.duration_seconds):
            raise TestSetMaterializationError("TestSet descriptor contains a non-finite duration")
        total_duration += item.duration_seconds
        if not math.isfinite(total_duration) or total_duration > limits.max_total_duration_seconds:
            raise TestSetMaterializationError("TestSet exceeds the Worker total-duration limit")
        if (
            not limits.min_sample_rate_hz <= item.sample_rate_hz <= limits.max_sample_rate_hz
            or item.channels > limits.max_channels
            or item.duration_seconds > limits.max_duration_seconds
        ):
            raise TestSetMaterializationError("TestSet descriptor exceeds the Worker PCM limits")


async def _wait_for_cancellation(cancellation: asyncio.Event) -> None:
    while not cancellation.is_set():
        try:
            await cancellation.wait()
        except TimeoutError:
            await asyncio.sleep(0)


def _assert_safe_parent_and_no_stale_partials(inputs: Path) -> None:
    try:
        metadata = inputs.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise TestSetMaterializationError("TestSet input parent is unsafe")
        for entry in os.scandir(inputs):
            if entry.name.startswith(".test_set.") and entry.name.endswith(".partial"):
                raise TestSetMaterializationError(
                    "stale TestSet materialization directory is present"
                )
    except TestSetMaterializationError:
        raise
    except OSError as exc:
        raise TestSetMaterializationError("TestSet input parent cannot be inspected") from exc


def _path_exists_or_symlink(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise TestSetMaterializationError("TestSet destination cannot be inspected") from exc
    return True


def _validate_materialization(
    destination: Path,
    transfer: TestSetTransfer,
    limits: TestSetMaterializationLimits,
    cancellation: asyncio.Event | None,
) -> tuple[Path, ...]:
    try:
        metadata = destination.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise TestSetMaterializationError("TestSet destination is not a safe directory")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise TestSetMaterializationError("TestSet destination permissions are invalid")
        entries = list(os.scandir(destination))
    except TestSetMaterializationError:
        raise
    except OSError as exc:
        raise TestSetMaterializationError("TestSet destination cannot be inspected") from exc
    expected = [item.filename for item in transfer.items]
    actual = [entry.name for entry in entries]
    if len(actual) != len(expected) or set(actual) != set(expected):
        raise TestSetMaterializationError(
            "TestSet destination contains missing, extra, or stale entries"
        )
    paths: list[Path] = []
    for item in transfer.items:
        _check_cancelled(cancellation)
        path = destination / item.filename
        inspect_pcm_wave(path, item=item, limits=limits, cancellation=cancellation)
        paths.append(path)
    try:
        after = destination.lstat()
        final_entries = [entry.name for entry in os.scandir(destination)]
    except OSError as exc:
        raise TestSetMaterializationError("TestSet destination changed during validation") from exc
    if (
        (metadata.st_dev, metadata.st_ino) != (after.st_dev, after.st_ino)
        or len(final_entries) != len(expected)
        or set(final_entries) != set(expected)
    ):
        raise TestSetMaterializationError("TestSet destination changed during validation")
    return tuple(paths)


def _write_transfer_marker(path: Path, transfer: TestSetTransfer) -> None:
    try:
        parent_metadata = path.parent.lstat()
        if (
            stat.S_ISLNK(parent_metadata.st_mode)
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        ):
            raise TestSetMaterializationError("TestSet transfer marker is unsafe")
        try:
            existing = path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            stat.S_ISLNK(existing.st_mode)
            or not stat.S_ISREG(existing.st_mode)
            or stat.S_IMODE(existing.st_mode) != 0o600
        ):
            raise TestSetMaterializationError("TestSet transfer marker is unsafe")
    except TestSetMaterializationError:
        raise
    except OSError as exc:
        raise TestSetMaterializationError("TestSet transfer marker is unsafe") from exc
    document: dict[str, object] = {
        "schema_version": 1,
        "test_set_id": transfer.test_set_id,
        "family_id": transfer.family_id,
        "revision": transfer.revision,
        "manifest_sha256": transfer.manifest_sha256,
        "sample_plan_sha256": transfer.sample_plan_sha256,
        # The Worker cannot reconstruct Manager database snapshot semantics;
        # this hash is recorded as lease-bound Manager revalidation evidence.
        "sample_plan_revalidation": "manager_claim_snapshot",
        "inference_config": transfer.inference_config.model_dump(mode="json"),
        "inference_config_sha256": transfer.inference_config_sha256,
        "items": [
            {
                "test_set_item_id": item.test_set_item_id,
                "item_key": item.item_key,
                "sort_order": item.sort_order,
                "filename": item.filename,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "sample_rate_hz": item.sample_rate_hz,
                "channels": item.channels,
                "duration_seconds": item.duration_seconds,
            }
            for item in transfer.items
        ],
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        content = json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        _write_all(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise TestSetMaterializationError("TestSet transfer marker could not be written") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _remove_owned_staging(staging: Path) -> None:
    try:
        metadata = staging.lstat()
    except FileNotFoundError:
        return
    except OSError:
        return
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
        shutil.rmtree(staging, ignore_errors=True)


def _check_cancelled(cancellation: asyncio.Event | None) -> None:
    if cancellation is not None and cancellation.is_set():
        raise TestSetMaterializationCancelled("TestSet validation was cancelled")


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short TestSet marker write")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _secure_directory_mode(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise TestSetMaterializationError("TestSet staging path is not a directory")
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)
