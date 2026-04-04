from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Annotated, Any, TypeVar, cast

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from rvc_orchestrator_contracts import utc_now

from ..audit import add_audit_event
from ..database import Database
from ..dependencies import CurrentUserDep, SessionDep, SettingsDep
from ..models import Dataset, DatasetUploadSession, Experiment, Job, User
from ..schemas import (
    DatasetCreate,
    DatasetList,
    DatasetRead,
    DatasetUploadInitRequest,
    DatasetUploadInitResponse,
)
from ..services.artifacts import (
    ArtifactSpoolError,
    ArtifactVerificationMismatch,
    remove_spool_file,
    upload_token_hash,
    verify_object_to_spool,
    verify_upload_token,
)
from ..services.datasets import (
    DatasetPreparationError,
    PreparedDatasetSnapshot,
    cleanup_dataset_snapshot,
    dataset_extension,
    dataset_temporary_object_key,
    dataset_to_read,
    dataset_upload_request_fingerprint,
    dataset_upload_ttl_seconds,
    dataset_verified_object_keys,
    derive_dataset_upload_token,
    prepare_dataset_snapshot,
)
from ..services.workers import as_utc
from ..storage import (
    LocalStorageAdapter,
    ObjectNotFound,
    ObjectSizeMismatch,
    ObjectTooLarge,
    StorageAdapter,
    StorageError,
    storage_namespace_matches,
)

router = APIRouter(tags=["datasets"])
_T = TypeVar("_T")


class DatasetFinalizationLeaseLost(RuntimeError):
    pass


class DatasetUploadWriteLeaseLost(RuntimeError):
    pass


def get_storage(request: Request) -> StorageAdapter:
    return cast(StorageAdapter, request.app.state.storage)


StorageDep = Annotated[StorageAdapter, Depends(get_storage)]


def _upload_storage_matches(
    upload: DatasetUploadSession,
    storage: StorageAdapter,
) -> bool:
    return storage_namespace_matches(
        backend=upload.storage_backend,
        namespace_sha256=upload.storage_namespace_sha256,
        storage=storage,
    )


def _require_dataset_owner_or_admin(dataset: Dataset, user: User) -> None:
    if user.role != "admin" and dataset.created_by != user.id:
        raise HTTPException(status_code=404, detail="dataset not found")


async def _get_owned_dataset(
    dataset_id: str,
    *,
    session: SessionDep,
    user: CurrentUserDep,
) -> Dataset:
    dataset = await session.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset not found")
    _require_dataset_owner_or_admin(dataset, user)
    return dataset


def _public_api_base_url(request: Request, settings: SettingsDep) -> str:
    return settings.public_api_base_url or str(request.base_url).rstrip("/")


def _retry_metadata(
    upload: DatasetUploadSession,
    settings: SettingsDep,
) -> tuple[bool, int | None]:
    if upload.status in {"pending", "finalizing", "expired"}:
        retry_after = (
            settings.dataset_retry_after_seconds
            if upload.status != "pending" or upload.failure_code
            else None
        )
        return True, retry_after
    return False, None


async def _upload_init_response(
    upload: DatasetUploadSession,
    *,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageAdapter,
) -> DatasetUploadInitResponse:
    if not _upload_storage_matches(upload, storage):
        raise HTTPException(status_code=503, detail="dataset storage namespace is unavailable")
    dataset = await session.get(Dataset, upload.dataset_id)
    retryable, retry_after_seconds = _retry_metadata(upload, settings)
    if upload.status != "pending":
        return DatasetUploadInitResponse(
            upload_session_id=upload.id,
            dataset_id=upload.dataset_id,
            status=upload.status,  # type: ignore[arg-type]
            expires_at=upload.expires_at,
            dataset=dataset_to_read(dataset) if dataset is not None else None,
            failure_code=upload.failure_code,
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
        )
    local_token = None
    if upload.storage_backend == "local":
        local_token = derive_dataset_upload_token(
            upload.id,
            int(as_utc(upload.expires_at).timestamp()),
            settings,
        )
    target = await storage.create_upload_target(
        session_id=upload.id,
        object_key=upload.temporary_object_key,
        public_api_base_url=_public_api_base_url(request, settings),
        content_type=upload.content_type,
        content_length=upload.expected_size_bytes,
        sha256=upload.expected_sha256,
        expires_at=upload.expires_at,
        local_upload_token=local_token,
        local_upload_path=f"/api/v1/storage/dataset-uploads/{upload.id}",
    )
    return DatasetUploadInitResponse(
        upload_session_id=upload.id,
        dataset_id=upload.dataset_id,
        status="pending",
        method="PUT",
        upload_url=target.url,
        upload_headers=target.headers,
        expires_at=upload.expires_at,
        failure_code=upload.failure_code,
        retryable=True,
        retry_after_seconds=retry_after_seconds,
    )


async def _find_existing_upload(
    session: SessionDep,
    *,
    owner_id: str,
    idempotency_key: str,
) -> DatasetUploadSession | None:
    return cast(
        DatasetUploadSession | None,
        await session.scalar(
            select(DatasetUploadSession)
            .where(
                DatasetUploadSession.owner_id == owner_id,
                DatasetUploadSession.idempotency_key == idempotency_key,
            )
            .order_by(DatasetUploadSession.generation.desc())
            .limit(1)
        ),
    )


