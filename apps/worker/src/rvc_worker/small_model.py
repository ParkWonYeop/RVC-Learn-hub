"""Isolated wrapper for RVC's official checkpoint-to-small-model extraction.

This module never treats a training checkpoint as a deployable model and never copies
one into place.  It imports and calls the reviewed upstream
``infer.lib.train.process_ckpt.extract_small_model`` function inside the CLI process,
validates the serialized metadata, and only then atomically publishes the result.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from rvc_orchestrator_contracts import RVCVersion, SampleRate

from .rvc_commands import RVC_REVIEWED_COMMIT


class SmallModelExtractionError(RuntimeError):
    """Raised when the official extractor or its output fails verification."""


OfficialExtractor = Callable[[str, str, str, str, str, str], object]
MetadataLoader = Callable[[Path], object]
RevisionReader = Callable[[Path], str]

_EXPERIMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MAX_INFO_LENGTH = 4_096
_OFFICIAL_MODULE = "infer.lib.train.process_ckpt"
_OFFICIAL_SOURCE = Path("infer/lib/train/process_ckpt.py")


@dataclass(frozen=True, slots=True)
class SmallModelRuntime:
    """Injectable official call and deserializer for dependency-free unit tests."""

    extractor: OfficialExtractor
    metadata_loader: MetadataLoader


@dataclass(frozen=True, slots=True)
class SmallModelResult:
    """Verified small-model output metadata safe to put in an artifact manifest."""

    output: Path
    size_bytes: int
    sha256: str
    source_checkpoint_sha256: str
    experiment_name: str
    sample_rate: str
    version: str
    use_f0: bool
    info: str
    upstream_result: str
    repository_commit: str


def build_small_model_command(
    python_executable: str,
    repository_root: Path,
    checkpoint: Path,
    output: Path,
    experiment_name: str,
    sample_rate: SampleRate | str,
    use_f0: bool,
    version: RVCVersion | str,
    *,
    info: str = "Extracted model.",
    expected_commit: str = RVC_REVIEWED_COMMIT,
    allow_reviewed_projection: bool = False,
) -> tuple[str, ...]:
    """Build shell-free argv for running extraction in a dedicated Python process."""

    if not python_executable or "\x00" in python_executable:
        raise SmallModelExtractionError("python executable must be a non-empty NUL-free value")
    _validate_use_f0(use_f0)
    parsed_sample_rate = _parse_sample_rate(sample_rate)
    parsed_version = _parse_version(version)
    _validate_common_arguments(
        repository_root,
        checkpoint,
        output,
        experiment_name,
        info,
        expected_commit,
        require_existing_paths=False,
    )
    argv = (
        python_executable,
        "-m",
        "rvc_worker.small_model",
        "--repository-root",
        str(repository_root),
        "--checkpoint",
        str(checkpoint),
        "--output",
        str(output),
        "--experiment-name",
        experiment_name,
        "--sample-rate",
        parsed_sample_rate.value,
        "--use-f0",
        "1" if use_f0 else "0",
        "--version",
        parsed_version.value,
        "--info",
        info,
        "--expected-commit",
        expected_commit,
    )
    if allow_reviewed_projection:
        return (*argv, "--allow-reviewed-projection")
    return argv


def extract_small_model(
    repository_root: Path,
    checkpoint: Path,
    output: Path,
    experiment_name: str,
    sample_rate: SampleRate | str,
    use_f0: bool,
    version: RVCVersion | str,
    *,
    info: str = "Extracted model.",
    expected_commit: str = RVC_REVIEWED_COMMIT,
    runtime: SmallModelRuntime | None = None,
    revision_reader: RevisionReader | None = None,
    allow_reviewed_projection: bool = False,
) -> SmallModelResult:
    """Call the official extractor, verify its payload, and atomically move it to output."""

    _validate_use_f0(use_f0)
    parsed_sample_rate = _parse_sample_rate(sample_rate)
    parsed_version = _parse_version(version)
    expected_info = info or "Extracted model."
    _validate_common_arguments(
        repository_root,
        checkpoint,
        output,
        experiment_name,
        info,
        expected_commit,
        require_existing_paths=True,
    )
    selected_revision_reader = revision_reader
    if selected_revision_reader is None:
        selected_revision_reader = (
            _read_reviewed_projection_revision if allow_reviewed_projection else _read_git_revision
        )
    actual_commit = selected_revision_reader(repository_root).strip().lower()
    if actual_commit != expected_commit:
        raise SmallModelExtractionError(
            "RVC repository revision does not match the reviewed commit"
        )

    source_digest = _sha256_file(checkpoint)
    internal_name = f"orchestrator_{uuid4().hex}"
    staged_output = repository_root / "assets" / "weights" / f"{internal_name}.pth"
    publication_stage = output.parent / f".{output.name}.{uuid4().hex}.tmp"
    _require_safe_new_stage(staged_output)
    _require_safe_new_stage(publication_stage)
    selected_runtime = runtime
    try:
        with _repository_context(repository_root):
            selected_runtime = selected_runtime or _load_official_runtime(repository_root)
            upstream_result = selected_runtime.extractor(
                str(checkpoint),
                internal_name,
                parsed_sample_rate.value,
                "1" if use_f0 else "0",
                info,
                parsed_version.value,
            )
            if upstream_result != "Success.":
                raise SmallModelExtractionError(
                    "official RVC small-model extractor did not report success"
                )
            _require_nonempty_regular_file(staged_output, "official small-model output")
            metadata = selected_runtime.metadata_loader(staged_output)
        _validate_metadata(
            metadata,
            sample_rate=parsed_sample_rate,
            version=parsed_version,
            use_f0=use_f0,
            info=expected_info,
        )
        extracted_digest = _sha256_file(staged_output)
        if extracted_digest == source_digest:
            raise SmallModelExtractionError(
                "official extraction output is byte-identical to the training checkpoint"
            )
        try:
            with staged_output.open("rb") as source, publication_stage.open("xb") as destination:
                while chunk := source.read(1024 * 1024):
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            _require_nonempty_regular_file(publication_stage, "verified publication stage")
            if _sha256_file(publication_stage) != extracted_digest:
                raise SmallModelExtractionError(
                    "small-model publication stage checksum does not match extraction output"
                )
            os.replace(publication_stage, output)
        except OSError as exc:
            raise SmallModelExtractionError(
                "cannot atomically publish the verified small model"
            ) from exc
        _require_nonempty_regular_file(output, "published small model")
    finally:
        staged_output.unlink(missing_ok=True)
        publication_stage.unlink(missing_ok=True)

    output_stat = output.stat(follow_symlinks=False)
    return SmallModelResult(
        output=output,
        size_bytes=output_stat.st_size,
        sha256=_sha256_file(output),
        source_checkpoint_sha256=source_digest,
        experiment_name=experiment_name,
        sample_rate=parsed_sample_rate.value,
        version=parsed_version.value,
        use_f0=use_f0,
        info=expected_info,
        upstream_result="Success.",
        repository_commit=actual_commit,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint intended for ``SafeSubprocessRunner`` execution."""

    parser = _argument_parser()
    arguments = parser.parse_args(argv)
    try:
        result = extract_small_model(
            repository_root=Path(arguments.repository_root),
            checkpoint=Path(arguments.checkpoint),
            output=Path(arguments.output),
            experiment_name=arguments.experiment_name,
            sample_rate=arguments.sample_rate,
            use_f0=arguments.use_f0 == "1",
            version=arguments.version,
            info=arguments.info,
            expected_commit=arguments.expected_commit,
            allow_reviewed_projection=arguments.allow_reviewed_projection,
        )
    except SmallModelExtractionError as exc:
        parser.exit(1, f"small model extraction failed: {exc}\n")
    payload = asdict(result)
    payload["output"] = str(result.output)
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rvc_worker.small_model",
        description="Extract and verify an RVC deployable small model",
    )
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--experiment-name", required=True)
    parser.add_argument(
        "--sample-rate",
        required=True,
        choices=tuple(rate.value for rate in SampleRate),
    )
    parser.add_argument("--use-f0", required=True, choices=("0", "1"))
    parser.add_argument(
        "--version",
        required=True,
        choices=tuple(item.value for item in RVCVersion),
    )
    parser.add_argument("--info", default="Extracted model.")
    parser.add_argument(
        "--expected-commit",
        default=RVC_REVIEWED_COMMIT,
        choices=(RVC_REVIEWED_COMMIT,),
    )
    parser.add_argument("--allow-reviewed-projection", action="store_true")
    return parser


