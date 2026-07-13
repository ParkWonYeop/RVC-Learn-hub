from __future__ import annotations

import asyncio
import hashlib
import io
import struct
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import OperationalError
from starlette.concurrency import run_in_threadpool

import rvc_manager_api.routers.artifacts as artifact_routes
import rvc_manager_api.routers.workers as worker_routes
import rvc_manager_api.services.samples as sample_services
from rvc_manager_api.config import Settings
from rvc_manager_api.models import ArtifactUploadSession, Job, JobAttempt, JobLease, Sample, User
from rvc_manager_api.security import hash_password
from rvc_manager_api.services.artifacts import remove_spool_file
from rvc_manager_api.services.samples import inspect_sample_pcm_wav
from rvc_manager_api.storage import StorageError
from rvc_orchestrator_contracts import (
    RVC_REVIEWED_COMMIT,
    SAMPLE_MAX_TOTAL_OUTPUT_BYTES,
    SAMPLE_MAX_TOTAL_OUTPUT_DURATION_SECONDS,
    utc_now,
)

USER_PASSWORD = "sample-registration-password-1234"
IMAGE_DIGEST = "sha256:" + "9" * 64
ASSET_MANIFEST_SHA256 = "a" * 64
NATIVE_INFERENCE_MANIFEST_SHA256 = "b" * 64
NATIVE_INFERENCE_REQUEST_SHA256 = "c" * 64


def test_sample_settings_require_approved_runtime_and_reserved_artifact_slots() -> None:
    with pytest.raises(ValidationError, match="SAMPLE_APPROVED_RUNTIME_BUNDLES"):
        Settings(environment="test", auto_sample_jobs_enabled=True)
    with pytest.raises(ValidationError, match="reserve at least eight"):
        Settings(
            environment="test",
            artifact_attempt_max_sessions=135,
            test_set_max_items=128,
        )
    settings = Settings(
        environment="test",
        auto_sample_jobs_enabled=True,
        sample_approved_runtime_bundles=f"{IMAGE_DIGEST}@{ASSET_MANIFEST_SHA256}",
    )
    assert settings.approved_sample_runtime_bundles == {(IMAGE_DIGEST, ASSET_MANIFEST_SHA256)}


@pytest.mark.asyncio
async def test_sample_registration_raw_body_limit_rejects_declared_and_chunked_bodies(
    client: AsyncClient,
) -> None:
    oversized = b'{"lease_id":"' + (b"x" * (65 * 1024)) + b'"}'
    declared = await client.post(
        "/api/v1/workers/jobs/body-limit/samples",
        content=oversized,
        headers={"Content-Type": "application/json"},
    )
    assert declared.status_code == 413
    assert declared.json()["detail"] == "sample registration body is too large"

    async def chunks() -> Any:
        for offset in range(0, len(oversized), 1024):
            yield oversized[offset : offset + 1024]

    chunked = await client.post(
        "/api/v1/workers/jobs/body-limit/samples",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert chunked.status_code == 413
    assert chunked.json()["detail"] == "sample registration body is too large"


@pytest.mark.asyncio
async def test_sample_registration_openapi_declares_replay_and_operational_errors(
    client: AsyncClient,
) -> None:
    schema = (await client.get("/openapi.json")).json()
    responses = schema["paths"]["/api/v1/workers/jobs/{job_id}/samples"]["post"]["responses"]
    assert {"200", "201", "409", "413", "422", "429", "503"}.issubset(responses)
    download_responses = schema["paths"]["/api/v1/samples/{sample_id}/download"]["get"][
        "responses"
    ]
    assert {"200", "206", "409", "416", "429", "503"}.issubset(download_responses)


@pytest.mark.asyncio
async def test_verified_sample_file_response_releases_spool_and_slot_on_disconnect(
    tmp_path: Path,
) -> None:
    spool = tmp_path / "verified-sample.wav"
    spool.write_bytes(b"RIFF" + b"x" * (128 * 1024))
    held_slot = asyncio.Semaphore(0)
    response = artifact_routes._VerifiedSampleFileResponse(
        path=spool,
        media_type="audio/wav",
        headers={"ETag": '"stable"'},
        verification_semaphore=held_slot,
    )
    body_started = asyncio.Event()
    keep_client_open = asyncio.Event()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            body_started.set()
            await keep_client_open.wait()

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/api/v1/samples/example/download",
        "raw_path": b"/api/v1/samples/example/download",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("manager.test", 443),
    }
    transfer = asyncio.create_task(response(scope, receive, send))  # type: ignore[arg-type]
    await asyncio.wait_for(body_started.wait(), timeout=1)
    assert spool.exists()
    assert held_slot.locked()

    transfer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await transfer
    assert not spool.exists()
    await asyncio.wait_for(held_slot.acquire(), timeout=1)


@dataclass(slots=True)
class SampleJobContext:
    owner_headers: dict[str, str]
    other_headers: dict[str, str]
    worker_headers: dict[str, str]
    job_id: str
    claim: dict[str, Any]


def _wav(*, sample_rate: int, frames: int, value: int = 0) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(value.to_bytes(2, "little", signed=True) * frames)
    return output.getvalue()


