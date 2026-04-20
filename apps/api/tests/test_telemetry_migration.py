from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

BASE_REVISION = "c7b1e4d9a260"
WATERMARK_REVISION = "ca8d3e7f4b10"


def _config(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    api_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    monkeypatch.setenv(
        "JWT_SECRET",
        "telemetry-migration-secret-at-least-thirty-two-characters",
    )
    return Config(str(api_root / "alembic.ini"))


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info('{table}')")}


def test_terminal_telemetry_watermark_migration_constraints_and_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "terminal-telemetry.db"
    config = _config(database_path, monkeypatch)
    command.upgrade(config, BASE_REVISION)
    connection = sqlite3.connect(database_path)
    assert {
        "telemetry_log_count",
        "telemetry_metric_count",
    }.isdisjoint(_columns(connection, "job_attempts"))
    ingest_table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ingest_batches'"
    ).fetchone()[0]
    assert "ck_ingest_batches_payload_fingerprint_length" in ingest_table_sql
    connection.close()

    command.upgrade(config, WATERMARK_REVISION)
    connection = sqlite3.connect(database_path)
    assert {
        "telemetry_log_count",
        "telemetry_metric_count",
    }.issubset(_columns(connection, "job_attempts"))
    base_values = (
        "00000000-0000-4000-8000-000000000801",
        "00000000-0000-4000-8000-000000000802",
        "00000000-0000-4000-8000-000000000803",
        1,
        "fake",
        "failed",
        "2026-07-12 00:00:00+00:00",
    )
    statement = """
        INSERT INTO job_attempts (
            id, job_id, worker_id, attempt_number, engine_mode, status, started_at,
            telemetry_log_count, telemetry_metric_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    connection.execute(statement, (*base_values, None, None))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            statement,
            (
                "00000000-0000-4000-8000-000000000811",
                *base_values[1:],
                1,
                None,
            ),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            statement,
            (
                "00000000-0000-4000-8000-000000000812",
                *base_values[1:],
                -1,
                1,
            ),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            statement,
            (
                "00000000-0000-4000-8000-000000000813",
                *base_values[1:],
                1,
                2_147_483_648,
            ),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            statement,
            (
                "00000000-0000-4000-8000-000000000814",
                *base_values[1:5],
                "training",
                base_values[6],
                1,
                1,
            ),
        )
    connection.rollback()
    connection.close()

    command.downgrade(config, BASE_REVISION)
    connection = sqlite3.connect(database_path)
    assert {
        "telemetry_log_count",
        "telemetry_metric_count",
    }.isdisjoint(_columns(connection, "job_attempts"))
    connection.close()


def test_terminal_telemetry_watermark_migration_renders_postgresql(
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
        "telemetry-migration-secret-at-least-thirty-two-characters",
    )
    config = Config(str(api_root / "alembic.ini"))
    command.upgrade(config, WATERMARK_REVISION, sql=True)
    upgrade_sql = capsys.readouterr().out
    assert "ADD COLUMN telemetry_log_count INTEGER" in upgrade_sql
    assert "ADD COLUMN telemetry_metric_count INTEGER" in upgrade_sql
    assert "ck_job_attempts_telemetry_counts_all_null_or_present" in upgrade_sql
    assert "ck_job_attempts_telemetry_log_count_range" in upgrade_sql
    assert "ck_job_attempts_telemetry_metric_count_range" in upgrade_sql
    assert "ck_job_attempts_telemetry_counts_terminal_only" in upgrade_sql
