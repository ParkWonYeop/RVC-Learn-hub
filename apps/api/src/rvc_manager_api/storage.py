from __future__ import annotations

import base64
import hashlib
import os
import shutil
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Literal

import anyio
import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from rvc_orchestrator_contracts import utc_now

from .config import Settings
from .services.workers import as_utc


class StorageError(RuntimeError):
    """Base error that never includes credentials or a presigned URL."""


class InvalidObjectKey(StorageError):
    pass


class ObjectNotFound(StorageError):
    pass


class ObjectTooLarge(StorageError):
    pass


class ObjectSizeMismatch(StorageError):
    pass


UNBOUND_STORAGE_NAMESPACE_SHA256 = "0" * 64


@dataclass(frozen=True, slots=True)
class UploadTarget:
    url: str
    headers: dict[str, str]


def validate_object_key(object_key: str) -> PurePosixPath:
    if not object_key or "\\" in object_key:
        raise InvalidObjectKey("invalid object key")
    parsed = PurePosixPath(object_key)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise InvalidObjectKey("invalid object key")
    return parsed


class StorageAdapter(ABC):
    backend: Literal["local", "s3"]
    namespace_fingerprint: str

    @abstractmethod
    async def create_upload_target(
        self,
        *,
        session_id: str,
        object_key: str,
        public_api_base_url: str,
        content_type: str,
        content_length: int,
        sha256: str,
        expires_at: datetime,
        local_upload_token: str | None,
        local_upload_path: str | None = None,
    ) -> UploadTarget: ...

    @abstractmethod
    def stream_object(
        self,
        object_key: str,
        *,
        chunk_size: int,
        max_bytes: int,
    ) -> AsyncIterator[bytes]: ...

    @abstractmethod
    async def store_verified_file(
        self,
        object_key: str,
        source: Path,
        *,
        content_type: str,
        sha256: str,
    ) -> None: ...

    @abstractmethod
    async def delete_object(self, object_key: str) -> None: ...

    @abstractmethod
    def storage_uri(self, object_key: str) -> str: ...

    @abstractmethod
    async def create_download_url(
        self,
        object_key: str,
        *,
        content_disposition: str,
        expires_in_seconds: int,
    ) -> str | None: ...

    async def write_upload_stream(
        self,
        object_key: str,
        chunks: AsyncIterable[bytes],
        *,
        expected_size: int,
    ) -> None:
        raise StorageError("direct upload is unavailable for this storage backend")

    async def close(self) -> None:
        return None


def storage_namespace_matches(
    *,
    backend: str,
    namespace_sha256: str,
    storage: StorageAdapter,
) -> bool:
    """Match a persisted upload session to the exact configured object namespace."""

    return backend == storage.backend and namespace_sha256 == storage.namespace_fingerprint