async def _lock_dataset_then_upload(
    session: SessionDep,
    *,
    upload_id: str,
) -> tuple[Dataset | None, DatasetUploadSession | None]:
    identity = await session.get(DatasetUploadSession, upload_id)
    if identity is None:
        return None, None
    dataset = await session.scalar(
        select(Dataset).where(Dataset.id == identity.dataset_id).with_for_update()
    )
    upload = await session.scalar(
        select(DatasetUploadSession)
        .where(DatasetUploadSession.id == upload_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if dataset is None or upload is None or upload.dataset_id != dataset.id:
        return dataset, None
    return dataset, upload


async def _recover_stale_finalizing(
    upload: DatasetUploadSession,
    dataset: Dataset,
    *,
    session: SessionDep,
    settings: SettingsDep,
) -> bool:
    if upload.status != "finalizing":
        return False
    cutoff = utc_now() - timedelta(seconds=settings.dataset_finalizing_stale_seconds)
    heartbeat = upload.finalization_heartbeat_at or upload.updated_at
    if as_utc(heartbeat) > cutoff:
        return False
    token = upload.finalization_token
    if token is None:
        return False
    recovered = await session.execute(
        update(DatasetUploadSession)
        .where(
            DatasetUploadSession.id == upload.id,
            DatasetUploadSession.generation == upload.generation,
            DatasetUploadSession.status == "finalizing",
            DatasetUploadSession.finalization_token == token,
            func.coalesce(
                DatasetUploadSession.finalization_heartbeat_at,
                DatasetUploadSession.updated_at,
            )
            <= cutoff,
        )
        .values(
            status="expired",
            upload_write_token=None,
            upload_heartbeat_at=None,
            finalization_token=None,
            finalization_heartbeat_at=None,
            failure_code="stale_finalizing_recovered",
            updated_at=utc_now(),
        )
        .execution_options(synchronize_session=False)
    )
    if recovered.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        await session.refresh(upload)
        return False
    dataset.status = "upload_pending"
    dataset.failure_code = "stale_finalizing_recovered"
    dataset.retryable = True
    await session.commit()
    upload.status = "expired"
    upload.finalization_token = None
    upload.finalization_heartbeat_at = None
    upload.failure_code = "stale_finalizing_recovered"
    return True


async def _expire_upload(
    upload: DatasetUploadSession,
    dataset: Dataset,
    *,
    session: SessionDep,
    storage: StorageAdapter,
) -> None:
    if not _upload_storage_matches(upload, storage):
        raise HTTPException(status_code=503, detail="dataset storage namespace is unavailable")
    upload.status = "expired"
    upload.upload_write_token = None
    upload.upload_heartbeat_at = None
    upload.finalization_token = None
    upload.finalization_heartbeat_at = None
    upload.failure_code = "upload_expired"
    dataset.status = "upload_pending"
    dataset.failure_code = "upload_expired"
    dataset.retryable = True
    # A PUT may have started before expiry and can publish after an inline
    # deletion. Generation-fenced maintenance performs the delayed two-phase
    # staging cleanup instead.
    await session.commit()


async def _enforce_owner_quota(
    *,
    owner_id: str,
    requested_size: int,
    session: SessionDep,
    settings: SettingsDep,
) -> None:
    await session.execute(select(User.id).where(User.id == owner_id).with_for_update())
    active_statuses = ("pending", "finalizing")
    active_sessions = (
        await session.scalar(
            select(func.count())
            .select_from(DatasetUploadSession)
            .where(
                DatasetUploadSession.owner_id == owner_id,
                DatasetUploadSession.status.in_(active_statuses),
            )
        )
        or 0
    )
    if active_sessions >= settings.dataset_owner_max_sessions:
        raise HTTPException(status_code=409, detail="dataset upload session quota exceeded")
    active_bytes = (
        await session.scalar(
            select(func.coalesce(func.sum(DatasetUploadSession.expected_size_bytes), 0)).where(
                DatasetUploadSession.owner_id == owner_id,
                DatasetUploadSession.status.in_(active_statuses),
            )
        )
        or 0
    )
    if int(active_bytes) + requested_size > settings.dataset_owner_max_bytes:
        raise HTTPException(status_code=409, detail="dataset upload byte quota exceeded")


@router.post("/datasets", response_model=DatasetRead, status_code=status.HTTP_201_CREATED)
async def legacy_import_dataset(
    payload: DatasetCreate,
    session: SessionDep,
    user: CurrentUserDep,
    settings: SettingsDep,
) -> DatasetRead:
    if settings.environment == "production" or (
        settings.environment != "test" and user.role != "admin"
    ):
        raise HTTPException(
            status_code=403,
            detail="client-supplied dataset URIs are disabled",
        )
    dataset = Dataset(
        name=payload.name,
        storage_uri=payload.storage_uri,
        flat_storage_uri=payload.flat_storage_uri,
        is_usable=payload.flat_storage_uri is not None,
        status="legacy_imported",
        created_by=user.id,
    )
    session.add(dataset)
    await session.flush()
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="dataset.legacy_imported",
        resource_type="dataset",
        resource_id=dataset.id,
    )
    await session.commit()
    await session.refresh(dataset)
    return dataset_to_read(dataset)


@router.get("/datasets", response_model=DatasetList)
async def list_datasets(
    session: SessionDep,
    user: CurrentUserDep,
    response: Response,
    dataset_status: Annotated[str | None, Query(alias="status", max_length=32)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> DatasetList:
    filters = [] if user.role == "admin" else [Dataset.created_by == user.id]
    if dataset_status is not None:
        filters.append(Dataset.status == dataset_status)
    total = await session.scalar(select(func.count()).select_from(Dataset).where(*filters)) or 0
    datasets = list(
        (
            await session.scalars(
                select(Dataset)
                .where(*filters)
                .order_by(Dataset.created_at.desc(), Dataset.id.asc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"
    return DatasetList(
        items=[dataset_to_read(item) for item in datasets],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/datasets/{dataset_id}", response_model=DatasetRead)
async def get_dataset(
    dataset_id: str,
    session: SessionDep,
    user: CurrentUserDep,
    response: Response,
) -> DatasetRead:
    dataset = await _get_owned_dataset(dataset_id, session=session, user=user)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"
    return dataset_to_read(dataset)


@router.post(
    "/datasets/uploads/init",
    response_model=DatasetUploadInitResponse,
    status_code=status.HTTP_201_CREATED,
)
async def initialize_dataset_upload(
    payload: DatasetUploadInitRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    user: CurrentUserDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> DatasetUploadInitResponse:
    response.headers["Cache-Control"] = "no-store"
    owner_id = user.id
    if payload.size_bytes > settings.dataset_upload_max_bytes:
        raise HTTPException(status_code=413, detail="dataset exceeds configured size limit")
    fingerprint = dataset_upload_request_fingerprint(payload)
    existing = await _find_existing_upload(
        session,
        owner_id=owner_id,
        idempotency_key=payload.idempotency_key,
    )
    generation = 1
    dataset: Dataset | None = None
    if existing is not None:
        dataset, locked_upload = await _lock_dataset_then_upload(
            session,
            upload_id=existing.id,
        )
        if dataset is None or locked_upload is None:
            raise HTTPException(status_code=409, detail="dataset upload lost its dataset row")
        latest = await _find_existing_upload(
            session,
            owner_id=owner_id,
            idempotency_key=payload.idempotency_key,
        )
        if latest is None or latest.id != locked_upload.id:
            await session.rollback()
            raise HTTPException(status_code=409, detail="dataset upload generation changed")
        existing = locked_upload
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="dataset idempotency key conflict")
        if not _upload_storage_matches(existing, storage):
            raise HTTPException(status_code=503, detail="dataset storage namespace is unavailable")
        if existing.status == "finalizing":
            await _recover_stale_finalizing(
                existing,
                dataset,
                session=session,
                settings=settings,
            )
        if existing.status == "pending" and as_utc(existing.expires_at) <= utc_now():
            await _expire_upload(existing, dataset, session=session, storage=storage)
        if existing.status == "expired":
            generation = existing.generation + 1
        else:
            return await _upload_init_response(
                existing,
                request=request,
                session=session,
                settings=settings,
                storage=storage,
            )

    await _enforce_owner_quota(
        owner_id=owner_id,
        requested_size=payload.size_bytes,
        session=session,
        settings=settings,
    )
    upload_id = str(uuid.uuid4())
    dataset = dataset or Dataset(
        name=payload.name,
        storage_uri="pending://server-generated",
        flat_storage_uri=None,
        status="upload_pending",
        original_filename=payload.filename,
        original_size_bytes=payload.size_bytes,
        original_sha256=payload.sha256,
        original_mime_type=payload.content_type,
        is_usable=False,
        retryable=True,
        created_by=owner_id,
    )
    session.add(dataset)
    await session.flush()
    extension = dataset_extension(payload.filename)
    keys = dataset_verified_object_keys(dataset.id, upload_id, extension)
    dataset.storage_uri = storage.storage_uri(keys["original"])
    expires_at = utc_now() + timedelta(
        seconds=dataset_upload_ttl_seconds(payload.size_bytes, settings)
    )
    local_token = None
    local_token_hash = None
    if storage.backend == "local":
        local_token = derive_dataset_upload_token(
            upload_id,
            int(expires_at.timestamp()),
            settings,
        )
        local_token_hash = upload_token_hash(local_token)
    upload = DatasetUploadSession(
        id=upload_id,
        dataset_id=dataset.id,
        owner_id=owner_id,
        idempotency_key=payload.idempotency_key,
        generation=generation,
        request_fingerprint=fingerprint,
        filename=payload.filename,
        content_type=payload.content_type,
        expected_size_bytes=payload.size_bytes,
        expected_sha256=payload.sha256,
        temporary_object_key=dataset_temporary_object_key(dataset.id, upload_id),
        original_object_key=keys["original"],
        prepared_flat_object_key=keys["prepared_flat"],
        manifest_object_key=keys["manifest"],
        quality_report_object_key=keys["quality_report"],
        storage_backend=storage.backend,
        storage_namespace_sha256=storage.namespace_fingerprint,
        status="pending",
        upload_token_hash=local_token_hash,
        expires_at=expires_at,
    )
    session.add(upload)
    try:
        await session.flush()
        target = await storage.create_upload_target(
            session_id=upload.id,
            object_key=upload.temporary_object_key,
            public_api_base_url=_public_api_base_url(request, settings),
            content_type=upload.content_type,
            content_length=upload.expected_size_bytes,
            sha256=upload.expected_sha256,
            expires_at=upload.expires_at,
            local_upload_token=local_token,
            local_upload_path=f"/api/v1/storage/dataset-uploads/{upload.id}",
        )
    except IntegrityError as exc:
        await session.rollback()
        raced = await _find_existing_upload(
            session,
            owner_id=owner_id,
            idempotency_key=payload.idempotency_key,
        )
        if raced is None or raced.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="dataset upload session conflict") from exc
        return await _upload_init_response(
            raced,
            request=request,
            session=session,
            settings=settings,
            storage=storage,
        )
    except StorageError as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="dataset upload signing failed") from exc
    add_audit_event(
        session,
        actor_type="user",
        actor_id=owner_id,
        action="dataset.upload_initialized",
        resource_type="dataset",
        resource_id=dataset.id,
    )
    await session.commit()
    return DatasetUploadInitResponse(
        upload_session_id=upload.id,
        dataset_id=dataset.id,
        status="pending",
        method="PUT",
        upload_url=target.url,
        upload_headers=target.headers,
        expires_at=upload.expires_at,
        retryable=True,
    )


async def _touch_upload_write(
    database: Database,
    *,
    upload_id: str,
    generation: int,
    write_token: str,
) -> bool:
    now = utc_now()
    async with database.session_factory() as heartbeat_session:
        touched = await heartbeat_session.execute(
            update(DatasetUploadSession)
            .where(
                DatasetUploadSession.id == upload_id,
                DatasetUploadSession.generation == generation,
                DatasetUploadSession.status == "pending",
                DatasetUploadSession.upload_write_token == write_token,
                DatasetUploadSession.expires_at > now,
            )
            .values(upload_heartbeat_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        await heartbeat_session.commit()
    return bool(touched.rowcount == 1)  # type: ignore[attr-defined]


async def _clear_upload_write_claim(
    database: Database,
    *,
    upload_id: str,
    generation: int,
    write_token: str,
) -> bool:
    async with database.session_factory() as claim_session:
        cleared = await claim_session.execute(
            update(DatasetUploadSession)
            .where(
                DatasetUploadSession.id == upload_id,
                DatasetUploadSession.generation == generation,
                DatasetUploadSession.status == "pending",
                DatasetUploadSession.upload_write_token == write_token,
            )
            .values(
                upload_write_token=None,
                upload_heartbeat_at=None,
                updated_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
        )
        await claim_session.commit()
    return bool(cleared.rowcount == 1)  # type: ignore[attr-defined]


async def _expire_upload_write_claim(
    database: Database,
    *,
    upload_id: str,
    generation: int,
    write_token: str,
) -> bool:
    async with database.session_factory() as claim_session:
        dataset, upload = await _lock_dataset_then_upload(
            claim_session,
            upload_id=upload_id,
        )
        if (
            dataset is None
            or upload is None
            or upload.generation != generation
            or upload.status != "pending"
            or upload.upload_write_token != write_token
        ):
            await claim_session.rollback()
            return False
        upload.status = "expired"
        upload.upload_write_token = None
        upload.upload_heartbeat_at = None
        upload.uploaded_at = None
        upload.failure_code = "upload_write_deadline_exceeded"
        dataset.status = "upload_pending"
        dataset.failure_code = "upload_write_deadline_exceeded"
        dataset.retryable = True
        await claim_session.commit()
        return True


async def _run_with_upload_write_heartbeat(
    operation: Callable[[], Awaitable[_T]],
    *,
    database: Database,
    upload_id: str,
    generation: int,
    write_token: str,
    heartbeat_seconds: int,
) -> _T:
    async def heartbeat() -> bool:
        return await _touch_upload_write(
            database,
            upload_id=upload_id,
            generation=generation,
            write_token=write_token,
        )

    if not await heartbeat():
        raise DatasetUploadWriteLeaseLost
    done = anyio.Event()
    results: list[_T] = []
    errors: list[BaseException] = []

    async def run_operation() -> None:
        try:
            results.append(await operation())
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    lease_lost = False
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(run_operation)
        while not done.is_set():
            with anyio.move_on_after(heartbeat_seconds):
                await done.wait()
            if done.is_set():
                break
            if not await heartbeat():
                lease_lost = True
                await done.wait()
                break
        task_group.cancel_scope.cancel()
    if not lease_lost:
        lease_lost = not await heartbeat()
    if lease_lost:
        raise DatasetUploadWriteLeaseLost
    if errors:
        raise errors[0]
    return results[0]


_DATASET_UPLOAD_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "Content-Length is not a valid integer"},
    401: {"description": "Invalid local upload token"},
    404: {"description": "Local upload endpoint or session not found"},
    408: {"description": "Upload body exceeded the session deadline"},
    409: {"description": "Upload generation or write lease conflict"},
    410: {"description": "Upload session expired before transfer began"},
    411: {"description": "Content-Length is required"},
    413: {"description": "Upload exceeds the declared session size"},
    422: {"description": "Upload metadata or body size mismatch"},
    503: {"description": "Storage namespace or upload backend unavailable"},
}


@router.put(
    "/storage/dataset-uploads/{upload_session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_DATASET_UPLOAD_ERROR_RESPONSES,
)
async def local_dataset_upload(
    upload_session_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    upload_token: Annotated[str | None, Header(alias="X-RVC-Upload-Token")] = None,
) -> Response:
    if not isinstance(storage, LocalStorageAdapter):
        raise HTTPException(status_code=404, detail="upload endpoint not found")
    database = cast(Database, request.app.state.database)
    identity = await session.get(DatasetUploadSession, upload_session_id)
    if identity is None or identity.storage_backend != "local":
        raise HTTPException(status_code=404, detail="dataset upload session not found")
    dataset, upload = await _lock_dataset_then_upload(session, upload_id=upload_session_id)
    if upload is None or upload.storage_backend != "local":
        raise HTTPException(status_code=409, detail="dataset upload session changed")
    if not _upload_storage_matches(upload, storage):
        raise HTTPException(status_code=503, detail="dataset storage namespace is unavailable")
    if dataset is None:
        raise HTTPException(status_code=409, detail="dataset upload lost its dataset row")
    if upload.status != "pending":
        raise HTTPException(status_code=409, detail="dataset upload is not writable")
    expected_temporary_key = dataset_temporary_object_key(dataset.id, upload.id)
    if upload.temporary_object_key != expected_temporary_key:
        raise HTTPException(status_code=409, detail="dataset staging key is invalid")
    if as_utc(upload.expires_at) <= utc_now():
        await _expire_upload(upload, dataset, session=session, storage=storage)
        raise HTTPException(status_code=410, detail="dataset upload expired")
    if upload_token is None or not verify_upload_token(upload_token, upload.upload_token_hash):
        raise HTTPException(status_code=401, detail="invalid dataset upload token")
    raw_length = request.headers.get("content-length")
    if raw_length is None:
        raise HTTPException(status_code=411, detail="Content-Length is required")
    try:
        content_length = int(raw_length)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
    if content_length != upload.expected_size_bytes:
        raise HTTPException(status_code=422, detail="Content-Length does not match session")
    if request.headers.get("content-type", "").lower() != upload.content_type:
        raise HTTPException(status_code=422, detail="Content-Type does not match session")

    if upload.upload_write_token is not None:
        heartbeat = upload.upload_heartbeat_at or upload.updated_at
        stale_before = utc_now() - timedelta(seconds=settings.dataset_upload_write_stale_seconds)
        if heartbeat is not None and as_utc(heartbeat) > stale_before:
            raise HTTPException(status_code=409, detail="dataset upload write is active")
        upload.status = "expired"
        upload.upload_write_token = None
        upload.upload_heartbeat_at = None
        upload.failure_code = "stale_upload_writer"
        dataset.status = "upload_pending"
        dataset.failure_code = "stale_upload_writer"
        dataset.retryable = True
        await session.commit()
        raise HTTPException(status_code=409, detail="stale dataset upload writer was fenced")

    generation = upload.generation
    write_expires_at = as_utc(upload.expires_at)
    write_token = str(uuid.uuid4())
    claimed_at = utc_now()
    claimed = await session.execute(
        update(DatasetUploadSession)
        .where(
            DatasetUploadSession.id == upload.id,
            DatasetUploadSession.generation == generation,
            DatasetUploadSession.status == "pending",
            DatasetUploadSession.upload_write_token.is_(None),
        )
        .values(
            upload_write_token=write_token,
            upload_heartbeat_at=claimed_at,
            uploaded_at=None,
            failure_code=None,
            updated_at=claimed_at,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise HTTPException(status_code=409, detail="dataset upload write conflict")
    await session.commit()

    try:
        remaining_seconds = max(0.0, (write_expires_at - utc_now()).total_seconds())
        with anyio.fail_after(remaining_seconds):
            await _run_with_upload_write_heartbeat(
                lambda: storage.write_upload_stream(
                    expected_temporary_key,
                    request.stream(),
                    expected_size=upload.expected_size_bytes,
                ),
                database=database,
                upload_id=upload.id,
                generation=generation,
                write_token=write_token,
                heartbeat_seconds=settings.dataset_upload_write_heartbeat_seconds,
            )
    except TimeoutError as exc:
        await _expire_upload_write_claim(
            database,
            upload_id=upload.id,
            generation=generation,
            write_token=write_token,
        )
        await _cleanup_published_keys(storage, (expected_temporary_key,))
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            detail="dataset upload deadline exceeded",
        ) from exc
    except DatasetUploadWriteLeaseLost as exc:
        await _cleanup_published_keys(storage, (expected_temporary_key,))
        await _clear_upload_write_claim(
            database,
            upload_id=upload.id,
            generation=generation,
            write_token=write_token,
        )
        raise HTTPException(status_code=409, detail="dataset upload write lease was lost") from exc
    except ObjectTooLarge as exc:
        await _cleanup_published_keys(storage, (expected_temporary_key,))
        await _clear_upload_write_claim(
            database,
            upload_id=upload.id,
            generation=generation,
            write_token=write_token,
        )
        raise HTTPException(status_code=413, detail="dataset upload exceeds session size") from exc
    except ObjectSizeMismatch as exc:
        await _cleanup_published_keys(storage, (expected_temporary_key,))
        await _clear_upload_write_claim(
            database,
            upload_id=upload.id,
            generation=generation,
            write_token=write_token,
        )
        raise HTTPException(status_code=422, detail="dataset upload size mismatch") from exc
    except StorageError as exc:
        await _cleanup_published_keys(storage, (expected_temporary_key,))
        await _clear_upload_write_claim(
            database,
            upload_id=upload.id,
            generation=generation,
            write_token=write_token,
        )
        raise HTTPException(status_code=503, detail="dataset upload failed") from exc

    locked_dataset, locked_upload = await _lock_dataset_then_upload(
        session,
        upload_id=upload_session_id,
    )
    if (
        locked_dataset is None
        or locked_upload is None
        or locked_upload.dataset_id != locked_dataset.id
        or locked_upload.generation != generation
        or locked_upload.status != "pending"
        or locked_upload.upload_write_token != write_token
        or not _upload_storage_matches(locked_upload, storage)
        or locked_upload.temporary_object_key != expected_temporary_key
    ):
        await session.rollback()
        await _cleanup_published_keys(storage, (expected_temporary_key,))
        await _clear_upload_write_claim(
            database,
            upload_id=upload.id,
            generation=generation,
            write_token=write_token,
        )
        raise HTTPException(status_code=409, detail="dataset upload write lease was lost")
    locked_upload.uploaded_at = utc_now()
    locked_upload.upload_write_token = None
    locked_upload.upload_heartbeat_at = None
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _set_upload_failure(
    upload: DatasetUploadSession,
    dataset: Dataset,
    *,
    failure_code: str,
    retryable: bool,
    cleanup_temporary: bool,
    generation: int,
    finalization_token: str,
    session: SessionDep,
    storage: StorageAdapter,
) -> bool:
    upload_id = upload.id
    dataset_id = dataset.id
    await session.rollback()
    locked_dataset, locked_upload = await _lock_dataset_then_upload(
        session,
        upload_id=upload_id,
    )
    if (
        locked_dataset is None
        or locked_upload is None
        or locked_dataset.id != dataset_id
        or locked_upload.generation != generation
        or locked_upload.status != "finalizing"
        or locked_upload.finalization_token != finalization_token
    ):
        await session.rollback()
        return False
    if cleanup_temporary:
        try:
            await storage.delete_object(locked_upload.temporary_object_key)
        except StorageError:
            failure_code = "staging_cleanup_failed"
            retryable = False
    locked_upload.status = "pending" if retryable else "failed"
    locked_upload.finalization_token = None
    locked_upload.finalization_heartbeat_at = None
    locked_upload.failure_code = failure_code
    locked_dataset.status = "upload_pending" if retryable else "failed"
    locked_dataset.failure_code = failure_code
    locked_dataset.retryable = retryable
    await session.commit()
    return True


async def _cleanup_published_keys(
    storage: StorageAdapter,
    keys: Sequence[str],
) -> bool:
    cleanup_ok = True
    with anyio.CancelScope(shield=True):
        for key in reversed(keys):
            try:
                await storage.delete_object(key)
            except StorageError:
                cleanup_ok = False
    return cleanup_ok


async def _recover_aborted_finalization(
    database: Database,
    request_session: SessionDep,
    *,
    upload_id: str,
    dataset_id: str,
    generation: int,
    finalization_token: str,
    published_keys: Sequence[str],
    storage: StorageAdapter,
    failure_code: str,
) -> DatasetRead | None:
    """Resolve an aborted publisher without deleting a committed snapshot."""

    with anyio.CancelScope(shield=True):
        # Release any request-session locks before checking the durable outcome
        # on a fresh connection. A commit error can be reported after the DB
        # accepted the transaction, in which case canonical objects must stay.
        await request_session.rollback()
        async with database.session_factory() as recovery_session:
            dataset, upload = await _lock_dataset_then_upload(
                recovery_session,
                upload_id=upload_id,
            )
            if (
                dataset is None
                or upload is None
                or dataset.id != dataset_id
                or upload.generation != generation
            ):
                await recovery_session.rollback()
                return None
            if upload.status == "completed":
                completed_dataset = dataset_to_read(dataset)
                await recovery_session.rollback()
                return completed_dataset
            if upload.status == "finalizing" and upload.finalization_token != finalization_token:
                # Another owner of this same immutable session is active. Do
                # not delete keys that it may already have adopted.
                await recovery_session.rollback()
                return None

            cleanup_ok = await _cleanup_published_keys(storage, published_keys)
            if upload.status == "finalizing" and upload.finalization_token == finalization_token:
                upload.status = "pending" if cleanup_ok else "failed"
                upload.finalization_token = None
                upload.finalization_heartbeat_at = None
                upload.failure_code = failure_code if cleanup_ok else "partial_cleanup_failed"
                dataset.status = "upload_pending" if cleanup_ok else "failed"
                dataset.failure_code = upload.failure_code
                dataset.retryable = cleanup_ok
            elif not cleanup_ok:
                # Preserve a durable cleanup tombstone on a terminal/fenced
                # session without reopening it for finalization.
                upload.failure_code = "partial_cleanup_failed"
            await recovery_session.commit()
            return None
    return None


async def _touch_finalization(
    database: Database,
    *,
    upload_id: str,
    generation: int,
    finalization_token: str,
) -> bool:
    now = utc_now()
    async with database.session_factory() as heartbeat_session:
        touched = await heartbeat_session.execute(
            update(DatasetUploadSession)
            .where(
                DatasetUploadSession.id == upload_id,
                DatasetUploadSession.generation == generation,
                DatasetUploadSession.status == "finalizing",
                DatasetUploadSession.finalization_token == finalization_token,
            )
            .values(finalization_heartbeat_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        await heartbeat_session.commit()
    return bool(touched.rowcount == 1)  # type: ignore[attr-defined]


async def _run_with_finalization_heartbeat(
    operation: Callable[[], Awaitable[_T]],
    *,
    database: Database,
    upload_id: str,
    generation: int,
    finalization_token: str,
    heartbeat_seconds: int,
) -> _T:
    if not await _touch_finalization(
        database,
        upload_id=upload_id,
        generation=generation,
        finalization_token=finalization_token,
    ):
        raise DatasetFinalizationLeaseLost

    done = anyio.Event()
    results: list[_T] = []
    errors: list[BaseException] = []

    async def run_operation() -> None:
        try:
            results.append(await operation())
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    lease_lost = False
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(run_operation)
        while not done.is_set():
            with anyio.move_on_after(heartbeat_seconds):
                await done.wait()
            if done.is_set():
                break
            if not await _touch_finalization(
                database,
                upload_id=upload_id,
                generation=generation,
                finalization_token=finalization_token,
            ):
                lease_lost = True
                await done.wait()
                break
        task_group.cancel_scope.cancel()

    if not lease_lost:
        lease_lost = not await _touch_finalization(
            database,
            upload_id=upload_id,
            generation=generation,
            finalization_token=finalization_token,
        )
    if lease_lost:
        raise DatasetFinalizationLeaseLost
    if errors:
        raise errors[0]
    return results[0]


@router.post("/datasets/uploads/{upload_session_id}/finalize", response_model=DatasetRead)
async def finalize_dataset_upload(
    upload_session_id: str,
    request: Request,
    session: SessionDep,
    user: CurrentUserDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> DatasetRead:
    database = cast(Database, request.app.state.database)
    identity = await session.get(DatasetUploadSession, upload_session_id)
    if identity is None:
        raise HTTPException(status_code=404, detail="dataset upload session not found")
    dataset, upload = await _lock_dataset_then_upload(
        session,
        upload_id=upload_session_id,
    )
    if dataset is None or upload is None:
        raise HTTPException(status_code=409, detail="dataset upload lost its dataset row")
    _require_dataset_owner_or_admin(dataset, user)
    if not _upload_storage_matches(upload, storage):
        raise HTTPException(status_code=503, detail="dataset storage namespace is unavailable")
    if upload.status == "completed":
        return dataset_to_read(dataset)
    if upload.status in {"failed", "expired"}:
        raise HTTPException(status_code=409, detail=f"dataset upload is {upload.status}")
    expected_temporary_key = dataset_temporary_object_key(dataset.id, upload.id)
    expected_keys = dataset_verified_object_keys(
        dataset.id,
        upload.id,
        dataset_extension(upload.filename),
    )
    if (
        upload.temporary_object_key != expected_temporary_key
        or upload.original_object_key != expected_keys["original"]
        or upload.prepared_flat_object_key != expected_keys["prepared_flat"]
        or upload.manifest_object_key != expected_keys["manifest"]
        or upload.quality_report_object_key != expected_keys["quality_report"]
    ):
        raise HTTPException(status_code=409, detail="dataset upload object key is invalid")
    if upload.status == "finalizing":
        recovered = await _recover_stale_finalizing(
            upload,
            dataset,
            session=session,
            settings=settings,
        )
        if not recovered:
            raise HTTPException(status_code=409, detail="dataset upload is already finalizing")
        raise HTTPException(
            status_code=409,
            detail="stale dataset finalization was fenced; initialize a new generation",
        )
    if as_utc(upload.expires_at) <= utc_now():
        await _expire_upload(upload, dataset, session=session, storage=storage)
        raise HTTPException(status_code=409, detail="dataset upload expired")
    if upload.upload_write_token is not None:
        heartbeat = upload.upload_heartbeat_at or upload.updated_at
        stale_before = utc_now() - timedelta(seconds=settings.dataset_upload_write_stale_seconds)
        if heartbeat is not None and as_utc(heartbeat) > stale_before:
            raise HTTPException(status_code=409, detail="dataset upload write is active")
        upload.status = "expired"
        upload.upload_write_token = None
        upload.upload_heartbeat_at = None
        upload.failure_code = "stale_upload_writer"
        dataset.status = "upload_pending"
        dataset.failure_code = "stale_upload_writer"
        dataset.retryable = True
        await session.commit()
        raise HTTPException(status_code=409, detail="stale dataset upload writer was fenced")
    generation = upload.generation
    finalization_token = str(uuid.uuid4())
    claimed_at = utc_now()
    claimed = await session.execute(
        update(DatasetUploadSession)
        .where(
            DatasetUploadSession.id == upload.id,
            DatasetUploadSession.generation == generation,
            DatasetUploadSession.status == "pending",
            DatasetUploadSession.upload_write_token.is_(None),
        )
        .values(
            status="finalizing",
            finalization_token=finalization_token,
            finalization_heartbeat_at=claimed_at,
            failure_code=None,
            updated_at=claimed_at,
        )
    )
    if claimed.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise HTTPException(status_code=409, detail="dataset finalization conflict")
    dataset.status = "processing"
    dataset.failure_code = None
    dataset.retryable = True
    dataset.ingestion_started_at = utc_now()
    await session.commit()
    upload.status = "finalizing"
    upload.finalization_token = finalization_token

    spool_path: Path | None = None
    snapshot: PreparedDatasetSnapshot | None = None
    published_keys: list[str] = []

    async def publish_canonical(
        object_key: str,
        source: Path,
        content_type: str,
        sha256: str,
    ) -> None:
        await storage.store_verified_file(
            object_key,
            source,
            content_type=content_type,
            sha256=sha256,
        )
        # Record publication inside the lease-wrapped operation. If the final
        # heartbeat detects a lost token after the PUT succeeds, cleanup still
        # has exact ownership evidence for this upload-scoped canonical key.
        published_keys.append(object_key)

    try:
        verified_spool_path = await _run_with_finalization_heartbeat(
            lambda: verify_object_to_spool(
                storage,
                upload.temporary_object_key,
                expected_size=upload.expected_size_bytes,
                expected_sha256=upload.expected_sha256,
                settings=settings,
            ),
            database=database,
            upload_id=upload.id,
            generation=generation,
            finalization_token=finalization_token,
            heartbeat_seconds=settings.dataset_finalizing_heartbeat_seconds,
        )
        spool_path = verified_spool_path
        prepared_snapshot = await _run_with_finalization_heartbeat(
            lambda: anyio.to_thread.run_sync(
                partial(
                    prepare_dataset_snapshot,
                    verified_spool_path,
                    extension=dataset_extension(upload.filename),
                    settings=settings,
                )
            ),
            database=database,
            upload_id=upload.id,
            generation=generation,
            finalization_token=finalization_token,
            heartbeat_seconds=settings.dataset_finalizing_heartbeat_seconds,
        )
        snapshot = prepared_snapshot
        publications = (
            (
                upload.original_object_key,
                verified_spool_path,
                upload.content_type,
                upload.expected_sha256,
            ),
            (
                upload.prepared_flat_object_key,
                prepared_snapshot.prepared_flat_archive,
                "application/zip",
                prepared_snapshot.prepared_flat_sha256,
            ),
            (
                upload.manifest_object_key,
                prepared_snapshot.ingestion.manifest_path,
                "application/json",
                prepared_snapshot.manifest_sha256,
            ),
            (
                upload.quality_report_object_key,
                prepared_snapshot.ingestion.quality_report_path,
                "application/json",
                prepared_snapshot.quality_report_sha256,
            ),
        )
        for object_key, source, content_type, sha256 in publications:
            await _run_with_finalization_heartbeat(
                partial(
                    publish_canonical,
                    object_key,
                    source,
                    content_type,
                    sha256,
                ),
                database=database,
                upload_id=upload.id,
                generation=generation,
                finalization_token=finalization_token,
                heartbeat_seconds=settings.dataset_finalizing_heartbeat_seconds,
            )
    except anyio.get_cancelled_exc_class():
        await _recover_aborted_finalization(
            database,
            session,
            upload_id=upload.id,
            dataset_id=dataset.id,
            generation=generation,
            finalization_token=finalization_token,
            published_keys=published_keys,
            storage=storage,
            failure_code="finalization_cancelled",
        )
        raise
    except DatasetFinalizationLeaseLost as exc:
        await _cleanup_published_keys(storage, published_keys)
        raise HTTPException(
            status_code=409,
            detail="dataset finalization lease was lost",
        ) from exc
    except ObjectNotFound as exc:
        transitioned = await _set_upload_failure(
            upload,
            dataset,
            failure_code="uploaded_object_not_found",
            retryable=True,
            cleanup_temporary=False,
            generation=generation,
            finalization_token=finalization_token,
            session=session,
            storage=storage,
        )
        if not transitioned:
            await _cleanup_published_keys(storage, published_keys)
            raise HTTPException(
                status_code=409,
                detail="dataset finalization lease was lost",
            ) from exc
        raise HTTPException(status_code=409, detail="uploaded dataset object not found") from exc
    except (ArtifactVerificationMismatch, ObjectTooLarge) as exc:
        failure_code = (
            exc.failure_code if isinstance(exc, ArtifactVerificationMismatch) else "size_mismatch"
        )
        transitioned = await _set_upload_failure(
            upload,
            dataset,
            failure_code=failure_code,
            retryable=False,
            cleanup_temporary=True,
            generation=generation,
            finalization_token=finalization_token,
            session=session,
            storage=storage,
        )
        if not transitioned:
            await _cleanup_published_keys(storage, published_keys)
            raise HTTPException(
                status_code=409,
                detail="dataset finalization lease was lost",
            ) from exc
        raise HTTPException(status_code=422, detail="dataset size or SHA-256 mismatch") from exc
    except ArtifactSpoolError as exc:
        transitioned = await _set_upload_failure(
            upload,
            dataset,
            failure_code=exc.failure_code,
            retryable=True,
            cleanup_temporary=False,
            generation=generation,
            finalization_token=finalization_token,
            session=session,
            storage=storage,
        )
        if not transitioned:
            await _cleanup_published_keys(storage, published_keys)
            raise HTTPException(
                status_code=409,
                detail="dataset finalization lease was lost",
            ) from exc
        raise HTTPException(
            status_code=503,
            detail="dataset verification spool unavailable",
        ) from exc
    except DatasetPreparationError as exc:
        transitioned = await _set_upload_failure(
            upload,
            dataset,
            failure_code=exc.failure_code,
            retryable=exc.retryable,
            cleanup_temporary=not exc.retryable,
            generation=generation,
            finalization_token=finalization_token,
            session=session,
            storage=storage,
        )
        if not transitioned:
            await _cleanup_published_keys(storage, published_keys)
            raise HTTPException(
                status_code=409,
                detail="dataset finalization lease was lost",
            ) from exc
        http_status = 503 if exc.retryable else 422
        raise HTTPException(status_code=http_status, detail="dataset preparation failed") from exc
    except StorageError as exc:
        cleanup_ok = await _cleanup_published_keys(storage, published_keys)
        transitioned = await _set_upload_failure(
            upload,
            dataset,
            failure_code="dataset_publish_failed" if cleanup_ok else "partial_cleanup_failed",
            retryable=cleanup_ok,
            cleanup_temporary=False,
            generation=generation,
            finalization_token=finalization_token,
            session=session,
            storage=storage,
        )
        if not transitioned:
            raise HTTPException(
                status_code=409,
                detail="dataset finalization lease was lost",
            ) from exc
        raise HTTPException(status_code=503, detail="dataset object publish failed") from exc
    except Exception as exc:
        recovered_dataset = await _recover_aborted_finalization(
            database,
            session,
            upload_id=upload.id,
            dataset_id=dataset.id,
            generation=generation,
            finalization_token=finalization_token,
            published_keys=published_keys,
            storage=storage,
            failure_code="dataset_finalization_aborted",
        )
        if recovered_dataset is not None:
            return recovered_dataset
        raise HTTPException(status_code=503, detail="dataset finalization aborted") from exc
    finally:
        with anyio.CancelScope(shield=True):
            if snapshot is not None:
                try:
                    await anyio.to_thread.run_sync(cleanup_dataset_snapshot, snapshot)
                except DatasetPreparationError:
                    pass
            if spool_path is not None:
                try:
                    await remove_spool_file(spool_path)
                except ArtifactSpoolError:
                    pass

    assert snapshot is not None
    locked_dataset, locked_upload = await _lock_dataset_then_upload(
        session,
        upload_id=upload.id,
    )
    if (
        locked_dataset is None
        or locked_upload is None
        or locked_upload.status != "finalizing"
        or locked_upload.generation != generation
        or locked_upload.finalization_token != finalization_token
    ):
        await session.rollback()
        await _cleanup_published_keys(storage, published_keys)
        raise HTTPException(status_code=409, detail="dataset finalization lease was lost")
    dataset = locked_dataset
    finalized_upload = locked_upload
    durable_dataset_id = dataset.id
    durable_upload_id = finalized_upload.id
    manifest = snapshot.ingestion.manifest
    report = snapshot.ingestion.quality_report
    decoder_pending = report.decoder_pending_count
    dataset_status = "decoder_pending" if decoder_pending else "ready"
    validated_sample_rates = {
        entry.inspection.sample_rate_hz
        for entry in manifest.files
        if entry.inspection.status == "validated_pcm"
        and entry.inspection.sample_rate_hz is not None
    }
    validated_wav_count = sum(
        entry.inspection.status == "validated_pcm" for entry in manifest.files
    )
    dataset.flat_storage_uri = storage.storage_uri(finalized_upload.prepared_flat_object_key)
    dataset.manifest_storage_uri = storage.storage_uri(finalized_upload.manifest_object_key)
    dataset.quality_report_storage_uri = storage.storage_uri(
        finalized_upload.quality_report_object_key
    )
    dataset.prepared_flat_size_bytes = snapshot.prepared_flat_size_bytes
    dataset.prepared_flat_sha256 = snapshot.prepared_flat_sha256
    dataset.manifest_sha256 = snapshot.manifest_sha256
    dataset.quality_report_sha256 = snapshot.quality_report_sha256
    dataset.duration_sec = manifest.validated_wav_duration_seconds if validated_wav_count else None
    dataset.file_count = manifest.file_count
    dataset.sample_rate = (
        next(iter(validated_sample_rates)) if len(validated_sample_rates) == 1 else None
    )
    dataset.decoder_pending_count = decoder_pending
    dataset.source_file_entry_count = report.source_file_entries
    dataset.skipped_file_count = len(report.skipped)
    dataset.rejected_file_count = len(report.rejected)
    dataset.duplicate_file_count = len(report.duplicates)
    if report.pcm_quality is not None:
        dataset.pcm_quality_algorithm = report.pcm_quality.algorithm
        dataset.pcm_validated_file_count = report.pcm_quality.validated_file_count
        dataset.pcm_sample_count = report.pcm_quality.sample_count
        dataset.pcm_clipping_ratio = report.pcm_quality.clipping_ratio
        dataset.pcm_silence_ratio = report.pcm_quality.silence_ratio
        dataset.pcm_rms_ratio = report.pcm_quality.rms_ratio
        dataset.pcm_silence_threshold_dbfs = report.pcm_quality.silence_threshold_dbfs
        loudness = report.pcm_quality.loudness
        dataset.pcm_loudness_algorithm = loudness.algorithm
        dataset.pcm_loudness_analyzed_file_count = loudness.analyzed_file_count
        dataset.pcm_loudness_block_count = loudness.block_count
        dataset.pcm_loudness_gated_block_count = loudness.gated_block_count
        dataset.pcm_integrated_lufs = loudness.integrated_lufs
        dataset.pcm_loudness_unavailable_reason = loudness.unavailable_reason
    else:
        dataset.pcm_quality_algorithm = None
        dataset.pcm_validated_file_count = None
        dataset.pcm_sample_count = None
        dataset.pcm_clipping_ratio = None
        dataset.pcm_silence_ratio = None
        dataset.pcm_rms_ratio = None
        dataset.pcm_silence_threshold_dbfs = None
        dataset.pcm_loudness_algorithm = None
        dataset.pcm_loudness_analyzed_file_count = None
        dataset.pcm_loudness_block_count = None
        dataset.pcm_loudness_gated_block_count = None
        dataset.pcm_integrated_lufs = None
        dataset.pcm_loudness_unavailable_reason = None
    dataset.quality_report_json = {**report.to_dict(), "status": dataset_status}
    dataset.is_usable = decoder_pending == 0
    dataset.status = dataset_status
    dataset.failure_code = None
    dataset.retryable = False
    dataset.finalized_at = utc_now()
    finalized_upload.status = "completed"
    finalized_upload.finalization_token = None
    finalized_upload.finalization_heartbeat_at = None
    finalized_upload.failure_code = None
    finalized_upload.finalized_at = utc_now()
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="dataset.finalized",
        resource_type="dataset",
        resource_id=dataset.id,
        details={"status": dataset_status, "decoder_pending_count": decoder_pending},
    )
    try:
        await session.commit()
    except anyio.get_cancelled_exc_class():
        recovered_dataset = await _recover_aborted_finalization(
            database,
            session,
            upload_id=durable_upload_id,
            dataset_id=durable_dataset_id,
            generation=generation,
            finalization_token=finalization_token,
            published_keys=published_keys,
            storage=storage,
            failure_code="dataset_finalize_commit_cancelled",
        )
        if recovered_dataset is not None:
            with anyio.CancelScope(shield=True):
                await _cleanup_published_keys(storage, (expected_temporary_key,))
        raise
    except Exception as exc:
        recovered_dataset = await _recover_aborted_finalization(
            database,
            session,
            upload_id=durable_upload_id,
            dataset_id=durable_dataset_id,
            generation=generation,
            finalization_token=finalization_token,
            published_keys=published_keys,
            storage=storage,
            failure_code="dataset_finalize_commit_failed",
        )
        if recovered_dataset is None:
            raise HTTPException(
                status_code=503,
                detail="dataset finalization commit failed",
            ) from exc
        with anyio.CancelScope(shield=True):
            await _cleanup_published_keys(storage, (expected_temporary_key,))
        return recovered_dataset
    with anyio.CancelScope(shield=True):
        try:
            await storage.delete_object(finalized_upload.temporary_object_key)
        except StorageError:
            finalized_upload.failure_code = "staging_cleanup_pending"
            await session.commit()
    await session.refresh(dataset)
    return dataset_to_read(dataset)


def _prepared_or_conflict(dataset: Dataset) -> DatasetRead:
    if dataset.status in {"ready", "decoder_pending", "legacy_imported"}:
        return dataset_to_read(dataset)
    raise HTTPException(
        status_code=409,
        detail=f"dataset cannot be prepared from status {dataset.status}",
    )


@router.post("/datasets/{dataset_id}/validate", response_model=DatasetRead)
async def validate_dataset(
    dataset_id: str,
    session: SessionDep,
    user: CurrentUserDep,
) -> DatasetRead:
    dataset = await _get_owned_dataset(dataset_id, session=session, user=user)
    return _prepared_or_conflict(dataset)


@router.post("/datasets/{dataset_id}/prepare-flat", response_model=DatasetRead)
async def prepare_flat_dataset(
    dataset_id: str,
    session: SessionDep,
    user: CurrentUserDep,
) -> DatasetRead:
    dataset = await _get_owned_dataset(dataset_id, session=session, user=user)
    return _prepared_or_conflict(dataset)


@router.delete("/datasets/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dataset(
    dataset_id: str,
    session: SessionDep,
    user: CurrentUserDep,
    storage: StorageDep,
) -> Response:
    dataset = await session.scalar(
        select(Dataset).where(Dataset.id == dataset_id).with_for_update()
    )
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset not found")
    _require_dataset_owner_or_admin(dataset, user)
    experiment_exists = await session.scalar(
        select(Experiment.id).where(Experiment.dataset_id == dataset.id).limit(1)
    )
    job_exists = await session.scalar(select(Job.id).where(Job.dataset_id == dataset.id).limit(1))
    if experiment_exists is not None or job_exists is not None:
        raise HTTPException(status_code=409, detail="referenced dataset cannot be deleted")
    uploads = list(
        (
            await session.scalars(
                select(DatasetUploadSession)
                .where(DatasetUploadSession.dataset_id == dataset.id)
                .order_by(DatasetUploadSession.created_at.desc())
                .with_for_update()
            )
        ).all()
    )
    active_upload = next(
        (
            upload
            for upload in uploads
            if upload.upload_write_token is not None
            or upload.status == "finalizing"
            or (upload.status == "pending" and as_utc(upload.expires_at) > utc_now())
        ),
        None,
    )
    if active_upload is not None:
        raise HTTPException(status_code=409, detail="active dataset upload cannot be deleted")
    cleanup_pending = next(
        (
            upload
            for upload in uploads
            if upload.status in {"pending", "expired", "failed"}
            and upload.cleanup_completed_at is None
        ),
        None,
    )
    if cleanup_pending is not None:
        raise HTTPException(status_code=409, detail="dataset staging cleanup is pending")
    if any(not _upload_storage_matches(upload, storage) for upload in uploads):
        raise HTTPException(status_code=503, detail="dataset storage namespace is unavailable")
    dataset.status = "deleting"
    dataset.retryable = True
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="dataset.delete_started",
        resource_type="dataset",
        resource_id=dataset.id,
    )
    await session.commit()
    if uploads:
        keys = tuple(
            {
                key
                for upload in uploads
                for key in (
                    upload.temporary_object_key,
                    upload.original_object_key,
                    upload.prepared_flat_object_key,
                    upload.manifest_object_key,
                    upload.quality_report_object_key,
                )
            }
        )
        if not await _cleanup_published_keys(storage, keys):
            dataset.status = "delete_failed"
            dataset.failure_code = "object_cleanup_failed"
            dataset.retryable = True
            await session.commit()
            raise HTTPException(status_code=503, detail="dataset object cleanup failed")
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="dataset.deleted",
        resource_type="dataset",
        resource_id=dataset.id,
        details={"legacy_external_objects_preserved": not uploads},
    )
    await session.delete(dataset)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
