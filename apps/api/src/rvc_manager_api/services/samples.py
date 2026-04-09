from __future__ import annotations

import asyncio
import math
import os
import stat
import struct
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rvc_orchestrator_contracts import (
    RVC_REVIEWED_COMMIT,
    SAMPLE_MAX_TOTAL_OUTPUT_BYTES,
    SAMPLE_MAX_TOTAL_OUTPUT_DURATION_SECONDS,
    SAMPLE_PCM_CLIPPING_THRESHOLD,
    SAMPLE_PCM_METRICS_ALGORITHM,
    SAMPLE_PCM_SILENCE_THRESHOLD,
    ArtifactType,
    InferenceF0Method,
    JobConfig,
    SampleMetricsEvidence,
    SampleMetricValues,
    SampleRead,
    SampleRegistrationRequest,
)

from ..config import Settings
from ..models import Artifact, ArtifactUploadSession, Job, JobAttempt, Sample
from ..storage import (
    ObjectNotFound,
    ObjectTooLarge,
    StorageAdapter,
    StorageError,
    storage_namespace_matches,
)
from .artifacts import (
    ArtifactSpoolError,
    ArtifactVerificationMismatch,
    canonical_object_key,
    remove_spool_file,
    verify_object_to_spool,
)
from .workers import verified_test_set_transfer

_METRIC_ABSOLUTE_TOLERANCE = 1e-6
_METRIC_RELATIVE_TOLERANCE = 1e-5


class InvalidSampleWav(ValueError):
    def __init__(self, failure_code: str) -> None:
        super().__init__("sample output is not a supported bounded PCM WAV")
        self.failure_code = failure_code


class SampleStorageUnavailable(RuntimeError):
    pass


class SampleCompletionUnavailable(RuntimeError):
    """Current canonical bytes could not be reverified for a retryable reason."""


@dataclass(frozen=True, slots=True)
class SamplePcmInspection:
    sample_rate_hz: int
    channels: int
    duration_seconds: float
    metrics: SampleMetricValues


@dataclass(frozen=True, slots=True)
class VerifiedArtifactBinding:
    artifact: Artifact
    upload: ArtifactUploadSession


def _verified_binding_from_rows(
    artifact: Artifact,
    upload: ArtifactUploadSession,
    storage: StorageAdapter,
    *,
    job_id: str,
    attempt_id: str,
    artifact_type: ArtifactType,
    sha256: str | None = None,
    lease_id: str | None = None,
    worker_id: str | None = None,
) -> VerifiedArtifactBinding | None:
    if not storage_namespace_matches(
        backend=upload.storage_backend,
        namespace_sha256=upload.storage_namespace_sha256,
        storage=storage,
    ):
        raise SampleStorageUnavailable("artifact storage namespace is unavailable")
    expected_key = canonical_object_key(
        job_id,
        attempt_id,
        artifact_type.value,
        upload.id,
    )
    try:
        expected_uri = storage.storage_uri(expected_key)
    except StorageError as exc:
        raise SampleStorageUnavailable("artifact storage is unavailable") from exc
    verification = artifact.metadata_json.get("manager_verification")
    if not isinstance(verification, dict):
        return None
    if (
        upload.artifact_id != artifact.id
        or upload.status != "completed"
        or artifact.job_id != job_id
        or artifact.attempt_id != attempt_id
        or artifact.artifact_type != artifact_type.value
        or (sha256 is not None and artifact.sha256 != sha256)
        or artifact.size_bytes <= 0
        or artifact.storage_uri != expected_uri
        or upload.job_id != job_id
        or upload.attempt_id != attempt_id
        or upload.artifact_type != artifact_type.value
        or upload.canonical_object_key != expected_key
        or upload.expected_size_bytes != artifact.size_bytes
        or upload.expected_sha256 != artifact.sha256
        or upload.content_type != artifact.mime_type
        or upload.filename != artifact.filename
        or (lease_id is not None and upload.lease_id != lease_id)
        or (worker_id is not None and upload.worker_id != worker_id)
        or verification.get("algorithm") != "sha256"
        or verification.get("bounded_stream") is not True
        or verification.get("upload_session_id") != upload.id
        or verification.get("storage_backend") != storage.backend
    ):
        return None
    return VerifiedArtifactBinding(artifact=artifact, upload=upload)


