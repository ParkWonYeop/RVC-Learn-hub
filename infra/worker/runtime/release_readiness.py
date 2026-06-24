#!/usr/bin/env python3
"""Report native Worker release inputs without changing any runtime gate.

The runtime builder and qualification projector intentionally fail on the first
bad input.  That is correct for publication, but it is inconvenient while an
operator is assembling an offline release.  This tool runs the same strict
input/qualification verifiers and returns a complete, deterministic checklist.

It never invokes Docker, never uses the network, never writes an activation
projection, and always reports ``activation_permitted=false``.  A successful
exit means only that the enumerated evidence inputs are structurally present and
cross-bound; it is not an authorization to publish or enable the Worker.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any, BinaryIO, Literal

_RUNTIME_DIRECTORY = Path(__file__).resolve().parent
if str(_RUNTIME_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIRECTORY))

import qualification  # noqa: E402
import verify_inputs  # noqa: E402

FORMAT_VERSION = 1
REPORT_KIND = "rvc-worker-release-readiness"
REVIEW_KIND = "rvc-worker-release-review"

_MAX_REVIEW_MANIFEST_BYTES = 1024 * 1024
_MAX_REVIEW_EVIDENCE_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_REVIEW_EVIDENCE_BYTES = 256 * 1024 * 1024
_MAX_RUNTIME_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_DETAIL_CHARACTERS = 500

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVIEWER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+-]{0,127}$")
_UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z$"
)

_REVIEW_TOP_LEVEL_KEYS = {
    "format_version",
    "kind",
    "runtime_image",
    "base_image",
    "evidence",
    "review",
}
_RUNTIME_IMAGE_KEYS = {"digest", "os", "architecture", "user"}
_BASE_IMAGE_KEYS = {"reference", "os", "architecture"}
_REVIEW_KEYS = {"reviewed_at", "reviewer"}
_EVIDENCE_KEYS = {"type", "path", "size", "sha256", "result"}

REQUIRED_REVIEW_EVIDENCE: dict[str, str] = {
    "runtime-sbom": "complete",
    "vulnerability-scan": "passed",
    "container-scan": "passed",
    "secret-scan": "passed",
    "sast-scan": "passed",
    "license-review": "approved",
    "clean-host-lifecycle": "passed",
}

CHECK_IDS = (
    "source-archive",
    "wheelhouse",
    "runtime-assets",
    "base-image-amd64-digest",
    "runtime-build-manifest",
    "runtime-image-digest",
    "runtime-image-linux-amd64-user",
    "qualification-49-case",
    "release-review-manifest",
    *REQUIRED_REVIEW_EVIDENCE,
)

CheckStatus = Literal["verified", "missing", "invalid", "blocked-dependency"]


class ReadinessError(RuntimeError):
    """An operator input is unsafe, inconsistent, or structurally invalid."""


@dataclass(frozen=True)
class Check:
    identifier: str
    status: CheckStatus
    detail: str

    def as_json(self) -> dict[str, str]:
        return {"id": self.identifier, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class Arguments:
    source_manifest: Path | None
    source_archive: Path | None
    wheelhouse_manifest: Path | None
    wheelhouse_root: Path | None
    asset_manifest: Path | None
    asset_root: Path | None
    runtime_build_manifest: Path | None
    runtime_image_digest: str | None
    qualification_manifest: Path | None
    qualification_evidence: Path | None
    release_review: Path | None
    review_evidence_root: Path | None


@dataclass(frozen=True)
class ReviewEvidence:
    runtime_image_digest: str
    base_image: str
    checks: dict[str, Check]


def _safe_detail(value: object) -> str:
    rendered = " ".join(str(value).split())
    if len(rendered) > _MAX_DETAIL_CHARACTERS:
        return rendered[: _MAX_DETAIL_CHARACTERS - 3] + "..."
    return rendered


def _missing(identifier: str, detail: str) -> Check:
    return Check(identifier, "missing", detail)


def _invalid(identifier: str, detail: object) -> Check:
    return Check(identifier, "invalid", _safe_detail(detail))


def _verified(identifier: str, detail: str) -> Check:
    return Check(identifier, "verified", detail)


def _blocked(identifier: str, detail: str) -> Check:
    return Check(identifier, "blocked-dependency", detail)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReadinessError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ReadinessError(f"non-finite JSON number is forbidden: {value}")


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
    path: Path, *, maximum: int, label: str
) -> Iterator[tuple[BinaryIO, os.stat_result]]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ReadinessError("O_NOFOLLOW support is required")
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise ReadinessError(f"{label} is missing or cannot be opened safely") from exc
    stream = os.fdopen(descriptor, "rb", closefd=True)
    try:
        initial = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_size <= 0
            or initial.st_size > maximum
        ):
            raise ReadinessError(f"{label} has an unsafe type or size")
        yield stream, initial
        final = os.fstat(stream.fileno())
        if _stat_identity(initial) != _stat_identity(final):
            raise ReadinessError(f"{label} changed while it was being verified")
    finally:
        stream.close()


def _read_regular_file(path: Path, *, maximum: int, label: str) -> bytes:
    with _open_regular_file(path, maximum=maximum, label=label) as (stream, initial):
        content = stream.read(maximum + 1)
        if len(content) != initial.st_size or len(content) > maximum:
            raise ReadinessError(f"{label} changed or exceeds its size limit")
        return content


def _decode_json(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except ReadinessError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReadinessError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ReadinessError(f"{label} root must be a JSON object")
    return value


def _require_exact_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReadinessError(f"{label} must be a JSON object")
    actual = set(value)
    if actual != expected:
        missing = ",".join(sorted(expected - actual)) or "none"
        extra = ",".join(sorted(actual - expected)) or "none"
        raise ReadinessError(
            f"{label} fields differ (missing={missing}; extra={extra})"
        )
    return value


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    patterns = ("0", "1", "a", "f", "deadbeef", "0123456789abcdef")
    return any(pattern * (len(value) // len(pattern)) == lowered for pattern in patterns)


def _validate_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ReadinessError(f"{label} must be a lowercase SHA-256")
    if _is_placeholder(value):
        raise ReadinessError(f"{label} cannot be a placeholder hash")
    return value


def _validate_image_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _IMAGE_DIGEST.fullmatch(value) is None:
        raise ReadinessError(f"{label} must be a sha256 image digest")
    _validate_sha256(value.removeprefix("sha256:"), label)
    return value


def _validate_safe_relative_path(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ReadinessError(f"{label} is not a safe relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ReadinessError(f"{label} is not a safe relative path")
    return value


def _validate_positive_integer(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ReadinessError(f"{label} must be a positive integer")
    return value


def _sha256_stream(stream: IO[bytes]) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    with _open_regular_file(
        path, maximum=_MAX_RUNTIME_MANIFEST_BYTES, label="manifest"
    ) as (stream, _):
        return _sha256_stream(stream)


@contextlib.contextmanager
def _open_evidence_under_root(
    root: Path, relative: str
) -> Iterator[tuple[BinaryIO, os.stat_result]]:
    """Open a review file without following any root, parent, or leaf symlink."""

    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory_flag is None:
        raise ReadinessError("O_NOFOLLOW and O_DIRECTORY support are required")
    parts = PurePosixPath(relative).parts
    descriptors: list[int] = []
    stream: BinaryIO | None = None
    try:
        current = os.open(
            root,
            os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0),
        )
        descriptors.append(current)
        for part in parts[:-1]:
            current = os.open(
                part,
                os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0),
                dir_fd=current,
            )
            descriptors.append(current)
        file_descriptor = os.open(
            parts[-1],
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=current,
        )
        stream = os.fdopen(file_descriptor, "rb", closefd=True)
        initial = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_size <= 0
            or initial.st_size > _MAX_REVIEW_EVIDENCE_BYTES
        ):
            raise ReadinessError("review evidence has an unsafe type or size")
        yield stream, initial
        final = os.fstat(stream.fileno())
        if _stat_identity(initial) != _stat_identity(final):
            raise ReadinessError("review evidence changed while it was being verified")
    except ReadinessError:
        raise
    except OSError as exc:
        raise ReadinessError("review evidence cannot be opened without symlinks") from exc
    finally:
        if stream is not None:
            stream.close()
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _validate_review_manifest(
    manifest_path: Path, evidence_root: Path | None
) -> ReviewEvidence:
    content = _read_regular_file(
        manifest_path,
        maximum=_MAX_REVIEW_MANIFEST_BYTES,
        label="release review manifest",
    )
    document = _require_exact_keys(
        _decode_json(content, "release review manifest"),
        _REVIEW_TOP_LEVEL_KEYS,
        "release review manifest",
    )
    if document["format_version"] != FORMAT_VERSION or document["kind"] != REVIEW_KIND:
        raise ReadinessError("release review manifest format or kind is unsupported")

    runtime_image = _require_exact_keys(
        document["runtime_image"], _RUNTIME_IMAGE_KEYS, "runtime_image"
    )
    runtime_digest = _validate_image_digest(
        runtime_image["digest"], "release review runtime_image.digest"
    )
    if (
        runtime_image["os"] != "linux"
        or runtime_image["architecture"] != "amd64"
        or runtime_image["user"] != "10001:10001"
    ):
        raise ReadinessError(
            "runtime_image must be linux/amd64 with Config.User 10001:10001"
        )
    base = _require_exact_keys(document["base_image"], _BASE_IMAGE_KEYS, "base_image")
    base_reference = base["reference"]
    if (
        not isinstance(base_reference, str)
        or not base_reference.startswith(qualification.BASE_IMAGE_PREFIX)
    ):
        raise ReadinessError("base_image.reference is not the fixed PyTorch base")
    _validate_sha256(
        base_reference.removeprefix(qualification.BASE_IMAGE_PREFIX),
        "base_image.reference digest",
    )
    if base["os"] != "linux" or base["architecture"] != "amd64":
        raise ReadinessError("base_image must be reviewed as linux/amd64")

    review = _require_exact_keys(document["review"], _REVIEW_KEYS, "review")
    reviewed_at = review["reviewed_at"]
    reviewer = review["reviewer"]
    if not isinstance(reviewed_at, str) or _UTC_TIMESTAMP.fullmatch(reviewed_at) is None:
        raise ReadinessError("review.reviewed_at must be a strict UTC timestamp")
    if not isinstance(reviewer, str) or _REVIEWER.fullmatch(reviewer) is None:
        raise ReadinessError("review.reviewer is not a safe reviewer identifier")

    root = evidence_root or manifest_path.parent
    checks = {
        evidence_type: _missing(
            evidence_type, "required reviewed evidence record was not supplied"
        )
        for evidence_type in REQUIRED_REVIEW_EVIDENCE
    }
    records = document["evidence"]
    if not isinstance(records, list):
        raise ReadinessError("release review evidence must be a JSON array")
    seen: set[str] = set()
    total_size = 0
    for index, raw in enumerate(records):
        record = _require_exact_keys(raw, _EVIDENCE_KEYS, f"evidence[{index}]")
        evidence_type = record["type"]
        if not isinstance(evidence_type, str) or evidence_type not in REQUIRED_REVIEW_EVIDENCE:
            raise ReadinessError(f"evidence[{index}].type is not a required evidence type")
        if evidence_type in seen:
            raise ReadinessError(f"duplicate release review evidence: {evidence_type}")
        seen.add(evidence_type)
        relative = _validate_safe_relative_path(record["path"], f"evidence[{index}].path")
        expected_size = _validate_positive_integer(
            record["size"], f"evidence[{index}].size"
        )
        expected_hash = _validate_sha256(
            record["sha256"], f"evidence[{index}].sha256"
        )
        expected_result = REQUIRED_REVIEW_EVIDENCE[evidence_type]
        if record["result"] != expected_result:
            checks[evidence_type] = _invalid(
                evidence_type,
                f"review result must be {expected_result}",
            )
            continue
        try:
            with _open_evidence_under_root(root, relative) as (stream, file_status):
                if file_status.st_size != expected_size:
                    raise ReadinessError("review evidence size differs from its record")
                total_size += file_status.st_size
                if total_size > _MAX_TOTAL_REVIEW_EVIDENCE_BYTES:
                    raise ReadinessError("review evidence exceeds the total byte limit")
                if _sha256_stream(stream) != expected_hash:
                    raise ReadinessError("review evidence SHA-256 differs from its record")
            checks[evidence_type] = _verified(
                evidence_type,
                "reviewed evidence byte is hash-bound to the runtime review",
            )
        except ReadinessError as exc:
            checks[evidence_type] = _invalid(evidence_type, exc)
    return ReviewEvidence(runtime_digest, base_reference, checks)


def _pair_missing(first: object | None, second: object | None) -> bool:
    return first is None or second is None


def build_report(arguments: Arguments) -> dict[str, object]:
    checks: dict[str, Check] = {
        identifier: _missing(identifier, "required release input was not supplied")
        for identifier in CHECK_IDS
    }
    source_result: dict[str, Any] | None = None
    wheelhouse_result: dict[str, Any] | None = None
    asset_result: dict[str, Any] | None = None
    build_result: dict[str, str] | None = None
    review_result: ReviewEvidence | None = None

    if not _pair_missing(arguments.source_manifest, arguments.source_archive):
        assert arguments.source_manifest is not None
        assert arguments.source_archive is not None
        try:
            source_result = verify_inputs.verify_source(
                arguments.source_manifest, arguments.source_archive
            )
            checks["source-archive"] = _verified(
                "source-archive", "reviewed RVC source archive and manifest verified"
            )
        except (verify_inputs.VerificationError, OSError) as exc:
            checks["source-archive"] = _invalid("source-archive", exc)
    elif arguments.source_manifest is not None or arguments.source_archive is not None:
        checks["source-archive"] = _missing(
            "source-archive", "source manifest and archive must be supplied together"
        )

    if not _pair_missing(arguments.wheelhouse_manifest, arguments.wheelhouse_root):
        assert arguments.wheelhouse_manifest is not None
        assert arguments.wheelhouse_root is not None
        try:
            wheelhouse_result = verify_inputs.verify_wheelhouse(
                arguments.wheelhouse_manifest, arguments.wheelhouse_root
            )
            checks["wheelhouse"] = _verified(
                "wheelhouse", "exact Python 3.11 linux_x86_64 cu124 wheelhouse verified"
            )
        except (verify_inputs.VerificationError, OSError) as exc:
            checks["wheelhouse"] = _invalid("wheelhouse", exc)
    elif arguments.wheelhouse_manifest is not None or arguments.wheelhouse_root is not None:
        checks["wheelhouse"] = _missing(
            "wheelhouse", "wheelhouse manifest and root must be supplied together"
        )

    if not _pair_missing(arguments.asset_manifest, arguments.asset_root):
        assert arguments.asset_manifest is not None
        assert arguments.asset_root is not None
        try:
            asset_result = verify_inputs.verify_assets(
                arguments.asset_manifest, arguments.asset_root
            )
            checks["runtime-assets"] = _verified(
                "runtime-assets", "exact offline RVC model/tool asset inventory verified"
            )
        except (verify_inputs.VerificationError, OSError) as exc:
            checks["runtime-assets"] = _invalid("runtime-assets", exc)
    elif arguments.asset_manifest is not None or arguments.asset_root is not None:
        checks["runtime-assets"] = _missing(
            "runtime-assets", "asset manifest and root must be supplied together"
        )

    if arguments.runtime_build_manifest is not None:
        try:
            build_result = qualification.load_runtime_build_manifest(
                arguments.runtime_build_manifest
            )
            checks["runtime-build-manifest"] = _verified(
                "runtime-build-manifest",
                "strict pre-qualification runtime build identity verified",
            )
        except qualification.QualificationError as exc:
            checks["runtime-build-manifest"] = _invalid("runtime-build-manifest", exc)

    if arguments.runtime_image_digest is not None:
        try:
            _validate_image_digest(arguments.runtime_image_digest, "runtime image digest")
            checks["runtime-image-digest"] = _verified(
                "runtime-image-digest", "non-placeholder runtime image digest supplied"
            )
        except ReadinessError as exc:
            checks["runtime-image-digest"] = _invalid("runtime-image-digest", exc)

    if arguments.release_review is not None:
        try:
            review_result = _validate_review_manifest(
                arguments.release_review, arguments.review_evidence_root
            )
            checks["release-review-manifest"] = _verified(
                "release-review-manifest",
                "strict release review identity and evidence inventory verified",
            )
            checks["base-image-amd64-digest"] = _verified(
                "base-image-amd64-digest",
                "fixed digest-pinned base is reviewed as linux/amd64",
            )
            checks["runtime-image-linux-amd64-user"] = _verified(
                "runtime-image-linux-amd64-user",
                "runtime image is reviewed as linux/amd64 with Config.User 10001:10001",
            )
            checks.update(review_result.checks)
        except ReadinessError as exc:
            checks["release-review-manifest"] = _invalid("release-review-manifest", exc)
            checks["base-image-amd64-digest"] = _blocked(
                "base-image-amd64-digest", "release review manifest is invalid"
            )
            checks["runtime-image-linux-amd64-user"] = _blocked(
                "runtime-image-linux-amd64-user",
                "release review manifest is invalid",
            )
            for evidence_type in REQUIRED_REVIEW_EVIDENCE:
                checks[evidence_type] = _blocked(
                    evidence_type, "release review manifest is invalid"
                )

    if build_result is not None:
        try:
            if arguments.source_manifest is not None and source_result is not None:
                source_manifest_hash = _sha256_file(arguments.source_manifest)
                if build_result["RVC_SOURCE_MANIFEST_SHA256"] != source_manifest_hash:
                    raise ReadinessError("runtime build is not bound to the source manifest")
            if wheelhouse_result is not None and build_result[
                "RVC_WHEELHOUSE_MANIFEST_SHA256"
            ] != wheelhouse_result["manifest_sha256"]:
                raise ReadinessError("runtime build is not bound to the wheelhouse manifest")
            if asset_result is not None and build_result[
                "RVC_ASSET_MANIFEST_SHA256"
            ] != asset_result["manifest_sha256"]:
                raise ReadinessError("runtime build is not bound to the asset manifest")
            if review_result is not None and build_result["BASE_IMAGE"] != review_result.base_image:
                raise ReadinessError("runtime build and amd64 base review differ")
        except ReadinessError as exc:
            checks["runtime-build-manifest"] = _invalid("runtime-build-manifest", exc)

    if review_result is not None and arguments.runtime_image_digest is not None:
        if review_result.runtime_image_digest != arguments.runtime_image_digest:
            checks["runtime-image-digest"] = _invalid(
                "runtime-image-digest", "runtime digest and release review differ"
            )
        else:
            checks["runtime-image-digest"] = _verified(
                "runtime-image-digest",
                "runtime digest is reviewed as linux/amd64 non-root image identity",
            )

    qualification_inputs = (
        arguments.qualification_manifest,
        arguments.qualification_evidence,
        arguments.runtime_build_manifest,
        arguments.asset_manifest,
        arguments.runtime_image_digest,
    )
    qualification_supplied = any(value is not None for value in qualification_inputs[:2])
    if qualification_supplied and all(value is not None for value in qualification_inputs):
        assert arguments.qualification_manifest is not None
        assert arguments.qualification_evidence is not None
        assert arguments.runtime_build_manifest is not None
        assert arguments.asset_manifest is not None
        assert arguments.runtime_image_digest is not None
        try:
            qualification.verify_qualification_evidence(
                qualification_path=arguments.qualification_manifest,
                evidence_archive_path=arguments.qualification_evidence,
                runtime_build_manifest_path=arguments.runtime_build_manifest,
                asset_manifest_path=arguments.asset_manifest,
                runtime_image_digest=arguments.runtime_image_digest,
            )
            checks["qualification-49-case"] = _verified(
                "qualification-49-case",
                f"exact {len(qualification.REQUIRED_CASE_IDS)}-case archive and identity verified",
            )
        except qualification.QualificationError as exc:
            checks["qualification-49-case"] = _invalid("qualification-49-case", exc)
    elif qualification_supplied:
        checks["qualification-49-case"] = _blocked(
            "qualification-49-case",
            "qualification requires manifest, archive, build, asset manifest, and image digest",
        )

    ordered = [checks[identifier] for identifier in CHECK_IDS]
    verified = [check.identifier for check in ordered if check.status == "verified"]
    missing = [check.identifier for check in ordered if check.status == "missing"]
    invalid = [
        check.identifier
        for check in ordered
        if check.status in {"invalid", "blocked-dependency"}
    ]
    complete = len(verified) == len(ordered)
    return {
        "format_version": FORMAT_VERSION,
        "kind": REPORT_KIND,
        "decision": "evidence-inputs-verified" if complete else "blocked",
        "activation_permitted": False,
        "activation_projection_written": False,
        "required_qualification_case_count": len(qualification.REQUIRED_CASE_IDS),
        "checks": [check.as_json() for check in ordered],
        "verified": verified,
        "missing": missing,
        "invalid": invalid,
    }


def _canonical_json(value: dict[str, object]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_report(path: Path, content: bytes) -> None:
    parent = path.parent
    try:
        parent_status = parent.stat()
        target_status = path.lstat() if path.exists() or path.is_symlink() else None
    except OSError as exc:
        raise ReadinessError("report output path cannot be inspected safely") from exc
    if not stat.S_ISDIR(parent_status.st_mode) or parent.is_symlink():
        raise ReadinessError("report output parent must be a real directory")
    if target_status is not None and not stat.S_ISREG(target_status.st_mode):
        raise ReadinessError("report output may replace only a regular file")

    descriptor = -1
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
        written = 0
        while written < len(content):
            written += os.write(descriptor, content[written:])
        os.fchmod(descriptor, 0o644)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary_name, path)
        temporary_name = ""
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise ReadinessError("report could not be published atomically") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Enumerate native Worker release evidence without creating or enabling activation"
        )
    )
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--source-archive", type=Path)
    parser.add_argument("--wheelhouse-manifest", type=Path)
    parser.add_argument("--wheelhouse-root", type=Path)
    parser.add_argument("--asset-manifest", type=Path)
    parser.add_argument("--asset-root", type=Path)
    parser.add_argument("--runtime-build-manifest", type=Path)
    parser.add_argument("--runtime-image-digest")
    parser.add_argument("--qualification-manifest", type=Path)
    parser.add_argument("--qualification-evidence", type=Path)
    parser.add_argument("--release-review", type=Path)
    parser.add_argument("--review-evidence-root", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def _arguments(namespace: argparse.Namespace) -> Arguments:
    return Arguments(
        source_manifest=namespace.source_manifest,
        source_archive=namespace.source_archive,
        wheelhouse_manifest=namespace.wheelhouse_manifest,
        wheelhouse_root=namespace.wheelhouse_root,
        asset_manifest=namespace.asset_manifest,
        asset_root=namespace.asset_root,
        runtime_build_manifest=namespace.runtime_build_manifest,
        runtime_image_digest=namespace.runtime_image_digest,
        qualification_manifest=namespace.qualification_manifest,
        qualification_evidence=namespace.qualification_evidence,
        release_review=namespace.release_review,
        review_evidence_root=namespace.review_evidence_root,
    )


def main(argv: Sequence[str] | None = None) -> int:
    namespace = build_parser().parse_args(argv)
    report = build_report(_arguments(namespace))
    content = _canonical_json(report)
    try:
        if namespace.output is None:
            sys.stdout.buffer.write(content)
        else:
            _write_report(namespace.output, content)
    except ReadinessError as exc:
        print(f"release readiness error: {exc}", file=sys.stderr)
        return 2
    return 0 if report["decision"] == "evidence-inputs-verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
