from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

INITIAL_REVISION = "229a6edc0e40"
AUTH_REVISION = "7e9f4a2c1b6d"
ARTIFACT_UPLOAD_REVISION = "a4f8c2d9137e"
DATASET_UPLOAD_REVISION = "c2b7d4e8f901"
MAINTENANCE_REVISION = "e7c9a1b4d260"
TEST_SET_REVISION = "f3a8c6d9e120"
STORAGE_NAMESPACE_REVISION = "9d2f4b7c8e10"
SAMPLE_RUNTIME_REVISION = "b8e4a1c6d230"
WORKER_TOKEN_ROTATION_REVISION = "c4d9e8f1a720"
TEST_SET_UPLOAD_FENCING_REVISION = "a6c2e9f4b710"
DATASET_UPLOAD_FENCING_REVISION = "e2f8b4c6a930"
DATASET_PCM_QUALITY_REVISION = "f9c4a7d2b610"
DATASET_PCM_LOUDNESS_REVISION = "d8f2a6c4b901"
MODEL_REGISTRY_REVISION = "e4c7b9d2f610"
MAINTENANCE_DB_AUTHZ_REVISION = "f5d1c8a9b240"
UNBOUND_STORAGE_NAMESPACE_SHA256 = "0" * 64


def test_model_registry_migration_preserves_historical_attempts_and_downgrades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "model-registry.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, DATASET_PCM_LOUDNESS_REVISION)
    connection = sqlite3.connect(database_path)
    timestamp = "2026-07-12 00:00:00+00:00"
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        """
        INSERT INTO workers (
            id, row_version, name, token_hash, token_issued_at, status,
            capabilities_json, worker_version, rvc_commit_hash, is_active,
            created_at, updated_at
        ) VALUES (?, 1, 'historical-worker', ?, ?, 'idle', '{}', '0.1.0', ?, 1, ?, ?)
        """,
        (
            "00000000-0000-4000-8000-000000000903",
            "a" * 64,
            timestamp,
            "abcdef0",
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        """
        INSERT INTO datasets (
            id, name, storage_uri, is_usable, status, decoder_pending_count,
            retryable, created_at, updated_at
        ) VALUES (?, 'historical-dataset', 'local:///historical', 1,
                  'legacy_imported', 0, 0, ?, ?)
        """,
        ("00000000-0000-4000-8000-000000000904", timestamp, timestamp),
    )
    connection.execute(
        """
        INSERT INTO experiments (
            id, row_version, name, name_conflict_key, dataset_id,
            created_by, created_at, updated_at
        ) VALUES (?, 1, 'historical-experiment', NULL, ?, NULL, ?, ?)
        """,
        (
            "00000000-0000-4000-8000-000000000905",
            "00000000-0000-4000-8000-000000000904",
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        """
        INSERT INTO jobs (
            id, row_version, experiment_id, dataset_id, worker_id, job_name,
            status, config_json, priority, total_epoch, attempt_count,
            created_at, updated_at
        ) VALUES (?, 1, ?, ?, ?, 'historical-job', 'completed', '{}', 5, 1, 1, ?, ?)
        """,
        (
            "00000000-0000-4000-8000-000000000902",
            "00000000-0000-4000-8000-000000000905",
            "00000000-0000-4000-8000-000000000904",
            "00000000-0000-4000-8000-000000000903",
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        """
        INSERT INTO job_attempts (
            id, job_id, worker_id, attempt_number, engine_mode,
            telemetry_log_count, telemetry_metric_count, status,
            started_at, finished_at, runtime_image_digest,
            runtime_asset_manifest_sha256
        ) VALUES (?, ?, ?, 1, 'rvc_webui', NULL, NULL, 'completed', ?, ?, NULL, NULL)
        """,
        (
            "00000000-0000-4000-8000-000000000901",
            "00000000-0000-4000-8000-000000000902",
            "00000000-0000-4000-8000-000000000903",
            timestamp,
            timestamp,
        ),
    )
    connection.commit()
    connection.close()

    command.upgrade(config, MODEL_REGISTRY_REVISION)
    connection = sqlite3.connect(database_path)
    assert {
        "rvc_commit_hash",
        "execution_provenance_version",
    }.issubset(table_columns(connection, "job_attempts"))
    historical = connection.execute(
        """
        SELECT rvc_commit_hash, execution_provenance_version
        FROM job_attempts WHERE id = ?
        """,
        ("00000000-0000-4000-8000-000000000901",),
    ).fetchone()
    assert historical == (None, None)
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "experiment_model_registries",
        "model_registry_entries",
        "model_registry_operations",
    }.issubset(tables)
    entry_sql = table_sql(connection, "model_registry_entries")
    operation_sql = table_sql(connection, "model_registry_operations")
    assert "uq_model_registry_entry_active_slot" in entry_sql
    assert "uq_model_registry_entry_model_artifact" in entry_sql
    assert "uq_model_registry_entry_id_experiment" in entry_sql
    assert "fk_model_registry_operation_entry_experiment" in operation_sql
    assert "idempotency_key_hash_format" in operation_sql
    assert "request_fingerprint_format" in operation_sql
    assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
        MODEL_REGISTRY_REVISION,
    )
    connection.close()

    command.downgrade(config, DATASET_PCM_LOUDNESS_REVISION)
    connection = sqlite3.connect(database_path)
    assert "rvc_commit_hash" not in table_columns(connection, "job_attempts")
    assert "execution_provenance_version" not in table_columns(
        connection, "job_attempts"
    )
    remaining_tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "experiment_model_registries" not in remaining_tables
    assert "model_registry_entries" not in remaining_tables
    assert "model_registry_operations" not in remaining_tables
    preserved = connection.execute(
        "SELECT id FROM job_attempts WHERE id = ?",
        ("00000000-0000-4000-8000-000000000901",),
    ).fetchone()
    assert preserved == ("00000000-0000-4000-8000-000000000901",)
    connection.close()


