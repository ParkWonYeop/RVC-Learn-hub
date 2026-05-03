from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import hmac
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePath
from typing import Any, BinaryIO, TypeVar
from urllib.parse import quote

import anyio

from ..config import Settings
from ..models import Artifact
from ..schemas import ArtifactRead, ArtifactUploadInitRequest
from ..storage import StorageAdapter

_MIN_UPLOAD_TTL_SECONDS = 300
_ASSUMED_MIN_UPLOAD_BYTES_PER_SECOND = 2 * 1024**2

ResultT = TypeVar("ResultT")


class ArtifactVerificationMismatch(ValueError):
    def __init__(self, failure_code: str) -> None:
        super().__init__("uploaded artifact does not match declared size or checksum")
        self.failure_code = failure_code


class ArtifactSpoolError(RuntimeError):
    def __init__(self, failure_code: str) -> None:
        super().__init__("artifact verification spool I/O failed")
        self.failure_code = failure_code


def _spool_failure_code(exc: OSError) -> str:
    if exc.errno in {errno.EDQUOT, errno.EFBIG, errno.ENOSPC}:
        return "verification_spool_full"
    return "verification_spool_io_error"


async def remove_spool_file(path: Path) -> None:
    try:
        await _critical_to_thread(path.unlink, True)
    except OSError as exc:
        raise ArtifactSpoolError("verification_spool_cleanup_failed") from exc


async def _cleanup_partial_spool(
    *,
    descriptor: int | None,
    handle: BinaryIO | None,
    path: Path | None,
) -> OSError | None:
    first_error: OSError | None = None
    if handle is not None and not handle.closed:
        try:
            await anyio.to_thread.run_sync(handle.close)
        except OSError as exc:
            first_error = exc
    if descriptor is not None:
        try:
            await anyio.to_thread.run_sync(os.close, descriptor)
        except OSError as exc:
            first_error = first_error or exc
    if path is not None:
        try:
            await anyio.to_thread.run_sync(path.unlink, True)
        except OSError as exc:
            first_error = first_error or exc
    return first_error


