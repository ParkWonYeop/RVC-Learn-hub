from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import struct
import tempfile
import unicodedata
import wave
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Literal

SUPPORTED_AUDIO_EXTENSIONS = frozenset({".wav", ".flac", ".mp3", ".m4a", ".ogg", ".aac"})
SUPPORTED_ZIP_COMPRESSION = frozenset(
    {
        zipfile.ZIP_STORED,
        zipfile.ZIP_DEFLATED,
        zipfile.ZIP_BZIP2,
        zipfile.ZIP_LZMA,
    }
)

SourceKind = Literal["audio", "zip"]
ValidationStatus = Literal["validated_pcm", "decoder_pending"]
SkipReason = Literal["hidden", "macos_metadata", "non_audio"]
RejectionReason = Literal["empty_audio_file", "invalid_wav"]
DATASET_PCM_QUALITY_ALGORITHM = "pcm-sample-weighted-v1"
LoudnessUnavailableReason = Literal[
    "below_absolute_gate",
    "insufficient_duration",
    "unsupported_channel_layout",
    "unsupported_sample_rate",
]
DATASET_PCM_LOUDNESS_ALGORITHM = "itu-r-bs1770-4-mono-stereo-v1"
DATASET_PCM_LOUDNESS_SCOPE = "global-gate-over-per-file-complete-blocks-v1"
DATASET_PCM_LOUDNESS_BLOCK_DURATION_MS = 400
DATASET_PCM_LOUDNESS_BLOCK_OVERLAP_PERCENT = 75
DATASET_PCM_LOUDNESS_ABSOLUTE_GATE_LUFS = -70.0
DATASET_PCM_LOUDNESS_RELATIVE_GATE_LU = -10.0
DATASET_PCM_LOUDNESS_MIN_SAMPLE_RATE_HZ = 8_000
DATASET_PCM_LOUDNESS_MAX_SAMPLE_RATE_HZ = 384_000
DATASET_PCM_LOUDNESS_MAX_BLOCK_COUNT = 9_007_199_254_740_991

# The bilinear-transform parameters below reproduce the two normative 48 kHz
# K-weighting coefficient sets in ITU-R BS.1770-4 Tables 1 and 2. Keeping them
# named and versioned avoids silently changing historical LUFS semantics.
_K_WEIGHTING_SHELF_GAIN_DB = 3.999843853973347
_K_WEIGHTING_SHELF_Q = 0.7071752369554196
_K_WEIGHTING_SHELF_FREQUENCY_HZ = 1681.974450955533
_K_WEIGHTING_HIGH_PASS_Q = 0.5003270373238773
_K_WEIGHTING_HIGH_PASS_FREQUENCY_HZ = 38.13547087602444


class DatasetIngestionError(Exception):
    """Base class for deterministic, caller-safe ingestion failures."""

    code = "dataset_ingestion_failed"


class UnsafePathError(DatasetIngestionError):
    code = "unsafe_path"


class UnsafeArchiveError(DatasetIngestionError):
    code = "unsafe_archive"


class DatasetLimitExceededError(DatasetIngestionError):
    code = "dataset_limit_exceeded"


class UnsupportedInputError(DatasetIngestionError):
    code = "unsupported_input"


class DestinationExistsError(DatasetIngestionError):
    code = "destination_exists"


class PublishConflictError(DatasetIngestionError):
    code = "publish_conflict"


class InvalidWavError(DatasetIngestionError):
    code = "invalid_wav"


@dataclass(frozen=True, slots=True)
class IngestionLimits:
    max_archive_bytes: int = 5 * 1024**3
    max_entries: int = 10_000
    max_file_uncompressed_bytes: int = 2 * 1024**3
    max_total_uncompressed_bytes: int = 20 * 1024**3
    max_compression_ratio: float = 200.0
    copy_chunk_bytes: int = 1024 * 1024
    wav_analysis_chunk_frames: int = 4096
    silence_threshold_dbfs: float = -50.0

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_archive_bytes,
            self.max_entries,
            self.max_file_uncompressed_bytes,
            self.max_total_uncompressed_bytes,
            self.copy_chunk_bytes,
            self.wav_analysis_chunk_frames,
        )
        if any(value <= 0 for value in integer_limits):
            raise ValueError("ingestion size and chunk limits must be positive")
        if self.max_compression_ratio < 1.0 or not math.isfinite(self.max_compression_ratio):
            raise ValueError("max_compression_ratio must be finite and at least 1")
        if not -120.0 <= self.silence_threshold_dbfs < 0.0:
            raise ValueError("silence_threshold_dbfs must be in [-120, 0)")


@dataclass(frozen=True, slots=True)
class _PcmAggregateEvidence:
    sample_count: int
    clipped_sample_count: int
    silent_sample_count: int
    normalized_square_sum: float
    loudness: _PcmLoudnessScan


@dataclass(frozen=True, slots=True)
class _PcmLoudnessScan:
    unavailable_reason: LoudnessUnavailableReason | None
    block_count: int
    gated_block_count: int
    gated_energy_sum: float


@dataclass(frozen=True, slots=True)
class _BiquadCoefficients:
    b0: float
    b1: float
    b2: float
    a1: float
    a2: float


@dataclass(slots=True)
class _Biquad:
    coefficients: _BiquadCoefficients
    x1: float = 0.0
    x2: float = 0.0
    y1: float = 0.0
    y2: float = 0.0

    def process(self, value: float) -> float:
        coefficients = self.coefficients
        output = (
            coefficients.b0 * value
            + coefficients.b1 * self.x1
            + coefficients.b2 * self.x2
            - coefficients.a1 * self.y1
            - coefficients.a2 * self.y2
        )
        self.x2 = self.x1
        self.x1 = value
        self.y2 = self.y1
        self.y1 = output
        if not math.isfinite(output):
            raise InvalidWavError("K-weighting filter produced a non-finite sample")
        return output


