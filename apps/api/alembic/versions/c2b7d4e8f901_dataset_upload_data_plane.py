"""add dataset upload sessions and verified ingestion state

Revision ID: c2b7d4e8f901
Revises: a4f8c2d9137e
Create Date: 2026-07-11 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2b7d4e8f901"
down_revision: str | Sequence[str] | None = "a4f8c2d9137e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "datasets",
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="legacy_imported",
        ),
    )
    op.add_column("datasets", sa.Column("original_filename", sa.String(length=255)))
    op.add_column("datasets", sa.Column("original_size_bytes", sa.BigInteger()))
    op.add_column("datasets", sa.Column("original_sha256", sa.String(length=64)))
    op.add_column("datasets", sa.Column("original_mime_type", sa.String(length=255)))
    op.add_column("datasets", sa.Column("prepared_flat_size_bytes", sa.BigInteger()))
    op.add_column("datasets", sa.Column("prepared_flat_sha256", sa.String(length=64)))
    op.add_column("datasets", sa.Column("manifest_storage_uri", sa.String(length=2048)))
    op.add_column("datasets", sa.Column("manifest_sha256", sa.String(length=64)))
    op.add_column(
        "datasets",
        sa.Column("quality_report_storage_uri", sa.String(length=2048)),
    )
    op.add_column("datasets", sa.Column("quality_report_sha256", sa.String(length=64)))
    op.add_column(
        "datasets",
        sa.Column("decoder_pending_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("datasets", sa.Column("failure_code", sa.String(length=64)))
    op.add_column(
        "datasets",
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("datasets", sa.Column("ingestion_started_at", sa.DateTime(timezone=True)))
    op.add_column("datasets", sa.Column("finalized_at", sa.DateTime(timezone=True)))

    op.create_table(
        "dataset_upload_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("expected_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=False),
        sa.Column("temporary_object_key", sa.String(length=512), nullable=False),
        sa.Column("original_object_key", sa.String(length=512), nullable=False),
        sa.Column("prepared_flat_object_key", sa.String(length=512), nullable=False),
        sa.Column("manifest_object_key", sa.String(length=512), nullable=False),
        sa.Column("quality_report_object_key", sa.String(length=512), nullable=False),
        sa.Column("storage_backend", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("finalization_token", sa.String(length=36), nullable=True),
        sa.Column("upload_token_hash", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'finalizing', 'completed', 'failed', 'expired')",
            name=op.f("ck_dataset_upload_sessions_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name=op.f("fk_dataset_upload_sessions_dataset_id_datasets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_dataset_upload_sessions_owner_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_upload_sessions")),
        sa.UniqueConstraint(
            "owner_id",
            "idempotency_key",
            "generation",
            name="uq_dataset_upload_owner_idempotency_generation",
        ),
        sa.UniqueConstraint(
            "temporary_object_key",
            name=op.f("uq_dataset_upload_sessions_temporary_object_key"),
        ),
    )
    op.create_index(
        op.f("ix_dataset_upload_sessions_dataset_id"),
        "dataset_upload_sessions",
        ["dataset_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dataset_upload_sessions_owner_id"),
        "dataset_upload_sessions",
        ["owner_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dataset_upload_sessions_expires_at"),
        "dataset_upload_sessions",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_dataset_upload_status_expiry",
        "dataset_upload_sessions",
        ["status", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_dataset_upload_status_expiry",
        table_name="dataset_upload_sessions",
    )
    op.drop_index(
        op.f("ix_dataset_upload_sessions_expires_at"),
        table_name="dataset_upload_sessions",
    )
    op.drop_index(
        op.f("ix_dataset_upload_sessions_owner_id"),
        table_name="dataset_upload_sessions",
    )
    op.drop_index(
        op.f("ix_dataset_upload_sessions_dataset_id"),
        table_name="dataset_upload_sessions",
    )
    op.drop_table("dataset_upload_sessions")

    for column in (
        "finalized_at",
        "ingestion_started_at",
        "retryable",
        "failure_code",
        "decoder_pending_count",
        "quality_report_sha256",
        "quality_report_storage_uri",
        "manifest_sha256",
        "manifest_storage_uri",
        "prepared_flat_sha256",
        "prepared_flat_size_bytes",
        "original_mime_type",
        "original_sha256",
        "original_size_bytes",
        "original_filename",
        "status",
    ):
        op.drop_column("datasets", column)
