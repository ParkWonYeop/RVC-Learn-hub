from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import delete, select
from starlette.concurrency import run_in_threadpool

from rvc_manager_api.models import (
    Artifact,
    ArtifactUploadSession,
    Job,
    JobAttempt,
    JobLease,
    Metric,
    Sample,
    User,
    new_id,
)
from rvc_manager_api.models import (
    TestSet as LedgerTestSet,
)
from rvc_manager_api.models import (
    TestSetItem as LedgerTestSetItem,
)
from rvc_manager_api.security import hash_password
from rvc_manager_api.services.artifacts import canonical_object_key
from rvc_orchestrator_contracts import (
    RVC_REVIEWED_COMMIT,
    JobConfig,
    job_config_sha256,
    utc_now,
)

USER_PASSWORD = "experiment-comparison-password-1234"
_RUNTIME_DIGEST = f"sha256:{'1' * 64}"
_RUNTIME_ASSET_SHA256 = "2" * 64
_NATIVE_MANIFEST_SHA256 = "3" * 64
_NATIVE_REQUEST_SHA256 = "4" * 64


async def _seed_user(app: FastAPI, email: str) -> User:
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


async def _create_experiment_with_jobs(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    prefix: str,
    job_count: int = 2,
) -> tuple[str, list[str]]:
    dataset = await client.post(
        "/api/v1/datasets",
        headers=headers,
        json={
            "name": f"{prefix}-dataset",
            "storage_uri": f"local:///datasets/{prefix}.zip",
            "flat_storage_uri": f"local:///datasets/{prefix}-flat",
        },
    )
    assert dataset.status_code == 201, dataset.text
    experiment = await client.post(
        "/api/v1/experiments",
        headers=headers,
        json={
            "name": f"{prefix}-experiment",
            "dataset_id": dataset.json()["id"],
        },
    )
    assert experiment.status_code == 201, experiment.text
    job_ids: list[str] = []
    for index in range(job_count):
        job = await client.post(
            "/api/v1/jobs",
            headers=headers,
            json={
                "job_name": f"{prefix}-job-{index}",
                "experiment_id": experiment.json()["id"],
                "dataset_id": dataset.json()["id"],
                "model": {"version": "v2", "sample_rate": "40k"},
                "training": {"epochs": 80 + index},
            },
        )
        assert job.status_code == 201, job.text
        job_ids.append(str(job.json()["id"]))
    return str(experiment.json()["id"]), job_ids


