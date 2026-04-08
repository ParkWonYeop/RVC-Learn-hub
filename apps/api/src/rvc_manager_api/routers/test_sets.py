from __future__ import annotations

import hashlib
import math
import os
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Annotated, Any, TypeVar, cast

import anyio
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from rvc_orchestrator_contracts import utc_now

from ..audit import add_audit_event
from ..database import Database
from ..dependencies import CurrentUserDep, SessionDep, SettingsDep
from ..models import (
    Job,
    Preset,
    Sample,
    TestSet,
    TestSetItem,
    TestSetItemUploadSession,
    User,
    new_id,
)
from ..schemas import (
    PresetCreate,
    PresetList,
    PresetRead,
    PresetRevisionCreate,
    TestSetCreate,
    TestSetItemRead,
    TestSetItemUploadInitRequest,
    TestSetItemUploadInitResponse,
    TestSetList,
    TestSetRead,
    TestSetRevisionCreate,
)
from ..services.artifacts import (
    ArtifactSpoolError,
    ArtifactVerificationMismatch,
    remove_spool_file,
    upload_token_hash,
    verify_object_to_spool,
    verify_upload_token,
)
from ..services.test_sets import (
    InvalidTestSetWav,
    build_test_set_manifest_document,
    canonical_json,
    canonical_sha256,
    derive_test_set_upload_token,
    inspect_pcm_wav,
    preset_document,
    preset_to_read,
    test_set_item_object_key,
    test_set_manifest_object_key,
    test_set_temporary_object_key,
    test_set_to_read,
    test_set_upload_request_fingerprint,
)
from ..services.workers import as_utc
from ..storage import (
    LocalStorageAdapter,
    ObjectNotFound,
    ObjectSizeMismatch,
    ObjectTooLarge,
    StorageAdapter,
    StorageError,
)

router = APIRouter(tags=["test-sets"])
_T = TypeVar("_T")


class TestSetUploadWriteLeaseLost(RuntimeError):
    pass


class TestSetFinalizationLeaseLost(RuntimeError):
    pass


def get_storage(request: Request) -> StorageAdapter:
    return cast(StorageAdapter, request.app.state.storage)


StorageDep = Annotated[StorageAdapter, Depends(get_storage)]


def _upload_storage_matches(
    upload: TestSetItemUploadSession,
    storage: StorageAdapter,
) -> bool:
    return (
        upload.storage_backend == storage.backend
        and upload.storage_namespace_sha256 == storage.namespace_fingerprint
    )


def _private_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"


def _require_owner_or_admin(owner_id: str | None, user: User, resource: str) -> None:
    if user.role != "admin" and owner_id != user.id:
        raise HTTPException(status_code=404, detail=f"{resource} not found")


async def _owned_test_set(test_set_id: str, session: SessionDep, user: User) -> TestSet:
    test_set = await session.get(TestSet, test_set_id)
    if test_set is None:
        raise HTTPException(status_code=404, detail="test set not found")
    _require_owner_or_admin(test_set.created_by, user, "test set")
    return test_set


async def _owned_preset(preset_id: str, session: SessionDep, user: User) -> Preset:
    preset = await session.get(Preset, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="preset not found")
    _require_owner_or_admin(preset.created_by, user, "preset")
    return preset


async def _items_for_test_set(session: SessionDep, test_set_id: str) -> list[TestSetItem]:
    return list(
        (
            await session.scalars(
                select(TestSetItem)
                .where(TestSetItem.test_set_id == test_set_id)
                .order_by(TestSetItem.sort_order.asc(), TestSetItem.item_key.asc())
            )
        ).all()
    )


def _public_api_base_url(request: Request, settings: SettingsDep) -> str:
    return settings.public_api_base_url or str(request.base_url).rstrip("/")


async def _locked_owned_test_set(
    test_set_id: str,
    session: SessionDep,
    user: User,
) -> TestSet:
    test_set = await session.scalar(
        select(TestSet).where(TestSet.id == test_set_id).with_for_update()
    )
    if test_set is None:
        raise HTTPException(status_code=404, detail="test set not found")
    _require_owner_or_admin(test_set.created_by, user, "test set")
    return test_set


async def _find_upload_session(
    session: SessionDep,
    *,
    test_set_id: str,
    owner_id: str,
    idempotency_key: str,
    for_update: bool = False,
) -> TestSetItemUploadSession | None:
    statement = (
        select(TestSetItemUploadSession)
        .where(
            TestSetItemUploadSession.test_set_id == test_set_id,
            TestSetItemUploadSession.owner_id == owner_id,
            TestSetItemUploadSession.idempotency_key == idempotency_key,
        )
        .order_by(TestSetItemUploadSession.generation.desc())
        .limit(1)
    )
    if for_update:
        statement = statement.with_for_update()
    return cast(
        TestSetItemUploadSession | None,
        await session.scalar(statement),
    )


async def _upload_response(
    upload: TestSetItemUploadSession,
    *,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageAdapter,
) -> TestSetItemUploadInitResponse:
    if upload.status == "completed":
        item = await session.scalar(
            select(TestSetItem).where(
                TestSetItem.test_set_id == upload.test_set_id,
                TestSetItem.item_key == upload.item_key,
            )
        )
        return TestSetItemUploadInitResponse(
            upload_session_id=upload.id,
            test_set_id=upload.test_set_id,
            status="completed",
            expires_at=upload.expires_at,
            item=TestSetItemRead.model_validate(item) if item is not None else None,
            failure_code=upload.failure_code,
        )
    if upload.status != "pending":
        return TestSetItemUploadInitResponse(
            upload_session_id=upload.id,
            test_set_id=upload.test_set_id,
            status=upload.status,  # type: ignore[arg-type]
            expires_at=upload.expires_at,
            failure_code=upload.failure_code,
        )
    if not _upload_storage_matches(upload, storage):
        raise HTTPException(status_code=503, detail="test set storage backend is unavailable")
    local_token = None
    if storage.backend == "local":
        local_token = derive_test_set_upload_token(
            upload.id,
            int(as_utc(upload.expires_at).timestamp()),
            settings,
        )
        if upload_token_hash(local_token) != upload.upload_token_hash:
            raise HTTPException(status_code=409, detail="test set upload token state is invalid")
    try:
        target = await storage.create_upload_target(
            session_id=upload.id,
            object_key=upload.temporary_object_key,
            public_api_base_url=_public_api_base_url(request, settings),
            content_type=upload.content_type,
            content_length=upload.expected_size_bytes,
            sha256=upload.expected_sha256,
            expires_at=upload.expires_at,
            local_upload_token=local_token,
            local_upload_path=f"/api/v1/storage/test-set-item-uploads/{upload.id}",
        )
    except StorageError as exc:
        raise HTTPException(status_code=503, detail="test set upload signing failed") from exc
    return TestSetItemUploadInitResponse(
        upload_session_id=upload.id,
        test_set_id=upload.test_set_id,
        status="pending",
        method="PUT",
        upload_url=target.url,
        upload_headers=target.headers,
        expires_at=upload.expires_at,
        failure_code=upload.failure_code,
    )


