"""add verified artifact upload sessions

Revision ID: a4f8c2d9137e
Revises: 7e9f4a2c1b6d
Create Date: 2026-07-11 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a4f8c2d9137e"
down_revision: str | Sequence[str] | None = "7e9f4a2c1b6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifact_upload_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("lease_id", sa.String(length=36), nullable=False),
        sa.Column("worker_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_id", sa.String(length=36), nullable=True),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("expected_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "metadata_json",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()),
                "postgresql",
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=True),
        sa.Column("temporary_object_key", sa.String(length=512), nullable=False),
        sa.Column("canonical_object_key", sa.String(length=512), nullable=False),
        sa.Column("storage_backend", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("upload_token_hash", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'finalizing', 'completed', 'failed', 'expired')",
            name=op.f("ck_artifact_upload_sessions_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.id"],
            name=op.f("fk_artifact_upload_sessions_artifact_id_artifacts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["job_attempts.id"],
            name=op.f("fk_artifact_upload_sessions_attempt_id_job_attempts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_artifact_upload_sessions_job_id_jobs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lease_id"],
            ["job_leases.id"],
            name=op.f("fk_artifact_upload_sessions_lease_id_job_leases"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["worker_id"],
            ["workers.id"],
            name=op.f("fk_artifact_upload_sessions_worker_id_workers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifact_upload_sessions")),
        sa.UniqueConstraint(
            "artifact_id",
            name="uq_artifact_upload_artifact_id",
        ),
        sa.UniqueConstraint(
            "attempt_id",
            "idempotency_key",
            "generation",
            name="uq_artifact_upload_attempt_idempotency_generation",
        ),
        sa.UniqueConstraint(
            "canonical_object_key",
            name=op.f("uq_artifact_upload_sessions_canonical_object_key"),
        ),
        sa.UniqueConstraint("dedupe_key", name="uq_artifact_upload_dedupe_key"),
        sa.UniqueConstraint(
            "temporary_object_key",
            name=op.f("uq_artifact_upload_sessions_temporary_object_key"),
        ),
    )
    op.create_index(
        op.f("ix_artifact_upload_sessions_attempt_id"),
        "artifact_upload_sessions",
        ["attempt_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_artifact_upload_sessions_expires_at"),
        "artifact_upload_sessions",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_artifact_upload_sessions_job_id"),
        "artifact_upload_sessions",
        ["job_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_artifact_upload_sessions_lease_id"),
        "artifact_upload_sessions",
        ["lease_id"],
        unique=False,
    )
    op.create_index(
        "ix_artifact_upload_status_expiry",
        "artifact_upload_sessions",
        ["status", "expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_artifact_upload_sessions_worker_id"),
        "artifact_upload_sessions",
        ["worker_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_artifact_upload_sessions_worker_id"),
        table_name="artifact_upload_sessions",
    )
    op.drop_index(
        "ix_artifact_upload_status_expiry",
        table_name="artifact_upload_sessions",
    )
    op.drop_index(
        op.f("ix_artifact_upload_sessions_lease_id"),
        table_name="artifact_upload_sessions",
    )
    op.drop_index(
        op.f("ix_artifact_upload_sessions_job_id"),
        table_name="artifact_upload_sessions",
    )
    op.drop_index(
        op.f("ix_artifact_upload_sessions_expires_at"),
        table_name="artifact_upload_sessions",
    )
    op.drop_index(
        op.f("ix_artifact_upload_sessions_attempt_id"),
        table_name="artifact_upload_sessions",
    )
    op.drop_table("artifact_upload_sessions")