class LocalStorageAdapter(StorageAdapter):
    backend: Literal["local"] = "local"

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.namespace_fingerprint = hashlib.sha256(f"local\x1f{self.root}".encode()).hexdigest()

    def _path(self, object_key: str) -> Path:
        parsed = validate_object_key(object_key)
        target = self.root.joinpath(*parsed.parts).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise InvalidObjectKey("object key escapes storage root") from exc
        return target

    async def create_upload_target(
        self,
        *,
        session_id: str,
        object_key: str,
        public_api_base_url: str,
        content_type: str,
        content_length: int,
        sha256: str,
        expires_at: datetime,
        local_upload_token: str | None,
        local_upload_path: str | None = None,
    ) -> UploadTarget:
        del object_key, sha256, expires_at
        if local_upload_token is None:
            raise StorageError("local upload token is required")
        return UploadTarget(
            url=(
                f"{public_api_base_url.rstrip('/')}"
                f"{local_upload_path or f'/api/v1/storage/uploads/{session_id}'}"
            ),
            headers={
                "Content-Type": content_type,
                "Content-Length": str(content_length),
                "X-RVC-Upload-Token": local_upload_token,
            },
        )

    async def write_upload_stream(
        self,
        object_key: str,
        chunks: AsyncIterable[bytes],
        *,
        expected_size: int,
    ) -> None:
        target = self._path(object_key)
        try:
            await anyio.to_thread.run_sync(target.parent.mkdir, 0o750, True, True)
            partial_path = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
            handle: BinaryIO = await anyio.to_thread.run_sync(partial_path.open, "xb")
            await anyio.to_thread.run_sync(partial_path.chmod, 0o600)
        except OSError as exc:
            raise StorageError("local object upload could not be opened") from exc
        total = 0
        try:
            async for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if total > expected_size:
                    raise ObjectTooLarge("uploaded object exceeds declared size")
                await anyio.to_thread.run_sync(handle.write, chunk)
            await anyio.to_thread.run_sync(handle.flush)
            await anyio.to_thread.run_sync(os.fsync, handle.fileno())
        except ObjectTooLarge:
            await anyio.to_thread.run_sync(handle.close)
            await anyio.to_thread.run_sync(partial_path.unlink, True)
            raise
        except BaseException as exc:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(handle.close)
                await anyio.to_thread.run_sync(partial_path.unlink, True)
            if isinstance(exc, Exception):
                raise StorageError("local object upload failed") from exc
            raise
        await anyio.to_thread.run_sync(handle.close)
        if total != expected_size:
            await anyio.to_thread.run_sync(partial_path.unlink, True)
            raise ObjectSizeMismatch("uploaded object size does not match declaration")
        try:
            await anyio.to_thread.run_sync(os.replace, partial_path, target)
        except OSError as exc:
            await anyio.to_thread.run_sync(partial_path.unlink, True)
            raise StorageError("local object upload could not be published") from exc

    async def stream_object(
        self,
        object_key: str,
        *,
        chunk_size: int,
        max_bytes: int,
    ) -> AsyncIterator[bytes]:
        path = self._path(object_key)
        try:
            handle: BinaryIO = await anyio.to_thread.run_sync(path.open, "rb")
        except FileNotFoundError as exc:
            raise ObjectNotFound("object does not exist") from exc
        except OSError as exc:
            raise StorageError("local object could not be opened") from exc
        total = 0
        try:
            while True:
                chunk = await anyio.to_thread.run_sync(handle.read, chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ObjectTooLarge("object exceeds bounded read limit")
                yield chunk
        except ObjectTooLarge:
            raise
        except OSError as exc:
            raise StorageError("local object read failed") from exc
        finally:
            try:
                await anyio.to_thread.run_sync(handle.close)
            except OSError:
                pass

    async def store_verified_file(
        self,
        object_key: str,
        source: Path,
        *,
        content_type: str,
        sha256: str,
    ) -> None:
        del content_type, sha256
        target = self._path(object_key)
        await anyio.to_thread.run_sync(target.parent.mkdir, 0o750, True, True)
        partial_path = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")

        def copy_and_publish() -> None:
            published = False
            try:
                with source.open("rb") as reader, partial_path.open("xb") as writer:
                    os.chmod(partial_path, 0o600)
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
                os.chmod(partial_path, 0o440)
                # link(2) is an atomic no-replace publish in the same directory.
                # Canonical keys are write-once and can never silently replace an
                # object another finalization already exposed.
                os.link(partial_path, target, follow_symlinks=False)
                published = True
                partial_path.unlink()
                directory_fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except Exception:
                partial_path.unlink(missing_ok=True)
                if published:
                    target.unlink(missing_ok=True)
                raise

        try:
            await anyio.to_thread.run_sync(copy_and_publish)
        except Exception as exc:
            raise StorageError("local verified object publish failed") from exc

    async def delete_object(self, object_key: str) -> None:
        try:
            await anyio.to_thread.run_sync(self._path(object_key).unlink, True)
        except OSError as exc:
            raise StorageError("local object cleanup failed") from exc

    def storage_uri(self, object_key: str) -> str:
        parsed = validate_object_key(object_key)
        return f"local:///{parsed.as_posix()}"

    async def create_download_url(
        self,
        object_key: str,
        *,
        content_disposition: str,
        expires_in_seconds: int,
    ) -> None:
        del object_key, content_disposition, expires_in_seconds
        return None


class S3StorageAdapter(StorageAdapter):
    backend: Literal["s3"] = "s3"

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        access_key = settings.s3_access_key_id
        secret_key = settings.s3_secret_access_key
        if access_key is None or secret_key is None:
            raise ValueError("S3 credentials are required")
        self.bucket = settings.s3_bucket
        self.bind_checksum = settings.s3_presign_bind_checksum
        self.namespace_fingerprint = hashlib.sha256(
            "\x1f".join(
                (
                    "s3",
                    settings.s3_endpoint_url or "aws-default",
                    settings.s3_bucket,
                    settings.s3_region,
                    settings.s3_addressing_style,
                )
            ).encode("utf-8")
        ).hexdigest()
        client_config = Config(
            signature_version="s3v4",
            s3={"addressing_style": settings.s3_addressing_style},
        )

        def make_client(endpoint_url: str | None) -> Any:
            return boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=access_key.get_secret_value(),
                aws_secret_access_key=secret_key.get_secret_value(),
                region_name=settings.s3_region,
                verify=settings.s3_verify_tls,
                config=client_config,
            )

        self.client: Any = client or make_client(settings.s3_endpoint_url)
        if client is not None:
            self.presign_client: Any = client
        elif (
            settings.s3_presign_endpoint_url
            and settings.s3_presign_endpoint_url != settings.s3_endpoint_url
        ):
            self.presign_client = make_client(settings.s3_presign_endpoint_url)
        else:
            self.presign_client = self.client

    async def create_upload_target(
        self,
        *,
        session_id: str,
        object_key: str,
        public_api_base_url: str,
        content_type: str,
        content_length: int,
        sha256: str,
        expires_at: datetime,
        local_upload_token: str | None,
        local_upload_path: str | None = None,
    ) -> UploadTarget:
        del session_id, public_api_base_url, local_upload_token, local_upload_path
        validate_object_key(object_key)
        remaining = max(1, int((as_utc(expires_at) - utc_now()).total_seconds()))
        parameters: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": object_key,
            "ContentType": content_type,
            "ContentLength": content_length,
            "Metadata": {"sha256": sha256},
        }
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(content_length),
            "x-amz-meta-sha256": sha256,
        }
        if self.bind_checksum:
            checksum = base64.b64encode(bytes.fromhex(sha256)).decode("ascii")
            parameters["ChecksumSHA256"] = checksum
            headers["x-amz-checksum-sha256"] = checksum
        try:
            url = await anyio.to_thread.run_sync(
                partial(
                    self.presign_client.generate_presigned_url,
                    "put_object",
                    Params=parameters,
                    ExpiresIn=remaining,
                    HttpMethod="PUT",
                )
            )
        except Exception as exc:
            raise StorageError("object upload signing failed") from exc
        return UploadTarget(url=str(url), headers=headers)

    async def stream_object(
        self,
        object_key: str,
        *,
        chunk_size: int,
        max_bytes: int,
    ) -> AsyncIterator[bytes]:
        validate_object_key(object_key)
        try:
            response = await anyio.to_thread.run_sync(
                partial(self.client.get_object, Bucket=self.bucket, Key=object_key)
            )
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            if error_code in {"404", "NoSuchKey", "NotFound"}:
                raise ObjectNotFound("object does not exist") from exc
            raise StorageError("object storage read failed") from exc
        except Exception as exc:
            raise StorageError("object storage read failed") from exc
        body: Any = response["Body"]
        total = 0
        try:
            while True:
                chunk = await anyio.to_thread.run_sync(body.read, chunk_size)
                if not chunk:
                    break
                value = bytes(chunk)
                total += len(value)
                if total > max_bytes:
                    raise ObjectTooLarge("object exceeds bounded read limit")
                yield value
        except ObjectTooLarge:
            raise
        except Exception as exc:
            raise StorageError("object storage stream failed") from exc
        finally:
            try:
                await anyio.to_thread.run_sync(body.close)
            except Exception:
                pass

    async def store_verified_file(
        self,
        object_key: str,
        source: Path,
        *,
        content_type: str,
        sha256: str,
    ) -> None:
        validate_object_key(object_key)

        def conditional_put() -> None:
            with source.open("rb") as body:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=object_key,
                    Body=body,
                    ContentLength=source.stat().st_size,
                    ContentType=content_type,
                    Metadata={"sha256": sha256, "verified": "true"},
                    IfNoneMatch="*",
                )

        try:
            await anyio.to_thread.run_sync(conditional_put)
        except Exception as exc:
            raise StorageError("object storage publish failed") from exc

    async def delete_object(self, object_key: str) -> None:
        validate_object_key(object_key)
        try:
            await anyio.to_thread.run_sync(
                partial(self.client.delete_object, Bucket=self.bucket, Key=object_key)
            )
        except Exception as exc:
            raise StorageError("object storage cleanup failed") from exc

    def storage_uri(self, object_key: str) -> str:
        parsed = validate_object_key(object_key)
        return f"s3://{self.bucket}/{parsed.as_posix()}"

    async def create_download_url(
        self,
        object_key: str,
        *,
        content_disposition: str,
        expires_in_seconds: int,
    ) -> str:
        validate_object_key(object_key)
        try:
            url = await anyio.to_thread.run_sync(
                partial(
                    self.presign_client.generate_presigned_url,
                    "get_object",
                    Params={
                        "Bucket": self.bucket,
                        "Key": object_key,
                        "ResponseContentDisposition": content_disposition,
                    },
                    ExpiresIn=expires_in_seconds,
                    HttpMethod="GET",
                )
            )
        except Exception as exc:
            raise StorageError("object storage download signing failed") from exc
        return str(url)

    async def close(self) -> None:
        clients = [self.client]
        if self.presign_client is not self.client:
            clients.append(self.presign_client)
        for client in clients:
            close = getattr(client, "close", None)
            if close is not None:
                await anyio.to_thread.run_sync(close)


def create_storage_adapter(settings: Settings) -> StorageAdapter:
    if settings.resolved_storage_backend == "local":
        return LocalStorageAdapter(settings.local_storage_root)
    return S3StorageAdapter(settings)