class _LoudnessBlockScanner:
    """Bounded-memory K-weighted block scanner for mono/stereo PCM.

    Filter state and overlapping windows reset at every source-file boundary. Only
    complete 400 ms blocks enter the dataset-global two-stage gate, so unrelated
    files are never joined into a synthetic block.
    """

    __slots__ = (
        "_block_frames",
        "_filters",
        "_frames_seen",
        "_gate_lufs",
        "_hop_frames",
        "_ring",
        "_ring_index",
        "_window_energy",
        "block_count",
        "gated_block_count",
        "gated_energy_sum",
    )

    def __init__(self, sample_rate_hz: int, channels: int, gate_lufs: float) -> None:
        if channels not in {1, 2}:
            raise ValueError("loudness scanner accepts only mono or stereo PCM")
        if not math.isfinite(gate_lufs):
            raise ValueError("loudness gate must be finite")
        # Common audio rates produce exact 400/100 ms frame counts. For unusual
        # rates the half-up rounding rule is part of the versioned algorithm.
        self._block_frames = (sample_rate_hz * 2 + 2) // 5
        self._hop_frames = (sample_rate_hz + 5) // 10
        if self._block_frames <= 0 or self._hop_frames <= 0:
            raise ValueError("sample rate is too low for loudness blocks")
        shelf, high_pass = _k_weighting_coefficients(sample_rate_hz)
        self._filters = [(_Biquad(shelf), _Biquad(high_pass)) for _ in range(channels)]
        self._gate_lufs = gate_lufs
        self._ring = [0.0] * self._block_frames
        self._ring_index = 0
        self._window_energy = 0.0
        self._frames_seen = 0
        self.block_count = 0
        self.gated_block_count = 0
        self.gated_energy_sum = 0.0

    def process_frame(self, normalized_samples: tuple[float, ...]) -> None:
        if len(normalized_samples) != len(self._filters):
            raise InvalidWavError("PCM channel count changed during loudness analysis")
        frame_energy = 0.0
        for sample, (shelf, high_pass) in zip(normalized_samples, self._filters, strict=True):
            filtered = high_pass.process(shelf.process(sample))
            frame_energy += filtered * filtered
        if not math.isfinite(frame_energy) or frame_energy < 0.0:
            raise InvalidWavError("K-weighted frame energy is invalid")

        if self._frames_seen < self._block_frames:
            self._ring[self._frames_seen] = frame_energy
            self._window_energy += frame_energy
        else:
            previous = self._ring[self._ring_index]
            self._ring[self._ring_index] = frame_energy
            self._ring_index = (self._ring_index + 1) % self._block_frames
            self._window_energy += frame_energy - previous
        self._frames_seen += 1

        if (
            self._frames_seen >= self._block_frames
            and (self._frames_seen - self._block_frames) % self._hop_frames == 0
        ):
            self._record_complete_block()

    def _record_complete_block(self) -> None:
        block_energy = self._window_energy / self._block_frames
        # Rolling subtraction can create a tiny negative residual after a long
        # all-zero tail. It is energy-equivalent to zero, never a valid gate hit.
        if block_energy < 0.0 and block_energy > -1e-15:
            block_energy = 0.0
        if not math.isfinite(block_energy) or block_energy < 0.0:
            raise InvalidWavError("K-weighted block energy is invalid")
        self.block_count += 1
        if block_energy > 0.0 and _energy_to_lufs(block_energy) > self._gate_lufs:
            self.gated_block_count += 1
            self.gated_energy_sum += block_energy
            if not math.isfinite(self.gated_energy_sum):
                raise InvalidWavError("K-weighted gated energy is non-finite")


@dataclass(frozen=True, slots=True)
class AudioInspection:
    status: ValidationStatus
    duration_seconds: float | None
    sample_rate_hz: int | None
    channels: int | None
    sample_width_bytes: int | None
    peak_ratio: float | None
    clipping_ratio: float | None
    silence_ratio: float | None
    rms_ratio: float | None
    _aggregate_evidence: _PcmAggregateEvidence | None

    @classmethod
    def decoder_pending(cls) -> AudioInspection:
        return cls(
            status="decoder_pending",
            duration_seconds=None,
            sample_rate_hz=None,
            channels=None,
            sample_width_bytes=None,
            peak_ratio=None,
            clipping_ratio=None,
            silence_ratio=None,
            rms_ratio=None,
            _aggregate_evidence=None,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "duration_seconds": self.duration_seconds,
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "sample_width_bytes": self.sample_width_bytes,
            "peak_ratio": self.peak_ratio,
            "clipping_ratio": self.clipping_ratio,
            "silence_ratio": self.silence_ratio,
            "rms_ratio": self.rms_ratio,
        }


@dataclass(frozen=True, slots=True)
class PcmLoudnessAggregate:
    algorithm: str
    scope: str
    block_duration_ms: int
    block_overlap_percent: int
    absolute_gate_lufs: float
    relative_gate_lu: float
    analyzed_file_count: int
    block_count: int
    gated_block_count: int
    integrated_lufs: float | None
    unavailable_reason: LoudnessUnavailableReason | None

    def __post_init__(self) -> None:
        if self.algorithm != DATASET_PCM_LOUDNESS_ALGORITHM:
            raise ValueError("unsupported PCM loudness algorithm")
        if self.scope != DATASET_PCM_LOUDNESS_SCOPE:
            raise ValueError("unsupported PCM loudness aggregation scope")
        if (
            self.block_duration_ms != DATASET_PCM_LOUDNESS_BLOCK_DURATION_MS
            or self.block_overlap_percent != DATASET_PCM_LOUDNESS_BLOCK_OVERLAP_PERCENT
            or self.absolute_gate_lufs != DATASET_PCM_LOUDNESS_ABSOLUTE_GATE_LUFS
            or self.relative_gate_lu != DATASET_PCM_LOUDNESS_RELATIVE_GATE_LU
        ):
            raise ValueError("PCM loudness parameters do not match the algorithm version")
        if (
            not 0 <= self.analyzed_file_count <= 10_000
            or self.block_count < 0
            or self.block_count > DATASET_PCM_LOUDNESS_MAX_BLOCK_COUNT
            or self.gated_block_count < 0
            or self.gated_block_count > DATASET_PCM_LOUDNESS_MAX_BLOCK_COUNT
            or self.gated_block_count > self.block_count
        ):
            raise ValueError("PCM loudness counts are invalid")
        if self.integrated_lufs is None:
            if self.unavailable_reason is None or self.gated_block_count != 0:
                raise ValueError(
                    "unavailable PCM loudness must include a reason and no gated blocks"
                )
        elif (
            self.unavailable_reason is not None
            or self.gated_block_count == 0
            or not math.isfinite(self.integrated_lufs)
            or not -70.0 <= self.integrated_lufs <= 10.0
        ):
            raise ValueError("integrated PCM loudness is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "algorithm": self.algorithm,
            "scope": self.scope,
            "block_duration_ms": self.block_duration_ms,
            "block_overlap_percent": self.block_overlap_percent,
            "absolute_gate_lufs": self.absolute_gate_lufs,
            "relative_gate_lu": self.relative_gate_lu,
            "analyzed_file_count": self.analyzed_file_count,
            "block_count": self.block_count,
            "gated_block_count": self.gated_block_count,
            "integrated_lufs": self.integrated_lufs,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True, slots=True)
