"""Pure discovery of version-aware RVC output artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from rvc_orchestrator_contracts import RVCVersion

from .pretrained import feature_directory


class ArtifactDiscoveryError(RuntimeError):
    """Raised when RVC artifacts are missing or ambiguous."""


_EXPERIMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CHECKPOINT = re.compile(r"^(?P<kind>[GD])_(?P<epoch>\d+)\.pth$")


@dataclass(frozen=True, slots=True)
class Checkpoint:
    kind: str
    epoch: int
    path: Path


@dataclass(frozen=True, slots=True)
class DiscoveredArtifacts:
    experiment_logs: Path
    feature_directory: Path
    generator_checkpoints: tuple[Checkpoint, ...]
    discriminator_checkpoints: tuple[Checkpoint, ...]
    small_model: Path | None
    index_candidates: tuple[Path, ...]
    total_features: Path | None


def discover_artifacts(
    logs_root: Path,
    weights_root: Path,
    experiment_name: str,
    version: RVCVersion | str,
) -> DiscoveredArtifacts:
    _validate_experiment_name(experiment_name)
    experiment_logs = logs_root / experiment_name
    checkpoints = discover_checkpoints(experiment_logs)
    small_model_candidate = weights_root / f"{experiment_name}.pth"
    total_features_candidate = experiment_logs / "total_fea.npy"
    return DiscoveredArtifacts(
        experiment_logs=experiment_logs,
        feature_directory=experiment_logs / feature_directory(version),
        generator_checkpoints=tuple(item for item in checkpoints if item.kind == "G"),
        discriminator_checkpoints=tuple(item for item in checkpoints if item.kind == "D"),
        small_model=small_model_candidate if small_model_candidate.is_file() else None,
        index_candidates=find_index_candidates(experiment_logs),
        total_features=total_features_candidate if total_features_candidate.is_file() else None,
    )


def discover_checkpoints(experiment_logs: Path) -> tuple[Checkpoint, ...]:
    if not experiment_logs.is_dir():
        return ()
    found: list[Checkpoint] = []
    for path in experiment_logs.iterdir():
        if not path.is_file() or path.is_symlink():
            continue
        match = _CHECKPOINT.fullmatch(path.name)
        if match:
            found.append(Checkpoint(match.group("kind"), int(match.group("epoch")), path))
    return tuple(sorted(found, key=lambda item: (item.epoch, item.kind, item.path.name)))


def find_index_candidates(experiment_logs: Path) -> tuple[Path, ...]:
    if not experiment_logs.is_dir():
        return ()
    candidates = {
        path
        for pattern in ("added_*.index", "add_*.index")
        for path in experiment_logs.glob(pattern)
        if path.is_file() and not path.is_symlink()
    }
    return tuple(sorted(candidates, key=lambda path: path.name))


def select_final_index(candidates: tuple[Path, ...] | list[Path]) -> Path:
    unique = tuple(sorted(set(candidates), key=lambda path: path.name))
    if not unique:
        raise ArtifactDiscoveryError("RVC index was not produced")
    if len(unique) > 1:
        names = ", ".join(path.name for path in unique)
        raise ArtifactDiscoveryError(
            f"multiple RVC indexes require an explicit profile choice: {names}"
        )
    return unique[0]


def latest_generator_checkpoint(checkpoints: tuple[Checkpoint, ...]) -> Path:
    generators = [item for item in checkpoints if item.kind == "G"]
    if not generators:
        raise ArtifactDiscoveryError("no generator checkpoint was produced")
    return max(generators, key=lambda item: item.epoch).path


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_experiment_name(value: str) -> None:
    if not _EXPERIMENT_NAME.fullmatch(value):
        raise ArtifactDiscoveryError("experiment name is not a safe RVC path component")
