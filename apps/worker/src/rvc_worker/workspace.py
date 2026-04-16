"""Job workspace isolation and disk preflight."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


class WorkspaceError(RuntimeError):
    """Raised when a safe job workspace cannot be prepared."""


class InsufficientDiskError(WorkspaceError):
    """Raised when the worker does not have the configured free space."""


_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_component(untrusted: str) -> str:
    """Return a readable, collision-resistant component with no path semantics."""

    value = str(untrusted)
    if not value or "\x00" in value:
        raise WorkspaceError("workspace identifier cannot be empty or contain NUL")
    readable = _SAFE_CHARS.sub("-", value).strip(".-_")[:40] or "id"
    digest = sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{readable}-{digest}"


def ensure_within(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise WorkspaceError(f"path escapes workspace root: {resolved_path}")
    return resolved_path


@dataclass(frozen=True, slots=True)
class JobWorkspace:
    root: Path
    inputs: Path
    work: Path
    outputs: Path
    logs: Path
    spool: Path

    def assert_path(self, path: Path) -> Path:
        return ensure_within(path, self.root)


class WorkspaceManager:
    def __init__(self, root: Path, *, min_free_bytes: int = 0) -> None:
        self.root = root.expanduser().resolve()
        self.min_free_bytes = min_free_bytes

    def prepare(self, job_id: str, attempt_id: str) -> JobWorkspace:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.check_disk()
        job_root = ensure_within(
            self.root / safe_component(job_id) / safe_component(attempt_id), self.root
        )
        directories = {
            "inputs": job_root / "inputs",
            "work": job_root / "work",
            "outputs": job_root / "outputs",
            "logs": job_root / "logs",
            "spool": job_root / "spool",
        }
        for directory in (job_root, *directories.values()):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink():
                raise WorkspaceError(f"workspace directory cannot be a symlink: {directory}")
            ensure_within(directory, self.root)
        return JobWorkspace(root=job_root, **directories)

    def check_disk(self) -> int:
        probe = self.root if self.root.exists() else self.root.parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            free = shutil.disk_usage(probe).free
        except OSError as exc:
            raise WorkspaceError(f"cannot inspect free space for {probe}") from exc
        if free < self.min_free_bytes:
            raise InsufficientDiskError(
                f"worker requires {self.min_free_bytes} free bytes but only {free} are available"
            )
        return free
