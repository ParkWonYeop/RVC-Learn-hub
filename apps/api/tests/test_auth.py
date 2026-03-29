from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Literal

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from rvc_manager_api.bootstrap import (
    BootstrapError,
    build_parser,
    ensure_admin_user,
    resolve_bootstrap_credentials,
)
from rvc_manager_api.config import Settings
from rvc_manager_api.database import Database
from rvc_manager_api.models import (
    AuditEvent,
    Dataset,
    RevokedAccessToken,
    User,
    Worker,
)
from rvc_manager_api.security import hash_password, issue_access_token
from rvc_orchestrator_contracts import utc_now

USER_PASSWORD = "ordinary-user-password-1234"


async def seed_user(
    app: FastAPI,
    *,
    email: str,
    role: Literal["admin", "user"] = "user",
    disabled: bool = False,
    password: str = USER_PASSWORD,
) -> User:
    encoded = await run_in_threadpool(hash_password, password)
    async with app.state.database.session_factory() as session:
        user = User(
            email=email.casefold(),
            password_hash=encoded,
            role=role,
            disabled=disabled,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def login_headers(
    client: AsyncClient,
    *,
    email: str,
    password: str = USER_PASSWORD,
) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    assert response.json()["token_type"] == "bearer"
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def worker_capabilities() -> dict[str, object]:
    return {
        "worker_version": "0.1.0",
        "rvc_commit_hash": "0123456789abcdef",
        "supported_rvc_versions": ["v2"],
        "supported_training_f0_methods": ["rmvpe"],
        "gpus": [
            {
                "index": 0,
                "name": "Test GPU",
                "total_vram_mb": 24 * 1024,
                "free_vram_mb": 20 * 1024,
            }
        ],
        "disk_free_bytes": 500_000_000_000,
        "rvc_assets_ready": True,
    }


async def register_worker(client: AsyncClient, name: str) -> tuple[str, str]:
    response = await client.post(
        "/api/v1/workers/register",
        headers={"X-Worker-Bootstrap-Token": "test-bootstrap-token"},
        json={"name": name, "capabilities": worker_capabilities()},
    )
    assert response.status_code == 201, response.text
    return response.json()["worker_id"], response.json()["worker_token"]


async def test_login_me_uses_argon2_and_audits_without_plaintext(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    login = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "  ADMIN@EXAMPLE.TEST ",
            "password": "correct-horse-battery-staple",
        },
    )
    assert login.status_code == 200, login.text
    body = login.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert body["access_token"].count(".") == 2
    assert login.headers["Cache-Control"] == "no-store"

    auth = {"Authorization": f"Bearer {body['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=auth)
    assert me.status_code == 200
    assert me.json()["email"] == "admin@example.test"
    assert me.json()["role"] == "admin"
    assert me.json()["disabled"] is False

    async with app.state.database.session_factory() as session:
        admin = await session.scalar(select(User).where(User.email == "admin@example.test"))
        assert admin is not None
        assert admin.password_hash.startswith("$argon2id$")
        events = list((await session.scalars(select(AuditEvent))).all())
        assert any(event.action == "auth.login.succeeded" for event in events)
        persisted = " ".join(str(event.details_json) for event in events)
        assert body["access_token"] not in persisted
        assert "correct-horse-battery-staple" not in persisted


async def test_login_failures_do_not_enumerate_users(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    await seed_user(app, email="disabled@example.test", disabled=True)
    attempts = (
        {"email": "missing@example.test", "password": USER_PASSWORD},
        {"email": "admin@example.test", "password": "wrong-password"},
        {"email": "disabled@example.test", "password": USER_PASSWORD},
    )
    responses = [await client.post("/api/v1/auth/login", json=item) for item in attempts]
    assert [response.status_code for response in responses] == [401, 401, 401]
    assert {response.json()["detail"] for response in responses} == {"incorrect email or password"}
    assert {response.headers["WWW-Authenticate"] for response in responses} == {"Bearer"}


async def test_anonymous_and_worker_credentials_cannot_use_manager_api(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    anonymous_requests = (
        await client.get("/api/v1/auth/me"),
        await client.get("/api/v1/datasets"),
        await client.get("/api/v1/experiments"),
        await client.get("/api/v1/jobs"),
        await client.get("/api/v1/workers"),
        await client.post(
            "/api/v1/datasets",
            json={"name": "anonymous", "storage_uri": "s3://datasets/anonymous.zip"},
        ),
    )
    assert all(response.status_code == 401 for response in anonymous_requests)

    _, worker_token = await register_worker(client, "auth-domain-worker")
    worker_auth = {"Authorization": f"Bearer {worker_token}"}
    assert (await client.get("/api/v1/auth/me", headers=worker_auth)).status_code == 401
    assert (await client.get("/api/v1/workers/me", headers=admin_headers)).status_code == 401


async def test_user_ownership_admin_visibility_and_legacy_null_owner(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    await seed_user(app, email="owner@example.test")
    await seed_user(app, email="other@example.test")
    owner_headers = await login_headers(client, email="owner@example.test")
    other_headers = await login_headers(client, email="other@example.test")

    dataset = await client.post(
        "/api/v1/datasets",
        headers=owner_headers,
        json={
            "name": "owner-dataset",
            "storage_uri": "s3://datasets/owner.zip",
            "flat_storage_uri": "s3://datasets/owner-flat/",
        },
    )
    assert dataset.status_code == 201, dataset.text
    dataset_id = dataset.json()["id"]
    experiment = await client.post(
        "/api/v1/experiments",
        headers=owner_headers,
        json={"name": "owner-experiment", "dataset_id": dataset_id},
    )
    assert experiment.status_code == 201, experiment.text
    experiment_id = experiment.json()["id"]
    job = await client.post(
        "/api/v1/jobs",
        headers=owner_headers,
        json={
            "job_name": "owner-job",
            "experiment_id": experiment_id,
            "dataset_id": dataset_id,
            "model": {"version": "v2", "sample_rate": "40k"},
        },
    )
    assert job.status_code == 201, job.text

    assert (await client.get("/api/v1/datasets", headers=other_headers)).json()["total"] == 0
    assert (
        await client.get(f"/api/v1/datasets/{dataset_id}", headers=other_headers)
    ).status_code == 404
    assert (
        await client.get(f"/api/v1/jobs/{job.json()['id']}", headers=other_headers)
    ).status_code == 404
    assert (
        await client.post(
            "/api/v1/experiments",
            headers=other_headers,
            json={"name": "foreign", "dataset_id": dataset_id},
        )
    ).status_code == 404

    assert (await client.get("/api/v1/datasets", headers=admin_headers)).json()["total"] == 1
    assert (
        await client.get(f"/api/v1/jobs/{job.json()['id']}", headers=admin_headers)
    ).status_code == 200
    assert (await client.get("/api/v1/workers", headers=owner_headers)).status_code == 403

    async with app.state.database.session_factory() as session:
        legacy = Dataset(
            name="legacy-orphan",
            storage_uri="s3://datasets/legacy.zip",
            created_by=None,
        )
        session.add(legacy)
        await session.commit()
        await session.refresh(legacy)
        legacy_id = legacy.id
    assert (
        await client.get(f"/api/v1/datasets/{legacy_id}", headers=owner_headers)
    ).status_code == 404
    assert (
        await client.get(f"/api/v1/datasets/{legacy_id}", headers=admin_headers)
    ).status_code == 200

    cancelled = await client.post(
        f"/api/v1/jobs/{job.json()['id']}/cancel",
        headers=owner_headers,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


async def test_logout_revokes_jti_and_expired_tokens_are_rejected(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    headers = await login_headers(
        client,
        email="admin@example.test",
        password="correct-horse-battery-staple",
    )
    raw_token = headers["Authorization"].removeprefix("Bearer ")
    logout = await client.post("/api/v1/auth/logout", headers=headers)
    assert logout.status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401

    async with app.state.database.session_factory() as session:
        revoked = list((await session.scalars(select(RevokedAccessToken))).all())
        assert len(revoked) == 1
        assert revoked[0].jti not in raw_token
        events = list(
            (
                await session.scalars(select(AuditEvent).where(AuditEvent.action == "auth.logout"))
            ).all()
        )
        assert len(events) == 1
        assert raw_token not in str(events[0].details_json)

        admin = await session.scalar(select(User).where(User.email == "admin@example.test"))
        assert admin is not None
        expired_token, _ = issue_access_token(
            admin.id,
            app.state.settings,
            now=utc_now()
            - timedelta(
                seconds=app.state.settings.jwt_access_ttl_seconds
                + app.state.settings.jwt_leeway_seconds
                + 1
            ),
        )
    expired_headers = {"Authorization": f"Bearer {expired_token}"}
    assert (await client.get("/api/v1/auth/me", headers=expired_headers)).status_code == 401


async def test_concurrent_logout_never_returns_server_error(client: AsyncClient) -> None:
    headers = await login_headers(
        client,
        email="admin@example.test",
        password="correct-horse-battery-staple",
    )
    first, second = await asyncio.gather(
        client.post("/api/v1/auth/logout", headers=headers),
        client.post("/api/v1/auth/logout", headers=headers),
    )
    assert {first.status_code, second.status_code}.issubset({204, 401})


async def test_bootstrap_is_one_time_and_accepts_protected_secret_file(
    app: FastAPI,
    tmp_path,
) -> None:
    async with app.state.database.session_factory() as session:
        existing, created = await ensure_admin_user(
            session,
            email="ADMIN@example.test",
            password="unused-idempotent-password",
        )
        assert created is False
        assert existing.email == "admin@example.test"
    async with app.state.database.session_factory() as session:
        with pytest.raises(BootstrapError, match="already closed"):
            await ensure_admin_user(
                session,
                email="second-admin@example.test",
                password="another-secure-password",
            )

    email_file = tmp_path / "admin-email"
    password_file = tmp_path / "admin-password"
    email_file.write_text("from-file@example.test\n", encoding="utf-8")
    password_file.write_text("file-password-123456\n", encoding="utf-8")
    password_file.chmod(0o600)
    assert resolve_bootstrap_credentials(
        email_file=email_file,
        password_file=password_file,
        environ={},
    ) == ("from-file@example.test", "file-password-123456")
    with pytest.raises(BootstrapError, match="forbidden"):
        resolve_bootstrap_credentials(
            email="admin@example.test",
            environ={"ADMIN_BOOTSTRAP_PASSWORD": "must-not-be-an-environment-secret"},
        )
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--password", "must-never-be-a-cli-argument"])


async def test_concurrent_bootstrap_creates_exactly_one_initial_admin(tmp_path) -> None:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'bootstrap-race.db'}",
        jwt_secret="bootstrap-race-jwt-secret-with-at-least-thirty-two-characters",
    )
    database = Database(settings)
    await database.create_all()

    async def attempt(email: str) -> tuple[str, bool]:
        async with database.session_factory() as session:
            user, created = await ensure_admin_user(
                session,
                email=email,
                password="concurrent-bootstrap-password",
            )
            return user.email, created

    results = await asyncio.gather(
        attempt("first@example.test"),
        attempt("second@example.test"),
        return_exceptions=True,
    )
    assert len([result for result in results if not isinstance(result, Exception)]) == 1
    assert len([result for result in results if isinstance(result, BootstrapError)]) == 1
    async with database.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 1
    await database.dispose()


def test_production_requires_distinct_jwt_secret_or_readable_secret_file(tmp_path) -> None:
    production = {
        "environment": "production",
        "database_url": "postgresql+asyncpg://manager:test@db/manager",
        "worker_bootstrap_token": "bootstrap",
        "worker_token_pepper": "production-worker-token-pepper-long-value",
        "storage_backend": "s3",
        "s3_endpoint_url": "https://minio.example.test",
        "s3_presign_endpoint_url": "https://objects.example.test",
        "s3_access_key_id": "test-access-key",
        "s3_secret_access_key": "test-secret-key",
    }
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(**production)

    secret_file = tmp_path / "jwt-secret"
    secret_file.write_text("file-jwt-secret-with-at-least-thirty-two-characters\n")
    settings = Settings(**production, jwt_secret_file=secret_file)
    assert settings.jwt_secret.get_secret_value().startswith("file-jwt-secret")
    with pytest.raises(ValidationError, match="distinct"):
        Settings(
            **production,
            jwt_secret="production-worker-token-pepper-long-value",
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://objects.example.test",
        "https://user:password@objects.example.test",
        "https://objects.example.test?signature=secret",
        "https://objects.example.test#fragment",
        "objects.example.test",
    ],
)
def test_production_presign_endpoint_requires_clean_absolute_https(endpoint: str) -> None:
    with pytest.raises(ValidationError, match="S3_PRESIGN_ENDPOINT_URL"):
        Settings(
            environment="production",
            database_url="postgresql+asyncpg://manager:test@db/manager",
            worker_bootstrap_token="bootstrap",
            worker_token_pepper="production-worker-token-pepper-long-value",
            jwt_secret="production-jwt-secret-with-at-least-thirty-two-characters",
            storage_backend="s3",
            s3_endpoint_url="http://minio:9000",
            s3_presign_endpoint_url=endpoint,
            s3_access_key_id="test-access-key",
            s3_secret_access_key="test-secret-key",
        )


def test_nonproduction_presign_endpoint_allows_clean_absolute_http_only() -> None:
    settings = Settings(
        environment="test",
        storage_backend="s3",
        s3_endpoint_url="http://minio:9000",
        s3_presign_endpoint_url="http://objects.example.test:9000/minio",
        s3_access_key_id="test-access-key",
        s3_secret_access_key="test-secret-key",
        jwt_secret="test-jwt-secret-with-at-least-thirty-two-characters",
    )
    assert settings.s3_presign_endpoint_url.startswith("http://")
    with pytest.raises(ValidationError, match="absolute HTTP"):
        Settings(
            environment="test",
            storage_backend="s3",
            s3_endpoint_url="http://minio:9000",
            s3_presign_endpoint_url="relative/minio",
            s3_access_key_id="test-access-key",
            s3_secret_access_key="test-secret-key",
            jwt_secret="test-jwt-secret-with-at-least-thirty-two-characters",
        )


async def test_admin_worker_list_detail_and_next_job_route_order(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    first_id, first_token = await register_worker(client, "worker-list-a")
    second_id, _ = await register_worker(client, "worker-list-b")
    async with app.state.database.session_factory() as session:
        second = await session.get(Worker, second_id)
        assert second is not None
        second.last_heartbeat_at = utc_now() - timedelta(hours=1)
        second.is_active = False
        await session.commit()

    response = await client.get(
        "/api/v1/workers?offset=0&limit=1",
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2
    assert len(response.json()["items"]) == 1
    assert "token_hash" not in response.text
    assert "worker_token" not in response.text

    detail = await client.get(f"/api/v1/workers/{second_id}", headers=admin_headers)
    assert detail.status_code == 200
    assert detail.json()["online"] is False
    assert detail.json()["is_active"] is False
    assert detail.json()["capabilities"]["worker_version"] == "0.1.0"

    # The admin detail route is deliberately registered after static Worker
    # protocol paths; a worker bearer must still reach /next-job.
    next_job = await client.get(
        "/api/v1/workers/next-job",
        headers={"Authorization": f"Bearer {first_token}"},
    )
    assert next_job.status_code == 204
    assert first_id != second_id
