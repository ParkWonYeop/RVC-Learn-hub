from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import cast

from ..config import Settings
from ..dataset_ingestion import (
    DatasetIngestionError,
    DatasetIngestionResult,
    IngestionLimits,
    ingest_dataset,
)
from ..models import Dataset
from ..schemas import (
    DatasetPcmLoudnessRead,
    DatasetPcmLoudnessUnavailableReason,
    DatasetPcmQualityRead,
    DatasetRead,
    DatasetUploadInitRequest,
)

_MIN_UPLOAD_TTL_SECONDS = 300
_ASSUMED_MIN_UPLOAD_BYTES_PER_SECOND = 2 * 1024**2


class DatasetPreparationError(RuntimeError):
    def __init__(self, failure_code: str, *, retryable: bool) -> None:
        super().__init__("dataset preparation failed")
        self.failure_code = failure_code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class PreparedDatasetSnapshot:
    root: Path
    ingestion: DatasetIngestionResult
    prepared_flat_archive: Path
    prepared_flat_size_bytes: int
    prepared_flat_sha256: str
    manifest_sha256: str
    quality_report_sha256: str


def dataset_to_read(dataset: Dataset) -> DatasetRead:
    algorithm = dataset.pcm_quality_algorithm
    validated_file_count = dataset.pcm_validated_file_count
    sample_count = dataset.pcm_sample_count
    clipping_ratio = dataset.pcm_clipping_ratio
    silence_ratio = dataset.pcm_silence_ratio
    rms_ratio = dataset.pcm_rms_ratio
    silence_threshold_dbfs = dataset.pcm_silence_threshold_dbfs
    pcm_values = (
        algorithm,
        validated_file_count,
        sample_count,
        clipping_ratio,
        silence_ratio,
        rms_ratio,
        silence_threshold_dbfs,
    )
    if all(value is None for value in pcm_values):
        pcm_quality = None
    elif any(value is None for value in pcm_values):
        raise RuntimeError("Dataset PCM quality columns are incomplete")
    else:
        if algorithm != "pcm-sample-weighted-v1":
            raise RuntimeError("Dataset PCM quality algorithm is unsupported")
        assert validated_file_count is not None
        assert sample_count is not None
        assert clipping_ratio is not None
        assert silence_ratio is not None
        assert rms_ratio is not None
        assert silence_threshold_dbfs is not None
        loudness_algorithm = dataset.pcm_loudness_algorithm
        loudness_analyzed_file_count = dataset.pcm_loudness_analyzed_file_count
        loudness_block_count = dataset.pcm_loudness_block_count
        loudness_gated_block_count = dataset.pcm_loudness_gated_block_count
        integrated_lufs = dataset.pcm_integrated_lufs
        loudness_unavailable_reason = dataset.pcm_loudness_unavailable_reason
        loudness_required_values = (
            loudness_algorithm,
            loudness_analyzed_file_count,
            loudness_block_count,
            loudness_gated_block_count,
        )
        loudness_values = (
            *loudness_required_values,
            integrated_lufs,
            loudness_unavailable_reason,
        )
        if all(value is None for value in loudness_values):
            # Historical PCM aggregates finalized before the LUFS migration stay
            # explicitly null; raw quality JSON is never reinterpreted/backfilled.
            loudness = None
        elif any(value is None for value in loudness_required_values):
            raise RuntimeError("Dataset PCM loudness columns are incomplete")
        else:
            if loudness_algorithm != "itu-r-bs1770-4-mono-stereo-v1":
                raise RuntimeError("Dataset PCM loudness algorithm is unsupported")
            if loudness_unavailable_reason not in {
                None,
                "below_absolute_gate",
                "insufficient_duration",
                "unsupported_channel_layout",
                "unsupported_sample_rate",
            }:
                raise RuntimeError("Dataset PCM loudness unavailable reason is unsupported")
            assert loudness_analyzed_file_count is not None
            assert loudness_block_count is not None
            assert loudness_gated_block_count is not None
            loudness = DatasetPcmLoudnessRead(
                algorithm="itu-r-bs1770-4-mono-stereo-v1",
                scope="global-gate-over-per-file-complete-blocks-v1",
                block_duration_ms=400,
                block_overlap_percent=75,
                absolute_gate_lufs=-70.0,
                relative_gate_lu=-10.0,
                analyzed_file_count=loudness_analyzed_file_count,
                block_count=loudness_block_count,
                gated_block_count=loudness_gated_block_count,
                integrated_lufs=integrated_lufs,
                unavailable_reason=cast(
                    DatasetPcmLoudnessUnavailableReason | None,
                    loudness_unavailable_reason,
                ),
            )
        pcm_quality = DatasetPcmQualityRead(
            algorithm="pcm-sample-weighted-v1",
            validated_file_count=validated_file_count,
            sample_count=sample_count,
            clipping_ratio=clipping_ratio,
            silence_ratio=silence_ratio,
            rms_ratio=rms_ratio,
            silence_threshold_dbfs=silence_threshold_dbfs,
            loudness=loudness,
        )
    return DatasetRead.model_validate(dataset).model_copy(update={"pcm_quality": pcm_quality})


