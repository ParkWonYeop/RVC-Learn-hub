from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

BASE_REVISION = "a6c2e9f4b710"
EXPERIMENT_CRUD_REVISION = "d1e7a9c4f620"


def _config(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    api_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    monkeypatch.setenv(
        "JWT_SECRET",
        "experiment-migration-jwt-secret-with-at-least-thirty-two-characters",
    )
    return Config(str(api_root / "alembic.ini"))


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info('{table}')")}


def test_experiment_migration_quarantines_duplicates_and_restricts_job_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "experiment-crud.db"
    config = _config(database_path, monkeypatch)
    command.upgrade(config, BASE_REVISION)
    connection = sqlite3.connect(database_path)
    timestamp = "2026-07-11 00:00:00+00:00"
    owner_id = "00000000-0000-4000-8000-000000000301"
    dataset_id = "00000000-0000-4000-8000-000000000302"
    connection.execute(
        """
        INSERT INTO users (
            id, email, password_hash, role, disabled, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            owner_id,
            "migration-experiment-owner@example.test",
            "$argon2id$migration-hash",
            "user",
            0,
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        """
        INSERT INTO datasets (
            id, name, storage_uri, flat_storage_uri, is_usable, created_by,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            dataset_id,
            "migration-dataset",
            "s3://datasets/migration.zip",
            "s3://datasets/migration-flat/",
            owner_id,
            timestamp,
            timestamp,
        ),
    )
    experiments = (
        ("00000000-0000-4000-8000-000000000311", "duplicate", owner_id),
        ("00000000-0000-4000-8000-000000000312", "duplicate", owner_id),
        ("00000000-0000-4000-8000-000000000313", "already unique", owner_id),
        ("00000000-0000-4000-8000-000000000314", "orphan history", None),
    )
    connection.executemany(
        """
        INSERT INTO experiments (
            id, name, dataset_id, description, created_by, created_at, updated_at
        ) VALUES (?, ?, ?, NULL, ?, ?, ?)
        """,
        [
            (experiment_id, name, dataset_id, created_by, timestamp, timestamp)
            for experiment_id, name, created_by in experiments
        ],
    )
    connection.commit()
    connection.close()

    command.upgrade(config, EXPERIMENT_CRUD_REVISION)
    connection = sqlite3.connect(database_path)
    assert {"row_version", "name_conflict_key"}.issubset(
        _columns(connection, "experiments")
    )
    migrated = list(
        connection.execute(
            """
            SELECT id, name, created_by, row_version, name_conflict_key
            FROM experiments
            ORDER BY id
            """
        )
    )
    assert migrated == [
        (experiments[0][0], "duplicate", owner_id, 1, None),
        (experiments[1][0], "duplicate", owner_id, 1, None),
        (experiments[2][0], "already unique", owner_id, 1, "already unique"),
        (experiments[3][0], "orphan history", None, 1, None),
    ]
    foreign_keys = list(connection.execute("PRAGMA foreign_key_list('jobs')"))
    assert any(
        row[2] == "experiments"
        and row[3] == "experiment_id"
        and str(row[6]).upper() == "RESTRICT"
        for row in foreign_keys
    )

    fresh_id = "00000000-0000-4000-8000-000000000315"
    connection.execute(
        """
        INSERT INTO experiments (
            id, row_version, name, name_conflict_key, dataset_id, description,
            created_by, created_at, updated_at
        ) VALUES (?, 1, ?, ?, ?, NULL, ?, ?, ?)
        """,
        (
            fresh_id,
            "fresh name",
            "fresh name",
            dataset_id,
            owner_id,
            timestamp,
            timestamp,
        ),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO experiments (
                id, row_version, name, name_conflict_key, dataset_id, description,
                created_by, created_at, updated_at
            ) VALUES (?, 1, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                "00000000-0000-4000-8000-000000000316",
                "fresh name",
                "fresh name",
                dataset_id,
                owner_id,
                timestamp,
                timestamp,
            ),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE experiments SET name_conflict_key = ? WHERE id = ?",
            ("different key", fresh_id),
        )
    connection.rollback()
    connection.close()

    command.downgrade(config, BASE_REVISION)
    connection = sqlite3.connect(database_path)
    assert {"row_version", "name_conflict_key"}.isdisjoint(
        _columns(connection, "experiments")
    )
    downgraded = list(
        connection.execute("SELECT id, name, created_by FROM experiments ORDER BY id")
    )
    assert downgraded == [
        (experiment_id, name, created_by)
        for experiment_id, name, created_by in experiments
    ] + [(fresh_id, "fresh name", owner_id)]
    foreign_keys = list(connection.execute("PRAGMA foreign_key_list('jobs')"))
    assert any(
        row[2] == "experiments"
        and row[3] == "experiment_id"
        and str(row[6]).upper() == "CASCADE"
        for row in foreign_keys
    )
    connection.close()


def test_experiment_migration_renders_postgresql_offline_sql(
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
        "experiment-migration-jwt-secret-with-at-least-thirty-two-characters",
    )
    config = Config(str(api_root / "alembic.ini"))

    command.upgrade(config, EXPERIMENT_CRUD_REVISION, sql=True)
    rendered = capsys.readouterr().out
    assert "row_version INTEGER" in rendered
    assert "name_conflict_key VARCHAR(128)" in rendered
    assert "uq_experiments_owner_name_conflict_key" in rendered
    assert "name_conflict_key IS NULL OR name_conflict_key = name" in rendered
    assert "ON DELETE RESTRICT" in rendered
