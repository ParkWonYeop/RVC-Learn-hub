from __future__ import annotations

import asyncio
from typing import Annotated, NoReturn, cast

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status

from ..dependencies import CurrentUserDep, SessionDep, SettingsDep, UserAuthDep
from ..schemas import (
    ModelRegistryCandidateCreate,
    ModelRegistryEntryPromote,
    ModelRegistryEntryRevoke,
    ModelRegistryMutationRead,
    ModelRegistryRead,
)
from ..services.model_registry import (
    ModelRegistryAuthenticationChanged,
    ModelRegistryConflict,
    ModelRegistryNotFound,
    ModelRegistryUnavailable,
    create_candidate,
    get_registry,
    promote_entry,
    revoke_entry,
)
from ..storage import StorageAdapter

router = APIRouter(prefix="/experiments/{experiment_id}/model-registry", tags=["model-registry"])

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    ),
]
ExpectedActorId = Annotated[
    str | None,
    Header(
        alias="X-RVC-Expected-Actor-ID",
        min_length=36,
        max_length=36,
        pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$",
    ),
]


def _private_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"
    response.headers["X-Content-Type-Options"] = "nosniff"


def _require_expected_actor(actual_actor_id: str, expected_actor_id: str | None) -> None:
    if expected_actor_id is not None and expected_actor_id != actual_actor_id:
        raise ModelRegistryConflict("authenticated actor changed; reload before retry")


def _raise_registry_error(exc: Exception) -> NoReturn:
    private_headers = {
        "Cache-Control": "private, no-store",
        "Vary": "Authorization",
    }
    if isinstance(exc, ModelRegistryNotFound):
        raise HTTPException(status_code=404, detail=str(exc), headers=private_headers) from exc
    if isinstance(exc, ModelRegistryAuthenticationChanged):
        raise HTTPException(
            status_code=401,
            detail="invalid or expired access token",
            headers={**private_headers, "WWW-Authenticate": "Bearer"},
        ) from exc
    if isinstance(exc, ModelRegistryConflict):
        raise HTTPException(status_code=409, detail=str(exc), headers=private_headers) from exc
    if isinstance(exc, ModelRegistryUnavailable):
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={**private_headers, "Retry-After": "1"},
        ) from exc
    raise exc


@router.get("", response_model=ModelRegistryRead)
async def read_model_registry(
    experiment_id: str,
    session: SessionDep,
    user: CurrentUserDep,
    response: Response,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ModelRegistryRead:
    try:
        result = await get_registry(
            session,
            experiment_id=experiment_id,
            actor=user,
            offset=offset,
            limit=limit,
        )
    except (ModelRegistryNotFound, ModelRegistryConflict) as exc:
        _raise_registry_error(exc)
    _private_no_store(response)
    return result


@router.post(
    "/candidates",
    response_model=ModelRegistryMutationRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_model_registry_candidate(
    experiment_id: str,
    payload: ModelRegistryCandidateCreate,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    auth: UserAuthDep,
    idempotency_key: IdempotencyKey,
    expected_actor_id: ExpectedActorId = None,
) -> ModelRegistryMutationRead:
    try:
        _require_expected_actor(auth.user.id, expected_actor_id)
        result, replayed = await create_candidate(
            session,
            cast(StorageAdapter, request.app.state.storage),
            settings,
            cast(asyncio.Semaphore, request.app.state.sample_verification_semaphore),
            experiment_id=experiment_id,
            actor=auth.user,
            actor_token_version=auth.claims.access_token_version,
            payload=payload,
            idempotency_key=idempotency_key,
            path=request.url.path,
        )
    except (
        ModelRegistryNotFound,
        ModelRegistryAuthenticationChanged,
        ModelRegistryConflict,
        ModelRegistryUnavailable,
    ) as exc:
        _raise_registry_error(exc)
    _private_no_store(response)
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return result


@router.post("/entries/{entry_id}/promote", response_model=ModelRegistryMutationRead)
async def promote_model_registry_entry(
    experiment_id: str,
    entry_id: str,
    payload: ModelRegistryEntryPromote,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    auth: UserAuthDep,
    idempotency_key: IdempotencyKey,
    expected_actor_id: ExpectedActorId = None,
) -> ModelRegistryMutationRead:
    try:
        _require_expected_actor(auth.user.id, expected_actor_id)
        result, replayed = await promote_entry(
            session,
            cast(StorageAdapter, request.app.state.storage),
            settings,
            cast(asyncio.Semaphore, request.app.state.sample_verification_semaphore),
            experiment_id=experiment_id,
            entry_id=entry_id,
            actor=auth.user,
            actor_token_version=auth.claims.access_token_version,
            payload=payload,
            idempotency_key=idempotency_key,
            path=request.url.path,
        )
    except (
        ModelRegistryNotFound,
        ModelRegistryAuthenticationChanged,
        ModelRegistryConflict,
        ModelRegistryUnavailable,
    ) as exc:
        _raise_registry_error(exc)
    _private_no_store(response)
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return result


@router.post("/entries/{entry_id}/revoke", response_model=ModelRegistryMutationRead)
async def revoke_model_registry_entry(
    experiment_id: str,
    entry_id: str,
    payload: ModelRegistryEntryRevoke,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    auth: UserAuthDep,
    idempotency_key: IdempotencyKey,
    expected_actor_id: ExpectedActorId = None,
) -> ModelRegistryMutationRead:
    try:
        _require_expected_actor(auth.user.id, expected_actor_id)
        result, replayed = await revoke_entry(
            session,
            settings,
            experiment_id=experiment_id,
            entry_id=entry_id,
            actor=auth.user,
            actor_token_version=auth.claims.access_token_version,
            payload=payload,
            idempotency_key=idempotency_key,
            path=request.url.path,
        )
    except (
        ModelRegistryNotFound,
        ModelRegistryAuthenticationChanged,
        ModelRegistryConflict,
    ) as exc:
        _raise_registry_error(exc)
    _private_no_store(response)
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return result
