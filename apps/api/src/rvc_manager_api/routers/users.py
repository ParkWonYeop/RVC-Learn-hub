from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError
from starlette.concurrency import run_in_threadpool

from ..audit import add_audit_event
from ..config import Settings
from ..dependencies import AdminUserDep, SessionDep, SettingsDep
from ..models import AdminBootstrapState, AdminUserOperation, User
from ..schemas import (
    AdminUserAccessUpdate,
    AdminUserCreate,
    AdminUserList,
    AdminUserPasswordReset,
    AdminUserRead,
)
from ..security import hash_password, normalize_email, validate_management_password

router = APIRouter(prefix="/admin/users", tags=["admin-users"])

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    ),
]
OperationType = Literal["create", "access_update", "password_reset"]

_IDEMPOTENCY_CONFLICT = "idempotency key conflicts with a prior user lifecycle request"
_STALE_USER = "user changed; refresh and retry"
_PASSWORD_POLICY = "password does not satisfy the administrator password policy"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_read(user: User) -> AdminUserRead:
    return AdminUserRead(
        id=user.id,
        email=user.email,
        role=user.role,  # type: ignore[arg-type]
        active=not user.disabled,
        row_version=user.row_version,
        created_at=_as_utc(user.created_at),
        updated_at=_as_utc(user.updated_at),
    )


