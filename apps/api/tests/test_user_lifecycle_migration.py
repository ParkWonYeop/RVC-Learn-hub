from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

BASE_REVISION = "f9c4a7d2b610"
USER_LIFECYCLE_REVISION = "b4a91d7e2c63"


def _config(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    api_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    monkeypatch.setenv(
        "JWT_SECRET",
        "user-lifecycle-migration-secret-at-least-thirty-two-characters",
    )
    return Config(str(api_root / "alembic.ini"))


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    return {
        str(row["name"]): row
        for row in connection.execute(f"PRAGMA table_info('{table}')")
    }


def test_user_lifecycle_migration_preserves_users_and_downgrades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "user-lifecycle.db"
    config = _config(database_path, monkeypatch)
    command.upgrade(config, BASE_REVISION)
    connection = sqlite3.connect(database_path)
    timestamp = "2026-07-12 00:00:00+00:00"
    user_id = "00000000-0000-4000-8000-000000000701"
    connection.execute(
        """
        INSERT INTO users (
            id, email, password_hash, role, disabled, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            "migration-admin@example.test",
            "$argon2id$migration-hash",
            "admin",
            0,
            timestamp,
            timestamp,
        ),
    )
    connection.commit()
    connection.close()

    command.upgrade(config, USER_LIFECYCLE_REVISION)
    connection = sqlite3.connect(database_path)
    columns = _columns(connection, "users")
    assert {"row_version", "access_token_version"}.issubset(columns)
    assert columns["row_version"]["dflt_value"] is None
    assert columns["access_token_version"]["dflt_value"] is None
    migrated = connection.execute(
        """
        SELECT email, password_hash, role, disabled, row_version, access_token_version
        FROM users WHERE id = ?
        """,
        (user_id,),
    ).fetchone()
    assert tuple(migrated) == (
        "migration-admin@example.test",
        "$argon2id$migration-hash",
        "admin",
        0,
        1,
        1,
    )
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "admin_user_operations" in tables
    operation_columns = set(_columns(connection, "admin_user_operations"))
    assert {
        "actor_id",
        "idempotency_key_hash",
        "request_fingerprint",
        "operation_type",
        "resource_id",
        "response_json",
        "created_at",
    }.issubset(operation_columns)
    indexes = {
        str(row[1]) for row in connection.execute("PRAGMA index_list('users')")
    }
    assert "ix_users_role_disabled_created_at" in indexes
    operation_indexes = {
        str(row[1])
        for row in connection.execute("PRAGMA index_list('admin_user_operations')")
    }
    assert {
        "ix_admin_user_operations_actor_id",
        "ix_admin_user_operations_resource_id",
        "sqlite_autoindex_admin_user_operations_2",
    }.issubset(operation_indexes)
    foreign_keys = list(
        connection.execute("PRAGMA foreign_key_list('admin_user_operations')")
    )
    assert {
        (str(row[2]), str(row[3]), str(row[6]).upper()) for row in foreign_keys
    } == {
        ("users", "actor_id", "RESTRICT"),
        ("users", "resource_id", "RESTRICT"),
    }
    connection.execute(
        """
        INSERT INTO admin_user_operations (
            id, actor_id, idempotency_key_hash, request_fingerprint,
            operation_type, resource_id, response_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "00000000-0000-4000-8000-000000000702",
            user_id,
            "a" * 64,
            "b" * 64,
            "password_reset",
            user_id,
            json.dumps({"id": user_id, "row_version": 2}),
            timestamp,
        ),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE users SET row_version = 0 WHERE id = ?", (user_id,))
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE users SET access_token_version = 0 WHERE id = ?",
            (user_id,),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO admin_user_operations (
                id, actor_id, idempotency_key_hash, request_fingerprint,
                operation_type, resource_id, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "00000000-0000-4000-8000-000000000703",
                user_id,
                "c" * 64,
                "d" * 64,
                "delete",
                user_id,
                "{}",
                timestamp,
            ),
        )
    connection.rollback()
    connection.close()

    command.downgrade(config, BASE_REVISION)
    connection = sqlite3.connect(database_path)
    assert {"row_version", "access_token_version"}.isdisjoint(
        _columns(connection, "users")
    )
    assert "admin_user_operations" not in {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    preserved = connection.execute(
        "SELECT email, password_hash, role, disabled FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    assert tuple(preserved) == (
        "migration-admin@example.test",
        "$argon2id$migration-hash",
        "admin",
        0,
    )
    connection.close()


def test_user_lifecycle_migration_renders_postgresql_upgrade_and_downgrade(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    api_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://manager:unused@postgres/manager",
    )
    monkeypatch.setenv(
        "JWT_SECRET",
        "user-lifecycle-migration-secret-at-least-thirty-two-characters",
    )
    config = Config(str(api_root / "alembic.ini"))

    command.upgrade(config, USER_LIFECYCLE_REVISION, sql=True)
    upgrade_sql = capsys.readouterr().out
    assert "ADD COLUMN row_version INTEGER DEFAULT '1' NOT NULL" in upgrade_sql
    assert "ADD COLUMN access_token_version INTEGER DEFAULT '1' NOT NULL" in upgrade_sql
    assert "CREATE TABLE admin_user_operations" in upgrade_sql
    assert "JSONB NOT NULL" in upgrade_sql
    assert "uq_admin_user_operation_actor_key" in upgrade_sql
    assert "ON DELETE RESTRICT" in upgrade_sql

    command.downgrade(
        config,
        f"{USER_LIFECYCLE_REVISION}:{BASE_REVISION}",
        sql=True,
    )
    downgrade_sql = capsys.readouterr().out
    assert "DROP TABLE admin_user_operations" in downgrade_sql
    assert "DROP COLUMN access_token_version" in downgrade_sql
    assert "DROP COLUMN row_version" in downgrade_sql
