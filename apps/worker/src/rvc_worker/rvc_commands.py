"""Pure command builders for the reviewed RVC WebUI command-line interface.

The upstream WebUI assembles shell strings.  The worker deliberately represents
every invocation as an argv tuple so paths and user-controlled identifiers are
never re-interpreted by a shell.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rvc_orchestrator_contracts import JobConfig, SampleRate, TrainingF0Method

from .pretrained import PretrainedPair, resolve_pretrained

RVC_UPSTREAM_REPOSITORY = "RVC-Project/Retrieval-based-Voice-Conversion-WebUI"
RVC_REVIEWED_COMMIT = "7ef19867780cf703841ebafb565a4e47d1ea86ff"

_SAMPLE_RATE_HZ = {
    SampleRate.KHZ_40: 40_000,
    SampleRate.KHZ_48: 48_000,
}
_DEVICE = re.compile(r"^(?:cuda(?::[0-9]+)?|cpu|mps)$")


class RvcCommandError(ValueError):
    """Raised when a job cannot be represented by the reviewed RVC CLI."""


@dataclass(frozen=True, slots=True)
class RvcCliRuntime:
    """Non-secret, operator-controlled values used to construct RVC commands."""

    python_executable: str
    repository_root: Path
    cpu_workers: int = 2
    device: str = "cuda"
    use_half: bool = True
    preprocess_no_parallel: bool = False
    preprocess_segment_seconds: float = 3.7
    available_gpu_ids: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        if not self.python_executable or "\x00" in self.python_executable:
            raise RvcCommandError("python executable must be a non-empty NUL-free value")
        if not self.repository_root.is_absolute():
            raise RvcCommandError("RVC repository root must be absolute")
        if not 1 <= self.cpu_workers <= 256:
            raise RvcCommandError("cpu_workers must be between 1 and 256")
        if not _DEVICE.fullmatch(self.device):
            raise RvcCommandError("device must be cuda, cuda:N, cpu, or mps")
        if not 1.0 <= self.preprocess_segment_seconds <= 30.0:
            raise RvcCommandError("preprocess segment length must be between 1 and 30 seconds")
        if self.available_gpu_ids is not None:
            _validated_gpu_ids(self.available_gpu_ids)


@dataclass(frozen=True, slots=True)
class RvcCommandPlan:
    """All upstream CLI invocations required before index/model collection."""

    preprocessing: tuple[str, ...]
    f0_extraction: tuple[tuple[str, ...], ...]
    feature_extraction: tuple[tuple[str, ...], ...]
    training: tuple[str, ...]


def validate_gpu_ids(
    requested: list[int] | tuple[int, ...],
    available: tuple[int, ...] | None,
) -> tuple[int, ...]:
    """Validate syntax and, when reported, the Worker's visible GPU inventory."""

    parsed = _validated_gpu_ids(requested)
    if available is None:
        return parsed
    visible = set(_validated_gpu_ids(available, allow_empty=True))
    missing = [gpu_id for gpu_id in parsed if gpu_id not in visible]
    if missing:
        rendered = ", ".join(str(gpu_id) for gpu_id in missing)
        raise RvcCommandError(f"requested GPU IDs are not visible to this Worker: {rendered}")
    return parsed


def build_preprocess_command(
    config: JobConfig,
    runtime: RvcCliRuntime,
    dataset_directory: Path,
    experiment_directory: Path,
) -> tuple[str, ...]:
    """Build ``infer/modules/train/preprocess.py`` argv for a flat dataset."""

    _require_absolute_directory_argument(dataset_directory, "dataset directory")
    _require_absolute_directory_argument(experiment_directory, "experiment directory")
    return (
        runtime.python_executable,
        _script(runtime, "infer/modules/train/preprocess.py"),
        str(dataset_directory),
        str(_SAMPLE_RATE_HZ[config.model.sample_rate]),
        str(runtime.cpu_workers),
        str(experiment_directory),
        str(runtime.preprocess_no_parallel),
        f"{runtime.preprocess_segment_seconds:.1f}",
    )


def build_f0_extraction_commands(
    config: JobConfig,
    runtime: RvcCliRuntime,
    experiment_directory: Path,
) -> tuple[tuple[str, ...], ...]:
    """Build the CPU or sharded RMVPE-GPU F0 extraction invocations."""

    _require_absolute_directory_argument(experiment_directory, "experiment directory")
    if not config.model.use_f0:
        return ()
    method = config.f0_extraction.training_f0_method
    if method is None:
        raise RvcCommandError("F0-enabled jobs require a training F0 method")
    if method is not TrainingF0Method.RMVPE_GPU:
        return (
            (
                runtime.python_executable,
                _script(runtime, "infer/modules/train/extract/extract_f0_print.py"),
                str(experiment_directory),
                str(runtime.cpu_workers),
                method.value,
            ),
        )

    requested = config.f0_extraction.rmvpe_gpu_ids or []
    gpu_ids = validate_gpu_ids(requested, runtime.available_gpu_ids)
    shard_count = len(gpu_ids)
    return tuple(
        (
            runtime.python_executable,
            _script(runtime, "infer/modules/train/extract/extract_f0_rmvpe.py"),
            str(shard_count),
            str(shard_index),
            str(gpu_id),
            str(experiment_directory),
            _bool_argument(runtime.use_half),
        )
        for shard_index, gpu_id in enumerate(gpu_ids)
    )