def artifact_provenance_matches(
    binding: VerifiedArtifactBinding,
    *,
    rvc_commit_hash: str,
    runtime_image_digest: str,
    runtime_asset_manifest_sha256: str,
    native_inference_manifest_sha256: str,
    native_inference_request_sha256: str,
    native_sample_role: str,
) -> bool:
    metadata = binding.artifact.metadata_json
    return bool(
        metadata.get("rvc_commit_hash") == rvc_commit_hash
        and metadata.get("runtime_image_digest") == runtime_image_digest
        and metadata.get("runtime_asset_manifest_sha256") == runtime_asset_manifest_sha256
        and metadata.get("native_inference_manifest_sha256") == native_inference_manifest_sha256
        and metadata.get("native_inference_request_sha256") == native_inference_request_sha256
        and metadata.get("native_sample_role") == native_sample_role
    )


def _accumulate_pcm(
    chunk: bytes,
    *,
    sample_width: int,
) -> tuple[int, float, float, int, int]:
    if len(chunk) % sample_width:
        raise InvalidSampleWav("truncated_wav_pcm")
    integer_scale = 1 << (sample_width * 8 - 1)
    scale = float(integer_scale)
    # Signed PCM has one more negative magnitude than positive magnitude.  V2
    # derives a quantized threshold for each rail, so unsigned 8-bit 0x00 and
    # 0xff are both counted as clipped instead of missing the positive rail.
    negative_clip_threshold = -math.ceil(integer_scale * SAMPLE_PCM_CLIPPING_THRESHOLD)
    positive_clip_threshold = math.ceil((integer_scale - 1) * SAMPLE_PCM_CLIPPING_THRESHOLD)
    if sample_width == 1:
        values = (value - 128 for value in chunk)
    elif sample_width == 2:
        values = (value[0] for value in struct.iter_unpack("<h", chunk))
    elif sample_width == 3:
        values = (
            int.from_bytes(chunk[offset : offset + 3], "little", signed=True)
            for offset in range(0, len(chunk), 3)
        )
    elif sample_width == 4:
        values = (value[0] for value in struct.iter_unpack("<i", chunk))
    else:
        raise InvalidSampleWav("wav_sample_width_not_supported")

    count = 0
    square_sum = 0.0
    peak = 0.0
    clipped = 0
    silent = 0
    for integer_value in values:
        normalized = integer_value / scale
        amplitude = abs(normalized)
        count += 1
        square_sum += normalized * normalized
        peak = max(peak, amplitude)
        clipped += (
            integer_value <= negative_clip_threshold or integer_value >= positive_clip_threshold
        )
        silent += amplitude <= SAMPLE_PCM_SILENCE_THRESHOLD
    return count, square_sum, peak, clipped, silent


def inspect_sample_pcm_wav(
    path: Path,
    settings: Settings,
    *,
    deadline_monotonic: float | None = None,
) -> SamplePcmInspection:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    source = None
    try:
        descriptor = os.open(path, flags)
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode):
            os.close(descriptor)
            descriptor = None
            raise InvalidSampleWav("wav_not_regular_file")
        source = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = None
        header = source.read(12)
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise InvalidSampleWav("wav_read_failed") from exc
    try:
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise InvalidSampleWav("invalid_wav_signature")
        if int.from_bytes(header[4:8], "little") + 8 != identity.st_size:
            raise InvalidSampleWav("wav_riff_size_mismatch")
        source.seek(0)
        with wave.open(source, mode="rb") as audio:
            if audio.getcomptype() != "NONE":
                raise InvalidSampleWav("compressed_wav_not_allowed")
            channels = audio.getnchannels()
            sample_rate = audio.getframerate()
            sample_width = audio.getsampwidth()
            frame_count = audio.getnframes()
            if not 1 <= channels <= settings.sample_max_channels:
                raise InvalidSampleWav("wav_channel_limit")
            if not (
                settings.test_set_min_sample_rate_hz
                <= sample_rate
                <= settings.test_set_max_sample_rate_hz
            ):
                raise InvalidSampleWav("wav_sample_rate_limit")
            if sample_width not in {1, 2, 3, 4}:
                raise InvalidSampleWav("wav_sample_width_not_supported")
            if frame_count <= 0:
                raise InvalidSampleWav("empty_wav")
            duration = frame_count / sample_rate
            if duration > settings.sample_max_duration_seconds:
                raise InvalidSampleWav("wav_duration_limit")

            decoded_bytes = 0
            sample_count = 0
            square_sum = 0.0
            peak = 0.0
            clipped = 0
            silent = 0
            frames_remaining = frame_count
            while frames_remaining:
                if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                    raise InvalidSampleWav("verification_timeout")
                requested = min(frames_remaining, 65_536)
                chunk = audio.readframes(requested)
                if not chunk:
                    break
                decoded_bytes += len(chunk)
                consumed_frames = len(chunk) // (channels * sample_width)
                if consumed_frames <= 0:
                    raise InvalidSampleWav("truncated_wav_pcm")
                frames_remaining -= consumed_frames
                (
                    chunk_count,
                    chunk_square_sum,
                    chunk_peak,
                    chunk_clipped,
                    chunk_silent,
                ) = _accumulate_pcm(chunk, sample_width=sample_width)
                sample_count += chunk_count
                square_sum += chunk_square_sum
                peak = max(peak, chunk_peak)
                clipped += chunk_clipped
                silent += chunk_silent
            expected_bytes = frame_count * channels * sample_width
            if (
                frames_remaining != 0
                or decoded_bytes != expected_bytes
                or sample_count != frame_count * channels
            ):
                raise InvalidSampleWav("truncated_wav_pcm")
    except InvalidSampleWav:
        raise
    except (EOFError, wave.Error, OSError) as exc:
        raise InvalidSampleWav("invalid_wav_structure") from exc
    finally:
        if source is not None:
            try:
                source.close()
            except OSError:
                pass

    return SamplePcmInspection(
        sample_rate_hz=sample_rate,
        channels=channels,
        duration_seconds=duration,
        metrics=SampleMetricValues(
            peak_amplitude=peak,
            rms=math.sqrt(square_sum / sample_count),
            clipping_ratio=clipped / sample_count,
            silence_ratio=silent / sample_count,
        ),
    )