async def _expire_upload(
    upload: TestSetItemUploadSession,
    *,
    session: SessionDep,
    storage: StorageAdapter,
) -> None:
    if not _upload_storage_matches(upload, storage):
        raise HTTPException(
            status_code=503,
            detail="test set upload storage backend is unavailable",
        )
    upload.status = "expired"
    upload.upload_write_token = None
    upload.upload_heartbeat_at = None
    upload.finalization_token = None
    upload.finalization_heartbeat_at = None
    upload.failure_code = "upload_expired"
    # Staging deletion is deliberately deferred to the generation-fenced
    # maintenance task. A local request or presigned S3 PUT may have started
    # before expiry and could otherwise publish after this inline deletion.
    await session.flush()


async def _enforce_upload_quota(
    *,
    owner_id: str,
    test_set_id: str,
    requested_size: int,
    session: SessionDep,
    settings: SettingsDep,
) -> None:
    active = ("pending", "finalizing")
    owner_sessions = (
        await session.scalar(
            select(func.count())
            .select_from(TestSetItemUploadSession)
            .where(
                TestSetItemUploadSession.owner_id == owner_id,
                TestSetItemUploadSession.status.in_(active),
            )
        )
        or 0
    )
    if owner_sessions >= settings.test_set_owner_max_sessions:
        raise HTTPException(status_code=429, detail="test set upload session quota exceeded")
    owner_bytes = (
        await session.scalar(
            select(func.coalesce(func.sum(TestSetItemUploadSession.expected_size_bytes), 0)).where(
                TestSetItemUploadSession.owner_id == owner_id,
                TestSetItemUploadSession.status.in_(active),
            )
        )
        or 0
    )
    if int(owner_bytes) + requested_size > settings.test_set_owner_max_bytes:
        raise HTTPException(status_code=413, detail="test set upload byte quota exceeded")
    item_count = (
        await session.scalar(
            select(func.count())
            .select_from(TestSetItem)
            .where(TestSetItem.test_set_id == test_set_id)
        )
        or 0
    )
    reserved_count = (
        await session.scalar(
            select(func.count())
            .select_from(TestSetItemUploadSession)
            .where(
                TestSetItemUploadSession.test_set_id == test_set_id,
                TestSetItemUploadSession.status.in_(active),
            )
        )
        or 0
    )
    if item_count + reserved_count >= settings.test_set_max_items:
        raise HTTPException(status_code=409, detail="test set item limit reached")


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
            update(TestSetItemUploadSession)
            .where(
                TestSetItemUploadSession.id == upload_id,
                TestSetItemUploadSession.generation == generation,
                TestSetItemUploadSession.status == "pending",
                TestSetItemUploadSession.upload_write_token == write_token,
                TestSetItemUploadSession.expires_at > now,
            )
            .values(upload_heartbeat_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        await heartbeat_session.commit()
    return bool(touched.rowcount == 1)  # type: ignore[attr-defined]


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
            update(TestSetItemUploadSession)
            .where(
                TestSetItemUploadSession.id == upload_id,
                TestSetItemUploadSession.generation == generation,
                TestSetItemUploadSession.status == "finalizing",
                TestSetItemUploadSession.finalization_token == finalization_token,
            )
            .values(finalization_heartbeat_at=now, updated_at=now)
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
            update(TestSetItemUploadSession)
            .where(
                TestSetItemUploadSession.id == upload_id,
                TestSetItemUploadSession.generation == generation,
                TestSetItemUploadSession.status == "pending",
                TestSetItemUploadSession.upload_write_token == write_token,
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
        expired = await claim_session.execute(
            update(TestSetItemUploadSession)
            .where(
                TestSetItemUploadSession.id == upload_id,
                TestSetItemUploadSession.generation == generation,
                TestSetItemUploadSession.status == "pending",
                TestSetItemUploadSession.upload_write_token == write_token,
            )
            .values(
                status="expired",
                upload_write_token=None,
                upload_heartbeat_at=None,
                uploaded_at=None,
                failure_code="upload_write_deadline_exceeded",
                updated_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
        )
        await claim_session.commit()
    return bool(expired.rowcount == 1)  # type: ignore[attr-defined]


async def _run_with_lease_heartbeat(
    operation: Callable[[], Awaitable[_T]],
    *,
    heartbeat: Callable[[], Awaitable[bool]],
    heartbeat_seconds: int,
    lease_lost_error: type[RuntimeError],
) -> _T:
    if not await heartbeat():
        raise lease_lost_error

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
                # Let the storage operation unwind before returning. If it
                # publishes after losing the CAS token, the caller removes the
                # exact generation key before reporting the conflict.
                lease_lost = True
                await done.wait()
                break
        task_group.cancel_scope.cancel()

    if not lease_lost:
        lease_lost = not await heartbeat()
    if lease_lost:
        raise lease_lost_error
    if errors:
        raise errors[0]
    return results[0]


async def _mark_finalizing_upload_failed(
    upload_id: str,
    finalization_token: str,
    failure_code: str,
    *,
    session: SessionDep,
    generation: int | None = None,
) -> bool:
    predicates = [
        TestSetItemUploadSession.id == upload_id,
        TestSetItemUploadSession.status == "finalizing",
        TestSetItemUploadSession.finalization_token == finalization_token,
    ]
    if generation is not None:
        predicates.append(TestSetItemUploadSession.generation == generation)
    transitioned = cast(
        CursorResult[Any],
        await session.execute(
            update(TestSetItemUploadSession)
            .where(*predicates)
            .values(
                status="failed",
                finalization_token=None,
                finalization_heartbeat_at=None,
                failure_code=failure_code,
                updated_at=utc_now(),
            )
        ),
    )
    await session.commit()
    return transitioned.rowcount == 1


async def _delete_best_effort(storage: StorageAdapter, object_key: str) -> bool:
    try:
        await storage.delete_object(object_key)
    except StorageError:
        return False
    return True


async def _canonical_object_matches_item(
    storage: StorageAdapter,
    object_key: str,
    item: TestSetItem,
    *,
    chunk_size: int,
) -> bool:
    digest = hashlib.sha256()
    total = 0
    async for chunk in storage.stream_object(
        object_key,
        chunk_size=chunk_size,
        max_bytes=item.size_bytes + 1,
    ):
        total += len(chunk)
        digest.update(chunk)
    return total == item.size_bytes and digest.hexdigest() == item.sha256


async def _write_manifest_spool(document: dict[str, object]) -> tuple[Path, str]:
    payload = canonical_json(document)
    descriptor, raw_path = await anyio.to_thread.run_sync(
        lambda: tempfile.mkstemp(prefix="rvc-test-set-manifest-")
    )
    path = Path(raw_path)

    def write_and_sync() -> None:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())

    try:
        await anyio.to_thread.run_sync(write_and_sync)
    except OSError:
        await anyio.to_thread.run_sync(path.unlink, True)
        raise
    return path, hashlib.sha256(payload).hexdigest()


@router.post("/test-sets", response_model=TestSetRead, status_code=status.HTTP_201_CREATED)
async def create_test_set(
    payload: TestSetCreate,
    response: Response,
    session: SessionDep,
    user: CurrentUserDep,
) -> TestSetRead:
    test_set = TestSet(
        family_id=new_id(),
        name=payload.name,
        revision=1,
        description=payload.description,
        status="draft",
        item_count=0,
        created_by=user.id,
    )
    session.add(test_set)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="test set name already has an initial revision",
        ) from exc
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="test_set.created",
        resource_type="test_set",
        resource_id=test_set.id,
        details={"family_id": test_set.family_id, "revision": 1},
    )
    await session.commit()
    await session.refresh(test_set)
    _private_no_store(response)
    return test_set_to_read(test_set, [])


