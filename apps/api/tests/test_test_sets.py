from __future__ import annotations

import asyncio
import hashlib
import io
import json
import wave
from datetime import timedelta
from pathlib import Path
from typing import Literal

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from rvc_manager_api.models import (
    Artifact,
    AuditEvent,
    Dataset,
    Experiment,
    Job,
    JobAttempt,
    User,
    Worker,
)
from rvc_manager_api.models import (
    Sample as LedgerSample,
)
from rvc_manager_api.models import (
    TestSet as LedgerTestSet,
)
from rvc_manager_api.models import (
    TestSetItem as LedgerTestSetItem,
)
from rvc_manager_api.models import (
    TestSetItemUploadSession as LedgerTestSetItemUploadSession,
)
from rvc_manager_api.routers.test_sets import _mark_finalizing_upload_failed
from rvc_manager_api.security import hash_password
from rvc_manager_api.services.test_sets import (
    build_test_set_manifest_document,
    canonical_sha256,
)
from rvc_orchestrator_contracts import utc_now

USER_PASSWORD = "test-set-user-password-1234"
TEST_RUNTIME_IMAGE_DIGEST = "sha256:" + "b" * 64
TEST_RUNTIME_ASSET_MANIFEST_SHA256 = "c" * 64


@pytest.mark.asyncio
async def test_test_set_local_put_openapi_declares_deadline_and_operational_errors(
    client: AsyncClient,
) -> None:
    schema = (await client.get("/openapi.json")).json()
    responses = schema["paths"][
        "/api/v1/storage/test-set-item-uploads/{upload_session_id}"
    ]["put"]["responses"]
    assert {"204", "408", "409", "413", "422", "503"}.issubset(responses)


