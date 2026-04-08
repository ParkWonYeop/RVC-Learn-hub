"""add immutable TestSet, Preset and Sample provenance ledger

Revision ID: f3a8c6d9e120
Revises: e7c9a1b4d260
Create Date: 2026-07-11 20:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3a8c6d9e120"
down_revision: str | Sequence[str] | None = "e7c9a1b4d260"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_VALUE = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.create_table(
        "test_sets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("family_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("manifest_storage_uri", sa.String(length=2048), nullable=True),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name=op.f("ck_test_sets_revision_positive")),
        sa.CheckConstraint(
            "item_count >= 0", name=op.f("ck_test_sets_item_count_non_negative")
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'ready', 'failed')",
            name=op.f("ck_test_sets_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_test_sets_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_sets")),
        sa.UniqueConstraint(
            "family_id", "revision", name="uq_test_set_family_revision"
        ),
        sa.UniqueConstraint(
            "created_by", "name", "revision", name="uq_test_set_owner_name_revision"
        ),
    )
    op.create_index(op.f("ix_test_sets_family_id"), "test_sets", ["family_id"])
    op.create_index(op.f("ix_test_sets_status"), "test_sets", ["status"])
    op.create_index(op.f("ix_test_sets_created_by"), "test_sets", ["created_by"])
    op.create_index(
        "ix_test_set_owner_name", "test_sets", ["created_by", "name", "revision"]
    )

    op.create_table(
        "presets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("family_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("config_json", JSON_VALUE, nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name=op.f("ck_presets_revision_positive")),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_presets_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_presets")),
        sa.UniqueConstraint(
            "family_id", "revision", name="uq_preset_family_revision"
        ),
        sa.UniqueConstraint(
            "created_by", "name", "revision", name="uq_preset_owner_name_revision"
        ),
    )
    op.create_index(op.f("ix_presets_family_id"), "presets", ["family_id"])
    op.create_index(op.f("ix_presets_created_by"), "presets", ["created_by"])
    op.create_index(
        "ix_preset_owner_name", "presets", ["created_by", "name", "revision"]
    )

    op.create_table(
        "test_set_item_upload_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("test_set_id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("item_key", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("expected_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=False),
        sa.Column("license_reference", sa.String(length=320), nullable=False),
        sa.Column("provenance_reference", sa.String(length=320), nullable=False),
        sa.Column("temporary_object_key", sa.String(length=512), nullable=False),
        sa.Column("canonical_object_key", sa.String(length=512), nullable=False),
        sa.Column("storage_backend", sa.String(length=16), nullable=False),
        sa.Column("storage_namespace_sha256", sa.String(length=64), nullable=False),
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
            name=op.f("ck_test_set_item_upload_sessions_status_allowed"),
        ),
        sa.CheckConstraint(
            "generation > 0",
            name=op.f("ck_test_set_item_upload_sessions_generation_positive"),
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name=op.f("ck_test_set_item_upload_sessions_sort_order_non_negative"),
        ),
        sa.CheckConstraint(
            "expected_size_bytes > 0",
            name=op.f("ck_test_set_item_upload_sessions_expected_size_positive"),
        ),
        sa.CheckConstraint(
            "length(storage_namespace_sha256) = 64",
            name=op.f(
                "ck_test_set_item_upload_sessions_storage_namespace_sha256_length"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_test_set_item_upload_sessions_owner_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["test_set_id"],
            ["test_sets.id"],
            name=op.f("fk_test_set_item_upload_sessions_test_set_id_test_sets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_set_item_upload_sessions")),
        sa.UniqueConstraint(
            "canonical_object_key",
            name=op.f("uq_test_set_item_upload_sessions_canonical_object_key"),
        ),
        sa.UniqueConstraint(
            "temporary_object_key",
            name=op.f("uq_test_set_item_upload_sessions_temporary_object_key"),
        ),
        sa.UniqueConstraint(
            "test_set_id",
            "owner_id",
            "idempotency_key",
            "generation",
            name="uq_test_set_item_upload_idempotency_generation",
        ),
    )
    op.create_index(
        op.f("ix_test_set_item_upload_sessions_test_set_id"),
        "test_set_item_upload_sessions",
        ["test_set_id"],
    )
    op.create_index(
        op.f("ix_test_set_item_upload_sessions_owner_id"),
        "test_set_item_upload_sessions",
        ["owner_id"],
    )
    op.create_index(
        op.f("ix_test_set_item_upload_sessions_expires_at"),
        "test_set_item_upload_sessions",
        ["expires_at"],
    )
    op.create_index(
        "ix_test_set_item_upload_status_expiry",
        "test_set_item_upload_sessions",
        ["status", "expires_at"],
    )

    op.create_table(
        "test_set_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("test_set_id", sa.String(length=36), nullable=False),
        sa.Column("item_key", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("storage_uri", sa.String(length=2048), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("sample_rate_hz", sa.Integer(), nullable=False),
        sa.Column("channels", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("license_reference", sa.String(length=320), nullable=False),
        sa.Column("provenance_reference", sa.String(length=320), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sort_order >= 0", name=op.f("ck_test_set_items_sort_order_non_negative")
        ),
        sa.CheckConstraint(
            "size_bytes > 0", name=op.f("ck_test_set_items_size_bytes_positive")
        ),
        sa.CheckConstraint(
            "sample_rate_hz > 0", name=op.f("ck_test_set_items_sample_rate_positive")
        ),
        sa.CheckConstraint(
            "channels > 0", name=op.f("ck_test_set_items_channels_positive")
        ),
        sa.CheckConstraint(
            "duration_seconds > 0", name=op.f("ck_test_set_items_duration_positive")
        ),
        sa.ForeignKeyConstraint(
            ["test_set_id"],
            ["test_sets.id"],
            name=op.f("fk_test_set_items_test_set_id_test_sets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_set_items")),
        sa.UniqueConstraint(
            "test_set_id", "item_key", name="uq_test_set_item_key"
        ),
        sa.UniqueConstraint(
            "test_set_id", "sort_order", name="uq_test_set_item_order"
        ),
        sa.UniqueConstraint(
            "id", "test_set_id", name="uq_test_set_item_id_test_set"
        ),
    )
    op.create_index(
        op.f("ix_test_set_items_test_set_id"), "test_set_items", ["test_set_id"]
    )

    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column("test_set_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("preset_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("sample_plan_json", JSON_VALUE, nullable=True))
        batch_op.add_column(
            sa.Column("sample_plan_sha256", sa.String(length=64), nullable=True)
        )
        batch_op.create_foreign_key(
            op.f("fk_jobs_test_set_id_test_sets"),
            "test_sets",
            ["test_set_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            op.f("fk_jobs_preset_id_presets"),
            "presets",
            ["preset_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(op.f("ix_jobs_test_set_id"), ["test_set_id"])
        batch_op.create_index(op.f("ix_jobs_preset_id"), ["preset_id"])
        batch_op.create_unique_constraint(
            "uq_job_test_set_snapshot", ["id", "test_set_id"]
        )

    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.create_unique_constraint(
            "uq_job_attempt_id_job", ["id", "job_id"]
        )

    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.create_unique_constraint(
            "uq_artifact_id_job_attempt", ["id", "job_id", "attempt_id"]
        )

    op.create_table(
        "samples",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("test_set_id", sa.String(length=36), nullable=False),
        sa.Column("test_set_item_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_id", sa.String(length=36), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("model_sha256", sa.String(length=64), nullable=False),
        sa.Column("index_sha256", sa.String(length=64), nullable=True),
        sa.Column("inference_f0_method", sa.String(length=16), nullable=False),
        sa.Column("inference_config_sha256", sa.String(length=64), nullable=False),
        sa.Column("output_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("output_sha256", sa.String(length=64), nullable=False),
        sa.Column("output_sample_rate_hz", sa.Integer(), nullable=False),
        sa.Column("output_channels", sa.Integer(), nullable=False),
        sa.Column("output_duration_seconds", sa.Float(), nullable=False),
        sa.Column("metrics_json", JSON_VALUE, nullable=False),
        sa.Column("rvc_commit_hash", sa.String(length=64), nullable=False),
        sa.Column("runtime_image_digest", sa.String(length=255), nullable=False),
        sa.Column("runtime_asset_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "output_size_bytes > 0", name=op.f("ck_samples_output_size_positive")
        ),
        sa.CheckConstraint(
            "output_sample_rate_hz > 0",
            name=op.f("ck_samples_output_sample_rate_positive"),
        ),
        sa.CheckConstraint(
            "output_channels > 0", name=op.f("ck_samples_output_channels_positive")
        ),
        sa.CheckConstraint(
            "output_duration_seconds > 0",
            name=op.f("ck_samples_output_duration_positive"),
        ),
        sa.CheckConstraint(
            "inference_f0_method IN ('pm', 'harvest', 'crepe', 'rmvpe')",
            name=op.f("ck_samples_inference_f0_method_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_samples_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "job_id"],
            ["job_attempts.id", "job_attempts.job_id"],
            name="fk_sample_attempt_job",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "test_set_id"],
            ["jobs.id", "jobs.test_set_id"],
            name="fk_sample_job_test_set",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["test_set_item_id", "test_set_id"],
            ["test_set_items.id", "test_set_items.test_set_id"],
            name="fk_sample_item_test_set",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id", "job_id", "attempt_id"],
            ["artifacts.id", "artifacts.job_id", "artifacts.attempt_id"],
            name="fk_sample_artifact_job_attempt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["test_set_id"],
            ["test_sets.id"],
            name=op.f("fk_samples_test_set_id_test_sets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_samples")),
        sa.UniqueConstraint("artifact_id", name="uq_sample_artifact"),
        sa.UniqueConstraint(
            "attempt_id",
            "test_set_item_id",
            "inference_config_sha256",
            name="uq_sample_attempt_item_config",
        ),
    )
    for column in ("job_id", "attempt_id", "test_set_id", "test_set_item_id"):
        op.create_index(op.f(f"ix_samples_{column}"), "samples", [column])


def downgrade() -> None:
    for column in ("test_set_item_id", "test_set_id", "attempt_id", "job_id"):
        op.drop_index(op.f(f"ix_samples_{column}"), table_name="samples")
    op.drop_table("samples")
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.drop_constraint("uq_artifact_id_job_attempt", type_="unique")
    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.drop_constraint("uq_job_attempt_id_job", type_="unique")
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_constraint("uq_job_test_set_snapshot", type_="unique")
        batch_op.drop_index(op.f("ix_jobs_preset_id"))
        batch_op.drop_index(op.f("ix_jobs_test_set_id"))
        batch_op.drop_constraint(op.f("fk_jobs_preset_id_presets"), type_="foreignkey")
        batch_op.drop_constraint(op.f("fk_jobs_test_set_id_test_sets"), type_="foreignkey")
        batch_op.drop_column("sample_plan_sha256")
        batch_op.drop_column("sample_plan_json")
        batch_op.drop_column("preset_id")
        batch_op.drop_column("test_set_id")
    op.drop_index(op.f("ix_test_set_items_test_set_id"), table_name="test_set_items")
    op.drop_table("test_set_items")
    op.drop_index(
        "ix_test_set_item_upload_status_expiry",
        table_name="test_set_item_upload_sessions",
    )
    op.drop_index(
        op.f("ix_test_set_item_upload_sessions_expires_at"),
        table_name="test_set_item_upload_sessions",
    )
    op.drop_index(
        op.f("ix_test_set_item_upload_sessions_owner_id"),
        table_name="test_set_item_upload_sessions",
    )
    op.drop_index(
        op.f("ix_test_set_item_upload_sessions_test_set_id"),
        table_name="test_set_item_upload_sessions",
    )
    op.drop_table("test_set_item_upload_sessions")
    op.drop_index("ix_preset_owner_name", table_name="presets")
    op.drop_index(op.f("ix_presets_created_by"), table_name="presets")
    op.drop_index(op.f("ix_presets_family_id"), table_name="presets")
    op.drop_table("presets")
    op.drop_index("ix_test_set_owner_name", table_name="test_sets")
    op.drop_index(op.f("ix_test_sets_created_by"), table_name="test_sets")
    op.drop_index(op.f("ix_test_sets_status"), table_name="test_sets")
    op.drop_index(op.f("ix_test_sets_family_id"), table_name="test_sets")
    op.drop_table("test_sets")
