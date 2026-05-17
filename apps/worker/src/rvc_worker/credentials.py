"""Atomic 0600 storage for the one-time issued Worker bearer token."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_WORKER_TOKEN = re.compile(r"^rvcw_[A-Za-z0-9_-]{32,128}$")


class CredentialError(RuntimeError):
    """Raised when persistent Worker credentials are unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class WorkerCredential:
    manager_url: str
    worker_id: str
    worker_name: str
    worker_token: str = field(repr=False)
    pending_rotation_id: str | None = None
    pending_worker_token: str | None = field(default=None, repr=False)
    pending_rotation_expires_at: datetime | None = None
    schema_version: str = "2.0"

    def stage_rotation(
        self,
        *,
        rotation_id: str,
        worker_token: str,
        expires_at: datetime,
    ) -> WorkerCredential:
        return replace(
            self,
            pending_rotation_id=rotation_id,
            pending_worker_token=worker_token,
            pending_rotation_expires_at=expires_at,
            schema_version="2.0",
        )

    def activate_pending(self) -> WorkerCredential:
        if self.pending_worker_token is None:
            raise CredentialError("Worker credential has no pending token rotation")
        return replace(
            self,
            worker_token=self.pending_worker_token,
            pending_rotation_id=None,
            pending_worker_token=None,
            pending_rotation_expires_at=None,
            schema_version="2.0",
        )

    def clear_pending(self) -> WorkerCredential:
        return replace(
            self,
            pending_rotation_id=None,
            pending_worker_token=None,
            pending_rotation_expires_at=None,
            schema_version="2.0",
        )


class CredentialStore:
    def __init__(self, path: Path) -> None:
        # Do not resolve the final component: load must reject a credential-file
        # symlink rather than silently following it to an attacker-chosen target.
        self.path = path.expanduser().absolute()

    def load(self, *, manager_url: str, worker_name: str) -> WorkerCredential | None:
        if not self.path.exists():
            return None
        descriptor = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise CredentialError(
                    "Worker credential path must be a regular file, not a symlink"
                )
            if info.st_mode & 0o077:
                raise CredentialError("Worker credential file must have mode 0600")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                document: Any = json.load(stream)
        except CredentialError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise CredentialError("Worker credential file is unreadable or invalid") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(document, dict) or document.get("schema_version") not in {"1.0", "2.0"}:
            raise CredentialError("unsupported Worker credential schema")
        pending_expires_at = _optional_datetime(document.get("pending_rotation_expires_at"))
        credential = WorkerCredential(
            manager_url=str(document.get("manager_url", "")),
            worker_id=str(document.get("worker_id", "")),
            worker_name=str(document.get("worker_name", "")),
            worker_token=str(document.get("worker_token", "")),
            pending_rotation_id=_optional_text(document.get("pending_rotation_id")),
            pending_worker_token=_optional_text(document.get("pending_worker_token")),
            pending_rotation_expires_at=pending_expires_at,
        )
        if not credential.worker_id or not credential.worker_token:
            raise CredentialError("Worker credential is missing its id or token")
        _validate_pending_rotation(credential)
        if credential.manager_url != manager_url or credential.worker_name != worker_name:
            raise CredentialError(
                "stored Worker credential belongs to a different Manager or Worker name"
            )
        return credential

    def save(self, credential: WorkerCredential) -> None:
        _validate_pending_rotation(credential)
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            parent_info = parent.lstat()
        except OSError as exc:
            raise CredentialError("cannot inspect Worker credential directory") from exc
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise CredentialError("Worker credential directory cannot be a symlink")
        document = {
            "schema_version": "2.0",
            "manager_url": credential.manager_url,
            "worker_id": credential.worker_id,
            "worker_name": credential.worker_name,
            "worker_token": credential.worker_token,
            "pending_rotation_id": credential.pending_rotation_id,
            "pending_worker_token": credential.pending_worker_token,
            "pending_rotation_expires_at": (
                credential.pending_rotation_expires_at.astimezone(UTC).isoformat()
                if credential.pending_rotation_expires_at is not None
                else None
            ),
        }
        descriptor = -1
        temporary_path: str | None = None
        try:
            descriptor, temporary_path = tempfile.mkstemp(prefix=".worker-credential-", dir=parent)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump(document, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
            os.chmod(self.path, 0o600)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_descriptor = os.open(parent, directory_flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            raise CredentialError(f"cannot persist Worker credential: {self.path}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise CredentialError("Worker pending credential fields are invalid")
    return value


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CredentialError("Worker pending credential expiry is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CredentialError("Worker pending credential expiry is invalid") from exc
    if parsed.tzinfo is None:
        raise CredentialError("Worker pending credential expiry must include a timezone")
    return parsed.astimezone(UTC)


def _validate_pending_rotation(credential: WorkerCredential) -> None:
    if _WORKER_TOKEN.fullmatch(credential.worker_token) is None:
        raise CredentialError("Worker credential token format is invalid")
    fields = (
        credential.pending_rotation_id,
        credential.pending_worker_token,
        credential.pending_rotation_expires_at,
    )
    if any(value is not None for value in fields) != all(value is not None for value in fields):
        raise CredentialError("Worker pending credential fields must be provided together")
    if credential.pending_rotation_id is not None:
        assert credential.pending_worker_token is not None
        if _WORKER_TOKEN.fullmatch(credential.pending_worker_token) is None:
            raise CredentialError("Worker pending credential token format is invalid")
        try:
            parsed = uuid.UUID(credential.pending_rotation_id)
        except ValueError as exc:
            raise CredentialError("Worker pending rotation id is invalid") from exc
        if str(parsed) != credential.pending_rotation_id:
            raise CredentialError("Worker pending rotation id is invalid")
        assert credential.pending_rotation_expires_at is not None
        if credential.pending_rotation_expires_at.tzinfo is None:
            raise CredentialError("Worker pending credential expiry must include a timezone")
