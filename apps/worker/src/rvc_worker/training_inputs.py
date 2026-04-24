"""Deterministic preparation of the files consumed by the RVC training CLI."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rvc_orchestrator_contracts import JobConfig, RVCVersion, SampleRate


class TrainingInputError(RuntimeError):
    """Raised when extracted RVC inputs are missing, ambiguous, or unsafe."""


@dataclass(frozen=True, slots=True)
class PreparedTrainingInputs:
    filelist_path: Path
    config_path: Path
    training_example_count: int
    mute_example_count: int
    config_template: str


_SAFE_STEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def prepare_training_inputs(
    config: JobConfig,
    repository_root: Path,
    experiment_directory: Path,
    *,
    use_half: bool,
) -> PreparedTrainingInputs:
    """Create ``filelist.txt`` and a job-local copy of the upstream JSON config."""

    repository = repository_root.resolve()
    experiment = experiment_directory.resolve()
    if not repository.is_dir() or repository.is_symlink():
        raise TrainingInputError("RVC repository is missing or is a symlink")
    if not experiment.is_dir() or experiment.is_symlink():
        raise TrainingInputError("RVC experiment directory is missing or is a symlink")

    wavs = _files_by_stem(experiment / "0_gt_wavs", ".wav")
    features = _files_by_stem(experiment / config.feature_directory, ".npy")
    names = set(wavs) & set(features)
    f0_values: dict[str, Path] = {}
    f0_nsf_values: dict[str, Path] = {}
    if config.model.use_f0:
        f0_values = _files_by_stem(experiment / "2a_f0", ".wav.npy")
        f0_nsf_values = _files_by_stem(experiment / "2b-f0nsf", ".wav.npy")
        names &= set(f0_values) & set(f0_nsf_values)
    if not names:
        raise TrainingInputError("no complete RVC training examples were found")

    rows: list[str] = []
    for name in sorted(names):
        fields = [wavs[name], features[name]]
        if config.model.use_f0:
            fields.extend((f0_values[name], f0_nsf_values[name]))
        rows.append(_filelist_row(fields, config.model.speaker_id))

    mute_fields = _mute_fields(config, repository)
    mute_row = _filelist_row(mute_fields, config.model.speaker_id)
    rows.extend((mute_row, mute_row))

    template = training_config_template(config.model.version, config.model.sample_rate)
    source_config = repository / "configs" / template
    document = _read_config(source_config)
    train = document.get("train")
    if not isinstance(train, dict):
        raise TrainingInputError("RVC config template has no train mapping")
    train["fp16_run"] = use_half

    filelist_path = experiment / "filelist.txt"
    config_path = experiment / "config.json"
    _atomic_write(filelist_path, "\n".join(rows) + "\n")
    _atomic_write(
        config_path,
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return PreparedTrainingInputs(
        filelist_path=filelist_path,
        config_path=config_path,
        training_example_count=len(names),
        mute_example_count=2,
        config_template=template,
    )


def training_config_template(version: RVCVersion, sample_rate: SampleRate) -> str:
    """Return the exact config branch used by reviewed upstream ``infer-web.py``."""

    if version is RVCVersion.V1 or sample_rate is SampleRate.KHZ_40:
        return f"v1/{sample_rate.value}.json"
    return f"v2/{sample_rate.value}.json"


def _files_by_stem(directory: Path, suffix: str) -> dict[str, Path]:
    if not directory.is_dir() or directory.is_symlink():
        raise TrainingInputError(f"required RVC directory is missing or unsafe: {directory.name}")
    discovered: dict[str, Path] = {}
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.name.endswith(suffix):
            continue
        if not path.is_file() or path.is_symlink():
            raise TrainingInputError(f"RVC input is not a regular file: {path.name}")
        stem = path.name[: -len(suffix)]
        if not _SAFE_STEM.fullmatch(stem):
            raise TrainingInputError(f"RVC input has an unsafe stem: {path.name}")
        if stem in discovered:
            raise TrainingInputError(f"duplicate RVC input stem: {stem}")
        discovered[stem] = path.resolve()
    return discovered


def _mute_fields(config: JobConfig, repository: Path) -> list[Path]:
    dimension = "256" if config.model.version is RVCVersion.V1 else "768"
    fields = [
        repository / "logs/mute/0_gt_wavs" / f"mute{config.model.sample_rate.value}.wav",
        repository / "logs/mute" / f"3_feature{dimension}/mute.npy",
    ]
    if config.model.use_f0:
        fields.extend(
            (
                repository / "logs/mute/2a_f0/mute.wav.npy",
                repository / "logs/mute/2b-f0nsf/mute.wav.npy",
            )
        )
    for path in fields:
        if not path.is_file() or path.is_symlink():
            raise TrainingInputError(f"required upstream mute asset is missing: {path.name}")
    return [path.resolve() for path in fields]


def _filelist_row(paths: list[Path], speaker_id: int) -> str:
    rendered = [str(path) for path in paths]
    if any("|" in value or "\n" in value or "\r" in value for value in rendered):
        raise TrainingInputError("RVC filelist paths contain a reserved character")
    return "|".join((*rendered, str(speaker_id)))


def _read_config(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise TrainingInputError(f"RVC config template is missing: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrainingInputError("cannot read RVC config template") from exc
    if not isinstance(document, dict):
        raise TrainingInputError("RVC config template must be a JSON object")
    return document


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