def _rail_wav(sample_width: int) -> bytes:
    bits = sample_width * 8
    minimum = -(1 << (bits - 1))
    maximum = (1 << (bits - 1)) - 1
    if sample_width == 1:
        frames = bytes((0, 128, 255))
    elif sample_width == 2:
        frames = struct.pack("<hhh", minimum, 0, maximum)
    elif sample_width == 3:
        frames = b"".join(
            value.to_bytes(3, "little", signed=True) for value in (minimum, 0, maximum)
        )
    else:
        frames = struct.pack("<iii", minimum, 0, maximum)
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(sample_width)
        audio.setframerate(8_000)
        audio.writeframes(frames)
    return output.getvalue()


@pytest.mark.parametrize("sample_width", [1, 2, 3, 4])
def test_pcm_v2_counts_both_integer_rails_as_clipped(
    tmp_path: Path,
    sample_width: int,
) -> None:
    source = tmp_path / f"rails-{sample_width}.wav"
    source.write_bytes(_rail_wav(sample_width))
    inspection = inspect_sample_pcm_wav(source, Settings(environment="test"))
    assert inspection.metrics.peak_amplitude == 1.0
    assert inspection.metrics.clipping_ratio == pytest.approx(2 / 3)
    assert inspection.metrics.silence_ratio == pytest.approx(1 / 3)


@pytest.mark.asyncio
async def test_cancelled_sample_inspection_joins_thread_before_cleanup_and_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool_path = tmp_path / "sample-inspection.wav"
    spool_path.write_bytes(_wav(sample_rate=40_000, frames=4_000))
    settings = Settings(environment="test")
    inspection_started = threading.Event()
    finish_inspection = threading.Event()
    original_inspection = artifact_routes.inspect_sample_pcm_wav

    def blocked_inspection(
        path: Path,
        supplied_settings: Settings,
        *,
        deadline_monotonic: float | None = None,
    ) -> Any:
        inspection_started.set()
        assert finish_inspection.wait(timeout=5)
        return original_inspection(
            path,
            supplied_settings,
            deadline_monotonic=deadline_monotonic,
        )

    monkeypatch.setattr(artifact_routes, "inspect_sample_pcm_wav", blocked_inspection)
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()

    async def route_inspection_boundary() -> Any:
        try:
            return await artifact_routes._inspect_sample_pcm_wav_joined(
                spool_path,
                settings,
                deadline_monotonic=time.monotonic() + 30,
            )
        finally:
            try:
                await remove_spool_file(spool_path)
            finally:
                semaphore.release()

    request_task = asyncio.create_task(route_inspection_boundary())
    assert await asyncio.wait_for(
        asyncio.to_thread(inspection_started.wait, 2),
        timeout=3,
    )
    request_task.cancel()
    await asyncio.sleep(0)
    request_task.cancel()
    await asyncio.sleep(0)

    assert not request_task.done()
    assert spool_path.is_file()
    assert semaphore.locked()

    finish_inspection.set()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    assert not spool_path.exists()
    assert not semaphore.locked()


async def _user(app: FastAPI, email: str) -> User:
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


async def _login(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": USER_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _test_set_item(
    client: AsyncClient,
    headers: dict[str, str],
    test_set_id: str,
    *,
    item_key: str,
    sort_order: int,
    content: bytes,
) -> dict[str, Any]:
    initialized = await client.post(
        f"/api/v1/test-sets/{test_set_id}/item-uploads/init",
        headers=headers,
        json={
            "item_key": item_key,
            "display_name": item_key,
            "sort_order": sort_order,
            "filename": f"{item_key}.wav",
            "content_type": "audio/wav",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "license_reference": "license:test",
            "provenance_reference": "consent:test",
            "idempotency_key": f"sample-test-set-{item_key}-0001",
        },
    )
    assert initialized.status_code == 201, initialized.text
    target = initialized.json()
    uploaded = await client.put(
        target["upload_url"],
        headers=target["upload_headers"],
        content=content,
    )
    assert uploaded.status_code == 204, uploaded.text
    finalized = await client.post(
        f"/api/v1/test-sets/item-uploads/{target['upload_session_id']}/finalize",
        headers=headers,
    )
    assert finalized.status_code == 200, finalized.text
    return cast(dict[str, Any], finalized.json())


