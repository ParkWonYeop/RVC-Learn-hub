"""Deterministic, bounded FAISS index construction for RVC feature arrays.

The numerical dependencies deliberately are not imported at module import time.  They
belong to the pinned RVC runtime image, while the Worker control plane and its unit tests
must remain usable without NumPy, FAISS, or scikit-learn installed.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from rvc_orchestrator_contracts import RVCVersion, feature_directory_for_version


class IndexBuildError(RuntimeError):
    """Raised when feature input is unsafe or an index cannot be verified."""


_EXPERIMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_UINT32_MAX = (1 << 32) - 1


@dataclass(frozen=True, slots=True)
class IndexBuildLimits:
    """Resource ceilings applied before concatenating untrusted feature arrays."""

    max_files: int = 100_000
    max_file_bytes: int = 4 * 1024**3
    max_total_input_bytes: int = 64 * 1024**3
    max_rows_per_file: int = 5_000_000
    max_total_rows: int = 20_000_000
    max_float32_bytes: int = 64 * 1024**3
    kmeans_threshold_rows: int = 200_000
    kmeans_clusters: int = 10_000
    add_batch_rows: int = 8_192

    def __post_init__(self) -> None:
        positive_values = (
            self.max_files,
            self.max_file_bytes,
            self.max_total_input_bytes,
            self.max_rows_per_file,
            self.max_total_rows,
            self.max_float32_bytes,
            self.kmeans_threshold_rows,
            self.kmeans_clusters,
            self.add_batch_rows,
        )
        if any(value <= 0 for value in positive_values):
            raise ValueError("all index build limits must be greater than zero")
        if self.kmeans_clusters > self.max_total_rows:
            raise ValueError("kmeans_clusters cannot exceed max_total_rows")


@dataclass(frozen=True, slots=True)
class IndexDependencies:
    """Injected numerical runtime used by tests and the pinned RVC environment."""

    numpy: Any
    faiss: Any
    mini_batch_kmeans: Any | None = None


@dataclass(frozen=True, slots=True)
class IndexBuildResult:
    """Verified files and parameters produced by one deterministic index build."""

    total_features: Path
    trained_index: Path
    added_index: Path
    source_files: tuple[Path, ...]
    source_rows: int
    indexed_rows: int
    dimension: int
    n_ivf: int
    nprobe: int
    seed: int
    used_kmeans: bool


def build_index_command(
    python_executable: str,
    experiment_directory: Path,
    experiment_name: str,
    version: RVCVersion | str,
    *,
    seed: int = 0,
    cpu_workers: int = 1,
) -> tuple[str, ...]:
    """Build shell-free argv for an isolated FAISS index subprocess."""

    if not python_executable or "\x00" in python_executable:
        raise IndexBuildError("python executable must be a non-empty NUL-free value")
    _require_absolute_nul_free(experiment_directory, "experiment directory")
    _validate_experiment_name(experiment_name)
    parsed_version = _parse_version(version)
    _validate_seed(seed)
    if not 1 <= cpu_workers <= 256:
        raise IndexBuildError("cpu_workers must be between 1 and 256")
    return (
        python_executable,
        "-m",
        "rvc_worker.index_builder",
        "--experiment-directory",
        str(experiment_directory),
        "--experiment-name",
        experiment_name,
        "--version",
        parsed_version.value,
        "--seed",
        str(seed),
        "--cpu-workers",
        str(cpu_workers),
    )


def build_rvc_index(
    experiment_directory: Path,
    experiment_name: str,
    version: RVCVersion | str,
    *,
    seed: int = 0,
    cpu_workers: int = 1,
    limits: IndexBuildLimits | None = None,
    dependencies: IndexDependencies | None = None,
) -> IndexBuildResult:
    """Build an index from the version-specific feature directory in an RVC log tree."""

    parsed_version = _parse_version(version)
    return build_index(
        experiment_directory / feature_directory_for_version(parsed_version),
        experiment_directory,
        experiment_name,
        parsed_version,
        seed=seed,
        cpu_workers=cpu_workers,
        limits=limits,
        dependencies=dependencies,
    )


def build_index(
    features_directory: Path,
    output_directory: Path,
    experiment_name: str,
    version: RVCVersion | str,
    *,
    seed: int = 0,
    cpu_workers: int = 1,
    limits: IndexBuildLimits | None = None,
    dependencies: IndexDependencies | None = None,
) -> IndexBuildResult:
    """Build RVC's trained and populated IVF-Flat indexes from safe ``.npy`` inputs.

    ``features_directory`` and ``output_directory`` must already exist.  The function
    never follows a feature-file symlink, never enables NumPy pickle loading, and stages
    every output before replacing its final path.
    """

    parsed_version = _parse_version(version)
    dimension = 256 if parsed_version is RVCVersion.V1 else 768
    _validate_experiment_name(experiment_name)
    _validate_seed(seed)
    if not 1 <= cpu_workers <= 256:
        raise IndexBuildError("cpu_workers must be between 1 and 256")
    selected_limits = limits or IndexBuildLimits()
    _require_directory(features_directory, "feature directory")
    _require_directory(output_directory, "output directory")

    source_files = _discover_feature_files(features_directory, selected_limits)
    runtime = dependencies or _load_core_dependencies()
    arrays, source_rows = _load_feature_arrays(
        source_files,
        dimension,
        selected_limits,
        runtime.numpy,
    )

    try:
        features = runtime.numpy.concatenate(arrays, axis=0)
        features = runtime.numpy.ascontiguousarray(features, dtype=runtime.numpy.float32)
        permutation = runtime.numpy.random.default_rng(seed).permutation(source_rows)
        features = runtime.numpy.ascontiguousarray(
            features[permutation], dtype=runtime.numpy.float32
        )
    except Exception as exc:
        raise IndexBuildError("cannot concatenate and deterministically shuffle features") from exc
    _validate_matrix(features, dimension, source_rows, runtime.numpy, "combined features")

    used_kmeans = source_rows > selected_limits.kmeans_threshold_rows
    if used_kmeans:
        factory = runtime.mini_batch_kmeans or _load_mini_batch_kmeans()
        cluster_count = min(selected_limits.kmeans_clusters, source_rows)
        try:
            estimator = factory(
                n_clusters=cluster_count,
                verbose=False,
                batch_size=min(256 * cpu_workers, source_rows),
                compute_labels=False,
                init="random",
                random_state=seed,
                n_init=1,
            )
            features = estimator.fit(features).cluster_centers_
            features = runtime.numpy.ascontiguousarray(
                features, dtype=runtime.numpy.float32
            )
        except Exception as exc:
            raise IndexBuildError("deterministic MiniBatchKMeans reduction failed") from exc
        _validate_matrix(
            features,
            dimension,
            cluster_count,
            runtime.numpy,
            "kmeans centers",
        )

    indexed_rows = _matrix_rows(features)
    n_ivf = max(1, min(int(16 * math.sqrt(indexed_rows)), indexed_rows // 39))
    nprobe = 1
    total_features = output_directory / "total_fea.npy"
    trained_index = output_directory / (
        f"trained_IVF{n_ivf}_Flat_nprobe_{nprobe}_{experiment_name}_{parsed_version.value}.index"
    )
    added_index = output_directory / (
        f"added_IVF{n_ivf}_Flat_nprobe_{nprobe}_{experiment_name}_{parsed_version.value}.index"
    )
    for path in (total_features, trained_index, added_index):
        _require_safe_destination(path)

    temporary_total = _temporary_path(output_directory, "total_fea", ".npy")
    temporary_trained = _temporary_path(output_directory, "trained", ".index")
    temporary_added = _temporary_path(output_directory, "added", ".index")
    staged = (temporary_total, temporary_trained, temporary_added)
    try:
        with temporary_total.open("xb") as stream:
            runtime.numpy.save(stream, features, allow_pickle=False)
        _require_nonempty_regular_file(temporary_total, "staged total feature array")

        try:
            index = runtime.faiss.index_factory(dimension, f"IVF{n_ivf},Flat")
            index_ivf = runtime.faiss.extract_index_ivf(index)
            index_ivf.nprobe = nprobe
            index.train(features)
        except Exception as exc:
            raise IndexBuildError("FAISS IVF-Flat training failed") from exc
        if hasattr(index, "is_trained") and not bool(index.is_trained):
            raise IndexBuildError("FAISS index did not report a trained state")
        try:
            runtime.faiss.write_index(index, str(temporary_trained))
        except Exception as exc:
            raise IndexBuildError("cannot serialize trained FAISS index") from exc
        _require_nonempty_regular_file(temporary_trained, "staged trained index")

        try:
            for offset in range(0, indexed_rows, selected_limits.add_batch_rows):
                index.add(features[offset : offset + selected_limits.add_batch_rows])
        except Exception as exc:
            raise IndexBuildError("cannot add feature vectors to FAISS index") from exc
        if hasattr(index, "ntotal") and int(index.ntotal) != indexed_rows:
            raise IndexBuildError("FAISS index vector count does not match validated features")
        try:
            runtime.faiss.write_index(index, str(temporary_added))
        except Exception as exc:
            raise IndexBuildError("cannot serialize populated FAISS index") from exc
        _require_nonempty_regular_file(temporary_added, "staged populated index")

        os.replace(temporary_total, total_features)
        os.replace(temporary_trained, trained_index)
        os.replace(temporary_added, added_index)
    except IndexBuildError:
        raise
    except OSError as exc:
        raise IndexBuildError("cannot atomically publish index outputs") from exc
    finally:
        for path in staged:
            path.unlink(missing_ok=True)

    return IndexBuildResult(
        total_features=total_features,
        trained_index=trained_index,
        added_index=added_index,
        source_files=source_files,
        source_rows=source_rows,
        indexed_rows=indexed_rows,
        dimension=dimension,
        n_ivf=n_ivf,
        nprobe=nprobe,
        seed=seed,
        used_kmeans=used_kmeans,
    )


def _discover_feature_files(
    directory: Path, limits: IndexBuildLimits
) -> tuple[Path, ...]:
    candidates = sorted(
        (path for path in directory.iterdir() if path.suffix == ".npy"),
        key=lambda path: path.name,
    )
    if not candidates:
        raise IndexBuildError("feature directory contains no .npy arrays")
    if len(candidates) > limits.max_files:
        raise IndexBuildError("feature array count exceeds the configured limit")
    total_input_bytes = 0
    for path in candidates:
        _require_nonempty_regular_file(path, "feature array")
        size = path.stat(follow_symlinks=False).st_size
        if size > limits.max_file_bytes:
            raise IndexBuildError(f"feature array exceeds the per-file limit: {path.name}")
        total_input_bytes += size
        if total_input_bytes > limits.max_total_input_bytes:
            raise IndexBuildError("feature arrays exceed the total input byte limit")
    return tuple(candidates)


def _load_feature_arrays(
    paths: tuple[Path, ...],
    dimension: int,
    limits: IndexBuildLimits,
    numpy: Any,
) -> tuple[list[Any], int]:
    arrays: list[Any] = []
    total_rows = 0
    for path in paths:
        try:
            array = numpy.load(str(path), allow_pickle=False)
        except Exception as exc:
            raise IndexBuildError(f"cannot load safe NumPy feature array: {path.name}") from exc
        rows = _matrix_rows_with_dimension(array, dimension, path.name)
        if rows <= 0:
            raise IndexBuildError(f"feature array is empty: {path.name}")
        if rows > limits.max_rows_per_file:
            raise IndexBuildError(f"feature array row count exceeds limit: {path.name}")
        total_rows += rows
        if total_rows > limits.max_total_rows:
            raise IndexBuildError("feature array row count exceeds the total limit")
        if total_rows * dimension * 4 > limits.max_float32_bytes:
            raise IndexBuildError("float32 feature matrix exceeds the memory limit")
        try:
            if not bool(numpy.issubdtype(array.dtype, numpy.floating)):
                raise IndexBuildError(f"feature array must use a floating dtype: {path.name}")
            array = numpy.ascontiguousarray(array, dtype=numpy.float32)
        except IndexBuildError:
            raise
        except Exception as exc:
            raise IndexBuildError(f"cannot normalize feature dtype: {path.name}") from exc
        _validate_matrix(array, dimension, rows, numpy, path.name)
        arrays.append(array)
    return arrays, total_rows


def _validate_matrix(
    value: Any,
    dimension: int,
    expected_rows: int,
    numpy: Any,
    label: str,
) -> None:
    rows = _matrix_rows_with_dimension(value, dimension, label)
    if rows != expected_rows:
        raise IndexBuildError(f"{label} has an unexpected row count")
    try:
        finite = bool(numpy.isfinite(value).all())
    except Exception as exc:
        raise IndexBuildError(f"cannot inspect finite values in {label}") from exc
    if not finite:
        raise IndexBuildError(f"{label} contains NaN or infinity")


def _matrix_rows(value: Any) -> int:
    shape = getattr(value, "shape", None)
    if not isinstance(shape, tuple) or len(shape) != 2:
        raise IndexBuildError("feature data is not a two-dimensional matrix")
    return int(shape[0])


def _matrix_rows_with_dimension(value: Any, dimension: int, label: str) -> int:
    shape = getattr(value, "shape", None)
    if not isinstance(shape, tuple) or len(shape) != 2:
        raise IndexBuildError(f"{label} must be a two-dimensional matrix")
    try:
        rows = int(shape[0])
        columns = int(shape[1])
    except (TypeError, ValueError) as exc:
        raise IndexBuildError(f"{label} has an invalid shape") from exc
    if rows != shape[0] or columns != shape[1] or rows < 0:
        raise IndexBuildError(f"{label} has an invalid shape")
    if columns != dimension:
        raise IndexBuildError(f"{label} must have feature dimension {dimension}")
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint intended for execution by ``SafeSubprocessRunner``."""

    parser = argparse.ArgumentParser(
        prog="python -m rvc_worker.index_builder",
        description="Build and verify an RVC FAISS index",
        allow_abbrev=False,
    )
    parser.add_argument("--experiment-directory", required=True)
    parser.add_argument("--experiment-name", required=True)
    parser.add_argument(
        "--version",
        required=True,
        choices=tuple(item.value for item in RVCVersion),
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--cpu-workers", type=int, default=1)
    arguments = parser.parse_args(argv)
    try:
        result = build_rvc_index(
            Path(arguments.experiment_directory),
            arguments.experiment_name,
            arguments.version,
            seed=arguments.seed,
            cpu_workers=arguments.cpu_workers,
        )
    except IndexBuildError as exc:
        parser.exit(1, f"RVC index build failed: {exc}\n")
    print(
        json.dumps(
            {
                "total_features": str(result.total_features),
                "trained_index": str(result.trained_index),
                "added_index": str(result.added_index),
                "source_files": [str(path) for path in result.source_files],
                "source_rows": result.source_rows,
                "indexed_rows": result.indexed_rows,
                "dimension": result.dimension,
                "n_ivf": result.n_ivf,
                "nprobe": result.nprobe,
                "seed": result.seed,
                "used_kmeans": result.used_kmeans,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _load_core_dependencies() -> IndexDependencies:
    try:
        numpy = importlib.import_module("numpy")
        faiss = importlib.import_module("faiss")
    except ImportError as exc:
        raise IndexBuildError(
            "RVC index runtime requires NumPy and FAISS; install them in the pinned runtime"
        ) from exc
    return IndexDependencies(numpy=numpy, faiss=faiss)


def _load_mini_batch_kmeans() -> Any:
    try:
        cluster = importlib.import_module("sklearn.cluster")
        return cluster.MiniBatchKMeans
    except (ImportError, AttributeError) as exc:
        raise IndexBuildError(
            "feature sets above 200,000 rows require scikit-learn MiniBatchKMeans"
        ) from exc


def _parse_version(version: RVCVersion | str) -> RVCVersion:
    try:
        return RVCVersion(version)
    except ValueError as exc:
        raise IndexBuildError("index version must be v1 or v2") from exc


def _validate_experiment_name(value: str) -> None:
    if not _EXPERIMENT_NAME.fullmatch(value):
        raise IndexBuildError("experiment name is not a safe RVC path component")


def _validate_seed(seed: int) -> None:
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= _UINT32_MAX:
        raise IndexBuildError("seed must be an unsigned 32-bit integer")


def _require_directory(path: Path, label: str) -> None:
    _require_absolute_nul_free(path, label)
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as exc:
        raise IndexBuildError(f"{label} does not exist") from exc
    if path.is_symlink() or not stat.S_ISDIR(mode):
        raise IndexBuildError(f"{label} must be a non-symlink directory")


def _require_nonempty_regular_file(path: Path, label: str) -> None:
    try:
        file_stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise IndexBuildError(f"{label} is missing") from exc
    if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
        raise IndexBuildError(f"{label} must be a regular non-symlink file")
    if file_stat.st_size <= 0:
        raise IndexBuildError(f"{label} must not be empty")


def _require_safe_destination(path: Path) -> None:
    _require_absolute_nul_free(path, "index destination")
    if path.exists() or path.is_symlink():
        _require_nonempty_regular_file(path, "existing index destination")


def _require_absolute_nul_free(path: Path, label: str) -> None:
    rendered = str(path)
    if (
        not path.is_absolute()
        or "\x00" in rendered
        or path != Path(os.path.abspath(rendered))
    ):
        raise IndexBuildError(f"{label} must be an absolute, normalized, NUL-free path")


def _temporary_path(directory: Path, label: str, suffix: str) -> Path:
    return directory / f".rvc-orchestrator-{label}-{os.getpid()}-{uuid4().hex}{suffix}"


if __name__ == "__main__":
    raise SystemExit(main())