def _load_official_runtime(repository_root: Path) -> SmallModelRuntime:
    try:
        module = importlib.import_module(_OFFICIAL_MODULE)
    except Exception as exc:
        raise SmallModelExtractionError("cannot import the official RVC checkpoint module") from exc
    module_file_raw = getattr(module, "__file__", None)
    if not isinstance(module_file_raw, str):
        raise SmallModelExtractionError("official RVC checkpoint module has no source path")
    expected_source = (repository_root / _OFFICIAL_SOURCE).resolve(strict=True)
    if Path(module_file_raw).resolve(strict=True) != expected_source:
        raise SmallModelExtractionError(
            "loaded checkpoint extractor is outside the pinned RVC tree"
        )
    extractor = getattr(module, "extract_small_model", None)
    torch = getattr(module, "torch", None)
    if not callable(extractor) or torch is None or not callable(getattr(torch, "load", None)):
        raise SmallModelExtractionError("official RVC checkpoint module has an invalid API")

    def metadata_loader(path: Path) -> object:
        return torch.load(str(path), map_location="cpu", weights_only=True)

    return SmallModelRuntime(extractor=extractor, metadata_loader=metadata_loader)


def _validate_metadata(
    metadata: object,
    *,
    sample_rate: SampleRate,
    version: RVCVersion,
    use_f0: bool,
    info: str,
) -> None:
    if not isinstance(metadata, Mapping):
        raise SmallModelExtractionError("extracted small model metadata must be a mapping")
    weight = metadata.get("weight")
    config = metadata.get("config")
    if not isinstance(weight, Mapping) or not weight:
        raise SmallModelExtractionError("extracted small model has no deployable weights")
    if any(not isinstance(key, str) or "enc_q" in key for key in weight):
        raise SmallModelExtractionError("extracted small model contains invalid weight keys")
    if not isinstance(config, (list, tuple)) or len(config) != 18:
        raise SmallModelExtractionError("extracted small model has an invalid inference config")
    expected_sample_rate_hz = 40_000 if sample_rate is SampleRate.KHZ_40 else 48_000
    config_sample_rate = config[-1]
    if (
        isinstance(config_sample_rate, bool)
        or not isinstance(config_sample_rate, int)
        or config_sample_rate != expected_sample_rate_hz
    ):
        raise SmallModelExtractionError(
            "extracted small model inference config has the wrong sample rate"
        )
    if metadata.get("version") != version.value:
        raise SmallModelExtractionError("extracted small model version metadata does not match")
    if metadata.get("sr") != sample_rate.value:
        raise SmallModelExtractionError("extracted small model sample-rate metadata does not match")
    f0_value = metadata.get("f0")
    if isinstance(f0_value, bool) or f0_value != int(use_f0):
        raise SmallModelExtractionError("extracted small model F0 metadata does not match")
    if metadata.get("info") != info:
        raise SmallModelExtractionError("extracted small model info metadata does not match")