@router.post(
    "/test-sets/{test_set_id}/item-uploads/init",
    response_model=TestSetItemUploadInitResponse,
    status_code=status.HTTP_201_CREATED,
)
async def initialize_test_set_item_upload(
    test_set_id: str,
    payload: TestSetItemUploadInitRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    user: CurrentUserDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> TestSetItemUploadInitResponse:
    response.headers["Cache-Control"] = "no-store"
    if payload.size_bytes > settings.test_set_item_max_bytes:
        raise HTTPException(status_code=413, detail="test set item exceeds size limit")

    locked_user = await session.scalar(select(User).where(User.id == user.id).with_for_update())
    if locked_user is None or locked_user.disabled:
        raise HTTPException(status_code=401, detail="user is unavailable")
    test_set = await _locked_owned_test_set(test_set_id, session, user)
    if test_set.status != "draft":
        raise HTTPException(status_code=409, detail="test set revision is immutable")

    fingerprint = test_set_upload_request_fingerprint(payload)
    existing = await _find_upload_session(
        session,
        test_set_id=test_set.id,
        owner_id=user.id,
        idempotency_key=payload.idempotency_key,
        for_update=True,
    )
    generation = 1
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="test set idempotency key conflict")
        if existing.status == "pending" and as_utc(existing.expires_at) <= utc_now():
            await _expire_upload(existing, session=session, storage=storage)
        if existing.status == "failed":
            if not _upload_storage_matches(existing, storage):
                raise HTTPException(
                    status_code=503,
                    detail="failed test set upload backend is unavailable",
                )
            # A retry always receives a new upload id and therefore new staging
            # and canonical keys. Inline deletion can race a late PUT/finalizer,
            # and canonical bytes are never deleted without publication-token
            # ownership evidence.
            existing.status = "expired"
            existing.upload_write_token = None
            existing.upload_heartbeat_at = None
            existing.finalization_token = None
            existing.finalization_heartbeat_at = None
            existing.failure_code = "superseded_by_retry"
            await session.flush()
        if existing.status == "expired":
            if not _upload_storage_matches(existing, storage):
                raise HTTPException(
                    status_code=503,
                    detail="expired test set upload backend is unavailable",
                )
            generation = existing.generation + 1
        else:
            return await _upload_response(
                existing,
                request=request,
                session=session,
                settings=settings,
                storage=storage,
            )

    await _enforce_upload_quota(
        owner_id=user.id,
        test_set_id=test_set.id,
        requested_size=payload.size_bytes,
        session=session,
        settings=settings,
    )
    failed_conflicts = list(
        (
            await session.scalars(
                select(TestSetItemUploadSession)
                .where(
                    TestSetItemUploadSession.test_set_id == test_set.id,
                    TestSetItemUploadSession.status == "failed",
                    or_(
                        TestSetItemUploadSession.item_key == payload.item_key,
                        TestSetItemUploadSession.sort_order == payload.sort_order,
                    ),
                )
                .with_for_update()
            )
        ).all()
    )
    for failed in failed_conflicts:
        if not _upload_storage_matches(failed, storage):
            raise HTTPException(
                status_code=503,
                detail="failed test set upload backend is unavailable",
            )
        failed.status = "expired"
        failed.upload_write_token = None
        failed.upload_heartbeat_at = None
        failed.finalization_token = None
        failed.finalization_heartbeat_at = None
        failed.failure_code = "superseded_by_retry"
    await session.flush()
    item_conflict = await session.scalar(
        select(TestSetItem.id)
        .where(
            TestSetItem.test_set_id == test_set.id,
            or_(
                TestSetItem.item_key == payload.item_key,
                TestSetItem.sort_order == payload.sort_order,
            ),
        )
        .limit(1)
    )
    reservation_conflict = await session.scalar(
        select(TestSetItemUploadSession.id)
        .where(
            TestSetItemUploadSession.test_set_id == test_set.id,
            TestSetItemUploadSession.status.in_(("pending", "finalizing")),
            or_(
                TestSetItemUploadSession.item_key == payload.item_key,
                TestSetItemUploadSession.sort_order == payload.sort_order,
            ),
        )
        .limit(1)
    )
    if item_conflict is not None or reservation_conflict is not None:
        raise HTTPException(status_code=409, detail="test set item key or order is reserved")

    upload_id = str(uuid.uuid4())
    expires_at = utc_now() + timedelta(seconds=settings.test_set_upload_ttl_seconds)
    local_token = None
    local_token_hash = None
    if storage.backend == "local":
        local_token = derive_test_set_upload_token(
            upload_id,
            int(expires_at.timestamp()),
            settings,
        )
        local_token_hash = upload_token_hash(local_token)
    upload = TestSetItemUploadSession(
        id=upload_id,
        test_set_id=test_set.id,
        owner_id=user.id,
        idempotency_key=payload.idempotency_key,
        generation=generation,
        request_fingerprint=fingerprint,
        item_key=payload.item_key,
        display_name=payload.display_name,
        sort_order=payload.sort_order,
        filename=payload.filename,
        content_type=payload.content_type,
        expected_size_bytes=payload.size_bytes,
        expected_sha256=payload.sha256,
        license_reference=payload.license_reference,
        provenance_reference=payload.provenance_reference,
        temporary_object_key=test_set_temporary_object_key(test_set.id, upload_id),
        canonical_object_key=test_set_item_object_key(test_set.id, upload_id),
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
            local_upload_path=f"/api/v1/storage/test-set-item-uploads/{upload.id}",
        )
    except IntegrityError as exc:
        await session.rollback()
        raced = await _find_upload_session(
            session,
            test_set_id=test_set.id,
            owner_id=user.id,
            idempotency_key=payload.idempotency_key,
        )
        if raced is None or raced.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="test set upload conflict") from exc
        return await _upload_response(
            raced,
            request=request,
            session=session,
            settings=settings,
            storage=storage,
        )
    except StorageError as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="test set upload signing failed") from exc
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="test_set.item_upload_initialized",
        resource_type="test_set",
        resource_id=test_set.id,
        details={"item_key": payload.item_key, "sort_order": payload.sort_order},
    )
    await session.commit()
    return TestSetItemUploadInitResponse(
        upload_session_id=upload.id,
        test_set_id=test_set.id,
        status="pending",
        method="PUT",
        upload_url=target.url,
        upload_headers=target.headers,
        expires_at=upload.expires_at,
    )