async def _sample_job(
    app: FastAPI,
    client: AsyncClient,
    *,
    suffix: str,
    item_count: int = 1,
    resample_sr: int = 0,
    index_rate: float = 0.75,
    build_index: bool = True,
) -> SampleJobContext:
    owner = await _user(app, f"sample-owner-{suffix}@example.test")
    other = await _user(app, f"sample-other-{suffix}@example.test")
    owner_headers = await _login(client, owner.email)
    other_headers = await _login(client, other.email)
    test_set = await client.post(
        "/api/v1/test-sets",
        headers=owner_headers,
        json={"name": f"sample-set-{suffix}"},
    )
    assert test_set.status_code == 201, test_set.text
    test_set_id = test_set.json()["id"]
    for index in range(item_count):
        await _test_set_item(
            client,
            owner_headers,
            test_set_id,
            item_key=f"speech-{index}",
            sort_order=index,
            content=_wav(sample_rate=16_000, frames=1_600 + index * 400),
        )
    ready = await client.post(
        f"/api/v1/test-sets/{test_set_id}/finalize",
        headers=owner_headers,
    )
    assert ready.status_code == 200, ready.text
    dataset = await client.post(
        "/api/v1/datasets",
        headers=owner_headers,
        json={
            "name": f"sample-dataset-{suffix}",
            "storage_uri": f"local:///legacy/{suffix}.zip",
            "flat_storage_uri": f"local:///legacy/{suffix}-flat.zip",
        },
    )
    assert dataset.status_code == 201, dataset.text
    experiment = await client.post(
        "/api/v1/experiments",
        headers=owner_headers,
        json={
            "name": f"sample-experiment-{suffix}",
            "dataset_id": dataset.json()["id"],
        },
    )
    assert experiment.status_code == 201, experiment.text
    app.state.settings.auto_sample_jobs_enabled = True
    app.state.settings.sample_approved_runtime_bundles = f"{IMAGE_DIGEST}@{ASSET_MANIFEST_SHA256}"
    job = await client.post(
        "/api/v1/jobs",
        headers=owner_headers,
        json={
            "job_name": f"sample-job-{suffix}",
            "experiment_id": experiment.json()["id"],
            "dataset_id": dataset.json()["id"],
            "rvc_backend": {
                "backend_type": "rvc_webui",
                "rvc_version": "v2",
                "rvc_commit_hash": RVC_REVIEWED_COMMIT,
            },
            "model": {"version": "v2", "sample_rate": "40k"},
            "index": {"build_index": build_index},
            "auto_inference_samples": {
                "enabled": True,
                "test_set_id": test_set_id,
                "inference_f0_method": "rmvpe",
                "index_rate": index_rate,
                "resample_sr": resample_sr,
            },
        },
    )
    assert job.status_code == 201, job.text
    registered = await client.post(
        "/api/v1/workers/register",
        headers={"X-Worker-Bootstrap-Token": "test-bootstrap-token"},
        json={
            "name": f"sample-worker-{suffix}",
            "capabilities": {
                "engine_mode": "rvc_webui",
                "worker_version": "sample-registration-test",
                "rvc_commit_hash": RVC_REVIEWED_COMMIT,
                "supported_rvc_versions": ["v2"],
                "supported_training_f0_methods": ["rmvpe"],
                "supported_inference_f0_methods": ["rmvpe"],
                "fixed_test_set_inference_ready": True,
                "gpus": [
                    {
                        "index": 0,
                        "name": "Sample Test GPU",
                        "total_vram_mb": 24576,
                        "free_vram_mb": 24000,
                    }
                ],
                "disk_free_bytes": 100 * 1024**3,
                "rvc_assets_ready": True,
                "runtime_image_digest": IMAGE_DIGEST,
                "runtime_asset_manifest_sha256": ASSET_MANIFEST_SHA256,
            },
        },
    )
    assert registered.status_code == 201, registered.text
    worker_token = registered.json()["worker_token"]
    worker_headers = {"Authorization": f"Bearer {worker_token}"}
    claimed = await client.post(
        "/api/v1/workers/jobs/claim",
        headers=worker_headers,
        json={"max_wait_seconds": 0},
    )
    assert claimed.status_code == 200, claimed.text
    return SampleJobContext(
        owner_headers=owner_headers,
        other_headers=other_headers,
        worker_headers=worker_headers,
        job_id=job.json()["id"],
        claim=claimed.json(),
    )


def _provenance() -> dict[str, object]:
    return {
        "rvc_commit_hash": RVC_REVIEWED_COMMIT,
        "runtime_image_digest": IMAGE_DIGEST,
        "runtime_asset_manifest_sha256": ASSET_MANIFEST_SHA256,
        "native_inference_manifest_sha256": NATIVE_INFERENCE_MANIFEST_SHA256,
        "native_inference_request_sha256": NATIVE_INFERENCE_REQUEST_SHA256,
    }


async def _artifact(
    client: AsyncClient,
    context: SampleJobContext,
    *,
    artifact_type: str,
    filename: str,
    content_type: str,
    content: bytes,
    key: str,
) -> tuple[dict[str, Any], str]:
    digest = hashlib.sha256(content).hexdigest()
    initialized = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/artifact-uploads/init",
        headers=context.worker_headers,
        json={
            "lease_id": context.claim["lease_id"],
            "attempt_id": context.claim["attempt_id"],
            "idempotency_key": key,
            "artifact_type": artifact_type,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": len(content),
            "sha256": digest,
            "metadata": {
                **_provenance(),
                "native_sample_role": {
                    "final_small_model": "sample_model",
                    "final_index": "sample_index",
                    "sample": "sample_output",
                }[artifact_type],
            },
        },
    )
    assert initialized.status_code == 201, initialized.text
    target = initialized.json()
    uploaded = await client.put(
        target["upload_url"],
        headers=target["upload_headers"],
        content=content,
    )
    assert uploaded.status_code == 204, uploaded.text
    finalized = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/artifact-uploads/"
        f"{target['upload_session_id']}/finalize",
        headers=context.worker_headers,
        json={
            "lease_id": context.claim["lease_id"],
            "attempt_id": context.claim["attempt_id"],
        },
    )
    assert finalized.status_code == 200, finalized.text
    return finalized.json(), target["upload_session_id"]


def _registration(
    context: SampleJobContext,
    *,
    descriptor: dict[str, Any],
    artifact: dict[str, Any],
    model_sha256: str,
    index_sha256: str | None,
    frames: int,
    sample_rate: int = 40_000,
) -> dict[str, Any]:
    transfer = context.claim["test_set_transfer"]
    return {
        "lease_id": context.claim["lease_id"],
        "attempt_id": context.claim["attempt_id"],
        "test_set_id": transfer["test_set_id"],
        "test_set_item_id": descriptor["test_set_item_id"],
        "artifact_id": artifact["id"],
        "sample_plan_sha256": transfer["sample_plan_sha256"],
        "input_sha256": descriptor["sha256"],
        "model_sha256": model_sha256,
        "index_sha256": index_sha256,
        "inference_f0_method": "rmvpe",
        "inference_config_sha256": transfer["inference_config_sha256"],
        "output_size_bytes": artifact["size_bytes"],
        "output_sha256": artifact["sha256"],
        "output_sample_rate_hz": sample_rate,
        "output_channels": 1,
        "output_duration_seconds": frames / sample_rate,
        "metrics": {
            "peak_amplitude": 0.0,
            "rms": 0.0,
            "clipping_ratio": 0.0,
            "silence_ratio": 1.0,
        },
        **_provenance(),
    }


