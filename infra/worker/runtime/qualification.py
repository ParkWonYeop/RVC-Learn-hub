#!/usr/bin/env python3
"""Validate native RVC release evidence and project a fail-closed activation.

This utility intentionally uses only the Python standard library.  It does not
run qualification cases and it does not infer success from image labels.  It
accepts only the complete reviewed matrix, binds it to an exact runtime build
and asset manifest, then emits the small immutable projection consumed by the
Worker.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import IO, Any, BinaryIO

FORMAT_VERSION = 1
QUALIFICATION_KIND = "rvc-native-runtime-qualification"
ACTIVATION_KIND = "rvc-runtime-activation"
RVC_COMMIT = "7ef19867780cf703841ebafb565a4e47d1ea86ff"
BASE_IMAGE_PREFIX = "pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime@sha256:"
TORCH_VERSION = "2.6.0+cu124"
TORCHVISION_VERSION = "0.21.0+cu124"
TORCHAUDIO_VERSION = "2.6.0+cu124"
CUDA_VERSION = "12.4"
CUDNN_VERSION = "9"
SUPPORTED_INFERENCE_F0_METHODS = ["pm", "harvest", "crepe", "rmvpe"]

_MAX_JSON_BYTES = 1024 * 1024
_MAX_BUILD_MANIFEST_BYTES = 256 * 1024
_MAX_ASSET_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_EVIDENCE_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_REPORT_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_REPORT_BYTES = 256 * 1024 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_RELEASE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_IMAGE_REFERENCE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9./_-]*:[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)
_REVIEWER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+-]{0,127}$")
_UTC_TIMESTAMP = re.compile(
    r"^(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})T"
    r"(?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?P<fraction>\.[0-9]{1,6})?Z$"
)

_TOP_LEVEL_KEYS = {
    "format_version",
    "kind",
    "runtime",
    "cases",
    "evidence_archive",
    "review",
}
_RUNTIME_KEYS = {
    "image_digest",
    "release_version",
    "orchestrator_commit",
    "rvc_commit",
    "base_image",
    "source_manifest_sha256",
    "wheelhouse_manifest_sha256",
    "asset_manifest_sha256",
    "projection_manifest_sha256",
    "fairseq_commit",
    "torch",
    "torchvision",
    "torchaudio",
    "cuda",
    "cudnn",
}
_CASE_KEYS = {"case_id", "result", "report_path", "report_sha256"}
_EVIDENCE_ARCHIVE_KEYS = {"file", "size", "sha256"}
_REVIEW_KEYS = {"reviewed_at", "reviewer"}
_ACTIVATION_KEYS = {
    "format_version",
    "kind",
    "runtime_image_digest",
    "runtime_asset_manifest_sha256",
    "qualification_evidence_sha256",
    "gpu_smoke_verified",
    "profile_stage_set_verified",
    "native_sample_inference_verified",
    "supported_inference_f0_methods",
}
_BUILD_MANIFEST_KEYS = {
    "RUNTIME_BUILD_FORMAT_VERSION",
    "PRODUCT",
    "COMPONENT",
    "IMAGE",
    "RELEASE_VERSION",
    "ORCHESTRATOR_SOURCE_COMMIT",
    "BASE_IMAGE",
    "RVC_SOURCE_COMMIT",
    "RVC_SOURCE_MANIFEST_SHA256",
    "RVC_WHEELHOUSE_MANIFEST_SHA256",
    "RVC_ASSET_MANIFEST_SHA256",
    "RVC_PROJECTION_MANIFEST_SHA256",
    "RVC_FAIRSEQ_COMMIT",
    "RVC_TORCH_VERSION",
    "RVC_CUDA_RUNTIME_VERSION",
    "RVC_CUDNN_MAJOR",
    "GPU_SMOKE_VERIFIED",
    "PROFILE_STAGE_SET_VERIFIED",
}


def _required_case_ids() -> frozenset[str]:
    cases: set[str] = set()
    for version in ("v1", "v2"):
        for sample_rate in ("40k", "48k"):
            for f0_mode in ("f0-off", "f0-on"):
                cases.add(f"core-{version}-{sample_rate}-{f0_mode}")
    for method in ("pm", "harvest", "dio", "rmvpe", "rmvpe-gpu"):
        cases.add(f"training-f0-{method}")
    for version in ("v1", "v2"):
        for sample_rate in ("40k", "48k"):
            for index_mode in ("index-off", "index-on"):
                for method in SUPPORTED_INFERENCE_F0_METHODS:
                    cases.add(
                        f"sample-{version}-{sample_rate}-{index_mode}-{method}"
                    )
    for operation in (
        "cancellation",
        "restart-recovery",
        "telemetry-spool",
        "no-public-egress",
    ):
        cases.add(f"ops-{operation}")
    return frozenset(cases)


REQUIRED_CASE_IDS = _required_case_ids()


class QualificationError(RuntimeError):
    """The release evidence or requested projection is unsafe or incomplete."""


def _require_exact_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise QualificationError(f"{label} must be a JSON object")
    actual = set(value)
    if actual != expected:
        missing = ",".join(sorted(expected - actual)) or "none"
        extra = ",".join(sorted(actual - expected)) or "none"
        raise QualificationError(
            f"{label} fields differ (missing={missing}; extra={extra})"
        )
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QualificationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise QualificationError(f"non-finite JSON number is forbidden: {value}")


def _decode_json(content: bytes, label: str) -> dict[str, Any]:
    try:
        decoded = content.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except QualificationError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise QualificationError(f"{label} root must be a JSON object")
    return value


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
    )


@contextlib.contextmanager
def _open_regular_file(
    path: Path,
    *,
    maximum: int,
    label: str,
) -> Iterator[tuple[BinaryIO, os.stat_result]]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise QualificationError("O_NOFOLLOW support is required")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise QualificationError(f"{label} is missing or cannot be opened safely") from exc
    stream = os.fdopen(descriptor, "rb", closefd=True)
    try:
        initial = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_size <= 0
            or initial.st_size > maximum
        ):
            raise QualificationError(f"{label} has unsafe size or file type")
        yield stream, initial
        final = os.fstat(stream.fileno())
        if _stat_identity(initial) != _stat_identity(final):
            raise QualificationError(f"{label} changed while it was being verified")
    finally:
        stream.close()


def _read_regular_file(path: Path, *, maximum: int, label: str) -> bytes:
    with _open_regular_file(path, maximum=maximum, label=label) as (stream, initial):
        content = stream.read(maximum + 1)
        if len(content) != initial.st_size or len(content) > maximum:
            raise QualificationError(f"{label} changed or exceeds its size limit")
        return content


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    patterns = (
        "0",
        "1",
        "a",
        "f",
        "deadbeef",
        "0123456789abcdef",
        "1234567890abcdef",
    )
    return any(pattern * (len(value) // len(pattern)) == lowered for pattern in patterns)


def _validate_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise QualificationError(f"{label} must be a lowercase SHA-256")
    if _is_placeholder(value):
        raise QualificationError(f"{label} cannot be a placeholder hash")
    return value


def _validate_image_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _IMAGE_DIGEST.fullmatch(value) is None:
        raise QualificationError(f"{label} must be a sha256 image digest")
    _validate_sha256(value.removeprefix("sha256:"), label)
    return value


def _validate_commit(value: object, label: str) -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise QualificationError(f"{label} must be a lowercase 40-hex commit")
    if _is_placeholder(value):
        raise QualificationError(f"{label} cannot be a placeholder commit")
    return value


def _validate_positive_integer(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise QualificationError(f"{label} must be a positive integer")
    return value


def _validate_safe_basename(value: object, label: str) -> str:
    result = _validate_safe_relative_path(value, label)
    if len(PurePosixPath(result).parts) != 1:
        raise QualificationError(f"{label} must be a basename")
    return result


def _validate_safe_relative_path(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise QualificationError(f"{label} is not a safe relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise QualificationError(f"{label} is not a safe relative path")
    return value


def _validate_reviewed_at(value: object) -> str:
    if not isinstance(value, str):
        raise QualificationError("review.reviewed_at must be a strict UTC timestamp")
    match = _UTC_TIMESTAMP.fullmatch(value)
    if match is None:
        raise QualificationError("review.reviewed_at must end in Z and use RFC 3339 UTC")
    rendered = value[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise QualificationError("review.reviewed_at is not a valid UTC timestamp") from exc
    if parsed.tzinfo != dt.UTC:
        raise QualificationError("review.reviewed_at must use UTC")
    return value


def _validate_runtime(value: object) -> dict[str, str]:
    runtime = _require_exact_keys(value, _RUNTIME_KEYS, "runtime")
    image_digest = _validate_image_digest(runtime["image_digest"], "runtime.image_digest")
    release_version = runtime["release_version"]
    if not isinstance(release_version, str) or _RELEASE_VERSION.fullmatch(release_version) is None:
        raise QualificationError("runtime.release_version is invalid")
    orchestrator_commit = _validate_commit(
        runtime["orchestrator_commit"], "runtime.orchestrator_commit"
    )
    if runtime["rvc_commit"] != RVC_COMMIT:
        raise QualificationError("runtime.rvc_commit is not the reviewed RVC commit")
    base_image = runtime["base_image"]
    if not isinstance(base_image, str) or not base_image.startswith(BASE_IMAGE_PREFIX):
        raise QualificationError("runtime.base_image is not the fixed digest-pinned base")
    base_digest = base_image.removeprefix(BASE_IMAGE_PREFIX)
    _validate_sha256(base_digest, "runtime.base_image digest")
    source_hash = _validate_sha256(
        runtime["source_manifest_sha256"], "runtime.source_manifest_sha256"
    )
    wheelhouse_hash = _validate_sha256(
        runtime["wheelhouse_manifest_sha256"], "runtime.wheelhouse_manifest_sha256"
    )
    asset_hash = _validate_sha256(
        runtime["asset_manifest_sha256"], "runtime.asset_manifest_sha256"
    )
    projection_hash = _validate_sha256(
        runtime["projection_manifest_sha256"], "runtime.projection_manifest_sha256"
    )
    fairseq_commit = _validate_commit(runtime["fairseq_commit"], "runtime.fairseq_commit")
    fixed_versions = {
        "torch": TORCH_VERSION,
        "torchvision": TORCHVISION_VERSION,
        "torchaudio": TORCHAUDIO_VERSION,
        "cuda": CUDA_VERSION,
        "cudnn": CUDNN_VERSION,
    }
    for key, expected in fixed_versions.items():
        if runtime[key] != expected:
            raise QualificationError(f"runtime.{key} is not the fixed release value")
    return {
        "image_digest": image_digest,
        "release_version": release_version,
        "orchestrator_commit": orchestrator_commit,
        "rvc_commit": RVC_COMMIT,
        "base_image": base_image,
        "source_manifest_sha256": source_hash,
        "wheelhouse_manifest_sha256": wheelhouse_hash,
        "asset_manifest_sha256": asset_hash,
        "projection_manifest_sha256": projection_hash,
        "fairseq_commit": fairseq_commit,
        **fixed_versions,
    }


def _validate_cases(value: object) -> dict[str, str]:
    if not isinstance(value, list):
        raise QualificationError("cases must be a JSON array")
    reports: dict[str, str] = {}
    case_ids: list[str] = []
    for index, item in enumerate(value):
        case = _require_exact_keys(item, _CASE_KEYS, f"cases[{index}]")
        case_id = case["case_id"]
        if not isinstance(case_id, str) or case_id not in REQUIRED_CASE_IDS:
            raise QualificationError(f"cases[{index}].case_id is not required")
        if case_id in case_ids:
            raise QualificationError(f"duplicate qualification case: {case_id}")
        case_ids.append(case_id)
        if case["result"] != "passed":
            raise QualificationError(f"qualification case did not pass: {case_id}")
        report_path = _validate_safe_relative_path(
            case["report_path"], f"cases[{index}].report_path"
        )
        expected_path = f"reports/{case_id}.json"
        if report_path != expected_path:
            raise QualificationError(
                f"qualification report path does not match case identity: {case_id}"
            )
        if report_path in reports:
            raise QualificationError(f"duplicate qualification report path: {report_path}")
        reports[report_path] = _validate_sha256(
            case["report_sha256"], f"cases[{index}].report_sha256"
        )
    actual = set(case_ids)
    if actual != REQUIRED_CASE_IDS or len(case_ids) != len(REQUIRED_CASE_IDS):
        missing = ",".join(sorted(REQUIRED_CASE_IDS - actual)) or "none"
        extra = ",".join(sorted(actual - REQUIRED_CASE_IDS)) or "none"
        raise QualificationError(
            f"qualification case set is incomplete (missing={missing}; extra={extra})"
        )
    return reports


def _validate_review(value: object) -> None:
    review = _require_exact_keys(value, _REVIEW_KEYS, "review")
    _validate_reviewed_at(review["reviewed_at"])
    reviewer = review["reviewer"]
    if not isinstance(reviewer, str) or _REVIEWER.fullmatch(reviewer) is None:
        raise QualificationError("review.reviewer is not a safe reviewer identifier")


def _sha256_stream(stream: IO[bytes]) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _verify_evidence_archive(
    path: Path,
    archive_value: object,
    reports: dict[str, str],
) -> None:
    archive = _require_exact_keys(
        archive_value, _EVIDENCE_ARCHIVE_KEYS, "evidence_archive"
    )
    expected_file = _validate_safe_basename(archive["file"], "evidence_archive.file")
    if path.name != expected_file:
        raise QualificationError("evidence archive basename does not match qualification")
    expected_size = _validate_positive_integer(archive["size"], "evidence_archive.size")
    expected_hash = _validate_sha256(archive["sha256"], "evidence_archive.sha256")

    try:
        with _open_regular_file(
            path,
            maximum=_MAX_EVIDENCE_ARCHIVE_BYTES,
            label="evidence archive",
        ) as (stream, initial):
            if initial.st_size != expected_size:
                raise QualificationError("evidence archive size does not match qualification")
            actual_hash = _sha256_stream(stream)
            if actual_hash != expected_hash:
                raise QualificationError("evidence archive SHA-256 does not match qualification")
            stream.seek(0)
            with tarfile.open(fileobj=stream, mode="r|gz") as evidence:
                seen: set[str] = set()
                total_size = 0
                member_count = 0
                for member in evidence:
                    member_count += 1
                    if member_count > len(reports):
                        raise QualificationError(
                            "evidence archive contains too many members"
                        )
                    name = _validate_safe_relative_path(
                        member.name, "evidence archive member"
                    )
                    if name in seen:
                        raise QualificationError(f"duplicate evidence archive member: {name}")
                    seen.add(name)
                    if not member.isfile() or member.islnk() or member.issym():
                        raise QualificationError(
                            f"evidence archive member is not a regular report: {name}"
                        )
                    if member.size <= 0 or member.size > _MAX_REPORT_BYTES:
                        raise QualificationError(
                            f"evidence archive report has unsafe size: {name}"
                        )
                    total_size += member.size
                    if total_size > _MAX_TOTAL_REPORT_BYTES:
                        raise QualificationError("evidence archive reports exceed total limit")
                    expected_report_hash = reports.get(name)
                    if expected_report_hash is None:
                        raise QualificationError(
                            f"evidence archive contains an unlisted report: {name}"
                        )
                    extracted = evidence.extractfile(member)
                    if extracted is None:
                        raise QualificationError(
                            f"evidence archive report cannot be read: {name}"
                        )
                    with extracted:
                        report_hash = _sha256_stream(extracted)
                    if report_hash != expected_report_hash:
                        raise QualificationError(
                            f"evidence archive report SHA-256 mismatch: {name}"
                        )
                if member_count != len(reports) or seen != set(reports):
                    raise QualificationError("evidence archive is missing required reports")
    except QualificationError:
        raise
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise QualificationError("evidence archive is not a valid tar.gz file") from exc


def _load_build_manifest(path: Path) -> dict[str, str]:
    content = _read_regular_file(
        path,
        maximum=_MAX_BUILD_MANIFEST_BYTES,
        label="runtime build manifest",
    )
    try:
        rendered = content.decode("utf-8")
    except UnicodeError as exc:
        raise QualificationError("runtime build manifest is not UTF-8") from exc
    if "\r" in rendered or not rendered.endswith("\n"):
        raise QualificationError("runtime build manifest must use canonical LF lines")
    values: dict[str, str] = {}
    for line in rendered.splitlines():
        if not line or line.startswith(("#", " ", "\t")) or "=" not in line:
            raise QualificationError("runtime build manifest contains a malformed line")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not value:
            raise QualificationError("runtime build manifest contains an invalid assignment")
        if key in values:
            raise QualificationError(f"runtime build manifest has duplicate key: {key}")
        values[key] = value
    if set(values) != _BUILD_MANIFEST_KEYS:
        missing = ",".join(sorted(_BUILD_MANIFEST_KEYS - set(values))) or "none"
        extra = ",".join(sorted(set(values) - _BUILD_MANIFEST_KEYS)) or "none"
        raise QualificationError(
            f"runtime build manifest fields differ (missing={missing}; extra={extra})"
        )
    if (
        values["RUNTIME_BUILD_FORMAT_VERSION"] != "1"
        or values["PRODUCT"] != "rvc-training-orchestrator"
        or values["COMPONENT"] != "worker-rvc-runtime"
    ):
        raise QualificationError("runtime build manifest identity is unsupported")
    if values["GPU_SMOKE_VERIFIED"] != "false" or values[
        "PROFILE_STAGE_SET_VERIFIED"
    ] != "false":
        raise QualificationError("runtime build manifest must retain pre-qualification gates")
    image = values["IMAGE"]
    release = values["RELEASE_VERSION"]
    if _IMAGE_REFERENCE.fullmatch(image) is None or _RELEASE_VERSION.fullmatch(release) is None:
        raise QualificationError("runtime build manifest image or release is invalid")
    if image.rsplit(":", 1)[1] != release:
        raise QualificationError("runtime build manifest image tag does not match release")
    _validate_commit(
        values["ORCHESTRATOR_SOURCE_COMMIT"],
        "runtime build manifest orchestrator commit",
    )
    if values["RVC_SOURCE_COMMIT"] != RVC_COMMIT:
        raise QualificationError("runtime build manifest RVC commit is not reviewed")
    if not values["BASE_IMAGE"].startswith(BASE_IMAGE_PREFIX):
        raise QualificationError("runtime build manifest base image is not fixed")
    _validate_sha256(
        values["BASE_IMAGE"].removeprefix(BASE_IMAGE_PREFIX),
        "runtime build manifest base digest",
    )
    for key in (
        "RVC_SOURCE_MANIFEST_SHA256",
        "RVC_WHEELHOUSE_MANIFEST_SHA256",
        "RVC_ASSET_MANIFEST_SHA256",
        "RVC_PROJECTION_MANIFEST_SHA256",
    ):
        _validate_sha256(values[key], f"runtime build manifest {key}")
    _validate_commit(values["RVC_FAIRSEQ_COMMIT"], "runtime build manifest fairseq commit")
    fixed = {
        "RVC_TORCH_VERSION": TORCH_VERSION,
        "RVC_CUDA_RUNTIME_VERSION": CUDA_VERSION,
        "RVC_CUDNN_MAJOR": CUDNN_VERSION,
    }
    for key, expected in fixed.items():
        if values[key] != expected:
            raise QualificationError(f"runtime build manifest {key} is not fixed")
    return values


def load_runtime_build_manifest(path: Path) -> dict[str, str]:
    """Load the strict pre-qualification runtime build identity."""

    return _load_build_manifest(path)


def _verify_asset_manifest(path: Path, expected_hash: str) -> None:
    content = _read_regular_file(
        path,
        maximum=_MAX_ASSET_MANIFEST_BYTES,
        label="asset manifest",
    )
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise QualificationError("asset manifest byte hash does not match runtime provenance")
    manifest = _require_exact_keys(
        _decode_json(content, "asset manifest"),
        {"schema_version", "kind", "rvc_commit", "assets"},
        "asset manifest",
    )
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["kind"] != "rvc-assets"
        or manifest["rvc_commit"] != RVC_COMMIT
        or not isinstance(manifest["assets"], list)
        or not manifest["assets"]
    ):
        raise QualificationError("asset manifest identity is unsupported")


def _cross_check_runtime(
    runtime: dict[str, str],
    build: dict[str, str],
    runtime_image_digest: str,
) -> None:
    actual_image_digest = _validate_image_digest(
        runtime_image_digest, "--runtime-image-digest"
    )
    comparisons = {
        "image_digest": actual_image_digest,
        "release_version": build["RELEASE_VERSION"],
        "orchestrator_commit": build["ORCHESTRATOR_SOURCE_COMMIT"],
        "rvc_commit": build["RVC_SOURCE_COMMIT"],
        "base_image": build["BASE_IMAGE"],
        "source_manifest_sha256": build["RVC_SOURCE_MANIFEST_SHA256"],
        "wheelhouse_manifest_sha256": build["RVC_WHEELHOUSE_MANIFEST_SHA256"],
        "asset_manifest_sha256": build["RVC_ASSET_MANIFEST_SHA256"],
        "projection_manifest_sha256": build["RVC_PROJECTION_MANIFEST_SHA256"],
        "fairseq_commit": build["RVC_FAIRSEQ_COMMIT"],
        "torch": TORCH_VERSION,
        "torchvision": TORCHVISION_VERSION,
        "torchaudio": TORCHAUDIO_VERSION,
        "cuda": CUDA_VERSION,
        "cudnn": CUDNN_VERSION,
    }
    for key, expected in comparisons.items():
        if runtime[key] != expected:
            raise QualificationError(f"qualification runtime identity mismatch: {key}")


def _canonical_json(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_new_atomic(path: Path, content: bytes) -> None:
    parent = path.parent
    try:
        parent_status = parent.stat()
    except OSError as exc:
        raise QualificationError("output parent directory is missing") from exc
    if not stat.S_ISDIR(parent_status.st_mode) or parent.is_symlink():
        raise QualificationError("output parent must be a real directory")
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise QualificationError("output path cannot be inspected safely") from exc
    else:
        raise QualificationError("output path already exists or is a symlink")

    descriptor = -1
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.tmp.", dir=parent
        )
        written = 0
        while written < len(content):
            written += os.write(descriptor, content[written:])
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary_name, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise QualificationError("output path appeared during atomic publication") from exc
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except QualificationError:
        raise
    except OSError as exc:
        raise QualificationError("activation projection could not be published atomically") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _disabled_activation() -> dict[str, Any]:
    return {
        "format_version": FORMAT_VERSION,
        "kind": ACTIVATION_KIND,
        "runtime_image_digest": None,
        "runtime_asset_manifest_sha256": None,
        "qualification_evidence_sha256": None,
        "gpu_smoke_verified": False,
        "profile_stage_set_verified": False,
        "native_sample_inference_verified": False,
        "supported_inference_f0_methods": [],
    }


def _qualified_activation(
    *, runtime: dict[str, str], qualification_bytes: bytes
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "kind": ACTIVATION_KIND,
        "runtime_image_digest": runtime["image_digest"],
        "runtime_asset_manifest_sha256": runtime["asset_manifest_sha256"],
        "qualification_evidence_sha256": hashlib.sha256(qualification_bytes).hexdigest(),
        "gpu_smoke_verified": True,
        "profile_stage_set_verified": True,
        "native_sample_inference_verified": True,
        "supported_inference_f0_methods": list(SUPPORTED_INFERENCE_F0_METHODS),
    }
    if set(value) != _ACTIVATION_KEYS:
        raise AssertionError("activation schema drifted")
    return value


def _verify_qualification_bytes(
    *,
    qualification_bytes: bytes,
    evidence_archive_path: Path,
    runtime_build_manifest_path: Path,
    asset_manifest_path: Path,
    runtime_image_digest: str,
) -> dict[str, str]:
    qualification = _require_exact_keys(
        _decode_json(qualification_bytes, "qualification manifest"),
        _TOP_LEVEL_KEYS,
        "qualification manifest",
    )
    if (
        type(qualification["format_version"]) is not int
        or qualification["format_version"] != FORMAT_VERSION
        or qualification["kind"] != QUALIFICATION_KIND
    ):
        raise QualificationError("qualification manifest format or kind is unsupported")
    runtime = _validate_runtime(qualification["runtime"])
    reports = _validate_cases(qualification["cases"])
    _validate_review(qualification["review"])
    _verify_evidence_archive(
        evidence_archive_path, qualification["evidence_archive"], reports
    )
    build = _load_build_manifest(runtime_build_manifest_path)
    _cross_check_runtime(runtime, build, runtime_image_digest)
    _verify_asset_manifest(asset_manifest_path, runtime["asset_manifest_sha256"])
    return runtime


def verify_qualification_evidence(
    *,
    qualification_path: Path,
    evidence_archive_path: Path,
    runtime_build_manifest_path: Path,
    asset_manifest_path: Path,
    runtime_image_digest: str,
) -> dict[str, str]:
    """Verify the complete qualification chain without creating an activation.

    Release-readiness reporting needs the same byte and identity checks as the
    projection path, but it must not manufacture even a temporary enabled
    activation.  Keeping the shared validation here prevents a weaker report-only
    interpretation of the 49-case contract.
    """

    qualification_bytes = _read_regular_file(
        qualification_path,
        maximum=_MAX_JSON_BYTES,
        label="qualification manifest",
    )
    return _verify_qualification_bytes(
        qualification_bytes=qualification_bytes,
        evidence_archive_path=evidence_archive_path,
        runtime_build_manifest_path=runtime_build_manifest_path,
        asset_manifest_path=asset_manifest_path,
        runtime_image_digest=runtime_image_digest,
    )


def project_activation(
    *,
    qualification_path: Path,
    evidence_archive_path: Path,
    runtime_build_manifest_path: Path,
    asset_manifest_path: Path,
    runtime_image_digest: str,
    output_path: Path,
) -> None:
    qualification_bytes = _read_regular_file(
        qualification_path,
        maximum=_MAX_JSON_BYTES,
        label="qualification manifest",
    )
    runtime = _verify_qualification_bytes(
        qualification_bytes=qualification_bytes,
        evidence_archive_path=evidence_archive_path,
        runtime_build_manifest_path=runtime_build_manifest_path,
        asset_manifest_path=asset_manifest_path,
        runtime_image_digest=runtime_image_digest,
    )
    activation = _qualified_activation(
        runtime=runtime,
        qualification_bytes=qualification_bytes,
    )
    _write_new_atomic(output_path, _canonical_json(activation))


def disable_activation(*, output_path: Path) -> None:
    _write_new_atomic(output_path, _canonical_json(_disabled_activation()))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Project reviewed RVC runtime qualification into an activation file"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    project = subparsers.add_parser(
        "project", help="verify complete qualification evidence and emit an enabled projection"
    )
    project.add_argument("--qualification", type=Path, required=True)
    project.add_argument("--evidence-archive", type=Path, required=True)
    project.add_argument("--runtime-build-manifest", type=Path, required=True)
    project.add_argument("--asset-manifest", type=Path, required=True)
    project.add_argument("--runtime-image-digest", required=True)
    project.add_argument("--output", type=Path, required=True)
    disabled = subparsers.add_parser(
        "disabled", help="emit the exact fail-closed disabled projection"
    )
    disabled.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "project":
            project_activation(
                qualification_path=arguments.qualification,
                evidence_archive_path=arguments.evidence_archive,
                runtime_build_manifest_path=arguments.runtime_build_manifest,
                asset_manifest_path=arguments.asset_manifest,
                runtime_image_digest=arguments.runtime_image_digest,
                output_path=arguments.output,
            )
        else:
            disable_activation(output_path=arguments.output)
    except QualificationError as exc:
        print(f"qualification error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
