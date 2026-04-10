from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from rvc_manager_api.app import create_app
from rvc_manager_api.bootstrap import ensure_admin_user
from rvc_manager_api.config import Settings

ADMIN_EMAIL = "admin@example.test"
ADMIN_PASSWORD = "correct-horse-battery-staple"


@pytest_asyncio.fixture
async def app(tmp_path) -> AsyncIterator[FastAPI]:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'manager.db'}",
        worker_bootstrap_token="test-bootstrap-token",
        worker_token_pepper="test-worker-token-pepper",
        jwt_secret="test-jwt-secret-with-at-least-thirty-two-characters",
        lease_seconds=60,
        allow_fake_workers=True,
        storage_backend="local",
        local_storage_root=tmp_path / "object-storage",
        dataset_ingestion_root=tmp_path / "dataset-ingestion",
    )
    instance = create_app(settings)
    await instance.state.database.create_all()
    async with instance.state.database.session_factory() as session:
        await ensure_admin_user(
            session,
            email=ADMIN_EMAIL,
            password=ADMIN_PASSWORD,
        )
    yield instance
    await instance.state.database.dispose()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


@pytest_asyncio.fixture
async def admin_headers(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
