"""Worker-side verified artifact upload wire models and local discovery."""

from __future__ import annotations

import heapq
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from rvc_orchestrator_contracts import ArtifactType, ContractModel, JobClaim

from .artifacts import sha256_file
from .workspace import JobWorkspace

_CHECKPOINT_NAME = re.compile(r"^[GD]_(?P<epoch>[0-9]+)\.pth$")


class ArtifactUploadInitRequest(ContractModel):
    lease_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)
    artifact_type: ArtifactType
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=3, max_length=255)
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactUploadFinalizeRequest(ContractModel):
    lease_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)


class PublishedArtifact(ContractModel):
    id: str
    job_id: str
    attempt_id: str
    artifact_type: ArtifactType
    filename: str
    size_bytes: int
    sha256: str
    mime_type: str | None
    metadata_json: dict[str, Any]
    created_at: datetime


class ArtifactUploadInitResponse(ContractModel):
    upload_session_id: str
    status: Literal["pending", "finalizing", "completed", "failed", "expired"]
    method: Literal["PUT"] | None = None
    upload_url: str | None = None
    upload_headers: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime
    artifact: PublishedArtifact | None = None
    failure_code: str | None = None
    retryable: bool = False
    retry_after_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ArtifactUploadCandidate:
    artifact_type: ArtifactType
    path: Path
    relative_path: str
    size_bytes: int
    sha256: str
    content_type: str
    metadata: dict[str, Any]
    idempotency_discriminator: str | None = None

    def init_request(self, claim: JobClaim) -> ArtifactUploadInitRequest:
        return ArtifactUploadInitRequest(
            lease_id=claim.lease_id,
            attempt_id=claim.attempt_id,
            idempotency_key=_upload_idempotency_key(
                claim.attempt_id,
                self.artifact_type,
                self.sha256,
                self.idempotency_discriminator,
            ),
            artifact_type=self.artifact_type,
            filename=self.path.name,
            content_type=self.content_type,
            size_bytes=self.size_bytes,
            sha256=self.sha256,
            metadata=self.metadata,
        )


