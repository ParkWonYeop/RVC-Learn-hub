from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .models import RevokedAccessToken, User, Worker
from .security import AccessTokenClaims, InvalidAccessToken, decode_access_token, hash_worker_token
from .services.mlflow import MlflowCoordinator

bearer_scheme = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_mlflow(request: Request) -> MlflowCoordinator:
    return cast(MlflowCoordinator, request.app.state.mlflow)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.database.session_factory() as session:
        yield session


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_worker(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Worker:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("worker bearer token required")
    digest = hash_worker_token(credentials.credentials, settings)
    worker = await session.scalar(
        select(Worker).where(Worker.token_hash == digest, Worker.is_active.is_(True))
    )
    if worker is None:
        raise _unauthorized("invalid worker token")
    return worker


@dataclass(frozen=True, slots=True)
class UserAuthContext:
    user: User
    claims: AccessTokenClaims


async def require_user_auth(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserAuthContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("user bearer token required")
    try:
        claims = decode_access_token(credentials.credentials, settings)
    except InvalidAccessToken as exc:
        raise _unauthorized("invalid or expired access token") from exc
    if await session.get(RevokedAccessToken, claims.jti) is not None:
        raise _unauthorized("invalid or expired access token")
    user = await session.get(User, claims.subject)
    if user is None or user.disabled or user.access_token_version != claims.access_token_version:
        raise _unauthorized("invalid or expired access token")
    return UserAuthContext(user=user, claims=claims)


async def current_user(auth: Annotated[UserAuthContext, Depends(require_user_auth)]) -> User:
    return auth.user


async def require_admin_user(
    auth: Annotated[UserAuthContext, Depends(require_user_auth)],
) -> User:
    if auth.user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="administrator role required",
        )
    return auth.user


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
MlflowDep = Annotated[MlflowCoordinator, Depends(get_mlflow)]
WorkerDep = Annotated[Worker, Depends(require_worker)]
UserAuthDep = Annotated[UserAuthContext, Depends(require_user_auth)]
CurrentUserDep = Annotated[User, Depends(current_user)]
AdminUserDep = Annotated[User, Depends(require_admin_user)]
