"""Strict release-owned activation projection for native sample inference.

The qualification evidence itself is produced outside the Worker image.  The
release builder projects only the immutable decision fields below into a
read-only file.  The Worker accepts either the exact disabled template or a
fully qualified document; partially enabled documents are rejected.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runner import RvcRunnerError


class RuntimeActivationError(RvcRunnerError):
    """The release-owned runtime activation projection is unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class QualifiedNativeSampleRuntime:
    """Immutable evidence required to construct the production sample runner."""

    runtime_image_digest: str
    runtime_asset_manifest_sha256: str
    qualification_evidence_sha256: str


_MAX_ACTIVATION_BYTES = 64 * 1024
_MAX_ASSET_MANIFEST_BYTES = 16 * 1024**2
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXPECTED_KEYS = {
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
_INFERENCE_F0_METHODS = ["pm", "harvest", "crepe", "rmvpe"]


def load_runtime_activation(
    path: Path,
    *,
    native_source_root: Path,
) -> QualifiedNativeSampleRuntime | None:
    """Load an exact disabled template or fully qualified activation document.

    A missing projection means the release has not activated sample inference.
    Once a file exists, every structural, permission, and provenance mismatch is
    fatal rather than being downgraded to an unqualified runtime.
    """

    try:
        content = _read_regular_file(
            path,
            maximum=_MAX_ACTIVATION_BYTES,
            require_read_only=True,
        )
    except FileNotFoundError:
        return None
    document = _decode_document(content)
    if set(document) != _EXPECTED_KEYS:
        raise RuntimeActivationError("runtime activation fields are invalid")
    if (
        type(document["format_version"]) is not int
        or document["format_version"] != 1
        or document["kind"] != "rvc-runtime-activation"
    ):
        raise RuntimeActivationError("runtime activation format is unsupported")

    runtime_image_digest = document["runtime_image_digest"]
    asset_manifest_sha256 = document["runtime_asset_manifest_sha256"]
    qualification_evidence_sha256 = document["qualification_evidence_sha256"]
    gpu_smoke_verified = document["gpu_smoke_verified"]
    profile_stage_set_verified = document["profile_stage_set_verified"]
    native_sample_inference_verified = document["native_sample_inference_verified"]
    methods = document["supported_inference_f0_methods"]

    disabled = (
        runtime_image_digest is None
        and asset_manifest_sha256 is None
        and qualification_evidence_sha256 is None
        and gpu_smoke_verified is False
        and profile_stage_set_verified is False
        and native_sample_inference_verified is False
        and isinstance(methods, list)
        and methods == []
    )
    if disabled:
        return None

    qualified = (
        isinstance(runtime_image_digest, str)
        and _IMAGE_DIGEST.fullmatch(runtime_image_digest) is not None
        and isinstance(asset_manifest_sha256, str)
        and _SHA256.fullmatch(asset_manifest_sha256) is not None
        and isinstance(qualification_evidence_sha256, str)
        and _SHA256.fullmatch(qualification_evidence_sha256) is not None
        and gpu_smoke_verified is True
        and profile_stage_set_verified is True
        and native_sample_inference_verified is True
        and isinstance(methods, list)
        and methods == _INFERENCE_F0_METHODS
    )
    if not qualified:
        raise RuntimeActivationError(
            "runtime activation must be fully qualified or the exact disabled template"
        )

    try:
        asset_manifest = _read_regular_file(
            native_source_root / "assets-manifest.json",
            maximum=_MAX_ASSET_MANIFEST_BYTES,
            require_read_only=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeActivationError("qualified runtime asset manifest is missing") from exc
    actual_asset_sha256 = hashlib.sha256(asset_manifest).hexdigest()
    if actual_asset_sha256 != asset_manifest_sha256:
        raise RuntimeActivationError("runtime activation does not match the native asset manifest")
    return QualifiedNativeSampleRuntime(
        runtime_image_digest=runtime_image_digest,
        runtime_asset_manifest_sha256=asset_manifest_sha256,
        qualification_evidence_sha256=qualification_evidence_sha256,
    )


def _decode_document(content: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeActivationError("runtime activation has duplicate JSON keys")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise RuntimeActivationError("runtime activation contains a non-finite number")

    try:
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeActivationError("runtime activation is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise RuntimeActivationError("runtime activation root must be an object")
    return document


def _read_regular_file(
    path: Path,
    *,
    maximum: int,
    require_read_only: bool,
) -> bytes:
    try:
        descriptor = _open_absolute_nofollow(path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RuntimeActivationError("runtime activation input cannot be opened safely") from exc
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode) or initial.st_size <= 0 or initial.st_size > maximum:
            raise RuntimeActivationError("runtime activation input metadata is invalid")
        if require_read_only and initial.st_mode & 0o222:
            raise RuntimeActivationError("runtime activation projection must be read-only")
        content = bytearray()
        while True:
            chunk = os.read(descriptor, min(1024**2, maximum + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > maximum:
                raise RuntimeActivationError("runtime activation input exceeds its size limit")
        final = os.fstat(descriptor)
        if _stat_identity(initial) != _stat_identity(final) or len(content) != initial.st_size:
            raise RuntimeActivationError("runtime activation input changed while being read")
        return bytes(content)
    except OSError as exc:
        raise RuntimeActivationError("runtime activation input cannot be read safely") from exc
    finally:
        os.close(descriptor)


def _open_absolute_nofollow(path: Path) -> int:
    rendered = str(path)
    if "\x00" in rendered or not path.is_absolute() or path != Path(os.path.abspath(rendered)):
        raise RuntimeActivationError("runtime activation path must be absolute and normalized")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RuntimeActivationError("runtime activation requires O_NOFOLLOW support")
    components = path.parts[1:]
    if not components:
        raise RuntimeActivationError("runtime activation path cannot be the filesystem root")
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | getattr(os, "O_CLOEXEC", 0)
    )
    current = os.open("/", directory_flags)
    try:
        for component in components[:-1]:
            following = os.open(component, directory_flags, dir_fd=current)
            os.close(current)
            current = following
        return os.open(
            components[-1],
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | nofollow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=current,
        )
    finally:
        os.close(current)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