class PcmQualityAggregate:
    algorithm: str
    validated_file_count: int
    sample_count: int
    clipping_ratio: float
    silence_ratio: float
    rms_ratio: float
    silence_threshold_dbfs: float
    loudness: PcmLoudnessAggregate

    def to_dict(self) -> dict[str, object]:
        return {
            "algorithm": self.algorithm,
            "validated_file_count": self.validated_file_count,
            "sample_count": self.sample_count,
            "clipping_ratio": self.clipping_ratio,
            "silence_ratio": self.silence_ratio,
            "rms_ratio": self.rms_ratio,
            "silence_threshold_dbfs": self.silence_threshold_dbfs,
            "loudness": self.loudness.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ManifestFile:
    sequence: int
    canonical_path: str
    source_path: str
    extension: str
    size_bytes: int
    sha256: str
    inspection: AudioInspection

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "canonical_path": self.canonical_path,
            "source_path": self.source_path,
            "extension": self.extension,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "inspection": self.inspection.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    schema_version: int
    analysis_version: str
    wav_silence_threshold_dbfs: float
    content_sha256: str
    file_count: int
    total_bytes: int
    validated_wav_duration_seconds: float
    files: tuple[ManifestFile, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "analysis_version": self.analysis_version,
            "wav_silence_threshold_dbfs": self.wav_silence_threshold_dbfs,
            "content_sha256": self.content_sha256,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "validated_wav_duration_seconds": self.validated_wav_duration_seconds,
            "files": [entry.to_dict() for entry in self.files],
        }

    def to_json(self) -> str:
        return _json_document(self.to_dict())


@dataclass(frozen=True, slots=True)
class SkippedSource:
    source_path: str
    reason: SkipReason

    def to_dict(self) -> dict[str, object]:
        return {"source_path": self.source_path, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class RejectedSource:
    source_path: str
    reason: RejectionReason
    detail: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "source_path": self.source_path,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class DuplicateSource:
    source_path: str
    sha256: str
    duplicate_of: str

    def to_dict(self) -> dict[str, object]:
        return {
            "source_path": self.source_path,
            "sha256": self.sha256,
            "duplicate_of": self.duplicate_of,
        }


@dataclass(frozen=True, slots=True)
class QualityReport:
    schema_version: int
    source_kind: SourceKind
    source_file_entries: int
    included_count: int
    decoder_pending_count: int
    pcm_quality: PcmQualityAggregate | None
    skipped: tuple[SkippedSource, ...]
    rejected: tuple[RejectedSource, ...]
    duplicates: tuple[DuplicateSource, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_kind": self.source_kind,
            "source_file_entries": self.source_file_entries,
            "included_count": self.included_count,
            "decoder_pending_count": self.decoder_pending_count,
            "pcm_quality": self.pcm_quality.to_dict() if self.pcm_quality is not None else None,
            "skipped": [entry.to_dict() for entry in self.skipped],
            "rejected": [entry.to_dict() for entry in self.rejected],
            "duplicates": [entry.to_dict() for entry in self.duplicates],
        }

    def to_json(self) -> str:
        return _json_document(self.to_dict())


class NoUsableAudioError(DatasetIngestionError):
    code = "no_usable_audio"

    def __init__(self, report: QualityReport) -> None:
        super().__init__("the input contains no usable audio files")
        self.report = report


@dataclass(frozen=True, slots=True)
class DatasetIngestionResult:
    destination: Path
    flat_directory: Path
    manifest_path: Path
    quality_report_path: Path
    manifest: DatasetManifest
    quality_report: QualityReport


@dataclass(frozen=True, slots=True)
class _ArchiveMember:
    info: zipfile.ZipInfo
    normalized_path: str


@dataclass(slots=True)
class _CollectionState:
    files: list[ManifestFile]
    skipped: list[SkippedSource]
    rejected: list[RejectedSource]
    duplicates: list[DuplicateSource]
    digest_to_path: dict[str, str]
    actual_copied_bytes: int = 0
    source_file_entries: int = 0


def ingest_dataset(
    source: str | Path,
    *,
    job_temp_root: str | Path,
    destination: str | Path,
    limits: IngestionLimits | None = None,
) -> DatasetIngestionResult:
    """Validate and atomically publish one uploaded audio file or ZIP dataset.

    ``source`` and ``destination`` must both be inside ``job_temp_root``. The published
    destination has a flat ``prepared_flat`` audio directory plus deterministic manifest and
    quality-report sidecars. An existing destination is never intentionally replaced.
    """

    active_limits = limits or IngestionLimits()
    root = _validated_job_root(job_temp_root)
    source_path = _validated_source(source, root)
    destination_path = _validated_destination(destination, root)
    source_extension = source_path.suffix.lower()
    if source_extension == ".zip":
        source_kind: SourceKind = "zip"
    elif source_extension in SUPPORTED_AUDIO_EXTENSIONS:
        source_kind = "audio"
    else:
        raise UnsupportedInputError(
            f"supported inputs are ZIP or audio with extensions: "
            f"{', '.join(sorted(SUPPORTED_AUDIO_EXTENSIONS))}"
        )

    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination_path.name}.staging-",
            dir=destination_path.parent,
        )
    )
    published = False
    try:
        flat_directory = staging / "prepared_flat"
        candidates = staging / ".candidates"
        flat_directory.mkdir(mode=0o700)
        candidates.mkdir(mode=0o700)
        state = _CollectionState([], [], [], [], {})
        with _open_regular_binary(source_path) as (source_stream, source_size):
            if source_kind == "zip":
                if source_size > active_limits.max_archive_bytes:
                    raise DatasetLimitExceededError("archive input exceeds max_archive_bytes")
                _collect_zip(
                    source_stream,
                    candidates,
                    flat_directory,
                    state,
                    active_limits,
                )
            else:
                if source_size > active_limits.max_file_uncompressed_bytes:
                    raise DatasetLimitExceededError(
                        "audio input exceeds max_file_uncompressed_bytes"
                    )
                state.source_file_entries = 1
                _collect_audio_stream(
                    source_stream,
                    source_path.name,
                    source_extension,
                    source_size,
                    candidates,
                    flat_directory,
                    state,
                    active_limits,
                )

        report = _build_quality_report(source_kind, flat_directory, state, active_limits)
        if not state.files:
            raise NoUsableAudioError(report)
        manifest = _build_manifest(state.files, active_limits)
        shutil.rmtree(candidates)
        _write_exclusive(staging / "manifest.json", manifest.to_json())
        _write_exclusive(staging / "quality_report.json", report.to_json())
        _publish_without_overwrite(staging, destination_path)
        published = True
        return DatasetIngestionResult(
            destination=destination_path,
            flat_directory=destination_path / "prepared_flat",
            manifest_path=destination_path / "manifest.json",
            quality_report_path=destination_path / "quality_report.json",
            manifest=manifest,
            quality_report=report,
        )
    except DatasetIngestionError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError) as exc:
        raise UnsafeArchiveError("archive integrity or compression validation failed") from exc
    finally:
        if not published and os.path.lexists(staging):
            shutil.rmtree(staging)