async def _advance_to_uploading(
    client: AsyncClient,
    context: SampleJobContext,
) -> None:
    for target in (
        "downloading_dataset",
        "validating_dataset",
        "preparing_flat_dataset",
        "preprocessing",
        "extracting_f0",
        "extracting_features",
        "training",
        "saving_checkpoint",
        "building_index",
        "collecting_small_model",
        "generating_samples",
        "evaluating",
        "uploading_artifacts",
    ):
        updated = await client.post(
            f"/api/v1/workers/jobs/{context.job_id}/status",
            headers=context.worker_headers,
            json={"lease_id": context.claim["lease_id"], "status": target},
        )
        assert updated.status_code == 200, updated.text


async def test_verified_sample_registration_replay_completion_list_and_download(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await _sample_job(app, client, suffix="happy", item_count=2)
    model, _ = await _artifact(
        client,
        context,
        artifact_type="final_small_model",
        filename="final_small_model.pth",
        content_type="application/x-pytorch",
        content=b"verified-model" * 32,
        key="sample-model-happy-0001",
    )
    index, _ = await _artifact(
        client,
        context,
        artifact_type="final_index",
        filename="final.index",
        content_type="application/octet-stream",
        content=b"verified-index" * 32,
        key="sample-index-happy-0001",
    )
    descriptors = context.claim["test_set_transfer"]["items"]
    first_bytes = _wav(sample_rate=40_000, frames=4_000)
    first_artifact, first_upload_id = await _artifact(
        client,
        context,
        artifact_type="sample",
        filename="sample-first.wav",
        content_type="audio/wav",
        content=first_bytes,
        key="sample-output-happy-0001",
    )
    first_payload = _registration(
        context,
        descriptor=descriptors[0],
        artifact=first_artifact,
        model_sha256=model["sha256"],
        index_sha256=index["sha256"],
        frames=4_000,
    )
    semaphore = app.state.sample_verification_semaphore
    for _ in range(app.state.settings.sample_verification_max_concurrency):
        await semaphore.acquire()
    saturated = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json=first_payload,
    )
    assert saturated.status_code == 429
    assert saturated.headers["retry-after"] == "1"
    for _ in range(app.state.settings.sample_verification_max_concurrency):
        semaphore.release()
    first = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json=first_payload,
    )
    assert first.status_code == 201, first.text
    assert first.json()["metrics"]["authoritative_source"] == "manager_computed"
    assert first.json()["metrics"]["manager_computed"]["silence_ratio"] == 1.0
    replay = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json=first_payload,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]
    await _advance_to_uploading(client, context)
    partial = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/status",
        headers=context.worker_headers,
        json={"lease_id": context.claim["lease_id"], "status": "completed"},
    )
    assert partial.status_code == 409
    assert partial.json()["detail"] == "required samples are not registered"

    second_payload = _registration(
        context,
        descriptor=descriptors[1],
        artifact=first_artifact,
        model_sha256=model["sha256"],
        index_sha256=index["sha256"],
        frames=4_000,
    )
    second = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json=second_payload,
    )
    assert second.status_code == 201, second.text
    assert second.json()["artifact_id"] == first.json()["artifact_id"]
    assert second.json()["id"] != first.json()["id"]
    original_stream = app.state.storage.stream_object

    async def fail_sample_stream(
        object_key: str,
        *,
        chunk_size: int,
        max_bytes: int,
    ) -> Any:
        if "/sample/" in object_key:
            raise StorageError("injected transient read failure")
        async for chunk in original_stream(
            object_key,
            chunk_size=chunk_size,
            max_bytes=max_bytes,
        ):
            yield chunk

    monkeypatch.setattr(app.state.storage, "stream_object", fail_sample_stream)
    unavailable_completion = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/status",
        headers=context.worker_headers,
        json={"lease_id": context.claim["lease_id"], "status": "completed"},
    )
    assert unavailable_completion.status_code == 503
    assert unavailable_completion.headers["retry-after"] == "5"
    monkeypatch.setattr(app.state.storage, "stream_object", original_stream)
    async with app.state.database.session_factory() as session:
        first_upload = await session.get(ArtifactUploadSession, first_upload_id)
        assert first_upload is not None
        first_canonical_path = app.state.storage._path(first_upload.canonical_object_key)
        first_canonical_bytes = first_canonical_path.read_bytes()
        first_canonical_path.chmod(0o600)
        first_canonical_path.write_bytes(
            first_canonical_bytes[:-1] + bytes([first_canonical_bytes[-1] ^ 1])
        )
    mutated_completion = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/status",
        headers=context.worker_headers,
        json={"lease_id": context.claim["lease_id"], "status": "completed"},
    )
    assert mutated_completion.status_code == 409
    first_canonical_path.write_bytes(first_canonical_bytes)
    first_canonical_path.chmod(0o440)
    async with app.state.database.session_factory() as session:
        second_row = await session.get(Sample, second.json()["id"])
        assert second_row is not None
        canonical_duration = second_row.output_duration_seconds
        second_row.output_duration_seconds = canonical_duration + 0.01
        await session.commit()
    tampered_completion = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/status",
        headers=context.worker_headers,
        json={"lease_id": context.claim["lease_id"], "status": "completed"},
    )
    assert tampered_completion.status_code == 409
    async with app.state.database.session_factory() as session:
        second_row = await session.get(Sample, second.json()["id"])
        assert second_row is not None
        second_row.output_duration_seconds = canonical_duration
        await session.commit()
    original_total_output_bytes = sample_services.SAMPLE_MAX_TOTAL_OUTPUT_BYTES
    monkeypatch.setattr(
        sample_services,
        "SAMPLE_MAX_TOTAL_OUTPUT_BYTES",
        len(first_bytes) * 2 - 1,
    )
    total_limited_completion = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/status",
        headers=context.worker_headers,
        json={"lease_id": context.claim["lease_id"], "status": "completed"},
    )
    assert total_limited_completion.status_code == 409
    monkeypatch.setattr(
        sample_services,
        "SAMPLE_MAX_TOTAL_OUTPUT_BYTES",
        original_total_output_bytes,
    )
    completed = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/status",
        headers=context.worker_headers,
        json={"lease_id": context.claim["lease_id"], "status": "completed"},
    )
    assert completed.status_code == 200, completed.text

    owner_list = await client.get(
        f"/api/v1/jobs/{context.job_id}/samples",
        headers=context.owner_headers,
    )
    assert owner_list.status_code == 200, owner_list.text
    assert owner_list.json()["total"] == 2
    assert "storage_uri" not in owner_list.text
    assert owner_list.headers["cache-control"] == "private, no-store"
    assert (
        await client.get(
            f"/api/v1/jobs/{context.job_id}/samples",
            headers=context.other_headers,
        )
    ).status_code == 404
    assert (
        await client.get(
            f"/api/v1/jobs/{context.job_id}/samples",
            headers=admin_headers,
        )
    ).json()["total"] == 2
    first_canonical_path.chmod(0o600)
    first_canonical_path.write_bytes(
        first_canonical_bytes[:-1] + bytes([first_canonical_bytes[-1] ^ 1])
    )
    mutated_download = await client.get(
        f"/api/v1/samples/{first.json()['id']}/download",
        headers=context.owner_headers,
    )
    assert mutated_download.status_code == 409
    first_canonical_path.write_bytes(first_canonical_bytes)
    first_canonical_path.chmod(0o440)
    for _ in range(app.state.settings.sample_verification_max_concurrency):
        await semaphore.acquire()
    saturated_download = await client.get(
        f"/api/v1/samples/{first.json()['id']}/download",
        headers=context.owner_headers,
    )
    assert saturated_download.status_code == 429
    assert saturated_download.headers["retry-after"] == "1"
    for _ in range(app.state.settings.sample_verification_max_concurrency):
        semaphore.release()
    downloaded = await client.get(
        f"/api/v1/samples/{first.json()['id']}/download",
        headers=context.owner_headers,
    )
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == first_bytes
    assert downloaded.headers["content-type"].startswith("audio/wav")
    assert downloaded.headers["cache-control"] == "private, no-store"
    stable_etag = f'"{first.json()["output_sha256"]}"'
    assert downloaded.headers["etag"] == stable_etag
    ranged = await client.get(
        f"/api/v1/samples/{first.json()['id']}/download",
        headers={
            **context.owner_headers,
            "Range": "bytes=0-3",
            "If-Range": stable_etag,
        },
    )
    assert ranged.status_code == 206, ranged.text
    assert ranged.content == first_bytes[:4]
    assert ranged.headers["accept-ranges"] == "bytes"
    assert ranged.headers["content-range"] == f"bytes 0-3/{len(first_bytes)}"
    assert ranged.headers["content-length"] == "4"
    assert ranged.headers["cache-control"] == "private, no-store"
    stale_validator = await client.get(
        f"/api/v1/samples/{first.json()['id']}/download",
        headers={
            **context.owner_headers,
            "Range": "bytes=0-3",
            "If-Range": '"stale-sample-etag"',
        },
    )
    assert stale_validator.status_code == 200
    assert stale_validator.content == first_bytes
    assert stale_validator.headers["etag"] == stable_etag
    unsatisfied_range = await client.get(
        f"/api/v1/samples/{first.json()['id']}/download",
        headers={
            **context.owner_headers,
            "Range": f"bytes={len(first_bytes) + 1}-{len(first_bytes) + 4}",
        },
    )
    assert unsatisfied_range.status_code == 416
    assert unsatisfied_range.headers["content-range"] == f"bytes */{len(first_bytes)}"
    assert unsatisfied_range.headers["cache-control"] == "private, no-store"
    assert (
        await client.get(
            f"/api/v1/samples/{first.json()['id']}/download",
            headers=context.other_headers,
        )
    ).status_code == 404
    admin_download = await client.get(
        f"/api/v1/samples/{first.json()['id']}/download",
        headers=admin_headers,
    )
    assert admin_download.status_code == 200
    assert admin_download.content == first_bytes