def _validate_common_arguments(
    repository_root: Path,
    checkpoint: Path,
    output: Path,
    experiment_name: str,
    info: str,
    expected_commit: str,
    *,
    require_existing_paths: bool,
) -> None:
    for path, label in (
        (repository_root, "RVC repository root"),
        (checkpoint, "generator checkpoint"),
        (output, "small-model output"),
    ):
        _require_absolute_nul_free(path, label)
    if not _EXPERIMENT_NAME.fullmatch(experiment_name):
        raise SmallModelExtractionError("experiment name is not a safe RVC path component")
    if "\x00" in info or len(info) > _MAX_INFO_LENGTH:
        raise SmallModelExtractionError("model info must be NUL-free and at most 4096 characters")
    normalized_commit = expected_commit.strip().lower()
    if not _COMMIT.fullmatch(normalized_commit) or normalized_commit != expected_commit:
        raise SmallModelExtractionError("expected RVC commit must be 40 lowercase hex characters")
    if expected_commit != RVC_REVIEWED_COMMIT:
        raise SmallModelExtractionError("RVC commit has not been reviewed by this Worker build")
    if checkpoint.suffix != ".pth" or output.suffix != ".pth":
        raise SmallModelExtractionError("checkpoint and small-model output must use .pth")
    if checkpoint == output:
        raise SmallModelExtractionError("small-model output cannot overwrite its source checkpoint")
    if not require_existing_paths:
        return
    _require_directory(repository_root, "RVC repository root")
    _require_nonempty_regular_file(
        repository_root / _OFFICIAL_SOURCE,
        "official RVC checkpoint source",
    )
    _require_directory(repository_root / "assets" / "weights", "RVC weights directory")
    _require_nonempty_regular_file(checkpoint, "generator checkpoint")
    _require_directory(output.parent, "small-model output parent")
    if output.exists() or output.is_symlink():
        _require_nonempty_regular_file(output, "existing small-model output")