def sample_metrics_match(
    reported: SampleMetricValues,
    computed: SampleMetricValues,
) -> bool:
    for field in ("peak_amplitude", "rms", "clipping_ratio", "silence_ratio"):
        if not math.isclose(
            getattr(reported, field),
            getattr(computed, field),
            rel_tol=_METRIC_RELATIVE_TOLERANCE,
            abs_tol=_METRIC_ABSOLUTE_TOLERANCE,
        ):
            return False
    return True


def sample_metrics_evidence(
    payload: SampleRegistrationRequest,
    inspection: SamplePcmInspection,
) -> SampleMetricsEvidence:
    return SampleMetricsEvidence(
        algorithm=SAMPLE_PCM_METRICS_ALGORITHM,
        clipping_threshold=SAMPLE_PCM_CLIPPING_THRESHOLD,
        silence_threshold=SAMPLE_PCM_SILENCE_THRESHOLD,
        worker_reported=payload.metrics,
        manager_computed=inspection.metrics,
        worker_reported_duration_seconds=payload.output_duration_seconds,
        manager_computed_sample_rate_hz=inspection.sample_rate_hz,
        manager_computed_channels=inspection.channels,
        manager_computed_duration_seconds=inspection.duration_seconds,
    )


def sample_to_read(sample: Sample) -> SampleRead:
    return SampleRead(
        id=sample.id,
        job_id=sample.job_id,
        attempt_id=sample.attempt_id,
        test_set_id=sample.test_set_id,
        test_set_item_id=sample.test_set_item_id,
        artifact_id=sample.artifact_id,
        input_sha256=sample.input_sha256,
        model_sha256=sample.model_sha256,
        index_sha256=sample.index_sha256,
        inference_f0_method=InferenceF0Method(sample.inference_f0_method),
        inference_config_sha256=sample.inference_config_sha256,
        native_inference_manifest_sha256=sample.native_inference_manifest_sha256,
        native_inference_request_sha256=sample.native_inference_request_sha256,
        output_size_bytes=sample.output_size_bytes,
        output_sha256=sample.output_sha256,
        output_sample_rate_hz=sample.output_sample_rate_hz,
        output_channels=sample.output_channels,
        output_duration_seconds=sample.output_duration_seconds,
        metrics=SampleMetricsEvidence.model_validate(sample.metrics_json),
        rvc_commit_hash=sample.rvc_commit_hash,
        runtime_image_digest=sample.runtime_image_digest,
        runtime_asset_manifest_sha256=sample.runtime_asset_manifest_sha256,
        created_at=sample.created_at,
    )


