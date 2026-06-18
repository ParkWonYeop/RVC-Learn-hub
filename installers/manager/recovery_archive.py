#!/usr/bin/env python3
"""Snapshot and safely extract a Manager recovery archive."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath

_BUFFER_SIZE = 1024 * 1024


class RecoveryArchiveError(RuntimeError):
    """Raised when a recovery archive cannot be handled safely."""


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _open_no_follow(path: Path, flags: int, mode: int = 0o600) -> int:
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)


def _open_source_no_follow(path: Path) -> int:
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise RecoveryArchiveError("snapshot source must be an absolute path without traversal")
    directory_fd = os.open(
        "/",
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise RecoveryArchiveError(f"snapshot source path is unsafe: {path}") from exc
    finally:
        os.close(directory_fd)


def snapshot_regular_file(source: Path, destination: Path, max_bytes: int) -> int:
    """Copy one pathname through a stable regular-file descriptor."""
    source_fd = -1
    destination_fd = -1
    try:
        source_fd = _open_source_no_follow(source)
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise RecoveryArchiveError(f"input is not a regular file: {source}")
        if before.st_size <= 0 or before.st_size > max_bytes:
            raise RecoveryArchiveError(f"input size is outside the configured limit: {source}")
        parent = destination.parent
        if not parent.is_dir() or parent.is_symlink():
            raise RecoveryArchiveError("snapshot destination parent is unsafe")
        destination_fd = _open_no_follow(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        copied = 0
        while True:
            chunk = os.read(source_fd, _BUFFER_SIZE)
            if not chunk:
                break
            copied += len(chunk)
            if copied > max_bytes:
                raise RecoveryArchiveError("input grew beyond the configured limit while copying")
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise RecoveryArchiveError("short write while snapshotting input")
                view = view[written:]
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        stable_fields = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_fields = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if copied != before.st_size or stable_fields != after_fields:
            raise RecoveryArchiveError("input changed while it was being snapshotted")
        return copied
    except OSError as exc:
        raise RecoveryArchiveError(f"could not snapshot regular file: {source}") from exc
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if source_fd >= 0:
            os.close(source_fd)


def _safe_member(name: str, expected_root: str) -> PurePosixPath:
    if not name or "\\" in name or any(ord(character) < 32 for character in name):
        raise RecoveryArchiveError("archive contains an unsafe member path")
    path = PurePosixPath(name.rstrip("/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RecoveryArchiveError("archive contains path traversal")
    if not path.parts or path.parts[0] != expected_root:
        raise RecoveryArchiveError("archive member escapes its component root")
    return path


def _validated_members(
    archive: Path,
    expected_root: str,
    max_members: int,
    max_unpacked_bytes: int,
) -> tuple[list[tarfile.TarInfo], int, int]:
    members: list[tarfile.TarInfo] = []
    names: set[str] = set()
    regular_files = 0
    unpacked_bytes = 0
    try:
        with tarfile.open(archive, mode="r:gz") as bundle:
            for member in bundle:
                path = _safe_member(member.name, expected_root)
                canonical = path.as_posix()
                if canonical in names:
                    raise RecoveryArchiveError("archive contains a duplicate member path")
                names.add(canonical)
                if not (member.isdir() or member.isreg()):
                    raise RecoveryArchiveError("archive contains a link or special member")
                if (
                    member.size < 0
                    or member.sparse is not None
                    or any(key.startswith("GNU.sparse") for key in member.pax_headers)
                ):
                    raise RecoveryArchiveError("archive contains a sparse or invalid member")
                if member.isreg():
                    regular_files += 1
                    unpacked_bytes += member.size
                members.append(member)
                if len(members) > max_members:
                    raise RecoveryArchiveError("archive member count exceeds the configured limit")
                if unpacked_bytes > max_unpacked_bytes:
                    raise RecoveryArchiveError("archive size exceeds the configured unpacked limit")
    except (OSError, tarfile.TarError) as exc:
        raise RecoveryArchiveError("archive is not a readable gzip tar file") from exc
    if not members or expected_root not in names:
        raise RecoveryArchiveError("archive component root is missing")
    return members, regular_files, unpacked_bytes


def _disk_preflight(
    destination: Path,
    unpacked_bytes: int,
    regular_files: int,
    reserve_bytes: int,
    reserve_inodes: int,
) -> None:
    usage = shutil.disk_usage(destination)
    if usage.free < unpacked_bytes + reserve_bytes:
        raise RecoveryArchiveError("insufficient free bytes for safe archive extraction")
    stats = os.statvfs(destination)
    if stats.f_favail and stats.f_favail < regular_files + reserve_inodes:
        raise RecoveryArchiveError("insufficient free inodes for safe archive extraction")


def _ensure_private_parents(destination: Path, relative: PurePosixPath) -> Path:
    current = destination
    for part in relative.parts[:-1]:
        current /= part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            mode = current.lstat().st_mode
            if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                raise RecoveryArchiveError(
                    "archive parent path is not a private directory"
                ) from None
    return destination.joinpath(*relative.parts)


def extract_archive(
    archive: Path,
    destination: Path,
    expected_root: str,
    max_members: int,
    max_unpacked_bytes: int,
    reserve_bytes: int,
    reserve_inodes: int,
) -> dict[str, int]:
    if not destination.is_dir() or destination.is_symlink():
        raise RecoveryArchiveError("archive extraction destination is unsafe")
    members, regular_files, unpacked_bytes = _validated_members(
        archive, expected_root, max_members, max_unpacked_bytes
    )
    _disk_preflight(destination, unpacked_bytes, regular_files, reserve_bytes, reserve_inodes)
    try:
        with tarfile.open(archive, mode="r:gz") as bundle:
            indexed = {member.name.rstrip("/"): member for member in bundle}
            for reviewed in members:
                relative = _safe_member(reviewed.name, expected_root)
                target = _ensure_private_parents(destination, relative)
                if reviewed.isdir():
                    try:
                        target.mkdir(mode=0o700)
                    except FileExistsError:
                        mode = target.lstat().st_mode
                        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                            raise RecoveryArchiveError(
                                "archive directory target is unsafe"
                            ) from None
                    continue
                current = indexed.get(reviewed.name.rstrip("/"))
                if current is None or current.size != reviewed.size or not current.isreg():
                    raise RecoveryArchiveError("archive changed between validation and extraction")
                stream = bundle.extractfile(current)
                if stream is None:
                    raise RecoveryArchiveError("archive member could not be read")
                output_fd = _open_no_follow(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                written = 0
                try:
                    while True:
                        chunk = stream.read(_BUFFER_SIZE)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > reviewed.size:
                            raise RecoveryArchiveError("archive member exceeded its declared size")
                        view = memoryview(chunk)
                        while view:
                            count = os.write(output_fd, view)
                            if count <= 0:
                                raise RecoveryArchiveError("short write while extracting archive")
                            view = view[count:]
                    if written != reviewed.size:
                        raise RecoveryArchiveError("archive member was shorter than declared")
                    os.fsync(output_fd)
                finally:
                    os.close(output_fd)
                    stream.close()
    except (OSError, tarfile.TarError) as exc:
        raise RecoveryArchiveError("safe archive extraction failed") from exc
    return {
        "members": len(members),
        "regular_files": regular_files,
        "unpacked_bytes": unpacked_bytes,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--source", type=Path, required=True)
    snapshot.add_argument("--destination", type=Path, required=True)
    snapshot.add_argument("--max-bytes", type=_positive, required=True)
    extract = subparsers.add_parser("extract")
    extract.add_argument("--archive", type=Path, required=True)
    extract.add_argument("--destination", type=Path, required=True)
    extract.add_argument("--expected-root", required=True)
    extract.add_argument("--max-members", type=_positive, required=True)
    extract.add_argument("--max-unpacked-bytes", type=_positive, required=True)
    extract.add_argument("--reserve-bytes", type=_positive, required=True)
    extract.add_argument("--reserve-inodes", type=_positive, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "snapshot":
            result = {
                "bytes": snapshot_regular_file(
                    arguments.source, arguments.destination, arguments.max_bytes
                )
            }
        else:
            result = extract_archive(
                arguments.archive,
                arguments.destination,
                arguments.expected_root,
                arguments.max_members,
                arguments.max_unpacked_bytes,
                arguments.reserve_bytes,
                arguments.reserve_inodes,
            )
    except RecoveryArchiveError as exc:
        print(f"recovery archive rejected: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