def _read_git_revision(repository_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmallModelExtractionError("cannot verify the RVC repository revision") from exc
    revision = result.stdout.strip().lower()
    if result.returncode != 0 or not _COMMIT.fullmatch(revision):
        raise SmallModelExtractionError("cannot verify the RVC repository revision")
    return revision


def _read_reviewed_projection_revision(repository_root: Path) -> str:
    """Read the private projection marker created by ``PinnedRvcRunner``.

    This is opt-in because a marker alone is not a substitute for validating an
    arbitrary checkout.  The typed runner validates the shared source revision and
    hashes every projected input before it starts this isolated CLI process.
    """

    projection_marker = repository_root / ".orchestrator-projection.json"
    revision_marker = repository_root / ".rvc-reviewed-commit"
    _require_nonempty_regular_file(projection_marker, "RVC projection manifest")
    _require_nonempty_regular_file(revision_marker, "RVC projection revision marker")
    try:
        projection = json.loads(projection_marker.read_text(encoding="utf-8"))
        revision = revision_marker.read_text(encoding="ascii").strip().lower()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SmallModelExtractionError("cannot read reviewed RVC projection markers") from exc
    if (
        not isinstance(projection, Mapping)
        or projection.get("schema_version") != 1
        or projection.get("rvc_commit_hash") != RVC_REVIEWED_COMMIT
        or revision != RVC_REVIEWED_COMMIT
    ):
        raise SmallModelExtractionError("reviewed RVC projection revision does not match")
    return revision


@contextmanager
def _repository_context(repository_root: Path) -> Iterator[None]:
    previous_directory = Path.cwd()
    rendered_root = str(repository_root)
    inserted = rendered_root not in sys.path
    if inserted:
        sys.path.insert(0, rendered_root)
    try:
        os.chdir(repository_root)
        yield
    finally:
        os.chdir(previous_directory)
        if inserted:
            try:
                sys.path.remove(rendered_root)
            except ValueError:
                pass


def _require_safe_new_stage(path: Path) -> None:
    _require_absolute_nul_free(path, "small-model staging path")
    if path.exists() or path.is_symlink():
        raise SmallModelExtractionError("small-model staging path already exists")


def _require_directory(path: Path, label: str) -> None:
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as exc:
        raise SmallModelExtractionError(f"{label} does not exist") from exc
    if path.is_symlink() or not stat.S_ISDIR(mode):
        raise SmallModelExtractionError(f"{label} must be a non-symlink directory")


def _require_nonempty_regular_file(path: Path, label: str) -> None:
    try:
        file_stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SmallModelExtractionError(f"{label} is missing") from exc
    if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
        raise SmallModelExtractionError(f"{label} must be a regular non-symlink file")
    if file_stat.st_size <= 0:
        raise SmallModelExtractionError(f"{label} must not be empty")


def _require_absolute_nul_free(path: Path, label: str) -> None:
    rendered = str(path)
    if not path.is_absolute() or "\x00" in rendered or path != Path(os.path.abspath(rendered)):
        raise SmallModelExtractionError(f"{label} must be an absolute, normalized, NUL-free path")


def _validate_use_f0(value: bool) -> None:
    if not isinstance(value, bool):
        raise SmallModelExtractionError("use_f0 must be a boolean")


def _parse_sample_rate(value: SampleRate | str) -> SampleRate:
    try:
        return SampleRate(value)
    except ValueError as exc:
        raise SmallModelExtractionError("small-model sample rate must be 40k or 48k") from exc


def _parse_version(value: RVCVersion | str) -> RVCVersion:
    try:
        return RVCVersion(value)
    except ValueError as exc:
        raise SmallModelExtractionError("small-model version must be v1 or v2") from exc


def _sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