async def verified_artifact_binding(
    session: AsyncSession,
    storage: StorageAdapter,
    *,
    artifact_id: str,
    job_id: str,
    attempt_id: str,
    artifact_type: ArtifactType,
    sha256: str | None = None,
    lease_id: str | None = None,
    worker_id: str | None = None,
) -> VerifiedArtifactBinding | None:
    artifact = await session.get(Artifact, artifact_id)
    upload = await session.scalar(
        select(ArtifactUploadSession).where(
            ArtifactUploadSession.artifact_id == artifact_id,
            ArtifactUploadSession.status == "completed",
        )
    )
    if artifact is None or upload is None:
        return None
    return _verified_binding_from_rows(
        artifact,
        upload,
        storage,
        job_id=job_id,
        attempt_id=attempt_id,
        artifact_type=artifact_type,
        sha256=sha256,
        lease_id=lease_id,
        worker_id=worker_id,
    )


async def verified_artifact_by_hash(
    session: AsyncSession,
    storage: StorageAdapter,
    *,
    job_id: str,
    attempt_id: str,
    artifact_type: ArtifactType,
    sha256: str,
    lease_id: str | None = None,
    worker_id: str | None = None,
) -> VerifiedArtifactBinding | None:
    artifact_ids = list(
        (
            await session.scalars(
                select(Artifact.id).where(
                    Artifact.job_id == job_id,
                    Artifact.attempt_id == attempt_id,
                    Artifact.artifact_type == artifact_type.value,
                    Artifact.sha256 == sha256,
                )
            )
        ).all()
    )
    if len(artifact_ids) != 1:
        return None
    return await verified_artifact_binding(
        session,
        storage,
        artifact_id=artifact_ids[0],
        job_id=job_id,
        attempt_id=attempt_id,
        artifact_type=artifact_type,
        sha256=sha256,
        lease_id=lease_id,
        worker_id=worker_id,
    )


async def verify_current_artifact_bytes(
    binding: VerifiedArtifactBinding,
    storage: StorageAdapter,
    settings: Settings,
    *,
    deadline_monotonic: float,
) -> bool:
    """Re-read one canonical object and bind current bytes to its ledger hash."""

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return False
    spool_path: Path | None = None
    verified = False
    try:
        async with asyncio.timeout(remaining):
            spool_path = await verify_object_to_spool(
                storage,
                binding.upload.canonical_object_key,
                expected_size=binding.artifact.size_bytes,
                expected_sha256=binding.artifact.sha256,
                settings=settings,
            )
        verified = True
    except (ArtifactVerificationMismatch, ObjectNotFound, ObjectTooLarge):
        verified = False
    except (TimeoutError, ArtifactSpoolError, StorageError) as exc:
        raise SampleCompletionUnavailable(
            "canonical Sample verification is temporarily unavailable"
        ) from exc
    finally:
        if spool_path is not None:
            try:
                await remove_spool_file(spool_path)
            except ArtifactSpoolError as exc:
                raise SampleCompletionUnavailable(
                    "canonical Sample verification cleanup is unavailable"
                ) from exc
    return verified


def sample_matches_registration(
    sample: Sample,
    payload: SampleRegistrationRequest,
    evidence: SampleMetricsEvidence,
) -> bool:
    try:
        stored_evidence = SampleMetricsEvidence.model_validate(sample.metrics_json)
    except ValueError:
        return False
    return bool(
        sample.attempt_id == payload.attempt_id
        and sample.test_set_id == payload.test_set_id
        and sample.test_set_item_id == payload.test_set_item_id
        and sample.artifact_id == payload.artifact_id
        and sample.input_sha256 == payload.input_sha256
        and sample.model_sha256 == payload.model_sha256
        and sample.index_sha256 == payload.index_sha256
        and sample.inference_f0_method == payload.inference_f0_method.value
        and sample.inference_config_sha256 == payload.inference_config_sha256
        and sample.native_inference_manifest_sha256 == payload.native_inference_manifest_sha256
        and sample.native_inference_request_sha256 == payload.native_inference_request_sha256
        and sample.output_size_bytes == payload.output_size_bytes
        and sample.output_sha256 == payload.output_sha256
        and sample.output_sample_rate_hz == payload.output_sample_rate_hz
        and sample.output_channels == payload.output_channels
        and math.isclose(
            sample.output_duration_seconds,
            evidence.manager_computed_duration_seconds,
            rel_tol=0,
            abs_tol=max(1 / evidence.manager_computed_sample_rate_hz, 1e-6),
        )
        and sample.rvc_commit_hash == payload.rvc_commit_hash
        and sample.runtime_image_digest == payload.runtime_image_digest
        and sample.runtime_asset_manifest_sha256 == payload.runtime_asset_manifest_sha256
        and stored_evidence == evidence
    )