def collect_artifact_candidates(
    claim: JobClaim,
    workspace: JobWorkspace,
    *,
    is_fake: bool,
    max_object_bytes: int = 5 * 1024**3,
    max_files: int = 256,
    max_total_bytes: int = 100 * 1024**3,
    checkpoint_retention: int = 20,
) -> tuple[ArtifactUploadCandidate, ...]:
    """Collect only configured, regular, non-empty files within the job workspace."""

    config = claim.config
    outputs = workspace.outputs
    candidates: list[tuple[ArtifactType, Path]] = []

    def add(artifact_type: ArtifactType, path: Path, *, enabled: bool = True) -> None:
        if enabled and path.is_file() and not path.is_symlink():
            candidates.append((artifact_type, path))
            if len(candidates) > max_files:
                raise RuntimeError("artifact file count exceeds the Worker attempt quota")

    add(
        ArtifactType.FINAL_SMALL_MODEL,
        outputs / "model/final_small_model.pth",
        enabled=config.artifacts.collect_small_model,
    )
    add(
        ArtifactType.FINAL_INDEX,
        outputs / "index/final.index",
        enabled=(
            config.index.build_index
            and config.index.collect_added_index
            and config.artifacts.collect_index
        ),
    )
    add(ArtifactType.ENVIRONMENT, outputs / "environment.json")
    add(ArtifactType.CONFIG, outputs / "config.json")
    add(ArtifactType.DATASET_REPORT, outputs / "dataset_report.json")

    sample_root = outputs / "samples"
    if config.artifacts.collect_samples and sample_root.is_dir():
        for path in sorted(sample_root.iterdir(), key=lambda item: item.name):
            if path.suffix.lower() in {".wav", ".flac", ".mp3"}:
                add(ArtifactType.SAMPLE, path)

    experiment_logs = workspace.work / "rvc" / "logs" / config.job_name
    add(
        ArtifactType.TRAIN_LOG,
        experiment_logs / "train.log",
        enabled=config.artifacts.collect_logs,
    )
    add(
        ArtifactType.TOTAL_FEATURES,
        experiment_logs / "total_fea.npy",
        enabled=(
            config.index.build_index
            and config.index.collect_total_fea
            and config.artifacts.collect_index
        ),
    )
    if config.artifacts.collect_tensorboard and experiment_logs.is_dir():
        tensorboard_paths: list[Path] = []
        for path in experiment_logs.rglob("events.out.tfevents.*"):
            if path.is_file() and not path.is_symlink():
                tensorboard_paths.append(path)
                if len(candidates) + len(tensorboard_paths) > max_files:
                    raise RuntimeError("artifact file count exceeds the Worker attempt quota")
        for path in sorted(tensorboard_paths):
            add(ArtifactType.TENSORBOARD, path)
    if config.artifacts.collect_checkpoints and experiment_logs.is_dir():
        for path in _retained_checkpoints(experiment_logs, "G_*.pth", checkpoint_retention):
            add(ArtifactType.GENERATOR_CHECKPOINT, path)
        for path in _retained_checkpoints(experiment_logs, "D_*.pth", checkpoint_retention):
            add(ArtifactType.DISCRIMINATOR_CHECKPOINT, path)

    required: set[ArtifactType] = set()
    if config.artifacts.collect_small_model:
        required.add(ArtifactType.FINAL_SMALL_MODEL)
    if (
        config.index.build_index
        and config.index.collect_added_index
        and config.artifacts.collect_index
    ):
        required.add(ArtifactType.FINAL_INDEX)
    present = {artifact_type for artifact_type, _ in candidates}
    if missing := required - present:
        names = ", ".join(sorted(item.value for item in missing))
        raise RuntimeError(f"required artifacts are missing before upload: {names}")

    result: list[ArtifactUploadCandidate] = []
    unique: set[tuple[ArtifactType, str, str | None]] = set()
    total_bytes = 0
    for artifact_type, path in candidates:
        resolved = workspace.assert_path(path)
        size = resolved.stat().st_size
        if size <= 0:
            raise RuntimeError(f"artifact is empty: {artifact_type.value}/{path.name}")
        if size > max_object_bytes:
            raise RuntimeError(
                f"artifact exceeds the Worker object size quota: {artifact_type.value}/{path.name}"
            )
        total_bytes += size
        if total_bytes > max_total_bytes:
            raise RuntimeError("artifact bytes exceed the Worker attempt quota")
        digest = sha256_file(resolved)
        relative_path = resolved.relative_to(workspace.root).as_posix()
        discriminator = relative_path if artifact_type is ArtifactType.SAMPLE else None
        identity = (artifact_type, digest, discriminator)
        if identity in unique:
            raise RuntimeError(
                "duplicate artifact type/checksum pair cannot be published safely: "
                f"{artifact_type.value}/{path.name}"
            )
        unique.add(identity)
        result.append(
            ArtifactUploadCandidate(
                artifact_type=artifact_type,
                path=resolved,
                relative_path=relative_path,
                size_bytes=size,
                sha256=digest,
                content_type=_artifact_content_type(artifact_type, resolved),
                metadata={
                    "source_relative_path": relative_path,
                    "runner_fake": is_fake,
                },
                idempotency_discriminator=discriminator,
            )
        )
    return tuple(result)


def _retained_checkpoints(root: Path, pattern: str, retention: int) -> list[Path]:
    if retention <= 0:
        return []
    latest: list[tuple[int, str, Path]] = []
    for path in root.glob(pattern):
        if path.is_symlink() or not path.is_file():
            continue
        match = _CHECKPOINT_NAME.fullmatch(path.name)
        if match is None:
            continue
        entry = (int(match.group("epoch")), path.name, path)
        if len(latest) < retention:
            heapq.heappush(latest, entry)
        elif entry > latest[0]:
            heapq.heapreplace(latest, entry)
    return [entry[2] for entry in sorted(latest)]


def _artifact_content_type(artifact_type: ArtifactType, path: Path) -> str:
    if artifact_type in {
        ArtifactType.FINAL_SMALL_MODEL,
        ArtifactType.GENERATOR_CHECKPOINT,
        ArtifactType.DISCRIMINATOR_CHECKPOINT,
    }:
        return "application/x-pytorch"
    if artifact_type is ArtifactType.TRAIN_LOG:
        return "text/plain"
    if artifact_type in {
        ArtifactType.ENVIRONMENT,
        ArtifactType.CONFIG,
        ArtifactType.DATASET_REPORT,
    }:
        return "application/json"
    if artifact_type is ArtifactType.SAMPLE:
        return {
            ".wav": "audio/wav",
            ".flac": "audio/flac",
            ".mp3": "audio/mpeg",
        }[path.suffix.lower()]
    return "application/octet-stream"


def _upload_idempotency_key(
    attempt_id: str,
    artifact_type: ArtifactType,
    digest: str,
    discriminator: str | None,
) -> str:
    from hashlib import sha256

    value = "\x1f".join(
        (
            "artifact-upload",
            attempt_id,
            artifact_type.value,
            digest,
            discriminator or "",
        )
    )
    return sha256(value.encode()).hexdigest()
