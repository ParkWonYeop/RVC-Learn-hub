from __future__ import annotations

import asyncio
import hashlib
import io
import json
import struct
import wave
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from rvc_manager_api.models import (
    AuditEvent,
    Dataset,
    DatasetUploadSession,
    JobLease,
    User,
)
from rvc_manager_api.schemas import DatasetPcmLoudnessRead, DatasetPcmQualityRead
from rvc_manager_api.security import hash_password
from rvc_manager_api.services.storage_adoption import adopt_storage_sessions
from rvc_manager_api.storage import (
    UNBOUND_STORAGE_NAMESPACE_SHA256,
    LocalStorageAdapter,
    StorageError,
)
from rvc_orchestrator_contracts import utc_now


def test_dataset_pcm_quality_schema_rejects_bool_partial_and_non_finite_values() -> None:
    valid: dict[str, object] = {
        "algorithm": "pcm-sample-weighted-v1",
        "validated_file_count": 1,
        "sample_count": 4,
        "clipping_ratio": 0.0,
        "silence_ratio": 0.5,
        "rms_ratio": 0.25,
        "silence_threshold_dbfs": -50.0,
    }
    DatasetPcmQualityRead.model_validate(valid)
    for invalid in (
        {**valid, "sample_count": True},
        {**valid, "clipping_ratio": float("nan")},
        {**valid, "silence_ratio": float("inf")},
        {**valid, "rms_ratio": -0.01},
        {key: value for key, value in valid.items() if key != "sample_count"},
    ):
        with pytest.raises(ValueError):
            DatasetPcmQualityRead.model_validate(invalid)


def test_dataset_pcm_loudness_schema_requires_exact_finite_state() -> None:
    valid: dict[str, object] = {
        "algorithm": "itu-r-bs1770-4-mono-stereo-v1",
        "scope": "global-gate-over-per-file-complete-blocks-v1",
        "block_duration_ms": 400,
        "block_overlap_percent": 75,
        "absolute_gate_lufs": -70.0,
        "relative_gate_lu": -10.0,
        "analyzed_file_count": 1,
        "block_count": 7,
        "gated_block_count": 7,
        "integrated_lufs": -23.0,
        "unavailable_reason": None,
    }
    DatasetPcmLoudnessRead.model_validate(valid)
    for invalid in (
        {**valid, "integrated_lufs": float("nan")},
        {**valid, "integrated_lufs": float("inf")},
        {**valid, "absolute_gate_lufs": -69.0},
        {**valid, "gated_block_count": 8},
        {**valid, "integrated_lufs": None},
        {**valid, "unavailable_reason": "below_absolute_gate"},
    ):
        with pytest.raises(ValueError):
            DatasetPcmLoudnessRead.model_validate(invalid)

    DatasetPcmLoudnessRead.model_validate(
        {
            **valid,
            "block_count": 0,
            "gated_block_count": 0,
            "integrated_lufs": None,
            "unavailable_reason": "insufficient_duration",
        }
    )


USER_PASSWORD = "dataset-owner-password-1234"


