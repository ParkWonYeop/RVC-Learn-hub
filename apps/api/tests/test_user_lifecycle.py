from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import func, select

from rvc_manager_api.models import AdminUserOperation, AuditEvent, User

ADMIN_PASSWORD = "correct-horse-battery-staple"
MANAGED_PASSWORD = "Violet-River-Quartz-9274!"
RESET_PASSWORD = "Copper-Meteor-Lantern-4826!"


def _mutation_headers(auth: dict[str, str], key: str) -> dict[str, str]:
    return {**auth, "Idempotency-Key": key}


async def _create_user(
    client: AsyncClient,
    admin_headers: dict[str, str],
    *,
    email: str,
    key: str,
    password: str = MANAGED_PASSWORD,
    role: str = "user",
    active: bool = True,
) -> Response:
    return await client.post(
        "/api/v1/admin/users",
        headers=_mutation_headers(admin_headers, key),
        json={
            "email": email,
            "password": password,
            "role": role,
            "active": active,
        },
    )


async def _login(
    client: AsyncClient,
    *,
    email: str,
    password: str,
) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_admin_user_create_replay_list_filter_and_secret_free_audit(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    created = await _create_user(
        client,
        admin_headers,
        email="  MANAGED@EXAMPLE.TEST ",
        key="create-managed-user-1",
    )
    assert created.status_code == 201, created.text
    assert created.headers["Cache-Control"] == "no-store"
    body = created.json()
    assert body == {
        "id": body["id"],
        "email": "managed@example.test",
        "role": "user",
        "active": True,
        "row_version": 1,
        "created_at": body["created_at"],
        "updated_at": body["updated_at"],
    }
    assert not {"password", "password_hash", "access_token_version", "disabled"} & body.keys()

    replay = await _create_user(
        client,
        admin_headers,
        email="managed@example.test",
        key="create-managed-user-1",
    )
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == body

    key_conflict = await _create_user(
        client,
        admin_headers,
        email="managed@example.test",
        key="create-managed-user-1",
        role="admin",
    )
    assert key_conflict.status_code == 409
    assert (
        key_conflict.json()["detail"]
        == "idempotency key conflicts with a prior user lifecycle request"
    )
    duplicate = await _create_user(
        client,
        admin_headers,
        email="MANAGED@EXAMPLE.TEST",
        key="create-managed-user-duplicate",
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "user email already exists"

    filtered = await client.get(
        "/api/v1/admin/users",
        headers=admin_headers,
        params={"email": " MANAGED@EXAMPLE.TEST ", "role": "user", "active": "true"},
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.headers["Cache-Control"] == "no-store"
    assert filtered.json() == {
        "items": [body],
        "total": 1,
        "offset": 0,
        "limit": 50,
    }
    paged = await client.get(
        "/api/v1/admin/users",
        headers=admin_headers,
        params={"offset": 1, "limit": 1},
    )
    assert paged.status_code == 200
    assert paged.json()["total"] == 2
    assert len(paged.json()["items"]) == 1
    detail = await client.get(f"/api/v1/admin/users/{body['id']}", headers=admin_headers)
    assert detail.status_code == 200
    assert detail.json() == body

    unknown_field = await client.post(
        "/api/v1/admin/users",
        headers=_mutation_headers(admin_headers, "create-unknown-field"),
        json={
            "email": "unknown-field@example.test",
            "password": MANAGED_PASSWORD,
            "role": "user",
            "active": True,
            "unknown": True,
        },
    )
    assert unknown_field.status_code == 422
    oversized = await client.post(
        "/api/v1/admin/users",
        headers=_mutation_headers(admin_headers, "create-oversized"),
        json={
            "email": "oversized@example.test",
            "password": MANAGED_PASSWORD,
            "role": "user",
            "active": True,
            "padding": "x" * 20_000,
        },
    )
    assert oversized.status_code == 413
    assert oversized.json()["detail"] == "user lifecycle request body is too large"
    weak = await _create_user(
        client,
        admin_headers,
        email="weak@example.test",
        key="create-weak-password",
        password="aaaaaaaaaaaaaaaa",
    )
    assert weak.status_code == 422
    assert weak.json()["detail"] == "password does not satisfy the administrator password policy"

    async with app.state.database.session_factory() as session:
        user = await session.scalar(select(User).where(User.id == body["id"]))
        assert user is not None
        assert user.password_hash.startswith("$argon2id$")
        assert user.password_hash != MANAGED_PASSWORD
        operations = list((await session.scalars(select(AdminUserOperation))).all())
        assert len(operations) == 1
        assert operations[0].operation_type == "create"
        assert operations[0].idempotency_key_hash != "create-managed-user-1"
        events = list((await session.scalars(select(AuditEvent))).all())
        persisted = " ".join(
            [
                *(str(event.details_json) for event in events),
                *(str(operation.response_json) for operation in operations),
                *(operation.request_fingerprint for operation in operations),
            ]
        )
        assert MANAGED_PASSWORD not in persisted
        assert "create-managed-user-1" not in persisted
        assert len(
            [event for event in events if event.action == "admin.user.created"]
        ) == 1


async def test_non_admin_user_cannot_enumerate_or_mutate_users(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    created = await _create_user(
        client,
        admin_headers,
        email="ordinary@example.test",
        key="create-ordinary-for-rbac",
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    user_headers = await _login(
        client,
        email="ordinary@example.test",
        password=MANAGED_PASSWORD,
    )
    requests = (
        await client.get("/api/v1/admin/users", headers=user_headers),
        await client.get(f"/api/v1/admin/users/{user_id}", headers=user_headers),
        await client.get(
            "/api/v1/admin/users/00000000-0000-4000-8000-000000000099",
            headers=user_headers,
        ),
        await _create_user(
            client,
            user_headers,
            email="forbidden@example.test",
            key="forbidden-create",
        ),
        await client.patch(
            f"/api/v1/admin/users/{user_id}",
            headers=_mutation_headers(user_headers, "forbidden-patch"),
            json={"expected_row_version": 1, "role": "admin", "active": True},
        ),
        await client.post(
            f"/api/v1/admin/users/{user_id}/password-reset",
            headers=_mutation_headers(user_headers, "forbidden-reset"),
            json={"expected_row_version": 1, "new_password": RESET_PASSWORD},
        ),
    )
    assert {response.status_code for response in requests} == {403}
    assert {response.json()["detail"] for response in requests} == {
        "administrator role required"
    }


async def test_access_changes_are_cas_fenced_and_tokens_never_resurrect(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    created = await _create_user(
        client,
        admin_headers,
        email="access-target@example.test",
        key="create-access-target",
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    original_headers = await _login(
        client,
        email="access-target@example.test",
        password=MANAGED_PASSWORD,
    )

    disabled = await client.patch(
        f"/api/v1/admin/users/{user_id}",
        headers=_mutation_headers(admin_headers, "disable-access-target"),
        json={"expected_row_version": 1, "role": "user", "active": False},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["row_version"] == 2
    assert disabled.json()["active"] is False
    assert (await client.get("/api/v1/auth/me", headers=original_headers)).status_code == 401

    enabled = await client.patch(
        f"/api/v1/admin/users/{user_id}",
        headers=_mutation_headers(admin_headers, "enable-access-target"),
        json={"expected_row_version": 2, "role": "user", "active": True},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["row_version"] == 3
    # Re-enabling cannot resurrect the token minted before disable.
    assert (await client.get("/api/v1/auth/me", headers=original_headers)).status_code == 401

    stale = await client.patch(
        f"/api/v1/admin/users/{user_id}",
        headers=_mutation_headers(admin_headers, "stale-access-target"),
        json={"expected_row_version": 1, "role": "admin", "active": True},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == "user changed; refresh and retry"

    unchanged = await client.patch(
        f"/api/v1/admin/users/{user_id}",
        headers=_mutation_headers(admin_headers, "unchanged-access-target"),
        json={"expected_row_version": 3, "role": "user", "active": True},
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["row_version"] == 3
    unchanged_replay = await client.patch(
        f"/api/v1/admin/users/{user_id}",
        headers=_mutation_headers(admin_headers, "unchanged-access-target"),
        json={"expected_row_version": 3, "role": "user", "active": True},
    )
    assert unchanged_replay.status_code == 200
    assert unchanged_replay.headers["Idempotency-Replayed"] == "true"
    assert unchanged_replay.json() == unchanged.json()

    me = await client.get("/api/v1/auth/me", headers=admin_headers)
    assert me.status_code == 200
    admin_id = me.json()["id"]
    self_demotion = await client.patch(
        f"/api/v1/admin/users/{admin_id}",
        headers=_mutation_headers(admin_headers, "self-demotion"),
        json={"expected_row_version": 1, "role": "user", "active": True},
    )
    self_disable = await client.patch(
        f"/api/v1/admin/users/{admin_id}",
        headers=_mutation_headers(admin_headers, "self-disable"),
        json={"expected_row_version": 1, "role": "admin", "active": False},
    )
    assert {self_demotion.status_code, self_disable.status_code} == {409}
    assert {self_demotion.json()["detail"], self_disable.json()["detail"]} == {
        "administrators cannot disable or demote their own account"
    }

    async with app.state.database.session_factory() as session:
        user = await session.get(User, user_id)
        assert user is not None
        assert user.row_version == 3
        assert user.access_token_version == 3
        actions = list(
            (
                await session.scalars(
                    select(AuditEvent.action).where(AuditEvent.resource_id == user_id)
                )
            ).all()
        )
        assert actions.count("admin.user.access_updated") == 2
        assert actions.count("admin.user.access_unchanged") == 1


async def test_password_reset_invalidates_all_prior_tokens_and_is_idempotent(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    created = await _create_user(
        client,
        admin_headers,
        email="reset-target@example.test",
        key="create-reset-target",
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    first_token = await _login(
        client,
        email="reset-target@example.test",
        password=MANAGED_PASSWORD,
    )
    second_token = await _login(
        client,
        email="reset-target@example.test",
        password=MANAGED_PASSWORD,
    )
    reset_payload: dict[str, Any] = {
        "expected_row_version": 1,
        "new_password": RESET_PASSWORD,
    }
    reset = await client.post(
        f"/api/v1/admin/users/{user_id}/password-reset",
        headers=_mutation_headers(admin_headers, "reset-target-password"),
        json=reset_payload,
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["row_version"] == 2
    assert (await client.get("/api/v1/auth/me", headers=first_token)).status_code == 401
    assert (await client.get("/api/v1/auth/me", headers=second_token)).status_code == 401
    old_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "reset-target@example.test", "password": MANAGED_PASSWORD},
    )
    assert old_login.status_code == 401
    new_headers = await _login(
        client,
        email="reset-target@example.test",
        password=RESET_PASSWORD,
    )
    assert (await client.get("/api/v1/auth/me", headers=new_headers)).status_code == 200

    replay = await client.post(
        f"/api/v1/admin/users/{user_id}/password-reset",
        headers=_mutation_headers(admin_headers, "reset-target-password"),
        json=reset_payload,
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == reset.json()
    conflict = await client.post(
        f"/api/v1/admin/users/{user_id}/password-reset",
        headers=_mutation_headers(admin_headers, "reset-target-password"),
        json={
            "expected_row_version": 1,
            "new_password": "Marble-Signal-Forest-6318!",
        },
    )
    assert conflict.status_code == 409
    assert (
        conflict.json()["detail"]
        == "idempotency key conflicts with a prior user lifecycle request"
    )

    async with app.state.database.session_factory() as session:
        user = await session.get(User, user_id)
        assert user is not None
        assert user.row_version == 2
        assert user.access_token_version == 2
        events = list(
            (
                await session.scalars(
                    select(AuditEvent).where(AuditEvent.resource_id == user_id)
                )
            ).all()
        )
        assert len(
            [event for event in events if event.action == "admin.user.password_reset"]
        ) == 1
        assert RESET_PASSWORD not in " ".join(str(event.details_json) for event in events)


async def test_concurrent_cross_demotion_preserves_one_active_admin(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    second = await _create_user(
        client,
        admin_headers,
        email="second-admin@example.test",
        key="create-second-admin",
        role="admin",
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["id"]
    second_headers = await _login(
        client,
        email="second-admin@example.test",
        password=MANAGED_PASSWORD,
    )
    first_me = await client.get("/api/v1/auth/me", headers=admin_headers)
    assert first_me.status_code == 200
    first_id = first_me.json()["id"]

    demote_second, demote_first = await asyncio.gather(
        client.patch(
            f"/api/v1/admin/users/{second_id}",
            headers=_mutation_headers(admin_headers, "cross-demote-second"),
            json={"expected_row_version": 1, "role": "user", "active": True},
        ),
        client.patch(
            f"/api/v1/admin/users/{first_id}",
            headers=_mutation_headers(second_headers, "cross-demote-first"),
            json={"expected_row_version": 1, "role": "user", "active": True},
        ),
    )
    statuses = [demote_second.status_code, demote_first.status_code]
    assert statuses.count(200) == 1
    assert statuses.count(401) + statuses.count(403) == 1

    async with app.state.database.session_factory() as session:
        active_admins = await session.scalar(
            select(func.count()).select_from(User).where(
                User.role == "admin",
                User.disabled.is_(False),
            )
        )
        assert active_admins == 1
        users = list(
            (
                await session.scalars(
                    select(User).where(User.id.in_([first_id, second_id]))
                )
            ).all()
        )
        assert sorted(user.role for user in users) == ["admin", "user"]


async def test_access_token_without_version_claim_is_rejected_after_upgrade(
    app: FastAPI,
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    me = await client.get("/api/v1/auth/me", headers=admin_headers)
    assert me.status_code == 200
    now = datetime.now(UTC).replace(microsecond=0)
    settings = app.state.settings
    legacy_token = jwt.encode(
        {
            "sub": me.json()["id"],
            "jti": str(uuid.uuid4()),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )

    rejected = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {legacy_token}"},
    )

    assert rejected.status_code == 401
    assert rejected.json()["detail"] == "invalid or expired access token"
