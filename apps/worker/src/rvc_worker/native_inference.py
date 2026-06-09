"""Pinned fixed-TestSet inference stages for the reviewed native RVC runtime.

The Worker factory constructs this dependency only after a strict, release-owned
``runtime-activation.json`` projection proves the surrounding runtime was fully
qualified. Missing, disabled, or invalid activation remains fail-closed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import stat
import sys
import wave
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from rvc_orchestrator_contracts import (
    SAMPLE_MAX_TOTAL_OUTPUT_BYTES,
    SAMPLE_MAX_TOTAL_OUTPUT_DURATION_SECONDS,
    JobStatus,
)

from .native_runner import (
    NativeProcessRunner,
    NativeRvcRunnerError,
    NativeSampleInferenceBinding,
)
from .process import ProcessSpec, SafeSubprocessRunner
from .runner import RvcRunContext, StageResult
from .rvc_commands import RVC_REVIEWED_COMMIT
from .workspace import WorkspaceError, ensure_within


class NativeSampleInferenceError(NativeRvcRunnerError):
    """Fixed-TestSet inference could not preserve its immutable boundary."""


@dataclass(frozen=True, slots=True)
class NativeSampleInferenceLimits:
    inference_timeout_seconds: float = 3_600.0
    max_model_bytes: int = 2 * 1024**3
    max_index_bytes: int = 16 * 1024**3
    max_output_bytes: int = 256 * 1024**2
    max_output_duration_seconds: float = 600.0
    max_total_output_bytes: int = SAMPLE_MAX_TOTAL_OUTPUT_BYTES
    max_total_output_duration_seconds: float = SAMPLE_MAX_TOTAL_OUTPUT_DURATION_SECONDS
    max_manifest_bytes: int = 16 * 1024**2
    max_result_bytes: int = 16 * 1024**2
    max_crepe_asset_bytes: int = 1024**3
    max_items: int = 128

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.inference_timeout_seconds)
            or not 0 < self.inference_timeout_seconds <= 7 * 24 * 3_600
            or not 1 <= self.max_model_bytes <= 16 * 1024**3
            or not 1 <= self.max_index_bytes <= 100 * 1024**3
            or not 44 <= self.max_output_bytes <= 256 * 1024**2
            or not math.isfinite(self.max_output_duration_seconds)
            or not 0 < self.max_output_duration_seconds <= 600
            or not 44 <= self.max_total_output_bytes <= SAMPLE_MAX_TOTAL_OUTPUT_BYTES
            or not math.isfinite(self.max_total_output_duration_seconds)
            or not (
                0
                < self.max_total_output_duration_seconds
                <= SAMPLE_MAX_TOTAL_OUTPUT_DURATION_SECONDS
            )
            or not 1 <= self.max_manifest_bytes <= 64 * 1024**2
            or not 1 <= self.max_result_bytes <= 64 * 1024**2
            or isinstance(self.max_crepe_asset_bytes, bool)
            or not 1 <= self.max_crepe_asset_bytes <= 8 * 1024**3
            or not 1 <= self.max_items <= 128
        ):
            raise ValueError("invalid native sample inference limits")


@dataclass(frozen=True, slots=True)
class _FileEvidence:
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _PcmEvidence:
    size_bytes: int
    sha256: str
    sample_rate_hz: int
    channels: int
    sample_width_bytes: int
    frame_count: int
    duration_seconds: float
    metrics: Mapping[str, float] | None = None


@dataclass(frozen=True, slots=True)
class NativeInferencePublishedFile:
    """A fixed workspace file whose bytes were re-opened and re-hashed."""

    path: Path
    workspace_relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class NativeInferencePublishedSample:
    """Publisher-safe evidence for one deterministic TestSet output."""

    test_set_item_id: str
    item_key: str
    sort_order: int
    input_sha256: str
    output: NativeInferencePublishedFile
    output_sample_rate_hz: int
    output_channels: int
    output_sample_width_bytes: int
    output_frame_count: int
    output_duration_seconds: float
    metrics: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class NativeCrepeModelEvidence:
    """Manifest-bound offline CREPE model evidence, or absent for other F0 methods."""

    asset_size_bytes: int
    asset_sha256: str
    weights_only: bool
    model_capacity: str


@dataclass(frozen=True, slots=True)
class NativeInferencePublication:
    """Strictly loaded manifest projection for artifact/sample publication."""

    manifest: NativeInferencePublishedFile
    job_id: str
    attempt_id: str
    test_set_id: str
    family_id: str
    revision: int
    test_set_manifest_sha256: str
    sample_plan_sha256: str
    inference_config_sha256: str
    inference_request_sha256: str
    inference_f0_method: str
    metrics_algorithm: str
    model: NativeInferencePublishedFile
    index: NativeInferencePublishedFile | None
    rvc_commit_hash: str
    runtime_image_digest: str
    runtime_asset_manifest_sha256: str
    crepe_model: NativeCrepeModelEvidence | None
    samples: tuple[NativeInferencePublishedSample, ...]


_SHA256 = frozenset("0123456789abcdef")
_SAMPLE_MANIFEST_NAME = "inference_manifest.json"
_EVALUATION_NAME = "sample_evaluation.json"
_REQUEST_NAME = "native-sample-inference-request.json"
_RESULT_NAME = "native-sample-inference-result.json"
_PCM_METRICS_ALGORITHM = "pcm-normalized-v2"
_CLIPPING_THRESHOLD = 0.999
_SILENCE_THRESHOLD = 0.0001
_CREPE_MODEL_RELATIVE_PATH = "runtime/crepe/full.pth"
_CREPE_MODEL_CAPACITY = "full"
_PROJECTION_BASE_DIRECTORIES = (
    "infer",
    "configs",
    "assets/pretrained",
    "assets/pretrained_v2",
    "assets/hubert",
    "assets/rmvpe",
    "logs/mute",
)


class NativeFixedTestSetInferenceDependency:
    """Run only GENERATING_SAMPLES/EVALUATING against one bound native runtime."""

    def __init__(
        self,
        *,
        runtime_image_digest: str,
        expected_asset_manifest_sha256: str | None = None,
        process_runner: NativeProcessRunner | None = None,
        limits: NativeSampleInferenceLimits | None = None,
    ) -> None:
        if re.fullmatch(r"sha256:[0-9a-f]{64}", runtime_image_digest) is None:
            raise NativeSampleInferenceError(
                "sample inference requires an immutable runtime image digest"
            )
        if expected_asset_manifest_sha256 is not None and not _is_sha256(
            expected_asset_manifest_sha256
        ):
            raise NativeSampleInferenceError(
                "sample inference requires an immutable asset manifest digest"
            )
        self.runtime_image_digest = runtime_image_digest
        self.expected_asset_manifest_sha256 = expected_asset_manifest_sha256
        self.process_runner = process_runner or SafeSubprocessRunner()
        self.limits = limits or NativeSampleInferenceLimits()
        self._binding: NativeSampleInferenceBinding | None = None

    def bind_native_runtime(self, binding: NativeSampleInferenceBinding) -> None:
        if (
            binding.rvc_commit_hash != RVC_REVIEWED_COMMIT
            or not _is_sha256(binding.asset_manifest_sha256)
            or not _is_sha256(binding.projection_manifest_sha256)
            or not Path(binding.python_executable).is_absolute()
            or "\x00" in binding.python_executable
            or binding.device
            not in {
                "cpu",
                "mps",
                *(f"cuda:{index}" for index in range(256)),
                "cuda",
            }
            or not isinstance(binding.use_half, bool)
        ):
            raise NativeSampleInferenceError("sample inference runtime binding is not reviewed")
        if (
            self.expected_asset_manifest_sha256 is not None
            and binding.asset_manifest_sha256 != self.expected_asset_manifest_sha256
        ):
            raise NativeSampleInferenceError(
                "sample inference asset manifest does not match release activation"
            )
        if self._binding is not None and self._binding != binding:
            raise NativeSampleInferenceError(
                "sample inference dependency cannot be rebound to another runtime"
            )
        self._binding = binding

    async def run_stage(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        if cancellation.is_set():
            raise asyncio.CancelledError
        if self._binding is None:
            raise NativeSampleInferenceError(
                "sample inference dependency has not been bound to a native runtime"
            )
        if stage is JobStatus.GENERATING_SAMPLES:
            return await self._generate(context, cancellation)
        if stage is JobStatus.EVALUATING:
            return await asyncio.to_thread(self._evaluate, context, cancellation)
        raise NativeSampleInferenceError(
            "sample inference dependency received an unsupported stage"
        )

    def load_publication(self, context: RvcRunContext) -> NativeInferencePublication:
        """Return re-hashed typed evidence for a downstream publisher."""

        if self._binding is None:
            raise NativeSampleInferenceError(
                "sample inference dependency has not been bound to a native runtime"
            )
        return load_native_inference_publication(
            context,
            self._binding,
            expected_runtime_image_digest=self.runtime_image_digest,
            limits=self.limits,
        )

    async def _generate(
        self,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        transfer = _require_sample_claim(context)
        binding = self._binding
        assert binding is not None
        if len(transfer.items) > self.limits.max_items:
            raise NativeSampleInferenceError("TestSet exceeds the inference item limit")
        sample_root = context.workspace.outputs / "samples"
        manifest_path = sample_root / _SAMPLE_MANIFEST_NAME
        _require_safe_parent(context.workspace.outputs, context.workspace.root)
        sample_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _require_safe_directory(sample_root, context.workspace.root)

        if _path_exists_or_symlink(manifest_path):
            manifest = _load_json_file(manifest_path, self.limits.max_manifest_bytes)
            created = _validate_published_manifest(
                context,
                manifest,
                binding,
                self.limits,
                expected_runtime_image_digest=self.runtime_image_digest,
            )
            return StageResult(
                (*created, manifest_path),
                {
                    "replayed": True,
                    "sample_count": len(created),
                    "inference_manifest_sha256": _snapshot_file(
                        manifest_path, maximum=self.limits.max_manifest_bytes
                    ).sha256,
                },
            )

        _validate_transfer_marker(context)
        input_records = _validate_test_set_inputs(context)
        model_path = context.workspace.outputs / "model" / "final_small_model.pth"
        _require_safe_directory(model_path.parent, context.workspace.root)
        model = _snapshot_file(model_path, maximum=self.limits.max_model_bytes)
        inference = transfer.inference_config
        index_path: Path | None = None
        index: _FileEvidence | None = None
        if inference.index_rate > 0:
            index_path = context.workspace.outputs / "index" / "final.index"
            _require_safe_directory(index_path.parent, context.workspace.root)
            index = _snapshot_file(index_path, maximum=self.limits.max_index_bytes)

        projection_marker = context.rvc_root / ".orchestrator-projection.json"
        _require_safe_directory(context.rvc_root, context.workspace.root)
        projection_instance = _snapshot_file(
            projection_marker, maximum=self.limits.max_manifest_bytes
        )
        crepe_model = (
            _snapshot_crepe_projection(
                context,
                projection_instance,
                maximum=self.limits.max_crepe_asset_bytes,
                marker_maximum=self.limits.max_manifest_bytes,
            )
            if inference.inference_f0_method.value == "crepe"
            else None
        )
        request_path = context.workspace.work / _REQUEST_NAME
        result_path = context.workspace.work / _RESULT_NAME
        expected_rate = (
            inference.resample_sr
            if inference.resample_sr != 0
            else (40_000 if context.claim.config.model.sample_rate.value == "40k" else 48_000)
        )
        request = {
            "schema_version": 1,
            "rvc_commit_hash": binding.rvc_commit_hash,
            "workspace_root": str(context.workspace.root),
            "projection_root": str(context.rvc_root),
            "projection_marker_sha256": projection_instance.sha256,
            "crepe_model": _crepe_evidence_document(crepe_model),
            "model": {
                "path": str(model_path),
                "size_bytes": model.size_bytes,
                "sha256": model.sha256,
                "version": context.claim.config.model.version.value,
                "sample_rate": context.claim.config.model.sample_rate.value,
                "sample_rate_hz": expected_rate
                if inference.resample_sr == 0
                else (40_000 if context.claim.config.model.sample_rate.value == "40k" else 48_000),
                "use_f0": context.claim.config.model.use_f0,
                "speaker_id": context.claim.config.model.speaker_id,
            },
            "index": (
                {
                    "path": str(index_path),
                    "size_bytes": index.size_bytes,
                    "sha256": index.sha256,
                    "dimension": (256 if context.claim.config.model.version.value == "v1" else 768),
                }
                if index_path is not None and index is not None
                else None
            ),
            "inference": inference.model_dump(mode="json"),
            "expected_output_sample_rate_hz": expected_rate,
            "device": binding.device,
            "use_half": binding.use_half,
            "runtime_image_digest": self.runtime_image_digest,
            "metrics_algorithm": _PCM_METRICS_ALGORITHM,
            "limits": {
                "max_output_bytes": self.limits.max_output_bytes,
                "max_output_duration_seconds": self.limits.max_output_duration_seconds,
                "max_total_output_bytes": self.limits.max_total_output_bytes,
                "max_total_output_duration_seconds": (
                    self.limits.max_total_output_duration_seconds
                ),
                "max_result_bytes": self.limits.max_result_bytes,
            },
            "items": [
                {
                    "test_set_item_id": item.test_set_item_id,
                    "input_path": str(context.workspace.inputs / "test_set" / item.filename),
                    "output_path": str(sample_root / f"{item.test_set_item_id}.wav"),
                    "size_bytes": evidence.size_bytes,
                    "sha256": evidence.sha256,
                    "sample_rate_hz": item.sample_rate_hz,
                    "channels": item.channels,
                    "duration_seconds": item.duration_seconds,
                }
                for item, evidence in zip(transfer.items, input_records, strict=True)
            ],
        }
        request_sha256 = _canonical_json_sha256(request)
        _atomic_write_json(request_path, request, context.workspace.root)
        if _path_exists_or_symlink(result_path):
            _remove_regular_file(result_path, context.workspace.root)

        command = build_native_sample_inference_command(
            binding.python_executable,
            request_path,
            result_path,
            request_sha256,
        )
        inference_environment = _inference_environment(context)
        if (
            crepe_model is not None
            and _snapshot_crepe_projection(
                context,
                projection_instance,
                maximum=self.limits.max_crepe_asset_bytes,
                marker_maximum=self.limits.max_manifest_bytes,
            )
            != crepe_model
        ):
            raise NativeSampleInferenceError("CREPE projection changed before inference")
        result = await self.process_runner.run(
            ProcessSpec(
                argv=command,
                cwd=context.rvc_root,
                workspace_root=context.workspace.root,
                stdout_path=context.workspace.logs / "native-sample-inference.stdout.log",
                stderr_path=context.workspace.logs / "native-sample-inference.stderr.log",
                env=inference_environment,
                timeout_seconds=self.limits.inference_timeout_seconds,
            ),
            cancellation,
        )
        if cancellation.is_set():
            raise asyncio.CancelledError
        _require_safe_directory(sample_root, context.workspace.root)
        _require_safe_directory(model_path.parent, context.workspace.root)
        _require_safe_directory(context.rvc_root, context.workspace.root)
        _require_safe_directory(result_path.parent, context.workspace.root)
        if index_path is not None:
            _require_safe_directory(index_path.parent, context.workspace.root)
        if crepe_model is not None:
            _require_snapshot(
                context.rvc_root.joinpath(*_CREPE_MODEL_RELATIVE_PATH.split("/")),
                _file_evidence_from_crepe(crepe_model),
                maximum=self.limits.max_crepe_asset_bytes,
                required_mode=0o444,
            )
        driver_result = _load_json_file(result_path, self.limits.max_result_bytes)
        output_records = _validate_driver_result(
            context,
            driver_result,
            model,
            index,
            expected_rate,
            self.limits,
            binding,
            expected_runtime_image_digest=self.runtime_image_digest,
            expected_request_sha256=request_sha256,
            expected_crepe_model=crepe_model,
        )
        _require_snapshot(model_path, model, maximum=self.limits.max_model_bytes)
        if index_path is not None and index is not None:
            _require_snapshot(index_path, index, maximum=self.limits.max_index_bytes)

        manifest = _build_inference_manifest(
            context,
            binding,
            projection_instance,
            model,
            index,
            input_records,
            output_records,
            driver_result,
        )
        _atomic_write_json(manifest_path, manifest, context.workspace.root)
        published = _validate_published_manifest(
            context,
            manifest,
            binding,
            self.limits,
            expected_runtime_image_digest=self.runtime_image_digest,
        )
        return StageResult(
            (*published, manifest_path),
            {
                "replayed": False,
                "sample_count": len(published),
                "inference_manifest_sha256": _snapshot_file(
                    manifest_path, maximum=self.limits.max_manifest_bytes
                ).sha256,
                "subprocess_logs": {
                    "stdout": str(result.stdout_path.relative_to(context.workspace.root)),
                    "stderr": str(result.stderr_path.relative_to(context.workspace.root)),
                },
            },
        )

    def _evaluate(
        self,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        if cancellation.is_set():
            raise asyncio.CancelledError
        assert self._binding is not None
        manifest_path = context.workspace.outputs / "samples" / _SAMPLE_MANIFEST_NAME
        manifest = _load_json_file(manifest_path, self.limits.max_manifest_bytes)
        samples = _validate_published_manifest(
            context,
            manifest,
            self._binding,
            self.limits,
            expected_runtime_image_digest=self.runtime_image_digest,
        )
        raw_items = _require_list(manifest.get("items"), "sample manifest items")
        metrics = [
            _require_mapping(item, "sample manifest item").get("metrics") for item in raw_items
        ]
        normalized_metrics = [
            _validate_metrics(_require_mapping(value, "sample metrics")) for value in metrics
        ]
        count = len(normalized_metrics)
        summary = {
            key: {
                "minimum": min(item[key] for item in normalized_metrics),
                "maximum": max(item[key] for item in normalized_metrics),
                "mean": math.fsum(item[key] for item in normalized_metrics) / count,
            }
            for key in ("peak_amplitude", "rms", "clipping_ratio", "silence_ratio")
        }
        report_path = context.workspace.outputs / "metrics" / _EVALUATION_NAME
        report = {
            "schema_version": 1,
            "sample_evaluation_performed": True,
            "job_id": context.claim.job_id,
            "attempt_id": context.claim.attempt_id,
            "sample_count": count,
            "inference_manifest_sha256": _snapshot_file(
                manifest_path, maximum=self.limits.max_manifest_bytes
            ).sha256,
            "metrics_algorithm": _PCM_METRICS_ALGORITHM,
            "metrics_summary": summary,
        }
        _atomic_write_json(report_path, report, context.workspace.root)
        if cancellation.is_set():
            _remove_regular_file(report_path, context.workspace.root)
            raise asyncio.CancelledError
        return StageResult(
            (*samples, manifest_path, report_path),
            {"sample_count": count, "metrics_summary": summary},
        )


def build_native_sample_inference_command(
    python_executable: str,
    request_path: Path,
    result_path: Path,
    request_sha256: str,
) -> tuple[str, ...]:
    """Build the only reviewed entry point; upstream WebUI CLIs are forbidden."""

    if (
        not python_executable
        or "\x00" in python_executable
        or not Path(python_executable).is_absolute()
        or any("\x00" in str(path) for path in (request_path, result_path))
        or not request_path.is_absolute()
        or not result_path.is_absolute()
        or not _is_sha256(request_sha256)
    ):
        raise NativeSampleInferenceError("native sample inference command is invalid")
    return (
        python_executable,
        "-m",
        "rvc_worker.native_inference_driver",
        "--request",
        str(request_path),
        "--result",
        str(result_path),
        "--request-sha256",
        request_sha256,
    )


def load_native_inference_publication(
    context: RvcRunContext,
    binding: NativeSampleInferenceBinding,
    *,
    expected_runtime_image_digest: str,
    limits: NativeSampleInferenceLimits | None = None,
) -> NativeInferencePublication:
    """Load only the fixed manifest path and independently re-hash every file.

    Manifest path strings are validated as evidence but are never used to resolve
    files. All paths below are re-derived from the attempt workspace and immutable
    TestSet IDs.
    """

    effective_limits = limits or NativeSampleInferenceLimits()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", expected_runtime_image_digest) is None:
        raise NativeSampleInferenceError("expected runtime image digest is invalid")
    manifest_path = context.workspace.outputs / "samples" / _SAMPLE_MANIFEST_NAME
    document = _load_json_file(manifest_path, effective_limits.max_manifest_bytes)
    _validate_published_manifest(
        context,
        document,
        binding,
        effective_limits,
        expected_runtime_image_digest=expected_runtime_image_digest,
    )
    runtime = _require_mapping(document["runtime_evidence"], "runtime evidence")
    if runtime.get("runtime_image_digest") != expected_runtime_image_digest:
        raise NativeSampleInferenceError("sample runtime image digest does not match")
    crepe_model = _validate_runtime_evidence(runtime)
    manifest_evidence = _snapshot_file(manifest_path, maximum=effective_limits.max_manifest_bytes)
    model_path = context.workspace.outputs / "model" / "final_small_model.pth"
    model_evidence = _snapshot_file(model_path, maximum=effective_limits.max_model_bytes)
    transfer = _require_sample_claim(context)
    index_file: NativeInferencePublishedFile | None = None
    if transfer.inference_config.index_rate > 0:
        index_path = context.workspace.outputs / "index" / "final.index"
        index_evidence = _snapshot_file(index_path, maximum=effective_limits.max_index_bytes)
        index_file = NativeInferencePublishedFile(
            path=index_path,
            workspace_relative_path="outputs/index/final.index",
            size_bytes=index_evidence.size_bytes,
            sha256=index_evidence.sha256,
        )
    raw_items = _require_list(document["items"], "manifest items")
    samples: list[NativeInferencePublishedSample] = []
    for transfer_item, raw_item in zip(transfer.items, raw_items, strict=True):
        item = _require_mapping(raw_item, "manifest item")
        output_path = (
            context.workspace.outputs / "samples" / f"{transfer_item.test_set_item_id}.wav"
        )
        output = _inspect_pcm_wave(
            output_path,
            maximum=effective_limits.max_output_bytes,
            include_metrics=True,
        )
        metrics = _validate_metrics(_require_mapping(item["metrics"], "sample metrics"))
        samples.append(
            NativeInferencePublishedSample(
                test_set_item_id=transfer_item.test_set_item_id,
                item_key=transfer_item.item_key,
                sort_order=transfer_item.sort_order,
                input_sha256=transfer_item.sha256,
                output=NativeInferencePublishedFile(
                    path=output_path,
                    workspace_relative_path=(
                        f"outputs/samples/{transfer_item.test_set_item_id}.wav"
                    ),
                    size_bytes=output.size_bytes,
                    sha256=output.sha256,
                ),
                output_sample_rate_hz=output.sample_rate_hz,
                output_channels=output.channels,
                output_sample_width_bytes=output.sample_width_bytes,
                output_frame_count=output.frame_count,
                output_duration_seconds=output.duration_seconds,
                metrics=MappingProxyType(metrics),
            )
        )
    return NativeInferencePublication(
        manifest=NativeInferencePublishedFile(
            path=manifest_path,
            workspace_relative_path="outputs/samples/inference_manifest.json",
            size_bytes=manifest_evidence.size_bytes,
            sha256=manifest_evidence.sha256,
        ),
        job_id=context.claim.job_id,
        attempt_id=context.claim.attempt_id,
        test_set_id=transfer.test_set_id,
        family_id=transfer.family_id,
        revision=transfer.revision,
        test_set_manifest_sha256=transfer.manifest_sha256,
        sample_plan_sha256=transfer.sample_plan_sha256,
        inference_config_sha256=transfer.inference_config_sha256,
        inference_request_sha256=str(document["inference_request_sha256"]),
        inference_f0_method=transfer.inference_config.inference_f0_method.value,
        metrics_algorithm=_PCM_METRICS_ALGORITHM,
        model=NativeInferencePublishedFile(
            path=model_path,
            workspace_relative_path="outputs/model/final_small_model.pth",
            size_bytes=model_evidence.size_bytes,
            sha256=model_evidence.sha256,
        ),
        index=index_file,
        rvc_commit_hash=binding.rvc_commit_hash,
        runtime_image_digest=expected_runtime_image_digest,
        runtime_asset_manifest_sha256=binding.asset_manifest_sha256,
        crepe_model=crepe_model,
        samples=tuple(samples),
    )


def _require_sample_claim(context: RvcRunContext) -> Any:
    if not context.claim.config.auto_inference_samples.enabled:
        raise NativeSampleInferenceError("sample inference is disabled for this Job")
    transfer = context.claim.test_set_transfer
    if transfer is None:
        raise NativeSampleInferenceError("sample Job has no verified TestSet snapshot")
    return transfer


def _validate_transfer_marker(context: RvcRunContext) -> None:
    transfer = _require_sample_claim(context)
    marker_path = context.workspace.outputs / "test_set_transfer.json"
    marker = _load_json_file(marker_path, 16 * 1024**2)
    expected_items = [
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
    ]
    expected = {
        "schema_version": 1,
        "test_set_id": transfer.test_set_id,
        "family_id": transfer.family_id,
        "revision": transfer.revision,
        "manifest_sha256": transfer.manifest_sha256,
        "sample_plan_sha256": transfer.sample_plan_sha256,
        "sample_plan_revalidation": "manager_claim_snapshot",
        "inference_config": transfer.inference_config.model_dump(mode="json"),
        "inference_config_sha256": transfer.inference_config_sha256,
        "items": expected_items,
    }
    if marker != expected:
        raise NativeSampleInferenceError(
            "TestSet transfer marker does not match the claimed snapshot"
        )


def _validate_test_set_inputs(context: RvcRunContext) -> tuple[_PcmEvidence, ...]:
    transfer = _require_sample_claim(context)
    total_bytes = sum(item.size_bytes for item in transfer.items)
    total_duration = math.fsum(item.duration_seconds for item in transfer.items)
    if total_bytes > 2 * 1024**3 or total_duration > 3_600:
        raise NativeSampleInferenceError("TestSet exceeds the fixed inference limits")
    root = context.workspace.inputs / "test_set"
    _require_safe_directory(root, context.workspace.root)
    expected_names = [item.filename for item in transfer.items]
    try:
        actual_names = [entry.name for entry in os.scandir(root)]
    except OSError as exc:
        raise NativeSampleInferenceError("TestSet input inventory is unreadable") from exc
    if len(actual_names) != len(expected_names) or set(actual_names) != set(expected_names):
        raise NativeSampleInferenceError("TestSet input inventory is not exact")
    records = []
    for item in transfer.items:
        if item.filename != f"{item.test_set_item_id}.wav":
            raise NativeSampleInferenceError("TestSet item filename is not deterministic")
        record = _inspect_pcm_wave(
            root / item.filename,
            maximum=max(item.size_bytes, 44),
            include_metrics=False,
        )
        tolerance = max(0.000001, 1 / item.sample_rate_hz)
        if (
            record.size_bytes != item.size_bytes
            or record.sha256 != item.sha256
            or record.sample_rate_hz != item.sample_rate_hz
            or record.channels != item.channels
            or abs(record.duration_seconds - item.duration_seconds) > tolerance
        ):
            raise NativeSampleInferenceError(
                "TestSet PCM input does not match the verified snapshot"
            )
        records.append(record)
    return tuple(records)


def _validate_driver_result(
    context: RvcRunContext,
    document: Mapping[str, Any],
    model: _FileEvidence,
    index: _FileEvidence | None,
    expected_rate: int,
    limits: NativeSampleInferenceLimits,
    binding: NativeSampleInferenceBinding,
    *,
    expected_runtime_image_digest: str,
    expected_request_sha256: str,
    expected_crepe_model: NativeCrepeModelEvidence | None,
) -> tuple[_PcmEvidence, ...]:
    transfer = _require_sample_claim(context)
    if set(document) != {
        "schema_version",
        "metrics_algorithm",
        "request_sha256",
        "model",
        "index",
        "runtime",
        "items",
    }:
        raise NativeSampleInferenceError("sample inference driver result fields are invalid")
    if (
        document.get("schema_version") != 1
        or document.get("metrics_algorithm") != _PCM_METRICS_ALGORITHM
        or document.get("request_sha256") != expected_request_sha256
    ):
        raise NativeSampleInferenceError("sample inference driver result version is invalid")
    raw_model = _require_mapping(document.get("model"), "driver model evidence")
    if raw_model != {"size_bytes": model.size_bytes, "sha256": model.sha256}:
        raise NativeSampleInferenceError("sample inference model evidence changed")
    raw_index = document.get("index")
    if index is None:
        if raw_index is not None:
            raise NativeSampleInferenceError("index evidence is forbidden at index_rate=0")
    else:
        index_mapping = _require_mapping(raw_index, "driver index evidence")
        if (
            index_mapping.get("size_bytes") != index.size_bytes
            or index_mapping.get("sha256") != index.sha256
            or index_mapping.get("dimension")
            != (256 if context.claim.config.model.version.value == "v1" else 768)
            or not isinstance(index_mapping.get("vector_count"), int)
            or isinstance(index_mapping.get("vector_count"), bool)
            or index_mapping["vector_count"] <= 0
        ):
            raise NativeSampleInferenceError("FAISS index evidence is invalid")
    runtime = _require_mapping(document.get("runtime"), "runtime evidence")
    runtime_crepe_model = _validate_runtime_evidence(runtime)
    if (
        runtime.get("device") != binding.device
        or runtime.get("use_half") != binding.use_half
        or runtime.get("runtime_image_digest") != expected_runtime_image_digest
        or runtime_crepe_model != expected_crepe_model
    ):
        raise NativeSampleInferenceError("sample inference runtime evidence changed")
    raw_items = _require_list(document.get("items"), "driver result items")
    if len(raw_items) != len(transfer.items):
        raise NativeSampleInferenceError("sample inference result count is invalid")
    outputs: list[_PcmEvidence] = []
    for item, raw in zip(transfer.items, raw_items, strict=True):
        record = _require_mapping(raw, "driver result item")
        expected_path = f"outputs/samples/{item.test_set_item_id}.wav"
        if (
            set(record)
            != {
                "test_set_item_id",
                "output_path",
                "size_bytes",
                "sha256",
                "sample_rate_hz",
                "channels",
                "sample_width_bytes",
                "frame_count",
                "duration_seconds",
                "metrics",
            }
            or record.get("test_set_item_id") != item.test_set_item_id
            or record.get("output_path") != expected_path
        ):
            raise NativeSampleInferenceError("sample inference result identity is invalid")
        output = context.workspace.outputs / "samples" / f"{item.test_set_item_id}.wav"
        inspected = _inspect_pcm_wave(
            output,
            maximum=limits.max_output_bytes,
            include_metrics=True,
        )
        metrics = _validate_metrics(_require_mapping(record.get("metrics"), "sample metrics"))
        if (
            inspected.size_bytes != record.get("size_bytes")
            or inspected.sha256 != record.get("sha256")
            or inspected.sample_rate_hz != expected_rate
            or inspected.sample_rate_hz != record.get("sample_rate_hz")
            or inspected.channels != 1
            or inspected.channels != record.get("channels")
            or inspected.sample_width_bytes != record.get("sample_width_bytes")
            or inspected.frame_count != record.get("frame_count")
            or not _same_float(inspected.duration_seconds, record.get("duration_seconds"))
            or inspected.duration_seconds > limits.max_output_duration_seconds
            or inspected.metrics is None
            or any(not _same_float(inspected.metrics[key], metrics[key]) for key in metrics)
        ):
            raise NativeSampleInferenceError("sample inference PCM output is invalid")
        outputs.append(inspected)
    if (
        sum(output.size_bytes for output in outputs) > limits.max_total_output_bytes
        or math.fsum(output.duration_seconds for output in outputs)
        > limits.max_total_output_duration_seconds
    ):
        raise NativeSampleInferenceError("sample inference output exceeds total limits")
    return tuple(outputs)


def _build_inference_manifest(
    context: RvcRunContext,
    binding: NativeSampleInferenceBinding,
    projection_instance: _FileEvidence,
    model: _FileEvidence,
    index: _FileEvidence | None,
    inputs: Sequence[_PcmEvidence],
    outputs: Sequence[_PcmEvidence],
    driver_result: Mapping[str, Any],
) -> dict[str, Any]:
    transfer = _require_sample_claim(context)
    driver_items = _require_list(driver_result.get("items"), "driver result items")
    index_result = driver_result.get("index")
    return {
        "schema_version": 1,
        "kind": "native-fixed-test-set-inference",
        "metrics_algorithm": _PCM_METRICS_ALGORITHM,
        "inference_request_sha256": driver_result["request_sha256"],
        "job_id": context.claim.job_id,
        "attempt_id": context.claim.attempt_id,
        "lease_id_sha256": hashlib.sha256(context.claim.lease_id.encode()).hexdigest(),
        "test_set": {
            "test_set_id": transfer.test_set_id,
            "family_id": transfer.family_id,
            "revision": transfer.revision,
            "manifest_sha256": transfer.manifest_sha256,
            "sample_plan_sha256": transfer.sample_plan_sha256,
            "inference_config_sha256": transfer.inference_config_sha256,
        },
        "inference_config": transfer.inference_config.model_dump(mode="json"),
        "model": {
            "path": "outputs/model/final_small_model.pth",
            "size_bytes": model.size_bytes,
            "sha256": model.sha256,
            "version": context.claim.config.model.version.value,
            "sample_rate": context.claim.config.model.sample_rate.value,
            "use_f0": context.claim.config.model.use_f0,
        },
        "index": (
            {
                "path": "outputs/index/final.index",
                "size_bytes": index.size_bytes,
                "sha256": index.sha256,
                "dimension": _require_mapping(index_result, "driver index")["dimension"],
                "vector_count": _require_mapping(index_result, "driver index")["vector_count"],
            }
            if index is not None
            else {
                "path": None,
                "size_bytes": None,
                "sha256": None,
                "dimension": None,
                "vector_count": None,
            }
        ),
        "rvc_runtime": {
            "rvc_commit_hash": binding.rvc_commit_hash,
            "asset_manifest_sha256": binding.asset_manifest_sha256,
            "projection_manifest_sha256": binding.projection_manifest_sha256,
            "attempt_projection_marker_sha256": projection_instance.sha256,
        },
        "runtime_evidence": driver_result["runtime"],
        "items": [
            {
                "test_set_item_id": item.test_set_item_id,
                "item_key": item.item_key,
                "sort_order": item.sort_order,
                "input": {
                    "path": f"inputs/test_set/{item.filename}",
                    "size_bytes": input_record.size_bytes,
                    "sha256": input_record.sha256,
                    "sample_rate_hz": input_record.sample_rate_hz,
                    "channels": input_record.channels,
                    "sample_width_bytes": input_record.sample_width_bytes,
                    "frame_count": input_record.frame_count,
                    "duration_seconds": input_record.duration_seconds,
                },
                "output": {
                    "path": f"outputs/samples/{item.test_set_item_id}.wav",
                    "size_bytes": output_record.size_bytes,
                    "sha256": output_record.sha256,
                    "sample_rate_hz": output_record.sample_rate_hz,
                    "channels": output_record.channels,
                    "sample_width_bytes": output_record.sample_width_bytes,
                    "frame_count": output_record.frame_count,
                    "duration_seconds": output_record.duration_seconds,
                },
                "metrics": _require_mapping(driver_item, "driver item")["metrics"],
            }
            for item, input_record, output_record, driver_item in zip(
                transfer.items, inputs, outputs, driver_items, strict=True
            )
        ],
    }


def _validate_published_manifest(
    context: RvcRunContext,
    manifest: Mapping[str, Any],
    binding: NativeSampleInferenceBinding,
    limits: NativeSampleInferenceLimits,
    *,
    expected_runtime_image_digest: str,
) -> tuple[Path, ...]:
    transfer = _require_sample_claim(context)
    _require_safe_directory(context.rvc_root, context.workspace.root)
    required_keys = {
        "schema_version",
        "kind",
        "metrics_algorithm",
        "inference_request_sha256",
        "job_id",
        "attempt_id",
        "lease_id_sha256",
        "test_set",
        "inference_config",
        "model",
        "index",
        "rvc_runtime",
        "runtime_evidence",
        "items",
    }
    if set(manifest) != required_keys or (
        manifest.get("schema_version") != 1
        or manifest.get("kind") != "native-fixed-test-set-inference"
        or manifest.get("metrics_algorithm") != _PCM_METRICS_ALGORITHM
        or not _is_sha256(manifest.get("inference_request_sha256"))
        or manifest.get("job_id") != context.claim.job_id
        or manifest.get("attempt_id") != context.claim.attempt_id
        or manifest.get("lease_id_sha256")
        != hashlib.sha256(context.claim.lease_id.encode()).hexdigest()
        or manifest.get("inference_config") != transfer.inference_config.model_dump(mode="json")
    ):
        raise NativeSampleInferenceError("published sample manifest identity is invalid")
    test_set = _require_mapping(manifest.get("test_set"), "manifest TestSet")
    if test_set != {
        "test_set_id": transfer.test_set_id,
        "family_id": transfer.family_id,
        "revision": transfer.revision,
        "manifest_sha256": transfer.manifest_sha256,
        "sample_plan_sha256": transfer.sample_plan_sha256,
        "inference_config_sha256": transfer.inference_config_sha256,
    }:
        raise NativeSampleInferenceError("published TestSet evidence is invalid")
    rvc = _require_mapping(manifest.get("rvc_runtime"), "manifest RVC runtime")
    if (
        rvc.get("rvc_commit_hash") != binding.rvc_commit_hash
        or rvc.get("asset_manifest_sha256") != binding.asset_manifest_sha256
        or rvc.get("projection_manifest_sha256") != binding.projection_manifest_sha256
        or not _is_sha256(rvc.get("attempt_projection_marker_sha256"))
    ):
        raise NativeSampleInferenceError("published RVC runtime evidence is invalid")
    marker = _snapshot_file(
        context.rvc_root / ".orchestrator-projection.json",
        maximum=limits.max_manifest_bytes,
    )
    if marker.sha256 != rvc["attempt_projection_marker_sha256"]:
        raise NativeSampleInferenceError("attempt RVC projection changed after inference")
    expected_crepe_model = (
        _snapshot_crepe_projection(
            context,
            marker,
            maximum=limits.max_crepe_asset_bytes,
            marker_maximum=limits.max_manifest_bytes,
        )
        if transfer.inference_config.inference_f0_method.value == "crepe"
        else None
    )
    runtime_evidence = _require_mapping(
        manifest.get("runtime_evidence"), "manifest runtime evidence"
    )
    runtime_crepe_model = _validate_runtime_evidence(runtime_evidence)
    if (
        runtime_evidence.get("device") != binding.device
        or runtime_evidence.get("use_half") != binding.use_half
        or runtime_evidence.get("runtime_image_digest") != expected_runtime_image_digest
        or runtime_crepe_model != expected_crepe_model
    ):
        raise NativeSampleInferenceError("published runtime binding evidence is invalid")
    model = _require_mapping(manifest.get("model"), "manifest model")
    model_path = context.workspace.outputs / "model" / "final_small_model.pth"
    _require_safe_directory(model_path.parent, context.workspace.root)
    model_evidence = _snapshot_file(model_path, maximum=limits.max_model_bytes)
    if (
        model.get("path") != "outputs/model/final_small_model.pth"
        or model.get("size_bytes") != model_evidence.size_bytes
        or model.get("sha256") != model_evidence.sha256
        or model.get("version") != context.claim.config.model.version.value
        or model.get("sample_rate") != context.claim.config.model.sample_rate.value
        or model.get("use_f0") != context.claim.config.model.use_f0
    ):
        raise NativeSampleInferenceError("published model evidence is invalid")
    index = _require_mapping(manifest.get("index"), "manifest index")
    if transfer.inference_config.index_rate == 0:
        if index != {
            "path": None,
            "size_bytes": None,
            "sha256": None,
            "dimension": None,
            "vector_count": None,
        }:
            raise NativeSampleInferenceError("published no-index evidence is invalid")
    else:
        index_path = context.workspace.outputs / "index" / "final.index"
        _require_safe_directory(index_path.parent, context.workspace.root)
        index_evidence = _snapshot_file(index_path, maximum=limits.max_index_bytes)
        if (
            index.get("path") != "outputs/index/final.index"
            or index.get("size_bytes") != index_evidence.size_bytes
            or index.get("sha256") != index_evidence.sha256
            or index.get("dimension")
            != (256 if context.claim.config.model.version.value == "v1" else 768)
            or not isinstance(index.get("vector_count"), int)
            or isinstance(index.get("vector_count"), bool)
            or index["vector_count"] <= 0
        ):
            raise NativeSampleInferenceError("published FAISS index evidence is invalid")
    input_records = _validate_test_set_inputs(context)
    raw_items = _require_list(manifest.get("items"), "published sample items")
    if len(raw_items) != len(transfer.items):
        raise NativeSampleInferenceError("published sample inventory count is invalid")
    sample_root = context.workspace.outputs / "samples"
    _require_safe_directory(sample_root, context.workspace.root)
    expected_names = {
        _SAMPLE_MANIFEST_NAME,
        *(f"{item.test_set_item_id}.wav" for item in transfer.items),
    }
    try:
        actual_names = {entry.name for entry in os.scandir(sample_root)}
    except OSError as exc:
        raise NativeSampleInferenceError("published sample inventory is unreadable") from exc
    if actual_names != expected_names:
        raise NativeSampleInferenceError("published sample inventory is not exact")
    expected_rate = (
        transfer.inference_config.resample_sr
        if transfer.inference_config.resample_sr != 0
        else (40_000 if context.claim.config.model.sample_rate.value == "40k" else 48_000)
    )
    created: list[Path] = []
    published_outputs: list[_PcmEvidence] = []
    for item, input_record, raw in zip(transfer.items, input_records, raw_items, strict=True):
        record = _require_mapping(raw, "published sample item")
        if set(record) != {
            "test_set_item_id",
            "item_key",
            "sort_order",
            "input",
            "output",
            "metrics",
        } or (
            record.get("test_set_item_id") != item.test_set_item_id
            or record.get("item_key") != item.item_key
            or record.get("sort_order") != item.sort_order
        ):
            raise NativeSampleInferenceError("published sample identity is invalid")
        raw_input = _require_mapping(record.get("input"), "published sample input")
        if raw_input != {
            "path": f"inputs/test_set/{item.filename}",
            "size_bytes": input_record.size_bytes,
            "sha256": input_record.sha256,
            "sample_rate_hz": input_record.sample_rate_hz,
            "channels": input_record.channels,
            "sample_width_bytes": input_record.sample_width_bytes,
            "frame_count": input_record.frame_count,
            "duration_seconds": input_record.duration_seconds,
        }:
            raise NativeSampleInferenceError("published sample input evidence is invalid")
        output_path = sample_root / f"{item.test_set_item_id}.wav"
        output = _inspect_pcm_wave(
            output_path,
            maximum=limits.max_output_bytes,
            include_metrics=True,
        )
        raw_output = _require_mapping(record.get("output"), "published sample output")
        if raw_output != {
            "path": f"outputs/samples/{item.test_set_item_id}.wav",
            "size_bytes": output.size_bytes,
            "sha256": output.sha256,
            "sample_rate_hz": output.sample_rate_hz,
            "channels": output.channels,
            "sample_width_bytes": output.sample_width_bytes,
            "frame_count": output.frame_count,
            "duration_seconds": output.duration_seconds,
        } or (
            output.sample_rate_hz != expected_rate
            or output.channels != 1
            or output.duration_seconds > limits.max_output_duration_seconds
        ):
            raise NativeSampleInferenceError("published sample output evidence is invalid")
        metrics = _validate_metrics(_require_mapping(record.get("metrics"), "metrics"))
        if output.metrics is None or any(
            not _same_float(output.metrics[key], metrics[key]) for key in metrics
        ):
            raise NativeSampleInferenceError("published sample metrics are invalid")
        created.append(output_path)
        published_outputs.append(output)
    if (
        sum(output.size_bytes for output in published_outputs) > limits.max_total_output_bytes
        or math.fsum(output.duration_seconds for output in published_outputs)
        > limits.max_total_output_duration_seconds
    ):
        raise NativeSampleInferenceError("published sample output exceeds total limits")
    return tuple(created)


def _snapshot_crepe_projection(
    context: RvcRunContext,
    marker_evidence: _FileEvidence,
    *,
    maximum: int,
    marker_maximum: int,
) -> NativeCrepeModelEvidence:
    marker_path = context.rvc_root / ".orchestrator-projection.json"
    marker = _load_json_file(
        marker_path,
        marker_maximum,
        required_mode=0o444,
    )
    if (
        set(marker)
        != {
            "schema_version",
            "rvc_commit_hash",
            "projection_directories",
            "files",
        }
        or marker.get("schema_version") != 1
        or marker.get("rvc_commit_hash") != RVC_REVIEWED_COMMIT
        or marker.get("projection_directories") != [*_PROJECTION_BASE_DIRECTORIES, "runtime/crepe"]
        or not isinstance(marker.get("files"), list)
    ):
        raise NativeSampleInferenceError("CREPE projection marker is invalid")
    records: dict[str, _FileEvidence] = {}
    for raw in marker["files"]:
        record = _require_mapping(raw, "projection file record")
        if set(record) != {"path", "size_bytes", "sha256"}:
            raise NativeSampleInferenceError("CREPE projection record is invalid")
        path = record.get("path")
        size = record.get("size_bytes")
        digest = record.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or path in records
            or type(size) is not int
            or size <= 0
            or not _is_sha256(digest)
        ):
            raise NativeSampleInferenceError("CREPE projection record is invalid")
        assert isinstance(digest, str)
        records[path] = _FileEvidence(size, digest)
    crepe_paths = {path for path in records if path.startswith("runtime/crepe/")}
    if crepe_paths != {_CREPE_MODEL_RELATIVE_PATH}:
        raise NativeSampleInferenceError("CREPE projection inventory is not exact")
    asset_path = context.rvc_root.joinpath(*_CREPE_MODEL_RELATIVE_PATH.split("/"))
    _require_safe_directory(asset_path.parent, context.workspace.root)
    try:
        actual_names = {entry.name for entry in os.scandir(asset_path.parent)}
    except OSError as exc:
        raise NativeSampleInferenceError("CREPE projection inventory is unreadable") from exc
    if actual_names != {asset_path.name}:
        raise NativeSampleInferenceError("CREPE projection inventory is not exact")
    expected = records[_CREPE_MODEL_RELATIVE_PATH]
    actual = _snapshot_file(
        asset_path,
        maximum=maximum,
        required_mode=0o444,
    )
    if actual != expected:
        raise NativeSampleInferenceError("CREPE asset differs from projection marker")
    _require_snapshot(
        marker_path,
        marker_evidence,
        maximum=marker_maximum,
        required_mode=0o444,
    )
    return NativeCrepeModelEvidence(
        asset_size_bytes=actual.size_bytes,
        asset_sha256=actual.sha256,
        weights_only=True,
        model_capacity=_CREPE_MODEL_CAPACITY,
    )


def _crepe_evidence_document(
    evidence: NativeCrepeModelEvidence | None,
) -> dict[str, Any] | None:
    if evidence is None:
        return None
    return {
        "asset_size_bytes": evidence.asset_size_bytes,
        "asset_sha256": evidence.asset_sha256,
        "weights_only": evidence.weights_only,
        "model_capacity": evidence.model_capacity,
    }


def _file_evidence_from_crepe(evidence: NativeCrepeModelEvidence) -> _FileEvidence:
    return _FileEvidence(evidence.asset_size_bytes, evidence.asset_sha256)


def _validate_crepe_evidence(value: Any) -> NativeCrepeModelEvidence | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "asset_size_bytes",
        "asset_sha256",
        "weights_only",
        "model_capacity",
    }:
        raise NativeSampleInferenceError("native CREPE runtime evidence is invalid")
    size = value.get("asset_size_bytes")
    digest = value.get("asset_sha256")
    if (
        type(size) is not int
        or size <= 0
        or not _is_sha256(digest)
        or value.get("weights_only") is not True
        or value.get("model_capacity") != _CREPE_MODEL_CAPACITY
    ):
        raise NativeSampleInferenceError("native CREPE runtime evidence is invalid")
    assert isinstance(digest, str)
    return NativeCrepeModelEvidence(
        asset_size_bytes=size,
        asset_sha256=digest,
        weights_only=True,
        model_capacity=_CREPE_MODEL_CAPACITY,
    )


def _validate_runtime_evidence(
    value: Mapping[str, Any],
) -> NativeCrepeModelEvidence | None:
    required = {
        "torch_version",
        "torch_major_minor",
        "python_version",
        "platform",
        "device",
        "cuda_available",
        "cuda_version",
        "model_load_trust_mode",
        "operator_asset_load_trust_mode",
        "crepe_model",
        "runtime_image_digest",
        "use_half",
    }
    if (
        set(value) != required
        or not isinstance(value.get("torch_version"), str)
        or not isinstance(value.get("torch_major_minor"), str)
        or not isinstance(value.get("python_version"), str)
        or not isinstance(value.get("platform"), str)
        or not isinstance(value.get("device"), str)
        or not isinstance(value.get("cuda_available"), bool)
        or not isinstance(value.get("use_half"), bool)
        or value.get("cuda_version") is not None
        and not isinstance(value.get("cuda_version"), str)
        or not isinstance(value.get("runtime_image_digest"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value["runtime_image_digest"]) is None
        or value.get("model_load_trust_mode") != "weights_only=True"
        or value.get("operator_asset_load_trust_mode") != "manifest-verified;weights_only=False"
    ):
        raise NativeSampleInferenceError("native inference runtime evidence is invalid")
    try:
        major, minor = (int(part) for part in str(value["torch_major_minor"]).split("."))
    except (TypeError, ValueError) as exc:
        raise NativeSampleInferenceError("native inference torch evidence is invalid") from exc
    rendered_version = re.match(r"^(\d+)\.(\d+)(?:\.|$)", value["torch_version"])
    if rendered_version is None or (major, minor) != (
        int(rendered_version.group(1)),
        int(rendered_version.group(2)),
    ):
        raise NativeSampleInferenceError("native inference torch versions do not match")
    if (major, minor) < (2, 6):
        raise NativeSampleInferenceError("native inference requires torch 2.6 or newer")
    return _validate_crepe_evidence(value.get("crepe_model"))


def _inspect_pcm_wave(
    path: Path,
    *,
    maximum: int,
    include_metrics: bool,
) -> _PcmEvidence:
    descriptor, initial = _open_regular(path, maximum=maximum)
    digest = hashlib.sha256()
    metric_count = 0
    metric_square_sum = 0.0
    metric_peak = 0.0
    metric_clipped = 0
    metric_silent = 0
    try:
        if stat.S_IMODE(initial.st_mode) != 0o600:
            raise NativeSampleInferenceError("sample PCM file permissions are invalid")
        with os.fdopen(os.dup(descriptor), "rb") as source:
            while chunk := source.read(1024**2):
                digest.update(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(descriptor), "rb") as source:
            try:
                with wave.open(source, "rb") as audio:
                    channels = audio.getnchannels()
                    sample_rate = audio.getframerate()
                    sample_width = audio.getsampwidth()
                    frame_count = audio.getnframes()
                    if (
                        audio.getcomptype() != "NONE"
                        or channels <= 0
                        or sample_rate <= 0
                        or frame_count <= 0
                        or sample_width not in {1, 2, 3, 4}
                    ):
                        raise NativeSampleInferenceError("sample WAV is not bounded PCM")
                    if include_metrics and (channels != 1 or sample_width != 2):
                        raise NativeSampleInferenceError("generated sample WAV must be mono PCM16")
                    decoded = 0
                    while decoded < frame_count:
                        frame_bytes = audio.readframes(min(65_536, frame_count - decoded))
                        if not frame_bytes or len(frame_bytes) % (channels * sample_width):
                            raise NativeSampleInferenceError("sample PCM frames are truncated")
                        count = len(frame_bytes) // (channels * sample_width)
                        decoded += count
                        if include_metrics:
                            values = array("h")
                            values.frombytes(frame_bytes)
                            if sys.byteorder != "little":
                                values.byteswap()
                            (
                                chunk_count,
                                chunk_square_sum,
                                chunk_peak,
                                chunk_clipped,
                                chunk_silent,
                            ) = _accumulate_integer_pcm(
                                values,
                                sample_width_bytes=2,
                            )
                            metric_count += chunk_count
                            metric_square_sum += chunk_square_sum
                            metric_peak = max(metric_peak, chunk_peak)
                            metric_clipped += chunk_clipped
                            metric_silent += chunk_silent
                    if decoded != frame_count or audio.readframes(1):
                        raise NativeSampleInferenceError("sample PCM frame count is invalid")
            except (EOFError, wave.Error) as exc:
                raise NativeSampleInferenceError("sample WAV structure is invalid") from exc
        final = os.fstat(descriptor)
        if _stat_identity(initial) != _stat_identity(final):
            raise NativeSampleInferenceError("sample file changed during verification")
        metrics = (
            _finish_integer_pcm_metrics(
                metric_count,
                metric_square_sum,
                metric_peak,
                metric_clipped,
                metric_silent,
            )
            if include_metrics
            else None
        )
        return _PcmEvidence(
            size_bytes=initial.st_size,
            sha256=digest.hexdigest(),
            sample_rate_hz=sample_rate,
            channels=channels,
            sample_width_bytes=sample_width,
            frame_count=frame_count,
            duration_seconds=frame_count / sample_rate,
            metrics=metrics,
        )
    except OSError as exc:
        raise NativeSampleInferenceError("sample PCM file could not be verified") from exc
    finally:
        os.close(descriptor)


def _integer_pcm_metrics(values: Sequence[int], *, sample_width_bytes: int) -> dict[str, float]:
    """Compute v2 normalized metrics with width-quantized rail clipping."""

    return _finish_integer_pcm_metrics(
        *_accumulate_integer_pcm(values, sample_width_bytes=sample_width_bytes)
    )


def _accumulate_integer_pcm(
    values: Sequence[int], *, sample_width_bytes: int
) -> tuple[int, float, float, int, int]:

    if sample_width_bytes not in {1, 2, 3, 4} or not values:
        raise NativeSampleInferenceError("sample PCM metrics input is invalid")
    scale = 1 << (sample_width_bytes * 8 - 1)
    minimum = -scale
    maximum = scale - 1
    negative_clip_threshold = -math.ceil(scale * _CLIPPING_THRESHOLD)
    positive_clip_threshold = math.ceil(maximum * _CLIPPING_THRESHOLD)
    count = 0
    square_sum = 0.0
    peak = 0.0
    clipped = 0
    silent = 0
    for value in values:
        if type(value) is not int or not minimum <= value <= maximum:
            raise NativeSampleInferenceError("sample PCM metric value is invalid")
        normalized = value / scale
        amplitude = abs(normalized)
        count += 1
        square_sum += normalized * normalized
        peak = max(peak, amplitude)
        clipped += value <= negative_clip_threshold or value >= positive_clip_threshold
        silent += amplitude <= _SILENCE_THRESHOLD
    return count, square_sum, peak, clipped, silent


def _finish_integer_pcm_metrics(
    count: int,
    square_sum: float,
    peak: float,
    clipped: int,
    silent: int,
) -> dict[str, float]:
    if count <= 0:
        raise NativeSampleInferenceError("sample PCM metrics input is empty")
    return {
        "peak_amplitude": peak,
        "rms": math.sqrt(square_sum / count),
        "clipping_ratio": clipped / count,
        "silence_ratio": silent / count,
    }


def _validate_metrics(value: Mapping[str, Any]) -> dict[str, float]:
    keys = {"peak_amplitude", "rms", "clipping_ratio", "silence_ratio"}
    if set(value) != keys:
        raise NativeSampleInferenceError("sample metric fields are invalid")
    result: dict[str, float] = {}
    for key in keys:
        raw = value[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise NativeSampleInferenceError("sample metric value is invalid")
        converted = float(raw)
        if not math.isfinite(converted) or not 0 <= converted <= 1:
            raise NativeSampleInferenceError("sample metric value is out of bounds")
        result[key] = converted
    return result


def _snapshot_file(
    path: Path,
    *,
    maximum: int,
    required_mode: int | None = None,
) -> _FileEvidence:
    descriptor, initial = _open_regular(path, maximum=maximum)
    digest = hashlib.sha256()
    try:
        if required_mode is not None and stat.S_IMODE(initial.st_mode) != required_mode:
            raise NativeSampleInferenceError("inference file permissions are invalid")
        while chunk := os.read(descriptor, 1024**2):
            digest.update(chunk)
        final = os.fstat(descriptor)
        if _stat_identity(initial) != _stat_identity(final):
            raise NativeSampleInferenceError("inference input changed during verification")
        return _FileEvidence(initial.st_size, digest.hexdigest())
    except OSError as exc:
        raise NativeSampleInferenceError("inference input could not be verified") from exc
    finally:
        os.close(descriptor)


def _require_snapshot(
    path: Path,
    expected: _FileEvidence,
    *,
    maximum: int,
    required_mode: int | None = None,
) -> None:
    actual = _snapshot_file(
        path,
        maximum=maximum,
        required_mode=required_mode,
    )
    if actual != expected:
        raise NativeSampleInferenceError("inference input changed during execution")


def _open_regular(path: Path, *, maximum: int) -> tuple[int, os.stat_result]:
    try:
        descriptor = _open_absolute_nofollow(path, os.O_RDONLY)
    except OSError as exc:
        raise NativeSampleInferenceError("inference file cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > maximum
        ):
            raise NativeSampleInferenceError("inference file metadata is invalid")
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _open_absolute_nofollow(path: Path, final_flags: int) -> int:
    """Open an absolute path with O_NOFOLLOW on every ancestor component."""

    rendered = str(path)
    if not path.is_absolute() or path != Path(os.path.abspath(rendered)):
        raise NativeSampleInferenceError("native inference path is not normalized")
    components = path.parts[1:]
    if not components:
        raise NativeSampleInferenceError("native inference path cannot be root")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    current = os.open("/", directory_flags)
    try:
        for component in components[:-1]:
            following = os.open(component, directory_flags, dir_fd=current)
            os.close(current)
            current = following
        return os.open(
            components[-1],
            final_flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=current,
        )
    finally:
        os.close(current)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _load_json_file(
    path: Path,
    maximum: int,
    *,
    required_mode: int = 0o600,
) -> Mapping[str, Any]:
    descriptor, initial = _open_regular(path, maximum=maximum)
    try:
        if stat.S_IMODE(initial.st_mode) != required_mode:
            raise NativeSampleInferenceError("native inference JSON permissions are invalid")
        content = bytearray()
        while chunk := os.read(descriptor, min(1024**2, maximum + 1 - len(content))):
            content.extend(chunk)
            if len(content) > maximum:
                raise NativeSampleInferenceError("native inference JSON exceeds its limit")
        if _stat_identity(initial) != _stat_identity(os.fstat(descriptor)):
            raise NativeSampleInferenceError("native inference JSON changed during read")
        document = json.loads(
            bytes(content).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NativeSampleInferenceError("native inference JSON is invalid") from exc
    finally:
        os.close(descriptor)
    if not isinstance(document, dict):
        raise NativeSampleInferenceError("native inference JSON root is invalid")
    return document


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NativeSampleInferenceError("native inference JSON has duplicate keys")
        result[key] = value
    return result


def _canonical_json_sha256(document: Mapping[str, Any]) -> str:
    try:
        content = json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NativeSampleInferenceError("native inference JSON is not canonical") from exc
    return hashlib.sha256(content).hexdigest()


def _atomic_write_json(path: Path, document: Mapping[str, Any], boundary: Path) -> None:
    try:
        ensure_within(path, boundary)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _require_safe_directory(path.parent, boundary)
    except (OSError, ValueError, WorkspaceError) as exc:
        raise NativeSampleInferenceError("native inference JSON destination is unsafe") from exc
    temporary_name = f".{path.name}.{uuid4().hex}.partial"
    parent_descriptor: int | None = None
    descriptor: int | None = None
    try:
        parent_descriptor = _open_absolute_nofollow(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        content = json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        _write_all(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
    except (OSError, TypeError, ValueError) as exc:
        raise NativeSampleInferenceError("native inference JSON could not be published") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_descriptor is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            os.close(parent_descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def _require_safe_parent(path: Path, boundary: Path) -> None:
    _require_safe_directory(path, boundary)


def _require_safe_directory(path: Path, boundary: Path) -> None:
    try:
        ensure_within(path, boundary)
        relative = path.relative_to(boundary)
        current = boundary
        metadata = current.stat(follow_symlinks=False)
        if current.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise NativeSampleInferenceError("native inference directory ancestry is unsafe")
        for component in relative.parts:
            current /= component
            metadata = current.stat(follow_symlinks=False)
            if current.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                raise NativeSampleInferenceError("native inference directory ancestry is unsafe")
    except (OSError, ValueError, WorkspaceError) as exc:
        raise NativeSampleInferenceError("native inference directory is unsafe") from exc
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise NativeSampleInferenceError("native inference directory is unsafe")


def _require_regular_file(path: Path, boundary: Path) -> None:
    try:
        ensure_within(path, boundary)
        metadata = path.stat(follow_symlinks=False)
    except (OSError, WorkspaceError) as exc:
        raise NativeSampleInferenceError("native inference file is unsafe") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise NativeSampleInferenceError("native inference file is unsafe")


def _remove_regular_file(path: Path, boundary: Path) -> None:
    _require_regular_file(path, boundary)
    parent_descriptor = _open_absolute_nofollow(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        metadata = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise NativeSampleInferenceError("native inference file is unsafe")
        os.unlink(path.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except OSError as exc:
        raise NativeSampleInferenceError("native inference file could not be removed") from exc
    finally:
        os.close(parent_descriptor)


def _path_exists_or_symlink(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise NativeSampleInferenceError("native inference path cannot be inspected") from exc
    return True


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    del label
    if not isinstance(value, dict):
        raise NativeSampleInferenceError("native inference object is invalid")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    del label
    if not isinstance(value, list):
        raise NativeSampleInferenceError("native inference list is invalid")
    return value


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256 for character in value)
    )


def _same_float(left: float, right: Any) -> bool:
    return (
        not isinstance(right, bool)
        and isinstance(right, (int, float))
        and math.isfinite(float(right))
        and math.isclose(left, float(right), rel_tol=0, abs_tol=1e-12)
    )


def _inference_environment(context: RvcRunContext) -> dict[str, str]:
    home = context.workspace.work / "sample-home"
    temporary = context.workspace.work / "sample-tmp"
    matplotlib = context.workspace.work / "sample-matplotlib"
    torch_home = context.workspace.work / "sample-torch-home"
    hf_home = context.workspace.work / "sample-hf-home"
    for directory in (home, temporary, matplotlib, torch_home, hf_home):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _require_safe_directory(directory, context.workspace.root)
    # These hints keep library cache lookups attempt-private and offline. They are
    # defense in depth, not an OS network-namespace or egress-policy attestation.
    # SafeSubprocessRunner builds a fresh allowlist environment, so proxy or URL
    # credentials from the Worker process are not inherited.
    return {
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "MPLCONFIGDIR": str(matplotlib),
        "TORCH_HOME": str(torch_home),
        "HF_HOME": str(hf_home),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "PYTHONPATH": str(context.rvc_root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "rmvpe_root": str(context.rvc_root / "assets" / "rmvpe"),
    }


__all__ = [
    "NativeCrepeModelEvidence",
    "NativeFixedTestSetInferenceDependency",
    "NativeInferencePublication",
    "NativeInferencePublishedFile",
    "NativeInferencePublishedSample",
    "NativeSampleInferenceError",
    "NativeSampleInferenceLimits",
    "build_native_sample_inference_command",
    "load_native_inference_publication",
]
