from __future__ import annotations

import asyncio

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from rvc_manager_api.models import AuditEvent, Experiment, MlflowSyncEvent, User
from rvc_manager_api.security import hash_password

USER_PASSWORD = "experiment-owner-password-1234"


async def _seed_user(app: FastAPI, email: str) -> User:
    password_hash = await run_in_threadpool(hash_password, USER_PASSWORD)
    async with app.state.database.session_factory() as session:
        user = User(
            email=email,
            password_hash=password_hash,
            role="user",
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


async def _dataset(
    client: AsyncClient,
    headers: dict[str, str],
    name: str,
) -> str:
    response = await client.post(
        "/api/v1/datasets",
        headers=headers,
        json={
            "name": name,
            "storage_uri": f"s3://datasets/{name}.zip",
            "flat_storage_uri": f"s3://datasets/{name}-flat/",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _experiment(
    client: AsyncClient,
    headers: dict[str, str],
    dataset_id: str,
    name: str,
    description: str | None = None,
) -> dict[str, object]:
    response = await client.post(
        "/api/v1/experiments",
        headers=headers,
        json={"name": name, "dataset_id": dataset_id, "description": description},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_experiment_owner_admin_update_is_description_only_and_audited(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    owner = await _seed_user(app, "experiment-owner@example.test")
    await _seed_user(app, "experiment-other@example.test")
    owner_headers = await _login(client, owner.email)
    other_headers = await _login(client, "experiment-other@example.test")
    dataset_id = await _dataset(client, owner_headers, "experiment-owner-dataset")
    created = await _experiment(
        client,
        owner_headers,
        dataset_id,
        "  speaker comparison  ",
        "initial",
    )
    experiment_id = str(created["id"])
    assert created["name"] == "speaker comparison"
    assert created["dataset_id"] == dataset_id
    assert created["row_version"] == 1
    assert "name_conflict_key" not in created

    duplicate = await client.post(
        "/api/v1/experiments",
        headers=owner_headers,
        json={
            "name": " speaker comparison ",
            "dataset_id": dataset_id,
            "description": None,
        },
    )
    assert duplicate.status_code == 409

    hidden_detail = await client.get(
        f"/api/v1/experiments/{experiment_id}", headers=other_headers
    )
    hidden_update = await client.patch(
        f"/api/v1/experiments/{experiment_id}",
        headers=other_headers,
        json={"expected_row_version": 1, "description": "not allowed"},
    )
    hidden_delete = await client.delete(
        f"/api/v1/experiments/{experiment_id}?expected_row_version=1",
        headers=other_headers,
    )
    assert [hidden_detail.status_code, hidden_update.status_code, hidden_delete.status_code] == [
        404,
        404,
        404,
    ]

    updated = await client.patch(
        f"/api/v1/experiments/{experiment_id}",
        headers=owner_headers,
        json={"expected_row_version": 1, "description": "updated"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["row_version"] == 2
    assert updated.json()["name"] == "speaker comparison"
    assert updated.json()["dataset_id"] == dataset_id
    assert updated.json()["description"] == "updated"

    stale = await client.patch(
        f"/api/v1/experiments/{experiment_id}",
        headers=owner_headers,
        json={"expected_row_version": 1, "description": "stale"},
    )
    immutable_dataset = await client.patch(
        f"/api/v1/experiments/{experiment_id}",
        headers=owner_headers,
        json={
            "expected_row_version": 2,
            "description": "attempted move",
            "dataset_id": "another-dataset",
        },
    )
    immutable_name = await client.patch(
        f"/api/v1/experiments/{experiment_id}",
        headers=owner_headers,
        json={
            "expected_row_version": 2,
            "description": "attempted rename",
            "name": "renamed",
        },
    )
    missing_change = await client.patch(
        f"/api/v1/experiments/{experiment_id}",
        headers=owner_headers,
        json={"expected_row_version": 2},
    )
    assert stale.status_code == 409
    assert immutable_dataset.status_code == 422
    assert immutable_name.status_code == 422
    assert missing_change.status_code == 422

    admin_update = await client.patch(
        f"/api/v1/experiments/{experiment_id}",
        headers=admin_headers,
        json={"expected_row_version": 2, "description": None},
    )
    assert admin_update.status_code == 200, admin_update.text
    assert admin_update.json()["row_version"] == 3
    assert admin_update.json()["description"] is None

    async with app.state.database.session_factory() as session:
        experiment = await session.get(Experiment, experiment_id)
        assert experiment is not None
        assert experiment.name_conflict_key == "speaker comparison"
        events = list(
            (
                await session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.resource_id == experiment_id)
                    .order_by(AuditEvent.occurred_at.asc())
                )
            ).all()
        )
    assert [event.action for event in events] == [
        "experiment.created",
        "experiment.updated",
        "experiment.updated",
    ]
    serialized_details = " ".join(str(event.details_json) for event in events)
    assert "initial" not in serialized_details
    assert "updated" not in serialized_details


async def test_concurrent_experiment_updates_accept_only_one_version(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    dataset_id = await _dataset(client, admin_headers, "concurrent-experiment-dataset")
    created = await _experiment(
        client,
        admin_headers,
        dataset_id,
        "concurrent experiment",
    )
    experiment_id = str(created["id"])

    first, second = await asyncio.gather(
        client.patch(
            f"/api/v1/experiments/{experiment_id}",
            headers=admin_headers,
            json={"expected_row_version": 1, "description": "first"},
        ),
        client.patch(
            f"/api/v1/experiments/{experiment_id}",
            headers=admin_headers,
            json={"expected_row_version": 1, "description": "second"},
        ),
    )
    assert sorted((first.status_code, second.status_code)) == [200, 409]
    current = await client.get(
        f"/api/v1/experiments/{experiment_id}", headers=admin_headers
    )
    assert current.status_code == 200
    assert current.json()["row_version"] == 2
    assert current.json()["description"] in {"first", "second"}

    async with app.state.database.session_factory() as session:
        update_events = list(
            (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.resource_id == experiment_id,
                        AuditEvent.action == "experiment.updated",
                    )
                )
            ).all()
        )
    assert len(update_events) == 1


async def test_one_experiment_accepts_multiple_immutable_job_conditions(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    dataset_id = await _dataset(client, admin_headers, "multi-condition-dataset")
    experiment = await _experiment(
        client,
        admin_headers,
        dataset_id,
        "multi-condition experiment",
    )
    experiment_id = str(experiment["id"])
    payloads = (
        {
            "job_name": "speaker-v2-40k-rmvpe",
            "experiment_id": experiment_id,
            "dataset_id": dataset_id,
            "model": {"version": "v2", "sample_rate": "40k", "use_f0": True},
            "f0_extraction": {"training_f0_method": "rmvpe"},
        },
        {
            "job_name": "speaker-v2-48k-harvest",
            "experiment_id": experiment_id,
            "dataset_id": dataset_id,
            "model": {"version": "v2", "sample_rate": "48k", "use_f0": True},
            "f0_extraction": {"training_f0_method": "harvest"},
        },
    )
    created = [
        await client.post("/api/v1/jobs", headers=admin_headers, json=payload)
        for payload in payloads
    ]
    assert [response.status_code for response in created] == [201, 201]
    assert {response.json()["dataset_id"] for response in created} == {dataset_id}
    assert {response.json()["config"]["model"]["sample_rate"] for response in created} == {
        "40k",
        "48k",
    }
    assert {
        response.json()["config"]["f0_extraction"]["training_f0_method"]
        for response in created
    } == {"rmvpe", "harvest"}

    listed = await client.get(
        f"/api/v1/jobs?experiment_id={experiment_id}&offset=0&limit=200",
        headers=admin_headers,
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 2
    assert {item["job_name"] for item in listed.json()["items"]} == {
        "speaker-v2-40k-rmvpe",
        "speaker-v2-48k-harvest",
    }


async def test_experiment_delete_blocks_jobs_and_mlflow_projection_then_audits(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    dataset_id = await _dataset(client, admin_headers, "delete-experiment-dataset")
    referenced = await _experiment(
        client,
        admin_headers,
        dataset_id,
        "referenced experiment",
    )
    referenced_id = str(referenced["id"])
    job = await client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        json={
            "job_name": "delete-reference-job",
            "experiment_id": referenced_id,
            "dataset_id": dataset_id,
            "model": {"version": "v2", "sample_rate": "40k"},
        },
    )
    assert job.status_code == 201, job.text
    blocked_by_job = await client.delete(
        f"/api/v1/experiments/{referenced_id}?expected_row_version=1",
        headers=admin_headers,
    )
    assert blocked_by_job.status_code == 409
    assert "jobs" in blocked_by_job.json()["detail"]

    projected = await _experiment(
        client,
        admin_headers,
        dataset_id,
        "projected experiment",
    )
    projected_id = str(projected["id"])
    async with app.state.database.session_factory() as session:
        session.add(
            MlflowSyncEvent(
                event_key=f"experiment:{projected_id}",
                event_type="experiment.created",
                aggregate_type="experiment",
                aggregate_id=projected_id,
                payload_json={"manager_experiment_id": projected_id},
                status="pending",
                attempt_count=0,
            )
        )
        await session.commit()
    blocked_by_projection = await client.delete(
        f"/api/v1/experiments/{projected_id}?expected_row_version=1",
        headers=admin_headers,
    )
    assert blocked_by_projection.status_code == 409
    assert "MLflow" in blocked_by_projection.json()["detail"]

    disposable = await _experiment(
        client,
        admin_headers,
        dataset_id,
        "disposable experiment",
    )
    disposable_id = str(disposable["id"])
    stale_delete = await client.delete(
        f"/api/v1/experiments/{disposable_id}?expected_row_version=2",
        headers=admin_headers,
    )
    assert stale_delete.status_code == 409
    deleted = await client.delete(
        f"/api/v1/experiments/{disposable_id}?expected_row_version=1",
        headers=admin_headers,
    )
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert (
        await client.get(
            f"/api/v1/experiments/{disposable_id}", headers=admin_headers
        )
    ).status_code == 404

    async with app.state.database.session_factory() as session:
        event = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.resource_id == disposable_id,
                AuditEvent.action == "experiment.deleted",
            )
        )
        assert event is not None
        assert event.details_json == {"dataset_id": dataset_id, "row_version": 1}


async def test_experiment_bounds_pagination_and_openapi_contract(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    dataset_id = await _dataset(client, admin_headers, "pagination-experiment-dataset")
    created_ids = {
        str(
            (
                await _experiment(
                    client,
                    admin_headers,
                    dataset_id,
                    f"pagination experiment {index}",
                )
            )["id"]
        )
        for index in range(3)
    }
    first = await client.get(
        "/api/v1/experiments?offset=0&limit=2", headers=admin_headers
    )
    second = await client.get(
        "/api/v1/experiments?offset=2&limit=2", headers=admin_headers
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["total"] == 3
    assert second.json()["total"] == 3
    listed_ids = {item["id"] for item in first.json()["items"] + second.json()["items"]}
    assert listed_ids == created_ids
    assert (
        await client.get("/api/v1/experiments?limit=201", headers=admin_headers)
    ).status_code == 422

    unknown_field = await client.post(
        "/api/v1/experiments",
        headers=admin_headers,
        json={
            "name": "unknown field",
            "dataset_id": dataset_id,
            "description": None,
            "manager_path": "/api/v1/workers",
        },
    )
    unsafe_name = await client.post(
        "/api/v1/experiments",
        headers=admin_headers,
        json={"name": "../unsafe", "dataset_id": dataset_id, "description": None},
    )
    oversized = await client.post(
        "/api/v1/experiments",
        headers={**admin_headers, "Content-Type": "application/json"},
        content=(
            '{"name":"oversized","dataset_id":"'
            + dataset_id
            + '","description":"'
            + "x" * 17_000
            + '"}'
        ).encode(),
    )
    async def oversized_chunks():
        yield (
            '{"name":"oversized-stream","dataset_id":"'
            + dataset_id
            + '","description":"'
        ).encode()
        yield b"x" * 17_000
        yield b'"}'

    oversized_stream = await client.post(
        "/api/v1/experiments",
        headers={**admin_headers, "Content-Type": "application/json"},
        content=oversized_chunks(),
    )
    assert unknown_field.status_code == 422
    assert unsafe_name.status_code == 422
    assert oversized.status_code == 413
    assert oversized_stream.status_code == 413

    openapi = (await client.get("/openapi.json")).json()
    detail_path = openapi["paths"]["/api/v1/experiments/{experiment_id}"]
    assert {"get", "patch", "delete"}.issubset(detail_path)
    assert "404" in detail_path["get"]["responses"]
    assert {"200", "404", "409", "413", "422"}.issubset(
        detail_path["patch"]["responses"]
    )
    assert {"204", "404", "409", "422"}.issubset(
        detail_path["delete"]["responses"]
    )
    update_schema = openapi["components"]["schemas"]["ExperimentUpdate"]
    assert set(update_schema["properties"]) == {"expected_row_version", "description"}
    assert "expected_row_version" in update_schema["required"]
    read_schema = openapi["components"]["schemas"]["ExperimentRead"]
    assert "row_version" in read_schema["properties"]
    create_responses = openapi["paths"]["/api/v1/experiments"]["post"]["responses"]
    assert {"201", "404", "409", "413", "422"}.issubset(create_responses)