async def _join_thread_task_after_cancellation(
    task: asyncio.Task[ResultT],
) -> ResultT:
    """Join an already-running thread task despite repeated outer cancellation."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    return task.result()


async def _critical_to_thread(
    function: Callable[..., ResultT],
    *args: Any,
    cancel_cleanup: Callable[[ResultT], None] | None = None,
) -> ResultT:
    """Join filesystem work before propagating cancellation to its caller."""

    operation = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError as cancelled:
        try:
            result = await _join_thread_task_after_cancellation(operation)
        except BaseException:
            raise cancelled from None
        if cancel_cleanup is not None:
            cleanup = asyncio.create_task(asyncio.to_thread(cancel_cleanup, result))
            try:
                await _join_thread_task_after_cancellation(cleanup)
            except BaseException:
                pass
        raise cancelled


def _create_verification_spool(spool_dir: Path | None) -> tuple[BinaryIO, Path]:
    descriptor: int | None = None
    path: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix="rvc-artifact-verify-",
            dir=spool_dir,
        )
        path = Path(raw_path)
        handle = os.fdopen(descriptor, "wb", closefd=True)
        descriptor = None
        return handle, path
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _cleanup_created_verification_spool(created: tuple[BinaryIO, Path]) -> None:
    handle, path = created
    first_error: OSError | None = None
    try:
        handle.close()
    except OSError as exc:
        first_error = exc
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        first_error = first_error or exc
    if first_error is not None:
        raise first_error


async def _critical_cleanup_partial_spool(
    *,
    descriptor: int | None,
    handle: BinaryIO | None,
    path: Path | None,
) -> OSError | None:
    cleanup = asyncio.create_task(
        _cleanup_partial_spool(
            descriptor=descriptor,
            handle=handle,
            path=path,
        )
    )
    try:
        return await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        return await _join_thread_task_after_cancellation(cleanup)


def effective_artifact_upload_ttl_seconds(size_bytes: int, settings: Settings) -> int:
    """Return a bounded upload window sized for a conservative single PUT.

    The configured value is the operator-controlled maximum. The fixed five-minute
    allowance covers signing, connection setup, and transient latency; the variable
    allowance assumes a sustained 2 MiB/s upload rate.
    """

    transfer_seconds = (
        size_bytes + _ASSUMED_MIN_UPLOAD_BYTES_PER_SECOND - 1
    ) // _ASSUMED_MIN_UPLOAD_BYTES_PER_SECOND
    estimated_seconds = _MIN_UPLOAD_TTL_SECONDS + transfer_seconds
    return min(settings.artifact_upload_ttl_seconds, estimated_seconds)


def upload_request_fingerprint(payload: ArtifactUploadInitRequest) -> str:
    canonical = json.dumps(
        payload.model_dump(mode="json", exclude={"lease_id", "attempt_id", "idempotency_key"}),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def upload_dedupe_key(attempt_id: str, artifact_type: str, sha256: str) -> str:
    value = f"{attempt_id}\x1f{artifact_type}\x1f{sha256}".encode()
    return hashlib.sha256(value).hexdigest()


def staging_object_key(attempt_id: str, upload_session_id: str) -> str:
    return f"artifacts/staging/{attempt_id}/{upload_session_id}"


def canonical_object_key(
    job_id: str,
    attempt_id: str,
    artifact_type: str,
    upload_session_id: str,
) -> str:
    return f"artifacts/verified/{job_id}/{attempt_id}/{artifact_type}/{upload_session_id}"


def derive_local_upload_token(
    upload_session_id: str,
    expires_at_timestamp: int,
    settings: Settings,
) -> str:
    message = f"artifact-upload\x1f{upload_session_id}\x1f{expires_at_timestamp}".encode()
    digest = hmac.new(
        settings.worker_token_pepper.get_secret_value().encode("utf-8"),
        message,
        hashlib.sha256,
    ).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"rvcu_{encoded}"


def upload_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_upload_token(token: str, expected_hash: str | None) -> bool:
    if expected_hash is None:
        return False
    return hmac.compare_digest(upload_token_hash(token), expected_hash)


def artifact_to_read(artifact: Artifact) -> ArtifactRead:
    return ArtifactRead.model_validate(artifact)


def safe_download_filename(filename: str, artifact_id: str) -> str:
    if (
        not filename
        or filename in {".", ".."}
        or PurePath(filename).name != filename
        or "\\" in filename
        or any(ord(character) < 32 for character in filename)
    ):
        return f"artifact-{artifact_id}.bin"
    return filename


def attachment_content_disposition(filename: str) -> str:
    ascii_name = "".join(
        character if character.isascii() and (character.isalnum() or character in "._-") else "_"
        for character in filename
    ).strip(".")
    if not ascii_name:
        ascii_name = "artifact.bin"
    ascii_name = ascii_name[:150]
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


async def verify_object_to_spool(
    storage: StorageAdapter,
    object_key: str,
    *,
    expected_size: int,
    expected_sha256: str,
    settings: Settings,
) -> Path:
    descriptor: int | None = None
    path: Path | None = None
    handle: BinaryIO | None = None
    digest = hashlib.sha256()
    total = 0
    try:
        spool_dir = settings.artifact_verification_spool_dir
        if spool_dir is not None:
            spool_dir = spool_dir.expanduser().resolve()
            await _critical_to_thread(spool_dir.mkdir, 0o700, True, True)
        created: tuple[BinaryIO, Path] = await _critical_to_thread(
            _create_verification_spool,
            spool_dir,
            cancel_cleanup=_cleanup_created_verification_spool,
        )
        active_handle, path = created
        handle = active_handle
        async for chunk in storage.stream_object(
            object_key,
            chunk_size=settings.artifact_stream_chunk_bytes,
            max_bytes=expected_size + 1,
        ):
            total += len(chunk)
            digest.update(chunk)
            written = await _critical_to_thread(active_handle.write, chunk)
            if written != len(chunk):
                raise OSError(errno.EIO, "short verification spool write")
        await _critical_to_thread(active_handle.flush)
        await _critical_to_thread(os.fsync, active_handle.fileno())
        await _critical_to_thread(active_handle.close)
        handle = None
        if total != expected_size:
            raise ArtifactVerificationMismatch("size_mismatch")
        if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
            raise ArtifactVerificationMismatch("sha256_mismatch")
        assert path is not None
        return path
    except OSError as exc:
        cleanup_error = await _critical_cleanup_partial_spool(
            descriptor=descriptor,
            handle=handle,
            path=path,
        )
        if cleanup_error is not None:
            raise ArtifactSpoolError("verification_spool_cleanup_failed") from cleanup_error
        raise ArtifactSpoolError(_spool_failure_code(exc)) from exc
    except BaseException as exc:
        cleanup_error = await _critical_cleanup_partial_spool(
            descriptor=descriptor,
            handle=handle,
            path=path,
        )
        if cleanup_error is not None and not isinstance(exc, asyncio.CancelledError):
            raise ArtifactSpoolError("verification_spool_cleanup_failed") from cleanup_error
        raise