def _collect_zip(
    archive_stream: IO[bytes],
    candidates: Path,
    flat_directory: Path,
    state: _CollectionState,
    limits: IngestionLimits,
) -> None:
    with zipfile.ZipFile(archive_stream, mode="r", allowZip64=True) as archive:
        members = _validate_archive_members(archive.infolist(), limits)
        for member in members:
            if member.info.is_dir():
                continue
            state.source_file_entries += 1
            skip_reason = _skip_reason(member.normalized_path)
            if skip_reason is not None:
                state.skipped.append(SkippedSource(member.normalized_path, skip_reason))
                continue
            extension = PurePosixPath(member.normalized_path).suffix.lower()
            if extension not in SUPPORTED_AUDIO_EXTENSIONS:
                state.skipped.append(SkippedSource(member.normalized_path, "non_audio"))
                continue
            with archive.open(member.info, mode="r") as member_stream:
                _collect_audio_stream(
                    member_stream,
                    member.normalized_path,
                    extension,
                    member.info.file_size,
                    candidates,
                    flat_directory,
                    state,
                    limits,
                )


def _collect_audio_stream(
    source_stream: IO[bytes],
    source_path: str,
    extension: str,
    expected_size: int,
    candidates: Path,
    flat_directory: Path,
    state: _CollectionState,
    limits: IngestionLimits,
) -> None:
    candidate = candidates / f"candidate-{state.source_file_entries:08d}{extension}"
    size_bytes, sha256 = _copy_and_hash(
        source_stream,
        candidate,
        state,
        limits,
    )
    if size_bytes != expected_size:
        candidate.unlink(missing_ok=True)
        raise UnsafeArchiveError("streamed member size differs from ZIP metadata")
    if size_bytes == 0:
        candidate.unlink()
        state.rejected.append(RejectedSource(source_path, "empty_audio_file", None))
        return

    if extension == ".wav":
        try:
            inspection = _inspect_pcm_wav(candidate, limits)
        except InvalidWavError as exc:
            candidate.unlink()
            state.rejected.append(RejectedSource(source_path, "invalid_wav", str(exc)))
            return
    else:
        inspection = AudioInspection.decoder_pending()

    existing_path = state.digest_to_path.get(sha256)
    if existing_path is not None:
        candidate.unlink()
        state.duplicates.append(DuplicateSource(source_path, sha256, existing_path))
        return

    sequence = len(state.files) + 1
    canonical_name = f"{sequence:06d}{extension}"
    canonical_relative = f"prepared_flat/{canonical_name}"
    canonical_file = flat_directory / canonical_name
    os.replace(candidate, canonical_file)
    state.digest_to_path[sha256] = canonical_relative
    state.files.append(
        ManifestFile(
            sequence=sequence,
            canonical_path=canonical_relative,
            source_path=source_path,
            extension=extension,
            size_bytes=size_bytes,
            sha256=sha256,
            inspection=inspection,
        )
    )