async def test_sample_registration_enforces_attempt_total_output_limits(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    context = await _sample_job(
        app,
        client,
        suffix="attempt-total-limits",
        item_count=2,
    )
    model, _ = await _artifact(
        client,
        context,
        artifact_type="final_small_model",
        filename="total-limit-model.pth",
        content_type="application/x-pytorch",
        content=b"total-limit-model" * 32,
        key="sample-model-total-limit-0001",
    )
    index, _ = await _artifact(
        client,
        context,
        artifact_type="final_index",
        filename="total-limit.index",
        content_type="application/octet-stream",
        content=b"total-limit-index" * 32,
        key="sample-index-total-limit-0001",
    )
    output_bytes = _wav(sample_rate=40_000, frames=4_000)
    output, _ = await _artifact(
        client,
        context,
        artifact_type="sample",
        filename="total-limit.wav",
        content_type="audio/wav",
        content=output_bytes,
        key="sample-output-total-limit-0001",
    )
    descriptors = context.claim["test_set_transfer"]["items"]
    first_payload = _registration(
        context,
        descriptor=descriptors[0],
        artifact=output,
        model_sha256=model["sha256"],
        index_sha256=index["sha256"],
        frames=4_000,
    )
    first = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json=first_payload,
    )
    assert first.status_code == 201, first.text
    second_payload = _registration(
        context,
        descriptor=descriptors[1],
        artifact=output,
        model_sha256=model["sha256"],
        index_sha256=index["sha256"],
        frames=4_000,
    )

    async with app.state.database.session_factory() as session:
        first_row = await session.get(Sample, first.json()["id"])
        assert first_row is not None
        first_row.output_size_bytes = SAMPLE_MAX_TOTAL_OUTPUT_BYTES - output["size_bytes"] + 1
        await session.commit()
    byte_limited = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json=second_payload,
    )
    assert byte_limited.status_code == 413
    assert byte_limited.json()["detail"] == "sample attempt exceeds total output limits"

    async with app.state.database.session_factory() as session:
        first_row = await session.get(Sample, first.json()["id"])
        assert first_row is not None
        first_row.output_size_bytes = output["size_bytes"]
        first_row.output_duration_seconds = (
            SAMPLE_MAX_TOTAL_OUTPUT_DURATION_SECONDS
            - second_payload["output_duration_seconds"]
            + 0.001
        )
        await session.commit()
    duration_limited = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json=second_payload,
    )
    assert duration_limited.status_code == 413
    assert duration_limited.json()["detail"] == ("sample attempt exceeds total output limits")
    async with app.state.database.session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Sample)
                .where(Sample.attempt_id == context.claim["attempt_id"])
            )
            == 1
        )