async def _seed_user(
    app: FastAPI,
    email: str,
    *,
    role: Literal["admin", "user"] = "user",
) -> User:
    encoded = await run_in_threadpool(hash_password, USER_PASSWORD)
    async with app.state.database.session_factory() as session:
        user = User(email=email, password_hash=encoded, role=role, disabled=False)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _login(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": USER_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _register_sample_worker(
    client: AsyncClient,
    name: str,
    *,
    sample_ready: bool,
    inference_methods: list[str] | None = None,
) -> str:
    response = await client.post(
        "/api/v1/workers/register",
        headers={"X-Worker-Bootstrap-Token": "test-bootstrap-token"},
        json={
            "name": name,
            "capabilities": {
                "engine_mode": "rvc_webui",
                "worker_version": "test-sample-transfer",
                "rvc_commit_hash": "7ef19867780cf703841ebafb565a4e47d1ea86ff",
                "supported_rvc_versions": ["v1", "v2"],
                "supported_training_f0_methods": [
                    "pm",
                    "harvest",
                    "dio",
                    "rmvpe",
                    "rmvpe_gpu",
                ],
                "supported_inference_f0_methods": inference_methods
                if inference_methods is not None
                else ["pm", "harvest", "crepe", "rmvpe"],
                "fixed_test_set_inference_ready": sample_ready,
                "gpus": [
                    {
                        "index": 0,
                        "name": "test-gpu",
                        "total_vram_mb": 24_576,
                        "free_vram_mb": 24_000,
                    }
                ],
                "disk_free_bytes": 100 * 1024**3,
                "rvc_assets_ready": True,
                "runtime_image_digest": (TEST_RUNTIME_IMAGE_DIGEST if sample_ready else None),
                "runtime_asset_manifest_sha256": (
                    TEST_RUNTIME_ASSET_MANIFEST_SHA256 if sample_ready else None
                ),
            },
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["worker_token"])


def _pcm_wav_bytes(
    *,
    sample_rate: int = 16_000,
    channels: int = 1,
    frames: int = 1_600,
) -> bytes:
    output = io.BytesIO()
    with wave.open(output, mode="wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x00\x00" * frames * channels)
    return output.getvalue()


async def _create_test_set(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    name: str,
) -> dict[str, object]:
    response = await client.post(
        "/api/v1/test-sets",
        headers=headers,
        json={"name": name, "description": "licensed fixed comparison fixtures"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _upload_test_set_item(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    test_set_id: str,
    content: bytes,
    item_key: str = "speech-clean",
    sort_order: int = 0,
    idempotency_key: str = "test-set-upload-0001",
) -> tuple[dict[str, object], dict[str, object]]:
    payload = {
        "item_key": item_key,
        "display_name": f"Fixture {item_key}",
        "sort_order": sort_order,
        "filename": f"{item_key}.wav",
        "content_type": "audio/wav",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "license_reference": "license-record:test-fixtures-v1",
        "provenance_reference": "consent-record:recording-session-1",
        "idempotency_key": idempotency_key,
    }
    initialized = await client.post(
        f"/api/v1/test-sets/{test_set_id}/item-uploads/init",
        headers=headers,
        json=payload,
    )
    assert initialized.status_code == 201, initialized.text
    target = initialized.json()
    uploaded = await client.put(
        str(target["upload_url"]),
        headers=target["upload_headers"],
        content=content,
    )
    assert uploaded.status_code == 204, uploaded.text
    finalized = await client.post(
        f"/api/v1/test-sets/item-uploads/{target['upload_session_id']}/finalize",
        headers=headers,
    )
    assert finalized.status_code == 200, finalized.text
    return target, finalized.json()


async def _initialize_test_set_item(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    test_set_id: str,
    content: bytes,
    idempotency_key: str,
) -> dict[str, object]:
    response = await client.post(
        f"/api/v1/test-sets/{test_set_id}/item-uploads/init",
        headers=headers,
        json={
            "item_key": hashlib.sha256(idempotency_key.encode()).hexdigest()[:16],
            "display_name": "Fenced upload",
            "sort_order": 0,
            "filename": "fenced.wav",
            "content_type": "audio/wav",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "license_reference": "license-record:fencing",
            "provenance_reference": "consent-record:fencing",
            "idempotency_key": idempotency_key,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_slow_local_test_set_put_renews_generation_heartbeat(
    app: FastAPI,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await _seed_user(app, "slow-put-heartbeat@example.test")
    headers = await _login(client, owner.email)
    draft = await _create_test_set(client, headers, name="slow-put-heartbeat")
    content = _pcm_wav_bytes()
    target = await _initialize_test_set_item(
        client,
        headers,
        test_set_id=str(draft["id"]),
        content=content,
        idempotency_key="slow-put-heartbeat-0001",
    )
    storage = app.state.storage
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
    app.state.settings.test_set_upload_write_heartbeat_seconds = 0.01
    upload_task = asyncio.create_task(
        client.put(
            str(target["upload_url"]),
            headers=target["upload_headers"],
            content=content,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    async with app.state.database.session_factory() as session:
        initial = await session.get(
            LedgerTestSetItemUploadSession,
            target["upload_session_id"],
        )
        assert initial is not None and initial.upload_heartbeat_at is not None
        initial_heartbeat = initial.upload_heartbeat_at
        write_token = initial.upload_write_token
        generation = initial.generation
    await asyncio.sleep(0.05)
    async with app.state.database.session_factory() as session:
        renewed = await session.get(
            LedgerTestSetItemUploadSession,
            target["upload_session_id"],
        )
        assert renewed is not None and renewed.upload_heartbeat_at is not None
        assert renewed.upload_heartbeat_at > initial_heartbeat
        assert renewed.upload_write_token == write_token
        assert renewed.generation == generation
    release.set()
    uploaded = await asyncio.wait_for(upload_task, timeout=2)
    assert uploaded.status_code == 204, uploaded.text


async def test_slow_local_test_set_put_cannot_extend_session_past_expiry(
    app: FastAPI,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await _seed_user(app, "slow-put-expiry@example.test")
    headers = await _login(client, owner.email)
    draft = await _create_test_set(client, headers, name="slow-put-expiry")
    test_set_id = str(draft["id"])
    content = _pcm_wav_bytes()
    target = await _initialize_test_set_item(
        client,
        headers,
        test_set_id=test_set_id,
        content=content,
        idempotency_key="slow-put-expiry-0001",
    )
    async with app.state.database.session_factory() as session:
        upload = await session.get(
            LedgerTestSetItemUploadSession,
            target["upload_session_id"],
        )
        assert upload is not None
        # Leave enough request-start margin for the full SQLite suite while
        # still forcing expiry during the deliberately stalled request body.
        upload.expires_at = utc_now() + timedelta(seconds=1)
        await session.commit()

    storage = app.state.storage
    original_write = storage.write_upload_stream
    started = asyncio.Event()

    async def slow_body_write(
        object_key: str,
        chunks: object,
        *,
        expected_size: int,
    ) -> None:
        async def delayed_chunks():
            async for chunk in chunks:  # type: ignore[union-attr]
                started.set()
                await asyncio.sleep(10)
                yield chunk

        await original_write(
            object_key,
            delayed_chunks(),
            expected_size=expected_size,
        )

    monkeypatch.setattr(storage, "write_upload_stream", slow_body_write)
    app.state.settings.test_set_upload_write_heartbeat_seconds = 0.01
    upload_task = asyncio.create_task(
        client.put(
            str(target["upload_url"]),
            headers=target["upload_headers"],
            content=content,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    response = await asyncio.wait_for(upload_task, timeout=2)
    assert response.status_code == 408
    assert response.json()["detail"] == "test set upload deadline exceeded"
    staging_path = (
        app.state.settings.local_storage_root
        / "test-sets"
        / "staging"
        / test_set_id
        / str(target["upload_session_id"])
    )
    assert not staging_path.exists()
    assert list(staging_path.parent.glob(f".{staging_path.name}.*.part")) == []
    async with app.state.database.session_factory() as session:
        expired = await session.get(
            LedgerTestSetItemUploadSession,
            target["upload_session_id"],
        )
        assert expired is not None
        assert expired.status == "expired"
        assert expired.upload_write_token is None
        assert expired.upload_heartbeat_at is None
        assert expired.failure_code == "upload_write_deadline_exceeded"


async def test_late_local_put_losing_cleanup_fence_removes_republished_staging(
    app: FastAPI,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await _seed_user(app, "late-put-fence@example.test")
    headers = await _login(client, owner.email)
    draft = await _create_test_set(client, headers, name="late-put-fence")
    test_set_id = str(draft["id"])
    content = _pcm_wav_bytes()
    target = await _initialize_test_set_item(
        client,
        headers,
        test_set_id=test_set_id,
        content=content,
        idempotency_key="late-put-fence-0001",
    )
    storage = app.state.storage
    original_write = storage.write_upload_stream
    started = asyncio.Event()
    release = asyncio.Event()

    async def late_write(
        object_key: str,
        chunks: object,
        *,
        expected_size: int,
    ) -> None:
        started.set()
        await release.wait()
        await original_write(object_key, chunks, expected_size=expected_size)

    monkeypatch.setattr(storage, "write_upload_stream", late_write)
    upload_task = asyncio.create_task(
        client.put(
            str(target["upload_url"]),
            headers=target["upload_headers"],
            content=content,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    async with app.state.database.session_factory() as session:
        await session.scalar(
            select(LedgerTestSet)
            .where(LedgerTestSet.id == test_set_id)
            .with_for_update()
        )
        upload = await session.scalar(
            select(LedgerTestSetItemUploadSession)
            .where(
                LedgerTestSetItemUploadSession.id == target["upload_session_id"]
            )
            .with_for_update()
        )
        assert upload is not None and upload.upload_write_token is not None
        upload.status = "expired"
        upload.upload_write_token = None
        upload.upload_heartbeat_at = None
        upload.failure_code = "staging_cleanup_pending"
        await session.commit()
    release.set()
    uploaded = await asyncio.wait_for(upload_task, timeout=2)
    assert uploaded.status_code == 409
    assert uploaded.json()["detail"] == "test set upload write lease was lost"
    staging_path = (
        app.state.settings.local_storage_root
        / "test-sets"
        / "staging"
        / test_set_id
        / str(target["upload_session_id"])
    )
    assert not staging_path.exists()


async def test_slow_test_set_finalize_heartbeat_prevents_stale_recovery(
    app: FastAPI,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rvc_manager_api.routers import test_sets as test_set_router

    owner = await _seed_user(app, "slow-finalize-heartbeat@example.test")
    headers = await _login(client, owner.email)
    draft = await _create_test_set(client, headers, name="slow-finalize-heartbeat")
    content = _pcm_wav_bytes()
    target = await _initialize_test_set_item(
        client,
        headers,
        test_set_id=str(draft["id"]),
        content=content,
        idempotency_key="slow-finalize-heartbeat-0001",
    )
    uploaded = await client.put(
        str(target["upload_url"]),
        headers=target["upload_headers"],
        content=content,
    )
    assert uploaded.status_code == 204
    original_verify = test_set_router.verify_object_to_spool
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_verify(*args: object, **kwargs: object) -> Path:
        started.set()
        await release.wait()
        return await original_verify(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(test_set_router, "verify_object_to_spool", slow_verify)
    app.state.settings.test_set_finalizing_heartbeat_seconds = 0.01
    app.state.settings.test_set_finalizing_stale_seconds = 0.04
    finalize_url = (
        f"/api/v1/test-sets/item-uploads/{target['upload_session_id']}/finalize"
    )
    first_task = asyncio.create_task(client.post(finalize_url, headers=headers))
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.sleep(0.08)
    competing = await client.post(finalize_url, headers=headers)
    assert competing.status_code == 409
    assert competing.json()["detail"] == "test set upload is already finalizing"
    async with app.state.database.session_factory() as session:
        upload = await session.get(
            LedgerTestSetItemUploadSession,
            target["upload_session_id"],
        )
        assert upload is not None
        assert upload.status == "finalizing"
        assert upload.finalization_token is not None
        assert upload.finalization_heartbeat_at is not None
    release.set()
    finalized = await asyncio.wait_for(first_task, timeout=3)
    assert finalized.status_code == 200, finalized.text


async def test_preset_revisions_are_hashed_immutable_and_owner_scoped(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    owner = await _seed_user(app, "preset-owner@example.test")
    await _seed_user(app, "preset-other@example.test")
    owner_headers = await _login(client, owner.email)
    other_headers = await _login(client, "preset-other@example.test")
    config = {
        "inference_f0_method": "rmvpe",
        "transpose": 0,
        "index_rate": 0.75,
        "filter_radius": 3,
        "resample_sr": 0,
        "rms_mix_rate": 0.25,
        "protect": 0.33,
    }
    created = await client.post(
        "/api/v1/presets",
        headers=owner_headers,
        json={"name": "studio-neutral", "config": config},
    )
    assert created.status_code == 201, created.text
    first = created.json()
    assert first["revision"] == 1
    assert first["config_sha256"] == canonical_sha256(config)
    assert "storage_uri" not in created.text

    duplicate = await client.post(
        "/api/v1/presets",
        headers=owner_headers,
        json={"name": "studio-neutral", "config": config},
    )
    assert duplicate.status_code == 409

    revised_config = {**config, "transpose": 3, "inference_f0_method": "crepe"}
    revised = await client.post(
        f"/api/v1/presets/{first['id']}/revisions",
        headers=owner_headers,
        json={"config": revised_config},
    )
    assert revised.status_code == 201, revised.text
    second = revised.json()
    assert second["family_id"] == first["family_id"]
    assert second["revision"] == 2
    assert second["config_sha256"] == canonical_sha256(revised_config)
    unchanged = await client.get(f"/api/v1/presets/{first['id']}", headers=owner_headers)
    assert unchanged.json()["config"] == config

    assert (await client.get("/api/v1/presets", headers=other_headers)).json()["total"] == 0
    assert (
        await client.get(f"/api/v1/presets/{first['id']}", headers=other_headers)
    ).status_code == 404
    assert (await client.get("/api/v1/presets", headers=admin_headers)).json()["total"] == 2
    assert (
        await client.delete(f"/api/v1/presets/{second['id']}", headers=owner_headers)
    ).status_code == 409
    assert (
        await client.delete(f"/api/v1/presets/{first['id']}", headers=owner_headers)
    ).status_code == 409
    reused_initial = await client.post(
        "/api/v1/presets",
        headers=owner_headers,
        json={"name": "studio-neutral", "config": config},
    )
    assert reused_initial.status_code == 409
    third = await client.post(
        f"/api/v1/presets/{second['id']}/revisions",
        headers=owner_headers,
        json={"config": {**revised_config, "transpose": 4}},
    )
    assert third.status_code == 201, third.text
    assert third.json()["revision"] == 3
    sole = await client.post(
        "/api/v1/presets",
        headers=owner_headers,
        json={"name": "temporary-preset", "config": config},
    )
    assert sole.status_code == 201
    assert (
        await client.delete(f"/api/v1/presets/{sole.json()['id']}", headers=owner_headers)
    ).status_code == 204


async def test_test_set_revision_hides_storage_and_ready_revision_is_immutable(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await _seed_user(app, "test-set-owner@example.test")
    await _seed_user(app, "test-set-other@example.test")
    owner_headers = await _login(client, owner.email)
    other_headers = await _login(client, "test-set-other@example.test")

    created = await client.post(
        "/api/v1/test-sets",
        headers=owner_headers,
        json={"name": "fixed-comparison", "description": "licensed fixtures"},
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["status"] == "draft"
    assert draft["items"] == []
    assert (
        await client.post(
            f"/api/v1/test-sets/{draft['id']}/revisions",
            headers=owner_headers,
            json={"description": "new"},
        )
    ).status_code == 409

    async with app.state.database.session_factory() as session:
        test_set = await session.get(LedgerTestSet, draft["id"])
        assert test_set is not None
        item = LedgerTestSetItem(
            test_set_id=test_set.id,
            item_key="speech-clean",
            display_name="Speech clean",
            sort_order=0,
            storage_uri="s3://private-test-sets/never-return-this.wav",
            original_filename="speech_clean.wav",
            size_bytes=48_044,
            sha256="a" * 64,
            mime_type="audio/wav",
            sample_rate_hz=48_000,
            channels=1,
            duration_seconds=0.5,
            license_reference="license-record:internal-1",
            provenance_reference="consent-record:recording-1",
        )
        session.add(item)
        await session.flush()
        test_set.status = "ready"
        test_set.item_count = 1
        test_set.manifest_sha256 = canonical_sha256(
            build_test_set_manifest_document(test_set, [item])
        )
        await session.commit()

    detail = await client.get(f"/api/v1/test-sets/{draft['id']}", headers=owner_headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["item_count"] == 1
    assert body["items"][0]["item_key"] == "speech-clean"
    assert "private-test-sets" not in detail.text
    assert "storage_uri" not in detail.text
    assert (
        await client.get(f"/api/v1/test-sets/{draft['id']}", headers=other_headers)
    ).status_code == 404
    assert (
        await client.delete(f"/api/v1/test-sets/{draft['id']}", headers=owner_headers)
    ).status_code == 409

    revision = await client.post(
        f"/api/v1/test-sets/{draft['id']}/revisions",
        headers=owner_headers,
        json={"description": "replacement fixtures"},
    )
    assert revision.status_code == 201, revision.text
    assert revision.json()["family_id"] == draft["family_id"]
    assert revision.json()["revision"] == 2
    assert revision.json()["items"] == []

    async with app.state.database.session_factory() as session:
        events = list(
            (
                await session.scalars(
                    select(AuditEvent).where(AuditEvent.resource_type.in_(("test_set", "preset")))
                )
            ).all()
        )
    assert any(event.action == "test_set.created" for event in events)
    assert any(event.action == "test_set.revision_created" for event in events)


async def test_draft_test_set_can_be_deleted_but_duplicate_initial_revision_conflicts(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await _seed_user(app, "draft-owner@example.test")
    headers = await _login(client, owner.email)
    payload = {"name": "temporary-test-set", "description": None}
    first = await client.post("/api/v1/test-sets", headers=headers, json=payload)
    assert first.status_code == 201
    duplicate = await client.post("/api/v1/test-sets", headers=headers, json=payload)
    assert duplicate.status_code == 409
    deleted = await client.delete(f"/api/v1/test-sets/{first.json()['id']}", headers=headers)
    assert deleted.status_code == 204
    assert (
        await client.get(f"/api/v1/test-sets/{first.json()['id']}", headers=headers)
    ).status_code == 404


async def test_test_set_wav_upload_finalize_manifest_and_immutability(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await _seed_user(app, "wav-owner@example.test")
    await _seed_user(app, "wav-other@example.test")
    headers = await _login(client, owner.email)
    other_headers = await _login(client, "wav-other@example.test")
    draft = await _create_test_set(client, headers, name="wav-ledger")
    test_set_id = str(draft["id"])
    content = _pcm_wav_bytes()
    initialized = await client.post(
        f"/api/v1/test-sets/{test_set_id}/item-uploads/init",
        headers=headers,
        json={
            "item_key": "speech-clean",
            "display_name": "Speech clean",
            "sort_order": 0,
            "filename": "speech_clean.wav",
            "content_type": "audio/wav",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "license_reference": "license:cc-by-4.0",
            "provenance_reference": "consent:fixture-1",
            "idempotency_key": "stable-upload-request-0001",
        },
    )
    assert initialized.status_code == 201, initialized.text
    target = initialized.json()
    assert initialized.headers["Cache-Control"] == "no-store"
    assert "storage_uri" not in initialized.text
    assert "test-sets/staging" not in initialized.text
    repeated = await client.post(
        f"/api/v1/test-sets/{test_set_id}/item-uploads/init",
        headers=headers,
        json={
            "item_key": "speech-clean",
            "display_name": "Speech clean",
            "sort_order": 0,
            "filename": "speech_clean.wav",
            "content_type": "audio/wav",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "license_reference": "license:cc-by-4.0",
            "provenance_reference": "consent:fixture-1",
            "idempotency_key": "stable-upload-request-0001",
        },
    )
    assert repeated.status_code == 201
    assert repeated.json()["upload_session_id"] == target["upload_session_id"]
    foreign_init = await client.post(
        f"/api/v1/test-sets/{test_set_id}/item-uploads/init",
        headers=other_headers,
        json={
            "item_key": "foreign",
            "display_name": "Foreign",
            "sort_order": 1,
            "filename": "foreign.wav",
            "content_type": "audio/wav",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "license_reference": "license:foreign",
            "provenance_reference": "consent:foreign",
            "idempotency_key": "foreign-upload-request-0001",
        },
    )
    assert foreign_init.status_code == 404
    conflicting_reservation = await client.post(
        f"/api/v1/test-sets/{test_set_id}/item-uploads/init",
        headers=headers,
        json={
            "item_key": "another-key",
            "display_name": "Collision",
            "sort_order": 0,
            "filename": "collision.wav",
            "content_type": "audio/wav",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "license_reference": "license:cc-by-4.0",
            "provenance_reference": "consent:fixture-2",
            "idempotency_key": "stable-upload-request-0002",
        },
    )
    assert conflicting_reservation.status_code == 409
    assert (
        await client.post(
            f"/api/v1/test-sets/item-uploads/{target['upload_session_id']}/finalize",
            headers=other_headers,
        )
    ).status_code == 404
    uploaded = await client.put(
        str(target["upload_url"]),
        headers=target["upload_headers"],
        content=content,
    )
    assert uploaded.status_code == 204, uploaded.text
    finalized_item = await client.post(
        f"/api/v1/test-sets/item-uploads/{target['upload_session_id']}/finalize",
        headers=headers,
    )
    assert finalized_item.status_code == 200, finalized_item.text
    assert finalized_item.headers["Cache-Control"] == "private, no-store"
    assert finalized_item.json()["sample_rate_hz"] == 16_000
    assert finalized_item.json()["channels"] == 1

    async with app.state.database.session_factory() as session:
        upload = await session.get(LedgerTestSetItemUploadSession, target["upload_session_id"])
        assert upload is not None
        raw_token = str(target["upload_headers"]["X-RVC-Upload-Token"])
        assert upload.upload_token_hash == hashlib.sha256(raw_token.encode()).hexdigest()
        assert raw_token not in upload.upload_token_hash

    canonical_path = (
        app.state.settings.local_storage_root
        / "test-sets"
        / "verified"
        / test_set_id
        / "items"
        / f"{target['upload_session_id']}.wav"
    )
    missing_path = canonical_path.with_suffix(".missing")
    canonical_path.rename(missing_path)
    missing_canonical = await client.post(
        f"/api/v1/test-sets/{test_set_id}/finalize",
        headers=headers,
    )
    assert missing_canonical.status_code == 409
    missing_path.rename(canonical_path)

    original_total_bytes = app.state.settings.test_set_max_total_bytes
    app.state.settings.test_set_max_total_bytes = len(content) - 1
    byte_limited = await client.post(
        f"/api/v1/test-sets/{test_set_id}/finalize",
        headers=headers,
    )
    assert byte_limited.status_code == 409
    assert byte_limited.json()["detail"] == "test set total byte limit exceeded"
    app.state.settings.test_set_max_total_bytes = original_total_bytes

    original_total_duration = app.state.settings.test_set_max_total_duration_seconds
    app.state.settings.test_set_max_total_duration_seconds = (
        finalized_item.json()["duration_seconds"] / 2
    )
    duration_limited = await client.post(
        f"/api/v1/test-sets/{test_set_id}/finalize",
        headers=headers,
    )
    assert duration_limited.status_code == 409
    assert duration_limited.json()["detail"] == "test set total duration limit exceeded"
    app.state.settings.test_set_max_total_duration_seconds = original_total_duration

    ready = await client.post(
        f"/api/v1/test-sets/{test_set_id}/finalize",
        headers=headers,
    )
    assert ready.status_code == 200, ready.text
    ready_body = ready.json()
    assert ready_body["status"] == "ready"
    assert ready_body["item_count"] == 1
    assert ready_body["manifest_sha256"]
    assert "storage_uri" not in ready.text
    manifest_path = (
        app.state.settings.local_storage_root
        / "test-sets"
        / "verified"
        / test_set_id
        / "manifest.json"
    )
    manifest_bytes = manifest_path.read_bytes()
    assert hashlib.sha256(manifest_bytes).hexdigest() == ready_body["manifest_sha256"]
    manifest = json.loads(manifest_bytes)
    assert manifest["items"][0]["item_key"] == "speech-clean"
    assert manifest["items"][0]["license_reference"] == "license:cc-by-4.0"
    assert "storage_uri" not in manifest_bytes.decode()
    assert str(finalized_item.json()["id"]) not in manifest_bytes.decode()
    listed = await client.get("/api/v1/test-sets", headers=headers)
    listed_entry = next(entry for entry in listed.json()["items"] if entry["id"] == test_set_id)
    assert listed_entry["item_count"] == 1
    assert listed_entry["items"] == []
    assert listed_entry["items_included"] is False
    assert ready_body["items_included"] is True
    assert (
        await client.post(
            f"/api/v1/test-sets/{test_set_id}/item-uploads/init",
            headers=headers,
            json={
                "item_key": "late",
                "display_name": "Late",
                "sort_order": 1,
                "filename": "late.wav",
                "content_type": "audio/wav",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "license_reference": "license:cc-by-4.0",
                "provenance_reference": "consent:late",
                "idempotency_key": "stable-upload-request-0003",
            },
        )
    ).status_code == 409
    assert (
        await client.delete(f"/api/v1/test-sets/{test_set_id}", headers=headers)
    ).status_code == 409


async def test_test_set_upload_rejects_bad_metadata_hash_and_pcm_then_allows_retry(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await _seed_user(app, "wav-validation@example.test")
    headers = await _login(client, owner.email)
    draft = await _create_test_set(client, headers, name="validation-ledger")
    test_set_id = str(draft["id"])
    content = _pcm_wav_bytes()
    base = {
        "item_key": "validation-item",
        "display_name": "Validation item",
        "sort_order": 0,
        "filename": "validation.wav",
        "content_type": "audio/wav",
        "size_bytes": len(content),
        "sha256": "0" * 64,
        "license_reference": "license:test",
        "provenance_reference": "consent:test",
        "idempotency_key": "invalid-hash-request-0001",
    }
    initialized = await client.post(
        f"/api/v1/test-sets/{test_set_id}/item-uploads/init",
        headers=headers,
        json=base,
    )
    assert initialized.status_code == 201
    target = initialized.json()
    assert (
        await client.put(
            str(target["upload_url"]),
            headers=target["upload_headers"],
            content=content,
        )
    ).status_code == 204
    mismatch = await client.post(
        f"/api/v1/test-sets/item-uploads/{target['upload_session_id']}/finalize",
        headers=headers,
    )
    assert mismatch.status_code == 422
    assert (
        await client.post(f"/api/v1/test-sets/{test_set_id}/finalize", headers=headers)
    ).status_code == 409

    retry_payload = {
        **base,
        "sha256": hashlib.sha256(content).hexdigest(),
        "idempotency_key": "valid-retry-request-0002",
    }
    retry = await client.post(
        f"/api/v1/test-sets/{test_set_id}/item-uploads/init",
        headers=headers,
        json=retry_payload,
    )
    assert retry.status_code == 201, retry.text
    retry_target = retry.json()
    assert (
        await client.put(
            str(retry_target["upload_url"]),
            headers=retry_target["upload_headers"],
            content=content,
        )
    ).status_code == 204
    assert (
        await client.post(
            f"/api/v1/test-sets/item-uploads/{retry_target['upload_session_id']}/finalize",
            headers=headers,
        )
    ).status_code == 200
    assert (
        await client.post(f"/api/v1/test-sets/{test_set_id}/finalize", headers=headers)
    ).status_code == 200

    traversal = await client.post(
        f"/api/v1/test-sets/{test_set_id}/item-uploads/init",
        headers=headers,
        json={**retry_payload, "filename": "../escape.wav"},
    )
    assert traversal.status_code in {409, 422}
    missing_provenance = await client.post(
        "/api/v1/test-sets",
        headers=headers,
        json={"name": "metadata-check", "description": None},
    )
    assert missing_provenance.status_code == 201
    invalid_metadata = await client.post(
        f"/api/v1/test-sets/{missing_provenance.json()['id']}/item-uploads/init",
        headers=headers,
        json={**retry_payload, "provenance_reference": ""},
    )
    assert invalid_metadata.status_code == 422
    for unsafe_reference in (
        "s3:private-bucket",
        "file:voice.wav",
        "https://example.test/license?token=secret",
        "license:record?token=secret",
        "license:../escape",
    ):
        unsafe_metadata = await client.post(
            f"/api/v1/test-sets/{missing_provenance.json()['id']}/item-uploads/init",
            headers=headers,
            json={**retry_payload, "license_reference": unsafe_reference},
        )
        assert unsafe_metadata.status_code == 422

    invalid_wav_set = await _create_test_set(client, headers, name="invalid-wav-ledger")
    invalid_wav = b"RIFF" + (36).to_bytes(4, "little") + b"WAVE" + b"\x00" * 32
    invalid_wav_init = await client.post(
        f"/api/v1/test-sets/{invalid_wav_set['id']}/item-uploads/init",
        headers=headers,
        json={
            "item_key": "broken-pcm",
            "display_name": "Broken PCM",
            "sort_order": 0,
            "filename": "broken.wav",
            "content_type": "audio/wav",
            "size_bytes": len(invalid_wav),
            "sha256": hashlib.sha256(invalid_wav).hexdigest(),
            "license_reference": "license:test",
            "provenance_reference": "consent:test",
            "idempotency_key": "invalid-wav-request-0001",
        },
    )
    assert invalid_wav_init.status_code == 201
    invalid_target = invalid_wav_init.json()
    assert (
        await client.put(
            str(invalid_target["upload_url"]),
            headers=invalid_target["upload_headers"],
            content=invalid_wav,
        )
    ).status_code == 204
    assert (
        await client.post(
            f"/api/v1/test-sets/item-uploads/{invalid_target['upload_session_id']}/finalize",
            headers=headers,
        )
    ).status_code == 422


async def test_finalization_failure_cas_preserves_wrong_token_and_completed_rows(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await _seed_user(app, "finalize-cas@example.test")
    headers = await _login(client, owner.email)
    draft = await _create_test_set(client, headers, name="finalize-cas-ledger")
    test_set_id = str(draft["id"])
    completed_target, _ = await _upload_test_set_item(
        client,
        headers,
        test_set_id=test_set_id,
        content=_pcm_wav_bytes(),
        idempotency_key="finalize-cas-completed-0001",
    )
    canonical_path = (
        app.state.settings.local_storage_root
        / "test-sets"
        / "verified"
        / test_set_id
        / "items"
        / f"{completed_target['upload_session_id']}.wav"
    )
    async with app.state.database.session_factory() as session:
        transitioned = await _mark_finalizing_upload_failed(
            str(completed_target["upload_session_id"]),
            "stale-token",
            "must_not_overwrite_completed",
            session=session,
        )
        assert transitioned is False
        completed = await session.get(
            LedgerTestSetItemUploadSession,
            completed_target["upload_session_id"],
        )
        assert completed is not None
        assert completed.status == "completed"
        assert completed.failure_code is None
    assert canonical_path.is_file()

    content = _pcm_wav_bytes(frames=800)
    pending = await client.post(
        f"/api/v1/test-sets/{test_set_id}/item-uploads/init",
        headers=headers,
        json={
            "item_key": "pending-cas",
            "display_name": "Pending CAS",
            "sort_order": 1,
            "filename": "pending-cas.wav",
            "content_type": "audio/wav",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "license_reference": "license-record:cas",
            "provenance_reference": "consent-record:cas",
            "idempotency_key": "finalize-cas-pending-0002",
        },
    )
    assert pending.status_code == 201
    pending_id = pending.json()["upload_session_id"]
    async with app.state.database.session_factory() as session:
        upload = await session.get(LedgerTestSetItemUploadSession, pending_id)
        assert upload is not None
        upload.status = "finalizing"
        upload.finalization_token = "correct-token"
        await session.commit()
        wrong_token = await _mark_finalizing_upload_failed(
            pending_id,
            "wrong-token",
            "must_not_transition",
            session=session,
        )
        assert wrong_token is False
        await session.refresh(upload)
        assert upload.status == "finalizing"
        assert upload.finalization_token == "correct-token"
        wrong_generation = await _mark_finalizing_upload_failed(
            pending_id,
            "correct-token",
            "must_not_transition_generation",
            session=session,
            generation=upload.generation + 1,
        )
        assert wrong_generation is False
        await session.refresh(upload)
        assert upload.status == "finalizing"
        correct_token = await _mark_finalizing_upload_failed(
            pending_id,
            "correct-token",
            "test_cleanup",
            session=session,
            generation=upload.generation,
        )
        assert correct_token is True
        await session.refresh(upload)
        assert upload.status == "failed"


async def test_storage_namespace_mismatch_preserves_upload_ledger(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await _seed_user(app, "namespace-mismatch@example.test")
    headers = await _login(client, owner.email)
    draft = await _create_test_set(client, headers, name="namespace-mismatch-ledger")
    test_set_id = str(draft["id"])
    content = _pcm_wav_bytes()
    payload = {
        "item_key": "namespace-item",
        "display_name": "Namespace item",
        "sort_order": 0,
        "filename": "namespace.wav",
        "content_type": "audio/wav",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "license_reference": "license-record:namespace",
        "provenance_reference": "consent-record:namespace",
        "idempotency_key": "namespace-mismatch-request-0001",
    }
    initialized = await client.post(
        f"/api/v1/test-sets/{test_set_id}/item-uploads/init",
        headers=headers,
        json=payload,
    )
    assert initialized.status_code == 201
    upload_id = initialized.json()["upload_session_id"]
    async with app.state.database.session_factory() as session:
        upload = await session.get(LedgerTestSetItemUploadSession, upload_id)
        assert upload is not None
        assert upload.storage_namespace_sha256 == app.state.storage.namespace_fingerprint
        upload.storage_namespace_sha256 = "0" * 64
        upload.expires_at = utc_now() - timedelta(seconds=1)
        await session.commit()

    retry = await client.post(
        f"/api/v1/test-sets/{test_set_id}/item-uploads/init",
        headers=headers,
        json=payload,
    )
    assert retry.status_code == 503
    assert (
        await client.post(
            f"/api/v1/test-sets/item-uploads/{upload_id}/finalize",
            headers=headers,
        )
    ).status_code == 503
    async with app.state.database.session_factory() as session:
        upload = await session.get(LedgerTestSetItemUploadSession, upload_id)
        assert upload is not None
        assert upload.status == "pending"
        upload.status = "failed"
        upload.failure_code = "forced_failure_for_delete_test"
        await session.commit()

    deleted = await client.delete(
        f"/api/v1/test-sets/{test_set_id}",
        headers=headers,
    )
    assert deleted.status_code == 503
    async with app.state.database.session_factory() as session:
        test_set = await session.get(LedgerTestSet, test_set_id)
        upload = await session.get(LedgerTestSetItemUploadSession, upload_id)
        assert test_set is not None
        assert test_set.status == "failed"
        assert test_set.failure_code == "delete_storage_backend_unavailable"
        assert upload is not None


async def test_sample_job_gate_and_storage_neutral_snapshot(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await _seed_user(app, "sample-job-owner@example.test")
    await _seed_user(app, "sample-job-other@example.test")
    headers = await _login(client, owner.email)
    other_headers = await _login(client, "sample-job-other@example.test")
    draft = await _create_test_set(client, headers, name="sample-job-ledger")
    test_set_id = str(draft["id"])
    _, item = await _upload_test_set_item(
        client,
        headers,
        test_set_id=test_set_id,
        content=_pcm_wav_bytes(),
    )
    ready = await client.post(
        f"/api/v1/test-sets/{test_set_id}/finalize",
        headers=headers,
    )
    assert ready.status_code == 200, ready.text
    dataset = await client.post(
        "/api/v1/datasets",
        headers=headers,
        json={
            "name": "sample-job-dataset",
            "storage_uri": "local:///legacy/raw.zip",
            "flat_storage_uri": "local:///legacy/prepared.zip",
        },
    )
    assert dataset.status_code == 201, dataset.text
    experiment = await client.post(
        "/api/v1/experiments",
        headers=headers,
        json={"name": "sample-job-experiment", "dataset_id": dataset.json()["id"]},
    )
    assert experiment.status_code == 201, experiment.text
    payload = {
        "job_name": "sample-snapshot-job",
        "experiment_id": experiment.json()["id"],
        "dataset_id": dataset.json()["id"],
        "model": {"version": "v2", "sample_rate": "40k"},
        "auto_inference_samples": {
            "enabled": True,
            "test_set_id": test_set_id,
            "inference_f0_method": "rmvpe",
            "transpose": 2,
            "index_rate": 0.75,
            "filter_radius": 3,
            "resample_sr": 0,
            "rms_mix_rate": 0.25,
            "protect": 0.33,
        },
    }
    gated = await client.post("/api/v1/jobs", headers=headers, json=payload)
    assert gated.status_code == 409
    app.state.settings.auto_sample_jobs_enabled = True
    app.state.settings.sample_approved_runtime_bundles = (
        f"{TEST_RUNTIME_IMAGE_DIGEST}@{TEST_RUNTIME_ASSET_MANIFEST_SHA256}"
    )
    foreign_dataset = await client.post(
        "/api/v1/datasets",
        headers=other_headers,
        json={
            "name": "foreign-sample-job-dataset",
            "storage_uri": "local:///legacy/foreign-raw.zip",
            "flat_storage_uri": "local:///legacy/foreign-prepared.zip",
        },
    )
    assert foreign_dataset.status_code == 201
    foreign_experiment = await client.post(
        "/api/v1/experiments",
        headers=other_headers,
        json={
            "name": "foreign-sample-job-experiment",
            "dataset_id": foreign_dataset.json()["id"],
        },
    )
    assert foreign_experiment.status_code == 201
    foreign_payload = {
        **payload,
        "job_name": "foreign-sample-snapshot",
        "experiment_id": foreign_experiment.json()["id"],
        "dataset_id": foreign_dataset.json()["id"],
    }
    foreign = await client.post("/api/v1/jobs", headers=other_headers, json=foreign_payload)
    assert foreign.status_code == 404
    created = await client.post("/api/v1/jobs", headers=headers, json=payload)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["test_set_id"] == test_set_id
    assert body["sample_plan_sha256"]
    assert body["preset_id"] is None

    async with app.state.database.session_factory() as session:
        job = await session.get(Job, body["id"])
        assert job is not None
        plan = job.sample_plan_json
        assert plan is not None
        assert canonical_sha256(plan) == job.sample_plan_sha256
        assert plan["items"][0]["test_set_item_id"] == item["id"]
        serialized = json.dumps(plan, sort_keys=True)
        assert "storage_uri" not in serialized
        assert "local:///" not in serialized
        assert plan["inference_config"]["transpose"] == 2

        ledger_item = await session.get(LedgerTestSetItem, item["id"])
        assert ledger_item is not None
        ledger_item.license_reference = "tampered-license"
        await session.commit()

    tampered_payload = {**payload, "job_name": "tampered-manifest-job"}
    tampered = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json=tampered_payload,
    )
    assert tampered.status_code == 409

    disabled_payload = {
        **payload,
        "job_name": "disabled-sample-job",
        "auto_inference_samples": {"enabled": False, "test_set_id": None},
    }
    disabled = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json=disabled_payload,
    )
    assert disabled.status_code == 201, disabled.text
    assert disabled.json()["test_set_id"] is None
    assert disabled.json()["sample_plan_sha256"] is None


async def test_sample_job_claim_and_item_download_are_capability_and_lease_bound(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await _seed_user(app, "sample-transfer-owner@example.test")
    headers = await _login(client, owner.email)
    draft = await _create_test_set(client, headers, name="sample-transfer-ledger")
    test_set_id = str(draft["id"])
    content = _pcm_wav_bytes(frames=2_400)
    upload_target, item = await _upload_test_set_item(
        client,
        headers,
        test_set_id=test_set_id,
        content=content,
        item_key="lease-bound-speech",
        idempotency_key="sample-transfer-upload-0001",
    )
    ready = await client.post(
        f"/api/v1/test-sets/{test_set_id}/finalize",
        headers=headers,
    )
    assert ready.status_code == 200, ready.text
    dataset = await client.post(
        "/api/v1/datasets",
        headers=headers,
        json={
            "name": "sample-transfer-dataset",
            "storage_uri": "local:///legacy/sample-transfer-raw.zip",
            "flat_storage_uri": "local:///legacy/sample-transfer-flat.zip",
        },
    )
    assert dataset.status_code == 201, dataset.text
    experiment = await client.post(
        "/api/v1/experiments",
        headers=headers,
        json={
            "name": "sample-transfer-experiment",
            "dataset_id": dataset.json()["id"],
        },
    )
    assert experiment.status_code == 201, experiment.text
    app.state.settings.auto_sample_jobs_enabled = True
    app.state.settings.sample_approved_runtime_bundles = (
        f"{TEST_RUNTIME_IMAGE_DIGEST}@{TEST_RUNTIME_ASSET_MANIFEST_SHA256}"
    )
    job_payload = {
        "job_name": "sample-transfer-job",
        "experiment_id": experiment.json()["id"],
        "dataset_id": dataset.json()["id"],
        "model": {"version": "v2", "sample_rate": "40k"},
        "auto_inference_samples": {
            "enabled": True,
            "test_set_id": test_set_id,
            "inference_f0_method": "rmvpe",
            "transpose": 0,
            "index_rate": 0.75,
            "filter_radius": 3,
            "resample_sr": 0,
            "rms_mix_rate": 0.25,
            "protect": 0.33,
        },
    }
    created = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json=job_payload,
    )
    assert created.status_code == 201, created.text

    unready_token = await _register_sample_worker(
        client,
        "sample-transfer-unready",
        sample_ready=False,
    )
    unready_claim = await client.post(
        "/api/v1/workers/jobs/claim",
        headers={"Authorization": f"Bearer {unready_token}"},
        json={"max_wait_seconds": 0},
    )
    assert unready_claim.status_code == 204

    wrong_method_token = await _register_sample_worker(
        client,
        "sample-transfer-wrong-method",
        sample_ready=True,
        inference_methods=["pm"],
    )
    wrong_method_claim = await client.post(
        "/api/v1/workers/jobs/claim",
        headers={"Authorization": f"Bearer {wrong_method_token}"},
        json={"max_wait_seconds": 0},
    )
    assert wrong_method_claim.status_code == 204

    worker_token = await _register_sample_worker(
        client,
        "sample-transfer-ready",
        sample_ready=True,
    )
    worker_headers = {"Authorization": f"Bearer {worker_token}"}
    claimed = await client.post(
        "/api/v1/workers/jobs/claim",
        headers=worker_headers,
        json={"max_wait_seconds": 0},
    )
    assert claimed.status_code == 200, claimed.text
    claim = claimed.json()
    transfer = claim["test_set_transfer"]
    assert transfer["test_set_id"] == test_set_id
    assert transfer["family_id"] == ready.json()["family_id"]
    assert transfer["revision"] == 1
    assert transfer["manifest_sha256"] == ready.json()["manifest_sha256"]
    assert transfer["sample_plan_sha256"] == created.json()["sample_plan_sha256"]
    assert "storage_uri" not in json.dumps(transfer, sort_keys=True)
    assert "?" not in transfer["items"][0]["download_path"]
    descriptor = transfer["items"][0]
    assert descriptor["test_set_item_id"] == item["id"]
    assert descriptor["filename"] == f"{item['id']}.wav"

    lease_headers = {
        **worker_headers,
        "X-RVC-Lease-ID": claim["lease_id"],
        "X-RVC-Attempt-ID": claim["attempt_id"],
    }
    wrong_lease = await client.get(
        descriptor["download_path"],
        headers={**lease_headers, "X-RVC-Lease-ID": "wrong-lease"},
    )
    assert wrong_lease.status_code == 409
    missing = await client.get(
        f"/api/v1/workers/jobs/{claim['job_id']}/test-set/items/missing-item",
        headers=lease_headers,
    )
    assert missing.status_code == 404
    downloaded = await client.get(descriptor["download_path"], headers=lease_headers)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == content
    assert downloaded.headers["content-type"].startswith("audio/wav")
    assert downloaded.headers["content-length"] == str(len(content))
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert downloaded.headers["vary"] == "Authorization"

    async with app.state.database.session_factory() as session:
        upload = await session.get(
            LedgerTestSetItemUploadSession,
            upload_target["upload_session_id"],
        )
        assert upload is not None
        original_namespace = upload.storage_namespace_sha256
        upload.storage_namespace_sha256 = "0" * 64
        await session.commit()
    unavailable = await client.get(descriptor["download_path"], headers=lease_headers)
    assert unavailable.status_code == 503
    async with app.state.database.session_factory() as session:
        upload = await session.get(
            LedgerTestSetItemUploadSession,
            upload_target["upload_session_id"],
        )
        assert upload is not None
        upload.storage_namespace_sha256 = original_namespace
        await session.commit()

    second = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={**job_payload, "job_name": "sample-transfer-manifest-recheck"},
    )
    assert second.status_code == 201, second.text
    manifest_path = (
        app.state.settings.local_storage_root
        / "test-sets"
        / "verified"
        / test_set_id
        / "manifest.json"
    )
    original_manifest = manifest_path.read_bytes()
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(b'{"tampered":true}')
    verifier_token = await _register_sample_worker(
        client,
        "sample-transfer-manifest-verifier",
        sample_ready=True,
    )
    verifier_headers = {"Authorization": f"Bearer {verifier_token}"}
    try:
        rejected_claim = await client.post(
            "/api/v1/workers/jobs/claim",
            headers=verifier_headers,
            json={"max_wait_seconds": 0},
        )
        assert rejected_claim.status_code == 204
        async with app.state.database.session_factory() as session:
            queued = await session.get(Job, second.json()["id"])
            verifier = await session.scalar(
                select(Worker).where(Worker.name == "sample-transfer-manifest-verifier")
            )
            assert queued is not None and queued.status == "queued"
            assert verifier is not None and verifier.current_job_id is None
    finally:
        manifest_path.write_bytes(original_manifest)
        manifest_path.chmod(0o440)
    recovered_claim = await client.post(
        "/api/v1/workers/jobs/claim",
        headers=verifier_headers,
        json={"max_wait_seconds": 0},
    )
    assert recovered_claim.status_code == 200, recovered_claim.text
    assert recovered_claim.json()["job_id"] == second.json()["id"]

    third = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={**job_payload, "job_name": "sample-transfer-plan-recheck"},
    )
    assert third.status_code == 201, third.text
    async with app.state.database.session_factory() as session:
        plan_tampered = await session.get(Job, third.json()["id"])
        assert plan_tampered is not None
        original_plan_sha256 = plan_tampered.sample_plan_sha256
        plan_tampered.sample_plan_sha256 = "0" * 64
        await session.commit()
    plan_verifier_token = await _register_sample_worker(
        client,
        "sample-transfer-plan-verifier",
        sample_ready=True,
    )
    plan_verifier_headers = {"Authorization": f"Bearer {plan_verifier_token}"}
    rejected_plan_claim = await client.post(
        "/api/v1/workers/jobs/claim",
        headers=plan_verifier_headers,
        json={"max_wait_seconds": 0},
    )
    assert rejected_plan_claim.status_code == 204
    async with app.state.database.session_factory() as session:
        plan_tampered = await session.get(Job, third.json()["id"])
        plan_verifier = await session.scalar(
            select(Worker).where(Worker.name == "sample-transfer-plan-verifier")
        )
        assert plan_tampered is not None and plan_tampered.status == "queued"
        assert plan_verifier is not None and plan_verifier.current_job_id is None
        plan_tampered.sample_plan_sha256 = original_plan_sha256
        await session.commit()
    recovered_plan_claim = await client.post(
        "/api/v1/workers/jobs/claim",
        headers=plan_verifier_headers,
        json={"max_wait_seconds": 0},
    )
    assert recovered_plan_claim.status_code == 200, recovered_plan_claim.text
    assert recovered_plan_claim.json()["job_id"] == third.json()["id"]


async def test_sample_composite_foreign_keys_reject_cross_ledger_mixing(
    app: FastAPI,
) -> None:
    owner = await _seed_user(app, "sample-fk-owner@example.test")
    async with app.state.database.session_factory() as session:
        dataset = Dataset(
            name="sample-fk-dataset",
            storage_uri="local:///fixtures/dataset.zip",
            flat_storage_uri="local:///fixtures/prepared.zip",
            status="legacy_imported",
            is_usable=True,
            decoder_pending_count=0,
            retryable=False,
            created_by=owner.id,
        )
        session.add(dataset)
        await session.flush()
        experiment = Experiment(
            name="sample-fk-experiment",
            dataset_id=dataset.id,
            description=None,
            created_by=owner.id,
        )
        worker = Worker(
            name="sample-fk-worker",
            token_hash="f" * 64,
            capabilities_json={},
            worker_version="test",
            rvc_commit_hash="a" * 40,
        )
        first_set = LedgerTestSet(
            family_id="10000000-0000-4000-8000-000000000001",
            name="sample-fk-first",
            revision=1,
            status="ready",
            item_count=1,
            created_by=owner.id,
        )
        second_set = LedgerTestSet(
            family_id="20000000-0000-4000-8000-000000000002",
            name="sample-fk-second",
            revision=1,
            status="ready",
            item_count=1,
            created_by=owner.id,
        )
        session.add_all((experiment, worker, first_set, second_set))
        await session.flush()
        first_item = LedgerTestSetItem(
            test_set_id=first_set.id,
            item_key="first",
            display_name="First",
            sort_order=0,
            storage_uri="local:///test-sets/first.wav",
            original_filename="first.wav",
            size_bytes=100,
            sha256="1" * 64,
            mime_type="audio/wav",
            sample_rate_hz=16_000,
            channels=1,
            duration_seconds=0.1,
            license_reference="license-record:first",
            provenance_reference="consent-record:first",
        )
        second_item = LedgerTestSetItem(
            test_set_id=second_set.id,
            item_key="second",
            display_name="Second",
            sort_order=0,
            storage_uri="local:///test-sets/second.wav",
            original_filename="second.wav",
            size_bytes=100,
            sha256="2" * 64,
            mime_type="audio/wav",
            sample_rate_hz=16_000,
            channels=1,
            duration_seconds=0.1,
            license_reference="license-record:second",
            provenance_reference="consent-record:second",
        )
        first_job = Job(
            experiment_id=experiment.id,
            dataset_id=dataset.id,
            job_name="sample-fk-job-1",
            status="completed",
            config_json={},
            test_set_id=first_set.id,
            priority=5,
            total_epoch=1,
        )
        second_job = Job(
            experiment_id=experiment.id,
            dataset_id=dataset.id,
            job_name="sample-fk-job-2",
            status="completed",
            config_json={},
            test_set_id=first_set.id,
            priority=5,
            total_epoch=1,
        )
        session.add_all((first_item, second_item, first_job, second_job))
        await session.flush()
        first_attempt = JobAttempt(
            job_id=first_job.id,
            worker_id=worker.id,
            attempt_number=1,
            engine_mode="test",
            status="completed",
        )
        second_attempt = JobAttempt(
            job_id=second_job.id,
            worker_id=worker.id,
            attempt_number=1,
            engine_mode="test",
            status="completed",
        )
        session.add_all((first_attempt, second_attempt))
        await session.flush()

        def artifact(
            identifier: str,
            job_id: str,
            attempt_id: str,
            sha_character: str,
        ) -> Artifact:
            return Artifact(
                id=identifier,
                job_id=job_id,
                attempt_id=attempt_id,
                artifact_type="sample",
                filename=f"{identifier}.wav",
                storage_uri=f"local:///samples/{identifier}.wav",
                size_bytes=100,
                sha256=sha_character * 64,
                mime_type="audio/wav",
                metadata_json={},
            )

        artifacts = [
            artifact("30000000-0000-4000-8000-000000000001", first_job.id, first_attempt.id, "3"),
            artifact("30000000-0000-4000-8000-000000000002", second_job.id, second_attempt.id, "4"),
            artifact("30000000-0000-4000-8000-000000000003", first_job.id, first_attempt.id, "5"),
            artifact("30000000-0000-4000-8000-000000000004", first_job.id, first_attempt.id, "6"),
        ]
        session.add_all(artifacts)
        await session.commit()

        def sample(
            *,
            job_id: str,
            attempt_id: str,
            test_set_id: str,
            item_id: str,
            artifact_id: str,
            config_character: str,
            output_character: str,
        ) -> LedgerSample:
            return LedgerSample(
                job_id=job_id,
                attempt_id=attempt_id,
                test_set_id=test_set_id,
                test_set_item_id=item_id,
                artifact_id=artifact_id,
                input_sha256="1" * 64,
                model_sha256="7" * 64,
                index_sha256=None,
                inference_f0_method="rmvpe",
                inference_config_sha256=config_character * 64,
                native_inference_manifest_sha256="c" * 64,
                native_inference_request_sha256="d" * 64,
                output_size_bytes=100,
                output_sha256=output_character * 64,
                output_sample_rate_hz=16_000,
                output_channels=1,
                output_duration_seconds=0.1,
                metrics_json={},
                rvc_commit_hash="a" * 40,
                runtime_image_digest="sha256:" + "9" * 64,
                runtime_asset_manifest_sha256="b" * 64,
            )

        session.add(
            sample(
                job_id=first_job.id,
                attempt_id=first_attempt.id,
                test_set_id=first_set.id,
                item_id=first_item.id,
                artifact_id=artifacts[0].id,
                config_character="c",
                output_character="3",
            )
        )
        await session.commit()

        invalid_samples = (
            sample(
                job_id=first_job.id,
                attempt_id=second_attempt.id,
                test_set_id=first_set.id,
                item_id=first_item.id,
                artifact_id=artifacts[1].id,
                config_character="d",
                output_character="4",
            ),
            sample(
                job_id=first_job.id,
                attempt_id=first_attempt.id,
                test_set_id=first_set.id,
                item_id=second_item.id,
                artifact_id=artifacts[2].id,
                config_character="e",
                output_character="5",
            ),
            sample(
                job_id=first_job.id,
                attempt_id=first_attempt.id,
                test_set_id=second_set.id,
                item_id=second_item.id,
                artifact_id=artifacts[3].id,
                config_character="f",
                output_character="6",
            ),
            sample(
                job_id=first_job.id,
                attempt_id=first_attempt.id,
                test_set_id=first_set.id,
                item_id=first_item.id,
                artifact_id=artifacts[1].id,
                config_character="0",
                output_character="4",
            ),
        )
        for invalid in invalid_samples:
            session.add(invalid)
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()
