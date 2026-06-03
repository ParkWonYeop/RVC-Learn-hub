"""Typed, fail-closed stage adapter for the reviewed RVC WebUI revision.

The adapter never executes the shared, image-level checkout directly.  It verifies
the reviewed revision, copies a narrow set of code/config/model inputs into the
attempt workspace, and runs every upstream command against that private projection.
The projection deliberately excludes upstream writable output directories.

Dataset transfer/validation/materialization is supplied by the agent-level
``DatasetStageRunner`` dependency. Sample inference fails closed without the
dependency injected from a strict, release-owned runtime activation projection.
The explicit ``native`` factory mode therefore remains guarded by runtime manifests
and installer qualification gates.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import uuid4

from rvc_orchestrator_contracts import JobClaim, JobStatus, TrainingF0Method

from .artifacts import (
    ArtifactDiscoveryError,
    Checkpoint,
    discover_checkpoints,
    find_index_candidates,
    select_final_index,
    sha256_file,
)
from .index_builder import build_index_command
from .pretrained import resolve_pretrained
from .process import OutputCallback, ProcessResult, ProcessSpec, SafeSubprocessRunner
from .runner import RvcConfigurationError, RvcRunContext, RvcRunnerError, StageResult
from .rvc_commands import (
    RVC_REVIEWED_COMMIT,
    RVC_UPSTREAM_REPOSITORY,
    RvcCliRuntime,
    RvcCommandError,
    build_f0_extraction_commands,
    build_feature_extraction_commands,
    build_preprocess_command,
    build_training_command,
    validate_gpu_ids,
)
from .small_model import build_small_model_command
from .training_inputs import prepare_training_inputs
from .training_metrics import (
    ParsedTrainingMetric,
    TrainingLogParser,
    read_tensorboard_scalars,
)
from .workspace import ensure_within


class NativeRvcRunnerError(RvcRunnerError):
    """Raised when a native RVC stage cannot preserve the execution boundary."""


class NativeRvcConfigurationError(NativeRvcRunnerError, RvcConfigurationError):
    """Raised when a claim conflicts with the reviewed native runtime contract."""


class NativeStageDependency(Protocol):
    """Explicit dependency for stages whose data contracts are not implemented here."""

    async def run_stage(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult: ...


@dataclass(frozen=True, slots=True)
class NativeSampleInferenceBinding:
    """Immutable evidence a sample dependency must bind before it can run."""

    rvc_commit_hash: str
    asset_manifest_sha256: str
    projection_manifest_sha256: str
    python_executable: str
    device: str
    use_half: bool


@dataclass(frozen=True, slots=True)
class NativeSampleInferenceRuntimeEvidence:
    """Exact runtime pair exposed to capability/active-attempt snapshots."""

    runtime_image_digest: str
    runtime_asset_manifest_sha256: str


class NativeSampleInferenceDependency(Protocol):
    """Explicit, reviewed implementation of fixed-TestSet sample stages only."""

    @property
    def runtime_image_digest(self) -> str: ...

    def bind_native_runtime(self, binding: NativeSampleInferenceBinding) -> None: ...

    async def run_stage(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult: ...


class NativeProcessRunner(Protocol):
    """Injectable subprocess boundary used by dependency-free stage tests."""

    def run(
        self,
        spec: ProcessSpec,
        cancellation: asyncio.Event,
        *,
        output_callback: OutputCallback | None = None,
    ) -> Awaitable[ProcessResult]: ...


class NativeTrainingTelemetrySink(Protocol):
    """Durable-first attempt sink bound by the WorkerAgent for one claim."""

    async def record_training_event(
        self,
        *,
        source: str,
        event_key: str,
        message: str | None,
        metrics: tuple[ParsedTrainingMetric, ...] = (),
        channel: str | None = None,
    ) -> None: ...

    async def finish_training(self) -> None: ...


RevisionReader = Callable[[Path], str]


@dataclass(frozen=True, slots=True)
class NativeRvcRuntime:
    """Reviewed source and bounded execution settings for the typed adapter."""

    source_root: Path
    asset_manifest_path: Path
    python_executable: str = sys.executable
    expected_commit: str = RVC_REVIEWED_COMMIT
    cpu_workers: int = 2
    device: str = "cuda"
    use_half: bool = True
    available_gpu_ids: tuple[int, ...] | None = None
    preprocess_timeout_seconds: float = 3_600.0
    extraction_timeout_seconds: float = 7_200.0
    training_timeout_seconds: float = 7 * 24 * 3_600.0
    index_timeout_seconds: float = 24 * 3_600.0
    small_model_timeout_seconds: float = 3_600.0
    max_metric_records: int = 10_000
    max_train_log_parse_bytes: int = 64 * 1024**2
    telemetry_poll_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        rendered_root = str(self.source_root)
        if (
            not self.source_root.is_absolute()
            or "\x00" in rendered_root
            or self.source_root != Path(os.path.abspath(rendered_root))
        ):
            raise NativeRvcRunnerError(
                "native RVC source_root must be absolute, normalized, and NUL-free"
            )
        if self.expected_commit != RVC_REVIEWED_COMMIT:
            raise NativeRvcRunnerError("native RVC source commit has not been reviewed")
        expected_manifest = self.source_root / "assets-manifest.json"
        if self.asset_manifest_path != expected_manifest:
            raise NativeRvcRunnerError(
                "native RVC asset manifest must be source_root/assets-manifest.json"
            )
        python_path = Path(self.python_executable)
        if (
            not self.python_executable
            or "\x00" in self.python_executable
            or not python_path.is_absolute()
            or python_path != Path(os.path.abspath(self.python_executable))
        ):
            raise NativeRvcRunnerError(
                "native RVC Python executable must be absolute, normalized, and NUL-free"
            )
        if re.fullmatch(r"(?:cuda(?::[0-9]+)?|cpu|mps)", self.device) is None:
            raise NativeRvcRunnerError("native RVC device is invalid")
        if not isinstance(self.use_half, bool):
            raise NativeRvcRunnerError("native RVC use_half must be boolean")
        if not 1 <= self.cpu_workers <= 256:
            raise NativeRvcRunnerError("native RVC cpu_workers must be between 1 and 256")
        timeouts = (
            self.preprocess_timeout_seconds,
            self.extraction_timeout_seconds,
            self.training_timeout_seconds,
            self.index_timeout_seconds,
            self.small_model_timeout_seconds,
        )
        if any(not math.isfinite(value) or value <= 0 for value in timeouts):
            raise NativeRvcRunnerError(
                "native RVC stage timeouts must be finite and greater than zero"
            )
        if (
            isinstance(self.max_metric_records, bool)
            or not 1 <= self.max_metric_records <= 1_000_000
            or isinstance(self.max_train_log_parse_bytes, bool)
            or not 1 <= self.max_train_log_parse_bytes <= 1024**3
            or not math.isfinite(self.telemetry_poll_interval_seconds)
            or not 0.05 <= self.telemetry_poll_interval_seconds <= 60.0
        ):
            raise NativeRvcRunnerError("native RVC metric parsing limits are invalid")


@dataclass(frozen=True, slots=True)
class CheckpointPair:
    epoch: int
    generator: Path
    discriminator: Path


@dataclass(frozen=True, slots=True)
class ProjectionInputRecord:
    path: Path
    size: int
    sha256: str
    mode: int


_PROJECTION_DIRECTORIES = (
    Path("infer"),
    Path("configs"),
    Path("assets/pretrained"),
    Path("assets/pretrained_v2"),
    Path("assets/hubert"),
    Path("assets/rmvpe"),
    Path("logs/mute"),
)
_OPTIONAL_PROJECTION_DIRECTORIES = (Path("runtime/crepe"),)
_CREPE_MODEL_PATH = Path("runtime/crepe/full.pth")
_REQUIRED_PROJECTED_FILES = (
    Path("infer/lib/audio.py"),
    Path("infer/lib/rmvpe.py"),
    Path("infer/lib/infer_pack/models.py"),
    Path("infer/modules/vc/modules.py"),
    Path("infer/modules/vc/pipeline.py"),
    Path("infer/modules/vc/utils.py"),
    Path("infer/modules/train/preprocess.py"),
    Path("infer/modules/train/extract/extract_f0_print.py"),
    Path("infer/modules/train/extract/extract_f0_rmvpe.py"),
    Path("infer/modules/train/extract_feature_print.py"),
    Path("infer/modules/train/train.py"),
    Path("infer/lib/train/process_ckpt.py"),
    Path("configs/v1/40k.json"),
    Path("configs/v1/48k.json"),
    Path("configs/v2/48k.json"),
    Path("assets/hubert/hubert_base.pt"),
    Path("assets/rmvpe/rmvpe.pt"),
    Path("assets/pretrained/G40k.pth"),
    Path("assets/pretrained/D40k.pth"),
    Path("assets/pretrained/f0G40k.pth"),
    Path("assets/pretrained/f0D40k.pth"),
    Path("assets/pretrained/G48k.pth"),
    Path("assets/pretrained/D48k.pth"),
    Path("assets/pretrained/f0G48k.pth"),
    Path("assets/pretrained/f0D48k.pth"),
    Path("assets/pretrained_v2/G40k.pth"),
    Path("assets/pretrained_v2/D40k.pth"),
    Path("assets/pretrained_v2/f0G40k.pth"),
    Path("assets/pretrained_v2/f0D40k.pth"),
    Path("assets/pretrained_v2/G48k.pth"),
    Path("assets/pretrained_v2/D48k.pth"),
    Path("assets/pretrained_v2/f0G48k.pth"),
    Path("assets/pretrained_v2/f0D48k.pth"),
    Path("logs/mute/0_gt_wavs/mute40k.wav"),
    Path("logs/mute/0_gt_wavs/mute48k.wav"),
    Path("logs/mute/3_feature256/mute.npy"),
    Path("logs/mute/3_feature768/mute.npy"),
    Path("logs/mute/2a_f0/mute.wav.npy"),
    Path("logs/mute/2b-f0nsf/mute.wav.npy"),
)
_PROJECTION_MARKER = Path(".orchestrator-projection.json")
_REVISION_MARKER = Path(".rvc-reviewed-commit")
_ALLOWED_FILE_SUFFIXES = {
    ".json",
    ".npy",
    ".npz",
    ".pth",
    ".pt",
    ".py",
    ".txt",
    ".wav",
    ".yaml",
    ".yml",
}
_MAX_PROJECTION_FILES = 250_000
_MAX_PROJECTION_BYTES = 50 * 1024**3
_MAX_METRIC_LINE_BYTES = 1024**2
_MAX_TENSORBOARD_FILES = 64
_MAX_TENSORBOARD_FILE_BYTES = 64 * 1024**2
_MAX_TENSORBOARD_TOTAL_BYTES = 256 * 1024**2
_CHECKPOINT_NAME = re.compile(r"^[GD]_[0-9]+\.pth$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASSET_MANIFEST_MAX_BYTES = 16 * 1024**2
_ASSET_MANIFEST_MAX_RECORDS = 10_000
_REQUIRED_NATIVE_ASSETS = frozenset(
    {
        *(path.as_posix() for path in _REQUIRED_PROJECTED_FILES if path.parts[0] == "assets"),
        *(path.as_posix() for path in _REQUIRED_PROJECTED_FILES if path.parts[0] == "logs"),
        "runtime/bin/ffmpeg",
        "runtime/bin/ffprobe",
    }
)


class PinnedRvcRunner:
    """Connect reviewed RVC commands to typed Worker stages in a private tree."""

    def __init__(
        self,
        runtime: NativeRvcRuntime,
        *,
        process_runner: NativeProcessRunner | None = None,
        stage_dependency: NativeStageDependency | None = None,
        sample_inference_dependency: NativeSampleInferenceDependency | None = None,
        revision_reader: RevisionReader | None = None,
    ) -> None:
        self.runtime = runtime
        self.process_runner = process_runner or SafeSubprocessRunner()
        self.stage_dependency = stage_dependency
        self.sample_inference_dependency = sample_inference_dependency
        self._revision_reader = revision_reader or _read_reviewed_revision
        self.verified_commit_hash = self._verify_source_revision()
        self.asset_manifest_sha256 = _verify_native_asset_manifest(
            runtime.asset_manifest_path,
            runtime.source_root,
            expected_commit=self.verified_commit_hash,
        )
        (
            self.projection_manifest_sha256,
            self.projection_inputs,
        ) = _load_reviewed_projection_manifest(
            runtime.source_root,
            expected_commit=self.verified_commit_hash,
            expected_asset_manifest_sha256=self.asset_manifest_sha256,
        )
        self._sample_inference_dependency_verified = False
        self._training_telemetry_binding: tuple[str, str, NativeTrainingTelemetrySink] | None = None
        if sample_inference_dependency is not None:
            try:
                sample_inference_dependency.bind_native_runtime(
                    NativeSampleInferenceBinding(
                        rvc_commit_hash=self.verified_commit_hash,
                        asset_manifest_sha256=self.asset_manifest_sha256,
                        projection_manifest_sha256=self.projection_manifest_sha256,
                        python_executable=self.runtime.python_executable,
                        device=self.runtime.device,
                        use_half=self.runtime.use_half,
                    )
                )
            except Exception as exc:
                raise NativeRvcRunnerError(
                    "native sample inference dependency rejected the reviewed runtime"
                ) from exc
            self._sample_inference_dependency_verified = True
        self.assets_ready = True

    def bind_training_telemetry(
        self,
        claim: JobClaim | None,
        sink: NativeTrainingTelemetrySink | None,
    ) -> None:
        """Bind or clear the only live training sink for this single-job runner."""

        if claim is None or sink is None:
            if claim is not None or sink is not None:
                raise NativeRvcRunnerError("native telemetry unbind identity is incomplete")
            self._training_telemetry_binding = None
            return
        if self._training_telemetry_binding is not None:
            raise NativeRvcRunnerError("native training telemetry is already bound")
        self._training_telemetry_binding = (claim.job_id, claim.attempt_id, sink)

    @property
    def sample_inference_runtime_evidence(
        self,
    ) -> NativeSampleInferenceRuntimeEvidence | None:
        """Return the claim-time runtime pair only for a verified dependency."""

        dependency = self.sample_inference_dependency
        if dependency is None or not self._sample_inference_dependency_verified:
            return None
        digest = dependency.runtime_image_digest
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise NativeRvcRunnerError("native sample inference runtime image digest is invalid")
        return NativeSampleInferenceRuntimeEvidence(
            runtime_image_digest=digest,
            runtime_asset_manifest_sha256=self.asset_manifest_sha256,
        )

    def validate_claim(
        self,
        claim: JobClaim,
        available_gpu_ids: tuple[int, ...],
    ) -> None:
        """Revalidate immutable runtime and claim resources immediately before execution."""

        if self._verify_source_revision() != self.verified_commit_hash:
            raise NativeRvcRunnerError("RVC source revision changed before claim execution")
        current_manifest = _verify_native_asset_manifest(
            self.runtime.asset_manifest_path,
            self.runtime.source_root,
            expected_commit=self.verified_commit_hash,
        )
        if current_manifest != self.asset_manifest_sha256:
            raise NativeRvcRunnerError("RVC asset manifest changed after Worker startup")
        projection_sha256, projection_inputs = _load_reviewed_projection_manifest(
            self.runtime.source_root,
            expected_commit=self.verified_commit_hash,
            expected_asset_manifest_sha256=self.asset_manifest_sha256,
        )
        if (
            projection_sha256 != self.projection_manifest_sha256
            or projection_inputs != self.projection_inputs
        ):
            raise NativeRvcRunnerError("RVC projection manifest changed after Worker startup")
        backend = claim.config.rvc_backend
        if backend.repository != RVC_UPSTREAM_REPOSITORY:
            raise NativeRvcConfigurationError("Job requests an unreviewed RVC repository")
        requested_commit = backend.rvc_commit_hash
        if requested_commit is not None and requested_commit.lower() != self.verified_commit_hash:
            raise NativeRvcConfigurationError(
                "Job RVC commit does not match the reviewed Worker runtime"
            )
        if (
            claim.config.auto_inference_samples.enabled
            and not self._sample_inference_dependency_verified
        ):
            raise NativeRvcConfigurationError(
                "native sample inference requires an explicit reviewed fixed TestSet dependency"
            )
        try:
            validate_gpu_ids(claim.config.training.gpu_ids, available_gpu_ids)
            if claim.config.f0_extraction.training_f0_method is TrainingF0Method.RMVPE_GPU:
                validate_gpu_ids(
                    claim.config.f0_extraction.rmvpe_gpu_ids or [],
                    available_gpu_ids,
                )
        except RvcCommandError as exc:
            raise NativeRvcConfigurationError("Job requests a GPU that is not visible") from exc

    async def run_stage(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        if cancellation.is_set():
            raise asyncio.CancelledError
        if stage in {
            JobStatus.DOWNLOADING_DATASET,
            JobStatus.VALIDATING_DATASET,
            JobStatus.PREPARING_FLAT_DATASET,
        }:
            return await self._run_dependency(stage, context, cancellation)
        if stage is JobStatus.GENERATING_SAMPLES:
            await asyncio.to_thread(self._ensure_projection, context)
            return await self._run_sample_inference_dependency(stage, context, cancellation)
        if stage is JobStatus.EVALUATING:
            if context.claim.config.auto_inference_samples.enabled:
                await asyncio.to_thread(self._ensure_projection, context)
                return await self._run_sample_inference_dependency(stage, context, cancellation)
            result = self._write_no_sample_evaluation(context)
            return StageResult(
                _verify_created_paths(context, result.created_paths), result.metadata
            )

        await asyncio.to_thread(self._ensure_projection, context)
        handlers: Mapping[
            JobStatus,
            Callable[[RvcRunContext, asyncio.Event], Awaitable[StageResult]],
        ] = {
            JobStatus.PREPROCESSING: self._preprocess,
            JobStatus.EXTRACTING_F0: self._extract_f0,
            JobStatus.EXTRACTING_FEATURES: self._extract_features,
            JobStatus.TRAINING: self._train,
            JobStatus.SAVING_CHECKPOINT: self._save_checkpoint,
            JobStatus.BUILDING_INDEX: self._build_index,
            JobStatus.COLLECTING_SMALL_MODEL: self._collect_small_model,
            JobStatus.UPLOADING_ARTIFACTS: self._prepare_artifact_manifest,
        }
        handler = handlers.get(stage)
        if handler is None:
            raise NativeRvcRunnerError(f"typed RVC adapter does not implement {stage.value}")
        result = await handler(context, cancellation)
        verified = _verify_created_paths(context, result.created_paths)
        return StageResult(verified, result.metadata)

    async def _run_dependency(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        if self.stage_dependency is None:
            raise NativeRvcRunnerError(
                f"{stage.value} requires an explicit reviewed stage dependency"
            )
        result = await self.stage_dependency.run_stage(stage, context, cancellation)
        return StageResult(
            _verify_created_paths(context, result.created_paths),
            result.metadata,
        )

    async def _run_sample_inference_dependency(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        dependency = self.sample_inference_dependency
        if dependency is None or not self._sample_inference_dependency_verified:
            raise NativeRvcRunnerError(
                f"{stage.value} requires an explicit reviewed sample inference dependency"
            )
        result = await dependency.run_stage(stage, context, cancellation)
        return StageResult(
            _verify_created_paths(context, result.created_paths),
            result.metadata,
        )

    async def _preprocess(self, context: RvcRunContext, cancellation: asyncio.Event) -> StageResult:
        flat_dataset = context.workspace.inputs / "prepared_flat"
        _require_safe_directory(flat_dataset, context.workspace.root, "prepared flat dataset")
        if not _collect_regular_files(
            flat_dataset,
            context.workspace.root,
            suffixes={".wav", ".flac", ".mp3"},
        ):
            raise NativeRvcRunnerError("prepared flat dataset contains no supported audio")
        experiment = context.experiment_logs
        experiment.mkdir(parents=True, exist_ok=True, mode=0o700)
        command = build_preprocess_command(
            context.claim.config,
            self._cli_runtime(context),
            flat_dataset,
            experiment,
        )
        result = await self._run_command(
            context,
            JobStatus.PREPROCESSING,
            command,
            cancellation,
            timeout_seconds=self.runtime.preprocess_timeout_seconds,
        )
        ground_truth = _collect_regular_files(
            experiment / "0_gt_wavs", context.workspace.root, suffixes={".wav"}
        )
        wav16 = _collect_regular_files(
            experiment / "1_16k_wavs", context.workspace.root, suffixes={".wav"}
        )
        if not ground_truth or not wav16:
            raise NativeRvcRunnerError("RVC preprocessing did not create both WAV datasets")
        return StageResult(
            (*ground_truth, *wav16),
            _command_metadata((result,), command_count=1),
        )

    async def _extract_f0(self, context: RvcRunContext, cancellation: asyncio.Event) -> StageResult:
        commands = build_f0_extraction_commands(
            context.claim.config,
            self._cli_runtime(context),
            context.experiment_logs,
        )
        if not commands:
            raise NativeRvcRunnerError("F0 stage was requested for a non-F0 job")
        results = await self._run_parallel_commands(
            context,
            JobStatus.EXTRACTING_F0,
            commands,
            cancellation,
            timeout_seconds=self.runtime.extraction_timeout_seconds,
        )
        f0 = _collect_regular_files(
            context.experiment_logs / "2a_f0", context.workspace.root, suffixes={".npy"}
        )
        f0_nsf = _collect_regular_files(
            context.experiment_logs / "2b-f0nsf",
            context.workspace.root,
            suffixes={".npy"},
        )
        if not f0 or {path.name for path in f0} != {path.name for path in f0_nsf}:
            raise NativeRvcRunnerError("RVC F0 outputs are missing or do not form aligned pairs")
        return StageResult(
            (*f0, *f0_nsf),
            _command_metadata(results, command_count=len(commands)),
        )

    async def _extract_features(
        self, context: RvcRunContext, cancellation: asyncio.Event
    ) -> StageResult:
        commands = build_feature_extraction_commands(
            context.claim.config,
            self._cli_runtime(context),
            context.experiment_logs,
        )
        results = await self._run_parallel_commands(
            context,
            JobStatus.EXTRACTING_FEATURES,
            commands,
            cancellation,
            timeout_seconds=self.runtime.extraction_timeout_seconds,
        )
        features = _collect_regular_files(
            context.experiment_logs / context.claim.config.feature_directory,
            context.workspace.root,
            suffixes={".npy"},
        )
        if not features:
            raise NativeRvcRunnerError("RVC feature extraction produced no feature arrays")
        return StageResult(
            features,
            {
                **_command_metadata(results, command_count=len(commands)),
                "feature_dimension": (
                    256 if context.claim.config.model.version.value == "v1" else 768
                ),
            },
        )

    async def _train(self, context: RvcRunContext, cancellation: asyncio.Event) -> StageResult:
        if context.claim.config.pretrained.mode != "auto":
            raise NativeRvcRunnerError(
                "typed RVC adapter does not accept custom pretrained paths yet"
            )
        prepared = prepare_training_inputs(
            context.claim.config,
            context.rvc_root,
            context.experiment_logs,
            use_half=self.runtime.use_half,
        )
        pretrained = resolve_pretrained(
            context.rvc_root,
            context.claim.config.model.version,
            context.claim.config.model.sample_rate,
            context.claim.config.model.use_f0,
            require_files=True,
        )
        command = build_training_command(
            context.claim.config,
            self._cli_runtime(context),
            pretrained,
        )
        telemetry_sink = self._training_telemetry_sink(context)
        collector = _StreamingMetricCollector(
            self.runtime.max_metric_records,
            telemetry_sink=telemetry_sink,
        )
        observer = (
            _TrainingFileTelemetryObserver(
                context.experiment_logs,
                telemetry_sink,
                max_train_log_bytes=self.runtime.max_train_log_parse_bytes,
                max_metric_records=self.runtime.max_metric_records,
                poll_interval_seconds=self.runtime.telemetry_poll_interval_seconds,
            )
            if telemetry_sink is not None
            else None
        )
        result = await self._run_training_command(
            context,
            command,
            cancellation,
            collector=collector,
            observer=observer,
        )
        stdout_metrics = await collector.finish()
        if telemetry_sink is not None:
            await telemetry_sink.finish_training()
        train_log = context.experiment_logs / "train.log"
        log_metrics: tuple[ParsedTrainingMetric, ...] = ()
        log_metrics_dropped = 0
        train_log_skip_reason: str | None = None
        if train_log.is_file() and not train_log.is_symlink():
            log_metrics, log_metrics_dropped, train_log_skip_reason = _parse_training_log_bounded(
                train_log,
                max_bytes=self.runtime.max_train_log_parse_bytes,
                max_records=self.runtime.max_metric_records,
            )
        checkpoints = discover_checkpoints(context.experiment_logs)
        if not checkpoints:
            raise NativeRvcRunnerError("RVC training did not create any checkpoint")
        created: list[Path] = [
            prepared.filelist_path,
            prepared.config_path,
            *(item.path for item in checkpoints),
        ]
        if train_log.is_file():
            created.append(train_log)
        created.extend(self._small_model_candidates(context))
        combined_metrics = tuple((*stdout_metrics, *log_metrics))
        combined_overflow = max(0, len(combined_metrics) - self.runtime.max_metric_records)
        metrics = combined_metrics[: self.runtime.max_metric_records]
        return StageResult(
            tuple(dict.fromkeys(created)),
            {
                **_command_metadata((result,), command_count=1),
                "training_examples": prepared.training_example_count,
                "mute_examples": prepared.mute_example_count,
                "config_template": prepared.config_template,
                "metric_sources": {
                    "stdout": str(result.stdout_path.relative_to(context.workspace.root)),
                    "train_log": (
                        str(train_log.relative_to(context.workspace.root))
                        if train_log.is_file()
                        else None
                    ),
                },
                "metrics": [_metric_document(metric) for metric in metrics],
                "metric_records_dropped": (
                    collector.dropped_records + log_metrics_dropped + combined_overflow
                ),
                "train_log_metric_parse_skip_reason": train_log_skip_reason,
            },
        )

    async def _run_training_command(
        self,
        context: RvcRunContext,
        command: tuple[str, ...],
        cancellation: asyncio.Event,
        *,
        collector: _StreamingMetricCollector,
        observer: _TrainingFileTelemetryObserver | None,
    ) -> ProcessResult:
        if observer is None:
            return await self._run_command(
                context,
                JobStatus.TRAINING,
                command,
                cancellation,
                timeout_seconds=self.runtime.training_timeout_seconds,
                output_callback=collector.feed,
            )

        stage_cancellation = asyncio.Event()
        observer_stop = asyncio.Event()

        async def relay_external_cancellation() -> None:
            await cancellation.wait()
            stage_cancellation.set()

        relay = asyncio.create_task(
            relay_external_cancellation(),
            name=f"training-cancel-relay-{context.claim.attempt_id}",
        )
        process_task = asyncio.create_task(
            self._run_command(
                context,
                JobStatus.TRAINING,
                command,
                stage_cancellation,
                timeout_seconds=self.runtime.training_timeout_seconds,
                output_callback=collector.feed,
            ),
            name=f"native-training-{context.claim.attempt_id}",
        )
        observer_task = asyncio.create_task(
            observer.run(observer_stop, stage_cancellation),
            name=f"training-file-telemetry-{context.claim.attempt_id}",
        )
        try:
            while not process_task.done():
                done, _ = await asyncio.wait(
                    {process_task, observer_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if observer_task in done:
                    if observer_task.cancelled():
                        stage_cancellation.set()
                        await asyncio.gather(process_task, return_exceptions=True)
                        raise asyncio.CancelledError
                    observer_failure = observer_task.exception()
                    if observer_failure is not None:
                        stage_cancellation.set()
                        await asyncio.gather(process_task, return_exceptions=True)
                        if cancellation.is_set():
                            raise asyncio.CancelledError
                        raise observer_failure
                    # The observer exits normally only for cancellation/stop.
                    if not process_task.done():
                        stage_cancellation.set()
                if process_task in done:
                    break
            result = await process_task
            observer_stop.set()
            await observer_task
            return result
        finally:
            observer_stop.set()
            stage_cancellation.set()
            relay.cancel()
            await asyncio.gather(relay, return_exceptions=True)
            if not process_task.done():
                process_task.cancel()
            if not observer_task.done():
                observer_task.cancel()
            await asyncio.gather(process_task, observer_task, return_exceptions=True)

    async def _save_checkpoint(
        self, context: RvcRunContext, cancellation: asyncio.Event
    ) -> StageResult:
        if cancellation.is_set():
            raise asyncio.CancelledError
        pair = select_latest_checkpoint_pair(discover_checkpoints(context.experiment_logs))
        return StageResult(
            (pair.generator, pair.discriminator),
            {
                "epoch": pair.epoch,
                "generator_sha256": sha256_file(pair.generator),
                "discriminator_sha256": sha256_file(pair.discriminator),
            },
        )

    async def _build_index(
        self, context: RvcRunContext, cancellation: asyncio.Event
    ) -> StageResult:
        command = build_index_command(
            self.runtime.python_executable,
            context.experiment_logs,
            context.experiment_name,
            context.claim.config.model.version,
            seed=0,
            cpu_workers=self.runtime.cpu_workers,
        )
        result = await self._run_command(
            context,
            JobStatus.BUILDING_INDEX,
            command,
            cancellation,
            timeout_seconds=self.runtime.index_timeout_seconds,
        )
        total_features = context.experiment_logs / "total_fea.npy"
        indexes = find_index_candidates(context.experiment_logs)
        selected = select_final_index(indexes)
        created = _verify_created_paths(context, (total_features, selected))
        return StageResult(
            created,
            {
                **_command_metadata((result,), command_count=1),
                "seed": 0,
                "source_index_name": selected.name,
                "source_index_sha256": sha256_file(selected),
                "total_features_sha256": sha256_file(total_features),
            },
        )

    async def _collect_small_model(
        self, context: RvcRunContext, cancellation: asyncio.Event
    ) -> StageResult:
        destination = context.workspace.outputs / "model" / "final_small_model.pth"
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        candidates = self._small_model_candidates(context)
        extraction_metadata: dict[str, Any] = {"extracted_from_checkpoint": False}
        command_results: tuple[ProcessResult, ...] = ()
        if len(candidates) > 1:
            names = ", ".join(str(path.relative_to(context.rvc_root)) for path in candidates)
            raise NativeRvcRunnerError(f"multiple deployable small models are ambiguous: {names}")
        if candidates:
            await asyncio.to_thread(
                _copy_verified_file,
                candidates[0],
                destination,
                context.workspace.root,
            )
        else:
            if not context.claim.config.artifacts.extract_small_model_if_missing:
                raise NativeRvcRunnerError("deployable small model is missing")
            pair = select_latest_checkpoint_pair(discover_checkpoints(context.experiment_logs))
            command = build_small_model_command(
                self.runtime.python_executable,
                context.rvc_root,
                pair.generator,
                destination,
                context.experiment_name,
                context.claim.config.model.sample_rate,
                context.claim.config.model.use_f0,
                context.claim.config.model.version,
                info=(
                    f"RVC Orchestrator job {context.claim.job_id} "
                    f"attempt {context.claim.attempt_id}"
                ),
                allow_reviewed_projection=True,
            )
            command_result = await self._run_command(
                context,
                JobStatus.COLLECTING_SMALL_MODEL,
                command,
                cancellation,
                timeout_seconds=self.runtime.small_model_timeout_seconds,
            )
            command_results = (command_result,)
            extraction_metadata = {
                "extracted_from_checkpoint": True,
                "checkpoint_epoch": pair.epoch,
                "source_checkpoint_sha256": sha256_file(pair.generator),
            }
        created: list[Path] = [destination]
        metadata: dict[str, Any] = {
            **extraction_metadata,
            "small_model_sha256": sha256_file(destination),
            **_command_metadata(command_results, command_count=len(command_results)),
        }
        if context.claim.config.index.build_index:
            source_index = select_final_index(find_index_candidates(context.experiment_logs))
            final_index = context.workspace.outputs / "index" / "final.index"
            final_index.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            await asyncio.to_thread(
                _copy_verified_file,
                source_index,
                final_index,
                context.workspace.root,
            )
            created.append(final_index)
            metadata.update(
                {
                    "source_index_name": source_index.name,
                    "source_index_sha256": sha256_file(source_index),
                    "final_index_sha256": sha256_file(final_index),
                }
            )
        return StageResult(tuple(created), metadata)

    async def _prepare_artifact_manifest(
        self, context: RvcRunContext, cancellation: asyncio.Event
    ) -> StageResult:
        if cancellation.is_set():
            raise asyncio.CancelledError
        config_path = context.workspace.outputs / "config.json"
        environment_path = context.workspace.outputs / "environment.json"
        await asyncio.to_thread(
            _write_json_atomic,
            config_path,
            context.claim.config.model_dump(mode="json"),
            context.workspace.root,
        )
        projection_marker = context.rvc_root / _PROJECTION_MARKER
        environment: dict[str, Any] = {
            "schema_version": 1,
            "runner": "pinned_rvc_typed_adapter",
            "profile_stage_set_verified": False,
            "rvc_commit_hash": self.verified_commit_hash,
            "rvc_asset_manifest_sha256": self.asset_manifest_sha256,
            "rvc_projection_manifest_sha256": self.projection_manifest_sha256,
            "rvc_source_projection_manifest_sha256": sha256_file(projection_marker),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "job_id": context.claim.job_id,
            "attempt_id": context.claim.attempt_id,
            "model_version": context.claim.config.model.version.value,
            "sample_rate": context.claim.config.model.sample_rate.value,
            "use_f0": context.claim.config.model.use_f0,
        }
        await asyncio.to_thread(
            _write_json_atomic,
            environment_path,
            environment,
            context.workspace.root,
        )
        artifact_files = _manifest_artifact_files(context)
        manifest_path = context.workspace.outputs / "artifact_manifest.json"
        manifest = {
            "schema_version": 1,
            "job_id": context.claim.job_id,
            "attempt_id": context.claim.attempt_id,
            "rvc_commit_hash": self.verified_commit_hash,
            "files": [
                {
                    "path": path.relative_to(context.workspace.root).as_posix(),
                    "size_bytes": path.stat(follow_symlinks=False).st_size,
                    "sha256": sha256_file(path),
                }
                for path in artifact_files
            ],
        }
        await asyncio.to_thread(
            _write_json_atomic,
            manifest_path,
            manifest,
            context.workspace.root,
        )
        return StageResult(
            (config_path, environment_path, manifest_path),
            {
                "manifest_sha256": sha256_file(manifest_path),
                "manifest_file_count": len(artifact_files),
            },
        )

    async def _run_command(
        self,
        context: RvcRunContext,
        stage: JobStatus,
        argv: tuple[str, ...],
        cancellation: asyncio.Event,
        *,
        timeout_seconds: float,
        shard: int | None = None,
        output_callback: OutputCallback | None = None,
    ) -> ProcessResult:
        suffix = f".{shard}" if shard is not None else ""
        log_root = context.workspace.logs / "rvc-subprocess"
        environment = _process_environment(context)
        return await self.process_runner.run(
            ProcessSpec(
                argv=argv,
                cwd=context.rvc_root,
                workspace_root=context.workspace.root,
                stdout_path=log_root / f"{stage.value}{suffix}.stdout.log",
                stderr_path=log_root / f"{stage.value}{suffix}.stderr.log",
                env=environment,
                timeout_seconds=timeout_seconds,
            ),
            cancellation,
            output_callback=output_callback,
        )

    async def _run_parallel_commands(
        self,
        context: RvcRunContext,
        stage: JobStatus,
        commands: Sequence[tuple[str, ...]],
        cancellation: asyncio.Event,
        *,
        timeout_seconds: float,
    ) -> tuple[ProcessResult, ...]:
        if not commands:
            return ()
        stage_cancellation = asyncio.Event()

        async def relay_external_cancellation() -> None:
            await cancellation.wait()
            stage_cancellation.set()

        relay = asyncio.create_task(relay_external_cancellation())
        tasks = [
            asyncio.create_task(
                self._run_command(
                    context,
                    stage,
                    command,
                    stage_cancellation,
                    timeout_seconds=timeout_seconds,
                    shard=index,
                )
            )
            for index, command in enumerate(commands)
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            failure: BaseException | None = None
            for task in done:
                if task.cancelled():
                    failure = asyncio.CancelledError()
                    break
                exception = task.exception()
                if exception is not None:
                    failure = exception
                    break
            if failure is not None:
                stage_cancellation.set()
                await asyncio.gather(*pending, return_exceptions=True)
                raise failure
            await asyncio.gather(*pending)
            return tuple(task.result() for task in tasks)
        finally:
            stage_cancellation.set()
            relay.cancel()
            await asyncio.gather(relay, return_exceptions=True)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _small_model_candidates(self, context: RvcRunContext) -> tuple[Path, ...]:
        possible = (
            context.rvc_root / "weights" / f"{context.experiment_name}.pth",
            context.rvc_root / "assets" / "weights" / f"{context.experiment_name}.pth",
        )
        return tuple(
            path for path in possible if _is_nonempty_regular_file(path) and not path.is_symlink()
        )

    def _training_telemetry_sink(
        self,
        context: RvcRunContext,
    ) -> NativeTrainingTelemetrySink | None:
        binding = self._training_telemetry_binding
        if binding is None:
            return None
        job_id, attempt_id, sink = binding
        if job_id != context.claim.job_id or attempt_id != context.claim.attempt_id:
            raise NativeRvcRunnerError(
                "native training telemetry binding does not match the current claim"
            )
        return sink

    def _cli_runtime(self, context: RvcRunContext) -> RvcCliRuntime:
        return RvcCliRuntime(
            python_executable=self.runtime.python_executable,
            repository_root=context.rvc_root,
            cpu_workers=self.runtime.cpu_workers,
            device=self.runtime.device,
            use_half=self.runtime.use_half,
            available_gpu_ids=self.runtime.available_gpu_ids,
        )

    def _write_no_sample_evaluation(self, context: RvcRunContext) -> StageResult:
        report = context.workspace.outputs / "metrics" / "evaluation.json"
        _write_json_atomic(
            report,
            {
                "schema_version": 1,
                "sample_evaluation_performed": False,
                "reason": "auto inference samples disabled",
            },
            context.workspace.root,
        )
        return StageResult((report,))

    def _verify_source_revision(self) -> str:
        _require_safe_directory(self.runtime.source_root, self.runtime.source_root, "RVC source")
        try:
            revision = self._revision_reader(self.runtime.source_root).strip().lower()
        except (OSError, subprocess.SubprocessError) as exc:
            raise NativeRvcRunnerError("cannot verify reviewed RVC source revision") from exc
        if revision != self.runtime.expected_commit:
            raise NativeRvcRunnerError("RVC source revision does not match reviewed commit")
        return revision

    def _ensure_projection(self, context: RvcRunContext) -> None:
        context.workspace.assert_path(context.rvc_root)
        if self._verify_source_revision() != self.verified_commit_hash:
            raise NativeRvcRunnerError("RVC source revision changed before stage execution")
        if context.rvc_root.exists() or context.rvc_root.is_symlink():
            _verify_projection(
                context.rvc_root,
                self.verified_commit_hash,
                self.projection_inputs,
            )
            return
        _create_projection(
            self.runtime.source_root,
            context.rvc_root,
            context.workspace.root,
            self.verified_commit_hash,
            self.projection_inputs,
        )
        _verify_projection(
            context.rvc_root,
            self.verified_commit_hash,
            self.projection_inputs,
        )


def select_latest_checkpoint_pair(checkpoints: Sequence[Checkpoint]) -> CheckpointPair:
    """Select the newest complete G/D epoch, rejecting divergent newest checkpoints."""

    generators = {item.epoch: item.path for item in checkpoints if item.kind == "G"}
    discriminators = {item.epoch: item.path for item in checkpoints if item.kind == "D"}
    if not generators or not discriminators:
        raise ArtifactDiscoveryError("both generator and discriminator checkpoints are required")
    newest_generator = max(generators)
    newest_discriminator = max(discriminators)
    if newest_generator != newest_discriminator:
        raise ArtifactDiscoveryError(
            "newest generator/discriminator checkpoints do not form an unambiguous pair"
        )
    generator = generators[newest_generator]
    discriminator = discriminators[newest_generator]
    if not _is_nonempty_regular_file(generator) or not _is_nonempty_regular_file(discriminator):
        raise ArtifactDiscoveryError("checkpoint pair contains an empty or unsafe file")
    return CheckpointPair(newest_generator, generator, discriminator)


class _StreamingMetricCollector:
    def __init__(
        self,
        max_records: int,
        *,
        telemetry_sink: NativeTrainingTelemetrySink | None = None,
    ) -> None:
        self._parser = TrainingLogParser()
        self._buffers = {"stdout": b"", "stderr": b""}
        self._line_indexes = {"stdout": 0, "stderr": 0}
        self._metrics: list[ParsedTrainingMetric] = []
        self._max_records = max_records
        self._telemetry_sink = telemetry_sink
        self.dropped_records = 0

    async def feed(self, channel: str, chunk: bytes) -> None:
        if channel not in self._buffers:
            raise NativeRvcRunnerError("native subprocess emitted an unknown output channel")
        buffer = self._buffers[channel] + chunk
        lines = buffer.split(b"\n")
        self._buffers[channel] = lines.pop()
        for line in lines:
            await self._feed_line(channel, line.rstrip(b"\r"))
        if len(self._buffers[channel]) > _MAX_METRIC_LINE_BYTES:
            oversized = self._buffers[channel]
            self._buffers[channel] = b""
            await self._feed_line(channel, oversized)

    async def finish(self) -> tuple[ParsedTrainingMetric, ...]:
        for channel in ("stdout", "stderr"):
            if self._buffers[channel]:
                await self._feed_line(channel, self._buffers[channel])
                self._buffers[channel] = b""
        return tuple(self._metrics)

    async def _feed_line(self, channel: str, value: bytes) -> None:
        if len(value) > _MAX_METRIC_LINE_BYTES:
            self.dropped_records += 1
            line_index = self._line_indexes[channel]
            self._line_indexes[channel] += 1
            if self._telemetry_sink is not None:
                await self._telemetry_sink.record_training_event(
                    source="stdout",
                    event_key=(
                        f"{channel}:{line_index}:oversized:{hashlib.sha256(value).hexdigest()}"
                    ),
                    message="worker subprocess output line exceeded the byte limit",
                    channel=channel,
                )
            return
        line = value.decode("utf-8", errors="replace")
        parsed = self._parser.feed(line) if channel == "stdout" else ()
        accepted: list[ParsedTrainingMetric] = []
        for metric in parsed:
            if len(self._metrics) >= self._max_records:
                self.dropped_records += 1
                continue
            normalized = replace(metric, source="stdout")
            self._metrics.append(normalized)
            accepted.append(normalized)
        line_index = self._line_indexes[channel]
        self._line_indexes[channel] += 1
        if self._telemetry_sink is not None and line:
            await self._telemetry_sink.record_training_event(
                source="stdout",
                event_key=(f"{channel}:{line_index}:{hashlib.sha256(value).hexdigest()}"),
                message=line,
                metrics=tuple(accepted),
                channel=channel,
            )


@dataclass(frozen=True, slots=True)
class _ObservedTrainingEvent:
    source: str
    event_key: str
    message: str | None
    metrics: tuple[ParsedTrainingMetric, ...]
    channel: str | None = None


class _TrainingFileTelemetryObserver:
    """Bounded incremental train.log tail and TensorBoard scalar poller."""

    def __init__(
        self,
        experiment_logs: Path,
        sink: NativeTrainingTelemetrySink,
        *,
        max_train_log_bytes: int,
        max_metric_records: int,
        poll_interval_seconds: float,
    ) -> None:
        self._experiment_logs = experiment_logs
        self._sink = sink
        self._max_train_log_bytes = max_train_log_bytes
        self._max_metric_records = max_metric_records
        self._poll_interval_seconds = poll_interval_seconds
        self._train_log_offset = 0
        self._train_log_buffer = b""
        self._train_log_buffer_offset = 0
        self._train_log_parser = TrainingLogParser()
        self._train_log_metric_count = 0
        self._tensorboard_event_keys: set[str] = set()

    async def run(
        self,
        stop: asyncio.Event,
        cancellation: asyncio.Event,
    ) -> None:
        while True:
            if cancellation.is_set():
                return
            final = stop.is_set()
            events = await asyncio.to_thread(self._poll_sync, final)
            for event in events:
                if cancellation.is_set():
                    return
                await self._sink.record_training_event(
                    source=event.source,
                    event_key=event.event_key,
                    message=event.message,
                    metrics=event.metrics,
                    channel=event.channel,
                )
            if final:
                return
            await _wait_for_training_observer(
                stop,
                cancellation,
                self._poll_interval_seconds,
            )

    def _poll_sync(self, final: bool) -> tuple[_ObservedTrainingEvent, ...]:
        events = list(self._read_train_log_events(final=final))
        events.extend(self._read_tensorboard_events())
        return tuple(events)

    def _read_train_log_events(
        self,
        *,
        final: bool,
    ) -> tuple[_ObservedTrainingEvent, ...]:
        path = self._experiment_logs / "train.log"
        try:
            path_stat = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise NativeRvcRunnerError("cannot inspect live RVC train.log") from exc
        if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
            raise NativeRvcRunnerError("live RVC train.log is unsafe")
        if path_stat.st_size > self._max_train_log_bytes:
            raise NativeRvcRunnerError("live RVC train.log exceeds its byte limit")
        if path_stat.st_size < self._train_log_offset:
            raise NativeRvcRunnerError("live RVC train.log was truncated during training")

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise NativeRvcRunnerError("cannot open live RVC train.log") from exc
        try:
            initial = os.fstat(descriptor)
            if (
                not stat.S_ISREG(initial.st_mode)
                or initial.st_ino != path_stat.st_ino
                or initial.st_dev != path_stat.st_dev
                or initial.st_size < self._train_log_offset
            ):
                raise NativeRvcRunnerError("live RVC train.log identity changed")
            os.lseek(descriptor, self._train_log_offset, os.SEEK_SET)
            remaining = initial.st_size - self._train_log_offset
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise NativeRvcRunnerError("live RVC train.log changed while reading")
                chunks.append(chunk)
                remaining -= len(chunk)
            final_stat = os.fstat(descriptor)
            if (
                final_stat.st_ino != initial.st_ino
                or final_stat.st_dev != initial.st_dev
                or final_stat.st_size < initial.st_size
            ):
                raise NativeRvcRunnerError("live RVC train.log changed while reading")
        finally:
            os.close(descriptor)

        new_bytes = b"".join(chunks)
        read_start = self._train_log_offset
        self._train_log_offset = initial.st_size
        data = self._train_log_buffer + new_bytes
        data_start = self._train_log_buffer_offset if self._train_log_buffer else read_start
        events: list[_ObservedTrainingEvent] = []
        cursor = 0
        while True:
            newline = data.find(b"\n", cursor)
            if newline < 0:
                break
            raw = data[cursor:newline].rstrip(b"\r")
            events.extend(self._train_log_line(data_start + cursor, raw))
            cursor = newline + 1
        self._train_log_buffer = data[cursor:]
        self._train_log_buffer_offset = data_start + cursor
        if final and self._train_log_buffer:
            events.extend(
                self._train_log_line(
                    self._train_log_buffer_offset,
                    self._train_log_buffer,
                )
            )
            self._train_log_buffer = b""
            self._train_log_buffer_offset = self._train_log_offset
        return tuple(events)

    def _train_log_line(
        self,
        byte_offset: int,
        raw: bytes,
    ) -> tuple[_ObservedTrainingEvent, ...]:
        if len(raw) > _MAX_METRIC_LINE_BYTES:
            return (
                _ObservedTrainingEvent(
                    source="train_log",
                    event_key=(f"{byte_offset}:oversized:{hashlib.sha256(raw).hexdigest()}"),
                    message="RVC train.log line exceeded the byte limit",
                    metrics=(),
                    channel="train_log",
                ),
            )
        line = raw.decode("utf-8", errors="replace")
        if not line:
            return ()
        parsed_metrics = tuple(
            replace(metric, source="train_log") for metric in self._train_log_parser.feed(line)
        )
        available = max(0, self._max_metric_records - self._train_log_metric_count)
        metrics = parsed_metrics[:available]
        self._train_log_metric_count += len(metrics)
        return (
            _ObservedTrainingEvent(
                source="train_log",
                event_key=(f"{byte_offset}:{hashlib.sha256(raw).hexdigest()}"),
                message=line,
                metrics=metrics,
                channel="train_log",
            ),
        )

    def _read_tensorboard_events(self) -> tuple[_ObservedTrainingEvent, ...]:
        try:
            candidates = tuple(sorted(self._experiment_logs.glob("events.out.tfevents.*")))
        except OSError as exc:
            raise NativeRvcRunnerError("cannot inspect live TensorBoard events") from exc
        if not candidates:
            return ()
        if len(candidates) > _MAX_TENSORBOARD_FILES:
            raise NativeRvcRunnerError("live TensorBoard event file limit was exceeded")
        identities: dict[Path, tuple[int, int, int]] = {}
        total_bytes = 0
        for path in candidates:
            try:
                path_stat = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise NativeRvcRunnerError("cannot inspect live TensorBoard event") from exc
            if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
                raise NativeRvcRunnerError("live TensorBoard event is unsafe")
            if path_stat.st_size > _MAX_TENSORBOARD_FILE_BYTES:
                raise NativeRvcRunnerError("live TensorBoard event exceeds its byte limit")
            total_bytes += path_stat.st_size
            if total_bytes > _MAX_TENSORBOARD_TOTAL_BYTES:
                raise NativeRvcRunnerError("live TensorBoard events exceed their byte limit")
            identities[path] = (path_stat.st_dev, path_stat.st_ino, path_stat.st_size)
        metrics = read_tensorboard_scalars(
            self._experiment_logs,
            after_step=-1,
            max_records=self._max_metric_records,
        )
        try:
            final_candidates = tuple(sorted(self._experiment_logs.glob("events.out.tfevents.*")))
        except OSError as exc:
            raise NativeRvcRunnerError("cannot recheck live TensorBoard events") from exc
        if final_candidates != candidates:
            # Event-file rotation is normal.  Emit nothing from the ambiguous
            # snapshot and retry the complete, newly stable inventory next poll.
            return ()
        final_total_bytes = 0
        for path, (device, inode, initial_size) in identities.items():
            try:
                final_stat = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise NativeRvcRunnerError("live TensorBoard event changed while reading") from exc
            if (
                path.is_symlink()
                or not stat.S_ISREG(final_stat.st_mode)
                or final_stat.st_dev != device
                or final_stat.st_ino != inode
                or final_stat.st_size < initial_size
                or final_stat.st_size > _MAX_TENSORBOARD_FILE_BYTES
            ):
                raise NativeRvcRunnerError("live TensorBoard event changed while reading")
            final_total_bytes += final_stat.st_size
        if final_total_bytes > _MAX_TENSORBOARD_TOTAL_BYTES:
            raise NativeRvcRunnerError("live TensorBoard events exceed their byte limit")
        events: list[_ObservedTrainingEvent] = []
        for metric in metrics:
            event_key = f"{metric.key}:{metric.step}:{metric.epoch}:{metric.value.hex()}"
            if event_key in self._tensorboard_event_keys:
                continue
            if len(self._tensorboard_event_keys) >= self._max_metric_records:
                raise NativeRvcRunnerError("live TensorBoard metric limit was exceeded")
            self._tensorboard_event_keys.add(event_key)
            events.append(
                _ObservedTrainingEvent(
                    source="tensorboard",
                    event_key=event_key,
                    message=None,
                    metrics=(metric,),
                )
            )
        return tuple(events)


async def _wait_for_training_observer(
    stop: asyncio.Event,
    cancellation: asyncio.Event,
    delay_seconds: float,
) -> None:
    tasks = (
        asyncio.create_task(stop.wait()),
        asyncio.create_task(cancellation.wait()),
    )
    try:
        await asyncio.wait(
            tasks,
            timeout=delay_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _parse_training_log_bounded(
    path: Path, *, max_bytes: int, max_records: int
) -> tuple[tuple[ParsedTrainingMetric, ...], int, str | None]:
    try:
        size = path.stat(follow_symlinks=False).st_size
    except OSError as exc:
        raise NativeRvcRunnerError("cannot inspect RVC train.log") from exc
    if size > max_bytes:
        return (), 0, "train_log_size_limit"
    parser = TrainingLogParser()
    metrics: list[ParsedTrainingMetric] = []
    dropped = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                for metric in parser.feed(line):
                    if len(metrics) >= max_records:
                        dropped += 1
                    else:
                        metrics.append(metric)
    except OSError as exc:
        raise NativeRvcRunnerError("cannot read RVC train.log") from exc
    return tuple(metrics), dropped, None


def _metric_document(metric: ParsedTrainingMetric) -> dict[str, Any]:
    return asdict(metric)


def _command_metadata(results: Sequence[ProcessResult], *, command_count: int) -> dict[str, Any]:
    return {
        "command_count": command_count,
        "subprocess_logs": [
            {
                "stdout": str(result.stdout_path),
                "stderr": str(result.stderr_path),
                "returncode": result.returncode,
            }
            for result in results
        ],
    }


def _process_environment(context: RvcRunContext) -> dict[str, str]:
    home = context.workspace.work / "home"
    temporary = context.workspace.work / "tmp"
    matplotlib = context.workspace.work / "matplotlib"
    for directory in (home, temporary, matplotlib):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        ensure_within(directory, context.workspace.root)
    return {
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "MPLCONFIGDIR": str(matplotlib),
        "PYTHONPATH": str(context.rvc_root),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _read_reviewed_revision(source_root: Path) -> str:
    marker = source_root / _REVISION_MARKER
    if marker.exists() or marker.is_symlink():
        if (
            not marker.is_file()
            or marker.is_symlink()
            or marker.stat(follow_symlinks=False).st_size > 128
        ):
            raise NativeRvcRunnerError("reviewed RVC revision marker is unsafe")
        try:
            revision = marker.read_text(encoding="ascii").strip().lower()
        except (OSError, UnicodeError) as exc:
            raise NativeRvcRunnerError("cannot read reviewed RVC revision marker") from exc
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise NativeRvcRunnerError("reviewed RVC revision marker is invalid")
        return revision
    return _read_git_revision(source_root)


def _read_git_revision(source_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeRvcRunnerError("cannot execute RVC revision verifier") from exc
    revision = result.stdout.strip().lower()
    if result.returncode != 0:
        raise NativeRvcRunnerError("RVC revision verifier rejected the source")
    return revision


def _verify_native_asset_manifest(
    manifest_path: Path,
    source_root: Path,
    *,
    expected_commit: str,
) -> str:
    """Verify the strict offline asset manifest and every referenced byte."""

    _require_safe_directory(source_root, source_root, "RVC source")
    ensure_within(manifest_path, source_root)
    if (
        not manifest_path.is_file()
        or manifest_path.is_symlink()
        or manifest_path.stat(follow_symlinks=False).st_size > _ASSET_MANIFEST_MAX_BYTES
    ):
        raise NativeRvcRunnerError("native RVC asset manifest is missing or unsafe")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise NativeRvcRunnerError(f"duplicate RVC asset manifest key: {key}")
            document[key] = value
        return document

    try:
        document = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NativeRvcRunnerError("native RVC asset manifest is invalid") from exc
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "kind",
        "rvc_commit",
        "assets",
    }:
        raise NativeRvcRunnerError("native RVC asset manifest fields are invalid")
    if (
        document["schema_version"] != 1
        or document["kind"] != "rvc-assets"
        or document["rvc_commit"] != expected_commit
    ):
        raise NativeRvcRunnerError("native RVC asset manifest targets an unreviewed runtime")
    records = document["assets"]
    if not isinstance(records, list) or not records or len(records) > _ASSET_MANIFEST_MAX_RECORDS:
        raise NativeRvcRunnerError("native RVC asset manifest record count is invalid")

    discovered: set[str] = set()
    for index, raw in enumerate(records):
        if not isinstance(raw, dict) or set(raw) != {
            "path",
            "sha256",
            "size",
            "license",
            "source",
            "executable",
        }:
            raise NativeRvcRunnerError(f"native RVC asset[{index}] fields are invalid")
        relative_value = raw["path"]
        if not isinstance(relative_value, str) or not relative_value or "\\" in relative_value:
            raise NativeRvcRunnerError(f"native RVC asset[{index}] path is invalid")
        relative = PurePosixPath(relative_value)
        if (
            relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.as_posix() != relative_value
            or relative_value in discovered
        ):
            raise NativeRvcRunnerError(f"native RVC asset[{index}] path is unsafe")
        digest = raw["sha256"]
        size = raw["size"]
        executable = raw["executable"]
        license_name = raw["license"]
        source = raw["source"]
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise NativeRvcRunnerError(f"native RVC asset[{index}] SHA-256 is invalid")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise NativeRvcRunnerError(f"native RVC asset[{index}] size is invalid")
        if not isinstance(executable, bool):
            raise NativeRvcRunnerError(f"native RVC asset[{index}] mode is invalid")
        if (
            not isinstance(license_name, str)
            or not license_name
            or not isinstance(source, str)
            or not source.startswith("https://")
            or any(character.isspace() for character in source)
        ):
            raise NativeRvcRunnerError(f"native RVC asset[{index}] provenance is invalid")
        path = source_root.joinpath(*relative.parts)
        ensure_within(path, source_root)
        if not _is_nonempty_regular_file(path) or path.is_symlink():
            raise NativeRvcRunnerError(f"native RVC asset is missing or unsafe: {relative_value}")
        file_stat = path.stat(follow_symlinks=False)
        if file_stat.st_size != size or sha256_file(path) != digest:
            raise NativeRvcRunnerError(f"native RVC asset checksum mismatch: {relative_value}")
        has_execute_bit = bool(file_stat.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        if has_execute_bit != executable:
            raise NativeRvcRunnerError(f"native RVC asset mode mismatch: {relative_value}")
        discovered.add(relative_value)

    missing = _REQUIRED_NATIVE_ASSETS - discovered
    if missing:
        raise NativeRvcRunnerError(f"native RVC required asset is missing: {sorted(missing)[0]}")
    return sha256_file(manifest_path)


def _load_reviewed_projection_manifest(
    source_root: Path,
    *,
    expected_commit: str,
    expected_asset_manifest_sha256: str,
) -> tuple[str, tuple[ProjectionInputRecord, ...]]:
    source_manifest = source_root / "source-manifest.json"
    projection_manifest = source_root / "projection-manifest.json"
    projection_lock = source_root / "projection-manifest.sha256"
    for path, label, maximum in (
        (source_manifest, "source manifest", 16 * 1024**2),
        (projection_manifest, "projection manifest", 64 * 1024**2),
        (projection_lock, "projection manifest lock", 128),
    ):
        ensure_within(path, source_root)
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat(follow_symlinks=False).st_size > maximum
        ):
            raise NativeRvcRunnerError(f"native RVC {label} is missing or unsafe")

    try:
        locked_sha256 = projection_lock.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise NativeRvcRunnerError("cannot read native RVC projection manifest lock") from exc
    manifest_sha256 = sha256_file(projection_manifest)
    if _SHA256.fullmatch(locked_sha256) is None or locked_sha256 != manifest_sha256:
        raise NativeRvcRunnerError("native RVC projection manifest hash is not locked")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise NativeRvcRunnerError(f"duplicate projection manifest key: {key}")
            result[key] = value
        return result

    try:
        source_document = json.loads(
            source_manifest.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )
        projection_document = json.loads(
            projection_manifest.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NativeRvcRunnerError("native RVC projection provenance is invalid") from exc
    if (
        not isinstance(source_document, dict)
        or set(source_document)
        != {"schema_version", "kind", "repository", "commit", "archive", "license"}
        or source_document.get("schema_version") != 1
        or source_document.get("kind") != "rvc-source"
        or source_document.get("repository")
        != "https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI"
        or source_document.get("commit") != expected_commit
    ):
        raise NativeRvcRunnerError("native RVC source manifest is not reviewed")
    if (
        not isinstance(projection_document, dict)
        or set(projection_document)
        != {
            "schema_version",
            "kind",
            "rvc_commit",
            "source_manifest_sha256",
            "asset_manifest_sha256",
            "files",
        }
        or projection_document.get("schema_version") != 1
        or projection_document.get("kind") != "rvc-projection-inputs"
        or projection_document.get("rvc_commit") != expected_commit
        or projection_document.get("source_manifest_sha256") != sha256_file(source_manifest)
        or projection_document.get("asset_manifest_sha256") != expected_asset_manifest_sha256
    ):
        raise NativeRvcRunnerError("native RVC projection manifest provenance does not match")
    raw_files = projection_document.get("files")
    if not isinstance(raw_files, list) or not raw_files or len(raw_files) > _MAX_PROJECTION_FILES:
        raise NativeRvcRunnerError("native RVC projection file inventory is invalid")

    records: list[ProjectionInputRecord] = []
    seen: set[str] = set()
    total_bytes = 0
    allowed_roots = tuple(
        path.as_posix() + "/"
        for path in (*_PROJECTION_DIRECTORIES, *_OPTIONAL_PROJECTION_DIRECTORIES)
    )
    for index, raw in enumerate(raw_files):
        if not isinstance(raw, dict) or set(raw) != {"path", "size", "sha256", "mode"}:
            raise NativeRvcRunnerError(f"native RVC projection file[{index}] is invalid")
        path_value = raw["path"]
        size = raw["size"]
        digest = raw["sha256"]
        mode = raw["mode"]
        if not isinstance(path_value, str) or not path_value or "\\" in path_value:
            raise NativeRvcRunnerError("native RVC projection path is invalid")
        pure = PurePosixPath(path_value)
        if (
            pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
            or pure.as_posix() != path_value
            or path_value in seen
            or not path_value.startswith(allowed_roots)
            or Path(path_value).suffix.lower() not in _ALLOWED_FILE_SUFFIXES
        ):
            raise NativeRvcRunnerError("native RVC projection path is unsafe")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise NativeRvcRunnerError("native RVC projection size is invalid")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise NativeRvcRunnerError("native RVC projection checksum is invalid")
        if not isinstance(mode, int) or isinstance(mode, bool) or not 0 <= mode <= 0o7777:
            raise NativeRvcRunnerError("native RVC projection mode is invalid")
        total_bytes += size
        if total_bytes > _MAX_PROJECTION_BYTES:
            raise NativeRvcRunnerError("native RVC projection exceeds the byte-size limit")
        record = ProjectionInputRecord(Path(*pure.parts), size, digest, mode)
        _verify_projection_source_record(source_root / record.path, record)
        records.append(record)
        seen.add(path_value)

    required = {path.as_posix() for path in _REQUIRED_PROJECTED_FILES}
    if not required.issubset(seen):
        raise NativeRvcRunnerError(
            f"required projected RVC input is missing: {sorted(required - seen)[0]}"
        )
    crepe_paths = {path for path in seen if path.startswith("runtime/crepe/")}
    if crepe_paths not in (set(), {_CREPE_MODEL_PATH.as_posix()}):
        raise NativeRvcRunnerError("native CREPE projection inventory is not exact")
    discovered: set[str] = set()
    for relative_root in _PROJECTION_DIRECTORIES:
        directory = source_root / relative_root
        _require_safe_directory(directory, source_root, str(relative_root))
        for path in _walk_regular_source_files(directory, source_root):
            if path.suffix.lower() in _ALLOWED_FILE_SUFFIXES:
                discovered.add(path.relative_to(source_root).as_posix())
    for relative_root in _OPTIONAL_PROJECTION_DIRECTORIES:
        directory = source_root / relative_root
        try:
            directory.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise NativeRvcRunnerError(
                "cannot inspect optional native projection directory"
            ) from exc
        _require_safe_directory(directory, source_root, str(relative_root))
        for path in _walk_regular_source_files(directory, source_root):
            discovered.add(path.relative_to(source_root).as_posix())
    if discovered != seen:
        raise NativeRvcRunnerError("native RVC projection inventory does not match source files")
    return manifest_sha256, tuple(records)


def _verify_projection_source_record(path: Path, record: ProjectionInputRecord) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise NativeRvcRunnerError("cannot open reviewed RVC projection input") from exc
    try:
        initial = os.fstat(descriptor)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_size != record.size
            or stat.S_IMODE(initial.st_mode) != record.mode
        ):
            raise NativeRvcRunnerError("reviewed RVC projection input metadata changed")
        digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            digest.update(chunk)
        final = os.fstat(descriptor)
        if (
            copied != record.size
            or final.st_size != record.size
            or stat.S_IMODE(final.st_mode) != record.mode
            or digest.hexdigest() != record.sha256
        ):
            raise NativeRvcRunnerError("reviewed RVC projection input bytes changed")
    finally:
        os.close(descriptor)


def _create_projection(
    source_root: Path,
    destination_root: Path,
    workspace_root: Path,
    commit: str,
    expected_inputs: tuple[ProjectionInputRecord, ...],
) -> None:
    ensure_within(destination_root, workspace_root)
    temporary = destination_root.parent / f".rvc-projection-{uuid4().hex}"
    ensure_within(temporary, workspace_root)
    temporary.mkdir(mode=0o700)
    records: list[dict[str, Any]] = []
    total_bytes = 0
    try:
        for expected in expected_inputs:
            source = source_root / expected.path
            destination = temporary / expected.path
            size, digest = _copy_projection_file(source, destination, expected)
            total_bytes += size
            if len(records) + 1 > _MAX_PROJECTION_FILES:
                raise NativeRvcRunnerError("RVC projection exceeds the file-count limit")
            if total_bytes > _MAX_PROJECTION_BYTES:
                raise NativeRvcRunnerError("RVC projection exceeds the byte-size limit")
            records.append(
                {
                    "path": expected.path.as_posix(),
                    "size_bytes": size,
                    "sha256": digest,
                }
            )

        revision_marker = temporary / _REVISION_MARKER
        revision_marker.write_text(commit + "\n", encoding="ascii")
        revision_marker.chmod(0o444)
        records.append(
            {
                "path": _REVISION_MARKER.as_posix(),
                "size_bytes": revision_marker.stat().st_size,
                "sha256": sha256_file(revision_marker),
            }
        )
        records.sort(key=lambda item: str(item["path"]))
        marker = temporary / _PROJECTION_MARKER
        marker.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "rvc_commit_hash": commit,
                    "projection_directories": [
                        path.as_posix() for path in _active_projection_directories(expected_inputs)
                    ],
                    "files": records,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        marker.chmod(0o444)
        for required in _REQUIRED_PROJECTED_FILES:
            if not _is_nonempty_regular_file(temporary / required):
                raise NativeRvcRunnerError(f"required projected RVC input is missing: {required}")

        # Only these locations may receive upstream output.  All source inputs stay
        # read-only and no path points back to the shared checkout.
        (temporary / "logs").chmod(0o700)
        (temporary / "assets").chmod(0o700)
        (temporary / "logs" / "mute").chmod(0o555)
        (temporary / "assets" / "weights").mkdir(mode=0o700)
        (temporary / "weights").mkdir(mode=0o700)
        temporary.chmod(0o700)
        os.replace(temporary, destination_root)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _walk_regular_source_files(directory: Path, source_root: Path) -> tuple[Path, ...]:
    found: list[Path] = []
    for root, directories, filenames in os.walk(directory, followlinks=False):
        root_path = Path(root)
        ensure_within(root_path, source_root)
        for name in tuple(directories):
            child = root_path / name
            if child.is_symlink():
                raise NativeRvcRunnerError(
                    f"RVC projection refuses a source-directory symlink: {child.name}"
                )
            try:
                mode = child.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise NativeRvcRunnerError("cannot inspect RVC source directory") from exc
            if not stat.S_ISDIR(mode):
                raise NativeRvcRunnerError("RVC source tree contains a special directory entry")
        for name in filenames:
            child = root_path / name
            try:
                mode = child.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise NativeRvcRunnerError("cannot inspect RVC source file") from exc
            if child.is_symlink() or not stat.S_ISREG(mode):
                raise NativeRvcRunnerError(
                    f"RVC projection accepts only regular non-symlink files: {child.name}"
                )
            found.append(child)
    return tuple(sorted(found, key=lambda item: item.relative_to(source_root).as_posix()))


def _copy_projection_file(
    source: Path,
    destination: Path,
    expected: ProjectionInputRecord,
) -> tuple[int, str]:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise NativeRvcRunnerError("cannot open reviewed RVC projection source") from exc
    try:
        source_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(source_stat.st_mode)
            or source_stat.st_size != expected.size
            or stat.S_IMODE(source_stat.st_mode) != expected.mode
        ):
            raise NativeRvcRunnerError("RVC projection source metadata differs from manifest")
        digest = hashlib.sha256()
        copied_bytes = 0
        with os.fdopen(descriptor, "rb", closefd=False) as source_stream:
            with destination.open("xb") as destination_stream:
                while True:
                    chunk = source_stream.read(1024 * 1024)
                    if not chunk:
                        break
                    copied_bytes += len(chunk)
                    digest.update(chunk)
                    destination_stream.write(chunk)
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
        final_source_stat = os.fstat(descriptor)
        copied_stat = destination.stat(follow_symlinks=False)
        copied_digest = digest.hexdigest()
        if (
            copied_bytes != expected.size
            or copied_stat.st_size != expected.size
            or final_source_stat.st_size != expected.size
            or stat.S_IMODE(final_source_stat.st_mode) != expected.mode
            or copied_digest != expected.sha256
        ):
            raise NativeRvcRunnerError("RVC projection source bytes differ from reviewed manifest")
        destination.chmod(0o444)
        return copied_stat.st_size, copied_digest
    finally:
        os.close(descriptor)


def _verify_projection(
    root: Path,
    expected_commit: str,
    expected_inputs: tuple[ProjectionInputRecord, ...],
) -> None:
    _require_safe_directory(root, root, "RVC execution projection")
    marker = root / _PROJECTION_MARKER
    if not _is_nonempty_regular_file(marker) or marker.is_symlink():
        raise NativeRvcRunnerError("RVC execution projection marker is missing or unsafe")
    if stat.S_IMODE(marker.stat(follow_symlinks=False).st_mode) != 0o444:
        raise NativeRvcRunnerError("RVC execution projection marker mode changed")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise NativeRvcRunnerError(f"duplicate RVC projection marker key: {key}")
            result[key] = value
        return result

    try:
        document = json.loads(
            marker.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NativeRvcRunnerError("cannot read RVC execution projection marker") from exc
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "schema_version",
            "rvc_commit_hash",
            "projection_directories",
            "files",
        }
        or document.get("schema_version") != 1
        or document.get("rvc_commit_hash") != expected_commit
        or document.get("projection_directories")
        != [path.as_posix() for path in _active_projection_directories(expected_inputs)]
        or not isinstance(document.get("files"), list)
    ):
        raise NativeRvcRunnerError("RVC execution projection marker does not match runtime")
    expected_records = {
        record.path.as_posix(): (record.size, record.sha256) for record in expected_inputs
    }
    revision_bytes = (expected_commit + "\n").encode("ascii")
    expected_records[_REVISION_MARKER.as_posix()] = (
        len(revision_bytes),
        hashlib.sha256(revision_bytes).hexdigest(),
    )
    expected_paths: set[str] = set()
    for raw_record in document["files"]:
        if not isinstance(raw_record, dict) or set(raw_record) != {
            "path",
            "size_bytes",
            "sha256",
        }:
            raise NativeRvcRunnerError("RVC projection contains an invalid file record")
        path_value = raw_record["path"]
        size = raw_record["size_bytes"]
        digest = raw_record["sha256"]
        if (
            not isinstance(path_value, str)
            or not path_value
            or "\\" in path_value
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise NativeRvcRunnerError("RVC projection contains invalid file metadata")
        pure = PurePosixPath(path_value)
        relative = Path(*pure.parts)
        relative_value = pure.as_posix()
        if (
            pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
            or relative_value != path_value
            or relative_value in expected_paths
            or relative_value not in expected_records
            or expected_records[relative_value] != (size, digest)
        ):
            raise NativeRvcRunnerError("RVC projection marker contains an unsafe path")
        path = root / relative
        ensure_within(path, root)
        _verify_projection_source_record(
            path,
            ProjectionInputRecord(relative, size, digest, 0o444),
        )
        expected_paths.add(relative_value)
    if expected_paths != set(expected_records):
        raise NativeRvcRunnerError("RVC execution projection inventory is incomplete")
    discovered: set[str] = {_REVISION_MARKER.as_posix()}
    for relative_root in _active_projection_directories(expected_inputs):
        immutable_root = root / relative_root
        for path in _walk_regular_source_files(immutable_root, root):
            discovered.add(path.relative_to(root).as_posix())
    if discovered != expected_paths:
        raise NativeRvcRunnerError("RVC execution projection contains unrecorded source files")


def _active_projection_directories(
    expected_inputs: tuple[ProjectionInputRecord, ...],
) -> tuple[Path, ...]:
    optional = tuple(
        root
        for root in _OPTIONAL_PROJECTION_DIRECTORIES
        if any(record.path.is_relative_to(root) for record in expected_inputs)
    )
    return (*_PROJECTION_DIRECTORIES, *optional)


def _collect_regular_files(
    directory: Path,
    workspace_root: Path,
    *,
    suffixes: set[str],
) -> tuple[Path, ...]:
    _require_safe_directory(directory, workspace_root, directory.name)
    result: list[Path] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.is_symlink():
            raise NativeRvcRunnerError(f"RVC output contains a symlink: {path.name}")
        try:
            mode = path.stat(follow_symlinks=False).st_mode
        except OSError as exc:
            raise NativeRvcRunnerError("cannot inspect an RVC output") from exc
        if not stat.S_ISREG(mode):
            raise NativeRvcRunnerError(f"RVC output is not a regular file: {path.name}")
        if path.suffix.lower() not in suffixes:
            continue
        if path.stat(follow_symlinks=False).st_size <= 0:
            raise NativeRvcRunnerError(f"RVC output is empty: {path.name}")
        result.append(ensure_within(path, workspace_root))
    return tuple(result)


def _verify_created_paths(context: RvcRunContext, paths: Sequence[Path]) -> tuple[Path, ...]:
    verified: list[Path] = []
    for path in paths:
        resolved = ensure_within(path, context.workspace.root)
        if not _is_nonempty_regular_file(resolved) or resolved.is_symlink():
            raise NativeRvcRunnerError(
                f"stage declared a missing, empty, or unsafe output: {path.name}"
            )
        verified.append(resolved)
    return tuple(dict.fromkeys(verified))


def _manifest_artifact_files(context: RvcRunContext) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for root in (context.workspace.outputs, context.experiment_logs):
        if not root.is_dir() or root.is_symlink():
            continue
        for path in root.rglob("*"):
            if path.name == _PROJECTION_MARKER.name or path.name == "artifact_manifest.json":
                continue
            if path.is_symlink():
                raise NativeRvcRunnerError("artifact manifest refuses symlink output")
            if path.is_file() and (
                root == context.workspace.outputs
                or path.name == "train.log"
                or path.name == "total_fea.npy"
                or path.name.startswith("added_")
                or _CHECKPOINT_NAME.fullmatch(path.name)
            ):
                candidates.append(ensure_within(path, context.workspace.root))
    return _verify_created_paths(context, tuple(sorted(set(candidates))))


def _copy_verified_file(source: Path, destination: Path, workspace_root: Path) -> None:
    source = ensure_within(source, workspace_root)
    destination = ensure_within(destination, workspace_root)
    if not _is_nonempty_regular_file(source) or source.is_symlink():
        raise NativeRvcRunnerError("cannot collect an unsafe RVC output")
    if destination.exists() or destination.is_symlink():
        if not _is_nonempty_regular_file(destination) or destination.is_symlink():
            raise NativeRvcRunnerError("small-model/index destination is unsafe")
        if sha256_file(destination) == sha256_file(source):
            return
    temporary = destination.parent / f".{destination.name}.{uuid4().hex}.tmp"
    ensure_within(temporary, workspace_root)
    try:
        with source.open("rb") as source_stream, temporary.open("xb") as destination_stream:
            shutil.copyfileobj(source_stream, destination_stream, length=1024 * 1024)
            destination_stream.flush()
            os.fsync(destination_stream.fileno())
        if sha256_file(temporary) != sha256_file(source):
            raise NativeRvcRunnerError("collected RVC output checksum changed during copy")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, document: Mapping[str, Any], workspace_root: Path) -> None:
    ensure_within(path, workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    ensure_within(temporary, workspace_root)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _require_safe_directory(path: Path, boundary: Path, label: str) -> None:
    try:
        ensure_within(path, boundary)
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as exc:
        raise NativeRvcRunnerError(f"{label} does not exist") from exc
    if path.is_symlink() or not stat.S_ISDIR(mode):
        raise NativeRvcRunnerError(f"{label} must be a regular non-symlink directory")


def _is_nonempty_regular_file(path: Path) -> bool:
    if not _is_regular_file(path):
        return False
    return path.stat(follow_symlinks=False).st_size > 0


def _is_regular_file(path: Path) -> bool:
    try:
        file_stat = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(file_stat.st_mode)