def alembic_config(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    api_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    monkeypatch.setenv(
        "JWT_SECRET",
        "migration-test-jwt-secret-with-at-least-thirty-two-characters",
    )
    return Config(str(api_root / "alembic.ini"))


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info('{table}')")}


def table_sql(connection: sqlite3.Connection, table: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    assert row is not None and row[0] is not None
    return str(row[0])


def test_auth_migration_preserves_legacy_user_state_and_downgrades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "migration.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, INITIAL_REVISION)

    connection = sqlite3.connect(database_path)
    timestamp = "2026-07-11 00:00:00+00:00"
    connection.executemany(
        """
        INSERT INTO users (
            id, email, password_hash, role, is_active, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                "00000000-0000-4000-8000-000000000001",
                "active@example.test",
                "$argon2id$active-hash",
                "admin",
                1,
                timestamp,
                timestamp,
            ),
            (
                "00000000-0000-4000-8000-000000000002",
                "inactive@example.test",
                "$argon2id$inactive-hash",
                "user",
                0,
                timestamp,
                timestamp,
            ),
        ),
    )
    connection.commit()
    connection.close()

    command.upgrade(config, "head")
    connection = sqlite3.connect(database_path)
    assert "disabled" in table_columns(connection, "users")
    assert "is_active" not in table_columns(connection, "users")
    users = list(
        connection.execute("SELECT email, password_hash, role, disabled FROM users ORDER BY email")
    )
    assert users == [
        ("active@example.test", "$argon2id$active-hash", "admin", 0),
        ("inactive@example.test", "$argon2id$inactive-hash", "user", 1),
    ]
    assert "revoked_access_tokens" in {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "artifact_upload_sessions" in {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    upload_columns = table_columns(connection, "artifact_upload_sessions")
    assert {
        "attempt_id",
        "lease_id",
        "worker_id",
        "artifact_id",
        "idempotency_key",
        "generation",
        "request_fingerprint",
        "dedupe_key",
        "temporary_object_key",
        "canonical_object_key",
        "expected_size_bytes",
        "expected_sha256",
        "status",
        "expires_at",
        "storage_namespace_sha256",
    }.issubset(upload_columns)
    upload_indexes = {
        str(row[1]) for row in connection.execute("PRAGMA index_list('artifact_upload_sessions')")
    }
    assert "ix_artifact_upload_status_expiry" in upload_indexes
    assert "dataset_upload_sessions" in {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    dataset_columns = table_columns(connection, "datasets")
    assert {
        "status",
        "original_filename",
        "original_size_bytes",
        "original_sha256",
        "prepared_flat_size_bytes",
        "prepared_flat_sha256",
        "manifest_storage_uri",
        "manifest_sha256",
        "quality_report_storage_uri",
        "quality_report_sha256",
        "decoder_pending_count",
        "failure_code",
        "retryable",
        "ingestion_started_at",
        "finalized_at",
        "source_file_entry_count",
        "skipped_file_count",
        "rejected_file_count",
        "duplicate_file_count",
        "pcm_quality_algorithm",
        "pcm_validated_file_count",
        "pcm_sample_count",
        "pcm_clipping_ratio",
        "pcm_silence_ratio",
        "pcm_rms_ratio",
        "pcm_silence_threshold_dbfs",
        "pcm_loudness_algorithm",
        "pcm_loudness_analyzed_file_count",
        "pcm_loudness_block_count",
        "pcm_loudness_gated_block_count",
        "pcm_integrated_lufs",
        "pcm_loudness_unavailable_reason",
    }.issubset(dataset_columns)
    dataset_upload_columns = table_columns(connection, "dataset_upload_sessions")
    assert {
        "dataset_id",
        "owner_id",
        "idempotency_key",
        "generation",
        "request_fingerprint",
        "temporary_object_key",
        "original_object_key",
        "prepared_flat_object_key",
        "manifest_object_key",
        "quality_report_object_key",
        "expected_size_bytes",
        "expected_sha256",
        "status",
        "upload_write_token",
        "upload_heartbeat_at",
        "finalization_token",
        "finalization_heartbeat_at",
        "expires_at",
        "cleanup_claim_run_id",
        "cleanup_claimed_at",
        "cleanup_claim_generation",
        "cleanup_first_deleted_at",
        "cleanup_completed_at",
        "storage_namespace_sha256",
    }.issubset(dataset_upload_columns)
    dataset_upload_indexes = {
        str(row[1]) for row in connection.execute("PRAGMA index_list('dataset_upload_sessions')")
    }
    assert "ix_dataset_upload_status_expiry" in dataset_upload_indexes
    assert "ix_dataset_upload_sessions_cleanup_claim_run_id" in dataset_upload_indexes
    assert "maintenance_task_runs" in {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "task_name",
        "job_id",
        "idempotency_key_hash",
        "dry_run",
        "status",
        "attempt_count",
        "max_attempts",
        "result_json",
        "last_error_code",
        "heartbeat_at",
    }.issubset(table_columns(connection, "maintenance_task_runs"))
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "test_sets",
        "test_set_items",
        "test_set_item_upload_sessions",
        "presets",
        "samples",
    }.issubset(tables)
    assert {
        "manifest_storage_uri",
        "manifest_sha256",
        "failure_code",
        "item_count",
    }.issubset(table_columns(connection, "test_sets"))
    assert {
        "test_set_id",
        "owner_id",
        "idempotency_key",
        "generation",
        "request_fingerprint",
        "item_key",
        "sort_order",
        "temporary_object_key",
        "canonical_object_key",
        "storage_namespace_sha256",
        "upload_token_hash",
        "upload_write_token",
        "upload_heartbeat_at",
        "finalization_token",
        "finalization_heartbeat_at",
        "cleanup_claim_run_id",
        "cleanup_claimed_at",
        "cleanup_claim_generation",
        "cleanup_first_deleted_at",
        "cleanup_completed_at",
        "status",
        "expires_at",
    }.issubset(table_columns(connection, "test_set_item_upload_sessions"))
    test_set_upload_indexes = {
        str(row[1])
        for row in connection.execute("PRAGMA index_list('test_set_item_upload_sessions')")
    }
    assert "ix_test_set_item_upload_sessions_cleanup_claim_run_id" in test_set_upload_indexes
    assert "test_set_staging_cleanup" in table_sql(connection, "maintenance_task_runs")
    assert {
        "runtime_image_digest",
        "runtime_asset_manifest_sha256",
    }.issubset(table_columns(connection, "job_attempts"))
    assert {
        "native_inference_manifest_sha256",
        "native_inference_request_sha256",
    }.issubset(table_columns(connection, "samples"))
    assert "uq_sample_artifact" not in table_sql(connection, "samples")
    assert {
        "row_version",
        "token_issued_at",
        "token_rotation_id",
        "pending_token_hash",
        "token_rotation_started_at",
        "token_rotation_expires_at",
    }.issubset(table_columns(connection, "workers"))
    assert "row_version" in table_columns(connection, "jobs")
    worker_indexes = {str(row[1]) for row in connection.execute("PRAGMA index_list('workers')")}
    assert "uq_workers_pending_token_hash" in worker_indexes
    bootstrap_state = connection.execute(
        "SELECT id, admin_user_id, lock_version FROM admin_bootstrap_state"
    ).fetchone()
    assert bootstrap_state == (1, None, 0)
    revoked_indexes = {
        str(row[1]) for row in connection.execute("PRAGMA index_list('revoked_access_tokens')")
    }
    assert "ix_revoked_access_tokens_expires_at" in revoked_indexes
    assert "ix_revoked_access_tokens_user_id" in revoked_indexes
    foreign_keys = list(connection.execute("PRAGMA foreign_key_list('revoked_access_tokens')"))
    assert any(row[2] == "users" and row[3] == "user_id" for row in foreign_keys)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO users (
                id, email, password_hash, role, disabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "00000000-0000-4000-8000-000000000003",
                "invalid-role@example.test",
                "hash",
                "superuser",
                0,
                timestamp,
                timestamp,
            ),
        )
    connection.rollback()
    connection.close()

    command.downgrade(config, INITIAL_REVISION)
    connection = sqlite3.connect(database_path)
    assert "is_active" in table_columns(connection, "users")
    assert "disabled" not in table_columns(connection, "users")
    legacy_states = list(connection.execute("SELECT email, is_active FROM users ORDER BY email"))
    assert legacy_states == [
        ("active@example.test", 1),
        ("inactive@example.test", 0),
    ]
    assert "revoked_access_tokens" not in {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "admin_bootstrap_state" not in {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "artifact_upload_sessions" not in {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "dataset_upload_sessions" not in {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    downgraded_tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "test_sets",
        "test_set_items",
        "test_set_item_upload_sessions",
        "presets",
        "samples",
    }.isdisjoint(downgraded_tables)
    assert {
        "test_set_id",
        "preset_id",
        "sample_plan_json",
        "sample_plan_sha256",
    }.isdisjoint(table_columns(connection, "jobs"))
    connection.close()

    command.upgrade(config, AUTH_REVISION)
    command.upgrade(config, ARTIFACT_UPLOAD_REVISION)
    command.upgrade(config, DATASET_UPLOAD_REVISION)
    command.upgrade(config, MAINTENANCE_REVISION)


def test_test_set_migration_renders_postgresql_offline_sql(
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
        "migration-test-jwt-secret-with-at-least-thirty-two-characters",
    )
    config = Config(str(api_root / "alembic.ini"))

    command.upgrade(config, TEST_SET_REVISION, sql=True)

    rendered = capsys.readouterr().out
    assert "CREATE TABLE test_sets" in rendered
    assert "manifest_storage_uri VARCHAR(2048)" in rendered
    assert "CREATE TABLE test_set_item_upload_sessions" in rendered
    assert "uq_test_set_item_upload_idempotency_generation" in rendered
    assert "fk_sample_attempt_job" in rendered
    assert "fk_sample_job_test_set" in rendered
    assert "fk_sample_item_test_set" in rendered
    assert "fk_sample_artifact_job_attempt" in rendered
    assert "CREATE TABLE samples" in rendered
    assert "TIMESTAMP WITH TIME ZONE" in rendered

    command.downgrade(
        config,
        f"{TEST_SET_REVISION}:{MAINTENANCE_REVISION}",
        sql=True,
    )
    downgraded = capsys.readouterr().out
    assert "DROP TABLE samples" in downgraded
    assert "DROP TABLE test_set_item_upload_sessions" in downgraded
    assert "DROP TABLE test_set_items" in downgraded
    assert "DROP TABLE presets" in downgraded
    assert "DROP TABLE test_sets" in downgraded


def test_sample_runtime_migration_upgrades_and_downgrades_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "sample-runtime.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, STORAGE_NAMESPACE_REVISION)
    connection = sqlite3.connect(database_path)
    assert "uq_sample_artifact" in table_sql(connection, "samples")
    connection.close()

    command.upgrade(config, SAMPLE_RUNTIME_REVISION)
    connection = sqlite3.connect(database_path)
    assert {
        "runtime_image_digest",
        "runtime_asset_manifest_sha256",
    }.issubset(table_columns(connection, "job_attempts"))
    assert {
        "native_inference_manifest_sha256",
        "native_inference_request_sha256",
    }.issubset(table_columns(connection, "samples"))
    assert "uq_sample_artifact" not in table_sql(connection, "samples")
    connection.close()

    command.downgrade(config, STORAGE_NAMESPACE_REVISION)
    connection = sqlite3.connect(database_path)
    assert "uq_sample_artifact" in table_sql(connection, "samples")
    assert "runtime_image_digest" not in table_columns(connection, "job_attempts")
    assert "runtime_asset_manifest_sha256" not in table_columns(connection, "job_attempts")
    assert "native_inference_manifest_sha256" not in table_columns(connection, "samples")
    assert "native_inference_request_sha256" not in table_columns(connection, "samples")
    connection.close()


def test_sample_runtime_migration_renders_postgresql_offline_sql(
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
        "migration-test-jwt-secret-with-at-least-thirty-two-characters",
    )
    config = Config(str(api_root / "alembic.ini"))

    command.upgrade(config, SAMPLE_RUNTIME_REVISION, sql=True)
    rendered = capsys.readouterr().out
    assert "runtime_image_digest VARCHAR(71)" in rendered
    assert "runtime_asset_manifest_sha256 VARCHAR(64)" in rendered
    assert "native_inference_manifest_sha256 VARCHAR(64)" in rendered
    assert "native_inference_request_sha256 VARCHAR(64)" in rendered
    assert "DROP CONSTRAINT uq_sample_artifact" in rendered


def test_worker_token_rotation_migration_backfills_and_downgrades_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "worker-token-rotation.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, SAMPLE_RUNTIME_REVISION)
    connection = sqlite3.connect(database_path)
    timestamp = "2026-07-11 00:00:00+00:00"
    connection.execute(
        """
        INSERT INTO workers (
            id, name, token_hash, status, capabilities_json, worker_version,
            rvc_commit_hash, last_heartbeat_at, current_job_id, is_active,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, 1, ?, ?)
        """,
        (
            "00000000-0000-4000-8000-000000000201",
            "migration-worker",
            "a" * 64,
            "idle",
            "{}",
            "migration-test",
            "0123456789abcdef",
            timestamp,
            timestamp,
        ),
    )
    connection.commit()
    connection.close()

    command.upgrade(config, WORKER_TOKEN_ROTATION_REVISION)
    connection = sqlite3.connect(database_path)
    columns = table_columns(connection, "workers")
    assert {
        "row_version",
        "token_issued_at",
        "token_rotation_id",
        "pending_token_hash",
        "token_rotation_started_at",
        "token_rotation_expires_at",
    }.issubset(columns)
    assert "row_version" in table_columns(connection, "jobs")
    backfilled = connection.execute(
        "SELECT token_issued_at FROM workers WHERE name = 'migration-worker'"
    ).fetchone()
    assert backfilled is not None and str(backfilled[0]).startswith("2026-07-11 00:00:00")
    indexes = {str(row[1]) for row in connection.execute("PRAGMA index_list('workers')")}
    assert "uq_workers_pending_token_hash" in indexes
    assert "token_rotation_fields_together" in table_sql(connection, "workers")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE workers SET token_rotation_id = ? WHERE name = 'migration-worker'",
            ("12345678-1234-4123-8123-123456789abc",),
        )
    connection.rollback()
    connection.close()

    command.downgrade(config, SAMPLE_RUNTIME_REVISION)
    connection = sqlite3.connect(database_path)
    assert {
        "row_version",
        "token_issued_at",
        "token_rotation_id",
        "pending_token_hash",
        "token_rotation_started_at",
        "token_rotation_expires_at",
    }.isdisjoint(table_columns(connection, "workers"))
    assert "row_version" not in table_columns(connection, "jobs")
    connection.close()


def test_worker_token_rotation_migration_renders_postgresql_offline_sql(
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
        "migration-test-jwt-secret-with-at-least-thirty-two-characters",
    )
    config = Config(str(api_root / "alembic.ini"))

    command.upgrade(config, WORKER_TOKEN_ROTATION_REVISION, sql=True)
    rendered = capsys.readouterr().out
    assert "token_issued_at TIMESTAMP WITH TIME ZONE" in rendered
    assert "row_version INTEGER" in rendered
    assert "pending_token_hash VARCHAR(64)" in rendered
    assert "token_rotation_fields_together" in rendered
    assert "uq_workers_pending_token_hash" in rendered


def test_test_set_upload_fencing_migration_renders_postgresql_offline_sql(
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
        "migration-test-jwt-secret-with-at-least-thirty-two-characters",
    )
    config = Config(str(api_root / "alembic.ini"))

    command.upgrade(config, TEST_SET_UPLOAD_FENCING_REVISION, sql=True)
    rendered = capsys.readouterr().out
    assert "upload_write_token VARCHAR(36)" in rendered
    assert "finalization_heartbeat_at TIMESTAMP WITH TIME ZONE" in rendered
    assert "cleanup_claim_generation INTEGER" in rendered
    assert "cleanup_first_deleted_at TIMESTAMP WITH TIME ZONE" in rendered
    assert "test_set_staging_cleanup" in rendered


def test_dataset_upload_fencing_migration_renders_postgresql_offline_sql(
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
        "migration-test-jwt-secret-with-at-least-thirty-two-characters",
    )
    config = Config(str(api_root / "alembic.ini"))

    command.upgrade(config, DATASET_UPLOAD_FENCING_REVISION, sql=True)
    rendered = capsys.readouterr().out
    assert "upload_write_token VARCHAR(36)" in rendered
    assert "upload_heartbeat_at TIMESTAMP WITH TIME ZONE" in rendered
    assert "finalization_heartbeat_at TIMESTAMP WITH TIME ZONE" in rendered
    assert "cleanup_claim_generation INTEGER" in rendered
    assert "cleanup_first_deleted_at TIMESTAMP WITH TIME ZONE" in rendered
    assert "upload_fencing_upgrade_required" in rendered
    assert "UPDATE datasets SET status=" in rendered
    assert "UPDATE dataset_upload_sessions SET status=" in rendered


def test_dataset_upload_fencing_sqlite_expires_legacy_active_rows_without_quota_trap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "dataset-upload-fencing.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, "d1e7a9c4f620")

    timestamp = "2026-07-11 00:00:00+00:00"
    connection = sqlite3.connect(database_path)
    rows = (
        (
            "00000000-0000-4000-8000-000000000201",
            "00000000-0000-4000-8000-000000000211",
            "pending",
            None,
        ),
        (
            "00000000-0000-4000-8000-000000000202",
            "00000000-0000-4000-8000-000000000212",
            "finalizing",
            "00000000-0000-4000-8000-000000000299",
        ),
        (
            "00000000-0000-4000-8000-000000000203",
            "00000000-0000-4000-8000-000000000213",
            "completed",
            None,
        ),
    )
    for index, (dataset_id, upload_id, status, finalization_token) in enumerate(rows, 1):
        connection.execute(
            """
            INSERT INTO datasets (
                id, name, storage_uri, flat_storage_uri, is_usable, status,
                decoder_pending_count, retryable, created_at, updated_at
            ) VALUES (?, ?, ?, NULL, 1, 'processing', 0, 0, ?, ?)
            """,
            (
                dataset_id,
                f"legacy-active-{index}",
                f"local:///datasets/verified/{dataset_id}/original.wav",
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO dataset_upload_sessions (
                id, dataset_id, owner_id, idempotency_key, generation,
                request_fingerprint, filename, content_type,
                expected_size_bytes, expected_sha256, temporary_object_key,
                original_object_key, prepared_flat_object_key,
                manifest_object_key, quality_report_object_key,
                storage_backend, storage_namespace_sha256, status,
                finalization_token, expires_at, created_at, updated_at
            ) VALUES (?, ?, NULL, ?, 1, ?, 'legacy.wav', 'audio/wav',
                      4, ?, ?, ?, ?, ?, ?, 'local', ?, ?, ?, ?, ?, ?)
            """,
            (
                upload_id,
                dataset_id,
                f"legacy-fencing-{index}",
                str(index) * 64,
                str(index + 3) * 64,
                f"datasets/staging/{dataset_id}/{upload_id}",
                f"datasets/verified/{dataset_id}/original.wav",
                f"datasets/verified/{dataset_id}/prepared_flat.zip",
                f"datasets/verified/{dataset_id}/manifest.json",
                f"datasets/verified/{dataset_id}/quality_report.json",
                "a" * 64,
                status,
                finalization_token,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
    connection.commit()
    connection.close()

    command.upgrade(config, DATASET_UPLOAD_FENCING_REVISION)
    connection = sqlite3.connect(database_path)
    migrated = list(
        connection.execute(
            """
            SELECT status, finalization_token, failure_code
            FROM dataset_upload_sessions ORDER BY id
            """
        )
    )
    assert migrated == [
        ("expired", None, "upload_fencing_upgrade_required"),
        ("expired", None, "upload_fencing_upgrade_required"),
        ("completed", None, None),
    ]
    datasets = list(
        connection.execute(
            "SELECT status, is_usable, failure_code, retryable FROM datasets ORDER BY id"
        )
    )
    assert datasets == [
        ("upload_pending", 0, "upload_fencing_upgrade_required", 1),
        ("upload_pending", 0, "upload_fencing_upgrade_required", 1),
        ("processing", 1, None, 0),
    ]
    active_count = connection.execute(
        """
        SELECT count(*) FROM dataset_upload_sessions
        WHERE status IN ('pending', 'finalizing')
        """
    ).fetchone()
    assert active_count == (0,)
    assert {
        "upload_write_token",
        "upload_heartbeat_at",
        "finalization_heartbeat_at",
        "cleanup_claim_generation",
        "cleanup_first_deleted_at",
    }.issubset(table_columns(connection, "dataset_upload_sessions"))
    connection.close()

    command.downgrade(config, "d1e7a9c4f620")
    connection = sqlite3.connect(database_path)
    assert {
        "upload_write_token",
        "upload_heartbeat_at",
        "finalization_heartbeat_at",
        "cleanup_claim_generation",
        "cleanup_first_deleted_at",
    }.isdisjoint(table_columns(connection, "dataset_upload_sessions"))
    assert connection.execute(
        "SELECT status FROM dataset_upload_sessions WHERE id = ?",
        ("00000000-0000-4000-8000-000000000211",),
    ).fetchone() == ("expired",)
    connection.close()


def test_dataset_pcm_quality_migration_preserves_historical_null_and_enforces_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "dataset-pcm-quality.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, DATASET_UPLOAD_FENCING_REVISION)

    dataset_id = "00000000-0000-4000-8000-000000000301"
    timestamp = "2026-07-12 00:00:00+00:00"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        INSERT INTO datasets (
            id, name, storage_uri, is_usable, status,
            decoder_pending_count, retryable, created_at, updated_at
        ) VALUES (?, 'historical', 'local:///historical.wav', 0,
                  'legacy_imported', 0, 0, ?, ?)
        """,
        (dataset_id, timestamp, timestamp),
    )
    connection.commit()
    connection.close()

    command.upgrade(config, DATASET_PCM_QUALITY_REVISION)
    connection = sqlite3.connect(database_path)
    aggregate_columns = {
        "source_file_entry_count",
        "skipped_file_count",
        "rejected_file_count",
        "duplicate_file_count",
        "pcm_quality_algorithm",
        "pcm_validated_file_count",
        "pcm_sample_count",
        "pcm_clipping_ratio",
        "pcm_silence_ratio",
        "pcm_rms_ratio",
        "pcm_silence_threshold_dbfs",
    }
    assert aggregate_columns.issubset(table_columns(connection, "datasets"))
    historical = connection.execute(
        """
        SELECT pcm_quality_algorithm, pcm_validated_file_count, pcm_sample_count,
               pcm_clipping_ratio, pcm_silence_ratio, pcm_rms_ratio,
               pcm_silence_threshold_dbfs
        FROM datasets WHERE id = ?
        """,
        (dataset_id,),
    ).fetchone()
    assert historical == (None, None, None, None, None, None, None)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE datasets SET pcm_quality_algorithm = 'pcm-sample-weighted-v1' WHERE id = ?",
            (dataset_id,),
        )
    connection.rollback()

    connection.execute(
        """
        UPDATE datasets
        SET file_count = 1,
            pcm_quality_algorithm = 'pcm-sample-weighted-v1',
            pcm_validated_file_count = 1,
            pcm_sample_count = 4,
            pcm_clipping_ratio = 0.0,
            pcm_silence_ratio = 0.5,
            pcm_rms_ratio = 0.25,
            pcm_silence_threshold_dbfs = -50.0
        WHERE id = ?
        """,
        (dataset_id,),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE datasets SET pcm_rms_ratio = 1.01 WHERE id = ?",
            (dataset_id,),
        )
    connection.rollback()
    connection.close()

    command.downgrade(config, DATASET_UPLOAD_FENCING_REVISION)
    connection = sqlite3.connect(database_path)
    assert aggregate_columns.isdisjoint(table_columns(connection, "datasets"))
    connection.close()


def test_dataset_pcm_quality_migration_renders_postgresql_offline_sql(
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
        "migration-test-jwt-secret-with-at-least-thirty-two-characters",
    )
    config = Config(str(api_root / "alembic.ini"))

    command.upgrade(config, DATASET_PCM_QUALITY_REVISION, sql=True)
    rendered = capsys.readouterr().out
    assert "ADD COLUMN pcm_sample_count BIGINT" in rendered
    assert "ADD COLUMN pcm_clipping_ratio FLOAT" in rendered
    assert "ck_datasets_pcm_quality_complete_and_bounded" in rendered
    assert "pcm-sample-weighted-v1" in rendered


def test_dataset_pcm_loudness_migration_preserves_historical_null_and_enforces_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "dataset-pcm-loudness.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, "ca8d3e7f4b10")

    dataset_id = "00000000-0000-4000-8000-000000000302"
    timestamp = "2026-07-12 00:00:00+00:00"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        INSERT INTO datasets (
            id, name, storage_uri, duration_sec, file_count,
            pcm_quality_algorithm, pcm_validated_file_count, pcm_sample_count,
            pcm_clipping_ratio, pcm_silence_ratio, pcm_rms_ratio,
            pcm_silence_threshold_dbfs, is_usable, status,
            decoder_pending_count, retryable, created_at, updated_at
        ) VALUES (?, 'historical-pcm', 'local:///historical.wav', 1.0, 1,
                  'pcm-sample-weighted-v1', 1, 48000, 0.0, 0.1, 0.2, -50.0,
                  1, 'ready', 0, 0, ?, ?)
        """,
        (dataset_id, timestamp, timestamp),
    )
    connection.commit()
    connection.close()

    command.upgrade(config, DATASET_PCM_LOUDNESS_REVISION)
    connection = sqlite3.connect(database_path)
    loudness_columns = {
        "pcm_loudness_algorithm",
        "pcm_loudness_analyzed_file_count",
        "pcm_loudness_block_count",
        "pcm_loudness_gated_block_count",
        "pcm_integrated_lufs",
        "pcm_loudness_unavailable_reason",
    }
    assert loudness_columns.issubset(table_columns(connection, "datasets"))
    assert connection.execute(
        """
        SELECT pcm_loudness_algorithm, pcm_loudness_analyzed_file_count,
               pcm_loudness_block_count, pcm_loudness_gated_block_count,
               pcm_integrated_lufs, pcm_loudness_unavailable_reason
        FROM datasets WHERE id = ?
        """,
        (dataset_id,),
    ).fetchone() == (None, None, None, None, None, None)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE datasets SET pcm_loudness_algorithm = "
            "'itu-r-bs1770-4-mono-stereo-v1' WHERE id = ?",
            (dataset_id,),
        )
    connection.rollback()

    connection.execute(
        """
        UPDATE datasets
        SET pcm_loudness_algorithm = 'itu-r-bs1770-4-mono-stereo-v1',
            pcm_loudness_analyzed_file_count = 1,
            pcm_loudness_block_count = 7,
            pcm_loudness_gated_block_count = 7,
            pcm_integrated_lufs = -23.0,
            pcm_loudness_unavailable_reason = NULL
        WHERE id = ?
        """,
        (dataset_id,),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE datasets SET pcm_loudness_gated_block_count = 8 WHERE id = ?",
            (dataset_id,),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE datasets SET pcm_integrated_lufs = NULL WHERE id = ?",
            (dataset_id,),
        )
    connection.rollback()
    connection.close()

    command.downgrade(config, "ca8d3e7f4b10")
    connection = sqlite3.connect(database_path)
    assert loudness_columns.isdisjoint(table_columns(connection, "datasets"))
    connection.close()


def test_dataset_pcm_loudness_migration_renders_postgresql_offline_sql(
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
        "migration-test-jwt-secret-with-at-least-thirty-two-characters",
    )
    config = Config(str(api_root / "alembic.ini"))

    command.upgrade(config, DATASET_PCM_LOUDNESS_REVISION, sql=True)
    rendered = capsys.readouterr().out
    assert "ADD COLUMN pcm_integrated_lufs FLOAT" in rendered
    assert "ck_datasets_pcm_loudness_complete_and_bounded" in rendered
    assert "itu-r-bs1770-4-mono-stereo-v1" in rendered


def test_test_set_sqlite_downgrade_removes_ledger_and_job_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "test-set-downgrade.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, TEST_SET_REVISION)
    connection = sqlite3.connect(database_path)
    assert "samples" in {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "test_set_id",
        "preset_id",
        "sample_plan_json",
        "sample_plan_sha256",
    }.issubset(table_columns(connection, "jobs"))
    connection.close()

    command.downgrade(config, MAINTENANCE_REVISION)
    connection = sqlite3.connect(database_path)
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "test_sets",
        "test_set_items",
        "test_set_item_upload_sessions",
        "presets",
        "samples",
    }.isdisjoint(tables)
    assert {
        "test_set_id",
        "preset_id",
        "sample_plan_json",
        "sample_plan_sha256",
    }.isdisjoint(table_columns(connection, "jobs"))
    job_attempt_indexes = {
        str(row[1]) for row in connection.execute("PRAGMA index_list('job_attempts')")
    }
    artifact_indexes = {str(row[1]) for row in connection.execute("PRAGMA index_list('artifacts')")}
    assert "uq_job_attempt_id_job" not in job_attempt_indexes
    assert "uq_artifact_id_job_attempt" not in artifact_indexes
    connection.close()


def test_dataset_upload_migration_renders_postgresql_offline_sql(
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
        "migration-test-jwt-secret-with-at-least-thirty-two-characters",
    )
    config = Config(str(api_root / "alembic.ini"))

    command.upgrade(config, DATASET_UPLOAD_REVISION, sql=True)

    rendered = capsys.readouterr().out
    assert "CREATE TABLE dataset_upload_sessions" in rendered
    assert "finalization_token VARCHAR(36)" in rendered
    assert "TIMESTAMP WITH TIME ZONE" in rendered
    assert "ix_dataset_upload_status_expiry" in rendered
    assert "uq_dataset_upload_owner_idempotency_generation" in rendered


def test_maintenance_migration_renders_postgresql_offline_sql(
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
        "migration-test-jwt-secret-with-at-least-thirty-two-characters",
    )
    config = Config(str(api_root / "alembic.ini"))

    command.upgrade(config, MAINTENANCE_REVISION, sql=True)

    rendered = capsys.readouterr().out
    assert "CREATE TABLE maintenance_task_runs" in rendered
    assert "dataset_staging_cleanup" in rendered
    assert "cleanup_claim_run_id VARCHAR(36)" in rendered
    assert "ix_maintenance_task_run_status_created" in rendered


def test_maintenance_db_authz_migration_renders_secure_postgresql_functions(
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
        "migration-test-jwt-secret-with-at-least-thirty-two-characters",
    )
    config = Config(str(api_root / "alembic.ini"))

    command.upgrade(
        config,
        f"{MODEL_REGISTRY_REVISION}:{MAINTENANCE_DB_AUTHZ_REVISION}",
        sql=True,
    )
    rendered = capsys.readouterr().out
    assert "CREATE FUNCTION public.rvc_maintenance_lock_dataset_parent" in rendered
    assert "CREATE FUNCTION public.rvc_maintenance_lock_test_set_parent" in rendered
    assert rendered.count("SECURITY DEFINER") == 2
    assert rendered.count("SET search_path = pg_catalog, pg_temp") == 2
    assert rendered.count("FROM public.dataset_upload_sessions AS upload") == 2
    assert "FROM public.datasets AS dataset" in rendered
    assert rendered.count("FROM public.test_set_item_upload_sessions AS upload") == 2
    assert "FROM public.test_sets AS test_set" in rendered
    assert rendered.count("REVOKE ALL ON FUNCTION") == 2
    assert "FROM PUBLIC" in rendered
    assert "EXECUTE " not in rendered

    command.downgrade(
        config,
        f"{MAINTENANCE_DB_AUTHZ_REVISION}:{MODEL_REGISTRY_REVISION}",
        sql=True,
    )
    downgraded = capsys.readouterr().out
    assert "DROP FUNCTION public.rvc_maintenance_lock_test_set_parent(text)" in downgraded
    assert "DROP FUNCTION public.rvc_maintenance_lock_dataset_parent(text)" in downgraded


def test_storage_namespace_migration_marks_history_unbound_and_downgrades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "storage-namespace.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, TEST_SET_REVISION)

    connection = sqlite3.connect(database_path)
    timestamp = "2026-07-11 00:00:00+00:00"
    connection.execute(
        """
        INSERT INTO datasets (
            id, name, storage_uri, flat_storage_uri, is_usable,
            created_at, updated_at
        ) VALUES (?, ?, ?, NULL, 0, ?, ?)
        """,
        (
            "00000000-0000-4000-8000-000000000102",
            "legacy namespace dataset",
            "local:///legacy/original",
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        """
        INSERT INTO dataset_upload_sessions (
            id, dataset_id, owner_id, idempotency_key, generation,
            request_fingerprint, filename, content_type, expected_size_bytes,
            expected_sha256, temporary_object_key, original_object_key,
            prepared_flat_object_key, manifest_object_key,
            quality_report_object_key, storage_backend, status, expires_at,
            created_at, updated_at
        ) VALUES (?, ?, NULL, ?, 1, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "00000000-0000-4000-8000-000000000101",
            "00000000-0000-4000-8000-000000000102",
            "legacy-dataset-upload",
            "1" * 64,
            "voice.wav",
            "audio/wav",
            "2" * 64,
            "staging/dataset",
            "verified/original",
            "verified/flat",
            "verified/manifest",
            "verified/report",
            "local",
            "expired",
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    connection.commit()
    connection.close()

    command.upgrade(config, STORAGE_NAMESPACE_REVISION)
    connection = sqlite3.connect(database_path)
    for table in ("dataset_upload_sessions", "artifact_upload_sessions"):
        columns = list(connection.execute(f"PRAGMA table_info('{table}')"))
        namespace_column = next(row for row in columns if row[1] == "storage_namespace_sha256")
        assert namespace_column[3] == 1
        assert namespace_column[4] is None
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        assert table_sql is not None
        assert "length(storage_namespace_sha256) = 64" in table_sql[0]
    stored = connection.execute(
        "SELECT storage_namespace_sha256 FROM dataset_upload_sessions"
    ).fetchone()
    assert stored == (UNBOUND_STORAGE_NAMESPACE_SHA256,)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE dataset_upload_sessions SET storage_namespace_sha256 = 'short'")
    connection.rollback()
    connection.close()

    command.downgrade(config, TEST_SET_REVISION)
    connection = sqlite3.connect(database_path)
    assert "storage_namespace_sha256" not in table_columns(connection, "dataset_upload_sessions")
    assert "storage_namespace_sha256" not in table_columns(connection, "artifact_upload_sessions")
    connection.close()


def test_storage_namespace_migration_renders_postgresql_offline_sql(
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
        "migration-test-jwt-secret-with-at-least-thirty-two-characters",
    )
    config = Config(str(api_root / "alembic.ini"))

    command.upgrade(config, STORAGE_NAMESPACE_REVISION, sql=True)

    rendered = capsys.readouterr().out
    assert "ADD COLUMN storage_namespace_sha256 VARCHAR(64)" in rendered
    assert UNBOUND_STORAGE_NAMESPACE_SHA256 in rendered
    assert "ck_dataset_upload_sessions_storage_namespace_sha256_length" in rendered
    assert "ck_artifact_upload_sessions_storage_namespace_sha256_length" in rendered
    assert "ALTER COLUMN storage_namespace_sha256 DROP DEFAULT" in rendered

    command.downgrade(
        config,
        f"{STORAGE_NAMESPACE_REVISION}:{TEST_SET_REVISION}",
        sql=True,
    )
    downgraded = capsys.readouterr().out
    assert "DROP COLUMN storage_namespace_sha256" in downgraded
