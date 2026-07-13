#!/usr/bin/env python3
"""Verify a private release bundle and publish its archive pair without overwrite."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024 * 1024
MAX_MEMBERS = 200_000
MAX_CHECKSUM_BYTES = 1024
MAX_VERIFIER_BYTES = 4 * 1024 * 1024

VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ARCHIVE_NAME_RE = re.compile(
    r"^rvc-(?P<component>manager|worker)-(?P<version>[A-Za-z0-9][A-Za-z0-9._-]{0,63})"
    r"-linux-amd64\.tar\.gz$"
)


class PublicationError(RuntimeError):
    """The candidate bundle cannot be verified or safely published."""


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


@contextlib.contextmanager
def _open_regular(
    path: Path,
    *,
    maximum: int,
    label: str,
) -> Iterator[tuple[int, os.stat_result]]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise PublicationError("O_NOFOLLOW support is required")
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise PublicationError(f"{label} is missing or cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > maximum
        ):
            raise PublicationError(f"{label} has an unsafe file type or size")
        yield descriptor, metadata
        if _identity(os.fstat(descriptor)) != _identity(metadata):
            raise PublicationError(f"{label} changed while it was being verified")
    finally:
        os.close(descriptor)


def _read_descriptor(
    descriptor: int,
    metadata: os.stat_result,
    *,
    maximum: int,
    label: str,
) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise PublicationError(f"{label} exceeds its size limit")
    if total != metadata.st_size:
        raise PublicationError(f"{label} changed while it was being read")
    return b"".join(chunks)


def _sha256_descriptor(descriptor: int, metadata: os.stat_result, *, label: str) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
        total += len(chunk)
    if total != metadata.st_size:
        raise PublicationError(f"{label} changed while it was hashed")
    return digest.hexdigest()


def _validate_checksum(
    content: bytes,
    *,
    archive_name: str,
    archive_hash: str,
) -> None:
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise PublicationError("release checksum sidecar is not ASCII") from exc
    expected = f"{archive_hash}  {archive_name}\n"
    if text != expected:
        raise PublicationError("release checksum sidecar does not match the archive bytes")


def _safe_member_parts(name: str, root_name: str) -> tuple[str, ...]:
    if not name or len(name.encode("utf-8")) > 4096:
        raise PublicationError("release archive contains an invalid member name")
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise PublicationError("release archive contains a control character in a path")
    path = PurePosixPath(name)
    parts = path.parts
    if (
        path.is_absolute()
        or not parts
        or parts[0] != root_name
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise PublicationError("release archive member escapes the expected root")
    return parts


def _extract_archive(descriptor: int, destination: Path, root_name: str) -> Path:
    os.lseek(descriptor, 0, os.SEEK_SET)
    duplicate = os.dup(descriptor)
    seen: set[str] = set()
    directories: list[tuple[Path, int]] = []
    member_count = 0
    extracted_bytes = 0
    saw_root = False
    try:
        with os.fdopen(duplicate, "rb", closefd=True) as stream:
            duplicate = -1
            with tarfile.open(fileobj=stream, mode="r:gz") as archive:
                for member in archive:
                    member_count += 1
                    if member_count > MAX_MEMBERS:
                        raise PublicationError("release archive has too many members")
                    parts = _safe_member_parts(member.name, root_name)
                    normalized = "/".join(parts)
                    if normalized in seen:
                        raise PublicationError("release archive contains a duplicate member")
                    seen.add(normalized)
                    target = destination.joinpath(*parts)
                    mode = member.mode & 0o777
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        if not target.is_dir() or target.is_symlink():
                            raise PublicationError(
                                "release archive directory conflicts with another member"
                            )
                        directories.append((target, mode))
                        if len(parts) == 1:
                            saw_root = True
                        continue
                    if not member.isfile():
                        raise PublicationError(
                            "release archive may contain only regular files and directories"
                        )
                    if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                        raise PublicationError("release archive member exceeds its size limit")
                    extracted_bytes += member.size
                    if extracted_bytes > MAX_EXTRACTED_BYTES:
                        raise PublicationError(
                            "release archive expands beyond its total size limit"
                        )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise PublicationError("release archive regular member has no payload")
                    written = 0
                    nofollow = getattr(os, "O_NOFOLLOW", 0)
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
                    output = os.open(target, flags, 0o600)
                    try:
                        with source:
                            while True:
                                chunk = source.read(1024 * 1024)
                                if not chunk:
                                    break
                                written += len(chunk)
                                if written > member.size:
                                    raise PublicationError(
                                        "release archive member exceeds its declared size"
                                    )
                                view = memoryview(chunk)
                                while view:
                                    consumed = os.write(output, view)
                                    view = view[consumed:]
                        if written != member.size:
                            raise PublicationError(
                                "release archive member is shorter than its declared size"
                            )
                        os.fchmod(output, mode)
                    finally:
                        os.close(output)
    except PublicationError:
        raise
    except (OSError, EOFError, tarfile.TarError, UnicodeError) as exc:
        raise PublicationError("release archive is not a safe tar.gz file") from exc
    finally:
        if duplicate >= 0:
            os.close(duplicate)
    if not saw_root:
        raise PublicationError("release archive is missing its explicit root directory")
    for directory, mode in reversed(directories):
        directory.chmod(mode)
    root = destination / root_name
    if not root.is_dir() or root.is_symlink():
        raise PublicationError("release archive root is not a real directory")
    return root


def _load_environment(path: Path) -> dict[str, str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PublicationError("release manifest is missing") from exc
    if not raw or len(raw) > 1024 * 1024:
        raise PublicationError("release manifest has an invalid size")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublicationError("release manifest is not UTF-8") from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PublicationError("release manifest contains a malformed line")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise PublicationError("release manifest contains an invalid or duplicate key")
        values[key] = value
    return values


def _run_verifier(
    verifier_source: str,
    root: Path,
    *,
    component: str,
    version: str,
    source_commit: str,
) -> None:
    for command in (
        (
            "verify-ledger",
            "--root",
            str(root),
            "--ledger-name",
            "SHA256SUMS",
        ),
        (
            "verify-bundle",
            "--root",
            str(root),
            "--component",
            component,
            "--version",
            version,
            "--source-commit",
            source_commit,
        ),
    ):
        result = subprocess.run(
            [sys.executable, "-c", verifier_source, *command],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise PublicationError(f"release verifier rejected {command[0]}")


def _verify_extracted_identity(
    root: Path,
    *,
    component: str,
    version: str,
    source_commit: str,
    runtime_image_id: str,
) -> None:
    manifest = _load_environment(root / "manifest.env")
    expected = {
        "BUNDLE_FORMAT_VERSION": "2",
        "PRODUCT": "rvc-training-orchestrator",
        "COMPONENT": component,
        "VERSION": version,
        "PLATFORM": "linux-amd64",
        "GIT_COMMIT": source_commit,
        "SELF_CONTAINED": "true",
    }
    if component == "worker":
        expected.update(
            {
                "RVC_RUNTIME_INCLUDED": "true",
                "RVC_NATIVE_RUNNER_AVAILABLE": "true",
            }
        )
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise PublicationError(f"release manifest differs for {key}")

    try:
        image_document = json.loads((root / "images-manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicationError("images manifest cannot be read after verification") from exc
    if not isinstance(image_document, dict):
        raise PublicationError("images manifest is not an object")
    images = image_document.get("images")
    if component == "worker":
        if not isinstance(images, list) or len(images) != 1:
            raise PublicationError("Worker candidate must contain exactly one runtime image")
        image = images[0]
        if (
            not isinstance(image, dict)
            or image.get("role") != "runtime"
            or image.get("image_id") != runtime_image_id
        ):
            raise PublicationError("Worker runtime image ID differs from the built candidate")
        activation = root / "infra/worker/runtime/runtime-activation.json"
        try:
            activation_metadata = activation.lstat()
        except OSError as exc:
            raise PublicationError("Worker runtime activation is missing") from exc
        if not stat.S_ISREG(activation_metadata.st_mode) or activation_metadata.st_mode & 0o222:
            raise PublicationError("Worker runtime activation must be a read-only regular file")


def _copy_descriptor_to_at(
    source: int,
    source_metadata: os.stat_result,
    directory: int,
    name: str,
) -> tuple[int, int]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | getattr(os, "O_CLOEXEC", 0)
    destination = os.open(name, flags, 0o644, dir_fd=directory)
    metadata = os.fstat(destination)
    identity = (metadata.st_dev, metadata.st_ino)
    try:
        os.lseek(source, 0, os.SEEK_SET)
        copied = 0
        while chunk := os.read(source, 1024 * 1024):
            copied += len(chunk)
            view = memoryview(chunk)
            while view:
                consumed = os.write(destination, view)
                view = view[consumed:]
        if copied != source_metadata.st_size:
            raise PublicationError("release source changed while preparing publication")
        os.fchmod(destination, 0o644)
        os.fsync(destination)
        return identity
    except BaseException:
        os.close(destination)
        destination = -1
        _unlink_if_identity(directory, name, identity)
        raise
    finally:
        if destination >= 0:
            os.close(destination)


def _exists_at(directory: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PublicationError("final output path cannot be inspected safely") from exc
    return True


def _unlink_if_identity(directory: int, name: str, identity: tuple[int, int]) -> None:
    try:
        metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (metadata.st_dev, metadata.st_ino) != identity:
        raise PublicationError("refusing to remove an output replaced by another process")
    os.unlink(name, dir_fd=directory)


def _publish_pair(
    archive_descriptor: int,
    archive_metadata: os.stat_result,
    checksum_descriptor: int,
    checksum_metadata: os.stat_result,
    *,
    output_dir: Path,
    archive_name: str,
) -> None:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise PublicationError("O_NOFOLLOW support is required")
    try:
        output = os.open(
            output_dir,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow,
        )
    except OSError as exc:
        raise PublicationError("output directory must be a real directory") from exc
    checksum_name = f"{archive_name}.sha256"
    token = secrets.token_hex(12)
    archive_temporary = f".{archive_name}.tmp.{token}"
    checksum_temporary = f".{checksum_name}.tmp.{token}"
    archive_identity: tuple[int, int] | None = None
    checksum_identity: tuple[int, int] | None = None
    published_checksum: tuple[int, int] | None = None
    published_archive: tuple[int, int] | None = None
    try:
        if _exists_at(output, archive_name) or _exists_at(output, checksum_name):
            raise PublicationError("final release archive or checksum already exists")
        archive_identity = _copy_descriptor_to_at(
            archive_descriptor,
            archive_metadata,
            output,
            archive_temporary,
        )
        checksum_identity = _copy_descriptor_to_at(
            checksum_descriptor,
            checksum_metadata,
            output,
            checksum_temporary,
        )
        try:
            os.link(
                checksum_temporary,
                checksum_name,
                src_dir_fd=output,
                dst_dir_fd=output,
                follow_symlinks=False,
            )
            published_checksum = checksum_identity
            os.fsync(output)
            os.link(
                archive_temporary,
                archive_name,
                src_dir_fd=output,
                dst_dir_fd=output,
                follow_symlinks=False,
            )
            published_archive = archive_identity
            os.fsync(output)
        except OSError as exc:
            if published_archive is not None:
                _unlink_if_identity(output, archive_name, published_archive)
            if published_checksum is not None:
                _unlink_if_identity(output, checksum_name, published_checksum)
            os.fsync(output)
            raise PublicationError("final output appeared during no-clobber publication") from exc
    finally:
        try:
            if archive_identity is not None:
                _unlink_if_identity(output, archive_temporary, archive_identity)
            if checksum_identity is not None:
                _unlink_if_identity(output, checksum_temporary, checksum_identity)
        finally:
            os.close(output)


def _snapshot_private_pair(
    archive: Path,
    checksum: Path,
    destination: Path,
) -> tuple[Path, Path]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise PublicationError("O_NOFOLLOW support is required")
    try:
        directory = os.open(
            destination,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow,
        )
    except OSError as exc:
        raise PublicationError("private snapshot directory is unsafe") from exc
    try:
        with (
            _open_regular(
                archive,
                maximum=MAX_ARCHIVE_BYTES,
                label="private release archive",
            ) as (archive_descriptor, archive_metadata),
            _open_regular(
                checksum,
                maximum=MAX_CHECKSUM_BYTES,
                label="private release checksum",
            ) as (checksum_descriptor, checksum_metadata),
        ):
            _copy_descriptor_to_at(
                archive_descriptor,
                archive_metadata,
                directory,
                archive.name,
            )
            _copy_descriptor_to_at(
                checksum_descriptor,
                checksum_metadata,
                directory,
                checksum.name,
            )
            os.fsync(directory)
    finally:
        os.close(directory)
    return destination / archive.name, destination / checksum.name


def verify_and_publish(arguments: argparse.Namespace) -> None:
    archive = arguments.archive
    checksum = arguments.checksum
    output_dir = arguments.output_dir
    component = arguments.component
    version = arguments.version
    source_commit = arguments.source_commit
    runtime_image_id = arguments.runtime_image_id

    match = ARCHIVE_NAME_RE.fullmatch(archive.name)
    if match is None or match.group("component") != component or match.group("version") != version:
        raise PublicationError("private archive name differs from component and version")
    if checksum.name != f"{archive.name}.sha256":
        raise PublicationError("private checksum sidecar has an unexpected name")
    if not VERSION_RE.fullmatch(version) or not COMMIT_RE.fullmatch(source_commit):
        raise PublicationError("release version or source commit is invalid")
    if component == "worker" and not DIGEST_RE.fullmatch(runtime_image_id):
        raise PublicationError("Worker runtime image ID is invalid")

    with _open_regular(
        arguments.verifier,
        maximum=MAX_VERIFIER_BYTES,
        label="trusted release verifier",
    ) as (verifier_descriptor, verifier_metadata):
        verifier_bytes = _read_descriptor(
            verifier_descriptor,
            verifier_metadata,
            maximum=MAX_VERIFIER_BYTES,
            label="trusted release verifier",
        )
    try:
        verifier_source = verifier_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublicationError("trusted release verifier is not UTF-8") from exc

    with tempfile.TemporaryDirectory(prefix="rvc-release-verify.") as temporary:
        temporary_root = Path(temporary)
        temporary_root.chmod(0o700)
        snapshot_root = temporary_root / "private"
        extraction_root = temporary_root / "extracted"
        snapshot_root.mkdir(mode=0o700)
        extraction_root.mkdir(mode=0o700)
        snapshot_archive, snapshot_checksum = _snapshot_private_pair(
            archive,
            checksum,
            snapshot_root,
        )
        with (
            _open_regular(
                snapshot_archive,
                maximum=MAX_ARCHIVE_BYTES,
                label="private release archive snapshot",
            ) as (archive_descriptor, archive_metadata),
            _open_regular(
                snapshot_checksum,
                maximum=MAX_CHECKSUM_BYTES,
                label="private release checksum snapshot",
            ) as (checksum_descriptor, checksum_metadata),
        ):
            archive_hash = _sha256_descriptor(
                archive_descriptor,
                archive_metadata,
                label="private release archive snapshot",
            )
            checksum_content = _read_descriptor(
                checksum_descriptor,
                checksum_metadata,
                maximum=MAX_CHECKSUM_BYTES,
                label="private release checksum snapshot",
            )
            _validate_checksum(
                checksum_content,
                archive_name=archive.name,
                archive_hash=archive_hash,
            )
            bundle_root = _extract_archive(
                archive_descriptor,
                extraction_root,
                archive.name.removesuffix(".tar.gz"),
            )
            _run_verifier(
                verifier_source,
                bundle_root,
                component=component,
                version=version,
                source_commit=source_commit,
            )
            _verify_extracted_identity(
                bundle_root,
                component=component,
                version=version,
                source_commit=source_commit,
                runtime_image_id=runtime_image_id,
            )
            _publish_pair(
                archive_descriptor,
                archive_metadata,
                checksum_descriptor,
                checksum_metadata,
                output_dir=output_dir,
                archive_name=archive.name,
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a private release archive and publish its pair without overwrite"
    )
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--checksum", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--verifier", type=Path, required=True)
    parser.add_argument("--component", choices=("manager", "worker"), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--runtime-image-id", default="none")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        verify_and_publish(arguments)
    except (OSError, PublicationError) as exc:
        print(f"release publication error: {exc}", file=sys.stderr)
        return 1
    print(arguments.output_dir / arguments.archive.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