def pcm_wav_bytes(samples: list[int], *, sample_rate: int = 8_000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, mode="wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return output.getvalue()


def zip_bytes(members: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in members:
            archive.writestr(name, content)
    return output.getvalue()


async def seed_user(app: FastAPI, email: str) -> User:
    password_hash = await run_in_threadpool(hash_password, USER_PASSWORD)
    async with app.state.database.session_factory() as session:
        user = User(
            email=email,
            password_hash=password_hash,
            role="user",
            disabled=False,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def login(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": USER_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def upload_payload(
    content: bytes,
    *,
    filename: str,
    content_type: str,
    idempotency_key: str,
    name: str = "dataset-upload",
    sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "filename": filename,
        "content_type": content_type,
        "size_bytes": len(content),
        "sha256": sha256 or hashlib.sha256(content).hexdigest(),
        "idempotency_key": idempotency_key,
    }


async def initialize(
    client: AsyncClient,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> Response:
    return await client.post(
        "/api/v1/datasets/uploads/init",
        headers=headers,
        json=payload,
    )


async def upload_to_target(
    client: AsyncClient,
    target: dict[str, Any],
    content: bytes,
) -> Response:
    return await client.put(
        target["upload_url"],
        headers=target["upload_headers"],
        content=content,
    )


def storage_files(app: FastAPI, prefix: str) -> list[Path]:
    storage = cast(LocalStorageAdapter, app.state.storage)
    root = storage.root / prefix
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


async def test_slow_local_dataset_put_renews_generation_heartbeat(
    app: FastAPI,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await seed_user(app, "dataset-slow-put@example.test")
    headers = await login(client, owner.email)
    content = pcm_wav_bytes([0, 1, -1, 0])
    initialized = await initialize(
        client,
        headers,
        upload_payload(
            content,
            filename="slow-put.wav",
            content_type="audio/wav",
            idempotency_key="dataset-slow-put-heartbeat-0001",
        ),
    )
    target = initialized.json()
    storage = cast(LocalStorageAdapter, app.state.storage)
    original_write = storage.write_upload_stream
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_write(
        object_key: str,
        chunks: object,
        *,
        expected_size: int,
    ) -> None:
        started.set()
        await release.wait()
        await original_write(object_key, chunks, expected_size=expected_size)

    monkeypatch.setattr(storage, "write_upload_stream", slow_write)
    app.state.settings.dataset_upload_write_heartbeat_seconds = 0.01
    put_task = asyncio.create_task(upload_to_target(client, target, content))
    await asyncio.wait_for(started.wait(), timeout=2)
    async with app.state.database.session_factory() as session:
        initial = await session.get(DatasetUploadSession, target["upload_session_id"])
        assert initial is not None and initial.upload_heartbeat_at is not None
        heartbeat = initial.upload_heartbeat_at
        token = initial.upload_write_token
        generation = initial.generation
    await asyncio.sleep(0.05)
    async with app.state.database.session_factory() as session:
        renewed = await session.get(DatasetUploadSession, target["upload_session_id"])
        assert renewed is not None and renewed.upload_heartbeat_at is not None
        assert renewed.upload_heartbeat_at > heartbeat
        assert renewed.upload_write_token == token
        assert renewed.generation == generation
    release.set()
    response = await asyncio.wait_for(put_task, timeout=2)
    assert response.status_code == 204, response.text


async def test_slow_local_dataset_put_deadline_joins_and_removes_partial(
    app: FastAPI,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await seed_user(app, "dataset-put-deadline@example.test")
    headers = await login(client, owner.email)
    content = pcm_wav_bytes([0, 2, -2, 0])
    initialized = await initialize(
        client,
        headers,
        upload_payload(
            content,
            filename="deadline.wav",
            content_type="audio/wav",
            idempotency_key="dataset-put-deadline-0001",
        ),
    )
    target = initialized.json()
    async with app.state.database.session_factory() as session:
        upload = await session.get(DatasetUploadSession, target["upload_session_id"])
        assert upload is not None
        upload.expires_at = utc_now() + timedelta(seconds=0.1)
        await session.commit()
    storage = cast(LocalStorageAdapter, app.state.storage)
    original_write = storage.write_upload_stream
    started = asyncio.Event()
    operation_finished = asyncio.Event()

    async def slow_body_write(
        object_key: str,
        chunks: object,
        *,
        expected_size: int,
    ) -> None:
        async def delayed_chunks():
            try:
                async for chunk in chunks:  # type: ignore[union-attr]
                    started.set()
                    await asyncio.sleep(10)
                    yield chunk
            finally:
                operation_finished.set()

        await original_write(object_key, delayed_chunks(), expected_size=expected_size)

    monkeypatch.setattr(storage, "write_upload_stream", slow_body_write)
    app.state.settings.dataset_upload_write_heartbeat_seconds = 0.01
    put_task = asyncio.create_task(upload_to_target(client, target, content))
    await asyncio.wait_for(started.wait(), timeout=2)
    response = await asyncio.wait_for(put_task, timeout=2)
    assert response.status_code == 408
    assert response.json()["detail"] == "dataset upload deadline exceeded"
    assert operation_finished.is_set()
    async with app.state.database.session_factory() as session:
        upload = await session.get(DatasetUploadSession, target["upload_session_id"])
        dataset = await session.get(Dataset, target["dataset_id"])
        assert upload is not None and dataset is not None
        staging = storage._path(upload.temporary_object_key)
        assert upload.status == "expired"
        assert upload.failure_code == "upload_write_deadline_exceeded"
        assert upload.upload_write_token is None
        assert dataset.status == "upload_pending"
    assert not staging.exists()
    assert list(staging.parent.glob(f".{staging.name}.*.part")) == []


async def test_late_dataset_writer_cannot_delete_replacement_generation_key(
    app: FastAPI,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await seed_user(app, "dataset-late-writer@example.test")
    headers = await login(client, owner.email)
    content = pcm_wav_bytes([0, 3, -3, 0])
    payload = upload_payload(
        content,
        filename="late-writer.wav",
        content_type="audio/wav",
        idempotency_key="dataset-late-writer-0001",
    )
    initialized = await initialize(client, headers, payload)
    old_target = initialized.json()
    storage = cast(LocalStorageAdapter, app.state.storage)
    original_write = storage.write_upload_stream
    started = asyncio.Event()
    release = asyncio.Event()
    operation_finished = asyncio.Event()

    async def late_write(
        object_key: str,
        chunks: object,
        *,
        expected_size: int,
    ) -> None:
        started.set()
        await release.wait()
        await original_write(object_key, chunks, expected_size=expected_size)
        operation_finished.set()

    monkeypatch.setattr(storage, "write_upload_stream", late_write)
    old_put_task = asyncio.create_task(upload_to_target(client, old_target, content))
    await asyncio.wait_for(started.wait(), timeout=2)
    async with app.state.database.session_factory() as session:
        dataset = await session.scalar(
            select(Dataset).where(Dataset.id == old_target["dataset_id"]).with_for_update()
        )
        old_upload = await session.scalar(
            select(DatasetUploadSession)
            .where(DatasetUploadSession.id == old_target["upload_session_id"])
            .with_for_update()
        )
        assert dataset is not None and old_upload is not None
        assert old_upload.upload_write_token is not None
        old_upload.status = "expired"
        old_upload.upload_write_token = None
        old_upload.upload_heartbeat_at = None
        old_upload.failure_code = "staging_cleanup_pending"
        dataset.status = "upload_pending"
        await session.commit()
    replacement = await initialize(client, headers, payload)
    assert replacement.status_code == 201
    replacement_target = replacement.json()
    assert replacement_target["upload_session_id"] != old_target["upload_session_id"]
    async with app.state.database.session_factory() as session:
        old_upload = await session.get(
            DatasetUploadSession,
            old_target["upload_session_id"],
        )
        new_upload = await session.get(
            DatasetUploadSession,
            replacement_target["upload_session_id"],
        )
        assert old_upload is not None and new_upload is not None
        old_staging = storage._path(old_upload.temporary_object_key)
        new_staging = storage._path(new_upload.temporary_object_key)
        assert new_upload.generation == old_upload.generation + 1
        assert new_upload.temporary_object_key != old_upload.temporary_object_key
        assert new_upload.original_object_key != old_upload.original_object_key
    new_staging.parent.mkdir(parents=True, exist_ok=True)
    new_staging.write_bytes(b"replacement-generation-sentinel")
    release.set()
    old_response = await asyncio.wait_for(old_put_task, timeout=2)
    assert old_response.status_code == 409
    assert old_response.json()["detail"] == "dataset upload write lease was lost"
    assert operation_finished.is_set()
    assert not old_staging.exists()
    assert new_staging.read_bytes() == b"replacement-generation-sentinel"


async def test_dataset_local_upload_openapi_lists_exact_operational_errors(
    client: AsyncClient,
) -> None:
    document = (await client.get("/openapi.json")).json()
    responses = document["paths"]["/api/v1/storage/dataset-uploads/{upload_session_id}"]["put"][
        "responses"
    ]
    assert {
        "204",
        "400",
        "401",
        "404",
        "408",
        "409",
        "410",
        "411",
        "413",
        "422",
        "503",
    }.issubset(responses)


async def test_owner_upload_finalize_publishes_verified_snapshot_without_uri_leak(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await seed_user(app, "dataset-owner@example.test")
    other = await seed_user(app, "dataset-other@example.test")
    owner_headers = await login(client, owner.email)
    other_headers = await login(client, other.email)
    wav = pcm_wav_bytes([0, 1_000, -1_000, 0])
    content = zip_bytes(
        [
            ("nested/speaker.wav", wav),
            ("docs/readme.txt", b"ignored"),
        ]
    )
    payload = upload_payload(
        content,
        filename="speaker-dataset.zip",
        content_type="application/zip",
        idempotency_key="dataset-owner-flow-0001",
    )

    initialized = await initialize(client, owner_headers, payload)
    assert initialized.status_code == 201, initialized.text
    target = initialized.json()
    dataset_id = target["dataset_id"]
    upload_id = target["upload_session_id"]
    assert target["method"] == "PUT"
    assert target["upload_url"].endswith(f"/dataset-uploads/{upload_id}")
    assert payload["filename"] not in target["upload_url"]
    assert target["upload_headers"]["Content-Length"] == str(len(content))
    assert initialized.headers["Cache-Control"] == "no-store"

    concealed = await client.post(
        f"/api/v1/datasets/uploads/{upload_id}/finalize",
        headers=other_headers,
    )
    assert concealed.status_code == 404
    other_detail = await client.get(
        f"/api/v1/datasets/{dataset_id}",
        headers=other_headers,
    )
    assert other_detail.status_code == 404
    other_delete = await client.delete(
        f"/api/v1/datasets/{dataset_id}",
        headers=other_headers,
    )
    assert other_delete.status_code == 404

    pending = await client.get(f"/api/v1/datasets/{dataset_id}", headers=owner_headers)
    assert pending.status_code == 200
    assert pending.json()["status"] == "upload_pending"
    assert "storage_uri" not in pending.json()
    assert "flat_storage_uri" not in pending.json()
    active_delete = await client.delete(
        f"/api/v1/datasets/{dataset_id}",
        headers=owner_headers,
    )
    assert active_delete.status_code == 409

    missing_token_headers = dict(target["upload_headers"])
    missing_token_headers.pop("X-RVC-Upload-Token")
    unauthorized_put = await client.put(
        target["upload_url"],
        headers=missing_token_headers,
        content=content,
    )
    assert unauthorized_put.status_code == 401
    uploaded = await upload_to_target(client, target, content)
    assert uploaded.status_code == 204, uploaded.text

    finalized = await client.post(
        f"/api/v1/datasets/uploads/{upload_id}/finalize",
        headers=owner_headers,
    )
    assert finalized.status_code == 200, finalized.text
    dataset = finalized.json()
    assert dataset["id"] == dataset_id
    assert dataset["status"] == "ready"
    assert dataset["is_usable"] is True
    assert dataset["original_sha256"] == hashlib.sha256(content).hexdigest()
    assert dataset["file_count"] == 1
    assert dataset["sample_rate"] == 8_000
    assert dataset["decoder_pending_count"] == 0
    assert dataset["source_file_entry_count"] == 2
    assert dataset["skipped_file_count"] == 1
    assert dataset["rejected_file_count"] == 0
    assert dataset["duplicate_file_count"] == 0
    assert dataset["pcm_quality"]["algorithm"] == "pcm-sample-weighted-v1"
    assert dataset["pcm_quality"]["validated_file_count"] == 1
    assert dataset["pcm_quality"]["sample_count"] == 4
    assert dataset["pcm_quality"]["clipping_ratio"] == 0
    assert dataset["pcm_quality"]["silence_ratio"] == 0.5
    assert dataset["pcm_quality"]["rms_ratio"] == pytest.approx((2 * 1_000**2 / 4) ** 0.5 / 32_768)
    assert dataset["pcm_quality"]["silence_threshold_dbfs"] == -50.0
    assert dataset["pcm_quality"]["loudness"] == {
        "algorithm": "itu-r-bs1770-4-mono-stereo-v1",
        "scope": "global-gate-over-per-file-complete-blocks-v1",
        "block_duration_ms": 400,
        "block_overlap_percent": 75,
        "absolute_gate_lufs": -70.0,
        "relative_gate_lu": -10.0,
        "analyzed_file_count": 1,
        "block_count": 0,
        "gated_block_count": 0,
        "integrated_lufs": None,
        "unavailable_reason": "insufficient_duration",
    }
    assert "quality_report_json" not in dataset
    assert "nested/speaker.wav" not in json.dumps(dataset)
    assert "docs/readme.txt" not in json.dumps(dataset)
    for private_field in (
        "storage_uri",
        "flat_storage_uri",
        "manifest_storage_uri",
        "quality_report_storage_uri",
    ):
        assert private_field not in dataset

    finalized_again = await client.post(
        f"/api/v1/datasets/uploads/{upload_id}/finalize",
        headers=owner_headers,
    )
    assert finalized_again.status_code == 200
    assert finalized_again.json() == dataset
    repeated_init = await initialize(client, owner_headers, payload)
    assert repeated_init.status_code == 201
    assert repeated_init.json()["status"] == "completed"
    assert repeated_init.json()["dataset"]["id"] == dataset_id
    assert repeated_init.json()["upload_url"] is None

    listed = await client.get("/api/v1/datasets", headers=owner_headers)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [dataset_id]
    assert listed.headers["Cache-Control"] == "private, no-store"
    assert (await client.get("/api/v1/datasets", headers=other_headers)).json()["items"] == []
    for route in ("validate", "prepare-flat"):
        prepared = await client.post(
            f"/api/v1/datasets/{dataset_id}/{route}",
            headers=owner_headers,
        )
        assert prepared.status_code == 200
        assert prepared.json()["prepared_flat_sha256"] == dataset["prepared_flat_sha256"]

    async with app.state.database.session_factory() as session:
        upload = await session.get(DatasetUploadSession, upload_id)
        persisted = await session.get(Dataset, dataset_id)
        assert upload is not None
        assert persisted is not None
        assert upload.status == "completed"
        assert upload.finalization_token is None
        assert persisted.storage_uri.startswith("local:///")
        assert persisted.flat_storage_uri is not None
        assert persisted.pcm_loudness_algorithm == "itu-r-bs1770-4-mono-stereo-v1"
        assert persisted.pcm_loudness_analyzed_file_count == 1
        assert persisted.pcm_loudness_block_count == 0
        assert persisted.pcm_loudness_gated_block_count == 0
        assert persisted.pcm_integrated_lufs is None
        assert persisted.pcm_loudness_unavailable_reason == "insufficient_duration"
        audit_actions = set(
            await session.scalars(
                select(AuditEvent.action).where(AuditEvent.resource_id == dataset_id)
            )
        )
        assert {"dataset.upload_initialized", "dataset.finalized"}.issubset(audit_actions)
        prepared_path = cast(LocalStorageAdapter, app.state.storage)._path(
            upload.prepared_flat_object_key
        )
        manifest_path = cast(LocalStorageAdapter, app.state.storage)._path(
            upload.manifest_object_key
        )
        report_path = cast(LocalStorageAdapter, app.state.storage)._path(
            upload.quality_report_object_key
        )
    assert prepared_path.is_file()
    assert manifest_path.is_file()
    assert report_path.is_file()
    ingestion_root = Path(app.state.settings.dataset_ingestion_root)
    ingestion_mode, ingestion_children = await run_in_threadpool(
        lambda: (ingestion_root.stat().st_mode & 0o777, list(ingestion_root.iterdir()))
    )
    assert ingestion_mode == 0o700
    assert ingestion_children == []
    with zipfile.ZipFile(prepared_path) as archive:
        assert archive.namelist() == ["prepared_flat/000001.wav"]
        assert archive.read("prepared_flat/000001.wav") == wav

    experiment = await client.post(
        "/api/v1/experiments",
        headers=owner_headers,
        json={"name": "delete-race-guard", "dataset_id": dataset_id},
    )
    assert experiment.status_code == 201, experiment.text
    guarded_delete = await client.delete(
        f"/api/v1/datasets/{dataset_id}",
        headers=owner_headers,
    )
    assert guarded_delete.status_code == 409
    assert prepared_path.is_file()


async def test_non_wav_is_decoder_pending_and_unreferenced_delete_cleans_objects(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await seed_user(app, "decoder-owner@example.test")
    owner_headers = await login(client, owner.email)
    content = b"ID3\x04\x00\x00decoder-pending-audio"
    payload = upload_payload(
        content,
        filename="speaker.mp3",
        content_type="audio/mpeg",
        idempotency_key="dataset-decoder-pending-0001",
    )
    initialized = await initialize(client, owner_headers, payload)
    assert initialized.status_code == 201
    target = initialized.json()
    assert (await upload_to_target(client, target, content)).status_code == 204
    finalized = await client.post(
        f"/api/v1/datasets/uploads/{target['upload_session_id']}/finalize",
        headers=owner_headers,
    )
    assert finalized.status_code == 200, finalized.text
    body = finalized.json()
    assert body["status"] == "decoder_pending"
    assert body["is_usable"] is False
    assert body["decoder_pending_count"] == 1
    assert body["duration_sec"] is None
    assert body["pcm_quality"] is None
    rejected_experiment = await client.post(
        "/api/v1/experiments",
        headers=owner_headers,
        json={"name": "must-not-run", "dataset_id": body["id"]},
    )
    assert rejected_experiment.status_code == 409

    verified_files = storage_files(app, f"datasets/verified/{body['id']}")
    assert len(verified_files) == 4
    deleted = await client.delete(
        f"/api/v1/datasets/{body['id']}",
        headers=owner_headers,
    )
    assert deleted.status_code == 204, deleted.text
    assert storage_files(app, f"datasets/verified/{body['id']}") == []
    assert (
        await client.get(f"/api/v1/datasets/{body['id']}", headers=owner_headers)
    ).status_code == 404


async def test_idempotency_quota_malicious_archive_and_checksum_fail_closed(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await seed_user(app, "unsafe-owner@example.test")
    owner_headers = await login(client, owner.email)
    app.state.settings.dataset_owner_max_sessions = 1
    malicious = zip_bytes([("../escape.wav", pcm_wav_bytes([0, 1]))])
    payload = upload_payload(
        malicious,
        filename="unsafe.zip",
        content_type="application/zip",
        idempotency_key="dataset-malicious-archive-0001",
    )
    initialized = await initialize(client, owner_headers, payload)
    assert initialized.status_code == 201
    target = initialized.json()

    conflict_payload = dict(payload)
    conflict_payload["name"] = "changed-name"
    assert (await initialize(client, owner_headers, conflict_payload)).status_code == 409
    quota_payload = upload_payload(
        pcm_wav_bytes([0, 2]),
        filename="second.wav",
        content_type="audio/wav",
        idempotency_key="dataset-owner-quota-0002",
    )
    quota = await initialize(client, owner_headers, quota_payload)
    assert quota.status_code == 409
    assert "quota" in quota.json()["detail"]

    assert (await upload_to_target(client, target, malicious)).status_code == 204
    failed = await client.post(
        f"/api/v1/datasets/uploads/{target['upload_session_id']}/finalize",
        headers=owner_headers,
    )
    assert failed.status_code == 422, failed.text
    failed_dataset = await client.get(
        f"/api/v1/datasets/{target['dataset_id']}",
        headers=owner_headers,
    )
    assert failed_dataset.json()["status"] == "failed"
    assert failed_dataset.json()["failure_code"] == "unsafe_archive"
    assert failed_dataset.json()["retryable"] is False
    assert storage_files(app, f"datasets/verified/{target['dataset_id']}") == []
    assert not (Path(app.state.settings.dataset_ingestion_root).parent / "escape.wav").exists()

    content = pcm_wav_bytes([0, 3, -3])
    wrong_sha = hashlib.sha256(b"same-size-wrong-checksum"[: len(content)]).hexdigest()
    checksum_payload = upload_payload(
        content,
        filename="checksum.wav",
        content_type="audio/wav",
        idempotency_key="dataset-checksum-mismatch-0001",
        sha256=wrong_sha,
    )
    checksum_init = await initialize(client, owner_headers, checksum_payload)
    assert checksum_init.status_code == 201
    checksum_target = checksum_init.json()
    assert (await upload_to_target(client, checksum_target, content)).status_code == 204
    checksum_failed = await client.post(
        f"/api/v1/datasets/uploads/{checksum_target['upload_session_id']}/finalize",
        headers=owner_headers,
    )
    assert checksum_failed.status_code == 422
    checksum_detail = await client.get(
        f"/api/v1/datasets/{checksum_target['dataset_id']}",
        headers=owner_headers,
    )
    assert checksum_detail.json()["failure_code"] == "sha256_mismatch"
    assert storage_files(app, f"datasets/verified/{checksum_target['dataset_id']}") == []

    disguised = b"this-is-not-a-wave-container"
    signature_payload = upload_payload(
        disguised,
        filename="disguised.wav",
        content_type="audio/wav",
        idempotency_key="dataset-signature-mismatch-0001",
    )
    signature_init = await initialize(client, owner_headers, signature_payload)
    assert signature_init.status_code == 201
    signature_target = signature_init.json()
    assert (await upload_to_target(client, signature_target, disguised)).status_code == 204
    signature_failed = await client.post(
        f"/api/v1/datasets/uploads/{signature_target['upload_session_id']}/finalize",
        headers=owner_headers,
    )
    assert signature_failed.status_code == 422
    signature_detail = await client.get(
        f"/api/v1/datasets/{signature_target['dataset_id']}",
        headers=owner_headers,
    )
    assert signature_detail.json()["failure_code"] == "content_signature_mismatch"
    assert storage_files(app, f"datasets/verified/{signature_target['dataset_id']}") == []


async def test_publish_failure_cleans_partial_objects_and_finalize_retries(
    app: FastAPI,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await seed_user(app, "retry-owner@example.test")
    owner_headers = await login(client, owner.email)
    content = pcm_wav_bytes([0, 100, -100, 0])
    payload = upload_payload(
        content,
        filename="retry.wav",
        content_type="audio/wav",
        idempotency_key="dataset-publish-retry-0001",
    )
    initialized = await initialize(client, owner_headers, payload)
    target = initialized.json()
    assert (await upload_to_target(client, target, content)).status_code == 204

    storage = cast(LocalStorageAdapter, app.state.storage)
    original_store = storage.store_verified_file
    calls = 0

    async def flaky_store(
        object_key: str,
        source: Path,
        *,
        content_type: str,
        sha256: str,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StorageError("injected publish failure")
        await original_store(
            object_key,
            source,
            content_type=content_type,
            sha256=sha256,
        )

    monkeypatch.setattr(storage, "store_verified_file", flaky_store)
    failed = await client.post(
        f"/api/v1/datasets/uploads/{target['upload_session_id']}/finalize",
        headers=owner_headers,
    )
    assert failed.status_code == 503, failed.text
    assert storage_files(app, f"datasets/verified/{target['dataset_id']}") == []
    async with app.state.database.session_factory() as session:
        upload = await session.get(DatasetUploadSession, target["upload_session_id"])
        assert upload is not None
        assert upload.status == "pending"
        assert upload.failure_code == "dataset_publish_failed"
        assert storage._path(upload.temporary_object_key).is_file()

    monkeypatch.setattr(storage, "store_verified_file", original_store)
    retried = await client.post(
        f"/api/v1/datasets/uploads/{target['upload_session_id']}/finalize",
        headers=owner_headers,
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "ready"
    assert len(storage_files(app, f"datasets/verified/{target['dataset_id']}")) == 4


async def test_slow_dataset_finalizer_renews_heartbeat_and_is_not_fenced(
    app: FastAPI,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rvc_manager_api.routers import datasets as dataset_router

    owner = await seed_user(app, "dataset-slow-finalizer@example.test")
    headers = await login(client, owner.email)
    content = pcm_wav_bytes([0, 8, -8, 0])
    payload = upload_payload(
        content,
        filename="slow-finalizer.wav",
        content_type="audio/wav",
        idempotency_key="dataset-slow-finalizer-0001",
    )
    initialized = await initialize(client, headers, payload)
    target = initialized.json()
    assert (await upload_to_target(client, target, content)).status_code == 204

    original_verify = dataset_router.verify_object_to_spool
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_verify(
        storage: object,
        object_key: str,
        *,
        expected_size: int,
        expected_sha256: str,
        settings: object,
    ) -> Path:
        started.set()
        await release.wait()
        return await original_verify(
            storage,  # type: ignore[arg-type]
            object_key,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            settings=settings,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(dataset_router, "verify_object_to_spool", slow_verify)
    app.state.settings.dataset_finalizing_heartbeat_seconds = 0.01
    app.state.settings.dataset_finalizing_stale_seconds = 0.04
    finalize_task = asyncio.create_task(
        client.post(
            f"/api/v1/datasets/uploads/{target['upload_session_id']}/finalize",
            headers=headers,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.sleep(0.08)
    replay = await initialize(client, headers, payload)
    assert replay.status_code == 201, replay.text
    assert replay.json()["status"] == "finalizing"
    assert replay.json()["upload_session_id"] == target["upload_session_id"]
    assert replay.json()["upload_url"] is None
    release.set()
    finalized = await asyncio.wait_for(finalize_task, timeout=3)
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["status"] == "ready"


async def test_stale_dataset_finalizer_cannot_delete_replacement_canonical_generation(
    app: FastAPI,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await seed_user(app, "dataset-stale-finalizer@example.test")
    headers = await login(client, owner.email)
    content = pcm_wav_bytes([0, 9, -9, 0])
    payload = upload_payload(
        content,
        filename="stale-finalizer.wav",
        content_type="audio/wav",
        idempotency_key="dataset-stale-finalizer-0001",
    )
    initialized = await initialize(client, headers, payload)
    old_target = initialized.json()
    assert (await upload_to_target(client, old_target, content)).status_code == 204

    storage = cast(LocalStorageAdapter, app.state.storage)
    original_store = storage.store_verified_file
    old_store_started = asyncio.Event()
    release_old_store = asyncio.Event()
    paused_once = False

    async def pause_old_generation_store(
        object_key: str,
        source: Path,
        *,
        content_type: str,
        sha256: str,
    ) -> None:
        nonlocal paused_once
        if old_target["upload_session_id"] in object_key and not paused_once:
            paused_once = True
            old_store_started.set()
            await release_old_store.wait()
        await original_store(
            object_key,
            source,
            content_type=content_type,
            sha256=sha256,
        )

    monkeypatch.setattr(storage, "store_verified_file", pause_old_generation_store)
    app.state.settings.dataset_finalizing_heartbeat_seconds = 0.01
    old_finalize_task = asyncio.create_task(
        client.post(
            f"/api/v1/datasets/uploads/{old_target['upload_session_id']}/finalize",
            headers=headers,
        )
    )
    await asyncio.wait_for(old_store_started.wait(), timeout=3)

    async with app.state.database.session_factory() as session:
        dataset = await session.scalar(
            select(Dataset).where(Dataset.id == old_target["dataset_id"]).with_for_update()
        )
        old_upload = await session.scalar(
            select(DatasetUploadSession)
            .where(DatasetUploadSession.id == old_target["upload_session_id"])
            .with_for_update()
        )
        assert dataset is not None and old_upload is not None
        assert old_upload.status == "finalizing"
        old_upload.status = "expired"
        old_upload.finalization_token = None
        old_upload.finalization_heartbeat_at = None
        old_upload.failure_code = "stale_finalizing_recovered"
        dataset.status = "upload_pending"
        dataset.failure_code = "stale_finalizing_recovered"
        dataset.retryable = True
        await session.commit()

    replacement = await initialize(client, headers, payload)
    assert replacement.status_code == 201, replacement.text
    replacement_target = replacement.json()
    assert replacement_target["upload_session_id"] != old_target["upload_session_id"]
    assert (await upload_to_target(client, replacement_target, content)).status_code == 204
    replacement_finalized = await client.post(
        f"/api/v1/datasets/uploads/{replacement_target['upload_session_id']}/finalize",
        headers=headers,
    )
    assert replacement_finalized.status_code == 200, replacement_finalized.text

    release_old_store.set()
    old_finalized = await asyncio.wait_for(old_finalize_task, timeout=3)
    assert old_finalized.status_code == 409, old_finalized.text
    assert old_finalized.json()["detail"] == "dataset finalization lease was lost"
    old_prefix = (
        f"datasets/verified/{old_target['dataset_id']}/uploads/{old_target['upload_session_id']}"
    )
    replacement_prefix = (
        f"datasets/verified/{old_target['dataset_id']}/uploads/"
        f"{replacement_target['upload_session_id']}"
    )
    assert storage_files(app, old_prefix) == []
    assert len(storage_files(app, replacement_prefix)) == 4


async def test_cancelled_dataset_finalizer_cleans_owned_canonical_and_retries(
    app: FastAPI,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await seed_user(app, "dataset-cancelled-finalizer@example.test")
    headers = await login(client, owner.email)
    content = pcm_wav_bytes([0, 13, -13, 0])
    payload = upload_payload(
        content,
        filename="cancelled-finalizer.wav",
        content_type="audio/wav",
        idempotency_key="dataset-cancelled-finalizer-0001",
    )
    initialized = await initialize(client, headers, payload)
    target = initialized.json()
    assert (await upload_to_target(client, target, content)).status_code == 204

    storage = cast(LocalStorageAdapter, app.state.storage)
    original_store = storage.store_verified_file
    second_store_started = asyncio.Event()
    never_release = asyncio.Event()
    calls = 0

    async def block_second_publication(
        object_key: str,
        source: Path,
        *,
        content_type: str,
        sha256: str,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            second_store_started.set()
            await never_release.wait()
        await original_store(
            object_key,
            source,
            content_type=content_type,
            sha256=sha256,
        )

    monkeypatch.setattr(storage, "store_verified_file", block_second_publication)
    finalize_task = asyncio.create_task(
        client.post(
            f"/api/v1/datasets/uploads/{target['upload_session_id']}/finalize",
            headers=headers,
        )
    )
    await asyncio.wait_for(second_store_started.wait(), timeout=3)
    finalize_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(finalize_task, timeout=3)

    assert storage_files(app, f"datasets/verified/{target['dataset_id']}") == []
    async with app.state.database.session_factory() as session:
        upload = await session.get(DatasetUploadSession, target["upload_session_id"])
        dataset = await session.get(Dataset, target["dataset_id"])
        assert upload is not None and dataset is not None
        assert upload.status == "pending"
        assert upload.finalization_token is None
        assert upload.failure_code == "finalization_cancelled"
        assert dataset.status == "upload_pending"
        assert dataset.retryable is True
        assert storage._path(upload.temporary_object_key).is_file()

    monkeypatch.setattr(storage, "store_verified_file", original_store)
    retried = await client.post(
        f"/api/v1/datasets/uploads/{target['upload_session_id']}/finalize",
        headers=headers,
    )
    assert retried.status_code == 200, retried.text
    assert len(storage_files(app, f"datasets/verified/{target['dataset_id']}")) == 4


async def test_dataset_finalize_precommit_failure_cleans_canonical_and_retries(
    app: FastAPI,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await seed_user(app, "dataset-finalize-precommit@example.test")
    headers = await login(client, owner.email)
    content = pcm_wav_bytes([0, 14, -14, 0])
    payload = upload_payload(
        content,
        filename="precommit-failure.wav",
        content_type="audio/wav",
        idempotency_key="dataset-finalize-precommit-0001",
    )
    initialized = await initialize(client, headers, payload)
    target = initialized.json()
    assert (await upload_to_target(client, target, content)).status_code == 204

    original_commit = AsyncSession.commit
    armed = True

    async def fail_before_completed_commit(session: AsyncSession) -> None:
        nonlocal armed
        completed_upload = any(
            isinstance(value, DatasetUploadSession) and value.status == "completed"
            for value in session.identity_map.values()
        )
        if armed and completed_upload:
            armed = False
            raise RuntimeError("injected final commit failure")
        await original_commit(session)

    monkeypatch.setattr(AsyncSession, "commit", fail_before_completed_commit)
    failed = await client.post(
        f"/api/v1/datasets/uploads/{target['upload_session_id']}/finalize",
        headers=headers,
    )
    assert failed.status_code == 503, failed.text
    assert failed.json()["detail"] == "dataset finalization commit failed"
    assert storage_files(app, f"datasets/verified/{target['dataset_id']}") == []
    async with app.state.database.session_factory() as session:
        upload = await session.get(DatasetUploadSession, target["upload_session_id"])
        dataset = await session.get(Dataset, target["dataset_id"])
        assert upload is not None and dataset is not None
        assert upload.status == "pending"
        assert upload.failure_code == "dataset_finalize_commit_failed"
        assert upload.finalization_token is None
        assert dataset.status == "upload_pending"
        assert dataset.retryable is True

    retried = await client.post(
        f"/api/v1/datasets/uploads/{target['upload_session_id']}/finalize",
        headers=headers,
    )
    assert retried.status_code == 200, retried.text


async def test_dataset_finalize_ambiguous_commit_preserves_completed_canonical(
    app: FastAPI,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await seed_user(app, "dataset-finalize-ambiguous@example.test")
    headers = await login(client, owner.email)
    content = pcm_wav_bytes([0, 15, -15, 0])
    payload = upload_payload(
        content,
        filename="ambiguous-commit.wav",
        content_type="audio/wav",
        idempotency_key="dataset-finalize-ambiguous-0001",
    )
    initialized = await initialize(client, headers, payload)
    target = initialized.json()
    assert (await upload_to_target(client, target, content)).status_code == 204

    original_commit = AsyncSession.commit
    armed = True

    async def commit_completed_then_report_failure(session: AsyncSession) -> None:
        nonlocal armed
        completed_upload = any(
            isinstance(value, DatasetUploadSession) and value.status == "completed"
            for value in session.identity_map.values()
        )
        if armed and completed_upload:
            armed = False
            await original_commit(session)
            raise RuntimeError("injected ambiguous commit result")
        await original_commit(session)

    monkeypatch.setattr(AsyncSession, "commit", commit_completed_then_report_failure)
    finalized = await client.post(
        f"/api/v1/datasets/uploads/{target['upload_session_id']}/finalize",
        headers=headers,
    )
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["status"] == "ready"
    assert len(storage_files(app, f"datasets/verified/{target['dataset_id']}")) == 4
    async with app.state.database.session_factory() as session:
        upload = await session.get(DatasetUploadSession, target["upload_session_id"])
        dataset = await session.get(Dataset, target["dataset_id"])
        assert upload is not None and dataset is not None
        assert upload.status == "completed"
        assert upload.failure_code is None
        assert dataset.status == "ready"


async def test_upgrade_expired_legacy_upload_replays_without_active_quota_trap(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await seed_user(app, "dataset-upgrade-replay@example.test")
    headers = await login(client, owner.email)
    content = pcm_wav_bytes([0, 10, -10, 0])
    payload = upload_payload(
        content,
        filename="upgrade-replay.wav",
        content_type="audio/wav",
        idempotency_key="dataset-upgrade-replay-0001",
    )
    initialized = await initialize(client, headers, payload)
    legacy_target = initialized.json()
    app.state.settings.dataset_owner_max_sessions = 1

    async with app.state.database.session_factory() as session:
        dataset = await session.scalar(
            select(Dataset).where(Dataset.id == legacy_target["dataset_id"]).with_for_update()
        )
        upload = await session.scalar(
            select(DatasetUploadSession)
            .where(DatasetUploadSession.id == legacy_target["upload_session_id"])
            .with_for_update()
        )
        assert dataset is not None and upload is not None
        legacy_prefix = f"datasets/verified/{dataset.id}"
        upload.original_object_key = f"{legacy_prefix}/original.wav"
        upload.prepared_flat_object_key = f"{legacy_prefix}/prepared_flat.zip"
        upload.manifest_object_key = f"{legacy_prefix}/manifest.json"
        upload.quality_report_object_key = f"{legacy_prefix}/quality_report.json"
        upload.status = "expired"
        upload.failure_code = "upload_fencing_upgrade_required"
        dataset.status = "upload_pending"
        dataset.failure_code = "upload_fencing_upgrade_required"
        dataset.retryable = True
        await session.commit()

    replay = await initialize(client, headers, payload)
    assert replay.status_code == 201, replay.text
    replacement = replay.json()
    assert replacement["upload_session_id"] != legacy_target["upload_session_id"]
    async with app.state.database.session_factory() as session:
        uploads = list(
            (
                await session.scalars(
                    select(DatasetUploadSession)
                    .where(DatasetUploadSession.dataset_id == legacy_target["dataset_id"])
                    .order_by(DatasetUploadSession.generation)
                )
            ).all()
        )
        active_count = await session.scalar(
            select(func.count())
            .select_from(DatasetUploadSession)
            .where(
                DatasetUploadSession.owner_id == owner.id,
                DatasetUploadSession.status.in_(("pending", "finalizing")),
            )
        )
    assert [upload.status for upload in uploads] == ["expired", "pending"]
    assert [upload.generation for upload in uploads] == [1, 2]
    assert active_count == 1
    assert replacement["upload_session_id"] in uploads[1].original_object_key
    assert uploads[0].original_object_key != uploads[1].original_object_key


async def test_completed_legacy_upload_remains_replayable_and_deletable_after_upgrade(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await seed_user(app, "dataset-upgrade-completed@example.test")
    headers = await login(client, owner.email)
    content = pcm_wav_bytes([0, 12, -12, 0])
    payload = upload_payload(
        content,
        filename="upgrade-completed.wav",
        content_type="audio/wav",
        idempotency_key="dataset-upgrade-completed-0001",
    )
    initialized = await initialize(client, headers, payload)
    target = initialized.json()
    assert (await upload_to_target(client, target, content)).status_code == 204
    finalized = await client.post(
        f"/api/v1/datasets/uploads/{target['upload_session_id']}/finalize",
        headers=headers,
    )
    assert finalized.status_code == 200, finalized.text

    storage = cast(LocalStorageAdapter, app.state.storage)
    async with app.state.database.session_factory() as session:
        dataset = await session.get(Dataset, target["dataset_id"])
        upload = await session.get(DatasetUploadSession, target["upload_session_id"])
        assert dataset is not None and upload is not None
        legacy_prefix = f"datasets/verified/{dataset.id}"
        replacements = {
            upload.original_object_key: f"{legacy_prefix}/original.wav",
            upload.prepared_flat_object_key: f"{legacy_prefix}/prepared_flat.zip",
            upload.manifest_object_key: f"{legacy_prefix}/manifest.json",
            upload.quality_report_object_key: f"{legacy_prefix}/quality_report.json",
        }
        for current_key, legacy_key in replacements.items():
            current_path = storage._path(current_key)
            legacy_path = storage._path(legacy_key)
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            current_path.replace(legacy_path)
        upload.original_object_key = replacements[upload.original_object_key]
        upload.prepared_flat_object_key = replacements[upload.prepared_flat_object_key]
        upload.manifest_object_key = replacements[upload.manifest_object_key]
        upload.quality_report_object_key = replacements[upload.quality_report_object_key]
        dataset.storage_uri = storage.storage_uri(upload.original_object_key)
        dataset.flat_storage_uri = storage.storage_uri(upload.prepared_flat_object_key)
        dataset.manifest_storage_uri = storage.storage_uri(upload.manifest_object_key)
        dataset.quality_report_storage_uri = storage.storage_uri(upload.quality_report_object_key)
        await session.commit()

    repeated_finalize = await client.post(
        f"/api/v1/datasets/uploads/{target['upload_session_id']}/finalize",
        headers=headers,
    )
    assert repeated_finalize.status_code == 200, repeated_finalize.text
    repeated_init = await initialize(client, headers, payload)
    assert repeated_init.status_code == 201, repeated_init.text
    assert repeated_init.json()["status"] == "completed"
    assert repeated_init.json()["upload_session_id"] == target["upload_session_id"]
    assert len(storage_files(app, f"datasets/verified/{target['dataset_id']}")) == 4
    deleted = await client.delete(
        f"/api/v1/datasets/{target['dataset_id']}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text
    assert storage_files(app, f"datasets/verified/{target['dataset_id']}") == []


async def test_dataset_delete_waits_for_expired_staging_cleanup(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await seed_user(app, "dataset-delete-cleanup@example.test")
    headers = await login(client, owner.email)
    content = pcm_wav_bytes([0, 11, -11, 0])
    initialized = await initialize(
        client,
        headers,
        upload_payload(
            content,
            filename="delete-cleanup.wav",
            content_type="audio/wav",
            idempotency_key="dataset-delete-cleanup-0001",
        ),
    )
    target = initialized.json()
    async with app.state.database.session_factory() as session:
        upload = await session.get(DatasetUploadSession, target["upload_session_id"])
        assert upload is not None
        upload.status = "expired"
        upload.failure_code = "upload_expired"
        await session.commit()

    blocked = await client.delete(
        f"/api/v1/datasets/{target['dataset_id']}",
        headers=headers,
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "dataset staging cleanup is pending"

    async with app.state.database.session_factory() as session:
        upload = await session.get(DatasetUploadSession, target["upload_session_id"])
        assert upload is not None
        upload.cleanup_completed_at = utc_now()
        await session.commit()
    deleted = await client.delete(
        f"/api/v1/datasets/{target['dataset_id']}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text


async def test_stale_finalize_recovery_expiry_generation_and_input_policy(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await seed_user(app, "recovery-owner@example.test")
    owner_headers = await login(client, owner.email)
    content = pcm_wav_bytes([0, 7, -7])
    payload = upload_payload(
        content,
        filename="recovery.wav",
        content_type="audio/wav",
        idempotency_key="dataset-stale-recovery-0001",
    )
    initialized = await initialize(client, owner_headers, payload)
    target = initialized.json()
    async with app.state.database.session_factory() as session:
        upload = await session.get(DatasetUploadSession, target["upload_session_id"])
        dataset = await session.get(Dataset, target["dataset_id"])
        assert upload is not None and dataset is not None
        upload.status = "finalizing"
        upload.finalization_token = "00000000-0000-4000-8000-000000000001"
        upload.updated_at = utc_now() - timedelta(
            seconds=app.state.settings.dataset_finalizing_stale_seconds + 1
        )
        dataset.status = "processing"
        await session.commit()
    recovered = await initialize(client, owner_headers, payload)
    assert recovered.status_code == 201
    assert recovered.json()["upload_session_id"] != target["upload_session_id"]
    async with app.state.database.session_factory() as session:
        old = await session.get(DatasetUploadSession, target["upload_session_id"])
        replacement = await session.get(
            DatasetUploadSession,
            recovered.json()["upload_session_id"],
        )
        assert old is not None and replacement is not None
        assert old.status == "expired"
        assert old.failure_code == "stale_finalizing_recovered"
        assert replacement.generation == 2
        assert replacement.temporary_object_key != old.temporary_object_key
        assert replacement.original_object_key != old.original_object_key

    assert (await upload_to_target(client, recovered.json(), content)).status_code == 204
    async with app.state.database.session_factory() as session:
        upload = await session.get(
            DatasetUploadSession,
            recovered.json()["upload_session_id"],
        )
        assert upload is not None
        old_key = upload.temporary_object_key
        upload.expires_at = utc_now() - timedelta(seconds=1)
        await session.commit()
    regenerated = await initialize(client, owner_headers, payload)
    assert regenerated.status_code == 201
    assert regenerated.json()["upload_session_id"] != recovered.json()["upload_session_id"]
    assert cast(LocalStorageAdapter, app.state.storage)._path(old_key).exists()
    async with app.state.database.session_factory() as session:
        latest = await session.get(
            DatasetUploadSession,
            regenerated.json()["upload_session_id"],
        )
        assert latest is not None
        assert latest.generation == 3

    invalid_mime = dict(payload)
    invalid_mime["idempotency_key"] = "dataset-invalid-mime-0001"
    invalid_mime["content_type"] = "application/octet-stream"
    assert (await initialize(client, owner_headers, invalid_mime)).status_code == 422
    traversal = dict(payload)
    traversal["idempotency_key"] = "dataset-invalid-path-0001"
    traversal["filename"] = "../recovery.wav"
    assert (await initialize(client, owner_headers, traversal)).status_code == 422
    app.state.settings.dataset_upload_max_bytes = len(content) - 1
    too_large = dict(payload)
    too_large["idempotency_key"] = "dataset-too-large-0001"
    assert (await initialize(client, owner_headers, too_large)).status_code == 413


async def test_dataset_upload_namespace_mismatch_preserves_ledger_and_objects(
    app: FastAPI,
    client: AsyncClient,
    tmp_path: Path,
) -> None:
    owner = await seed_user(app, "namespace-owner@example.test")
    owner_headers = await login(client, owner.email)
    content = pcm_wav_bytes([0, 13, -13, 0])
    payload = upload_payload(
        content,
        filename="namespace.wav",
        content_type="audio/wav",
        idempotency_key="dataset-namespace-mismatch-0001",
    )
    initialized = await initialize(client, owner_headers, payload)
    assert initialized.status_code == 201
    target = initialized.json()
    assert (await upload_to_target(client, target, content)).status_code == 204

    original_storage = cast(LocalStorageAdapter, app.state.storage)
    async with app.state.database.session_factory() as session:
        upload = await session.get(DatasetUploadSession, target["upload_session_id"])
        assert upload is not None
        staging_path = original_storage._path(upload.temporary_object_key)
        assert upload.storage_namespace_sha256 == original_storage.namespace_fingerprint
        assert staging_path.is_file()

    alternate_storage = LocalStorageAdapter(tmp_path / "alternate-dataset-objects")
    assert alternate_storage.backend == original_storage.backend
    assert alternate_storage.namespace_fingerprint != original_storage.namespace_fingerprint
    app.state.storage = alternate_storage
    try:
        replay = await initialize(client, owner_headers, payload)
        assert replay.status_code == 503
        overwritten = await upload_to_target(client, target, content)
        assert overwritten.status_code == 503
        finalized = await client.post(
            f"/api/v1/datasets/uploads/{target['upload_session_id']}/finalize",
            headers=owner_headers,
        )
        assert finalized.status_code == 503
    finally:
        app.state.storage = original_storage

    async with app.state.database.session_factory() as session:
        upload = await session.get(DatasetUploadSession, target["upload_session_id"])
        dataset = await session.get(Dataset, target["dataset_id"])
        assert upload is not None and dataset is not None
        assert upload.status == "pending"
        assert dataset.status == "upload_pending"
        assert upload.storage_namespace_sha256 == original_storage.namespace_fingerprint
    assert staging_path.is_file()

    bound_active_adoption = await adopt_storage_sessions(
        app.state.database,
        original_storage,
        kind="dataset",
        session_ids=(target["upload_session_id"],),
        dry_run=True,
    )
    assert bound_active_adoption.rejected == 1
    assert bound_active_adoption.items[0].code == "active_session"
    async with app.state.database.session_factory() as session:
        upload = await session.get(DatasetUploadSession, target["upload_session_id"])
        assert upload is not None
        upload.storage_namespace_sha256 = UNBOUND_STORAGE_NAMESPACE_SHA256
        await session.commit()
    active_adoption = await adopt_storage_sessions(
        app.state.database,
        original_storage,
        kind="dataset",
        session_ids=(target["upload_session_id"],),
        dry_run=True,
    )
    assert active_adoption.rejected == 1
    assert active_adoption.items[0].code == "active_session"
    async with app.state.database.session_factory() as session:
        upload = await session.get(DatasetUploadSession, target["upload_session_id"])
        assert upload is not None
        assert upload.storage_namespace_sha256 == UNBOUND_STORAGE_NAMESPACE_SHA256
        upload.storage_namespace_sha256 = original_storage.namespace_fingerprint
        await session.commit()

    finalized = await client.post(
        f"/api/v1/datasets/uploads/{target['upload_session_id']}/finalize",
        headers=owner_headers,
    )
    assert finalized.status_code == 200, finalized.text
    verified_files = storage_files(app, f"datasets/verified/{target['dataset_id']}")
    assert len(verified_files) == 4

    async with app.state.database.session_factory() as session:
        upload = await session.get(DatasetUploadSession, target["upload_session_id"])
        assert upload is not None
        upload.storage_namespace_sha256 = UNBOUND_STORAGE_NAMESPACE_SHA256
        await session.commit()
    wrong_target = await adopt_storage_sessions(
        app.state.database,
        alternate_storage,
        kind="dataset",
        session_ids=(target["upload_session_id"],),
        dry_run=True,
    )
    assert wrong_target.rejected == 1
    assert wrong_target.items[0].code in {
        "dataset_storage_uri_mismatch",
        "object_not_found",
    }
    preview = await adopt_storage_sessions(
        app.state.database,
        original_storage,
        kind="dataset",
        session_ids=(target["upload_session_id"],),
        dry_run=True,
    )
    assert preview.verified == 1
    assert preview.adopted == 0
    assert preview.target_storage_backend == "local"
    assert preview.target_storage_namespace_sha256 == original_storage.namespace_fingerprint
    async with app.state.database.session_factory() as session:
        upload = await session.get(DatasetUploadSession, target["upload_session_id"])
        assert upload is not None
        assert upload.storage_namespace_sha256 == UNBOUND_STORAGE_NAMESPACE_SHA256
    applied = await adopt_storage_sessions(
        app.state.database,
        original_storage,
        kind="dataset",
        session_ids=(target["upload_session_id"],),
        dry_run=False,
    )
    assert applied.adopted == 1
    replayed = await adopt_storage_sessions(
        app.state.database,
        original_storage,
        kind="dataset",
        session_ids=(target["upload_session_id"],),
        dry_run=False,
    )
    assert replayed.rejected == 0
    assert replayed.verified == 1
    assert replayed.items[0].code == "already_bound"
    async with app.state.database.session_factory() as session:
        adoption_audit = await session.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.resource_id == target["upload_session_id"],
                AuditEvent.action == "storage_namespace.adopted",
            )
            .order_by(AuditEvent.occurred_at.desc())
        )
        assert adoption_audit is not None
        assert adoption_audit.details_json["target_storage_backend"] == "local"
        assert (
            adoption_audit.details_json["target_storage_namespace_sha256"]
            == original_storage.namespace_fingerprint
        )
    app.state.storage = alternate_storage
    try:
        blocked_delete = await client.delete(
            f"/api/v1/datasets/{target['dataset_id']}",
            headers=owner_headers,
        )
        assert blocked_delete.status_code == 503
    finally:
        app.state.storage = original_storage
    assert len(storage_files(app, f"datasets/verified/{target['dataset_id']}")) == 4


async def test_delete_marks_dataset_unusable_before_external_object_cleanup(
    app: FastAPI,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await seed_user(app, "delete-race-owner@example.test")
    owner_headers = await login(client, owner.email)
    content = pcm_wav_bytes([0, 9, -9, 0])
    payload = upload_payload(
        content,
        filename="delete-race.wav",
        content_type="audio/wav",
        idempotency_key="dataset-delete-race-0001",
    )
    initialized = await initialize(client, owner_headers, payload)
    target = initialized.json()
    assert (await upload_to_target(client, target, content)).status_code == 204
    finalized = await client.post(
        f"/api/v1/datasets/uploads/{target['upload_session_id']}/finalize",
        headers=owner_headers,
    )
    assert finalized.status_code == 200

    storage = cast(LocalStorageAdapter, app.state.storage)
    original_delete = storage.delete_object
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    first_call = True

    async def paused_delete(object_key: str) -> None:
        nonlocal first_call
        if first_call:
            first_call = False
            cleanup_started.set()
            await allow_cleanup.wait()
        await original_delete(object_key)

    monkeypatch.setattr(storage, "delete_object", paused_delete)
    delete_task = asyncio.create_task(
        client.delete(
            f"/api/v1/datasets/{target['dataset_id']}",
            headers=owner_headers,
        )
    )
    await asyncio.wait_for(cleanup_started.wait(), timeout=2)
    raced_experiment = await client.post(
        "/api/v1/experiments",
        headers=owner_headers,
        json={"name": "must-not-race-delete", "dataset_id": target["dataset_id"]},
    )
    assert raced_experiment.status_code == 409
    allow_cleanup.set()
    deleted = await asyncio.wait_for(delete_task, timeout=2)
    assert deleted.status_code == 204, deleted.text
    assert storage_files(app, f"datasets/verified/{target['dataset_id']}") == []


async def test_job_creation_and_claim_recheck_dataset_readiness(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await seed_user(app, "job-gate-owner@example.test")
    owner_headers = await login(client, owner.email)
    dataset_response = await client.post(
        "/api/v1/datasets",
        headers=owner_headers,
        json={
            "name": "ready-then-invalidated",
            "storage_uri": "local:///legacy/original.zip",
            "flat_storage_uri": "local:///legacy/prepared.zip",
        },
    )
    assert dataset_response.status_code == 201
    dataset_id = dataset_response.json()["id"]
    experiment = await client.post(
        "/api/v1/experiments",
        headers=owner_headers,
        json={"name": "readiness-gate", "dataset_id": dataset_id},
    )
    assert experiment.status_code == 201
    experiment_id = experiment.json()["id"]
    job_payload = {
        "job_name": "queued-before-invalidation",
        "experiment_id": experiment_id,
        "dataset_id": dataset_id,
        "model": {"version": "v2", "sample_rate": "40k"},
    }
    queued = await client.post("/api/v1/jobs", headers=owner_headers, json=job_payload)
    assert queued.status_code == 201, queued.text

    async with app.state.database.session_factory() as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        dataset.status = "decoder_pending"
        dataset.is_usable = False
        await session.commit()

    blocked_payload = dict(job_payload)
    blocked_payload["job_name"] = "must-not-queue"
    blocked = await client.post(
        "/api/v1/jobs",
        headers=owner_headers,
        json=blocked_payload,
    )
    assert blocked.status_code == 409

    registered = await client.post(
        "/api/v1/workers/register",
        headers={"X-Worker-Bootstrap-Token": "test-bootstrap-token"},
        json={
            "name": "dataset-readiness-worker",
            "capabilities": {
                "engine_mode": "fake",
                "worker_version": "0.1.0",
                "rvc_commit_hash": "fake-dataset-gate",
                "supported_rvc_versions": ["v2"],
                "supported_training_f0_methods": ["rmvpe"],
                "gpus": [],
                "disk_free_bytes": 1_000_000_000,
                "rvc_assets_ready": False,
            },
        },
    )
    assert registered.status_code == 201, registered.text
    claim = await client.post(
        "/api/v1/workers/jobs/claim",
        headers={"Authorization": f"Bearer {registered.json()['worker_token']}"},
        json={"max_wait_seconds": 0},
    )
    assert claim.status_code == 204


async def test_legacy_client_uri_route_is_disabled_outside_test_policy(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await seed_user(app, "legacy-policy@example.test")
    owner_headers = await login(client, owner.email)
    app.state.settings.environment = "production"
    response = await client.post(
        "/api/v1/datasets",
        headers=owner_headers,
        json={
            "name": "client-uri-must-not-enter-production",
            "storage_uri": "s3://attacker-controlled/raw.zip",
            "flat_storage_uri": "s3://attacker-controlled/flat.zip",
        },
    )
    assert response.status_code == 403


async def test_active_worker_downloads_verified_flat_archive_without_uri_disclosure(
    app: FastAPI,
    client: AsyncClient,
    tmp_path: Path,
) -> None:
    owner = await seed_user(app, "worker-transfer-owner@example.test")
    owner_headers = await login(client, owner.email)
    wav = pcm_wav_bytes([0, 100, -100, 0])
    source = zip_bytes([("voice.wav", wav)])
    initialized = await initialize(
        client,
        owner_headers,
        upload_payload(
            source,
            filename="worker-transfer.zip",
            content_type="application/zip",
            idempotency_key="dataset-worker-transfer-0001",
        ),
    )
    assert initialized.status_code == 201
    target = initialized.json()
    assert (await upload_to_target(client, target, source)).status_code == 204
    finalized = await client.post(
        f"/api/v1/datasets/uploads/{target['upload_session_id']}/finalize",
        headers=owner_headers,
    )
    assert finalized.status_code == 200, finalized.text
    dataset = finalized.json()

    experiment = await client.post(
        "/api/v1/experiments",
        headers=owner_headers,
        json={"name": "worker-transfer", "dataset_id": dataset["id"]},
    )
    assert experiment.status_code == 201, experiment.text
    job = await client.post(
        "/api/v1/jobs",
        headers=owner_headers,
        json={
            "job_name": "worker-transfer-v2",
            "experiment_id": experiment.json()["id"],
            "dataset_id": dataset["id"],
            "model": {"version": "v2", "sample_rate": "40k"},
        },
    )
    assert job.status_code == 201, job.text

    async def register(name: str) -> str:
        response = await client.post(
            "/api/v1/workers/register",
            headers={"X-Worker-Bootstrap-Token": "test-bootstrap-token"},
            json={
                "name": name,
                "capabilities": {
                    "engine_mode": "rvc_webui",
                    "worker_version": "0.1.0",
                    "rvc_commit_hash": "abcdef1",
                    "supported_rvc_versions": ["v2"],
                    "supported_training_f0_methods": ["rmvpe"],
                    "gpus": [
                        {
                            "index": 0,
                            "name": "test-gpu",
                            "total_vram_mb": 24_576,
                            "free_vram_mb": 24_576,
                        }
                    ],
                    "disk_free_bytes": 100 * 1024**3,
                    "rvc_assets_ready": True,
                },
            },
        )
        assert response.status_code == 201, response.text
        return str(response.json()["worker_token"])

    owner_worker_token = await register("dataset-transfer-worker")
    foreign_worker_token = await register("dataset-transfer-foreign-worker")
    claim_storage = cast(LocalStorageAdapter, app.state.storage)
    app.state.storage = LocalStorageAdapter(tmp_path / "alternate-claim-dataset-objects")
    try:
        blocked_claim = await client.post(
            "/api/v1/workers/jobs/claim",
            headers={"Authorization": f"Bearer {owner_worker_token}"},
            json={"max_wait_seconds": 0},
        )
        assert blocked_claim.status_code == 204
    finally:
        app.state.storage = claim_storage
    claimed = await client.post(
        "/api/v1/workers/jobs/claim",
        headers={"Authorization": f"Bearer {owner_worker_token}"},
        json={"max_wait_seconds": 0},
    )
    assert claimed.status_code == 200, claimed.text
    claim = claimed.json()
    assert "dataset_storage_uri" not in claim
    transfer = claim["dataset_transfer"]
    assert transfer == {
        "dataset_id": dataset["id"],
        "download_path": f"/api/v1/workers/jobs/{job.json()['id']}/dataset",
        "filename": "prepared_flat.zip",
        "content_type": "application/zip",
        "size_bytes": dataset["prepared_flat_size_bytes"],
        "sha256": dataset["prepared_flat_sha256"],
    }
    worker_headers = {
        "Authorization": f"Bearer {owner_worker_token}",
        "X-RVC-Lease-ID": claim["lease_id"],
        "X-RVC-Attempt-ID": claim["attempt_id"],
    }
    downloaded = await client.get(transfer["download_path"], headers=worker_headers)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.headers["Content-Type"] == "application/zip"
    assert downloaded.headers["Content-Length"] == str(transfer["size_bytes"])
    assert "prepared_flat.zip" in downloaded.headers["Content-Disposition"]
    assert len(downloaded.content) == transfer["size_bytes"]
    assert hashlib.sha256(downloaded.content).hexdigest() == transfer["sha256"]
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
        assert archive.read("prepared_flat/000001.wav") == wav

    original_storage = cast(LocalStorageAdapter, app.state.storage)
    alternate_storage = LocalStorageAdapter(tmp_path / "alternate-worker-dataset-objects")
    app.state.storage = alternate_storage
    try:
        wrong_namespace = await client.get(transfer["download_path"], headers=worker_headers)
        assert wrong_namespace.status_code == 503
    finally:
        app.state.storage = original_storage

    foreign = await client.get(
        transfer["download_path"],
        headers={
            **worker_headers,
            "Authorization": f"Bearer {foreign_worker_token}",
        },
    )
    assert foreign.status_code == 409
    wrong_attempt = await client.get(
        transfer["download_path"],
        headers={**worker_headers, "X-RVC-Attempt-ID": "other-attempt"},
    )
    assert wrong_attempt.status_code == 409

    async with app.state.database.session_factory() as session:
        audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "dataset.worker_download_requested",
                AuditEvent.resource_id == dataset["id"],
            )
        )
        assert audit is not None
        assert "storage" not in audit.details_json
        persisted = await session.get(Dataset, dataset["id"])
        assert persisted is not None
        persisted.is_usable = False
        await session.commit()
    unusable = await client.get(transfer["download_path"], headers=worker_headers)
    assert unusable.status_code == 409
    async with app.state.database.session_factory() as session:
        persisted = await session.get(Dataset, dataset["id"])
        lease = await session.get(JobLease, claim["lease_id"])
        assert persisted is not None and lease is not None
        persisted.is_usable = True
        lease.expires_at = utc_now() - timedelta(seconds=1)
        await session.commit()
    stale = await client.get(transfer["download_path"], headers=worker_headers)
    assert stale.status_code == 409