def build_feature_extraction_commands(
    config: JobConfig,
    runtime: RvcCliRuntime,
    experiment_directory: Path,
) -> tuple[tuple[str, ...], ...]:
    """Build one HuBERT extraction invocation per selected GPU shard."""

    _require_absolute_directory_argument(experiment_directory, "experiment directory")
    gpu_ids = validate_gpu_ids(config.training.gpu_ids, runtime.available_gpu_ids)
    shard_count = len(gpu_ids)
    return tuple(
        (
            runtime.python_executable,
            _script(runtime, "infer/modules/train/extract_feature_print.py"),
            runtime.device,
            str(shard_count),
            str(shard_index),
            str(gpu_id),
            str(experiment_directory),
            config.model.version.value,
            _bool_argument(runtime.use_half),
        )
        for shard_index, gpu_id in enumerate(gpu_ids)
    )


def build_training_command(
    config: JobConfig,
    runtime: RvcCliRuntime,
    pretrained: PretrainedPair,
) -> tuple[str, ...]:
    """Build the reviewed ``train.py`` invocation, including all persistence flags."""

    gpu_ids = validate_gpu_ids(config.training.gpu_ids, runtime.available_gpu_ids)
    argv = [
        runtime.python_executable,
        _script(runtime, "infer/modules/train/train.py"),
        "-e",
        config.job_name,
        "-sr",
        config.model.sample_rate.value,
        "-f0",
        _bool_digit(config.model.use_f0),
        "-bs",
        str(config.training.batch_size_per_gpu),
        "-g",
        "-".join(str(gpu_id) for gpu_id in gpu_ids),
        "-te",
        str(config.training.epochs),
        "-se",
        str(config.training.save_every_epoch),
        "-pg",
        str(pretrained.generator),
        "-pd",
        str(pretrained.discriminator),
        "-l",
        _bool_digit(config.training.save_only_latest),
        "-c",
        _bool_digit(config.training.cache_dataset_in_gpu),
        "-sw",
        _bool_digit(config.training.save_every_weights),
        "-v",
        config.model.version.value,
    ]
    return tuple(argv)


def build_command_plan(
    config: JobConfig,
    runtime: RvcCliRuntime,
    dataset_directory: Path,
    experiment_directory: Path,
    *,
    pretrained: PretrainedPair | None = None,
) -> RvcCommandPlan:
    """Build a deterministic command snapshot for a validated Job config."""

    selected_pretrained = pretrained or resolve_pretrained(
        runtime.repository_root,
        config.model.version,
        config.model.sample_rate,
        config.model.use_f0,
    )
    return RvcCommandPlan(
        preprocessing=build_preprocess_command(
            config, runtime, dataset_directory, experiment_directory
        ),
        f0_extraction=build_f0_extraction_commands(config, runtime, experiment_directory),
        feature_extraction=build_feature_extraction_commands(
            config, runtime, experiment_directory
        ),
        training=build_training_command(config, runtime, selected_pretrained),
    )


def _validated_gpu_ids(
    gpu_ids: list[int] | tuple[int, ...], *, allow_empty: bool = False
) -> tuple[int, ...]:
    parsed = tuple(gpu_ids)
    if not parsed and not allow_empty:
        raise RvcCommandError("at least one GPU ID is required")
    if any(isinstance(gpu_id, bool) or not isinstance(gpu_id, int) for gpu_id in parsed):
        raise RvcCommandError("GPU IDs must be integers")
    if any(gpu_id < 0 for gpu_id in parsed):
        raise RvcCommandError("GPU IDs must be non-negative")
    if len(set(parsed)) != len(parsed):
        raise RvcCommandError("GPU IDs must be unique")
    return parsed


def _script(runtime: RvcCliRuntime, relative: str) -> str:
    return str(runtime.repository_root / relative)


def _bool_argument(value: bool) -> str:
    return "True" if value else "False"


def _bool_digit(value: bool) -> str:
    return "1" if value else "0"


def _require_absolute_directory_argument(path: Path, label: str) -> None:
    if not path.is_absolute() or "\x00" in str(path):
        raise RvcCommandError(f"{label} must be an absolute NUL-free path")
