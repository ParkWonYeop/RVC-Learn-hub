from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Literal, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import add_audit_event
from ..database import Database
from ..models import Artifact, ArtifactUploadSession, Dataset, DatasetUploadSession
from ..storage import (
    UNBOUND_STORAGE_NAMESPACE_SHA256,
    ObjectNotFound,
    ObjectTooLarge,
    StorageAdapter,
    StorageError,
)

StorageSessionKind = Literal["dataset", "artifact"]
StorageAdoptionKind = Literal["dataset", "artifact", "all"]
_METADATA_MAX_BYTES = 64 * 1024**2


@dataclass(frozen=True, slots=True)
class StorageAdoptionItem:
    kind: StorageSessionKind
    session_id: str
    outcome: Literal["adopted", "verified", "rejected", "not_found"]
    code: str


@dataclass(frozen=True, slots=True)
class StorageAdoptionResult:
    dry_run: bool
    requested_kind: StorageAdoptionKind
    target_storage_backend: str
    target_storage_namespace_sha256: str
    examined: int
    adopted: int
    verified: int
    rejected: int
    remaining_unbound: int
    items: list[StorageAdoptionItem]

    def as_json(self) -> dict[str, object]:
        return asdict(self)


async def _object_matches(
    storage: StorageAdapter,
    object_key: str,
    *,
    expected_sha256: str,
    chunk_size: int,
    expected_size: int | None,
) -> tuple[bool, str]:
    digest = hashlib.sha256()
    total = 0
    max_bytes = expected_size + 1 if expected_size is not None else _METADATA_MAX_BYTES
    try:
        async for chunk in storage.stream_object(
            object_key,
            chunk_size=chunk_size,
            max_bytes=max_bytes,
        ):
            total += len(chunk)
            digest.update(chunk)
    except ObjectNotFound:
        return False, "object_not_found"
    except ObjectTooLarge:
        return False, "object_size_mismatch"
    except StorageError:
        return False, "storage_unavailable"
    if expected_size is not None and total != expected_size:
        return False, "object_size_mismatch"
    if digest.hexdigest() != expected_sha256:
        return False, "object_sha256_mismatch"
    return True, "verified"


def _valid_sha256(value: str | None) -> bool:
    if value is None or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value.lower())


async def _verify_dataset_session(
    session: AsyncSession,
    upload: DatasetUploadSession,
    storage: StorageAdapter,
    *,
    chunk_size: int,
) -> tuple[bool, str]:
    if upload.status in {"pending", "finalizing"}:
        return False, "active_session"
    if upload.status in {"failed", "expired"}:
        return await _object_matches(
            storage,
            upload.temporary_object_key,
            expected_sha256=upload.expected_sha256,
            expected_size=upload.expected_size_bytes,
            chunk_size=chunk_size,
        )
    if upload.status != "completed":
        return False, "unsupported_status"
    dataset = await session.get(Dataset, upload.dataset_id)
    if dataset is None:
        return False, "dataset_missing"
    if (
        dataset.original_size_bytes != upload.expected_size_bytes
        or dataset.original_sha256 != upload.expected_sha256
        or dataset.prepared_flat_size_bytes is None
        or dataset.prepared_flat_size_bytes <= 0
        or not _valid_sha256(dataset.prepared_flat_sha256)
        or not _valid_sha256(dataset.manifest_sha256)
        or not _valid_sha256(dataset.quality_report_sha256)
    ):
        return False, "dataset_metadata_incomplete"
    expected_uris = (
        (dataset.storage_uri, upload.original_object_key),
        (dataset.flat_storage_uri, upload.prepared_flat_object_key),
        (dataset.manifest_storage_uri, upload.manifest_object_key),
        (dataset.quality_report_storage_uri, upload.quality_report_object_key),
    )
    try:
        if any(uri != storage.storage_uri(key) for uri, key in expected_uris):
            return False, "dataset_storage_uri_mismatch"
    except StorageError:
        return False, "storage_unavailable"
    objects = (
        (
            upload.original_object_key,
            upload.expected_sha256,
            upload.expected_size_bytes,
        ),
        (
            upload.prepared_flat_object_key,
            cast(str, dataset.prepared_flat_sha256),
            dataset.prepared_flat_size_bytes,
        ),
        (upload.manifest_object_key, cast(str, dataset.manifest_sha256), None),
        (
            upload.quality_report_object_key,
            cast(str, dataset.quality_report_sha256),
            None,
        ),
    )
    for object_key, expected_sha256, expected_size in objects:
        matches, code = await _object_matches(
            storage,
            object_key,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            chunk_size=chunk_size,
        )
        if not matches:
            return False, code
    return True, "verified"


