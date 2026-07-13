"""bind canonical JobConfig hashes to Jobs and attempts

Revision ID: a7c4e9f2b610
Revises: f5d1c8a9b240
Create Date: 2026-07-13 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c4e9f2b610"
down_revision: str | Sequence[str] | None = "f5d1c8a9b240"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    if sqlite:
        # SQLite must rebuild job_attempts to add a composite FK. Disable FK
        # actions before any DDL so dropping the old table cannot cascade into
        # historical leases/artifacts/telemetry that reference an attempt.
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    # Keep historical rows NULL. Their exact normalized creation-time bytes
    # cannot be reconstructed safely, so new claim/retry paths reject them.
    op.add_column(
        "jobs",
        sa.Column(
            "config_sha256",
            sa.String(length=64),
            sa.CheckConstraint(
                "config_sha256 IS NULL OR "
                "(length(config_sha256) = 64 AND config_sha256 = lower(config_sha256))",
                name=op.f("ck_jobs_config_sha256_shape"),
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "job_attempts",
        sa.Column(
            "job_config_sha256",
            sa.String(length=64),
            sa.CheckConstraint(
                "job_config_sha256 IS NULL OR "
                "(length(job_config_sha256) = 64 "
                "AND job_config_sha256 = lower(job_config_sha256))",
                name=op.f("ck_job_attempts_job_config_sha256_shape"),
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "artifact_upload_sessions",
        sa.Column("finalization_token", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "artifact_upload_sessions",
        sa.Column("upload_write_token", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "artifact_upload_sessions",
        sa.Column("upload_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "artifact_upload_sessions",
        sa.Column("cleanup_token", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "artifact_upload_sessions",
        sa.Column("cleanup_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "artifact_upload_sessions",
        sa.Column("staging_cleanup_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "artifact_upload_sessions",
        sa.Column("canonical_cleanup_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "artifact_upload_sessions",
        sa.Column("staging_cleanup_first_deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "artifact_upload_sessions",
        sa.Column("canonical_cleanup_first_deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_job_id_config_sha256",
        "jobs",
        ["id", "config_sha256"],
        unique=True,
    )
    if sqlite:
        with op.batch_alter_table("job_attempts", recreate="always") as batch_op:
            batch_op.create_foreign_key(
                "fk_job_attempt_job_config_snapshot",
                "jobs",
                ["job_id", "job_config_sha256"],
                ["id", "config_sha256"],
                ondelete="CASCADE",
            )
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")
    else:
        op.create_foreign_key(
            "fk_job_attempt_job_config_snapshot",
            "job_attempts",
            "jobs",
            ["job_id", "job_config_sha256"],
            ["id", "config_sha256"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    if sqlite:
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
        with op.batch_alter_table("job_attempts", recreate="always") as batch_op:
            batch_op.drop_constraint(
                "fk_job_attempt_job_config_snapshot",
                type_="foreignkey",
            )
            batch_op.drop_constraint(
                op.f("ck_job_attempts_job_config_sha256_shape"),
                type_="check",
            )
            batch_op.drop_column("job_config_sha256")
        op.drop_index("uq_job_id_config_sha256", table_name="jobs")
        with op.batch_alter_table("jobs", recreate="always") as batch_op:
            batch_op.drop_constraint(
                op.f("ck_jobs_config_sha256_shape"),
                type_="check",
            )
            batch_op.drop_column("config_sha256")
        op.drop_column("artifact_upload_sessions", "finalization_token")
        op.drop_column("artifact_upload_sessions", "canonical_cleanup_completed_at")
        op.drop_column("artifact_upload_sessions", "canonical_cleanup_first_deleted_at")
        op.drop_column("artifact_upload_sessions", "staging_cleanup_completed_at")
        op.drop_column("artifact_upload_sessions", "staging_cleanup_first_deleted_at")
        op.drop_column("artifact_upload_sessions", "cleanup_heartbeat_at")
        op.drop_column("artifact_upload_sessions", "cleanup_token")
        op.drop_column("artifact_upload_sessions", "upload_heartbeat_at")
        op.drop_column("artifact_upload_sessions", "upload_write_token")
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")
    else:
        op.drop_constraint(
            "fk_job_attempt_job_config_snapshot",
            "job_attempts",
            type_="foreignkey",
        )
        op.drop_index("uq_job_id_config_sha256", table_name="jobs")
        op.drop_column("job_attempts", "job_config_sha256")
        op.drop_column("jobs", "config_sha256")
        op.drop_column("artifact_upload_sessions", "finalization_token")
        op.drop_column("artifact_upload_sessions", "canonical_cleanup_completed_at")
        op.drop_column("artifact_upload_sessions", "canonical_cleanup_first_deleted_at")
        op.drop_column("artifact_upload_sessions", "staging_cleanup_completed_at")
        op.drop_column("artifact_upload_sessions", "staging_cleanup_first_deleted_at")
        op.drop_column("artifact_upload_sessions", "cleanup_heartbeat_at")
        op.drop_column("artifact_upload_sessions", "cleanup_token")
        op.drop_column("artifact_upload_sessions", "upload_heartbeat_at")
        op.drop_column("artifact_upload_sessions", "upload_write_token")
