"""Fail-closed TLS trust configuration for the Worker network boundary."""

from __future__ import annotations

import argparse
import os
import secrets
import ssl
import stat
import sys
from pathlib import Path

DEFAULT_CUSTOM_CA_BUNDLE_PATH = Path("/etc/rvc-worker/ca/custom-ca.pem")
MAX_CUSTOM_CA_BUNDLE_BYTES = 1024 * 1024
_ALLOWED_CA_MODES = frozenset({0o444, 0o644})


class CustomCABundleError(ValueError):
    """Raised when a custom CA bundle cannot be trusted as immutable input."""


def read_custom_ca_bundle(
    path: Path,
    *,
    required_uid: int = 0,
    expected_path: Path | None = None,
) -> str:
    """Read and validate one root-owned, bounded PEM CA bundle without symlinks."""

    candidate = Path(path)
    if expected_path is not None and candidate != expected_path:
        raise CustomCABundleError(f"custom CA bundle must use the fixed path {expected_path}")
    if not candidate.is_absolute() or "\x00" in str(candidate):
        raise CustomCABundleError("custom CA bundle path must be absolute and NUL-free")

    try:
        before = candidate.lstat()
    except OSError as exc:
        raise CustomCABundleError("custom CA bundle is missing or unreadable") from exc
    _validate_ca_stat(before, required_uid=required_uid)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise CustomCABundleError(
            "custom CA bundle cannot be opened without following links"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        _validate_ca_stat(opened, required_uid=required_uid)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise CustomCABundleError("custom CA bundle changed while it was opened")
        payload = _read_bounded(descriptor, maximum=MAX_CUSTOM_CA_BUNDLE_BYTES)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise CustomCABundleError("custom CA bundle changed while it was read")
    finally:
        os.close(descriptor)

    if len(payload) != opened.st_size:
        raise CustomCABundleError("custom CA bundle size changed while it was read")
    if b"\x00" in payload:
        raise CustomCABundleError("custom CA bundle contains a NUL byte")
    if b"PRIVATE KEY" in payload:
        raise CustomCABundleError("custom CA bundle must not contain private key material")
    try:
        pem = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CustomCABundleError("custom CA bundle must be ASCII PEM") from exc
    begin_count = pem.count("-----BEGIN CERTIFICATE-----")
    end_count = pem.count("-----END CERTIFICATE-----")
    if begin_count < 1 or begin_count != end_count:
        raise CustomCABundleError("custom CA bundle must contain complete PEM certificates")

    parser = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    parser.check_hostname = True
    parser.verify_mode = ssl.CERT_REQUIRED
    try:
        parser.load_verify_locations(cadata=pem)
    except ssl.SSLError as exc:
        raise CustomCABundleError("custom CA bundle contains invalid certificate data") from exc
    return pem


def create_worker_ssl_context(ca_bundle_path: Path | None) -> ssl.SSLContext:
    """Create the single verified TLS context shared by Manager and object clients."""

    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    if ca_bundle_path is not None:
        pem = read_custom_ca_bundle(
            ca_bundle_path,
            required_uid=0,
            expected_path=DEFAULT_CUSTOM_CA_BUNDLE_PATH,
        )
        try:
            context.load_verify_locations(cadata=pem)
        except ssl.SSLError as exc:  # pragma: no cover - already parsed above
            raise CustomCABundleError("custom CA bundle could not be loaded") from exc
    return context


def install_custom_ca_bundle(
    source: Path,
    destination: Path,
    *,
    required_source_uid: int,
    output_uid: int,
    output_gid: int,
) -> None:
    """Validate and atomically publish a non-secret custom CA configuration file."""

    pem = read_custom_ca_bundle(source, required_uid=required_source_uid)
    payload = pem.encode("ascii")
    parent = destination.parent
    if destination.name in {"", ".", ".."}:
        raise CustomCABundleError("custom CA destination filename is unsafe")
    try:
        parent_before = parent.lstat()
    except OSError as exc:
        raise CustomCABundleError("custom CA destination directory is missing") from exc
    if not stat.S_ISDIR(parent_before.st_mode) or stat.S_ISLNK(parent_before.st_mode):
        raise CustomCABundleError("custom CA destination directory is unsafe")
    if parent_before.st_uid != output_uid or stat.S_IMODE(parent_before.st_mode) & 0o022:
        raise CustomCABundleError(
            "custom CA destination directory must be owner-controlled and not writable by others"
        )

    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory = os.open(parent, directory_flags)
    except OSError as exc:
        raise CustomCABundleError(
            "custom CA destination directory cannot be opened safely"
        ) from exc

    temporary_name = f".{destination.name}.installing.{secrets.token_hex(8)}"
    temporary_created = False
    try:
        parent_opened = os.fstat(directory)
        if (parent_opened.st_dev, parent_opened.st_ino) != (
            parent_before.st_dev,
            parent_before.st_ino,
        ):
            raise CustomCABundleError("custom CA destination directory changed while opening")
        try:
            existing = os.stat(destination.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise CustomCABundleError("custom CA destination cannot be inspected safely") from exc
        if existing is not None:
            _validate_ca_stat(existing, required_uid=output_uid)

        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        create_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            output = os.open(temporary_name, create_flags, 0o444, dir_fd=directory)
        except OSError as exc:
            raise CustomCABundleError("custom CA temporary file cannot be created safely") from exc
        temporary_created = True
        try:
            _write_all(output, payload)
            output_stat = os.fstat(output)
            if (output_stat.st_uid, output_stat.st_gid) != (output_uid, output_gid):
                os.fchown(output, output_uid, output_gid)
            os.fchmod(output, 0o444)
            os.fsync(output)
        finally:
            os.close(output)
        os.replace(
            temporary_name,
            destination.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        temporary_created = False
        os.fsync(directory)
    finally:
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=directory)
            except OSError:
                pass
        os.close(directory)


def _validate_ca_stat(value: os.stat_result, *, required_uid: int) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise CustomCABundleError("custom CA bundle must be a regular non-symlink file")
    if value.st_uid != required_uid:
        raise CustomCABundleError(f"custom CA bundle must be owned by UID {required_uid}")
    mode = stat.S_IMODE(value.st_mode)
    if mode not in _ALLOWED_CA_MODES:
        raise CustomCABundleError("custom CA bundle mode must be 0444 or 0644")
    if value.st_size <= 0 or value.st_size > MAX_CUSTOM_CA_BUNDLE_BYTES:
        raise CustomCABundleError(
            f"custom CA bundle must be between 1 and {MAX_CUSTOM_CA_BUNDLE_BYTES} bytes"
        )


def _read_bounded(descriptor: int, *, maximum: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > maximum:
        raise CustomCABundleError(f"custom CA bundle exceeds {maximum} bytes")
    return payload


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise CustomCABundleError("custom CA bundle could not be written completely")
        view = view[written:]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or atomically install a Worker CA bundle"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--path", type=Path, required=True)
    validate.add_argument("--required-uid", type=int, required=True)
    install = subparsers.add_parser("install")
    install.add_argument("--source", type=Path, required=True)
    install.add_argument("--destination", type=Path, required=True)
    install.add_argument("--required-source-uid", type=int, required=True)
    install.add_argument("--output-uid", type=int, required=True)
    install.add_argument("--output-gid", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            read_custom_ca_bundle(args.path, required_uid=args.required_uid)
        else:
            install_custom_ca_bundle(
                args.source,
                args.destination,
                required_source_uid=args.required_source_uid,
                output_uid=args.output_uid,
                output_gid=args.output_gid,
            )
    except (CustomCABundleError, OSError) as exc:
        print(f"custom CA bundle validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - installer entry point
    raise SystemExit(main())
