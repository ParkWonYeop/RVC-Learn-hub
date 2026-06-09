"""Strict native inference Artifact publication and central Sample registration."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace

from pydantic import ValidationError

from rvc_orchestrator_contracts import (
    RVC_REVIEWED_COMMIT,
    SAMPLE_MAX_TOTAL_OUTPUT_BYTES,
    SAMPLE_MAX_TOTAL_OUTPUT_DURATION_SECONDS,
    ArtifactType,
    InferenceF0Method,
    JobClaim,
    SampleMetricValues,
    SampleRead,
    SampleRegistrationRequest,
)

from .native_inference import (
    NativeInferencePublication,
    NativeInferencePublishedFile,
    NativeInferencePublishedSample,
)
from .runner import RvcRuntimeIntegrityError
from .uploads import ArtifactUploadCandidate, PublishedArtifact
from .workspace import JobWorkspace

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_SAMPLE_ITEMS = 128
_MAX_SAMPLE_BYTES = 256 * 1024**2
_MAX_SAMPLE_DURATION_SECONDS = 600.0
_MAX_SAMPLE_CHANNELS = 2
_MAX_ARTIFACT_METADATA_BYTES = 16 * 1024


class NativeSamplePublicationError(RvcRuntimeIntegrityError):
    """Publication evidence no longer matches the current immutable claim."""


@dataclass(frozen=True, slots=True)
class NativeSamplePublicationPlan:
    publication: NativeInferencePublication
    candidates: tuple[ArtifactUploadCandidate, ...]
    upload_candidates: tuple[ArtifactUploadCandidate, ...]
    model_candidate: ArtifactUploadCandidate
    index_candidate: ArtifactUploadCandidate | None
    sample_candidates: tuple[ArtifactUploadCandidate, ...]
    sample_canonical_relative_paths: tuple[str, ...]


def prepare_native_sample_publication(
    claim: JobClaim,
    workspace: JobWorkspace,
    publication: NativeInferencePublication,
    candidates: tuple[ArtifactUploadCandidate, ...],
) -> NativeSamplePublicationPlan:
    """Re-bind a strict loader result to claim and upload candidates."""

    transfer = claim.test_set_transfer
    if not claim.config.auto_inference_samples.enabled or transfer is None:
        raise NativeSamplePublicationError("sample publication requires a TestSet claim")
    expected_commit = claim.config.rvc_backend.rvc_commit_hash or RVC_REVIEWED_COMMIT
    expected_rate = transfer.inference_config.resample_sr or (
        40_000 if claim.config.model.sample_rate.value == "40k" else 48_000
    )
    if (
        publication.job_id != claim.job_id
        or publication.attempt_id != claim.attempt_id
        or publication.test_set_id != transfer.test_set_id
        or publication.family_id != transfer.family_id
        or publication.revision != transfer.revision
        or publication.test_set_manifest_sha256 != transfer.manifest_sha256
        or publication.sample_plan_sha256 != transfer.sample_plan_sha256
        or publication.inference_config_sha256 != transfer.inference_config_sha256
        or _SHA256.fullmatch(publication.inference_request_sha256) is None
        or publication.inference_f0_method != transfer.inference_config.inference_f0_method.value
        or publication.metrics_algorithm != "pcm-normalized-v2"
        or publication.rvc_commit_hash != expected_commit
        or expected_commit != RVC_REVIEWED_COMMIT
        or _IMAGE_DIGEST.fullmatch(publication.runtime_image_digest) is None
        or _SHA256.fullmatch(publication.runtime_asset_manifest_sha256) is None
        or len(publication.samples) != len(transfer.items)
        or not 1 <= len(publication.samples) <= _MAX_SAMPLE_ITEMS
    ):
        raise NativeSamplePublicationError("native sample publication identity is invalid")

    expected_manifest_path = workspace.outputs / "samples" / "inference_manifest.json"
    expected_model_path = workspace.outputs / "model" / "final_small_model.pth"
    _require_published_file(
        publication.manifest,
        path=expected_manifest_path,
        relative_path="outputs/samples/inference_manifest.json",
    )
    _require_published_file(
        publication.model,
        path=expected_model_path,
        relative_path="outputs/model/final_small_model.pth",
    )
    if transfer.inference_config.index_rate == 0:
        if publication.index is not None:
            raise NativeSamplePublicationError("no-index publication contains an index")
    else:
        if publication.index is None:
            raise NativeSamplePublicationError("sample publication is missing its index")
        _require_published_file(
            publication.index,
            path=workspace.outputs / "index" / "final.index",
            relative_path="outputs/index/final.index",
        )

    for expected_item, sample in zip(transfer.items, publication.samples, strict=True):
        _require_published_file(
            sample.output,
            path=workspace.outputs / "samples" / f"{expected_item.test_set_item_id}.wav",
            relative_path=f"outputs/samples/{expected_item.test_set_item_id}.wav",
        )
        metrics = _sample_metrics(sample)
        if (
            sample.test_set_item_id != expected_item.test_set_item_id
            or sample.item_key != expected_item.item_key
            or sample.sort_order != expected_item.sort_order
            or sample.input_sha256 != expected_item.sha256
            or sample.output.size_bytes > _MAX_SAMPLE_BYTES
            or sample.output_sample_rate_hz != expected_rate
            or not 1 <= sample.output_channels <= _MAX_SAMPLE_CHANNELS
            or sample.output_sample_width_bytes not in {1, 2, 3, 4}
            or sample.output_frame_count <= 0
            or not math.isfinite(sample.output_duration_seconds)
            or not 0 < sample.output_duration_seconds <= _MAX_SAMPLE_DURATION_SECONDS
            or not math.isclose(
                sample.output_duration_seconds,
                sample.output_frame_count / sample.output_sample_rate_hz,
                rel_tol=0,
                abs_tol=max(1 / sample.output_sample_rate_hz, 1e-9),
            )
            or len(metrics.model_dump_json().encode("utf-8")) > 1024
        ):
            raise NativeSamplePublicationError("native sample item evidence is invalid")

    if (
        sum(sample.output.size_bytes for sample in publication.samples)
        > SAMPLE_MAX_TOTAL_OUTPUT_BYTES
        or math.fsum(sample.output_duration_seconds for sample in publication.samples)
        > SAMPLE_MAX_TOTAL_OUTPUT_DURATION_SECONDS
    ):
        raise NativeSamplePublicationError("native sample publication exceeds total limits")

    by_relative: dict[str, ArtifactUploadCandidate] = {}
    for candidate in candidates:
        if candidate.relative_path in by_relative:
            raise NativeSamplePublicationError("artifact candidate path is duplicated")
        by_relative[candidate.relative_path] = candidate
    model_candidate = _candidate_for_file(
        by_relative,
        publication.model,
        ArtifactType.FINAL_SMALL_MODEL,
        "application/x-pytorch",
    )
    index_candidate = (
        _candidate_for_file(
            by_relative,
            publication.index,
            ArtifactType.FINAL_INDEX,
            "application/octet-stream",
        )
        if publication.index is not None
        else None
    )
    sample_candidates = tuple(
        _candidate_for_file(
            by_relative,
            sample.output,
            ArtifactType.SAMPLE,
            "audio/wav",
        )
        for sample in publication.samples
    )
    actual_sample_paths = {
        candidate.relative_path
        for candidate in candidates
        if candidate.artifact_type is ArtifactType.SAMPLE
    }
    if actual_sample_paths != {candidate.relative_path for candidate in sample_candidates}:
        raise NativeSamplePublicationError("sample Artifact inventory is not exact")

    common_metadata: dict[str, object] = {
        "rvc_commit_hash": publication.rvc_commit_hash,
        "runtime_image_digest": publication.runtime_image_digest,
        "runtime_asset_manifest_sha256": publication.runtime_asset_manifest_sha256,
        "native_inference_manifest_sha256": publication.manifest.sha256,
        "native_inference_request_sha256": publication.inference_request_sha256,
    }
    replacements: dict[str, ArtifactUploadCandidate] = {}
    replacements[model_candidate.relative_path] = _with_metadata(
        model_candidate,
        common_metadata,
        role="sample_model",
    )
    if index_candidate is not None:
        replacements[index_candidate.relative_path] = _with_metadata(
            index_candidate,
            common_metadata,
            role="sample_index",
        )
    for candidate in sample_candidates:
        replacements[candidate.relative_path] = _with_metadata(
            candidate,
            common_metadata,
            role="sample_output",
        )

    enriched = tuple(replacements.get(item.relative_path, item) for item in candidates)
    enriched_by_relative = {item.relative_path: item for item in enriched}
    enriched_sample_candidates = tuple(
        enriched_by_relative[item.relative_path] for item in sample_candidates
    )
    canonical_sample_path_by_sha256: dict[str, str] = {}
    sample_canonical_relative_paths = tuple(
        canonical_sample_path_by_sha256.setdefault(item.sha256, item.relative_path)
        for item in enriched_sample_candidates
    )
    canonical_sample_paths = set(sample_canonical_relative_paths)
    return NativeSamplePublicationPlan(
        publication=publication,
        candidates=enriched,
        upload_candidates=tuple(
            item
            for item in enriched
            if item.artifact_type is not ArtifactType.SAMPLE
            or item.relative_path in canonical_sample_paths
        ),
        model_candidate=enriched_by_relative[model_candidate.relative_path],
        index_candidate=(
            enriched_by_relative[index_candidate.relative_path]
            if index_candidate is not None
            else None
        ),
        sample_candidates=enriched_sample_candidates,
        sample_canonical_relative_paths=sample_canonical_relative_paths,
    )


def validate_finalized_artifact(
    claim: JobClaim,
    candidate: ArtifactUploadCandidate,
    artifact: PublishedArtifact,
) -> None:
    verification = artifact.metadata_json.get("manager_verification")
    if not isinstance(verification, dict):
        raise NativeSamplePublicationError("Artifact is missing Manager verification")
    expected_metadata = {**candidate.metadata, "manager_verification": verification}
    if (
        _UUID.fullmatch(artifact.id) is None
        or artifact.job_id != claim.job_id
        or artifact.attempt_id != claim.attempt_id
        or artifact.artifact_type is not candidate.artifact_type
        or artifact.filename != candidate.path.name
        or artifact.size_bytes != candidate.size_bytes
        or artifact.sha256 != candidate.sha256
        or artifact.mime_type != candidate.content_type
        or artifact.metadata_json != expected_metadata
        or set(verification)
        != {"algorithm", "bounded_stream", "upload_session_id", "storage_backend"}
        or verification.get("algorithm") != "sha256"
        or verification.get("bounded_stream") is not True
        or not isinstance(verification.get("upload_session_id"), str)
        or _SAFE_ID.fullmatch(verification["upload_session_id"]) is None
        or verification.get("storage_backend") not in {"local", "s3"}
    ):
        raise NativeSamplePublicationError("finalized Artifact evidence is invalid")


def build_sample_registration_requests(
    claim: JobClaim,
    plan: NativeSamplePublicationPlan,
    finalized_by_relative_path: dict[str, PublishedArtifact],
) -> tuple[SampleRegistrationRequest, ...]:
    finalized_paths = set(finalized_by_relative_path)
    expected_paths = {candidate.relative_path for candidate in plan.candidates}
    if finalized_paths != expected_paths:
        raise NativeSamplePublicationError("finalized Artifact inventory is incomplete")
    _validate_finalized_inventory(claim, plan, finalized_by_relative_path)
    model_artifact = finalized_by_relative_path[plan.model_candidate.relative_path]
    index_artifact = (
        finalized_by_relative_path[plan.index_candidate.relative_path]
        if plan.index_candidate is not None
        else None
    )
    requests: list[SampleRegistrationRequest] = []
    try:
        for sample, candidate in zip(
            plan.publication.samples,
            plan.sample_candidates,
            strict=True,
        ):
            output_artifact = finalized_by_relative_path[candidate.relative_path]
            requests.append(
                SampleRegistrationRequest(
                    lease_id=claim.lease_id,
                    attempt_id=claim.attempt_id,
                    test_set_id=plan.publication.test_set_id,
                    test_set_item_id=sample.test_set_item_id,
                    artifact_id=output_artifact.id,
                    sample_plan_sha256=plan.publication.sample_plan_sha256,
                    input_sha256=sample.input_sha256,
                    model_sha256=model_artifact.sha256,
                    index_sha256=(index_artifact.sha256 if index_artifact else None),
                    inference_f0_method=InferenceF0Method(plan.publication.inference_f0_method),
                    inference_config_sha256=(plan.publication.inference_config_sha256),
                    native_inference_manifest_sha256=plan.publication.manifest.sha256,
                    native_inference_request_sha256=(plan.publication.inference_request_sha256),
                    output_size_bytes=output_artifact.size_bytes,
                    output_sha256=output_artifact.sha256,
                    output_sample_rate_hz=sample.output_sample_rate_hz,
                    output_channels=sample.output_channels,
                    output_duration_seconds=sample.output_duration_seconds,
                    metrics=_sample_metrics(sample),
                    rvc_commit_hash=plan.publication.rvc_commit_hash,
                    runtime_image_digest=plan.publication.runtime_image_digest,
                    runtime_asset_manifest_sha256=(plan.publication.runtime_asset_manifest_sha256),
                )
            )
    except ValidationError as exc:
        raise NativeSamplePublicationError(
            "central Sample registration request is invalid"
        ) from exc
    return tuple(requests)


def expand_finalized_artifacts(
    claim: JobClaim,
    plan: NativeSamplePublicationPlan,
    finalized_uploads: dict[str, PublishedArtifact],
) -> dict[str, PublishedArtifact]:
    """Alias duplicate Sample bytes only after every canonical upload finalized."""

    expected_upload_paths = {candidate.relative_path for candidate in plan.upload_candidates}
    if set(finalized_uploads) != expected_upload_paths:
        raise NativeSamplePublicationError("finalized Artifact upload inventory is incomplete")
    for candidate in plan.upload_candidates:
        validate_finalized_artifact(
            claim,
            candidate,
            finalized_uploads[candidate.relative_path],
        )
    expanded = dict(finalized_uploads)
    for candidate, canonical_path in zip(
        plan.sample_candidates,
        plan.sample_canonical_relative_paths,
        strict=True,
    ):
        canonical_artifact = finalized_uploads.get(canonical_path)
        if canonical_artifact is None:
            raise NativeSamplePublicationError("canonical Sample Artifact is missing")
        expanded[candidate.relative_path] = canonical_artifact
    _validate_finalized_inventory(claim, plan, expanded)
    return expanded


def validate_registered_sample(
    claim: JobClaim,
    request: SampleRegistrationRequest,
    response: SampleRead,
) -> None:
    duration_tolerance = max(1 / request.output_sample_rate_hz, 1e-6)
    if (
        _UUID.fullmatch(response.id) is None
        or response.job_id != claim.job_id
        or response.attempt_id != claim.attempt_id
        or response.test_set_id != request.test_set_id
        or response.test_set_item_id != request.test_set_item_id
        or response.artifact_id != request.artifact_id
        or response.input_sha256 != request.input_sha256
        or response.model_sha256 != request.model_sha256
        or response.index_sha256 != request.index_sha256
        or response.inference_f0_method is not request.inference_f0_method
        or response.inference_config_sha256 != request.inference_config_sha256
        or response.native_inference_manifest_sha256 != request.native_inference_manifest_sha256
        or response.native_inference_request_sha256 != request.native_inference_request_sha256
        or response.output_size_bytes != request.output_size_bytes
        or response.output_sha256 != request.output_sha256
        or response.output_sample_rate_hz != request.output_sample_rate_hz
        or response.output_channels != request.output_channels
        or not math.isclose(
            response.output_duration_seconds,
            request.output_duration_seconds,
            rel_tol=0,
            abs_tol=duration_tolerance,
        )
        or response.metrics.worker_reported != request.metrics
        or not math.isclose(
            response.metrics.worker_reported_duration_seconds,
            request.output_duration_seconds,
            rel_tol=0,
            abs_tol=duration_tolerance,
        )
        or response.metrics.manager_computed_sample_rate_hz != response.output_sample_rate_hz
        or response.metrics.manager_computed_channels != response.output_channels
        or not math.isclose(
            response.metrics.manager_computed_duration_seconds,
            response.output_duration_seconds,
            rel_tol=0,
            abs_tol=duration_tolerance,
        )
        or response.rvc_commit_hash != request.rvc_commit_hash
        or response.runtime_image_digest != request.runtime_image_digest
        or response.runtime_asset_manifest_sha256 != request.runtime_asset_manifest_sha256
    ):
        raise NativeSamplePublicationError("Manager Sample response is inconsistent")


def _validate_finalized_inventory(
    claim: JobClaim,
    plan: NativeSamplePublicationPlan,
    finalized_by_relative_path: dict[str, PublishedArtifact],
) -> None:
    canonical_by_path = {candidate.relative_path: candidate for candidate in plan.upload_candidates}
    upload_artifact_ids = [
        finalized_by_relative_path[candidate.relative_path].id
        for candidate in plan.upload_candidates
    ]
    if len(set(upload_artifact_ids)) != len(upload_artifact_ids):
        raise NativeSamplePublicationError("distinct Artifact uploads reused a Manager Artifact ID")
    for candidate in plan.upload_candidates:
        validate_finalized_artifact(
            claim,
            candidate,
            finalized_by_relative_path[candidate.relative_path],
        )
    for candidate, canonical_path in zip(
        plan.sample_candidates,
        plan.sample_canonical_relative_paths,
        strict=True,
    ):
        if candidate.relative_path == canonical_path:
            continue
        canonical_candidate = canonical_by_path.get(canonical_path)
        artifact = finalized_by_relative_path[candidate.relative_path]
        canonical_artifact = finalized_by_relative_path[canonical_path]
        if (
            canonical_candidate is None
            or canonical_candidate.artifact_type is not ArtifactType.SAMPLE
            or candidate.artifact_type is not ArtifactType.SAMPLE
            or candidate.size_bytes != canonical_candidate.size_bytes
            or candidate.sha256 != canonical_candidate.sha256
            or candidate.content_type != canonical_candidate.content_type
            or artifact != canonical_artifact
            or artifact.artifact_type is not ArtifactType.SAMPLE
            or artifact.size_bytes != candidate.size_bytes
            or artifact.sha256 != candidate.sha256
            or artifact.mime_type != candidate.content_type
            or artifact.metadata_json.get("rvc_commit_hash") != plan.publication.rvc_commit_hash
            or artifact.metadata_json.get("runtime_image_digest")
            != plan.publication.runtime_image_digest
            or artifact.metadata_json.get("runtime_asset_manifest_sha256")
            != plan.publication.runtime_asset_manifest_sha256
            or artifact.metadata_json.get("native_inference_manifest_sha256")
            != plan.publication.manifest.sha256
            or artifact.metadata_json.get("native_inference_request_sha256")
            != plan.publication.inference_request_sha256
            or artifact.metadata_json.get("native_sample_role") != "sample_output"
        ):
            raise NativeSamplePublicationError("aliased Sample Artifact evidence is invalid")


def _require_published_file(
    evidence: NativeInferencePublishedFile,
    *,
    path: object,
    relative_path: str,
) -> None:
    if (
        evidence.path != path
        or evidence.workspace_relative_path != relative_path
        or evidence.size_bytes <= 0
        or _SHA256.fullmatch(evidence.sha256) is None
    ):
        raise NativeSamplePublicationError("published file evidence is invalid")


def _candidate_for_file(
    candidates: dict[str, ArtifactUploadCandidate],
    evidence: NativeInferencePublishedFile,
    artifact_type: ArtifactType,
    content_type: str,
) -> ArtifactUploadCandidate:
    candidate = candidates.get(evidence.workspace_relative_path)
    if (
        candidate is None
        or candidate.path != evidence.path
        or candidate.artifact_type is not artifact_type
        or candidate.size_bytes != evidence.size_bytes
        or candidate.sha256 != evidence.sha256
        or candidate.content_type != content_type
    ):
        raise NativeSamplePublicationError("Artifact candidate changed after inference")
    return candidate


def _sample_metrics(sample: NativeInferencePublishedSample) -> SampleMetricValues:
    try:
        return SampleMetricValues.model_validate(dict(sample.metrics))
    except ValidationError as exc:
        raise NativeSamplePublicationError("sample metric evidence is invalid") from exc


def _with_metadata(
    candidate: ArtifactUploadCandidate,
    evidence: dict[str, object],
    *,
    role: str,
) -> ArtifactUploadCandidate:
    metadata = {
        **candidate.metadata,
        **evidence,
        "native_sample_role": role,
    }
    try:
        encoded = json.dumps(
            metadata,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NativeSamplePublicationError("Artifact metadata is not canonical JSON") from exc
    if len(encoded) > _MAX_ARTIFACT_METADATA_BYTES:
        raise NativeSamplePublicationError("Artifact metadata exceeds the Manager limit")
    return replace(candidate, metadata=metadata)


__all__ = [
    "NativeSamplePublicationError",
    "NativeSamplePublicationPlan",
    "build_sample_registration_requests",
    "expand_finalized_artifacts",
    "prepare_native_sample_publication",
    "validate_finalized_artifact",
    "validate_registered_sample",
]