async def _verify_artifact_session(
    session: AsyncSession,
    upload: ArtifactUploadSession,
    storage: StorageAdapter,
    *,
    chunk_size: int,
) -> tuple[bool, str]:
    if upload.status in {"pending", "finalizing"}:
        return False, "active_session"
    if upload.status in {"failed", "expired"}:
        return await _object_matches(
            storage,
            upload.temporary_object_key,
            expected_sha256=upload.expected_sha256,
            expected_size=upload.expected_size_bytes,
            chunk_size=chunk_size,
        )
    if upload.status != "completed" or upload.artifact_id is None:
        return False, "artifact_metadata_incomplete"
    artifact = await session.get(Artifact, upload.artifact_id)
    if artifact is None:
        return False, "artifact_missing"
    if (
        artifact.job_id != upload.job_id
        or artifact.attempt_id != upload.attempt_id
        or artifact.artifact_type != upload.artifact_type
        or artifact.size_bytes != upload.expected_size_bytes
        or artifact.sha256 != upload.expected_sha256
    ):
        return False, "artifact_metadata_mismatch"
    try:
        if artifact.storage_uri != storage.storage_uri(upload.canonical_object_key):
            return False, "artifact_storage_uri_mismatch"
    except StorageError:
        return False, "storage_unavailable"
    return await _object_matches(
        storage,
        upload.canonical_object_key,
        expected_sha256=artifact.sha256,
        expected_size=artifact.size_bytes,
        chunk_size=chunk_size,
    )


async def _candidate_ids(
    database: Database,
    *,
    kind: StorageAdoptionKind,
    session_ids: tuple[str, ...],
    limit: int,
) -> list[tuple[StorageSessionKind, str]]:
    if kind == "all":
        kinds: tuple[StorageSessionKind, ...] = ("dataset", "artifact")
    elif kind == "dataset":
        kinds = ("dataset",)
    else:
        kinds = ("artifact",)
    candidates: list[tuple[StorageSessionKind, str]] = []
    async with database.session_factory() as session:
        for current_kind in kinds:
            model = DatasetUploadSession if current_kind == "dataset" else ArtifactUploadSession
            query = select(model.id)
            if session_ids:
                query = query.where(model.id.in_(session_ids))
            else:
                query = query.where(
                    model.storage_namespace_sha256 == UNBOUND_STORAGE_NAMESPACE_SHA256
                )
            rows = list(
                (
                    await session.scalars(
                        query.order_by(model.created_at.asc(), model.id.asc()).limit(
                            limit - len(candidates)
                        )
                    )
                ).all()
            )
            candidates.extend((current_kind, row) for row in rows)
            if len(candidates) >= limit:
                break
    return candidates