def dataset_ready_for_training(dataset: Dataset) -> bool:
    return (
        dataset.is_usable
        and dataset.status in {"ready", "legacy_imported"}
        and dataset.flat_storage_uri is not None
    )


def dataset_upload_request_fingerprint(payload: DatasetUploadInitRequest) -> str:
    canonical = json.dumps(
        payload.model_dump(mode="json", exclude={"idempotency_key"}),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def dataset_upload_ttl_seconds(size_bytes: int, settings: Settings) -> int:
    transfer_seconds = (
        size_bytes + _ASSUMED_MIN_UPLOAD_BYTES_PER_SECOND - 1
    ) // _ASSUMED_MIN_UPLOAD_BYTES_PER_SECOND
    return min(
        settings.dataset_upload_ttl_seconds,
        _MIN_UPLOAD_TTL_SECONDS + transfer_seconds,
    )


def dataset_temporary_object_key(dataset_id: str, session_id: str) -> str:
    return f"datasets/staging/{dataset_id}/{session_id}"


def dataset_verified_object_keys(
    dataset_id: str,
    upload_session_id: str,
    extension: str,
) -> dict[str, str]:
    # Canonical publications are scoped to one immutable upload-session id.
    # A stale finalizer from an older generation can therefore neither block
    # nor remove the canonical objects of a replacement generation.
    prefix = f"datasets/verified/{dataset_id}/uploads/{upload_session_id}"
    return {
        "original": f"{prefix}/original{extension}",
        "prepared_flat": f"{prefix}/prepared_flat.zip",
        "manifest": f"{prefix}/manifest.json",
        "quality_report": f"{prefix}/quality_report.json",
    }


def derive_dataset_upload_token(
    upload_session_id: str,
    expires_at_timestamp: int,
    settings: Settings,
) -> str:
    message = f"dataset-upload\x1f{upload_session_id}\x1f{expires_at_timestamp}".encode()
    digest = hmac.new(
        settings.worker_token_pepper.get_secret_value().encode("utf-8"),
        message,
        hashlib.sha256,
    ).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"rvcd_{encoded}"


def validate_dataset_content_signature(path: Path, extension: str) -> None:
    try:
        with path.open("rb") as source:
            header = source.read(16)
    except OSError as exc:
        raise DatasetPreparationError("dataset_snapshot_io_error", retryable=True) from exc
    valid = False
    if extension == ".zip":
        try:
            valid = header.startswith(
                (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
            ) and zipfile.is_zipfile(path)
        except OSError as exc:
            raise DatasetPreparationError("dataset_snapshot_io_error", retryable=True) from exc
    elif extension == ".wav":
        valid = len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE"
    elif extension == ".flac":
        valid = header.startswith(b"fLaC")
    elif extension == ".mp3":
        valid = header.startswith(b"ID3") or (
            len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0
        )
    elif extension == ".m4a":
        valid = len(header) >= 12 and header[4:8] == b"ftyp"
    elif extension == ".ogg":
        valid = header.startswith(b"OggS")
    elif extension == ".aac":
        valid = header.startswith(b"ADIF") or (
            len(header) >= 2 and header[0] == 0xFF and header[1] & 0xF6 == 0xF0
        )
    if not valid:
        raise DatasetPreparationError("content_signature_mismatch", retryable=False)


def prepare_dataset_snapshot(
    verified_source: Path,
    *,
    extension: str,
    settings: Settings,
) -> PreparedDatasetSnapshot:
    root: Path | None = None
    try:
        ingestion_root = settings.dataset_ingestion_root.expanduser()
        ingestion_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if ingestion_root.is_symlink():
            raise DatasetPreparationError("unsafe_ingestion_root", retryable=False)
        ingestion_root = ingestion_root.resolve()
        os.chmod(ingestion_root, 0o700)
        root = Path(tempfile.mkdtemp(prefix="dataset-ingestion-", dir=ingestion_root))
        os.chmod(root, 0o700)
        source = root / f"source{extension}"
        _copy_verified_source(verified_source, source)
        validate_dataset_content_signature(source, extension)
        result = ingest_dataset(
            source,
            job_temp_root=root,
            destination=root / "canonical",
            limits=IngestionLimits(
                max_archive_bytes=settings.dataset_upload_max_bytes,
                max_entries=settings.dataset_max_entries,
                max_file_uncompressed_bytes=settings.dataset_max_file_uncompressed_bytes,
                max_total_uncompressed_bytes=settings.dataset_max_total_uncompressed_bytes,
                max_compression_ratio=settings.dataset_max_compression_ratio,
                copy_chunk_bytes=settings.artifact_stream_chunk_bytes,
            ),
        )
        prepared_archive = root / "prepared_flat.zip"
        _write_deterministic_flat_archive(result.flat_directory, prepared_archive)
        return PreparedDatasetSnapshot(
            root=root,
            ingestion=result,
            prepared_flat_archive=prepared_archive,
            prepared_flat_size_bytes=prepared_archive.stat().st_size,
            prepared_flat_sha256=sha256_file(prepared_archive),
            manifest_sha256=sha256_file(result.manifest_path),
            quality_report_sha256=sha256_file(result.quality_report_path),
        )
    except DatasetPreparationError:
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)
        raise
    except DatasetIngestionError as exc:
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)
        raise DatasetPreparationError(exc.code, retryable=False) from exc
    except OSError as exc:
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)
        raise DatasetPreparationError("dataset_snapshot_io_error", retryable=True) from exc


