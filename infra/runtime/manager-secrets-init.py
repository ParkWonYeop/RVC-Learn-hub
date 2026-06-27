"""Atomically project root-owned Manager secrets to least-privilege runtime volumes."""

from __future__ import annotations

import os
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

SOURCE_ROOT = Path("/run/secrets")
MAX_SECRET_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    target_root: Path
    uid: int
    gid: int
    secret_names: tuple[str, ...]


PROFILES = (
    Profile(
        name="api",
        target_root=Path("/prepared/api"),
        uid=10001,
        gid=10001,
        secret_names=(
            "postgres_password",
            "redis_password",
            "minio_app_access_key",
            "minio_app_secret_key",
            "worker_bootstrap_token",
            "worker_token_pepper",
            "jwt_secret",
        ),
    ),
    Profile(
        name="maintenance",
        target_root=Path("/prepared/maintenance"),
        uid=10001,
        gid=10001,
        secret_names=(
            "maintenance_postgres_password",
            "maintenance_redis_password",
            "maintenance_s3_access_key",
            "maintenance_s3_secret_key",
        ),
    ),
    Profile(
        name="mlflow",
        target_root=Path("/prepared/mlflow"),
        uid=10002,
        gid=10002,
        secret_names=(
            "mlflow_postgres_password",
            "mlflow_s3_access_key",
            "mlflow_s3_secret_key",
        ),
    ),
    Profile(
        name="database_authz",
        target_root=Path("/prepared/database-authz"),
        uid=10001,
        gid=10001,
        secret_names=(
            "postgres_password",
            "maintenance_postgres_password",
        ),
    ),
)

SEPARATED_SECRET_PAIRS = (
    ("postgres_password", "maintenance_postgres_password"),
    ("redis_password", "maintenance_redis_password"),
    ("minio_app_access_key", "maintenance_s3_access_key"),
    ("minio_app_secret_key", "maintenance_s3_secret_key"),
)


class ProjectionError(RuntimeError):
    """Raised when a secret source or projection target is unsafe."""


@dataclass(slots=True)
class StagedGeneration:
    profile: Profile
    generation_name: str
    previous_generation: str | None