def _copy_and_hash(
    source_stream: IO[bytes],
    destination: Path,
    state: _CollectionState,
    limits: IngestionLimits,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    copied = 0
    try:
        with destination.open("xb") as output:
            while True:
                chunk = source_stream.read(limits.copy_chunk_bytes)
                if not chunk:
                    break
                copied += len(chunk)
                state.actual_copied_bytes += len(chunk)
                if copied > limits.max_file_uncompressed_bytes:
                    raise DatasetLimitExceededError(
                        "streamed file exceeds max_file_uncompressed_bytes"
                    )
                if state.actual_copied_bytes > limits.max_total_uncompressed_bytes:
                    raise DatasetLimitExceededError(
                        "streamed data exceeds max_total_uncompressed_bytes"
                    )
                digest.update(chunk)
                output.write(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return copied, digest.hexdigest()


def _validate_archive_members(
    infos: list[zipfile.ZipInfo], limits: IngestionLimits
) -> list[_ArchiveMember]:
    if len(infos) > limits.max_entries:
        raise DatasetLimitExceededError("archive exceeds max_entries")
    total_uncompressed = 0
    seen_names: set[str] = set()
    members: list[_ArchiveMember] = []
    for info in infos:
        normalized = _safe_member_path(info.filename)
        duplicate_key = unicodedata.normalize("NFC", normalized).casefold()
        if duplicate_key in seen_names:
            raise UnsafeArchiveError("archive contains duplicate member paths")
        seen_names.add(duplicate_key)
        _validate_member_type(info)
        if info.flag_bits & 0x1:
            raise UnsafeArchiveError("encrypted ZIP members are not accepted")
        if info.compress_type not in SUPPORTED_ZIP_COMPRESSION:
            raise UnsafeArchiveError("ZIP member uses unsupported compression")
        if info.file_size < 0 or info.compress_size < 0:
            raise UnsafeArchiveError("ZIP member has invalid size metadata")
        if not info.is_dir():
            if info.file_size > limits.max_file_uncompressed_bytes:
                raise DatasetLimitExceededError(
                    "archive member exceeds max_file_uncompressed_bytes"
                )
            total_uncompressed += info.file_size
            if total_uncompressed > limits.max_total_uncompressed_bytes:
                raise DatasetLimitExceededError("archive exceeds max_total_uncompressed_bytes")
            ratio = _compression_ratio(info.file_size, info.compress_size)
            if ratio > limits.max_compression_ratio:
                raise DatasetLimitExceededError("archive member exceeds max_compression_ratio")
        members.append(_ArchiveMember(info=info, normalized_path=normalized))
    return sorted(
        members,
        key=lambda member: (
            unicodedata.normalize("NFC", member.normalized_path).casefold(),
            member.normalized_path,
        ),
    )


def _safe_member_path(raw_name: str) -> str:
    if not raw_name or "\x00" in raw_name:
        raise UnsafeArchiveError("ZIP member name is empty or contains NUL")
    if "\\" in raw_name:
        raise UnsafeArchiveError("ZIP member paths may not contain backslashes")
    if raw_name.startswith("/") or re.match(r"^[A-Za-z]:($|/)", raw_name):
        raise UnsafeArchiveError("ZIP member path must be relative")
    posix_path = PurePosixPath(raw_name)
    if posix_path.is_absolute() or any(part == ".." for part in posix_path.parts):
        raise UnsafeArchiveError("ZIP member path traversal is not accepted")
    parts = [
        unicodedata.normalize("NFC", part) for part in posix_path.parts if part not in {"", "."}
    ]
    if not parts:
        raise UnsafeArchiveError("ZIP member path has no usable components")
    normalized = "/".join(parts)
    if raw_name.endswith("/"):
        normalized += "/"
    return normalized


def _validate_member_type(info: zipfile.ZipInfo) -> None:
    unix_mode = info.external_attr >> 16
    file_type = stat.S_IFMT(unix_mode)
    if info.is_dir():
        if file_type not in {0, stat.S_IFDIR}:
            raise UnsafeArchiveError("ZIP directory member has a special file type")
        return
    if file_type not in {0, stat.S_IFREG}:
        raise UnsafeArchiveError("ZIP symlink and special-file members are not accepted")


def _skip_reason(member_path: str) -> SkipReason | None:
    parts = PurePosixPath(member_path).parts
    folded_parts = tuple(part.casefold() for part in parts)
    if (
        "__macosx" in folded_parts
        or ".ds_store" in folded_parts
        or any(part.startswith("._") for part in parts)
    ):
        return "macos_metadata"
    if any(part.startswith(".") for part in parts):
        return "hidden"
    return None


def _compression_ratio(uncompressed: int, compressed: int) -> float:
    if uncompressed == 0:
        return 0.0
    if compressed == 0:
        return math.inf
    return uncompressed / compressed


def _inspect_pcm_wav(path: Path, limits: IngestionLimits) -> AudioInspection:
    try:
        with wave.open(str(path), mode="rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            declared_frames = audio.getnframes()
            if audio.getcomptype() != "NONE":
                raise InvalidWavError("WAV compression is not supported; PCM is required")
            if channels <= 0 or channels > 64:
                raise InvalidWavError("WAV channel count is outside the supported range")
            if sample_width not in {1, 2, 3, 4}:
                raise InvalidWavError("WAV sample width must be 8, 16, 24, or 32-bit PCM")
            if sample_rate <= 0 or declared_frames <= 0:
                raise InvalidWavError("WAV must contain PCM frames with a positive sample rate")

            frame_width = channels * sample_width
            frames_read = 0
            sample_count = 0
            clipped_samples = 0
            silent_samples = 0
            max_absolute_sample = 0
            sum_squares = 0.0
            full_scale = 1 << (sample_width * 8 - 1)
            clipping_level = full_scale - 1
            silence_level = full_scale * 10 ** (limits.silence_threshold_dbfs / 20.0)
            loudness_unavailable_reason = _loudness_unavailable_reason(
                sample_rate,
                channels,
            )
            loudness_scanner = (
                _LoudnessBlockScanner(
                    sample_rate,
                    channels,
                    DATASET_PCM_LOUDNESS_ABSOLUTE_GATE_LUFS,
                )
                if loudness_unavailable_reason is None
                else None
            )

            while frames_read < declared_frames:
                requested = min(
                    limits.wav_analysis_chunk_frames,
                    declared_frames - frames_read,
                )
                frame_data = audio.readframes(requested)
                if not frame_data:
                    break
                if len(frame_data) % frame_width != 0:
                    raise InvalidWavError("WAV contains a truncated PCM frame")
                chunk_frame_count = len(frame_data) // frame_width
                frames_read += chunk_frame_count
                samples = iter(_pcm_samples(frame_data, sample_width))
                for _ in range(chunk_frame_count):
                    normalized_frame: list[float] = []
                    for _channel_index in range(channels):
                        try:
                            sample = next(samples)
                        except StopIteration as exc:
                            raise InvalidWavError("WAV PCM frame has missing channel data") from exc
                        absolute_sample = abs(sample)
                        sample_count += 1
                        max_absolute_sample = max(max_absolute_sample, absolute_sample)
                        if absolute_sample >= clipping_level:
                            clipped_samples += 1
                        if absolute_sample <= silence_level:
                            silent_samples += 1
                        sum_squares += float(sample * sample)
                        if loudness_scanner is not None:
                            normalized_frame.append(sample / full_scale)
                    if loudness_scanner is not None:
                        loudness_scanner.process_frame(tuple(normalized_frame))
                try:
                    next(samples)
                except StopIteration:
                    pass
                else:
                    raise InvalidWavError("WAV PCM frame has unexpected channel data")

            expected_samples = declared_frames * channels
            if frames_read != declared_frames or sample_count != expected_samples:
                raise InvalidWavError("WAV frame data is shorter than its header declares")
    except InvalidWavError:
        raise
    except (EOFError, OSError, struct.error, ValueError, wave.Error) as exc:
        raise InvalidWavError("invalid WAV container or unsupported PCM encoding") from exc

    rms = math.sqrt(sum_squares / sample_count) / full_scale
    loudness_scan = (
        _PcmLoudnessScan(
            unavailable_reason=loudness_unavailable_reason,
            block_count=0,
            gated_block_count=0,
            gated_energy_sum=0.0,
        )
        if loudness_scanner is None
        else _PcmLoudnessScan(
            unavailable_reason=None,
            block_count=loudness_scanner.block_count,
            gated_block_count=loudness_scanner.gated_block_count,
            gated_energy_sum=loudness_scanner.gated_energy_sum,
        )
    )
    return AudioInspection(
        status="validated_pcm",
        duration_seconds=_stable_float(declared_frames / sample_rate),
        sample_rate_hz=sample_rate,
        channels=channels,
        sample_width_bytes=sample_width,
        peak_ratio=_stable_float(min(max_absolute_sample / full_scale, 1.0)),
        clipping_ratio=_stable_float(clipped_samples / sample_count),
        silence_ratio=_stable_float(silent_samples / sample_count),
        rms_ratio=_stable_float(min(rms, 1.0)),
        _aggregate_evidence=_PcmAggregateEvidence(
            sample_count=sample_count,
            clipped_sample_count=clipped_samples,
            silent_sample_count=silent_samples,
            normalized_square_sum=sum_squares / float(full_scale * full_scale),
            loudness=loudness_scan,
        ),
    )


def _pcm_samples(frame_data: bytes, sample_width: int) -> Iterator[int]:
    if sample_width == 1:
        yield from (value - 128 for value in frame_data)
    elif sample_width == 2:
        yield from (sample[0] for sample in struct.iter_unpack("<h", frame_data))
    elif sample_width == 3:
        view = memoryview(frame_data)
        yield from (
            int.from_bytes(view[offset : offset + 3], byteorder="little", signed=True)
            for offset in range(0, len(view), 3)
        )
    elif sample_width == 4:
        yield from (sample[0] for sample in struct.iter_unpack("<i", frame_data))
    else:
        raise InvalidWavError("unsupported PCM sample width")


def _loudness_unavailable_reason(
    sample_rate_hz: int,
    channels: int,
) -> LoudnessUnavailableReason | None:
    # ``wave`` does not expose a WAVE_FORMAT_EXTENSIBLE speaker mask. Without
    # that mask, assigning BS.1770 surround/LFE channel weights would invent a
    # layout, so this version is deliberately limited to unambiguous mono/stereo.
    if channels not in {1, 2}:
        return "unsupported_channel_layout"
    if not (
        DATASET_PCM_LOUDNESS_MIN_SAMPLE_RATE_HZ
        <= sample_rate_hz
        <= DATASET_PCM_LOUDNESS_MAX_SAMPLE_RATE_HZ
    ):
        return "unsupported_sample_rate"
    return None


def _k_weighting_coefficients(
    sample_rate_hz: int,
) -> tuple[_BiquadCoefficients, _BiquadCoefficients]:
    if not (
        DATASET_PCM_LOUDNESS_MIN_SAMPLE_RATE_HZ
        <= sample_rate_hz
        <= DATASET_PCM_LOUDNESS_MAX_SAMPLE_RATE_HZ
    ):
        raise ValueError("sample rate is outside the versioned K-weighting range")

    shelf_k = math.tan(math.pi * _K_WEIGHTING_SHELF_FREQUENCY_HZ / sample_rate_hz)
    shelf_vh = 10.0 ** (_K_WEIGHTING_SHELF_GAIN_DB / 20.0)
    # This exponent is the De Man parameterization that reproduces the
    # normative BS.1770-4 48 kHz stage-1 coefficients.
    shelf_vb = shelf_vh**0.4996667741545416
    shelf_a0 = 1.0 + shelf_k / _K_WEIGHTING_SHELF_Q + shelf_k * shelf_k
    shelf = _BiquadCoefficients(
        b0=(shelf_vh + shelf_vb * shelf_k / _K_WEIGHTING_SHELF_Q + shelf_k * shelf_k) / shelf_a0,
        b1=2.0 * (shelf_k * shelf_k - shelf_vh) / shelf_a0,
        b2=(shelf_vh - shelf_vb * shelf_k / _K_WEIGHTING_SHELF_Q + shelf_k * shelf_k) / shelf_a0,
        a1=2.0 * (shelf_k * shelf_k - 1.0) / shelf_a0,
        a2=(1.0 - shelf_k / _K_WEIGHTING_SHELF_Q + shelf_k * shelf_k) / shelf_a0,
    )

    high_pass_k = math.tan(math.pi * _K_WEIGHTING_HIGH_PASS_FREQUENCY_HZ / sample_rate_hz)
    high_pass_a0 = 1.0 + high_pass_k / _K_WEIGHTING_HIGH_PASS_Q + high_pass_k * high_pass_k
    high_pass = _BiquadCoefficients(
        b0=1.0,
        b1=-2.0,
        b2=1.0,
        a1=2.0 * (high_pass_k * high_pass_k - 1.0) / high_pass_a0,
        a2=(1.0 - high_pass_k / _K_WEIGHTING_HIGH_PASS_Q + high_pass_k * high_pass_k)
        / high_pass_a0,
    )
    coefficients = (
        shelf.b0,
        shelf.b1,
        shelf.b2,
        shelf.a1,
        shelf.a2,
        high_pass.b0,
        high_pass.b1,
        high_pass.b2,
        high_pass.a1,
        high_pass.a2,
    )
    if not all(math.isfinite(value) for value in coefficients):
        raise ValueError("K-weighting coefficients are non-finite")
    return shelf, high_pass


def _energy_to_lufs(energy: float) -> float:
    if not math.isfinite(energy) or energy <= 0.0:
        raise ValueError("loudness energy must be positive and finite")
    loudness = -0.691 + 10.0 * math.log10(energy)
    if not math.isfinite(loudness):
        raise ValueError("computed loudness is non-finite")
    return loudness


def _scan_pcm_wav_loudness(
    path: Path,
    inspection: AudioInspection,
    gate_lufs: float,
) -> _PcmLoudnessScan:
    expected = (
        inspection.sample_rate_hz,
        inspection.channels,
        inspection.sample_width_bytes,
    )
    if inspection.status != "validated_pcm" or any(value is None for value in expected):
        raise InvalidWavError("second loudness pass requires validated PCM metadata")
    sample_rate_hz, channels, sample_width = expected
    assert sample_rate_hz is not None
    assert channels is not None
    assert sample_width is not None
    if _loudness_unavailable_reason(sample_rate_hz, channels) is not None:
        raise InvalidWavError("second loudness pass received unsupported PCM metadata")
    scanner = _LoudnessBlockScanner(sample_rate_hz, channels, gate_lufs)

    try:
        with _open_regular_binary(path) as (source, _source_size):
            with wave.open(source, mode="rb") as audio:
                actual = (audio.getframerate(), audio.getnchannels(), audio.getsampwidth())
                declared_frames = audio.getnframes()
                if audio.getcomptype() != "NONE" or actual != expected or declared_frames <= 0:
                    raise InvalidWavError("PCM metadata changed between loudness passes")
                frame_width = channels * sample_width
                frames_read = 0
                while frames_read < declared_frames:
                    requested = min(4096, declared_frames - frames_read)
                    frame_data = audio.readframes(requested)
                    if not frame_data or len(frame_data) % frame_width != 0:
                        raise InvalidWavError("WAV changed or truncated during loudness analysis")
                    chunk_frame_count = len(frame_data) // frame_width
                    frames_read += chunk_frame_count
                    samples = iter(_pcm_samples(frame_data, sample_width))
                    for _ in range(chunk_frame_count):
                        normalized_frame: list[float] = []
                        for _channel_index in range(channels):
                            try:
                                sample = next(samples)
                            except StopIteration as exc:
                                raise InvalidWavError(
                                    "WAV PCM frame has missing channel data"
                                ) from exc
                            normalized_frame.append(sample / (1 << (sample_width * 8 - 1)))
                        scanner.process_frame(tuple(normalized_frame))
                    try:
                        next(samples)
                    except StopIteration:
                        pass
                    else:
                        raise InvalidWavError("WAV PCM frame has unexpected channel data")
                if frames_read != declared_frames:
                    raise InvalidWavError("WAV frame data changed between loudness passes")
    except InvalidWavError:
        raise
    except (EOFError, OSError, struct.error, ValueError, wave.Error) as exc:
        raise InvalidWavError("second loudness pass could not validate PCM") from exc

    return _PcmLoudnessScan(
        unavailable_reason=None,
        block_count=scanner.block_count,
        gated_block_count=scanner.gated_block_count,
        gated_energy_sum=scanner.gated_energy_sum,
    )


def _build_manifest(files: list[ManifestFile], limits: IngestionLimits) -> DatasetManifest:
    content_digest = hashlib.sha256()
    content_digest.update(b"rvc-canonical-dataset-v1\x00")
    for entry in files:
        content_digest.update(entry.canonical_path.encode("ascii"))
        content_digest.update(b"\x00")
        content_digest.update(bytes.fromhex(entry.sha256))
        content_digest.update(entry.size_bytes.to_bytes(8, byteorder="big", signed=False))
    duration = sum(
        entry.inspection.duration_seconds or 0.0
        for entry in files
        if entry.inspection.status == "validated_pcm"
    )
    return DatasetManifest(
        schema_version=1,
        analysis_version="pcm-sample-v1",
        wav_silence_threshold_dbfs=limits.silence_threshold_dbfs,
        content_sha256=content_digest.hexdigest(),
        file_count=len(files),
        total_bytes=sum(entry.size_bytes for entry in files),
        validated_wav_duration_seconds=_stable_float(duration),
        files=tuple(files),
    )


def _build_quality_report(
    source_kind: SourceKind,
    flat_directory: Path,
    state: _CollectionState,
    limits: IngestionLimits,
) -> QualityReport:
    evidence = [
        entry.inspection._aggregate_evidence
        for entry in state.files
        if entry.inspection.status == "validated_pcm"
        and entry.inspection._aggregate_evidence is not None
    ]
    sample_count = sum(item.sample_count for item in evidence)
    pcm_quality = (
        PcmQualityAggregate(
            algorithm=DATASET_PCM_QUALITY_ALGORITHM,
            validated_file_count=len(evidence),
            sample_count=sample_count,
            clipping_ratio=_stable_float(
                sum(item.clipped_sample_count for item in evidence) / sample_count
            ),
            silence_ratio=_stable_float(
                sum(item.silent_sample_count for item in evidence) / sample_count
            ),
            rms_ratio=_stable_float(
                min(
                    math.sqrt(
                        math.fsum(item.normalized_square_sum for item in evidence) / sample_count
                    ),
                    1.0,
                )
            ),
            silence_threshold_dbfs=limits.silence_threshold_dbfs,
            loudness=_build_pcm_loudness(flat_directory, state.files, evidence),
        )
        if sample_count > 0
        else None
    )
    return QualityReport(
        schema_version=3,
        source_kind=source_kind,
        source_file_entries=state.source_file_entries,
        included_count=len(state.files),
        decoder_pending_count=sum(
            entry.inspection.status == "decoder_pending" for entry in state.files
        ),
        pcm_quality=pcm_quality,
        skipped=tuple(state.skipped),
        rejected=tuple(state.rejected),
        duplicates=tuple(state.duplicates),
    )


def _build_pcm_loudness(
    flat_directory: Path,
    files: list[ManifestFile],
    evidence: list[_PcmAggregateEvidence],
) -> PcmLoudnessAggregate:
    unavailable = {
        item.loudness.unavailable_reason
        for item in evidence
        if item.loudness.unavailable_reason is not None
    }
    if unavailable:
        reason: LoudnessUnavailableReason = (
            "unsupported_channel_layout"
            if "unsupported_channel_layout" in unavailable
            else "unsupported_sample_rate"
        )
        return _unavailable_pcm_loudness(reason, analyzed_file_count=0, block_count=0)

    analyzed_file_count = len(evidence)
    block_count = sum(item.loudness.block_count for item in evidence)
    if block_count == 0:
        return _unavailable_pcm_loudness(
            "insufficient_duration",
            analyzed_file_count=analyzed_file_count,
            block_count=0,
        )

    absolute_gated_count = sum(item.loudness.gated_block_count for item in evidence)
    if absolute_gated_count == 0:
        return _unavailable_pcm_loudness(
            "below_absolute_gate",
            analyzed_file_count=analyzed_file_count,
            block_count=block_count,
        )
    absolute_gated_energy = math.fsum(item.loudness.gated_energy_sum for item in evidence)
    if not math.isfinite(absolute_gated_energy) or absolute_gated_energy <= 0.0:
        raise InvalidWavError("absolute-gated dataset loudness energy is invalid")
    relative_gate_lufs = (
        _energy_to_lufs(absolute_gated_energy / absolute_gated_count)
        + DATASET_PCM_LOUDNESS_RELATIVE_GATE_LU
    )
    final_gate_lufs = max(
        DATASET_PCM_LOUDNESS_ABSOLUTE_GATE_LUFS,
        relative_gate_lufs,
    )

    second_pass: list[_PcmLoudnessScan] = []
    for entry in files:
        if entry.inspection.status != "validated_pcm":
            continue
        scan = _scan_pcm_wav_loudness(
            flat_directory / PurePosixPath(entry.canonical_path).name,
            entry.inspection,
            final_gate_lufs,
        )
        first_scan = entry.inspection._aggregate_evidence
        if first_scan is None or scan.block_count != first_scan.loudness.block_count:
            raise InvalidWavError("loudness block count changed between analysis passes")
        second_pass.append(scan)

    gated_block_count = sum(item.gated_block_count for item in second_pass)
    gated_energy_sum = math.fsum(item.gated_energy_sum for item in second_pass)
    if gated_block_count <= 0 or not math.isfinite(gated_energy_sum) or gated_energy_sum <= 0.0:
        raise InvalidWavError("relative loudness gate produced no valid blocks")
    integrated_lufs = _stable_float(_energy_to_lufs(gated_energy_sum / gated_block_count))
    if not -70.0 <= integrated_lufs <= 10.0:
        raise InvalidWavError("integrated PCM loudness is outside the supported finite range")
    return PcmLoudnessAggregate(
        algorithm=DATASET_PCM_LOUDNESS_ALGORITHM,
        scope=DATASET_PCM_LOUDNESS_SCOPE,
        block_duration_ms=DATASET_PCM_LOUDNESS_BLOCK_DURATION_MS,
        block_overlap_percent=DATASET_PCM_LOUDNESS_BLOCK_OVERLAP_PERCENT,
        absolute_gate_lufs=DATASET_PCM_LOUDNESS_ABSOLUTE_GATE_LUFS,
        relative_gate_lu=DATASET_PCM_LOUDNESS_RELATIVE_GATE_LU,
        analyzed_file_count=analyzed_file_count,
        block_count=block_count,
        gated_block_count=gated_block_count,
        integrated_lufs=integrated_lufs,
        unavailable_reason=None,
    )


def _unavailable_pcm_loudness(
    reason: LoudnessUnavailableReason,
    *,
    analyzed_file_count: int,
    block_count: int,
) -> PcmLoudnessAggregate:
    return PcmLoudnessAggregate(
        algorithm=DATASET_PCM_LOUDNESS_ALGORITHM,
        scope=DATASET_PCM_LOUDNESS_SCOPE,
        block_duration_ms=DATASET_PCM_LOUDNESS_BLOCK_DURATION_MS,
        block_overlap_percent=DATASET_PCM_LOUDNESS_BLOCK_OVERLAP_PERCENT,
        absolute_gate_lufs=DATASET_PCM_LOUDNESS_ABSOLUTE_GATE_LUFS,
        relative_gate_lu=DATASET_PCM_LOUDNESS_RELATIVE_GATE_LU,
        analyzed_file_count=analyzed_file_count,
        block_count=block_count,
        gated_block_count=0,
        integrated_lufs=None,
        unavailable_reason=reason,
    )


def _validated_job_root(job_temp_root: str | Path) -> Path:
    raw_root = Path(job_temp_root)
    if raw_root.is_symlink():
        raise UnsafePathError("job_temp_root may not be a symlink")
    try:
        root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise UnsafePathError("job_temp_root does not exist") from exc
    if not root.is_dir():
        raise UnsafePathError("job_temp_root must be a directory")
    return root


def _validated_source(source: str | Path, root: Path) -> Path:
    source_path = _lexical_path_under_root(source, root)
    _reject_symlink_components(source_path, root)
    try:
        source_stat = source_path.lstat()
    except OSError as exc:
        raise UnsafePathError("source does not exist") from exc
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
        raise UnsafePathError("source must be a regular file and not a symlink")
    return source_path


@contextmanager
def _open_regular_binary(path: Path) -> Iterator[tuple[IO[bytes], int]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise UnsafePathError("source could not be opened without following symlinks") from exc
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise UnsafePathError("opened source is not a regular file")
        with os.fdopen(descriptor, mode="rb", closefd=True) as stream:
            descriptor = -1
            yield stream, opened_stat.st_size
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validated_destination(destination: str | Path, root: Path) -> Path:
    destination_path = _lexical_path_under_root(destination, root)
    if destination_path == root:
        raise UnsafePathError("destination may not replace job_temp_root")
    _reject_symlink_components(destination_path.parent, root)
    if not destination_path.parent.is_dir():
        raise UnsafePathError("destination parent must already exist")
    if os.path.lexists(destination_path):
        raise DestinationExistsError("destination already exists")
    return destination_path


def _lexical_path_under_root(path: str | Path, root: Path) -> Path:
    raw_path = Path(path)
    if ".." in raw_path.parts:
        raise UnsafePathError("parent traversal is not accepted in local paths")
    candidate = raw_path if raw_path.is_absolute() else root / raw_path
    lexical = Path(os.path.abspath(candidate))
    try:
        lexical.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError("path must remain inside job_temp_root") from exc
    return lexical


def _reject_symlink_components(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError("path must remain inside job_temp_root") from exc
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise UnsafePathError("paths inside job_temp_root may not traverse symlinks")


def _publish_without_overwrite(staging: Path, destination: Path) -> None:
    lock_path = destination.parent / f".{destination.name}.publish.lock"
    try:
        lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise PublishConflictError("another publish is already in progress") from exc
    try:
        if os.path.lexists(destination):
            raise DestinationExistsError("destination appeared during ingestion")
        try:
            os.rename(staging, destination)
        except OSError as exc:
            if os.path.lexists(destination):
                raise DestinationExistsError("destination appeared during publish") from exc
            raise
    finally:
        try:
            os.close(lock_descriptor)
        except OSError:
            pass
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _write_exclusive(path: Path, document: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(document)


def _stable_float(value: float) -> float:
    return round(value, 12)


def _json_document(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