def _idempotency_key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request_fingerprint(
    settings: Settings,
    *,
    operation_type: OperationType,
    resource_id: str,
    document: dict[str, object],
) -> str:
    canonical = json.dumps(
        {
            "operation_type": operation_type,
            "resource_id": resource_id,
            "document": document,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    # A keyed fingerprint permits exact password-request replay without
    # persisting a reusable offline verifier for the plaintext password.
    return hmac.new(
        settings.jwt_secret.get_secret_value().encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()


async def _replay_or_none(
    session: AsyncSession,
    response: Response,
    *,
    actor_id: str,
    key_hash: str,
    operation_type: OperationType,
    fingerprint: str,
) -> AdminUserRead | None:
    operation = await session.scalar(
        select(AdminUserOperation).where(
            AdminUserOperation.actor_id == actor_id,
            AdminUserOperation.idempotency_key_hash == key_hash,
        )
    )
    if operation is None:
        return None
    if operation.operation_type != operation_type or operation.request_fingerprint != fingerprint:
        raise HTTPException(status_code=409, detail=_IDEMPOTENCY_CONFLICT)
    response.headers["Idempotency-Replayed"] = "true"
    response.headers["Cache-Control"] = "no-store"
    return AdminUserRead.model_validate(operation.response_json)


async def _lock_lifecycle_fence(
    session: AsyncSession,
    *,
    actor_id: str,
) -> User:
    # This singleton update is a real write on SQLite and a row lock on
    # PostgreSQL. It serializes all admin lifecycle writes, including the
    # two-admin cross-demotion race that a target-row lock cannot prevent.
    locked = await session.execute(
        update(AdminBootstrapState)
        .where(AdminBootstrapState.id == 1)
        .values(lock_version=AdminBootstrapState.lock_version + 1)
        .execution_options(synchronize_session=False)
    )
    if locked.rowcount != 1:  # type: ignore[attr-defined]
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="administrator lifecycle fence is unavailable",
        )
    actor = await session.scalar(
        select(User)
        .where(User.id == actor_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if actor is None or actor.disabled or actor.role != "admin":
        # The dependency may have authenticated before a concurrent demotion.
        # Rechecking behind the global fence prevents that stale authority
        # from applying a second mutation.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="administrator role required",
        )
    return actor


async def _locked_user(session: AsyncSession, user_id: str) -> User:
    user = await session.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user


def _record_operation(
    session: AsyncSession,
    *,
    actor_id: str,
    key_hash: str,
    operation_type: OperationType,
    fingerprint: str,
    result: AdminUserRead,
) -> None:
    session.add(
        AdminUserOperation(
            actor_id=actor_id,
            idempotency_key_hash=key_hash,
            request_fingerprint=fingerprint,
            operation_type=operation_type,
            resource_id=result.id,
            response_json=result.model_dump(mode="json"),
        )
    )


def _validate_new_password(password: str, email: str) -> None:
    try:
        validate_management_password(password, email=email)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_PASSWORD_POLICY) from exc


@router.get("", response_model=AdminUserList)
async def list_users(
    session: SessionDep,
    _admin: AdminUserDep,
    response: Response,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    email: Annotated[str | None, Query(min_length=3, max_length=320)] = None,
    role: Annotated[Literal["admin", "user"] | None, Query()] = None,
    active: Annotated[bool | None, Query()] = None,
) -> AdminUserList:
    filters = []
    if email is not None:
        try:
            normalized_email = normalize_email(email)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid email filter") from exc
        filters.append(User.email == normalized_email)
    if role is not None:
        filters.append(User.role == role)
    if active is not None:
        filters.append(User.disabled.is_(not active))
    total = await session.scalar(select(func.count()).select_from(User).where(*filters)) or 0
    users = list(
        (
            await session.scalars(
                select(User)
                .where(*filters)
                .order_by(User.created_at.desc(), User.id.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    response.headers["Cache-Control"] = "no-store"
    return AdminUserList(
        items=[_to_read(user) for user in users],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/{user_id}", response_model=AdminUserRead)
async def get_user(
    user_id: str,
    session: SessionDep,
    _admin: AdminUserDep,
    response: Response,
) -> AdminUserRead:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    response.headers["Cache-Control"] = "no-store"
    return _to_read(user)


@router.post("", response_model=AdminUserRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: AdminUserCreate,
    session: SessionDep,
    settings: SettingsDep,
    admin: AdminUserDep,
    response: Response,
    idempotency_key: IdempotencyKey,
) -> AdminUserRead:
    actor_id = admin.id
    key_hash = _idempotency_key_hash(idempotency_key)
    password = payload.password.get_secret_value()
    fingerprint = _request_fingerprint(
        settings,
        operation_type="create",
        resource_id="new",
        document={
            "email": payload.email,
            "password": password,
            "role": payload.role,
            "active": payload.active,
        },
    )
    replay = await _replay_or_none(
        session,
        response,
        actor_id=actor_id,
        key_hash=key_hash,
        operation_type="create",
        fingerprint=fingerprint,
    )
    if replay is not None:
        return replay
    await session.rollback()
    _validate_new_password(password, payload.email)
    encoded_password = await run_in_threadpool(hash_password, password)
    await _lock_lifecycle_fence(session, actor_id=actor_id)
    replay = await _replay_or_none(
        session,
        response,
        actor_id=actor_id,
        key_hash=key_hash,
        operation_type="create",
        fingerprint=fingerprint,
    )
    if replay is not None:
        await session.rollback()
        return replay
    if await session.scalar(select(User.id).where(User.email == payload.email).limit(1)):
        raise HTTPException(status_code=409, detail="user email already exists")
    user = User(
        email=payload.email,
        password_hash=encoded_password,
        role=payload.role,
        disabled=not payload.active,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="user email already exists") from exc
    result = _to_read(user)
    _record_operation(
        session,
        actor_id=actor_id,
        key_hash=key_hash,
        operation_type="create",
        fingerprint=fingerprint,
        result=result,
    )
    add_audit_event(
        session,
        actor_type="user",
        actor_id=actor_id,
        action="admin.user.created",
        resource_type="user",
        resource_id=user.id,
        details={"role": user.role, "active": not user.disabled, "row_version": 1},
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=_IDEMPOTENCY_CONFLICT) from exc
    response.headers["Cache-Control"] = "no-store"
    return result


@router.patch("/{user_id}", response_model=AdminUserRead)
async def update_user_access(
    user_id: str,
    payload: AdminUserAccessUpdate,
    session: SessionDep,
    settings: SettingsDep,
    admin: AdminUserDep,
    response: Response,
    idempotency_key: IdempotencyKey,
) -> AdminUserRead:
    actor_id = admin.id
    key_hash = _idempotency_key_hash(idempotency_key)
    fingerprint = _request_fingerprint(
        settings,
        operation_type="access_update",
        resource_id=user_id,
        document=payload.model_dump(mode="json"),
    )
    replay = await _replay_or_none(
        session,
        response,
        actor_id=actor_id,
        key_hash=key_hash,
        operation_type="access_update",
        fingerprint=fingerprint,
    )
    if replay is not None:
        return replay
    await session.rollback()
    await _lock_lifecycle_fence(session, actor_id=actor_id)
    replay = await _replay_or_none(
        session,
        response,
        actor_id=actor_id,
        key_hash=key_hash,
        operation_type="access_update",
        fingerprint=fingerprint,
    )
    if replay is not None:
        await session.rollback()
        return replay
    user = await _locked_user(session, user_id)
    if user.row_version != payload.expected_row_version:
        raise HTTPException(status_code=409, detail=_STALE_USER)
    desired_disabled = not payload.active
    if user.id == actor_id and (payload.role != "admin" or desired_disabled):
        raise HTTPException(
            status_code=409,
            detail="administrators cannot disable or demote their own account",
        )
    removes_active_admin = (
        user.role == "admin" and not user.disabled and (payload.role != "admin" or desired_disabled)
    )
    if removes_active_admin:
        active_admin_count = (
            await session.scalar(
                select(func.count())
                .select_from(User)
                .where(
                    User.role == "admin",
                    User.disabled.is_(False),
                )
            )
            or 0
        )
        if active_admin_count <= 1:
            raise HTTPException(
                status_code=409,
                detail="at least one active administrator is required",
            )
    changed_fields: list[str] = []
    previous_role = user.role
    previous_active = not user.disabled
    if user.role != payload.role:
        changed_fields.append("role")
    if user.disabled != desired_disabled:
        changed_fields.append("active")
    previous_row_version = user.row_version
    if changed_fields:
        user.role = payload.role
        user.disabled = desired_disabled
        # Role and active-state changes permanently invalidate tokens issued
        # under the former authorization state, including after reactivation.
        user.access_token_version += 1
        action = "admin.user.access_updated"
    else:
        action = "admin.user.access_unchanged"
    try:
        await session.flush()
    except StaleDataError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=_STALE_USER) from exc
    result = _to_read(user)
    _record_operation(
        session,
        actor_id=actor_id,
        key_hash=key_hash,
        operation_type="access_update",
        fingerprint=fingerprint,
        result=result,
    )
    add_audit_event(
        session,
        actor_type="user",
        actor_id=actor_id,
        action=action,
        resource_type="user",
        resource_id=user.id,
        details={
            "changed_fields": changed_fields,
            "previous_role": previous_role,
            "new_role": payload.role,
            "previous_active": previous_active,
            "new_active": payload.active,
            "previous_row_version": previous_row_version,
            "new_row_version": result.row_version,
            "access_tokens_invalidated": bool(changed_fields),
        },
    )
    try:
        await session.commit()
    except (IntegrityError, StaleDataError) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=_STALE_USER) from exc
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/{user_id}/password-reset", response_model=AdminUserRead)
async def reset_user_password(
    user_id: str,
    payload: AdminUserPasswordReset,
    session: SessionDep,
    settings: SettingsDep,
    admin: AdminUserDep,
    response: Response,
    idempotency_key: IdempotencyKey,
) -> AdminUserRead:
    actor_id = admin.id
    key_hash = _idempotency_key_hash(idempotency_key)
    password = payload.new_password.get_secret_value()
    fingerprint = _request_fingerprint(
        settings,
        operation_type="password_reset",
        resource_id=user_id,
        document={
            "expected_row_version": payload.expected_row_version,
            "new_password": password,
        },
    )
    replay = await _replay_or_none(
        session,
        response,
        actor_id=actor_id,
        key_hash=key_hash,
        operation_type="password_reset",
        fingerprint=fingerprint,
    )
    if replay is not None:
        return replay
    await session.rollback()
    unlocked_user = await session.get(User, user_id)
    if unlocked_user is None:
        raise HTTPException(status_code=404, detail="user not found")
    unlocked_email = unlocked_user.email
    _validate_new_password(password, unlocked_email)
    await session.rollback()
    encoded_password = await run_in_threadpool(hash_password, password)
    await _lock_lifecycle_fence(session, actor_id=actor_id)
    replay = await _replay_or_none(
        session,
        response,
        actor_id=actor_id,
        key_hash=key_hash,
        operation_type="password_reset",
        fingerprint=fingerprint,
    )
    if replay is not None:
        await session.rollback()
        return replay
    user = await _locked_user(session, user_id)
    if user.row_version != payload.expected_row_version:
        raise HTTPException(status_code=409, detail=_STALE_USER)
    _validate_new_password(password, user.email)
    previous_row_version = user.row_version
    user.password_hash = encoded_password
    user.access_token_version += 1
    try:
        await session.flush()
    except StaleDataError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=_STALE_USER) from exc
    result = _to_read(user)
    _record_operation(
        session,
        actor_id=actor_id,
        key_hash=key_hash,
        operation_type="password_reset",
        fingerprint=fingerprint,
        result=result,
    )
    add_audit_event(
        session,
        actor_type="user",
        actor_id=actor_id,
        action="admin.user.password_reset",
        resource_type="user",
        resource_id=user.id,
        details={
            "previous_row_version": previous_row_version,
            "new_row_version": result.row_version,
            "access_tokens_invalidated": True,
        },
    )
    try:
        await session.commit()
    except (IntegrityError, StaleDataError) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=_STALE_USER) from exc
    response.headers["Cache-Control"] = "no-store"
    return result
