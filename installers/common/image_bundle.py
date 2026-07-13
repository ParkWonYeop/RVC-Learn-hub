#!/usr/bin/env python3
"""Build and verify the container-image closure carried by an installer bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, Never, cast

FORMAT_VERSION = 2
PLATFORM = "linux/amd64"
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}$")
CONFIG_PATH_RE = re.compile(r"^(?:blobs/sha256/)?([0-9a-f]{64})(?:\.json)?$")

VERSION_LABEL = "org.opencontainers.image.version"
REVISION_LABEL = "org.opencontainers.image.revision"

TOP_LEVEL_KEYS = {
    "format_version",
    "component",
    "version",
    "platform",
    "self_contained",
    "archives",
    "images",
}
ARCHIVE_KEYS = {"path", "sha256", "size"}
IMAGE_KEYS = {
    "role",
    "source_reference",
    "reference",
    "image_id",
    "config_digest",
    "os",
    "architecture",
    "user",
    "archive",
    "release_labels",
}
RELEASE_LABEL_KEYS = {VERSION_LABEL, REVISION_LABEL}
EXPECTED_APPLICATION_USERS = {
    ("manager", "api"): "10001:10001",
    ("manager", "web"): "nextjs",
    ("manager", "mlflow"): "10002:10002",
    ("worker", "runtime"): "10001:10001",
}

ACTIVATION_KEYS = {
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
ACTIVATION_PATH = Path("infra/worker/runtime/runtime-activation.json")
QUALIFICATION_PATH = Path("runtime/qualification/qualification.json")
ASSET_MANIFEST_PATH = Path("runtime/assets-manifest.json")
INFERENCE_F0_METHODS = ["pm", "harvest", "crepe", "rmvpe"]
QUALIFICATION_KEYS = {
    "format_version",
    "kind",
    "runtime",
    "cases",
    "evidence_archive",
    "review",
}
QUALIFICATION_ARCHIVE_KEYS = {"file", "size", "sha256"}
QUALIFICATION_EVIDENCE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.tar\.gz$")
LEDGER_NAMES = {"SHA256SUMS", "RELEASE_SHA256SUMS"}
LEDGER_LINE_RE = re.compile(r"^([0-9a-f]{64})  (.+)$")
MAX_LEDGER_BYTES = 16 * 1024 * 1024
MAX_LEDGER_FILES = 100_000

MANAGER_ENVIRONMENT_IMAGE_KEYS = {
    "api": "API_IMAGE",
    "web": "WEB_IMAGE",
    "mlflow": "MLFLOW_IMAGE",
    "postgres": "POSTGRES_IMAGE",
    "redis": "REDIS_IMAGE",
    "minio": "MINIO_IMAGE",
    "minio-client": "MINIO_CLIENT_IMAGE",
    "nginx": "NGINX_IMAGE",
}
WORKER_PROVENANCE_ENVIRONMENT_KEYS = (
    "RVC_RUNTIME_INCLUDED",
    "RVC_NATIVE_RUNNER_AVAILABLE",
    "RVC_RUNTIME_IMAGE",
    "RVC_SOURCE_COMMIT",
    "RVC_BASE_IMAGE",
    "RVC_FAIRSEQ_COMMIT",
    "RVC_SOURCE_MANIFEST_SHA256",
    "RVC_WHEELHOUSE_MANIFEST_SHA256",
    "RVC_ASSET_MANIFEST_SHA256",
    "RVC_PROJECTION_MANIFEST_SHA256",
    "RVC_GPU_SMOKE_VERIFIED",
    "RVC_PROFILE_STAGE_SET_VERIFIED",
    "RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED",
)


class VerificationError(ValueError):
    """An image bundle did not satisfy its closed-world contract."""


def _fail(message: str) -> Never:
    raise VerificationError(message)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_regular_file(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        _fail(f"required file is missing: {path.name}")
    if not stat.S_ISREG(metadata.st_mode):
        _fail(f"path is not a regular non-symlink file: {path.name}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _load_manifest(root: Path) -> dict[str, Any]:
    raw = _read_regular_file(root / "images-manifest.json")
    try:
        document = json.loads(raw, object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"images manifest is not valid UTF-8 JSON: {exc}")
    if not isinstance(document, dict):
        _fail("images manifest must be a JSON object")
    return cast(dict[str, Any], document)


def _require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = ",".join(sorted(expected - actual)) or "none"
        extra = ",".join(sorted(actual - expected)) or "none"
        _fail(f"{label} keys differ (missing={missing}; extra={extra})")


def _expected_roles(component: str) -> tuple[str, ...]:
    if component == "manager":
        return (
            "api",
            "web",
            "mlflow",
            "postgres",
            "redis",
            "minio",
            "minio-client",
            "nginx",
        )
    if component == "worker":
        return ("runtime",)
    _fail(f"unsupported component: {component}")


def _application_roles(component: str) -> set[str]:
    if component == "manager":
        return {"api", "web", "mlflow"}
    if component == "worker":
        return {"runtime"}
    _fail(f"unsupported component: {component}")


def _expected_application_user(component: str, role: str) -> str | None:
    return EXPECTED_APPLICATION_USERS.get((component, role))


def _validate_image_user(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8")) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _fail(f"invalid container image user for role {role}")
    return value


def _expected_source_reference(component: str, version: str, role: str) -> str | None:
    manager_references = {
        "api": f"rvc-orchestrator-api:{version}",
        "web": f"rvc-orchestrator-web:{version}",
        "mlflow": f"rvc-orchestrator-mlflow:{version}",
        "postgres": "postgres:16-alpine",
        "redis": "redis:7.4-alpine",
        "minio": "minio/minio:RELEASE.2025-04-22T22-12-26Z",
        "minio-client": "minio/mc:RELEASE.2025-04-16T18-13-26Z",
        "nginx": "nginx:1.27-alpine",
    }
    if component == "manager":
        return manager_references.get(role)
    if component == "worker" and role == "runtime":
        return f"rvc-orchestrator-worker:{version}"
    return None


def _expected_runtime_reference(
    component: str, version: str, role: str, self_contained: bool
) -> str | None:
    source = _expected_source_reference(component, version, role)
    if component != "manager" or not self_contained:
        return source
    dependency_aliases = {
        "postgres": f"rvc-orchestrator-postgres:{version}",
        "redis": f"rvc-orchestrator-redis:{version}",
        "minio": f"rvc-orchestrator-minio:{version}",
        "minio-client": f"rvc-orchestrator-minio-client:{version}",
        "nginx": f"rvc-orchestrator-nginx:{version}",
    }
    return dependency_aliases.get(role, source)


def _validate_relative_archive_path(
    root: Path, value: Any, *, require_parent: bool = True
) -> tuple[str, Path]:
    if not isinstance(value, str) or not value:
        _fail("archive path must be a non-empty string")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or str(pure) != value
        or any(part in {"", ".", ".."} for part in pure.parts)
        or len(pure.parts) != 2
        or not value.startswith("images/")
        or not (value.endswith(".tar") or value.endswith(".tar.gz"))
    ):
        _fail(f"unsafe image archive path: {value}")
    candidate = root.joinpath(*pure.parts)
    if require_parent:
        current = root
        for part in pure.parts[:-1]:
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                _fail(f"image archive parent is missing: {value}")
            if not stat.S_ISDIR(metadata.st_mode):
                _fail(f"image archive parent is unsafe: {value}")
    return value, candidate


def _hash_regular_file(path: Path) -> tuple[str, int]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        _fail(f"image archive is missing: {path.name}")
    if not stat.S_ISREG(metadata.st_mode):
        _fail(f"image archive is not a regular non-symlink file: {path.name}")
    digest = hashlib.sha256()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest(), metadata.st_size


def _validate_ledger_name(value: Any) -> str:
    if not isinstance(value, str) or value not in LEDGER_NAMES:
        _fail("unsupported checksum ledger name")
    return value


def _validate_ledger_path(value: str, ledger_name: str) -> str:
    if not value or len(value.encode("utf-8")) > 4096:
        _fail("checksum ledger contains an invalid path")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or str(pure) != value
        or any(part in {"", ".", ".."} for part in pure.parts)
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or value == ledger_name
    ):
        _fail(f"checksum ledger contains an unsafe path: {value}")
    return value


def _regular_file_inventory(root: Path, ledger_name: str) -> dict[str, Path]:
    inventory: dict[str, Path] = {}
    pending: list[tuple[Path, PurePosixPath]] = [(root, PurePosixPath())]
    entry_count = 0
    while pending:
        directory, relative_directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            _fail(f"checksum inventory directory is unreadable: {exc}")
        for entry in entries:
            relative = relative_directory / entry.name
            relative_text = relative.as_posix()
            entry_count += 1
            if entry_count > MAX_LEDGER_FILES * 2:
                _fail("checksum inventory exceeds the entry limit")
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                _fail(f"checksum inventory entry is unreadable: {relative_text}: {exc}")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append((Path(entry.path), relative))
                continue
            if not stat.S_ISREG(metadata.st_mode):
                _fail(f"checksum inventory contains a non-regular entry: {relative_text}")
            if relative_text == ledger_name:
                continue
            _validate_ledger_path(relative_text, ledger_name)
            inventory[relative_text] = Path(entry.path)
            if len(inventory) > MAX_LEDGER_FILES:
                _fail("checksum inventory exceeds the file limit")
    return inventory


def _load_checksum_ledger(root: Path, ledger_name: str) -> dict[str, str]:
    ledger_path = root / ledger_name
    try:
        metadata = ledger_path.lstat()
    except FileNotFoundError:
        _fail(f"checksum ledger is missing: {ledger_name}")
    if not stat.S_ISREG(metadata.st_mode):
        _fail(f"checksum ledger is not a regular non-symlink file: {ledger_name}")
    if ledger_name == "RELEASE_SHA256SUMS" and metadata.st_mode & 0o222:
        _fail("installed release checksum ledger must be read-only")
    raw = _read_regular_file(ledger_path)
    if not raw or len(raw) > MAX_LEDGER_BYTES:
        _fail("checksum ledger has an invalid size")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _fail("checksum ledger is not valid UTF-8")
    if not text.endswith("\n"):
        _fail("checksum ledger must end with a newline")
    expected: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = LEDGER_LINE_RE.fullmatch(line)
        if match is None:
            _fail(f"checksum ledger has an invalid line: {line_number}")
        digest, relative = match.groups()
        relative = _validate_ledger_path(relative, ledger_name)
        if relative in expected:
            _fail(f"checksum ledger contains a duplicate path: {relative}")
        expected[relative] = digest
        if len(expected) > MAX_LEDGER_FILES:
            _fail("checksum ledger exceeds the file limit")
    return expected


def _verify_checksum_ledger(root: Path, ledger_name: str) -> None:
    expected = _load_checksum_ledger(root, ledger_name)
    actual = _regular_file_inventory(root, ledger_name)
    if set(expected) != set(actual):
        missing = ",".join(sorted(set(expected) - set(actual))) or "none"
        extra = ",".join(sorted(set(actual) - set(expected))) or "none"
        _fail(f"checksum inventory differs (missing={missing}; extra={extra})")
    for relative in sorted(expected):
        digest, _ = _hash_regular_file(actual[relative])
        if digest != expected[relative]:
            _fail(f"checksum mismatch: {relative}")


def _verify_ledger(args: argparse.Namespace) -> None:
    root = Path(args.root)
    if not root.is_dir() or root.is_symlink():
        _fail("checksum root must be a regular directory")
    _verify_checksum_ledger(root, _validate_ledger_name(args.ledger_name))


def _create_ledger(args: argparse.Namespace) -> None:
    root = Path(args.root)
    if not root.is_dir() or root.is_symlink():
        _fail("checksum root must be a regular directory")
    ledger_name = _validate_ledger_name(args.ledger_name)
    destination = root / ledger_name
    if destination.exists() or destination.is_symlink():
        _fail(f"checksum ledger output already exists: {ledger_name}")
    inventory = _regular_file_inventory(root, ledger_name)
    lines: list[str] = []
    for relative in sorted(inventory):
        digest, _ = _hash_regular_file(inventory[relative])
        lines.append(f"{digest}  {relative}\n")
    temporary = root / f".{ledger_name}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o444)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.writelines(lines)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o444, follow_symlinks=False)
        os.replace(temporary, destination)
        directory_descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
    _verify_checksum_ledger(root, ledger_name)


def _safe_tar_member_name(value: str) -> bool:
    pure = PurePosixPath(value)
    return bool(value) and not pure.is_absolute() and str(pure) == value and ".." not in pure.parts


def _read_docker_config_member(path: Path, member_name: str) -> bytes:
    try:
        with tarfile.open(path, mode="r:*") as archive:
            try:
                member = archive.getmember(member_name)
            except KeyError:
                _fail(f"Docker image archive Config member is missing: {path.name}")
            if not member.isreg() or member.size <= 0 or member.size > 4 * 1024 * 1024:
                _fail(f"Docker image archive Config has an invalid size: {path.name}")
            stream = archive.extractfile(member)
            if stream is None:
                _fail(f"Docker image archive Config is unreadable: {path.name}")
            raw = stream.read(member.size + 1)
    except (tarfile.TarError, OSError, EOFError) as exc:
        _fail(f"Docker image archive Config is invalid: {path.name}: {exc}")
    if len(raw) != member.size:
        _fail(f"Docker image archive Config is truncated: {path.name}")
    return raw


def _docker_config_user(raw: bytes, path: Path) -> str:
    try:
        document = json.loads(raw, object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"Docker image archive Config is invalid JSON: {path.name}: {exc}")
    if not isinstance(document, dict):
        _fail(f"Docker image archive Config must be an object: {path.name}")
    config = document.get("config")
    if not isinstance(config, dict):
        _fail(f"Docker image archive Config lacks a config object: {path.name}")
    return _validate_image_user(config.get("User", ""), "archive")


def _verify_docker_save_archive(
    path: Path, expected_images: list[dict[str, Any]] | None
) -> dict[str, str]:
    manifest_raw: bytes | None = None
    member_names: set[str] = set()
    regular_members: set[str] = set()
    member_count = 0
    try:
        with tarfile.open(path, mode="r|*") as archive:
            for member in archive:
                member_count += 1
                if member_count > 100_000:
                    _fail(f"Docker image archive has too many members: {path.name}")
                if not _safe_tar_member_name(member.name):
                    _fail(f"Docker image archive has an unsafe member: {path.name}")
                if member.name in member_names:
                    _fail(f"Docker image archive has a duplicate member: {path.name}")
                member_names.add(member.name)
                if member.isdir():
                    continue
                if not member.isreg():
                    _fail(f"Docker image archive has a non-regular member: {path.name}")
                regular_members.add(member.name)
                if member.name == "manifest.json":
                    if member.size <= 0 or member.size > 4 * 1024 * 1024:
                        _fail(f"Docker image archive manifest has an invalid size: {path.name}")
                    stream = archive.extractfile(member)
                    if stream is None:
                        _fail(f"Docker image archive manifest is unreadable: {path.name}")
                    manifest_raw = stream.read(member.size + 1)
                    if len(manifest_raw) != member.size:
                        _fail(f"Docker image archive manifest is truncated: {path.name}")
    except (tarfile.TarError, OSError, EOFError) as exc:
        _fail(f"Docker image archive is invalid: {path.name}: {exc}")
    if manifest_raw is None:
        _fail(f"Docker image archive lacks manifest.json: {path.name}")
    try:
        manifest = json.loads(manifest_raw, object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"Docker image archive manifest is invalid JSON: {path.name}: {exc}")
    if not isinstance(manifest, list) or len(manifest) > 1024:
        _fail(f"Docker image archive manifest must be a bounded array: {path.name}")

    expected_by_reference = (
        {item["reference"]: item for item in expected_images}
        if expected_images is not None
        else None
    )
    actual_references: set[str] = set()
    config_digests_by_reference: dict[str, str] = {}
    actual_configs: set[str] = set()
    layer_members: set[str] = set()
    for index, entry in enumerate(manifest):
        if not isinstance(entry, dict):
            _fail(f"Docker image archive entry {index} is not an object: {path.name}")
        _require_exact_keys(entry, {"Config", "RepoTags", "Layers"}, "Docker archive entry")
        config = entry["Config"]
        repo_tags = entry["RepoTags"]
        layers = entry["Layers"]
        if not isinstance(config, str) or not _safe_tar_member_name(config):
            _fail(f"Docker image archive Config is unsafe: {path.name}")
        config_match = CONFIG_PATH_RE.fullmatch(config)
        if config_match is None:
            _fail(f"Docker image archive Config is not digest-addressed: {path.name}")
        config_digest = f"sha256:{config_match.group(1)}"
        if config in actual_configs:
            _fail(f"Docker image archive has a duplicate Config entry: {path.name}")
        actual_configs.add(config)
        if config not in regular_members:
            _fail(f"Docker image archive Config member is missing: {path.name}")
        config_raw = _read_docker_config_member(path, config)
        if hashlib.sha256(config_raw).hexdigest() != config_match.group(1):
            _fail(f"Docker image archive Config content digest differs: {path.name}")
        config_user = _docker_config_user(config_raw, path)
        if not isinstance(repo_tags, list) or not repo_tags:
            _fail(f"Docker image archive RepoTags is empty or invalid: {path.name}")
        if not all(isinstance(tag, str) and REFERENCE_RE.fullmatch(tag) for tag in repo_tags):
            _fail(f"Docker image archive RepoTags is invalid: {path.name}")
        if len(repo_tags) != len(set(repo_tags)):
            _fail(f"Docker image archive has duplicate RepoTags: {path.name}")
        if not isinstance(layers, list) or not all(
            isinstance(layer, str) and _safe_tar_member_name(layer) for layer in layers
        ):
            _fail(f"Docker image archive Layers is invalid: {path.name}")
        for layer in layers:
            if layer not in regular_members:
                _fail(f"Docker image archive layer member is missing: {path.name}")
            layer_members.add(layer)
        for reference in repo_tags:
            if reference in actual_references:
                _fail(f"Docker image archive has a duplicate RepoTag: {reference}")
            actual_references.add(reference)
            config_digests_by_reference[reference] = config_digest
            if expected_by_reference is not None:
                expected = expected_by_reference.get(reference)
                if expected is None:
                    _fail(f"Docker image archive has an unexpected RepoTag: {reference}")
                if expected["config_digest"] != config_digest:
                    _fail(f"Docker image archive Config digest differs for {reference}")
                if expected["user"] != config_user:
                    _fail(f"Docker image archive Config user differs for {reference}")
    if expected_by_reference is not None and actual_references != set(expected_by_reference):
        missing = ",".join(sorted(set(expected_by_reference) - actual_references)) or "none"
        _fail(f"Docker image archive is missing expected RepoTags: {missing}")
    config_like_members = {
        name for name in regular_members if CONFIG_PATH_RE.fullmatch(name) is not None
    } - layer_members
    if expected_images is not None:
        expected_identity_members: set[str] = set()
        for item in expected_images:
            digest = item["image_id"].removeprefix("sha256:")
            candidates = {
                f"blobs/sha256/{digest}",
                f"{digest}.json",
            } & regular_members
            if not candidates:
                _fail(f"Docker image archive identity descriptor is missing: {path.name}")
            expected_identity_members.update(candidates)
        expected_metadata_members = actual_configs | expected_identity_members
        if config_like_members != expected_metadata_members:
            _fail(f"Docker image archive Config/identity inventory differs: {path.name}")
        for member_name in expected_identity_members - actual_configs:
            match = CONFIG_PATH_RE.fullmatch(member_name)
            if match is None:
                _fail(f"Docker image archive identity descriptor is unsafe: {path.name}")
            raw = _read_docker_config_member(path, member_name)
            if hashlib.sha256(raw).hexdigest() != match.group(1):
                _fail(f"Docker image archive identity descriptor digest differs: {path.name}")
    return config_digests_by_reference


def _docker_field(docker: str, reference: str, template: str) -> str:
    result = subprocess.run(
        [docker, "image", "inspect", "--format", template, reference],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _fail(f"container image inspection failed for {reference}")
    value = result.stdout.strip()
    if "\n" in value or "\r" in value:
        _fail(f"container image inspection returned multiple values for {reference}")
    return value


def _docker_string_list(docker: str, reference: str, field: str) -> list[str]:
    raw = _docker_field(docker, reference, f"{{{{json .{field}}}}}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        _fail(f"container image {field} is not valid JSON for {reference}")
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail(f"container image {field} is invalid for {reference}")
    return cast(list[str], value)


def _inspect_image(
    docker: str,
    component: str,
    version: str,
    source_commit: str,
    role: str,
    source_reference: str,
    reference: str,
    archive: str,
    *,
    verify_source: bool,
) -> dict[str, Any]:
    image_id = _docker_field(docker, reference, "{{.Id}}")
    operating_system = _docker_field(docker, reference, "{{.Os}}")
    architecture = _docker_field(docker, reference, "{{.Architecture}}")
    user = _validate_image_user(
        _docker_field(docker, reference, '{{with index .Config "User"}}{{.}}{{end}}'),
        role,
    )
    references = _docker_string_list(docker, reference, "RepoTags")
    references.extend(_docker_string_list(docker, reference, "RepoDigests"))
    if reference not in references:
        _fail(f"container image tag/reference mismatch for role {role}")
    if not DIGEST_RE.fullmatch(image_id):
        _fail(f"container image ID is not a SHA-256 digest for role {role}")
    if operating_system != "linux" or architecture != "amd64":
        _fail(f"container image for role {role} must be linux/amd64")
    expected_user = _expected_application_user(component, role)
    if expected_user is not None and user != expected_user:
        _fail(f"container image user mismatch for role {role}")
    if verify_source:
        source_image_id = _docker_field(docker, source_reference, "{{.Id}}")
        if source_image_id != image_id:
            _fail(f"source/runtime image identity mismatch for role {role}")

    release_labels: dict[str, str] = {}
    if role in _application_roles(component):
        for label, expected in (
            (VERSION_LABEL, version),
            (REVISION_LABEL, source_commit),
        ):
            template = f'{{{{ index .Config.Labels "{label}" }}}}'
            actual = _docker_field(docker, reference, template)
            if actual != expected:
                _fail(f"container image release label mismatch for role {role}: {label}")
            release_labels[label] = actual

    return {
        "role": role,
        "source_reference": source_reference,
        "reference": reference,
        "image_id": image_id,
        "config_digest": image_id,
        "os": operating_system,
        "architecture": architecture,
        "user": user,
        "archive": archive,
        "release_labels": release_labels,
    }


def _parse_image_spec(value: str) -> tuple[str, str, str, str]:
    parts = value.split("|", 3)
    if len(parts) != 4 or not all(parts):
        _fail("image spec must be ROLE|SOURCE_REFERENCE|REFERENCE|ARCHIVE")
    role, source_reference, reference, archive = parts
    if not ROLE_RE.fullmatch(role):
        _fail(f"invalid image role: {role}")
    if not REFERENCE_RE.fullmatch(source_reference):
        _fail(f"invalid source image reference for role {role}")
    if not REFERENCE_RE.fullmatch(reference):
        _fail(f"invalid image reference for role {role}")
    return role, source_reference, reference, archive


def _validate_manifest(
    root: Path,
    document: dict[str, Any],
    component: str,
    version: str,
    source_commit: str,
    *,
    verify_archive_bytes: bool,
    require_activation_read_only: bool = False,
) -> None:
    _require_exact_keys(document, TOP_LEVEL_KEYS, "images manifest")
    if document["format_version"] != FORMAT_VERSION:
        _fail("unsupported images manifest format version")
    if document["component"] != component:
        _fail("images manifest component mismatch")
    if document["version"] != version:
        _fail("images manifest version mismatch")
    if document["platform"] != PLATFORM:
        _fail("images manifest platform must be linux/amd64")
    if type(document["self_contained"]) is not bool:
        _fail("images manifest self_contained must be a boolean")
    if not VERSION_RE.fullmatch(version):
        _fail("invalid images manifest version")
    if not isinstance(source_commit, str) or not source_commit:
        _fail("source commit is missing")
    if document["self_contained"] and not COMMIT_RE.fullmatch(source_commit):
        _fail("self-contained image bundles require a committed source revision")

    archives = document["archives"]
    images = document["images"]
    if not isinstance(archives, list) or not isinstance(images, list):
        _fail("images manifest archives and images must be arrays")

    archive_paths: set[str] = set()
    archive_local_paths: dict[str, Path] = {}
    for index, item in enumerate(archives):
        if not isinstance(item, dict):
            _fail(f"archive entry {index} must be an object")
        _require_exact_keys(item, ARCHIVE_KEYS, f"archive entry {index}")
        archive_path, local_path = _validate_relative_archive_path(
            root, item["path"], require_parent=verify_archive_bytes
        )
        if archive_path in archive_paths:
            _fail(f"duplicate image archive path: {archive_path}")
        archive_paths.add(archive_path)
        archive_local_paths[archive_path] = local_path
        if not isinstance(item["sha256"], str) or not HASH_RE.fullmatch(item["sha256"]):
            _fail(f"invalid image archive SHA-256: {archive_path}")
        if type(item["size"]) is not int or item["size"] <= 0:
            _fail(f"invalid image archive size: {archive_path}")
        if verify_archive_bytes:
            actual_hash, actual_size = _hash_regular_file(local_path)
            if actual_hash != item["sha256"] or actual_size != item["size"]:
                _fail(f"image archive digest or size mismatch: {archive_path}")

    roles: set[str] = set()
    source_references: set[str] = set()
    references: set[str] = set()
    image_ids: set[str] = set()
    config_digests: set[str] = set()
    referenced_archives: set[str] = set()
    app_roles = _application_roles(component)
    for index, item in enumerate(images):
        if not isinstance(item, dict):
            _fail(f"image entry {index} must be an object")
        _require_exact_keys(item, IMAGE_KEYS, f"image entry {index}")
        role = item["role"]
        source_reference = item["source_reference"]
        reference = item["reference"]
        image_id = item["image_id"]
        config_digest = item["config_digest"]
        user = _validate_image_user(item["user"], role if isinstance(role, str) else str(index))
        archive = item["archive"]
        if not isinstance(role, str) or not ROLE_RE.fullmatch(role):
            _fail(f"invalid image role at entry {index}")
        if role in roles:
            _fail(f"duplicate image role: {role}")
        roles.add(role)
        if not isinstance(source_reference, str) or not REFERENCE_RE.fullmatch(source_reference):
            _fail(f"invalid source image reference for role {role}")
        if source_reference in source_references:
            _fail(f"duplicate source image reference: {source_reference}")
        source_references.add(source_reference)
        if not isinstance(reference, str) or not REFERENCE_RE.fullmatch(reference):
            _fail(f"invalid image reference for role {role}")
        if reference in references:
            _fail(f"duplicate image reference: {reference}")
        references.add(reference)
        expected_source = _expected_source_reference(component, version, role)
        if expected_source is not None and source_reference != expected_source:
            _fail(f"source tag/reference mismatch for role {role}")
        expected_reference = _expected_runtime_reference(
            component, version, role, document["self_contained"]
        )
        if expected_reference is not None and reference != expected_reference:
            _fail(f"tag/reference mismatch for role {role}")
        if not isinstance(image_id, str) or not DIGEST_RE.fullmatch(image_id):
            _fail(f"invalid image ID for role {role}")
        if not isinstance(config_digest, str) or not DIGEST_RE.fullmatch(config_digest):
            _fail(f"invalid config digest for role {role}")
        if image_id in image_ids or config_digest in config_digests:
            _fail(f"duplicate container image content for role {role}")
        image_ids.add(image_id)
        config_digests.add(config_digest)
        if item["os"] != "linux" or item["architecture"] != "amd64":
            _fail(f"container image for role {role} must be linux/amd64")
        expected_user = _expected_application_user(component, role)
        if expected_user is not None and user != expected_user:
            _fail(f"container image user mismatch for role {role}")
        if not isinstance(archive, str) or archive not in archive_paths:
            _fail(f"image role {role} refers to an unknown archive")
        referenced_archives.add(archive)
        labels = item["release_labels"]
        if not isinstance(labels, dict):
            _fail(f"release labels for role {role} must be an object")
        expected_label_keys = RELEASE_LABEL_KEYS if role in app_roles else set()
        _require_exact_keys(labels, expected_label_keys, f"release labels for role {role}")
        if role in app_roles and (
            labels[VERSION_LABEL] != version or labels[REVISION_LABEL] != source_commit
        ):
            _fail(f"release labels do not match bundle provenance for role {role}")

    if archive_paths != referenced_archives:
        _fail("every image archive must be referenced by at least one image")
    if document["self_contained"]:
        required_roles = set(_expected_roles(component))
        if roles != required_roles:
            missing = ",".join(sorted(required_roles - roles)) or "none"
            extra = ",".join(sorted(roles - required_roles)) or "none"
            _fail(f"self-contained image roles differ (missing={missing}; extra={extra})")
    if bool(images) != bool(archives):
        _fail("image and archive inventories must either both be empty or both be populated")
    if verify_archive_bytes:
        image_directory = root / "images"
        actual_archive_paths: set[str] = set()
        if image_directory.exists():
            if not image_directory.is_dir() or image_directory.is_symlink():
                _fail("bundle images path is not a regular directory")
            for entry in image_directory.iterdir():
                if entry.name.endswith(".tar") or entry.name.endswith(".tar.gz"):
                    actual_archive_paths.add(f"images/{entry.name}")
        if actual_archive_paths != archive_paths:
            extra = ",".join(sorted(actual_archive_paths - archive_paths)) or "none"
            missing = ",".join(sorted(archive_paths - actual_archive_paths)) or "none"
            _fail(f"image archive inventory differs (missing={missing}; extra={extra})")
        for archive_path, local_path in archive_local_paths.items():
            expected_images = [item for item in images if item["archive"] == archive_path]
            _verify_docker_save_archive(local_path, expected_images)
    if component == "worker":
        _validate_worker_activation(
            root,
            document,
            require_read_only=require_activation_read_only,
        )


def _create(args: argparse.Namespace) -> None:
    root = Path(args.root)
    if not root.is_dir() or root.is_symlink():
        _fail("bundle root must be a regular directory")
    self_contained = args.self_contained == "true"
    specs = [_parse_image_spec(value) for value in args.image]
    if not specs:
        document: dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "component": args.component,
            "version": args.version,
            "platform": PLATFORM,
            "self_contained": self_contained,
            "archives": [],
            "images": [],
        }
    else:
        images = [
            _inspect_image(
                args.docker_command,
                args.component,
                args.version,
                args.source_commit,
                role,
                source_reference,
                reference,
                archive,
                verify_source=True,
            )
            for role, source_reference, reference, archive in specs
        ]
        archive_items: list[dict[str, Any]] = []
        config_digests_by_archive: dict[str, dict[str, str]] = {}
        for archive in sorted({item["archive"] for item in images}):
            _, local_path = _validate_relative_archive_path(root, archive)
            sha256, size = _hash_regular_file(local_path)
            archive_items.append({"path": archive, "sha256": sha256, "size": size})
            config_digests_by_archive[archive] = _verify_docker_save_archive(
                local_path,
                None,
            )
        for item in images:
            config_digest = config_digests_by_archive[item["archive"]].get(item["reference"])
            if config_digest is None:
                _fail(f"Docker image archive is missing expected RepoTag: {item['reference']}")
            item["config_digest"] = config_digest
        document = {
            "format_version": FORMAT_VERSION,
            "component": args.component,
            "version": args.version,
            "platform": PLATFORM,
            "self_contained": self_contained,
            "archives": archive_items,
            "images": sorted(images, key=lambda item: item["role"]),
        }
    _validate_manifest(
        root,
        document,
        args.component,
        args.version,
        args.source_commit,
        verify_archive_bytes=True,
    )
    destination = root / "images-manifest.json"
    if destination.exists() or destination.is_symlink():
        _fail("images manifest output already exists")
    temporary = root / f".images-manifest.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o644)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _verified_document(
    args: argparse.Namespace,
    *,
    verify_archive_bytes: bool,
    require_activation_read_only: bool = False,
) -> dict[str, Any]:
    root = Path(args.root)
    if not root.is_dir() or root.is_symlink():
        _fail("bundle root must be a regular directory")
    document = _load_manifest(root)
    _validate_manifest(
        root,
        document,
        args.component,
        args.version,
        args.source_commit,
        verify_archive_bytes=verify_archive_bytes,
        require_activation_read_only=require_activation_read_only,
    )
    return document


def _verify_bundle(args: argparse.Namespace) -> None:
    _verified_document(args, verify_archive_bytes=True)


def _list_archives(args: argparse.Namespace) -> None:
    document = _verified_document(args, verify_archive_bytes=True)
    for archive in document["archives"]:
        print(archive["path"])


def _print_self_contained(args: argparse.Namespace) -> None:
    document = _verified_document(args, verify_archive_bytes=True)
    print("true" if document["self_contained"] else "false")


def _verify_loaded(args: argparse.Namespace) -> None:
    document = _verified_document(args, verify_archive_bytes=False)
    for expected in document["images"]:
        actual = _inspect_image(
            args.docker_command,
            args.component,
            args.version,
            args.source_commit,
            expected["role"],
            expected["source_reference"],
            expected["reference"],
            expected["archive"],
            verify_source=False,
        )
        # On containerd image stores, Docker exposes an OCI index digest as .Id while
        # docker save carries the image config digest separately. Matching .Id binds
        # the loaded index; archive verification already binds the config bytes.
        actual["config_digest"] = expected["config_digest"]
        if actual != expected:
            _fail(f"loaded container image differs for role {expected['role']}")


def _read_environment(path: Path) -> dict[str, str]:
    raw = _read_regular_file(path)
    if len(raw) > 1024 * 1024:
        _fail("release environment exceeds the verification limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _fail("release environment is not valid UTF-8")
    result: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line or line.startswith("#"):
            continue
        if any(ord(character) < 32 and character != "\t" for character in line):
            _fail(f"release environment has a control character on line {line_number}")
        if "=" not in line:
            _fail(f"release environment has an invalid line {line_number}")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            _fail(f"release environment has an invalid key on line {line_number}")
        if key in result:
            _fail(f"release environment has a duplicate key: {key}")
        result[key] = value
    return result


def _load_runtime_activation(root: Path, *, require_read_only: bool) -> dict[str, Any]:
    path = root / ACTIVATION_PATH
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        _fail("Worker runtime activation is missing")
    if not stat.S_ISREG(metadata.st_mode):
        _fail("Worker runtime activation must be a regular non-symlink file")
    if require_read_only and metadata.st_mode & 0o222:
        _fail("installed Worker runtime activation must be read-only")
    raw = _read_regular_file(path)
    if not raw or len(raw) > 64 * 1024:
        _fail("Worker runtime activation has an invalid size")
    try:
        document = json.loads(raw, object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"Worker runtime activation is not valid UTF-8 JSON: {exc}")
    if not isinstance(document, dict):
        _fail("Worker runtime activation must be a JSON object")
    return cast(dict[str, Any], document)


def _validate_worker_activation(
    root: Path,
    image_document: dict[str, Any],
    *,
    require_read_only: bool,
) -> None:
    activation = _load_runtime_activation(root, require_read_only=require_read_only)
    _require_exact_keys(activation, ACTIVATION_KEYS, "Worker runtime activation")
    if activation["format_version"] != 1:
        _fail("unsupported Worker runtime activation format version")
    if activation["kind"] != "rvc-runtime-activation":
        _fail("Worker runtime activation kind is invalid")

    gates = (
        activation["gpu_smoke_verified"],
        activation["profile_stage_set_verified"],
        activation["native_sample_inference_verified"],
    )
    if any(type(value) is not bool for value in gates):
        _fail("Worker runtime activation gates must be booleans")
    qualified = all(gates)
    if not qualified and any(gates):
        _fail("Worker runtime activation gates cannot be partially enabled")

    digest_keys = (
        "runtime_image_digest",
        "runtime_asset_manifest_sha256",
        "qualification_evidence_sha256",
    )
    methods = activation["supported_inference_f0_methods"]
    if qualified:
        if not isinstance(activation["runtime_image_digest"], str) or not DIGEST_RE.fullmatch(
            activation["runtime_image_digest"]
        ):
            _fail("qualified Worker activation has an invalid runtime image digest")
        for key in digest_keys[1:]:
            value = activation[key]
            if not isinstance(value, str) or not HASH_RE.fullmatch(value):
                _fail(f"qualified Worker activation has an invalid {key}")
        if methods != INFERENCE_F0_METHODS:
            _fail("qualified Worker activation has an invalid inference F0 method set")
        if not image_document["self_contained"]:
            _fail("qualified Worker activation requires a self-contained image bundle")
    else:
        if any(activation[key] is not None for key in digest_keys):
            _fail("unqualified Worker activation must not carry runtime digests")
        if methods != []:
            _fail("unqualified Worker activation must not advertise inference F0 methods")

    bundle_environment = _read_environment(root / "manifest.env")
    expected_gate = "true" if qualified else "false"
    for key in (
        "RVC_GPU_SMOKE_VERIFIED",
        "RVC_PROFILE_STAGE_SET_VERIFIED",
        "RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED",
    ):
        if bundle_environment.get(key) != expected_gate:
            _fail(f"Worker bundle manifest and runtime activation disagree on {key}")

    if not qualified:
        return

    runtime_images = [item for item in image_document["images"] if item.get("role") == "runtime"]
    if len(runtime_images) != 1:
        _fail("qualified Worker activation requires exactly one runtime image")
    if runtime_images[0]["image_id"] != activation["runtime_image_digest"]:
        _fail("Worker runtime activation image digest differs from the image manifest")
    asset_hash, _ = _hash_regular_file(root / ASSET_MANIFEST_PATH)
    if asset_hash != activation["runtime_asset_manifest_sha256"]:
        _fail("Worker runtime activation asset manifest digest differs from installed bytes")
    if bundle_environment.get("RVC_ASSET_MANIFEST_SHA256") != asset_hash:
        _fail("Worker bundle manifest and runtime activation asset digest differ")
    qualification_hash, _ = _hash_regular_file(root / QUALIFICATION_PATH)
    if qualification_hash != activation["qualification_evidence_sha256"]:
        _fail("Worker runtime activation qualification digest differs from installed bytes")
    qualification_path = root / QUALIFICATION_PATH
    qualification_metadata = qualification_path.lstat()
    if qualification_metadata.st_size <= 0 or qualification_metadata.st_size > 1024 * 1024:
        _fail("Worker runtime qualification has an invalid size")
    qualification_raw = _read_regular_file(qualification_path)
    try:
        qualification = json.loads(qualification_raw, object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"Worker runtime qualification is not valid UTF-8 JSON: {exc}")
    if not isinstance(qualification, dict):
        _fail("Worker runtime qualification must be a JSON object")
    _require_exact_keys(qualification, QUALIFICATION_KEYS, "Worker runtime qualification")
    if (
        qualification["format_version"] != 1
        or qualification["kind"] != "rvc-native-runtime-qualification"
    ):
        _fail("Worker runtime qualification format or kind is invalid")
    evidence = qualification["evidence_archive"]
    if not isinstance(evidence, dict):
        _fail("Worker runtime qualification evidence archive must be an object")
    _require_exact_keys(
        evidence,
        QUALIFICATION_ARCHIVE_KEYS,
        "Worker runtime qualification evidence archive",
    )
    evidence_name = evidence["file"]
    evidence_size = evidence["size"]
    evidence_hash = evidence["sha256"]
    if (
        not isinstance(evidence_name, str)
        or not QUALIFICATION_EVIDENCE_NAME_RE.fullmatch(evidence_name)
        or type(evidence_size) is not int
        or not 0 < evidence_size <= 512 * 1024 * 1024
        or not isinstance(evidence_hash, str)
        or not HASH_RE.fullmatch(evidence_hash)
    ):
        _fail("Worker runtime qualification evidence archive identity is invalid")
    actual_evidence_hash, actual_evidence_size = _hash_regular_file(
        root / "runtime/qualification" / evidence_name
    )
    if actual_evidence_hash != evidence_hash or actual_evidence_size != evidence_size:
        _fail("Worker runtime qualification evidence archive digest or size differs")


def _verify_environment(args: argparse.Namespace) -> None:
    document = _verified_document(
        args,
        verify_archive_bytes=False,
        require_activation_read_only=True,
    )
    root = Path(args.root)
    environment = _read_environment(Path(args.environment))
    release_manifest = _read_environment(root / "manifest.env")
    expected_manifest_values = {
        "BUNDLE_FORMAT_VERSION": "2",
        "PRODUCT": "rvc-training-orchestrator",
        "COMPONENT": args.component,
        "VERSION": args.version,
        "PLATFORM": "linux-amd64",
        "GIT_COMMIT": args.source_commit,
        "SELF_CONTAINED": "true" if document["self_contained"] else "false",
        "IMAGES_MANIFEST_FORMAT_VERSION": "2",
        "IMAGES_MANIFEST_PATH": "images-manifest.json",
    }
    for key, expected in expected_manifest_values.items():
        if release_manifest.get(key) != expected:
            _fail(f"release manifest provenance differs for {key}")
    if environment.get("ORCHESTRATOR_VERSION") != args.version:
        _fail("release environment version differs from the release manifest")

    role_keys = (
        MANAGER_ENVIRONMENT_IMAGE_KEYS
        if args.component == "manager"
        else {"runtime": "WORKER_IMAGE"}
    )
    for role in _expected_roles(args.component):
        key = role_keys[role]
        expected_reference = _expected_runtime_reference(
            args.component,
            args.version,
            role,
            document["self_contained"],
        )
        if expected_reference is None or release_manifest.get(key) != expected_reference:
            _fail(f"release manifest image reference differs for role {role}")
        if environment.get(key) != expected_reference:
            _fail(f"release environment image reference differs for role {role}")

    if args.component == "worker":
        for key in WORKER_PROVENANCE_ENVIRONMENT_KEYS:
            expected = release_manifest.get(key)
            if expected is None:
                _fail(f"Worker release manifest is missing {key}")
            if environment.get(key) != expected:
                _fail(f"Worker release environment provenance differs for {key}")
    expected_pull_policy = "never" if document["self_contained"] else "missing"
    if environment.get("RVC_IMAGE_PULL_POLICY") != expected_pull_policy:
        _fail("release environment pull policy differs from the image manifest")


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True)
    parser.add_argument("--component", choices=("manager", "worker"), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    _add_identity_arguments(create)
    create.add_argument("--self-contained", choices=("true", "false"), required=True)
    create.add_argument("--docker-command", default="docker")
    create.add_argument("--image", action="append", default=[])
    create.set_defaults(handler=_create)

    verify = commands.add_parser("verify-bundle")
    _add_identity_arguments(verify)
    verify.set_defaults(handler=_verify_bundle)

    archives = commands.add_parser("list-archives")
    _add_identity_arguments(archives)
    archives.set_defaults(handler=_list_archives)

    contained = commands.add_parser("print-self-contained")
    _add_identity_arguments(contained)
    contained.set_defaults(handler=_print_self_contained)

    loaded = commands.add_parser("verify-loaded")
    _add_identity_arguments(loaded)
    loaded.add_argument("--docker-command", default="docker")
    loaded.set_defaults(handler=_verify_loaded)

    environment = commands.add_parser("verify-environment")
    _add_identity_arguments(environment)
    environment.add_argument("--environment", required=True)
    environment.set_defaults(handler=_verify_environment)

    verify_ledger = commands.add_parser("verify-ledger")
    verify_ledger.add_argument("--root", required=True)
    verify_ledger.add_argument("--ledger-name", choices=tuple(sorted(LEDGER_NAMES)), required=True)
    verify_ledger.set_defaults(handler=_verify_ledger)

    create_ledger = commands.add_parser("create-ledger")
    create_ledger.add_argument("--root", required=True)
    create_ledger.add_argument("--ledger-name", choices=("RELEASE_SHA256SUMS",), required=True)
    create_ledger.set_defaults(handler=_create_ledger)
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        args.handler(args)
    except VerificationError as exc:
        print(f"image bundle verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
