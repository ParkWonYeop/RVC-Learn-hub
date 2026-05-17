"""Verified Dataset transfer and repeated safe materialization for real runners."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import unicodedata
import uuid
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from rvc_orchestrator_contracts import JobStatus

from .client import (
    DatasetTransferCancelled,
    DatasetTransferError,
    ManagerClient,
)
from .runner import RvcRunContext, RvcRunner, RvcRunnerError, StageResult


class DatasetMaterializationError(RvcRunnerError):
    """The canonical archive could not be proven safe inside the workspace."""


class DatasetMaterializationCancelled(DatasetMaterializationError):
    """The validation/extraction loop observed cooperative cancellation."""


@dataclass(frozen=True, slots=True)
class DatasetMaterializationLimits:
    max_archive_bytes: int = 5 * 1024**3
    max_entries: int = 10_000
    max_file_bytes: int = 2 * 1024**3
    max_total_bytes: int = 20 * 1024**3
    max_compression_ratio: float = 200.0
    chunk_bytes: int = 1024**2
    download_attempts: int = 3

    def __post_init__(self) -> None:
        if (
            self.max_archive_bytes <= 0
            or self.max_entries <= 0
            or self.max_file_bytes <= 0
            or self.max_total_bytes <= 0
            or self.max_file_bytes > self.max_total_bytes
            or self.max_compression_ratio < 1
            or self.chunk_bytes <= 0
            or not 1 <= self.download_attempts <= 10
        ):
            raise ValueError("invalid Dataset materialization limits")


@dataclass(frozen=True, slots=True)
class PreparedFlatEntry:
    archive_name: str
    filename: str
    size_bytes: int
    crc32: int


@dataclass(frozen=True, slots=True)
class PreparedFlatInspection:
    entries: tuple[PreparedFlatEntry, ...]
    total_size_bytes: int


class DatasetMaterializer:
    """Stage dependency that downloads and materializes ``prepared_flat.zip``."""

    def __init__(
        self,
        manager: ManagerClient,
        *,
        limits: DatasetMaterializationLimits | None = None,
    ) -> None:
        self.manager = manager
        self.limits = limits or DatasetMaterializationLimits()

    async def run_stage(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        if context.claim.dataset_transfer is None:
            raise DatasetMaterializationError("real Job claim has no Dataset transfer")
        if stage is JobStatus.DOWNLOADING_DATASET:
            return await self._download(context, cancellation)
        if stage is JobStatus.VALIDATING_DATASET:
            return await self._validate(context, cancellation)
        if stage is JobStatus.PREPARING_FLAT_DATASET:
            return await self._materialize(context, cancellation)
        raise DatasetMaterializationError(f"Dataset materializer does not implement {stage.value}")

    async def _download(
        self,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        transfer = context.claim.dataset_transfer
        assert transfer is not None
        archive = context.workspace.inputs / "prepared_flat.zip"
        delay = 1.0
        for attempt in range(1, self.limits.download_attempts + 1):
            if cancellation.is_set():
                raise asyncio.CancelledError
            try:
                downloaded = await self.manager.download_dataset(
                    context.claim,
                    archive,
                    cancellation=cancellation,
                )
                context.workspace.assert_path(downloaded)
                return StageResult(
                    (downloaded,),
                    {
                        "dataset_id": context.claim.config.dataset_id,
                        "size_bytes": transfer.size_bytes,
                        "sha256": transfer.sha256,
                    },
                )
            except DatasetTransferCancelled as exc:
                raise asyncio.CancelledError from exc
            except DatasetTransferError as exc:
                if not exc.retryable or attempt >= self.limits.download_attempts:
                    raise DatasetMaterializationError("verified Dataset download failed") from exc
                try:
                    await asyncio.wait_for(cancellation.wait(), timeout=delay)
                except TimeoutError:
                    delay = min(delay * 2, 10.0)
                    continue
                raise asyncio.CancelledError from exc
        raise DatasetMaterializationError("verified Dataset download failed")

    async def _validate(
        self,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        transfer = context.claim.dataset_transfer
        assert transfer is not None
        archive = context.workspace.inputs / "prepared_flat.zip"
        try:
            inspection = await asyncio.to_thread(
                inspect_prepared_flat_archive,
                archive,
                expected_size=transfer.size_bytes,
                expected_sha256=transfer.sha256,
                limits=self.limits,
                cancellation=cancellation,
            )
        except DatasetMaterializationCancelled as exc:
            raise asyncio.CancelledError from exc
        report = context.workspace.outputs / "dataset_report.json"
        await asyncio.to_thread(
            _write_json_atomic,
            report,
            {
                "valid": True,
                "dataset_id": transfer.dataset_id,
                "archive_sha256": transfer.sha256,
                "file_count": len(inspection.entries),
                "total_size_bytes": inspection.total_size_bytes,
            },
        )
        return StageResult(
            (archive, report),
            {
                "file_count": len(inspection.entries),
                "total_size_bytes": inspection.total_size_bytes,
            },
        )

    async def _materialize(
        self,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        transfer = context.claim.dataset_transfer
        assert transfer is not None
        archive = context.workspace.inputs / "prepared_flat.zip"
        destination = context.workspace.inputs / "prepared_flat"
        try:
            files = await asyncio.to_thread(
                materialize_prepared_flat_archive,
                archive,
                destination,
                expected_size=transfer.size_bytes,
                expected_sha256=transfer.sha256,
                limits=self.limits,
                cancellation=cancellation,
            )
        except DatasetMaterializationCancelled as exc:
            raise asyncio.CancelledError from exc
        for path in files:
            context.workspace.assert_path(path)
        return StageResult(tuple(files), {"file_count": len(files)})


class DatasetStageRunner:
    """Use the transfer dependency for real Dataset stages and delegate RVC work."""

    def __init__(self, runner: RvcRunner, materializer: DatasetMaterializer) -> None:
        self.runner = runner
        self.materializer = materializer

    async def run_stage(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        if stage in {
            JobStatus.DOWNLOADING_DATASET,
            JobStatus.VALIDATING_DATASET,
            JobStatus.PREPARING_FLAT_DATASET,
        }:
            return await self.materializer.run_stage(stage, context, cancellation)
        return await self.runner.run_stage(stage, context, cancellation)


_CANONICAL_AUDIO_NAME = re.compile(r"^[0-9]{6}\.(wav|flac|mp3|m4a|ogg|aac)$")


def inspect_prepared_flat_archive(
    archive: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    limits: DatasetMaterializationLimits,
    cancellation: asyncio.Event | None = None,
) -> PreparedFlatInspection:
    return _read_prepared_flat_archive(
        archive,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
        limits=limits,
        cancellation=cancellation,
        extraction_root=None,
    )


def materialize_prepared_flat_archive(
    archive: Path,
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    limits: DatasetMaterializationLimits,
    cancellation: asyncio.Event | None = None,
) -> tuple[Path, ...]:
    inspection = inspect_prepared_flat_archive(
        archive,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
        limits=limits,
        cancellation=cancellation,
    )
    if destination.exists() or destination.is_symlink():
        return _validate_existing_materialization(destination, inspection, cancellation)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial")
    try:
        os.mkdir(staging, mode=0o700)
        repeated = _read_prepared_flat_archive(
            archive,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            limits=limits,
            cancellation=cancellation,
            extraction_root=staging,
        )
        if repeated != inspection:
            raise DatasetMaterializationError("Dataset archive changed during materialization")
        if destination.exists() or destination.is_symlink():
            existing = _validate_existing_materialization(
                destination,
                inspection,
                cancellation,
            )
            shutil.rmtree(staging)
            return existing
        os.rename(staging, destination)
        _fsync_directory(destination.parent)
        return tuple(destination / entry.filename for entry in inspection.entries)
    except DatasetMaterializationError:
        raise
    except OSError as exc:
        raise DatasetMaterializationError("prepared Dataset could not be published") from exc
    finally:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)


def _read_prepared_flat_archive(
    archive: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    limits: DatasetMaterializationLimits,
    cancellation: asyncio.Event | None,
    extraction_root: Path | None,
) -> PreparedFlatInspection:
    if expected_size <= 0 or expected_size > limits.max_archive_bytes:
        raise DatasetMaterializationError("Dataset archive size exceeds the Worker limit")
    descriptor: int | None = None
    try:
        descriptor = os.open(archive, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
            raise DatasetMaterializationError("Dataset archive is not a regular exact-size file")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, limits.chunk_bytes):
            _check_cancelled(cancellation)
            digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise DatasetMaterializationError("Dataset archive checksum changed before extraction")
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = None
            with zipfile.ZipFile(source, mode="r", allowZip64=True) as bundle:
                if bundle.comment:
                    raise DatasetMaterializationError("Dataset archive comments are forbidden")
                infos = bundle.infolist()
                if not infos or len(infos) > limits.max_entries:
                    raise DatasetMaterializationError("Dataset archive file count is invalid")
                seen: set[str] = set()
                entries: list[PreparedFlatEntry] = []
                total_size = 0
                for info in infos:
                    _check_cancelled(cancellation)
                    entry = _validated_entry(info, seen, limits)
                    total_size += entry.size_bytes
                    if total_size > limits.max_total_bytes:
                        raise DatasetMaterializationError(
                            "Dataset archive total size exceeds the Worker limit"
                        )
                    entries.append(entry)
                    target = extraction_root / entry.filename if extraction_root else None
                    _stream_entry(
                        bundle,
                        info,
                        entry,
                        target,
                        chunk_bytes=limits.chunk_bytes,
                        cancellation=cancellation,
                    )
                return PreparedFlatInspection(tuple(entries), total_size)
    except DatasetMaterializationError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, zlib.error) as exc:
        raise DatasetMaterializationError("Dataset archive is corrupt or unreadable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validated_entry(
    info: zipfile.ZipInfo,
    seen: set[str],
    limits: DatasetMaterializationLimits,
) -> PreparedFlatEntry:
    name = unicodedata.normalize("NFC", info.filename)
    parts = name.split("/")
    if (
        name != info.filename
        or "\\" in name
        or info.is_dir()
        or len(parts) != 2
        or parts[0] != "prepared_flat"
        or not _CANONICAL_AUDIO_NAME.fullmatch(parts[1])
        or info.flag_bits & 0x1
        or info.compress_type != zipfile.ZIP_STORED
    ):
        raise DatasetMaterializationError("Dataset archive contains an unsafe member")
    normalized = name.casefold()
    if normalized in seen:
        raise DatasetMaterializationError("Dataset archive contains duplicate members")
    seen.add(normalized)
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    if info.create_system != 3 or not stat.S_ISREG(unix_mode):
        raise DatasetMaterializationError("Dataset archive member is not a regular file")
    if info.file_size <= 0 or info.file_size > limits.max_file_bytes:
        raise DatasetMaterializationError("Dataset archive member exceeds the file limit")
    ratio = info.file_size / max(1, info.compress_size)
    if ratio > limits.max_compression_ratio:
        raise DatasetMaterializationError("Dataset archive compression ratio is unsafe")
    return PreparedFlatEntry(name, parts[1], info.file_size, info.CRC)


def _stream_entry(
    bundle: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    entry: PreparedFlatEntry,
    target: Path | None,
    *,
    chunk_bytes: int,
    cancellation: asyncio.Event | None,
) -> None:
    descriptor: int | None = None
    total = 0
    checksum = 0
    try:
        if target is not None:
            descriptor = os.open(
                target,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise DatasetMaterializationError("Dataset member target is not regular")
        with bundle.open(info, mode="r") as member:
            while chunk := member.read(chunk_bytes):
                _check_cancelled(cancellation)
                total += len(chunk)
                if total > entry.size_bytes:
                    raise DatasetMaterializationError("Dataset member exceeds its declared size")
                checksum = zlib.crc32(chunk, checksum)
                if descriptor is not None:
                    _write_all(descriptor, chunk)
        if total != entry.size_bytes or checksum & 0xFFFFFFFF != entry.crc32:
            raise DatasetMaterializationError("Dataset member checksum or size is invalid")
        if descriptor is not None:
            os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_existing_materialization(
    destination: Path,
    inspection: PreparedFlatInspection,
    cancellation: asyncio.Event | None,
) -> tuple[Path, ...]:
    metadata = destination.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise DatasetMaterializationError("existing prepared Dataset directory is unsafe")
    expected = {entry.filename: entry for entry in inspection.entries}
    actual = {path.name: path for path in destination.iterdir()}
    if set(actual) != set(expected):
        raise DatasetMaterializationError("existing prepared Dataset contents differ")
    verified: list[Path] = []
    for name, entry in expected.items():
        _check_cancelled(cancellation)
        path = actual[name]
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        checksum = 0
        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size != entry.size_bytes:
                raise DatasetMaterializationError("existing prepared Dataset file is invalid")
            while chunk := os.read(descriptor, 1024**2):
                _check_cancelled(cancellation)
                checksum = zlib.crc32(chunk, checksum)
        finally:
            os.close(descriptor)
        if checksum & 0xFFFFFFFF != entry.crc32:
            raise DatasetMaterializationError("existing prepared Dataset checksum differs")
        verified.append(path)
    return tuple(verified)


def _check_cancelled(cancellation: asyncio.Event | None) -> None:
    if cancellation is not None and cancellation.is_set():
        raise DatasetMaterializationCancelled("Dataset materialization cancelled")


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short Dataset member write")
        view = view[written:]


def _write_json_atomic(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    content = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        _write_all(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