def _directory_lstat(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProjectionError(f"required directory is unavailable: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ProjectionError(f"required path is not a real directory: {path}")
    return metadata


def _read_secret(name: str) -> bytes:
    source = SOURCE_ROOT / name
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ProjectionError(f"required source secret is unreadable: {name}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ProjectionError(f"source secret is not a regular file: {name}")
        if metadata.st_size <= 0 or metadata.st_size > MAX_SECRET_BYTES:
            raise ProjectionError(f"source secret size is invalid: {name}")
        chunks: list[bytes] = []
        remaining = MAX_SECRET_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks).replace(b"\r", b"").replace(b"\n", b"")
        if not value or len(value) > MAX_SECRET_BYTES or b"\x00" in value:
            raise ProjectionError(f"source secret content is invalid: {name}")
        return value
    finally:
        os.close(descriptor)


def _read_all_sources() -> dict[str, bytes]:
    _directory_lstat(SOURCE_ROOT)
    required_names = sorted({name for profile in PROFILES for name in profile.secret_names})
    values = {name: _read_secret(name) for name in required_names}
    for api_name, maintenance_name in SEPARATED_SECRET_PAIRS:
        if values[api_name] == values[maintenance_name]:
            raise ProjectionError(
                f"maintenance credential must be distinct from API credential: {maintenance_name}"
            )
    return values


def _current_generation(profile: Profile) -> str | None:
    current = profile.target_root / "current"
    try:
        metadata = current.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProjectionError(f"cannot inspect {profile.name} current generation") from exc
    if not stat.S_ISLNK(metadata.st_mode):
        raise ProjectionError(f"{profile.name} current generation is not a symlink")
    target = os.readlink(current)
    if "/" in target or not target.startswith("generation-"):
        raise ProjectionError(f"{profile.name} current generation target is invalid")
    generation = profile.target_root / target
    generation_metadata = _directory_lstat(generation)
    if generation_metadata.st_uid != 0 or generation_metadata.st_gid != profile.gid:
        raise ProjectionError(f"{profile.name} generation ownership is invalid")
    if stat.S_IMODE(generation_metadata.st_mode) != 0o710:
        raise ProjectionError(f"{profile.name} generation mode is invalid")
    _validate_generation_inventory(profile, generation, complete=True)
    return target


def _validate_generation_inventory(
    profile: Profile,
    generation: Path,
    *,
    complete: bool,
) -> None:
    expected = set(profile.secret_names)
    actual: set[str] = set()
    for entry in os.scandir(generation):
        if entry.name not in expected:
            raise ProjectionError(f"unexpected {profile.name} runtime secret entry")
        if not entry.is_file(follow_symlinks=False):
            raise ProjectionError(f"unsafe {profile.name} runtime secret entry")
        if not complete:
            actual.add(entry.name)
            continue
        metadata = entry.stat(follow_symlinks=False)
        if metadata.st_uid != profile.uid or metadata.st_gid != profile.gid:
            raise ProjectionError(f"{profile.name} runtime secret ownership is invalid")
        if stat.S_IMODE(metadata.st_mode) != 0o400 or metadata.st_size <= 0:
            raise ProjectionError(f"{profile.name} runtime secret mode or size is invalid")
        actual.add(entry.name)
    if complete and actual != expected:
        raise ProjectionError(f"{profile.name} runtime secret inventory is incomplete")


def _validate_target_root(profile: Profile) -> str | None:
    metadata = _directory_lstat(profile.target_root)
    if metadata.st_uid != 0:
        raise ProjectionError(f"{profile.name} projection root must be owned by root")
    os.chown(profile.target_root, 0, 0)
    os.chmod(profile.target_root, 0o711)
    current = _current_generation(profile)
    allowed_current = {current} if current is not None else set()
    for entry in os.scandir(profile.target_root):
        if entry.name == "current" or entry.name in allowed_current:
            continue
        if entry.name.startswith("generation-") and entry.is_dir(follow_symlinks=False):
            _remove_generation(profile, entry.name, allow_incomplete=True)
            continue
        if entry.name.startswith(".current-") and entry.is_symlink():
            os.unlink(entry.path)
            continue
        raise ProjectionError(f"unexpected entry in {profile.name} projection root")
    return current


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_secret(path: Path, value: bytes, profile: Profile) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(value):
            written = os.write(descriptor, value[offset:])
            if written <= 0:
                raise ProjectionError("could not write projected runtime secret")
            offset += written
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
        os.fchown(descriptor, profile.uid, profile.gid)
    finally:
        os.close(descriptor)


def _stage_profile(profile: Profile, values: dict[str, bytes]) -> StagedGeneration:
    previous = _validate_target_root(profile)
    generation_name = f"generation-{uuid.uuid4().hex}"
    generation = profile.target_root / generation_name
    os.mkdir(generation, 0o700)
    try:
        for name in profile.secret_names:
            _write_secret(generation / name, values[name], profile)
        os.chown(generation, 0, profile.gid)
        os.chmod(generation, 0o710)
        _fsync_directory(generation)
        _validate_generation_inventory(profile, generation, complete=True)
        return StagedGeneration(profile, generation_name, previous)
    except BaseException:
        _remove_generation(profile, generation_name, allow_incomplete=True)
        raise


def _replace_current(profile: Profile, generation_name: str) -> None:
    temporary = f".current-{uuid.uuid4().hex}"
    temporary_path = profile.target_root / temporary
    os.symlink(generation_name, temporary_path)
    try:
        os.replace(temporary_path, profile.target_root / "current")
        _fsync_directory(profile.target_root)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _remove_generation(
    profile: Profile,
    generation_name: str,
    *,
    allow_incomplete: bool,
) -> None:
    if "/" in generation_name or not generation_name.startswith("generation-"):
        raise ProjectionError("refusing to remove an unsafe generation path")
    generation = profile.target_root / generation_name
    try:
        metadata = generation.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ProjectionError("refusing to remove an unsafe generation")
    _validate_generation_inventory(profile, generation, complete=not allow_incomplete)
    for entry in os.scandir(generation):
        os.unlink(entry.path)
    os.rmdir(generation)


def _rollback_activation(staged: list[StagedGeneration], activated: int) -> None:
    for state in reversed(staged[:activated]):
        if state.previous_generation is None:
            try:
                (state.profile.target_root / "current").unlink()
            except FileNotFoundError:
                pass
            _fsync_directory(state.profile.target_root)
        else:
            _replace_current(state.profile, state.previous_generation)


def project_runtime_secrets() -> None:
    values = _read_all_sources()
    staged: list[StagedGeneration] = []
    try:
        for profile in PROFILES:
            staged.append(_stage_profile(profile, values))
    except BaseException:
        for state in staged:
            _remove_generation(state.profile, state.generation_name, allow_incomplete=False)
        raise

    activated = 0
    try:
        for state in staged:
            try:
                _replace_current(state.profile, state.generation_name)
            except BaseException:
                current = state.profile.target_root / "current"
                try:
                    if current.is_symlink() and os.readlink(current) == state.generation_name:
                        activated += 1
                except OSError:
                    pass
                raise
            else:
                activated += 1
    except BaseException:
        _rollback_activation(staged, activated)
        for state in staged:
            _remove_generation(state.profile, state.generation_name, allow_incomplete=False)
        raise

    for state in staged:
        if state.previous_generation and state.previous_generation != state.generation_name:
            _remove_generation(
                state.profile,
                state.previous_generation,
                allow_incomplete=False,
            )
    print("Manager runtime secret projections are ready")


def main() -> int:
    try:
        project_runtime_secrets()
    except (OSError, ProjectionError) as exc:
        print(f"Manager runtime secret projection failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