async def test_sample_registration_uses_explicit_resample_rate_without_index(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    context = await _sample_job(
        app,
        client,
        suffix="resampled-no-index",
        resample_sr=16_000,
        index_rate=0,
        build_index=False,
    )
    model, _ = await _artifact(
        client,
        context,
        artifact_type="final_small_model",
        filename="final_small_model.pth",
        content_type="application/x-pytorch",
        content=b"resampled-model" * 32,
        key="sample-model-resampled-0001",
    )
    output_bytes = _wav(sample_rate=16_000, frames=1_600)
    output, _ = await _artifact(
        client,
        context,
        artifact_type="sample",
        filename="resampled-output.wav",
        content_type="audio/wav",
        content=output_bytes,
        key="sample-output-resampled-0001",
    )
    payload = _registration(
        context,
        descriptor=context.claim["test_set_transfer"]["items"][0],
        artifact=output,
        model_sha256=model["sha256"],
        index_sha256=None,
        frames=1_600,
        sample_rate=16_000,
    )
    registered = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json=payload,
    )
    assert registered.status_code == 201, registered.text
    assert registered.json()["output_sample_rate_hz"] == 16_000
    assert registered.json()["index_sha256"] is None


async def test_sample_registration_rejects_mismatched_provenance_pcm_and_namespace(
    app: FastAPI,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await _sample_job(app, client, suffix="reject")
    model, _ = await _artifact(
        client,
        context,
        artifact_type="final_small_model",
        filename="final_small_model.pth",
        content_type="application/x-pytorch",
        content=b"reject-model" * 32,
        key="sample-model-reject-0001",
    )
    index, _ = await _artifact(
        client,
        context,
        artifact_type="final_index",
        filename="final.index",
        content_type="application/octet-stream",
        content=b"reject-index" * 32,
        key="sample-index-reject-0001",
    )
    output_bytes = _wav(sample_rate=40_000, frames=4_000)
    output, output_upload_id = await _artifact(
        client,
        context,
        artifact_type="sample",
        filename="reject-output.wav",
        content_type="audio/wav",
        content=output_bytes,
        key="sample-output-reject-0001",
    )
    descriptor = context.claim["test_set_transfer"]["items"][0]
    payload = _registration(
        context,
        descriptor=descriptor,
        artifact=output,
        model_sha256=model["sha256"],
        index_sha256=index["sha256"],
        frames=4_000,
    )
    mismatches = (
        {"lease_id": "10000000-0000-4000-8000-000000000001"},
        {"attempt_id": "10000000-0000-4000-8000-000000000002"},
        {"test_set_id": "10000000-0000-4000-8000-000000000004"},
        {"test_set_item_id": "10000000-0000-4000-8000-000000000003"},
        {"input_sha256": "1" * 64},
        {"sample_plan_sha256": "2" * 64},
        {"inference_config_sha256": "3" * 64},
        {"inference_f0_method": "pm"},
        {"model_sha256": "4" * 64},
        {"index_sha256": "5" * 64},
        {"artifact_id": model["id"]},
        {"output_sha256": "6" * 64},
        {"output_size_bytes": output["size_bytes"] + 1},
        {"runtime_image_digest": "sha256:" + "8" * 64},
        {"runtime_asset_manifest_sha256": "7" * 64},
        {"rvc_commit_hash": "6" * 40},
    )
    for mutation in mismatches:
        rejected = await client.post(
            f"/api/v1/workers/jobs/{context.job_id}/samples",
            headers=context.worker_headers,
            json={**payload, **mutation},
        )
        assert rejected.status_code == 409, (mutation, rejected.text)
    missing_required_index = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json={**payload, "index_sha256": None},
    )
    assert missing_required_index.status_code == 409
    assert missing_required_index.json()["detail"] == "sample retrieval index is required"

    wrong_duration = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json={**payload, "output_duration_seconds": 0.2},
    )
    assert wrong_duration.status_code == 422
    wrong_channels = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json={**payload, "output_channels": 2},
    )
    assert wrong_channels.status_code == 422
    wrong_metrics = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json={**payload, "metrics": {**payload["metrics"], "rms": 0.5}},
    )
    assert wrong_metrics.status_code == 422

    wrong_rate_bytes = _wav(sample_rate=16_000, frames=1_600)
    wrong_rate, _ = await _artifact(
        client,
        context,
        artifact_type="sample",
        filename="wrong-rate.wav",
        content_type="audio/wav",
        content=wrong_rate_bytes,
        key="sample-output-reject-rate-0001",
    )
    rejected_rate = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json={
            **payload,
            "artifact_id": wrong_rate["id"],
            "output_sha256": wrong_rate["sha256"],
            "output_size_bytes": wrong_rate["size_bytes"],
            "output_sample_rate_hz": 16_000,
        },
    )
    assert rejected_rate.status_code == 422

    invalid_pcm, _ = await _artifact(
        client,
        context,
        artifact_type="sample",
        filename="invalid-pcm.wav",
        content_type="audio/wav",
        content=b"not-a-wave-file",
        key="sample-output-reject-pcm-0001",
    )
    rejected_pcm = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json={
            **payload,
            "artifact_id": invalid_pcm["id"],
            "output_sha256": invalid_pcm["sha256"],
            "output_size_bytes": invalid_pcm["size_bytes"],
        },
    )
    assert rejected_pcm.status_code == 422

    flac_mime, _ = await _artifact(
        client,
        context,
        artifact_type="sample",
        filename="wrong-mime.flac",
        content_type="audio/flac",
        content=b"fake-flac-data",
        key="sample-output-reject-mime-0001",
    )
    rejected_mime = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json={
            **payload,
            "artifact_id": flac_mime["id"],
            "output_sha256": flac_mime["sha256"],
            "output_size_bytes": flac_mime["size_bytes"],
        },
    )
    assert rejected_mime.status_code == 409

    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, output_upload_id)
        assert upload is not None
        canonical_path = app.state.storage._path(upload.canonical_object_key)
        canonical_bytes = canonical_path.read_bytes()
        canonical_path.chmod(0o600)
        canonical_path.write_bytes(canonical_bytes[:-1] + bytes([canonical_bytes[-1] ^ 1]))
    canonical_tampered = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json=payload,
    )
    assert canonical_tampered.status_code == 409
    canonical_path.write_bytes(canonical_bytes)
    canonical_path.chmod(0o440)

    async with app.state.database.session_factory() as session:
        upload = await session.get(ArtifactUploadSession, output_upload_id)
        assert upload is not None
        original_namespace = upload.storage_namespace_sha256
        upload.storage_namespace_sha256 = "0" * 64
        await session.commit()
    unavailable = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json=payload,
    )
    assert unavailable.status_code == 503
    async with app.state.database.session_factory() as session:
        upload = await session.scalar(
            select(ArtifactUploadSession).where(ArtifactUploadSession.id == output_upload_id)
        )
        assert upload is not None
        upload.storage_namespace_sha256 = original_namespace
        await session.commit()

    original_fence = artifact_routes._lock_current_sample_claim

    async def expire_after_canonical_read(session: Any, **kwargs: Any) -> JobLease:
        await session.execute(
            update(JobLease)
            .where(JobLease.id == context.claim["lease_id"])
            .values(active=False, released_at=utc_now())
        )
        await session.commit()
        return await original_fence(session, **kwargs)

    monkeypatch.setattr(
        artifact_routes,
        "_lock_current_sample_claim",
        expire_after_canonical_read,
    )
    fenced = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json=payload,
    )
    assert fenced.status_code == 409
    async with app.state.database.session_factory() as session:
        sample_count = (
            await session.scalar(
                select(func.count()).select_from(Sample).where(Sample.job_id == context.job_id)
            )
            or 0
        )
        assert sample_count == 0