async def _register_worker(client: AsyncClient, name: str) -> str:
    response = await client.post(
        "/api/v1/workers/register",
        headers={"X-Worker-Bootstrap-Token": "test-bootstrap-token"},
        json={
            "name": name,
            "capabilities": {
                "engine_mode": "rvc_webui",
                "worker_version": "0.2.0",
                "rvc_commit_hash": RVC_REVIEWED_COMMIT,
                "supported_rvc_versions": ["v2"],
                "supported_training_f0_methods": ["rmvpe"],
                "gpus": [],
                "disk_free_bytes": 500_000_000_000,
                "rvc_assets_ready": True,
            },
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["worker_id"])


def _manager_verification(upload_id: str) -> dict[str, object]:
    return {
        "algorithm": "sha256",
        "bounded_stream": True,
        "upload_session_id": upload_id,
        "storage_backend": "local",
    }


def _native_provenance(role: str) -> dict[str, str]:
    return {
        "rvc_commit_hash": RVC_REVIEWED_COMMIT,
        "runtime_image_digest": _RUNTIME_DIGEST,
        "runtime_asset_manifest_sha256": _RUNTIME_ASSET_SHA256,
        "native_inference_manifest_sha256": _NATIVE_MANIFEST_SHA256,
        "native_inference_request_sha256": _NATIVE_REQUEST_SHA256,
        "native_sample_role": role,
    }


def _verified_artifact_rows(
    app: FastAPI,
    *,
    job_id: str,
    attempt_id: str,
    lease_id: str,
    worker_id: str,
    artifact_type: str,
    filename: str,
    size_bytes: int,
    sha256: str,
    mime_type: str,
    role: str | None = None,
) -> tuple[Artifact, ArtifactUploadSession]:
    upload_id = new_id()
    object_key = canonical_object_key(job_id, attempt_id, artifact_type, upload_id)
    metadata: dict[str, object] = {
        "manager_verification": _manager_verification(upload_id),
    }
    if role is not None:
        metadata.update(_native_provenance(role))
    artifact = Artifact(
        job_id=job_id,
        attempt_id=attempt_id,
        artifact_type=artifact_type,
        filename=filename,
        storage_uri=app.state.storage.storage_uri(object_key),
        size_bytes=size_bytes,
        sha256=sha256,
        mime_type=mime_type,
        metadata_json=metadata,
    )
    upload = ArtifactUploadSession(
        id=upload_id,
        job_id=job_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        worker_id=worker_id,
        artifact_id=artifact.id,
        artifact_type=artifact_type,
        filename=filename,
        content_type=mime_type,
        expected_size_bytes=size_bytes,
        expected_sha256=sha256,
        metadata_json={},
        idempotency_key=f"comparison-{upload_id}",
        generation=1,
        request_fingerprint="f" * 64,
        temporary_object_key=f"artifacts/staging/{upload_id}",
        canonical_object_key=object_key,
        storage_backend="local",
        storage_namespace_sha256=app.state.storage.namespace_fingerprint,
        status="completed",
        expires_at=utc_now() + timedelta(hours=1),
        uploaded_at=utc_now(),
        finalized_at=utc_now(),
    )
    return artifact, upload


async def _seed_completed_comparison_ledgers(
    app: FastAPI,
    client: AsyncClient,
    job_ids: list[str],
) -> dict[str, Any]:
    worker_id = await _register_worker(client, f"comparison-worker-{new_id()}")
    now = utc_now().replace(microsecond=0) - timedelta(minutes=10)
    async with app.state.database.session_factory() as session:
        jobs = {
            job.id: job
            for job in (await session.scalars(select(Job).where(Job.id.in_(tuple(job_ids))))).all()
        }
        test_set = LedgerTestSet(
            family_id=new_id(),
            name="comparison-test-set",
            revision=1,
            status="ready",
            manifest_storage_uri="local:///test-sets/comparison/manifest.json",
            manifest_sha256="9" * 64,
            item_count=1,
            created_by=None,
            finalized_at=now,
        )
        session.add(test_set)
        await session.flush()
        test_item = LedgerTestSetItem(
            test_set_id=test_set.id,
            item_key="same-voice",
            display_name="Same voice",
            sort_order=0,
            storage_uri="local:///test-sets/comparison/same-voice.wav",
            original_filename="same-voice.wav",
            size_bytes=32044,
            sha256="8" * 64,
            mime_type="audio/wav",
            sample_rate_hz=16_000,
            channels=1,
            duration_seconds=1.0,
            license_reference="license-record:test",
            provenance_reference="provenance-record:test",
        )
        session.add(test_item)
        await session.flush()

        attempts: dict[str, JobAttempt] = {}
        leases: dict[str, JobLease] = {}
        for position, job_id in enumerate(job_ids):
            job = jobs[job_id]
            attempt_number = 2 if position == 0 else 1
            if position == 0:
                old_attempt = JobAttempt(
                    job_id=job.id,
                    worker_id=worker_id,
                    attempt_number=1,
                    engine_mode="rvc_webui",
                    status="failed",
                    started_at=now - timedelta(minutes=2),
                    finished_at=now - timedelta(minutes=1),
                )
                session.add(old_attempt)
                await session.flush()
                session.add(
                    Metric(
                        job_id=job.id,
                        attempt_id=old_attempt.id,
                        sequence=999,
                        epoch=79,
                        step=999,
                        key="loss_g_total",
                        value=999.0,
                        occurred_at=now - timedelta(minutes=1),
                    )
                )
            attempt = JobAttempt(
                job_id=job.id,
                worker_id=worker_id,
                attempt_number=attempt_number,
                engine_mode="rvc_webui",
                runtime_image_digest=_RUNTIME_DIGEST if position == 0 else None,
                runtime_asset_manifest_sha256=(_RUNTIME_ASSET_SHA256 if position == 0 else None),
                status="completed",
                started_at=now + timedelta(minutes=position),
                finished_at=now + timedelta(minutes=position + 4),
            )
            session.add(attempt)
            await session.flush()
            lease = JobLease(
                job_id=job.id,
                attempt_id=attempt.id,
                worker_id=worker_id,
                expires_at=now + timedelta(minutes=5),
                last_renewed_at=now + timedelta(minutes=4),
                released_at=now + timedelta(minutes=4),
                active=False,
            )
            session.add(lease)
            await session.flush()
            job.worker_id = worker_id
            job.status = "completed"
            job.current_attempt_id = attempt.id
            job.attempt_count = attempt_number
            job.current_epoch = job.total_epoch
            job.started_at = attempt.started_at
            job.completed_at = attempt.finished_at
            if position == 0:
                config_json = JobConfig.model_validate(job.config_json).model_dump(mode="json")
                config_json["auto_inference_samples"] = {
                    "enabled": True,
                    "test_set_id": test_set.id,
                    "inference_f0_method": "rmvpe",
                    "transpose": 0,
                    "index_rate": 0.75,
                    "filter_radius": 3,
                    "resample_sr": 0,
                    "rms_mix_rate": 0.25,
                    "protect": 0.33,
                }
                job.config_json = config_json
                job.test_set_id = test_set.id
                job.sample_plan_json = {"version": "test"}
                job.sample_plan_sha256 = "7" * 64
            normalized_config = JobConfig.model_validate(job.config_json).model_dump(mode="json")
            job.config_json = normalized_config
            job.config_sha256 = job_config_sha256(normalized_config)
            attempt.job_config_sha256 = job.config_sha256
            attempts[job_id] = attempt
            leases[job_id] = lease

        first_job_id, second_job_id = job_ids
        first_attempt = attempts[first_job_id]
        for sequence in range(205):
            session.add(
                Metric(
                    job_id=first_job_id,
                    attempt_id=first_attempt.id,
                    sequence=sequence,
                    epoch=sequence // 3,
                    step=sequence,
                    key="loss_g_total",
                    value=100.0 / (sequence + 1),
                    occurred_at=now + timedelta(seconds=sequence),
                )
            )
        session.add(
            Metric(
                job_id=first_job_id,
                attempt_id=first_attempt.id,
                sequence=205,
                epoch=None,
                step=None,
                key="private.unsupported.metric",
                value=123.0,
                occurred_at=now + timedelta(seconds=205),
            )
        )
        session.add(
            Metric(
                job_id=second_job_id,
                attempt_id=attempts[second_job_id].id,
                sequence=0,
                epoch=None,
                step=None,
                key="system.gpu.0.utilization_percent",
                value=42.5,
                occurred_at=now,
            )
        )

        model_sha = "a" * 64
        index_sha = "b" * 64
        output_sha = "c" * 64
        model_rows = _verified_artifact_rows(
            app,
            job_id=first_job_id,
            attempt_id=first_attempt.id,
            lease_id=leases[first_job_id].id,
            worker_id=worker_id,
            artifact_type="final_small_model",
            filename="comparison-model.pth",
            size_bytes=1024,
            sha256=model_sha,
            mime_type="application/octet-stream",
            role="sample_model",
        )
        index_rows = _verified_artifact_rows(
            app,
            job_id=first_job_id,
            attempt_id=first_attempt.id,
            lease_id=leases[first_job_id].id,
            worker_id=worker_id,
            artifact_type="final_index",
            filename="final.index",
            size_bytes=2048,
            sha256=index_sha,
            mime_type="application/octet-stream",
            role="sample_index",
        )
        output_rows = _verified_artifact_rows(
            app,
            job_id=first_job_id,
            attempt_id=first_attempt.id,
            lease_id=leases[first_job_id].id,
            worker_id=worker_id,
            artifact_type="sample",
            filename="same-voice.wav",
            size_bytes=32044,
            sha256=output_sha,
            mime_type="audio/wav",
            role="sample_output",
        )
        second_model_rows = _verified_artifact_rows(
            app,
            job_id=second_job_id,
            attempt_id=attempts[second_job_id].id,
            lease_id=leases[second_job_id].id,
            worker_id=worker_id,
            artifact_type="final_small_model",
            filename="second-model.pth",
            size_bytes=4096,
            sha256="d" * 64,
            mime_type="application/octet-stream",
        )
        for artifact, upload in (
            model_rows,
            index_rows,
            output_rows,
            second_model_rows,
        ):
            session.add(artifact)
            await session.flush()
            upload.artifact_id = artifact.id
            session.add(upload)
        await session.flush()
        session.add(
            Sample(
                job_id=first_job_id,
                attempt_id=first_attempt.id,
                test_set_id=test_set.id,
                test_set_item_id=test_item.id,
                artifact_id=output_rows[0].id,
                input_sha256=test_item.sha256,
                model_sha256=model_sha,
                index_sha256=index_sha,
                inference_f0_method="rmvpe",
                inference_config_sha256="6" * 64,
                native_inference_manifest_sha256=_NATIVE_MANIFEST_SHA256,
                native_inference_request_sha256=_NATIVE_REQUEST_SHA256,
                output_size_bytes=32044,
                output_sha256=output_sha,
                output_sample_rate_hz=40_000,
                output_channels=1,
                output_duration_seconds=0.4,
                metrics_json={
                    "algorithm": "pcm-normalized-v2",
                    "authoritative_source": "manager_computed",
                    "clipping_threshold": 0.999,
                    "silence_threshold": 0.0001,
                    "worker_reported": {
                        "peak_amplitude": 0.5,
                        "rms": 0.1,
                        "clipping_ratio": 0.0,
                        "silence_ratio": 0.2,
                    },
                    "manager_computed": {
                        "peak_amplitude": 0.5,
                        "rms": 0.1,
                        "clipping_ratio": 0.0,
                        "silence_ratio": 0.2,
                    },
                    "worker_reported_duration_seconds": 0.4,
                    "manager_computed_sample_rate_hz": 40_000,
                    "manager_computed_channels": 1,
                    "manager_computed_duration_seconds": 0.4,
                },
                rvc_commit_hash=RVC_REVIEWED_COMMIT,
                runtime_image_digest=_RUNTIME_DIGEST,
                runtime_asset_manifest_sha256=_RUNTIME_ASSET_SHA256,
            )
        )
        await session.commit()
        return {
            "attempts": {job_id: attempt.id for job_id, attempt in attempts.items()},
            "sample_item_id": test_item.id,
        }


def _comparison_params(job_ids: list[str]) -> list[tuple[str, str]]:
    return [("job_ids", job_id) for job_id in job_ids]


async def test_comparison_projects_only_current_verified_public_ledgers(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await _seed_user(app, "comparison-owner@example.test")
    headers = await _login(client, owner.email)
    experiment_id, job_ids = await _create_experiment_with_jobs(
        client,
        headers,
        prefix="comparison-happy",
    )
    seeded = await _seed_completed_comparison_ledgers(app, client, job_ids)

    response = await client.get(
        f"/api/v1/experiments/{experiment_id}/comparison",
        headers=headers,
        params=_comparison_params(list(reversed(job_ids))),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [job["id"] for job in payload["jobs"]] == list(reversed(job_ids))
    assert payload["metric_point_limit_per_key"] == 200
    first = next(job for job in payload["jobs"] if job["id"] == job_ids[0])
    second = next(job for job in payload["jobs"] if job["id"] == job_ids[1])
    assert first["current_attempt"] == {
        "id": seeded["attempts"][job_ids[0]],
        "attempt_number": 2,
        "engine_mode": "rvc_webui",
        "status": "completed",
        "started_at": first["current_attempt"]["started_at"],
        "finished_at": first["current_attempt"]["finished_at"],
    }
    loss = next(series for series in first["metrics"] if series["key"] == "loss_g_total")
    assert loss["total_points"] == 205
    assert loss["truncated"] is True
    assert len(loss["points"]) == 200
    assert [point["sequence"] for point in loss["points"]] == list(range(5, 205))
    assert all(point["value"] != 999.0 for point in loss["points"])
    assert "private.unsupported.metric" not in {series["key"] for series in first["metrics"]}
    assert second["metrics"][0]["key"] == "system.gpu.0.utilization_percent"
    assert first["availability"]["final_model"]["filename"] == "comparison-model.pth"
    assert first["availability"]["final_index"]["filename"] == "final.index"
    assert first["availability"]["samples"] == [
        {
            "id": first["availability"]["samples"][0]["id"],
            "test_set_item_id": seeded["sample_item_id"],
            "output_size_bytes": 32044,
            "output_sha256": "c" * 64,
            "output_sample_rate_hz": 40_000,
            "output_channels": 1,
            "output_duration_seconds": 0.4,
            "created_at": first["availability"]["samples"][0]["created_at"],
        }
    ]
    assert second["availability"]["final_model"]["filename"] == "second-model.pth"
    assert second["availability"]["final_index"] is None
    assert second["availability"]["samples"] == []
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["Vary"] == "Authorization"
    serialized = response.text
    for forbidden in (
        "storage_uri",
        "canonical_object_key",
        "temporary_object_key",
        "metadata_json",
        "manager_verification",
        "local:///",
    ):
        assert forbidden not in serialized


async def test_comparison_rbac_and_cross_experiment_selection_are_concealed(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    owner = await _seed_user(app, "comparison-rbac-owner@example.test")
    other = await _seed_user(app, "comparison-rbac-other@example.test")
    owner_headers = await _login(client, owner.email)
    other_headers = await _login(client, other.email)
    experiment_id, job_ids = await _create_experiment_with_jobs(
        client,
        owner_headers,
        prefix="comparison-rbac-owner",
    )
    _, other_job_ids = await _create_experiment_with_jobs(
        client,
        other_headers,
        prefix="comparison-rbac-other",
    )
    url = f"/api/v1/experiments/{experiment_id}/comparison"

    assert (await client.get(url, params=_comparison_params(job_ids))).status_code == 401
    assert (
        await client.get(url, headers=other_headers, params=_comparison_params(job_ids))
    ).status_code == 404
    cross = await client.get(
        url,
        headers=owner_headers,
        params=_comparison_params([job_ids[0], other_job_ids[0]]),
    )
    assert cross.status_code == 404
    assert cross.json()["detail"] == "selected jobs not found in experiment"
    assert (
        await client.get(url, headers=admin_headers, params=_comparison_params(job_ids))
    ).status_code == 200


async def test_comparison_rejects_invalid_selection_stale_attempt_and_non_finite_metric(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    owner = await _seed_user(app, "comparison-invalid-owner@example.test")
    headers = await _login(client, owner.email)
    experiment_id, job_ids = await _create_experiment_with_jobs(
        client,
        headers,
        prefix="comparison-invalid",
    )
    url = f"/api/v1/experiments/{experiment_id}/comparison"

    assert (await client.get(url, headers=headers)).status_code == 422
    assert (
        await client.get(url, headers=headers, params=_comparison_params(job_ids[:1]))
    ).status_code == 422
    assert (
        await client.get(
            url,
            headers=headers,
            params=_comparison_params([job_ids[0], job_ids[0]]),
        )
    ).status_code == 422
    assert (
        await client.get(
            url,
            headers=headers,
            params=_comparison_params([job_ids[0], "not-a-job-id"]),
        )
    ).status_code == 422
    too_many = [job_ids[index % 2] for index in range(17)]
    assert (
        await client.get(url, headers=headers, params=_comparison_params(too_many))
    ).status_code == 422

    worker_id = await _register_worker(client, f"invalid-comparison-worker-{new_id()}")
    now = utc_now().replace(microsecond=0)
    async with app.state.database.session_factory() as session:
        jobs = {
            job.id: job
            for job in (await session.scalars(select(Job).where(Job.id.in_(tuple(job_ids))))).all()
        }
        attempts: dict[str, JobAttempt] = {}
        for job_id in job_ids:
            attempt = JobAttempt(
                job_id=job_id,
                worker_id=worker_id,
                attempt_number=1,
                engine_mode="rvc_webui",
                status="completed",
                started_at=now,
                finished_at=now + timedelta(minutes=1),
            )
            session.add(attempt)
            await session.flush()
            job = jobs[job_id]
            job.worker_id = worker_id
            job.status = "completed"
            job.current_attempt_id = attempt.id
            job.attempt_count = 1
            job.current_epoch = job.total_epoch
            job.started_at = attempt.started_at
            job.completed_at = attempt.finished_at
            attempt.job_config_sha256 = job.config_sha256
            attempts[job_id] = attempt
        await session.commit()

    async with app.state.database.session_factory() as session:
        first = await session.get(Job, job_ids[0])
        assert first is not None
        valid_attempt_id = first.current_attempt_id
        first.current_attempt_id = new_id()
        await session.commit()
    stale = await client.get(url, headers=headers, params=_comparison_params(job_ids))
    assert stale.status_code == 409
    assert stale.json()["detail"] == "selected job comparison ledger is inconsistent"

    async with app.state.database.session_factory() as session:
        first = await session.get(Job, job_ids[0])
        assert first is not None and valid_attempt_id is not None
        first.current_attempt_id = valid_attempt_id
        session.add(
            Metric(
                job_id=job_ids[0],
                attempt_id=valid_attempt_id,
                sequence=0,
                epoch=1,
                step=1,
                key="loss_g_total",
                value=float("inf"),
                occurred_at=now,
            )
        )
        await session.commit()
    non_finite = await client.get(url, headers=headers, params=_comparison_params(job_ids))
    assert non_finite.status_code == 409

    async with app.state.database.session_factory() as session:
        await session.execute(delete(Metric).where(Metric.job_id == job_ids[0]))
        await session.commit()
    recovered = await client.get(url, headers=headers, params=_comparison_params(job_ids))
    assert recovered.status_code == 200, recovered.text

    async with app.state.database.session_factory() as session:
        first = await session.get(Job, job_ids[0])
        assert first is not None
        document = dict(first.config_json)
        artifacts = dict(document["artifacts"])
        artifacts["collect_logs"] = not artifacts["collect_logs"]
        document["artifacts"] = artifacts
        first.config_json = document
        await session.commit()
    stale_config = await client.get(url, headers=headers, params=_comparison_params(job_ids))
    assert stale_config.status_code == 409
    assert stale_config.json()["detail"] == "selected job comparison ledger is inconsistent"


async def test_comparison_openapi_documents_explicit_bounded_job_ids(client: AsyncClient) -> None:
    openapi = (await client.get("/openapi.json")).json()
    operation = openapi["paths"]["/api/v1/experiments/{experiment_id}/comparison"]["get"]
    job_ids = next(
        parameter for parameter in operation["parameters"] if parameter["name"] == "job_ids"
    )
    assert job_ids["required"] is True
    assert job_ids["schema"]["minItems"] == 2
    assert job_ids["schema"]["maxItems"] == 16
    assert "Repeat job_ids" in job_ids["description"]