def cleanup_dataset_snapshot(snapshot: PreparedDatasetSnapshot) -> None:
    try:
        shutil.rmtree(snapshot.root)
    except OSError as exc:
        raise DatasetPreparationError("dataset_snapshot_cleanup_failed", retryable=True) from exc


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(chunk_size):
                digest.update(chunk)
    except OSError as exc:
        raise DatasetPreparationError("dataset_snapshot_io_error", retryable=True) from exc
    return digest.hexdigest()


def _copy_verified_source(source: Path, destination: Path) -> None:
    try:
        with source.open("rb") as reader, destination.open("xb") as writer:
            os.chmod(destination, 0o600)
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
    except OSError as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise DatasetPreparationError("dataset_snapshot_io_error", retryable=True) from exc


def _write_deterministic_flat_archive(source: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(
            destination,
            mode="x",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as archive:
            for path in sorted(source.iterdir(), key=lambda item: item.name):
                if not path.is_file() or path.is_symlink():
                    raise DatasetPreparationError("unsafe_prepared_flat", retryable=False)
                info = zipfile.ZipInfo(f"prepared_flat/{path.name}")
                info.date_time = (1980, 1, 1, 0, 0, 0)
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o100600 << 16
                info.create_system = 3
                with (
                    path.open("rb") as reader,
                    archive.open(
                        info,
                        mode="w",
                        force_zip64=True,
                    ) as writer,
                ):
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
        os.chmod(destination, 0o600)
    except DatasetPreparationError:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise DatasetPreparationError("prepared_flat_publish_failed", retryable=True) from exc


def dataset_extension(filename: str) -> str:
    return PurePath(filename).suffix.lower()
