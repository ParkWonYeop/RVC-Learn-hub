"""RVC stage runner boundary, deterministic fake, and explicit command profiles."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from rvc_orchestrator_contracts import JobClaim, JobStatus

from .artifacts import (
    discover_artifacts,
    select_final_index,
    sha256_file,
)
from .process import ProcessSpec, SafeSubprocessRunner
from .workspace import JobWorkspace


class RvcRunnerError(RuntimeError):
    """Raised when an RVC stage cannot be executed safely."""


class RvcConfigurationError(RvcRunnerError):
    """The claim or reviewed runner configuration is deterministically invalid."""


class RvcRuntimeIntegrityError(RvcRunnerError):
    """The Worker runtime cannot prove that its pinned inputs are ready."""


@dataclass(frozen=True, slots=True)
class StageResult:
    created_paths: tuple[Path, ...] = ()
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RvcRunContext:
    claim: JobClaim
    workspace: JobWorkspace

    @property
    def experiment_name(self) -> str:
        # A job name is contract-validated and unique per run configuration.
        return self.claim.config.job_name

    @property
    def rvc_root(self) -> Path:
        return self.workspace.work / "rvc"

    @property
    def experiment_logs(self) -> Path:
        return self.rvc_root / "logs" / self.experiment_name

    @property
    def weights_root(self) -> Path:
        return self.rvc_root / "weights"


class RvcRunner(Protocol):
    async def run_stage(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult: ...


class FakeRvcRunner:
    """Safe default runner that exercises the complete worker flow without RVC/GPU."""

    def __init__(self, *, stage_delay_seconds: float = 0.0) -> None:
        self.stage_delay_seconds = stage_delay_seconds

    async def run_stage(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        await _interruptible_delay(self.stage_delay_seconds, cancellation)
        if cancellation.is_set():
            raise asyncio.CancelledError
        handler = getattr(self, f"_stage_{stage.value}", None)
        if handler is None:
            raise RvcRunnerError(f"FakeRvcRunner does not implement stage {stage.value}")
        result = handler(context)
        marker = context.workspace.work / ".stages" / f"{stage.value}.json"
        _write_json(
            marker,
            {
                "stage": stage.value,
                "job_id": context.claim.job_id,
                "attempt_id": context.claim.attempt_id,
            },
        )
        return StageResult(tuple((*result.created_paths, marker)), result.metadata)

    def _stage_downloading_dataset(self, context: RvcRunContext) -> StageResult:
        dataset = context.workspace.inputs / "dataset" / "source.wav"
        _write_bytes(dataset, b"FAKE-WAV-DATA")
        _write_json(
            context.workspace.inputs / "dataset_manifest.json",
            {
                "dataset_id": context.claim.config.dataset_id,
                "files": [dataset.name],
                "fake": True,
            },
        )
        config_path = context.workspace.outputs / "config.json"
        environment_path = context.workspace.outputs / "environment.json"
        _write_json(config_path, context.claim.config.model_dump(mode="json"))
        _write_json(
            environment_path,
            {
                "runner": "fake",
                "rvc_commit_hash": "fake-runner",
                "job_id": context.claim.job_id,
                "attempt_id": context.claim.attempt_id,
            },
        )
        return StageResult((dataset, config_path, environment_path))

    def _stage_validating_dataset(self, context: RvcRunContext) -> StageResult:
        report = context.workspace.outputs / "dataset_report.json"
        _write_json(report, {"valid": True, "file_count": 1, "fake": True})
        return StageResult((report,))

    def _stage_preparing_flat_dataset(self, context: RvcRunContext) -> StageResult:
        source = context.workspace.inputs / "dataset" / "source.wav"
        target = context.workspace.inputs / "prepared_flat" / "000001.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return StageResult((target,))

    def _stage_preprocessing(self, context: RvcRunContext) -> StageResult:
        gt = context.experiment_logs / "0_gt_wavs" / "000001.wav"
        wav16 = context.experiment_logs / "1_16k_wavs" / "000001.wav"
        _write_bytes(gt, b"FAKE-GT")
        _write_bytes(wav16, b"FAKE-16K")
        return StageResult((gt, wav16))

    def _stage_extracting_f0(self, context: RvcRunContext) -> StageResult:
        f0 = context.experiment_logs / "2a_f0" / "000001.wav.npy"
        f0nsf = context.experiment_logs / "2b-f0nsf" / "000001.wav.npy"
        _write_bytes(f0, b"FAKE-F0")
        _write_bytes(f0nsf, b"FAKE-F0NSF")
        return StageResult((f0, f0nsf))

    def _stage_extracting_features(self, context: RvcRunContext) -> StageResult:
        feature = context.experiment_logs / context.claim.config.feature_directory / "000001.npy"
        _write_bytes(feature, b"FAKE-FEATURE")
        return StageResult((feature,))

    def _stage_training(self, context: RvcRunContext) -> StageResult:
        epoch = context.claim.config.training.epochs
        generator = context.experiment_logs / f"G_{epoch}.pth"
        discriminator = context.experiment_logs / f"D_{epoch}.pth"
        small_model = context.weights_root / f"{context.experiment_name}.pth"
        train_log = context.experiment_logs / "train.log"
        _write_bytes(generator, b"FAKE-GENERATOR-CHECKPOINT")
        _write_bytes(discriminator, b"FAKE-DISCRIMINATOR-CHECKPOINT")
        _write_bytes(small_model, b"FAKE-SMALL-MODEL")
        _write_bytes(train_log, b"epoch=1 loss=0.0\n")
        return StageResult((generator, discriminator, small_model, train_log))

    def _stage_saving_checkpoint(self, context: RvcRunContext) -> StageResult:
        epoch = context.claim.config.training.epochs
        paths = (
            context.experiment_logs / f"G_{epoch}.pth",
            context.experiment_logs / f"D_{epoch}.pth",
        )
        if not all(path.is_file() for path in paths):
            raise RvcRunnerError("fake training did not create checkpoint pair")
        return StageResult(paths)

    def _stage_building_index(self, context: RvcRunContext) -> StageResult:
        total = context.experiment_logs / "total_fea.npy"
        index = context.experiment_logs / "added_IVF1_Flat_nprobe_1.index"
        _write_bytes(total, b"FAKE-TOTAL-FEATURES")
        _write_bytes(index, b"FAKE-FAISS-INDEX")
        return StageResult((total, index))

    def _stage_collecting_small_model(self, context: RvcRunContext) -> StageResult:
        artifacts = discover_artifacts(
            context.rvc_root / "logs",
            context.weights_root,
            context.experiment_name,
            context.claim.config.model.version,
        )
        if artifacts.small_model is None:
            raise RvcRunnerError("final small model is missing; checkpoint copying is forbidden")
        destination = context.workspace.outputs / "model" / "final_small_model.pth"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(artifacts.small_model, destination)
        created = [destination]
        if context.claim.config.index.build_index:
            source_index = select_final_index(artifacts.index_candidates)
            final_index = context.workspace.outputs / "index" / "final.index"
            final_index.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_index, final_index)
            created.append(final_index)
        return StageResult(tuple(created))

    def _stage_generating_samples(self, context: RvcRunContext) -> StageResult:
        sample = context.workspace.outputs / "samples" / "fixed_test_converted.wav"
        _write_bytes(sample, b"FAKE-CONVERTED-WAV")
        return StageResult((sample,))

    def _stage_evaluating(self, context: RvcRunContext) -> StageResult:
        metrics = context.workspace.outputs / "metrics" / "sample_metrics.json"
        _write_json(metrics, {"clipping_ratio": 0.0, "duration_match_ratio": 1.0, "fake": True})
        return StageResult((metrics,))

    def _stage_uploading_artifacts(self, context: RvcRunContext) -> StageResult:
        files = sorted(
            (path for path in context.workspace.outputs.rglob("*") if path.is_file()),
            key=lambda path: str(path.relative_to(context.workspace.outputs)),
        )
        manifest = context.workspace.outputs / "artifact_manifest.json"
        _write_json(
            manifest,
            {
                "fake": True,
                "files": [
                    {
                        "path": str(path.relative_to(context.workspace.outputs)),
                        "size_bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                    for path in files
                ],
            },
        )
        return StageResult((manifest,))


@dataclass(frozen=True, slots=True)
class StageCommand:
    argv: tuple[str, ...]
    timeout_seconds: float | None = None
    env: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class CommandProfile:
    profile_id: str
    repository_root: Path
    expected_commit_hash: str
    commands: Mapping[str, StageCommand]

    @classmethod
    def load(cls, path: Path) -> CommandProfile:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise RvcRunnerError(f"cannot read RVC command profile: {path}") from exc
        if not isinstance(document, dict):
            raise RvcRunnerError("RVC command profile must be a mapping")
        raw_commands = document.get("commands")
        if not isinstance(raw_commands, dict) or not raw_commands:
            raise RvcRunnerError("RVC command profile requires commands")
        commands: dict[str, StageCommand] = {}
        for stage, raw in raw_commands.items():
            if not isinstance(raw, dict) or not isinstance(raw.get("argv"), list):
                raise RvcRunnerError(f"profile command {stage!r} requires an argv list")
            argv = tuple(str(part) for part in raw["argv"])
            if not argv:
                raise RvcRunnerError(f"profile command {stage!r} has empty argv")
            environment = raw.get("env")
            if environment is not None and not isinstance(environment, dict):
                raise RvcRunnerError(f"profile command {stage!r} env must be a mapping")
            commands[str(stage)] = StageCommand(
                argv,
                float(raw["timeout_seconds"]) if raw.get("timeout_seconds") else None,
                {str(key): str(value) for key, value in environment.items()}
                if environment
                else None,
            )
        repository_raw = document.get("repository_root")
        expected_commit = str(document.get("expected_commit_hash", "")).strip().lower()
        profile_id = str(document.get("profile_id", "")).strip()
        if (
            not profile_id
            or not repository_raw
            or not 7 <= len(expected_commit) <= 64
            or any(character not in "0123456789abcdef" for character in expected_commit)
        ):
            raise RvcRunnerError(
                "profile_id, repository_root and expected_commit_hash are required"
            )
        return cls(
            profile_id,
            Path(str(repository_raw)).expanduser().resolve(),
            expected_commit,
            commands,
        )

    def verify_repository(self, *, timeout_seconds: float = 5.0) -> str:
        if not self.repository_root.is_dir():
            raise RvcRunnerError("profile repository_root does not exist")
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repository_root), "rev-parse", "HEAD"],
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RvcRunnerError(f"cannot verify RVC repository: {type(exc).__name__}") from exc
        actual = result.stdout.strip().lower()
        if result.returncode != 0 or not actual.startswith(self.expected_commit_hash):
            raise RvcRunnerError("RVC repository revision does not match the command profile")
        return actual


class ProfileRvcRunner:
    """Executes only commands explicitly present in a verified pinned profile."""

    def __init__(
        self,
        profile: CommandProfile,
        *,
        process_runner: SafeSubprocessRunner | None = None,
        verify_repository: bool = True,
    ) -> None:
        if verify_repository:
            self.verified_commit_hash = profile.verify_repository()
        else:
            self.verified_commit_hash = profile.expected_commit_hash
        self.profile = profile
        self.process_runner = process_runner or SafeSubprocessRunner()

    async def run_stage(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        command = self.profile.commands.get(stage.value)
        if command is None:
            raise RvcRunnerError(
                f"verified profile {self.profile.profile_id!r} has no command for {stage.value}"
            )
        values = _profile_values(context, self.profile)
        argv = tuple(_render_argument(argument, values) for argument in command.argv)
        log_root = context.workspace.logs / "subprocess"
        result = await self.process_runner.run(
            ProcessSpec(
                argv=argv,
                cwd=context.workspace.work,
                workspace_root=context.workspace.root,
                stdout_path=log_root / f"{stage.value}.stdout.log",
                stderr_path=log_root / f"{stage.value}.stderr.log",
                env=command.env,
                timeout_seconds=command.timeout_seconds,
            ),
            cancellation,
        )
        return StageResult((result.stdout_path, result.stderr_path))


def create_runner(
    mode: str,
    *,
    profile_path: Path | None = None,
    verify_repository: bool = True,
    native_source_root: Path | None = None,
    native_python_executable: str | None = None,
    native_cpu_workers: int = 2,
    native_device: str = "cuda",
    native_use_half: bool = True,
    native_preprocess_timeout_seconds: float = 3_600.0,
    native_extraction_timeout_seconds: float = 7_200.0,
    native_training_timeout_seconds: float = 7 * 24 * 3_600.0,
    native_index_timeout_seconds: float = 24 * 3_600.0,
    native_small_model_timeout_seconds: float = 3_600.0,
    runtime_activation_path: Path | None = None,
) -> RvcRunner:
    if mode == "fake":
        return FakeRvcRunner()
    if mode == "profile" and profile_path is not None:
        return ProfileRvcRunner(
            CommandProfile.load(profile_path), verify_repository=verify_repository
        )
    if mode == "native":
        if native_source_root is None:
            raise RvcRunnerError("native RVC execution requires an absolute source root")
        # Local import is intentional: ``native_runner`` implements this protocol
        # and imports the stage types above. Importing it at module load would form
        # a cycle before ``RvcRunner`` has been defined.
        from .native_inference import NativeFixedTestSetInferenceDependency
        from .native_runner import NativeRvcRuntime, PinnedRvcRunner
        from .runtime_activation import load_runtime_activation

        activation = (
            load_runtime_activation(
                runtime_activation_path,
                native_source_root=native_source_root,
            )
            if runtime_activation_path is not None
            else None
        )
        sample_inference_dependency = (
            NativeFixedTestSetInferenceDependency(
                runtime_image_digest=activation.runtime_image_digest,
                expected_asset_manifest_sha256=(activation.runtime_asset_manifest_sha256),
            )
            if activation is not None
            else None
        )

        return PinnedRvcRunner(
            NativeRvcRuntime(
                source_root=native_source_root,
                asset_manifest_path=native_source_root / "assets-manifest.json",
                python_executable=native_python_executable or sys.executable,
                cpu_workers=native_cpu_workers,
                device=native_device,
                use_half=native_use_half,
                preprocess_timeout_seconds=native_preprocess_timeout_seconds,
                extraction_timeout_seconds=native_extraction_timeout_seconds,
                training_timeout_seconds=native_training_timeout_seconds,
                index_timeout_seconds=native_index_timeout_seconds,
                small_model_timeout_seconds=native_small_model_timeout_seconds,
            ),
            sample_inference_dependency=sample_inference_dependency,
        )
    raise RvcRunnerError(
        "actual RVC execution requires a verified profile or pinned native runtime"
    )


def _profile_values(context: RvcRunContext, profile: CommandProfile) -> dict[str, str]:
    config = context.claim.config
    pretrained_prefix = "f0" if config.model.use_f0 else ""
    training_f0_method = config.f0_extraction.training_f0_method
    rmvpe_gpu_ids = config.f0_extraction.rmvpe_gpu_ids or []
    return {
        "repository": str(profile.repository_root),
        "workspace": str(context.workspace.root),
        "inputs": str(context.workspace.inputs),
        "work": str(context.workspace.work),
        "outputs": str(context.workspace.outputs),
        "logs": str(context.workspace.logs),
        "rvc_logs": str(context.experiment_logs),
        "flat_dataset": str(context.workspace.inputs / "prepared_flat"),
        "experiment": context.experiment_name,
        "version": config.model.version.value,
        "sample_rate": config.model.sample_rate.value,
        "sample_rate_hz": str({"40k": 40_000, "48k": 48_000}[config.model.sample_rate.value]),
        "use_f0": "1" if config.model.use_f0 else "0",
        "pretrained_prefix": pretrained_prefix,
        "training_f0_method": training_f0_method.value if training_f0_method else "",
        "epochs": str(config.training.epochs),
        "save_every_epoch": str(config.training.save_every_epoch),
        "save_only_latest": "1" if config.training.save_only_latest else "0",
        "save_every_weights": "1" if config.training.save_every_weights else "0",
        "cache_dataset_in_gpu": "1" if config.training.cache_dataset_in_gpu else "0",
        "batch_size": str(config.training.batch_size_per_gpu),
        "gpu_ids": ",".join(str(value) for value in config.training.gpu_ids),
        "gpu_ids_dash": "-".join(str(value) for value in config.training.gpu_ids),
        "rmvpe_gpu_ids_dash": "-".join(str(value) for value in rmvpe_gpu_ids),
        "speaker_id": str(config.model.speaker_id),
    }


class _StrictValues(dict[str, str]):
    def __missing__(self, key: str) -> str:
        raise RvcRunnerError(f"unknown command profile placeholder: {key}")


def _render_argument(argument: str, values: Mapping[str, str]) -> str:
    try:
        rendered = argument.format_map(_StrictValues(values))
    except (ValueError, KeyError) as exc:
        raise RvcRunnerError(f"invalid command profile argument: {argument!r}") from exc
    if "\x00" in rendered:
        raise RvcRunnerError("rendered profile argument contains NUL")
    return rendered


async def _interruptible_delay(seconds: float, cancellation: asyncio.Event) -> None:
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(cancellation.wait(), timeout=seconds)
    except TimeoutError:
        return


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