async def _adopt_one(
    database: Database,
    storage: StorageAdapter,
    *,
    kind: StorageSessionKind,
    session_id: str,
    chunk_size: int,
    dry_run: bool,
) -> StorageAdoptionItem:
    async with database.session_factory() as session:
        upload: DatasetUploadSession | ArtifactUploadSession | None
        if kind == "dataset":
            upload = await session.scalar(
                select(DatasetUploadSession)
                .where(DatasetUploadSession.id == session_id)
                .with_for_update()
            )
        else:
            upload = await session.scalar(
                select(ArtifactUploadSession)
                .where(ArtifactUploadSession.id == session_id)
                .with_for_update()
            )
        if upload is None:
            return StorageAdoptionItem(kind, session_id, "not_found", "session_not_found")
        if upload.status in {"pending", "finalizing"}:
            add_audit_event(
                session,
                actor_type="operator",
                action="storage_namespace.adoption_rejected",
                resource_type=f"{kind}_upload_session",
                resource_id=session_id,
                details={
                    "dry_run": dry_run,
                    "failure_code": "active_session",
                    "target_storage_backend": storage.backend,
                    "target_storage_namespace_sha256": storage.namespace_fingerprint,
                },
            )
            await session.commit()
            return StorageAdoptionItem(kind, session_id, "rejected", "active_session")
        if upload.storage_namespace_sha256 != UNBOUND_STORAGE_NAMESPACE_SHA256:
            if (
                upload.storage_backend == storage.backend
                and upload.storage_namespace_sha256 == storage.namespace_fingerprint
            ):
                add_audit_event(
                    session,
                    actor_type="operator",
                    action="storage_namespace.adoption_verified",
                    resource_type=f"{kind}_upload_session",
                    resource_id=session_id,
                    details={
                        "dry_run": dry_run,
                        "result_code": "already_bound",
                        "target_storage_backend": storage.backend,
                        "target_storage_namespace_sha256": storage.namespace_fingerprint,
                    },
                )
                await session.commit()
                return StorageAdoptionItem(
                    kind,
                    session_id,
                    "verified",
                    "already_bound",
                )
            add_audit_event(
                session,
                actor_type="operator",
                action="storage_namespace.adoption_rejected",
                resource_type=f"{kind}_upload_session",
                resource_id=session_id,
                details={
                    "dry_run": dry_run,
                    "failure_code": "bound_to_other_namespace",
                    "target_storage_backend": storage.backend,
                    "target_storage_namespace_sha256": storage.namespace_fingerprint,
                },
            )
            await session.commit()
            return StorageAdoptionItem(kind, session_id, "rejected", "bound_to_other_namespace")
        if upload.storage_backend != storage.backend:
            verified, code = False, "storage_backend_mismatch"
        elif kind == "dataset":
            verified, code = await _verify_dataset_session(
                session,
                cast(DatasetUploadSession, upload),
                storage,
                chunk_size=chunk_size,
            )
        else:
            verified, code = await _verify_artifact_session(
                session,
                cast(ArtifactUploadSession, upload),
                storage,
                chunk_size=chunk_size,
            )
        resource_type = f"{kind}_upload_session"
        if not verified:
            add_audit_event(
                session,
                actor_type="operator",
                action="storage_namespace.adoption_rejected",
                resource_type=resource_type,
                resource_id=session_id,
                details={
                    "dry_run": dry_run,
                    "failure_code": code,
                    "target_storage_backend": storage.backend,
                    "target_storage_namespace_sha256": storage.namespace_fingerprint,
                },
            )
            await session.commit()
            return StorageAdoptionItem(kind, session_id, "rejected", code)
        if dry_run:
            add_audit_event(
                session,
                actor_type="operator",
                action="storage_namespace.adoption_verified",
                resource_type=resource_type,
                resource_id=session_id,
                details={
                    "dry_run": True,
                    "target_storage_backend": storage.backend,
                    "target_storage_namespace_sha256": storage.namespace_fingerprint,
                },
            )
            await session.commit()
            return StorageAdoptionItem(kind, session_id, "verified", "verified")
        upload.storage_namespace_sha256 = storage.namespace_fingerprint
        add_audit_event(
            session,
            actor_type="operator",
            action="storage_namespace.adopted",
            resource_type=resource_type,
            resource_id=session_id,
            details={
                "dry_run": False,
                "target_storage_backend": storage.backend,
                "target_storage_namespace_sha256": storage.namespace_fingerprint,
            },
        )
        await session.commit()
        return StorageAdoptionItem(kind, session_id, "adopted", "verified")


async def adopt_storage_sessions(
    database: Database,
    storage: StorageAdapter,
    *,
    kind: StorageAdoptionKind,
    session_ids: tuple[str, ...] = (),
    limit: int = 100,
    chunk_size: int = 1024**2,
    dry_run: bool = True,
) -> StorageAdoptionResult:
    if limit < 1 or limit > 500:
        raise ValueError("storage adoption limit must be between 1 and 500")
    if session_ids and len(session_ids) > limit:
        raise ValueError("session ID count exceeds the storage adoption limit")
    if session_ids and kind == "all":
        raise ValueError("explicit session IDs require dataset or artifact kind")
    candidates = await _candidate_ids(
        database,
        kind=kind,
        session_ids=session_ids,
        limit=limit,
    )
    items = [
        await _adopt_one(
            database,
            storage,
            kind=candidate_kind,
            session_id=session_id,
            chunk_size=chunk_size,
            dry_run=dry_run,
        )
        for candidate_kind, session_id in candidates
    ]
    if session_ids:
        found = {item.session_id for item in items}
        items.extend(
            StorageAdoptionItem(
                cast(StorageSessionKind, kind),
                session_id,
                "not_found",
                "session_not_found",
            )
            for session_id in session_ids
            if session_id not in found
        )
    async with database.session_factory() as session:
        dataset_remaining = await session.scalar(
            select(func.count())
            .select_from(DatasetUploadSession)
            .where(
                DatasetUploadSession.storage_namespace_sha256 == UNBOUND_STORAGE_NAMESPACE_SHA256
            )
        )
        artifact_remaining = await session.scalar(
            select(func.count())
            .select_from(ArtifactUploadSession)
            .where(
                ArtifactUploadSession.storage_namespace_sha256 == UNBOUND_STORAGE_NAMESPACE_SHA256
            )
        )
    return StorageAdoptionResult(
        dry_run=dry_run,
        requested_kind=kind,
        target_storage_backend=storage.backend,
        target_storage_namespace_sha256=storage.namespace_fingerprint,
        examined=len(items),
        adopted=sum(item.outcome == "adopted" for item in items),
        verified=sum(item.outcome == "verified" for item in items),
        rejected=sum(item.outcome in {"rejected", "not_found"} for item in items),
        remaining_unbound=int(dataset_remaining or 0) + int(artifact_remaining or 0),
        items=items,
    )