@router.put(
    "/storage/test-set-item-uploads/{upload_session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_408_REQUEST_TIMEOUT: {
            "description": "The absolute upload-session deadline elapsed",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Upload generation, writer lease, or TestSet state changed",
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": "Upload exceeded the reserved byte count",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Content headers or streamed size do not match the session",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Bound storage namespace is unavailable",
        },
    },
)
async def local_test_set_item_upload(
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
    identity = await session.get(TestSetItemUploadSession, upload_session_id)
    if identity is None or identity.storage_backend != "local":
        raise HTTPException(status_code=404, detail="test set upload session not found")
    test_set = await session.scalar(
        select(TestSet).where(TestSet.id == identity.test_set_id).with_for_update()
    )
    upload = await session.scalar(
        select(TestSetItemUploadSession)
        .where(TestSetItemUploadSession.id == upload_session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if upload is None or upload.test_set_id != identity.test_set_id:
        raise HTTPException(status_code=409, detail="test set upload session changed")
    if not _upload_storage_matches(upload, storage):
        raise HTTPException(
            status_code=503,
            detail="test set upload storage namespace is unavailable",
        )
    if test_set is None or test_set.status != "draft":
        raise HTTPException(status_code=409, detail="test set upload is not writable")
    if upload.status != "pending":
        raise HTTPException(status_code=409, detail="test set upload is not writable")
    expected_temporary_key = test_set_temporary_object_key(test_set.id, upload.id)
    if upload.temporary_object_key != expected_temporary_key:
        raise HTTPException(status_code=409, detail="test set staging key is invalid")
    if as_utc(upload.expires_at) <= utc_now():
        await _expire_upload(upload, session=session, storage=storage)
        await session.commit()
        raise HTTPException(status_code=410, detail="test set upload expired")
    if upload_token is None or not verify_upload_token(upload_token, upload.upload_token_hash):
        raise HTTPException(status_code=401, detail="invalid test set upload token")
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
        stale_before = utc_now() - timedelta(seconds=settings.test_set_upload_write_stale_seconds)
        if heartbeat is not None and as_utc(heartbeat) > stale_before:
            raise HTTPException(status_code=409, detail="test set upload write is active")
        # Never replace a stale writer token on the same object key: the old
        # writer could finish after the replacement writer and delete/overwrite
        # its bytes. Fencing the generation forces a retry onto a new key.
        upload.status = "expired"
        upload.upload_write_token = None
        upload.upload_heartbeat_at = None
        upload.failure_code = "stale_upload_writer"
        await session.commit()
        raise HTTPException(status_code=409, detail="stale test set upload writer was fenced")

    generation = upload.generation
    write_expires_at = as_utc(upload.expires_at)
    write_token = str(uuid.uuid4())
    claimed_at = utc_now()
    claimed = await session.execute(
        update(TestSetItemUploadSession)
        .where(
            TestSetItemUploadSession.id == upload.id,
            TestSetItemUploadSession.generation == generation,
            TestSetItemUploadSession.status == "pending",
            TestSetItemUploadSession.upload_write_token.is_(None),
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
        raise HTTPException(status_code=409, detail="test set upload write conflict")
    await session.commit()

    try:
        remaining_seconds = max(0.0, (write_expires_at - utc_now()).total_seconds())
        with anyio.fail_after(remaining_seconds):
            await _run_with_lease_heartbeat(
                lambda: storage.write_upload_stream(
                    expected_temporary_key,
                    request.stream(),
                    expected_size=upload.expected_size_bytes,
                ),
                heartbeat=partial(
                    _touch_upload_write,
                    database,
                    upload_id=upload.id,
                    generation=generation,
                    write_token=write_token,
                ),
                heartbeat_seconds=settings.test_set_upload_write_heartbeat_seconds,
                lease_lost_error=TestSetUploadWriteLeaseLost,
            )
    except TimeoutError as exc:
        await _expire_upload_write_claim(
            database,
            upload_id=upload.id,
            generation=generation,
            write_token=write_token,
        )
        await _delete_best_effort(storage, expected_temporary_key)
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            detail="test set upload deadline exceeded",
        ) from exc
    except TestSetUploadWriteLeaseLost as exc:
        await _delete_best_effort(storage, expected_temporary_key)
        await _clear_upload_write_claim(
            database,
            upload_id=upload.id,
            generation=generation,
            write_token=write_token,
        )
        raise HTTPException(status_code=409, detail="test set upload write lease was lost") from exc
    except ObjectTooLarge as exc:
        await _delete_best_effort(storage, expected_temporary_key)
        await _clear_upload_write_claim(
            database,
            upload_id=upload.id,
            generation=generation,
            write_token=write_token,
        )
        raise HTTPException(status_code=413, detail="test set upload exceeds size") from exc
    except ObjectSizeMismatch as exc:
        await _delete_best_effort(storage, expected_temporary_key)
        await _clear_upload_write_claim(
            database,
            upload_id=upload.id,
            generation=generation,
            write_token=write_token,
        )
        raise HTTPException(status_code=422, detail="test set upload size mismatch") from exc
    except StorageError as exc:
        await _delete_best_effort(storage, expected_temporary_key)
        await _clear_upload_write_claim(
            database,
            upload_id=upload.id,
            generation=generation,
            write_token=write_token,
        )
        raise HTTPException(status_code=503, detail="test set upload failed") from exc

    locked_test_set = await session.scalar(
        select(TestSet).where(TestSet.id == identity.test_set_id).with_for_update()
    )
    locked_upload = await session.scalar(
        select(TestSetItemUploadSession)
        .where(TestSetItemUploadSession.id == upload_session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        locked_test_set is None
        or locked_test_set.status != "draft"
        or locked_upload is None
        or locked_upload.test_set_id != locked_test_set.id
        or locked_upload.generation != generation
        or locked_upload.status != "pending"
        or locked_upload.upload_write_token != write_token
        or not _upload_storage_matches(locked_upload, storage)
        or locked_upload.temporary_object_key != expected_temporary_key
    ):
        await session.rollback()
        await _delete_best_effort(storage, expected_temporary_key)
        await _clear_upload_write_claim(
            database,
            upload_id=upload.id,
            generation=generation,
            write_token=write_token,
        )
        raise HTTPException(status_code=409, detail="test set upload write lease was lost")
    locked_upload.uploaded_at = utc_now()
    locked_upload.upload_write_token = None
    locked_upload.upload_heartbeat_at = None
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/test-sets/item-uploads/{upload_session_id}/finalize",
    response_model=TestSetItemRead,
)
async def finalize_test_set_item_upload(
    upload_session_id: str,
    request: Request,
    response: Response,
    session: SessionDep,
    user: CurrentUserDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> TestSetItemRead:
    database = cast(Database, request.app.state.database)
    upload_identity = await session.get(TestSetItemUploadSession, upload_session_id)
    if upload_identity is None:
        raise HTTPException(status_code=404, detail="test set upload session not found")
    test_set = await _locked_owned_test_set(upload_identity.test_set_id, session, user)
    upload = await session.scalar(
        select(TestSetItemUploadSession)
        .where(TestSetItemUploadSession.id == upload_session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if upload is None or upload.test_set_id != test_set.id:
        raise HTTPException(status_code=409, detail="test set upload session changed")
    if not _upload_storage_matches(upload, storage):
        raise HTTPException(status_code=503, detail="test set storage backend is unavailable")
    if upload.status == "completed":
        item = await session.scalar(
            select(TestSetItem).where(
                TestSetItem.test_set_id == upload.test_set_id,
                TestSetItem.item_key == upload.item_key,
            )
        )
        if item is None:
            raise HTTPException(status_code=409, detail="completed upload lost its item")
        _private_no_store(response)
        return TestSetItemRead.model_validate(item)
    if test_set.status != "draft":
        raise HTTPException(status_code=409, detail="test set revision is immutable")
    if upload.status in {"failed", "expired"}:
        raise HTTPException(status_code=409, detail=f"test set upload is {upload.status}")
    expected_temporary_key = test_set_temporary_object_key(test_set.id, upload.id)
    expected_canonical_key = test_set_item_object_key(test_set.id, upload.id)
    if (
        upload.temporary_object_key != expected_temporary_key
        or upload.canonical_object_key != expected_canonical_key
    ):
        raise HTTPException(status_code=409, detail="test set upload object key is invalid")
    if upload.status == "finalizing":
        heartbeat = upload.finalization_heartbeat_at or upload.updated_at
        stale_at = as_utc(heartbeat) + timedelta(seconds=settings.test_set_finalizing_stale_seconds)
        if stale_at > utc_now():
            raise HTTPException(status_code=409, detail="test set upload is already finalizing")
        stale_token = upload.finalization_token
        if stale_token is None:
            raise HTTPException(status_code=409, detail="test set finalization token is missing")
        await _mark_finalizing_upload_failed(
            upload.id,
            stale_token,
            "stale_finalizing",
            session=session,
            generation=upload.generation,
        )
        # The recovering request did not publish the canonical key and must not
        # delete it. A live old finalizer observes the token fence and removes
        # only bytes it can prove it published; a crashed writer is preserved.
        raise HTTPException(status_code=409, detail="stale test set finalization was recovered")
    if as_utc(upload.expires_at) <= utc_now():
        await _expire_upload(upload, session=session, storage=storage)
        await session.commit()
        raise HTTPException(status_code=409, detail="test set upload expired")

    if upload.upload_write_token is not None:
        heartbeat = upload.upload_heartbeat_at or upload.updated_at
        stale_before = utc_now() - timedelta(seconds=settings.test_set_upload_write_stale_seconds)
        if heartbeat is not None and as_utc(heartbeat) > stale_before:
            raise HTTPException(status_code=409, detail="test set upload write is active")
        upload.status = "expired"
        upload.upload_write_token = None
        upload.upload_heartbeat_at = None
        upload.failure_code = "stale_upload_writer"
        await session.commit()
        raise HTTPException(status_code=409, detail="stale test set upload writer was fenced")
    generation = upload.generation
    finalization_token = str(uuid.uuid4())
    claimed_at = utc_now()
    claimed = await session.execute(
        update(TestSetItemUploadSession)
        .where(
            TestSetItemUploadSession.id == upload.id,
            TestSetItemUploadSession.generation == generation,
            TestSetItemUploadSession.status == "pending",
            TestSetItemUploadSession.upload_write_token.is_(None),
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
        raise HTTPException(status_code=409, detail="test set finalization conflict")
    await session.commit()

    spool_path: Path | None = None
    canonical_publication = anyio.Event()

    async def publish_canonical() -> None:
        assert spool_path is not None
        await storage.store_verified_file(
            expected_canonical_key,
            spool_path,
            content_type="audio/wav",
            sha256=upload.expected_sha256,
        )
        canonical_publication.set()

    finalization_heartbeat = partial(
        _touch_finalization,
        database,
        upload_id=upload.id,
        generation=generation,
        finalization_token=finalization_token,
    )
    try:
        verified_spool = await _run_with_lease_heartbeat(
            lambda: verify_object_to_spool(
                storage,
                expected_temporary_key,
                expected_size=upload.expected_size_bytes,
                expected_sha256=upload.expected_sha256,
                settings=settings,
            ),
            heartbeat=finalization_heartbeat,
            heartbeat_seconds=settings.test_set_finalizing_heartbeat_seconds,
            lease_lost_error=TestSetFinalizationLeaseLost,
        )
        spool_path = verified_spool
        inspection = await _run_with_lease_heartbeat(
            lambda: anyio.to_thread.run_sync(inspect_pcm_wav, verified_spool, settings),
            heartbeat=finalization_heartbeat,
            heartbeat_seconds=settings.test_set_finalizing_heartbeat_seconds,
            lease_lost_error=TestSetFinalizationLeaseLost,
        )
        await _run_with_lease_heartbeat(
            publish_canonical,
            heartbeat=finalization_heartbeat,
            heartbeat_seconds=settings.test_set_finalizing_heartbeat_seconds,
            lease_lost_error=TestSetFinalizationLeaseLost,
        )
    except TestSetFinalizationLeaseLost as exc:
        if canonical_publication.is_set():
            await _delete_best_effort(storage, expected_canonical_key)
        raise HTTPException(
            status_code=409,
            detail="test set finalization lease was lost",
        ) from exc
    except ObjectNotFound as exc:
        transitioned = await _mark_finalizing_upload_failed(
            upload.id,
            finalization_token,
            "uploaded_object_not_found",
            session=session,
            generation=generation,
        )
        if not transitioned:
            raise HTTPException(
                status_code=409, detail="test set finalization lease was lost"
            ) from exc
        raise HTTPException(status_code=409, detail="uploaded WAV object not found") from exc
    except (ArtifactVerificationMismatch, ObjectTooLarge) as exc:
        failure_code = (
            exc.failure_code if isinstance(exc, ArtifactVerificationMismatch) else "size_mismatch"
        )
        transitioned = await _mark_finalizing_upload_failed(
            upload.id,
            finalization_token,
            failure_code,
            session=session,
            generation=generation,
        )
        if not transitioned:
            raise HTTPException(
                status_code=409, detail="test set finalization lease was lost"
            ) from exc
        await _delete_best_effort(storage, expected_temporary_key)
        raise HTTPException(status_code=422, detail="WAV size or SHA-256 mismatch") from exc
    except InvalidTestSetWav as exc:
        transitioned = await _mark_finalizing_upload_failed(
            upload.id,
            finalization_token,
            exc.failure_code,
            session=session,
            generation=generation,
        )
        if not transitioned:
            raise HTTPException(
                status_code=409, detail="test set finalization lease was lost"
            ) from exc
        await _delete_best_effort(storage, expected_temporary_key)
        raise HTTPException(status_code=422, detail="unsupported or invalid PCM WAV") from exc
    except ArtifactSpoolError as exc:
        transitioned = await _mark_finalizing_upload_failed(
            upload.id,
            finalization_token,
            exc.failure_code,
            session=session,
            generation=generation,
        )
        if not transitioned:
            raise HTTPException(
                status_code=409, detail="test set finalization lease was lost"
            ) from exc
        raise HTTPException(status_code=503, detail="WAV verification spool unavailable") from exc
    except StorageError as exc:
        transitioned = await _mark_finalizing_upload_failed(
            upload.id,
            finalization_token,
            "test_set_item_publish_failed",
            session=session,
            generation=generation,
        )
        if not transitioned:
            raise HTTPException(
                status_code=409, detail="test set finalization lease was lost"
            ) from exc
        if canonical_publication.is_set():
            await _delete_best_effort(storage, expected_canonical_key)
        raise HTTPException(status_code=503, detail="WAV object publish failed") from exc
    finally:
        if spool_path is not None:
            try:
                await remove_spool_file(spool_path)
            except ArtifactSpoolError:
                pass

    assert canonical_publication.is_set()
    locked_test_set = await _locked_owned_test_set(upload.test_set_id, session, user)
    locked_upload = await session.scalar(
        select(TestSetItemUploadSession)
        .where(TestSetItemUploadSession.id == upload.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_upload is not None and locked_upload.status == "completed":
        completed_item = await session.scalar(
            select(TestSetItem).where(
                TestSetItem.test_set_id == locked_upload.test_set_id,
                TestSetItem.item_key == locked_upload.item_key,
            )
        )
        if completed_item is None:
            raise HTTPException(status_code=409, detail="completed upload lost its item")
        _private_no_store(response)
        return TestSetItemRead.model_validate(completed_item)
    if (
        locked_upload is None
        or locked_upload.status != "finalizing"
        or locked_upload.generation != generation
        or locked_upload.finalization_token != finalization_token
        or locked_test_set.status != "draft"
    ):
        await session.rollback()
        await _mark_finalizing_upload_failed(
            upload.id,
            finalization_token,
            "finalization_lease_lost",
            session=session,
            generation=generation,
        )
        if canonical_publication.is_set():
            await _delete_best_effort(storage, expected_canonical_key)
        raise HTTPException(status_code=409, detail="test set finalization lease was lost")

    conflict = await session.scalar(
        select(TestSetItem.id)
        .where(
            TestSetItem.test_set_id == locked_test_set.id,
            or_(
                TestSetItem.item_key == locked_upload.item_key,
                TestSetItem.sort_order == locked_upload.sort_order,
            ),
        )
        .limit(1)
    )
    if conflict is not None:
        await session.rollback()
        await _mark_finalizing_upload_failed(
            upload.id,
            finalization_token,
            "item_reservation_conflict",
            session=session,
            generation=generation,
        )
        if canonical_publication.is_set():
            await _delete_best_effort(storage, expected_canonical_key)
        raise HTTPException(status_code=409, detail="test set item key or order conflict")

    item = TestSetItem(
        test_set_id=locked_test_set.id,
        item_key=locked_upload.item_key,
        display_name=locked_upload.display_name,
        sort_order=locked_upload.sort_order,
        storage_uri=storage.storage_uri(locked_upload.canonical_object_key),
        original_filename=locked_upload.filename,
        size_bytes=locked_upload.expected_size_bytes,
        sha256=locked_upload.expected_sha256,
        mime_type="audio/wav",
        sample_rate_hz=inspection.sample_rate_hz,
        channels=inspection.channels,
        duration_seconds=inspection.duration_seconds,
        license_reference=locked_upload.license_reference,
        provenance_reference=locked_upload.provenance_reference,
    )
    session.add(item)
    locked_upload.status = "completed"
    locked_upload.finalization_token = None
    locked_upload.finalization_heartbeat_at = None
    locked_upload.failure_code = None
    locked_upload.finalized_at = utc_now()
    locked_test_set.item_count = (
        await session.scalar(
            select(func.count())
            .select_from(TestSetItem)
            .where(TestSetItem.test_set_id == locked_test_set.id)
        )
        or 0
    ) + 1
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        transitioned = await _mark_finalizing_upload_failed(
            upload.id,
            finalization_token,
            "item_reservation_conflict",
            session=session,
            generation=generation,
        )
        if transitioned and canonical_publication.is_set():
            cleanup_ok = await _delete_best_effort(storage, expected_canonical_key)
            if not cleanup_ok:
                await session.execute(
                    update(TestSetItemUploadSession)
                    .where(
                        TestSetItemUploadSession.id == upload.id,
                        TestSetItemUploadSession.status == "failed",
                    )
                    .values(
                        failure_code="canonical_cleanup_pending",
                        updated_at=utc_now(),
                    )
                )
                await session.commit()
        raise HTTPException(status_code=409, detail="test set item conflict") from exc
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="test_set.item_finalized",
        resource_type="test_set",
        resource_id=locked_test_set.id,
        details={
            "item_key": item.item_key,
            "sort_order": item.sort_order,
            "sha256": item.sha256,
        },
    )
    await session.commit()
    if not await _delete_best_effort(storage, upload.temporary_object_key):
        await session.execute(
            update(TestSetItemUploadSession)
            .where(
                TestSetItemUploadSession.id == upload.id,
                TestSetItemUploadSession.status == "completed",
            )
            .values(failure_code="staging_cleanup_pending", updated_at=utc_now())
        )
        await session.commit()
    await session.refresh(item)
    _private_no_store(response)
    return TestSetItemRead.model_validate(item)


@router.post("/test-sets/{test_set_id}/finalize", response_model=TestSetRead)
async def finalize_test_set(
    test_set_id: str,
    response: Response,
    session: SessionDep,
    user: CurrentUserDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> TestSetRead:
    test_set = await _locked_owned_test_set(test_set_id, session, user)
    if test_set.status == "ready":
        items = await _items_for_test_set(session, test_set.id)
        _private_no_store(response)
        return test_set_to_read(test_set, items)
    if test_set.status != "draft":
        raise HTTPException(status_code=409, detail="test set revision cannot be finalized")
    uploads = list(
        (
            await session.scalars(
                select(TestSetItemUploadSession).where(
                    TestSetItemUploadSession.test_set_id == test_set.id
                )
            )
        ).all()
    )
    if any(upload.status in {"pending", "finalizing", "failed"} for upload in uploads):
        raise HTTPException(status_code=409, detail="test set has unresolved upload sessions")
    items = await _items_for_test_set(session, test_set.id)
    if not items:
        raise HTTPException(status_code=409, detail="test set requires at least one item")
    if len(items) > settings.test_set_max_items:
        raise HTTPException(status_code=409, detail="test set item limit exceeded")
    if sum(item.size_bytes for item in items) > settings.test_set_max_total_bytes:
        raise HTTPException(status_code=409, detail="test set total byte limit exceeded")
    if (
        math.fsum(item.duration_seconds for item in items)
        > settings.test_set_max_total_duration_seconds
    ):
        raise HTTPException(status_code=409, detail="test set total duration limit exceeded")
    completed_uploads = [upload for upload in uploads if upload.status == "completed"]
    if len(completed_uploads) != len(items):
        raise HTTPException(
            status_code=409,
            detail="test set item upload ledger is incomplete",
        )
    try:
        for item in items:
            matches = [upload for upload in completed_uploads if upload.item_key == item.item_key]
            if len(matches) != 1:
                raise HTTPException(
                    status_code=409,
                    detail="test set item does not have exactly one completed upload",
                )
            completed = matches[0]
            expected_key = test_set_item_object_key(test_set.id, completed.id)
            if (
                not _upload_storage_matches(completed, storage)
                or completed.canonical_object_key != expected_key
                or completed.sort_order != item.sort_order
                or completed.display_name != item.display_name
                or completed.filename != item.original_filename
                or completed.expected_size_bytes != item.size_bytes
                or completed.expected_sha256 != item.sha256
                or completed.license_reference != item.license_reference
                or completed.provenance_reference != item.provenance_reference
                or storage.storage_uri(expected_key) != item.storage_uri
            ):
                raise HTTPException(
                    status_code=409,
                    detail="test set canonical item ledger is inconsistent",
                )
            if not await _canonical_object_matches_item(
                storage,
                expected_key,
                item,
                chunk_size=settings.artifact_stream_chunk_bytes,
            ):
                raise HTTPException(
                    status_code=409,
                    detail="test set canonical item verification failed",
                )
    except HTTPException:
        test_set.failure_code = "canonical_revalidation_failed"
        await session.commit()
        raise
    except (ObjectNotFound, ObjectTooLarge) as exc:
        test_set.failure_code = "canonical_revalidation_failed"
        await session.commit()
        raise HTTPException(
            status_code=409,
            detail="test set canonical item verification failed",
        ) from exc
    except StorageError as exc:
        test_set.failure_code = "canonical_revalidation_unavailable"
        await session.commit()
        raise HTTPException(
            status_code=503,
            detail="test set canonical item verification unavailable",
        ) from exc
    document = build_test_set_manifest_document(test_set, items)
    spool_path: Path | None = None
    object_key = test_set_manifest_object_key(test_set.id)
    try:
        spool_path, manifest_sha256 = await _write_manifest_spool(document)
        await storage.store_verified_file(
            object_key,
            spool_path,
            content_type="application/json",
            sha256=manifest_sha256,
        )
    except (OSError, StorageError) as exc:
        test_set.failure_code = "manifest_publish_failed"
        await session.commit()
        await _delete_best_effort(storage, object_key)
        raise HTTPException(status_code=503, detail="test set manifest publish failed") from exc
    finally:
        if spool_path is not None:
            await anyio.to_thread.run_sync(spool_path.unlink, True)

    test_set.manifest_storage_uri = storage.storage_uri(object_key)
    test_set.manifest_sha256 = manifest_sha256
    test_set.item_count = len(items)
    test_set.failure_code = None
    test_set.status = "ready"
    test_set.finalized_at = utc_now()
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="test_set.finalized",
        resource_type="test_set",
        resource_id=test_set.id,
        details={"item_count": len(items), "manifest_sha256": manifest_sha256},
    )
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        await _delete_best_effort(storage, object_key)
        raise
    await session.refresh(test_set)
    _private_no_store(response)
    return test_set_to_read(test_set, items)


@router.post(
    "/test-sets/{test_set_id}/revisions",
    response_model=TestSetRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_test_set_revision(
    test_set_id: str,
    payload: TestSetRevisionCreate,
    response: Response,
    session: SessionDep,
    user: CurrentUserDep,
) -> TestSetRead:
    source = await _owned_test_set(test_set_id, session, user)
    if source.status != "ready":
        raise HTTPException(status_code=409, detail="only a ready test set can be revised")
    latest = await session.scalar(
        select(TestSet)
        .where(TestSet.family_id == source.family_id)
        .order_by(TestSet.revision.desc())
        .with_for_update()
        .limit(1)
    )
    if latest is None:
        raise HTTPException(status_code=409, detail="test set family disappeared")
    revision = TestSet(
        family_id=source.family_id,
        name=source.name,
        revision=latest.revision + 1,
        description=payload.description,
        status="draft",
        item_count=0,
        created_by=user.id,
    )
    session.add(revision)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="test set revision conflict") from exc
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="test_set.revision_created",
        resource_type="test_set",
        resource_id=revision.id,
        details={"family_id": revision.family_id, "revision": revision.revision},
    )
    await session.commit()
    await session.refresh(revision)
    _private_no_store(response)
    return test_set_to_read(revision, [])


@router.get("/test-sets", response_model=TestSetList)
async def list_test_sets(
    response: Response,
    session: SessionDep,
    user: CurrentUserDep,
    test_set_status: Annotated[
        str | None,
        Query(alias="status", pattern="^(draft|ready|failed)$"),
    ] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> TestSetList:
    filters = [] if user.role == "admin" else [TestSet.created_by == user.id]
    if test_set_status is not None:
        filters.append(TestSet.status == test_set_status)
    total = await session.scalar(select(func.count()).select_from(TestSet).where(*filters)) or 0
    items = list(
        (
            await session.scalars(
                select(TestSet)
                .where(*filters)
                .order_by(TestSet.created_at.desc(), TestSet.id.asc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    _private_no_store(response)
    return TestSetList(
        items=[test_set_to_read(item, [], items_included=False) for item in items],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/test-sets/{test_set_id}", response_model=TestSetRead)
async def get_test_set(
    test_set_id: str,
    response: Response,
    session: SessionDep,
    user: CurrentUserDep,
) -> TestSetRead:
    test_set = await _owned_test_set(test_set_id, session, user)
    items = await _items_for_test_set(session, test_set.id)
    _private_no_store(response)
    return test_set_to_read(test_set, items)


@router.delete("/test-sets/{test_set_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_test_set(
    test_set_id: str,
    session: SessionDep,
    user: CurrentUserDep,
    storage: StorageDep,
) -> Response:
    test_set = await _locked_owned_test_set(test_set_id, session, user)
    if test_set.status not in {"draft", "failed"}:
        raise HTTPException(status_code=409, detail="ready test set revisions are immutable")
    references = (
        await session.scalar(
            select(func.count()).select_from(Job).where(Job.test_set_id == test_set.id)
        )
        or 0
    ) + (
        await session.scalar(
            select(func.count()).select_from(Sample).where(Sample.test_set_id == test_set.id)
        )
        or 0
    )
    if references:
        raise HTTPException(status_code=409, detail="test set revision is referenced")
    uploads = list(
        (
            await session.scalars(
                select(TestSetItemUploadSession).where(
                    TestSetItemUploadSession.test_set_id == test_set.id
                )
            )
        ).all()
    )
    if any(upload.status in {"pending", "finalizing"} for upload in uploads):
        raise HTTPException(status_code=409, detail="active test set upload cannot be deleted")
    if any(not _upload_storage_matches(upload, storage) for upload in uploads):
        test_set.status = "failed"
        test_set.failure_code = "delete_storage_backend_unavailable"
        await session.commit()
        raise HTTPException(
            status_code=503,
            detail="test set storage backend is unavailable for deletion",
        )
    items = await _items_for_test_set(session, test_set.id)
    completed_item_keys = {upload.item_key for upload in uploads if upload.status == "completed"}
    if any(item.item_key not in completed_item_keys for item in items):
        raise HTTPException(
            status_code=409,
            detail="test set contains an item without a cleanup session",
        )
    cleanup_keys = {
        key
        for upload in uploads
        for key in (upload.temporary_object_key, upload.canonical_object_key)
    }
    cleanup_keys.add(test_set_manifest_object_key(test_set.id))
    cleanup_failed = False
    for object_key in sorted(cleanup_keys):
        if not await _delete_best_effort(storage, object_key):
            cleanup_failed = True
    if cleanup_failed:
        test_set.status = "failed"
        test_set.failure_code = "delete_cleanup_failed"
        for upload in uploads:
            if upload.status != "completed":
                upload.failure_code = "delete_cleanup_pending"
        await session.commit()
        raise HTTPException(status_code=503, detail="test set object cleanup failed")
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="test_set.deleted",
        resource_type="test_set",
        resource_id=test_set.id,
    )
    await session.delete(test_set)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/presets", response_model=PresetRead, status_code=status.HTTP_201_CREATED)
async def create_preset(
    payload: PresetCreate,
    response: Response,
    session: SessionDep,
    user: CurrentUserDep,
) -> PresetRead:
    document = preset_document(payload.config)
    preset = Preset(
        family_id=new_id(),
        name=payload.name,
        revision=1,
        config_json=document,
        config_sha256=canonical_sha256(document),
        created_by=user.id,
    )
    session.add(preset)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="preset name already has an initial revision",
        ) from exc
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="preset.created",
        resource_type="preset",
        resource_id=preset.id,
        details={"family_id": preset.family_id, "revision": 1},
    )
    await session.commit()
    await session.refresh(preset)
    _private_no_store(response)
    return preset_to_read(preset)


@router.post(
    "/presets/{preset_id}/revisions",
    response_model=PresetRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_preset_revision(
    preset_id: str,
    payload: PresetRevisionCreate,
    response: Response,
    session: SessionDep,
    user: CurrentUserDep,
) -> PresetRead:
    source = await _owned_preset(preset_id, session, user)
    latest = await session.scalar(
        select(Preset)
        .where(Preset.family_id == source.family_id)
        .order_by(Preset.revision.desc())
        .with_for_update()
        .limit(1)
    )
    if latest is None:
        raise HTTPException(status_code=409, detail="preset family disappeared")
    document = preset_document(payload.config)
    revision = Preset(
        family_id=source.family_id,
        name=source.name,
        revision=latest.revision + 1,
        config_json=document,
        config_sha256=canonical_sha256(document),
        created_by=user.id,
    )
    session.add(revision)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="preset revision conflict") from exc
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="preset.revision_created",
        resource_type="preset",
        resource_id=revision.id,
        details={"family_id": revision.family_id, "revision": revision.revision},
    )
    await session.commit()
    await session.refresh(revision)
    _private_no_store(response)
    return preset_to_read(revision)


@router.get("/presets", response_model=PresetList)
async def list_presets(
    response: Response,
    session: SessionDep,
    user: CurrentUserDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PresetList:
    filters = [] if user.role == "admin" else [Preset.created_by == user.id]
    total = await session.scalar(select(func.count()).select_from(Preset).where(*filters)) or 0
    presets = list(
        (
            await session.scalars(
                select(Preset)
                .where(*filters)
                .order_by(Preset.created_at.desc(), Preset.id.asc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    _private_no_store(response)
    return PresetList(
        items=[preset_to_read(preset) for preset in presets],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/presets/{preset_id}", response_model=PresetRead)
async def get_preset(
    preset_id: str,
    response: Response,
    session: SessionDep,
    user: CurrentUserDep,
) -> PresetRead:
    preset = await _owned_preset(preset_id, session, user)
    _private_no_store(response)
    return preset_to_read(preset)


@router.delete("/presets/{preset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_preset(
    preset_id: str,
    session: SessionDep,
    user: CurrentUserDep,
) -> Response:
    preset = await _owned_preset(preset_id, session, user)
    family = list(
        (
            await session.scalars(
                select(Preset).where(Preset.family_id == preset.family_id).with_for_update()
            )
        ).all()
    )
    if not family:
        raise HTTPException(status_code=409, detail="preset family disappeared")
    reference = await session.scalar(select(Job.id).where(Job.preset_id == preset.id).limit(1))
    if reference is not None:
        raise HTTPException(status_code=409, detail="preset revision is referenced by a job")
    if len(family) > 1:
        raise HTTPException(
            status_code=409,
            detail="preset revisions cannot be deleted from a multi-revision family",
        )
    add_audit_event(
        session,
        actor_type="user",
        actor_id=user.id,
        action="preset.deleted",
        resource_type="preset",
        resource_id=preset.id,
    )
    await session.execute(delete(Preset).where(Preset.id == preset.id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
