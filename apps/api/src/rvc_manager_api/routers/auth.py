from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from ..audit import add_audit_event
from ..dependencies import SessionDep, SettingsDep, UserAuthDep
from ..models import RevokedAccessToken, User
from ..schemas import AccessTokenResponse, LoginRequest, UserRead
from ..security import audit_email_fingerprint, issue_access_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


def _login_failed() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="incorrect email or password",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.post("/login", response_model=AccessTokenResponse)
async def login(
    payload: LoginRequest,
    session: SessionDep,
    settings: SettingsDep,
    response: Response,
) -> AccessTokenResponse:
    user = await session.scalar(select(User).where(User.email == payload.email))
    password_ok = await run_in_threadpool(
        verify_password,
        payload.password.get_secret_value(),
        user.password_hash if user is not None else None,
    )
    if user is None or not password_ok or user.disabled:
        add_audit_event(
            session,
            actor_type="anonymous",
            action="auth.login.failed",
            resource_type="user",
            details={"email_fingerprint": audit_email_fingerprint(payload.email)},
        )
        await session.commit()
        raise _login_failed()

    token, claims = issue_access_token(
        user.id,
        settings,
        access_token_version=user.access_token_version,
    )
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="auth.login.succeeded",
        resource_type="user",
        resource_id=user.id,
        details={"jti": claims.jti, "expires_at": claims.expires_at.isoformat()},
    )
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return AccessTokenResponse(
        access_token=token,
        expires_in=settings.jwt_access_ttl_seconds,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(auth: UserAuthDep, session: SessionDep) -> Response:
    session.add(
        RevokedAccessToken(
            jti=auth.claims.jti,
            user_id=auth.user.id,
            expires_at=auth.claims.expires_at,
        )
    )
    add_audit_event(
        session,
        actor_type="user",
        actor_id=auth.user.id,
        action="auth.logout",
        resource_type="access_token",
        resource_id=auth.claims.jti,
        details={"expires_at": auth.claims.expires_at.isoformat()},
    )
    try:
        await session.commit()
    except IntegrityError:
        # Two concurrent logout requests may both authenticate before either
        # revocation row is committed. The primary-key collision still means
        # the token is revoked, so preserve idempotent logout semantics.
        await session.rollback()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserRead)
async def me(auth: UserAuthDep) -> User:
    return auth.user