async def sample_completion_ready(
    session: AsyncSession,
    job: Job,
    settings: Settings,
    storage: StorageAdapter,
    *,
    lease_id: str,
    worker_id: str,
) -> bool:
    if job.current_attempt_id is None:
        return False
    config = JobConfig.model_validate(job.config_json)
    if not config.auto_inference_samples.enabled:
        return True
    attempt = await session.get(JobAttempt, job.current_attempt_id)
    if (
        attempt is None
        or attempt.job_id != job.id
        or attempt.worker_id != worker_id
        or attempt.runtime_image_digest is None
        or attempt.runtime_asset_manifest_sha256 is None
        or (
            attempt.runtime_image_digest,
            attempt.runtime_asset_manifest_sha256,
        )
        not in settings.approved_sample_runtime_bundles
    ):
        return False
    transfer = await verified_test_set_transfer(
        session,
        job,
        config,
        storage=storage,
        settings=settings,
    )
    if transfer is None:
        return False
    samples = list(
        (
            await session.scalars(
                select(Sample).where(
                    Sample.job_id == job.id,
                    Sample.attempt_id == job.current_attempt_id,
                )
            )
        ).all()
    )
    if len(samples) != len(transfer.items):
        return False
    if (
        sum(sample.output_size_bytes for sample in samples) > SAMPLE_MAX_TOTAL_OUTPUT_BYTES
        or math.fsum(sample.output_duration_seconds for sample in samples)
        > SAMPLE_MAX_TOTAL_OUTPUT_DURATION_SECONDS
    ):
        return False
    samples_by_item = {sample.test_set_item_id: sample for sample in samples}
    if len(samples_by_item) != len(samples):
        return False
    expected_rate = transfer.inference_config.resample_sr or (
        40_000 if config.model.sample_rate.value == "40k" else 48_000
    )
    expected_commit = config.rvc_backend.rvc_commit_hash or RVC_REVIEWED_COMMIT
    if expected_commit != RVC_REVIEWED_COMMIT:
        return False
    artifact_rows = list(
        (
            await session.execute(
                select(Artifact, ArtifactUploadSession)
                .join(
                    ArtifactUploadSession,
                    ArtifactUploadSession.artifact_id == Artifact.id,
                )
                .where(
                    Artifact.job_id == job.id,
                    Artifact.attempt_id == job.current_attempt_id,
                    Artifact.artifact_type.in_(
                        (
                            ArtifactType.SAMPLE.value,
                            ArtifactType.FINAL_SMALL_MODEL.value,
                            ArtifactType.FINAL_INDEX.value,
                        )
                    ),
                    ArtifactUploadSession.status == "completed",
                )
            )
        ).all()
    )
    rows_by_id: dict[str, list[tuple[Artifact, ArtifactUploadSession]]] = {}
    rows_by_type_hash: dict[tuple[str, str], list[tuple[Artifact, ArtifactUploadSession]]] = {}
    for artifact, upload in artifact_rows:
        rows_by_id.setdefault(artifact.id, []).append((artifact, upload))
        rows_by_type_hash.setdefault((artifact.artifact_type, artifact.sha256), []).append(
            (artifact, upload)
        )

    def selected_binding(
        rows: list[tuple[Artifact, ArtifactUploadSession]],
        *,
        artifact_type: ArtifactType,
        sha256: str,
    ) -> VerifiedArtifactBinding | None:
        if len(rows) != 1:
            return None
        artifact, upload = rows[0]
        return _verified_binding_from_rows(
            artifact,
            upload,
            storage,
            job_id=job.id,
            attempt_id=job.current_attempt_id or "",
            artifact_type=artifact_type,
            sha256=sha256,
            lease_id=lease_id,
            worker_id=worker_id,
        )

    provenance: set[tuple[str, str, str, str, str | None, str, str]] = set()
    bindings_to_verify: dict[str, VerifiedArtifactBinding] = {}
    for descriptor in transfer.items:
        sample = samples_by_item.get(descriptor.test_set_item_id)
        if sample is None:
            return False
        try:
            evidence = SampleMetricsEvidence.model_validate(sample.metrics_json)
        except ValueError:
            return False
        if (
            sample.test_set_id != transfer.test_set_id
            or sample.input_sha256 != descriptor.sha256
            or sample.inference_f0_method != transfer.inference_config.inference_f0_method.value
            or sample.inference_config_sha256 != transfer.inference_config_sha256
            or sample.output_sample_rate_hz != expected_rate
            or sample.output_sample_rate_hz != evidence.manager_computed_sample_rate_hz
            or sample.output_channels != evidence.manager_computed_channels
            or not math.isclose(
                sample.output_duration_seconds,
                evidence.manager_computed_duration_seconds,
                rel_tol=0,
                abs_tol=max(1 / sample.output_sample_rate_hz, 1e-6),
            )
            or sample.rvc_commit_hash != expected_commit
        ):
            return False
        try:
            output_binding = selected_binding(
                rows_by_id.get(sample.artifact_id, []),
                artifact_type=ArtifactType.SAMPLE,
                sha256=sample.output_sha256,
            )
            model_binding = selected_binding(
                rows_by_type_hash.get(
                    (ArtifactType.FINAL_SMALL_MODEL.value, sample.model_sha256), []
                ),
                artifact_type=ArtifactType.FINAL_SMALL_MODEL,
                sha256=sample.model_sha256,
            )
            if transfer.inference_config.index_rate > 0:
                index_binding = (
                    selected_binding(
                        rows_by_type_hash.get(
                            (ArtifactType.FINAL_INDEX.value, sample.index_sha256 or ""),
                            [],
                        ),
                        artifact_type=ArtifactType.FINAL_INDEX,
                        sha256=sample.index_sha256 or "",
                    )
                    if sample.index_sha256 is not None
                    else None
                )
            else:
                index_binding = None
        except SampleStorageUnavailable as exc:
            raise SampleCompletionUnavailable(
                "Sample storage namespace is temporarily unavailable"
            ) from exc
        if (
            output_binding is None
            or model_binding is None
            or not artifact_provenance_matches(
                model_binding,
                rvc_commit_hash=sample.rvc_commit_hash,
                runtime_image_digest=sample.runtime_image_digest,
                runtime_asset_manifest_sha256=sample.runtime_asset_manifest_sha256,
                native_inference_manifest_sha256=sample.native_inference_manifest_sha256,
                native_inference_request_sha256=sample.native_inference_request_sha256,
                native_sample_role="sample_model",
            )
            or not artifact_provenance_matches(
                output_binding,
                rvc_commit_hash=sample.rvc_commit_hash,
                runtime_image_digest=sample.runtime_image_digest,
                runtime_asset_manifest_sha256=sample.runtime_asset_manifest_sha256,
                native_inference_manifest_sha256=sample.native_inference_manifest_sha256,
                native_inference_request_sha256=sample.native_inference_request_sha256,
                native_sample_role="sample_output",
            )
            or output_binding.artifact.mime_type != "audio/wav"
            or output_binding.artifact.size_bytes != sample.output_size_bytes
            or sample.output_channels < 1
            or not math.isfinite(sample.output_duration_seconds)
            or sample.output_duration_seconds <= 0
            or (transfer.inference_config.index_rate > 0 and index_binding is None)
            or (transfer.inference_config.index_rate == 0 and sample.index_sha256 is not None)
        ):
            return False
        if index_binding is not None and not artifact_provenance_matches(
            index_binding,
            rvc_commit_hash=sample.rvc_commit_hash,
            runtime_image_digest=sample.runtime_image_digest,
            runtime_asset_manifest_sha256=sample.runtime_asset_manifest_sha256,
            native_inference_manifest_sha256=sample.native_inference_manifest_sha256,
            native_inference_request_sha256=sample.native_inference_request_sha256,
            native_sample_role="sample_index",
        ):
            return False
        bindings_to_verify[output_binding.artifact.id] = output_binding
        bindings_to_verify[model_binding.artifact.id] = model_binding
        if index_binding is not None:
            bindings_to_verify[index_binding.artifact.id] = index_binding
        provenance.add(
            (
                sample.model_sha256,
                sample.runtime_image_digest,
                sample.runtime_asset_manifest_sha256,
                sample.rvc_commit_hash,
                sample.index_sha256,
                sample.native_inference_manifest_sha256,
                sample.native_inference_request_sha256,
            )
        )
    if len(provenance) != 1:
        return False
    only_provenance = next(iter(provenance))
    if (
        only_provenance[1] != attempt.runtime_image_digest
        or only_provenance[2] != attempt.runtime_asset_manifest_sha256
    ):
        return False
    verification_deadline = time.monotonic() + settings.sample_verification_timeout_seconds
    for binding in bindings_to_verify.values():
        if not await verify_current_artifact_bytes(
            binding,
            storage,
            settings,
            deadline_monotonic=verification_deadline,
        ):
            return False
    return True