@pytest.mark.asyncio
async def test_sample_registration_job_fence_takes_a_real_sqlite_write_lock(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    context = await _sample_job(app, client, suffix="sqlite-job-write-fence")

    async with app.state.database.session_factory() as fenced_session:
        job = await fenced_session.get(Job, context.job_id)
        attempt = await fenced_session.get(JobAttempt, context.claim["attempt_id"])
        assert job is not None
        assert attempt is not None
        assert job.test_set_id is not None
        assert job.sample_plan_sha256 is not None
        assert attempt.runtime_image_digest is not None
        assert attempt.runtime_asset_manifest_sha256 is not None
        config = sample_services.validated_job_config(job, attempt=attempt)
        initial_row_version = job.row_version

        fenced_job, fenced_attempt = await sample_services.acquire_sample_registration_job_fence(
            fenced_session,
            job_id=job.id,
            attempt_id=attempt.id,
            worker_id=attempt.worker_id,
            test_set_id=job.test_set_id,
            sample_plan_sha256=job.sample_plan_sha256,
            expected_config=config,
            runtime_image_digest=attempt.runtime_image_digest,
            runtime_asset_manifest_sha256=attempt.runtime_asset_manifest_sha256,
        )
        assert fenced_job.row_version == initial_row_version
        assert fenced_attempt.id == attempt.id

        # SELECT ... FOR UPDATE is ignored by SQLite.  A second writer can only
        # be rejected here because the service performed the no-op Job UPDATE.
        async with app.state.database.session_factory() as competing_session:
            await competing_session.execute(text("PRAGMA busy_timeout = 0"))
            with pytest.raises(OperationalError, match="database is locked"):
                await competing_session.execute(
                    update(Job)
                    .where(Job.id == context.job_id)
                    .values(current_epoch=Job.current_epoch)
                )
            await competing_session.rollback()
        await fenced_session.rollback()


@pytest.mark.asyncio
async def test_sample_registration_job_fence_revalidates_raw_config_after_update(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    context = await _sample_job(app, client, suffix="job-config-revalidation")

    async with app.state.database.session_factory() as session:
        job = await session.get(Job, context.job_id)
        attempt = await session.get(JobAttempt, context.claim["attempt_id"])
        assert job is not None
        assert attempt is not None
        assert job.test_set_id is not None
        assert job.sample_plan_sha256 is not None
        assert attempt.runtime_image_digest is not None
        assert attempt.runtime_asset_manifest_sha256 is not None
        expected_config = sample_services.validated_job_config(job, attempt=attempt)
        test_set_id = job.test_set_id
        sample_plan_sha256 = job.sample_plan_sha256
        worker_id = attempt.worker_id
        runtime_image_digest = attempt.runtime_image_digest
        runtime_asset_manifest_sha256 = attempt.runtime_asset_manifest_sha256
        tampered_config = dict(job.config_json)
        tampered_config["job_name"] = "same-hash-raw-json-tamper"
        await session.execute(
            update(Job).where(Job.id == context.job_id).values(config_json=tampered_config)
        )
        await session.commit()

    async with app.state.database.session_factory() as session:
        with pytest.raises(
            sample_services.SampleRegistrationFenceConflict,
            match="JobConfig snapshot changed",
        ):
            await sample_services.acquire_sample_registration_job_fence(
                session,
                job_id=context.job_id,
                attempt_id=context.claim["attempt_id"],
                worker_id=worker_id,
                test_set_id=test_set_id,
                sample_plan_sha256=sample_plan_sha256,
                expected_config=expected_config,
                runtime_image_digest=runtime_image_digest,
                runtime_asset_manifest_sha256=runtime_asset_manifest_sha256,
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_completion_reacquires_claim_fence_after_sample_readiness(
    app: FastAPI,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await _sample_job(app, client, suffix="completion-fence")
    model, _ = await _artifact(
        client,
        context,
        artifact_type="final_small_model",
        filename="fenced-model.pth",
        content_type="application/x-pytorch",
        content=b"fenced-model" * 32,
        key="sample-model-completion-fence",
    )
    index, _ = await _artifact(
        client,
        context,
        artifact_type="final_index",
        filename="fenced.index",
        content_type="application/octet-stream",
        content=b"fenced-index" * 32,
        key="sample-index-completion-fence",
    )
    output_bytes = _wav(sample_rate=40_000, frames=4_000)
    output, _ = await _artifact(
        client,
        context,
        artifact_type="sample",
        filename="fenced.wav",
        content_type="audio/wav",
        content=output_bytes,
        key="sample-output-completion-fence",
    )
    payload = _registration(
        context,
        descriptor=context.claim["test_set_transfer"]["items"][0],
        artifact=output,
        model_sha256=model["sha256"],
        index_sha256=index["sha256"],
        frames=4_000,
    )
    registered = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/samples",
        headers=context.worker_headers,
        json=payload,
    )
    assert registered.status_code == 201, registered.text
    await _advance_to_uploading(client, context)

    original_ready = worker_routes.sample_completion_ready

    async def expire_after_readiness(session: Any, *args: Any, **kwargs: Any) -> bool:
        ready = await original_ready(session, *args, **kwargs)
        assert ready is True
        await session.execute(
            update(JobLease)
            .where(JobLease.id == context.claim["lease_id"])
            .values(active=False, released_at=utc_now())
        )
        await session.commit()
        return True

    monkeypatch.setattr(worker_routes, "sample_completion_ready", expire_after_readiness)
    completed = await client.post(
        f"/api/v1/workers/jobs/{context.job_id}/status",
        headers=context.worker_headers,
        json={"lease_id": context.claim["lease_id"], "status": "completed"},
    )
    assert completed.status_code == 409
    assert completed.json()["detail"] == "job claim changed before status commit"
    async with app.state.database.session_factory() as session:
        job = await session.get(Job, context.job_id)
        attempt = await session.get(JobAttempt, context.claim["attempt_id"])
        assert job is not None and job.status == "uploading_artifacts"
        assert attempt is not None and attempt.status == "uploading_artifacts"
        assert attempt.finished_at is None
