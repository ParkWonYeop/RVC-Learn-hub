from __future__ import annotations

import argparse
import asyncio
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from rvc_orchestrator_contracts import utc_now

from .audit import add_audit_event
from .config import Settings
from .database import Database
from .models import AdminBootstrapState, User
from .security import hash_password, normalize_email


class BootstrapError(RuntimeError):
    pass


async def _lock_bootstrap_state(session: AsyncSession) -> AdminBootstrapState:
    state = await session.get(AdminBootstrapState, 1)
    if state is None:
        session.add(AdminBootstrapState(id=1, lock_version=0))
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
        state = await session.get(AdminBootstrapState, 1)
        if state is None:
            raise BootstrapError("administrator bootstrap state is unavailable")

    locked = await session.execute(
        update(AdminBootstrapState)
        .where(AdminBootstrapState.id == 1)
        .values(lock_version=AdminBootstrapState.lock_version + 1)
    )
    if locked.rowcount != 1:  # type: ignore[attr-defined]
        raise BootstrapError("administrator bootstrap lock could not be acquired")
    await session.refresh(state)
    return state


async def ensure_admin_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
) -> tuple[User, bool]:
    normalized_email = normalize_email(email)
    if not 12 <= len(password) <= 1_024:
        raise BootstrapError("bootstrap password must contain 12 to 1024 characters")
    encoded_password = await run_in_threadpool(hash_password, password)
    state = await _lock_bootstrap_state(session)
    if state.admin_user_id is not None:
        bootstrapped = await session.get(User, state.admin_user_id)
        if (
            bootstrapped is None
            or bootstrapped.email != normalized_email
            or bootstrapped.role != "admin"
            or bootstrapped.disabled
        ):
            raise BootstrapError("initial administrator bootstrap is already closed")
        add_audit_event(
            session,
            actor_type="system",
            action="auth.bootstrap_admin.existing",
            resource_type="user",
            resource_id=bootstrapped.id,
        )
        await session.commit()
        return bootstrapped, False

    first_user = await session.scalar(select(User).order_by(User.created_at.asc(), User.id.asc()))
    if first_user is not None:
        if (
            first_user.email != normalized_email
            or first_user.role != "admin"
            or first_user.disabled
        ):
            raise BootstrapError("initial administrator bootstrap is already closed")
        add_audit_event(
            session,
            actor_type="system",
            action="auth.bootstrap_admin.existing",
            resource_type="user",
            resource_id=first_user.id,
        )
        state.admin_user_id = first_user.id
        state.completed_at = utc_now()
        await session.commit()
        return first_user, False

    user = User(
        email=normalized_email,
        password_hash=encoded_password,
        role="admin",
        disabled=False,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise BootstrapError("administrator bootstrap conflicted with an existing user") from exc
    add_audit_event(
        session,
        actor_type="system",
        action="auth.bootstrap_admin.created",
        resource_type="user",
        resource_id=user.id,
    )
    state.admin_user_id = user.id
    state.completed_at = utc_now()
    await session.commit()
    await session.refresh(user)
    return user, True


def _read_value_file(path: Path, label: str, *, secret: bool) -> str:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise BootstrapError(f"{label} file must be a regular non-symlink file")
        if secret and metadata.st_mode & 0o077:
            raise BootstrapError(f"{label} file must not be accessible by group or others")
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as exc:
        raise BootstrapError(f"{label} file is not readable") from exc
    if not value:
        raise BootstrapError(f"{label} file is empty")
    if "\x00" in value:
        raise BootstrapError(f"{label} contains a NUL byte")
    return value


def _resolve_bootstrap_value(
    *,
    label: str,
    direct_value: str | None,
    file_value: Path | None,
    secret: bool,
) -> str:
    if direct_value is not None and file_value is not None:
        raise BootstrapError(f"configure only one {label} source")
    if file_value is not None:
        return _read_value_file(file_value, label, secret=secret)
    if direct_value is None or not direct_value:
        raise BootstrapError(f"{label} is required")
    return direct_value


def resolve_bootstrap_credentials(
    *,
    email: str | None = None,
    email_file: Path | None = None,
    password_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    environment = os.environ if environ is None else environ
    resolved_email_file = email_file
    if resolved_email_file is None and environment.get("ADMIN_BOOTSTRAP_EMAIL_FILE"):
        resolved_email_file = Path(environment["ADMIN_BOOTSTRAP_EMAIL_FILE"])
    resolved_password_file = password_file
    if resolved_password_file is None and environment.get("ADMIN_BOOTSTRAP_PASSWORD_FILE"):
        resolved_password_file = Path(environment["ADMIN_BOOTSTRAP_PASSWORD_FILE"])
    if "ADMIN_BOOTSTRAP_PASSWORD" in environment:
        raise BootstrapError("ADMIN_BOOTSTRAP_PASSWORD is forbidden; use a protected password file")
    resolved_email = _resolve_bootstrap_value(
        label="administrator email",
        direct_value=email if email is not None else environment.get("ADMIN_BOOTSTRAP_EMAIL"),
        file_value=resolved_email_file,
        secret=False,
    )
    resolved_password = _resolve_bootstrap_value(
        label="administrator password",
        direct_value=None,
        file_value=resolved_password_file,
        secret=True,
    )
    return normalize_email(resolved_email), resolved_password


async def bootstrap_admin(settings: Settings, *, email: str, password: str) -> bool:
    database = Database(settings)
    try:
        async with database.session_factory() as session:
            _, created = await ensure_admin_user(session, email=email, password=password)
            return created
    finally:
        await database.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the first RVC Manager administrator exactly once",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--email",
        help="administrator email (password CLI arguments are forbidden)",
    )
    parser.add_argument("--email-file", type=Path)
    parser.add_argument("--password-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        email, password = resolve_bootstrap_credentials(
            email=args.email,
            email_file=args.email_file,
            password_file=args.password_file,
        )
        created = asyncio.run(bootstrap_admin(Settings(), email=email, password=password))
    except (BootstrapError, ValueError) as exc:
        raise SystemExit(f"administrator bootstrap failed: {exc}") from exc
    result = "created" if created else "already exists"
    print(f"administrator bootstrap complete: {result}")


if __name__ == "__main__":
    main()
