"""add provenance-bound Experiment model registry

Revision ID: e4c7b9d2f610
Revises: d8f2a6c4b901
Create Date: 2026-07-12 23:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e4c7b9d2f610"
down_revision: str | Sequence[str] | None = "d8f2a6c4b901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A direct unique index avoids SQLite batch-table replacement of `jobs`,
    # which would cascade-delete historical attempts through existing FKs.
    op.create_index(
        "uq_job_id_experiment",
        "jobs",
        ["id", "experiment_id"],
        unique=True,
    )

    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.add_column(sa.Column("rvc_commit_hash", sa.String(length=64)))
        batch_op.add_column(
            sa.Column("execution_provenance_version", sa.String(length=32))
        )
        batch_op.create_check_constraint(
            op.f("ck_job_attempts_rvc_commit_hash_length"),
            "rvc_commit_hash IS NULL OR "
            "(length(rvc_commit_hash) >= 7 AND length(rvc_commit_hash) <= 64)",
        )
        batch_op.create_check_constraint(
            op.f("ck_job_attempts_execution_provenance_version_allowed"),
            "execution_provenance_version IS NULL OR "
            "execution_provenance_version = 'worker-claim-v1'",
        )

    op.create_table(
        "experiment_model_registries",
        sa.Column("experiment_id", sa.String(length=36), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "row_version >= 1",
            name=op.f("ck_experiment_model_registries_row_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            name=op.f("fk_experiment_model_registries_experiment_id_experiments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "experiment_id",
            name=op.f("pk_experiment_model_registries"),
        ),
    )

    op.create_table(
        "model_registry_entries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("experiment_id", sa.String(length=36), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("active_slot", sa.Integer()),
        sa.Column("source_job_id", sa.String(length=36), nullable=False),
        sa.Column("source_attempt_id", sa.String(length=36), nullable=False),
        sa.Column("source_job_name", sa.String(length=128), nullable=False),
        sa.Column("source_attempt_number", sa.Integer(), nullable=False),
        sa.Column("engine_mode", sa.String(length=32), nullable=False),
        sa.Column("job_config_sha256", sa.String(length=64), nullable=False),
        sa.Column("rvc_commit_hash", sa.String(length=64), nullable=False),
        sa.Column("execution_provenance_version", sa.String(length=32), nullable=False),
        sa.Column("runtime_image_digest", sa.String(length=71), nullable=False),
        sa.Column("runtime_asset_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("model_artifact_id", sa.String(length=36), nullable=False),
        sa.Column("model_filename", sa.String(length=255), nullable=False),
        sa.Column("model_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("model_sha256", sa.String(length=64), nullable=False),
        sa.Column("index_artifact_id", sa.String(length=36)),
        sa.Column("index_filename", sa.String(length=255)),
        sa.Column("index_size_bytes", sa.BigInteger()),
        sa.Column("index_sha256", sa.String(length=64)),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("approved_by", sa.String(length=36)),
        sa.Column("revoked_by", sa.String(length=36)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoke_reason", sa.String(length=32)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "row_version >= 1",
            name=op.f("ck_model_registry_entries_row_version_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'approved', 'revoked')",
            name=op.f("ck_model_registry_entries_status_allowed"),
        ),
        sa.CheckConstraint(
            "active_slot IS NULL OR active_slot = 1",
            name=op.f("ck_model_registry_entries_active_slot_allowed"),
        ),
        sa.CheckConstraint(
            "((status = 'candidate' AND active_slot IS NULL "
            "AND approved_by IS NULL AND revoked_by IS NULL "
            "AND approved_at IS NULL AND revoked_at IS NULL AND revoke_reason IS NULL) "
            "OR (status = 'approved' AND approved_at IS NOT NULL "
            "AND approved_by IS NOT NULL AND revoked_by IS NULL "
            "AND revoked_at IS NULL AND revoke_reason IS NULL) "
            "OR (status = 'revoked' AND active_slot IS NULL "
            "AND revoked_by IS NOT NULL AND revoked_at IS NOT NULL "
            "AND revoke_reason IS NOT NULL))",
            name=op.f("ck_model_registry_entries_status_timestamps_consistent"),
        ),
        sa.CheckConstraint(
            "active_slot IS NULL OR status = 'approved'",
            name=op.f("ck_model_registry_entries_active_slot_requires_approved"),
        ),
        sa.CheckConstraint(
            "(approved_by IS NULL AND approved_at IS NULL) OR "
            "(approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name=op.f("ck_model_registry_entries_approval_actor_timestamp_together"),
        ),
        sa.CheckConstraint(
            "revoke_reason IS NULL OR revoke_reason IN "
            "('quality_rejected', 'security_issue', 'operator_request')",
            name=op.f("ck_model_registry_entries_revoke_reason_allowed"),
        ),
        sa.CheckConstraint(
            "source_attempt_number >= 1",
            name=op.f("ck_model_registry_entries_source_attempt_number_positive"),
        ),
        sa.CheckConstraint(
            "model_size_bytes > 0",
            name=op.f("ck_model_registry_entries_model_size_positive"),
        ),
        sa.CheckConstraint(
            "length(model_sha256) = 64 AND model_sha256 = lower(model_sha256) "
            "AND length(job_config_sha256) = 64 "
            "AND job_config_sha256 = lower(job_config_sha256)",
            name=op.f("ck_model_registry_entries_required_sha256_lengths"),
        ),
        sa.CheckConstraint(
            "engine_mode = 'rvc_webui'",
            name=op.f("ck_model_registry_entries_engine_mode_rvc_webui"),
        ),
        sa.CheckConstraint(
            "execution_provenance_version = 'worker-claim-v1'",
            name=op.f(
                "ck_model_registry_entries_execution_provenance_version_worker_claim_v1"
            ),
        ),
        sa.CheckConstraint(
            "length(rvc_commit_hash) = 40 AND rvc_commit_hash = lower(rvc_commit_hash)",
            name=op.f("ck_model_registry_entries_rvc_commit_hash_reviewed_format"),
        ),
        sa.CheckConstraint(
            "length(runtime_image_digest) = 71 "
            "AND substr(runtime_image_digest, 1, 7) = 'sha256:' "
            "AND runtime_image_digest = lower(runtime_image_digest) "
            "AND length(runtime_asset_manifest_sha256) = 64 "
            "AND runtime_asset_manifest_sha256 = lower(runtime_asset_manifest_sha256)",
            name=op.f("ck_model_registry_entries_runtime_provenance_format"),
        ),
        sa.CheckConstraint(
            "(index_artifact_id IS NULL AND index_filename IS NULL "
            "AND index_size_bytes IS NULL AND index_sha256 IS NULL) OR "
            "(index_artifact_id IS NOT NULL AND index_filename IS NOT NULL "
            "AND index_size_bytes > 0 AND length(index_sha256) = 64 "
            "AND index_sha256 = lower(index_sha256))",
            name=op.f("ck_model_registry_entries_index_snapshot_together"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiment_model_registries.experiment_id"],
            name=op.f(
                "fk_model_registry_entries_experiment_id_experiment_model_registries"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_job_id", "experiment_id"],
            ["jobs.id", "jobs.experiment_id"],
            name="fk_model_registry_entry_job_experiment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_attempt_id", "source_job_id"],
            ["job_attempts.id", "job_attempts.job_id"],
            name="fk_model_registry_entry_attempt_job",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_artifact_id", "source_job_id", "source_attempt_id"],
            ["artifacts.id", "artifacts.job_id", "artifacts.attempt_id"],
            name="fk_model_registry_entry_model_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["index_artifact_id", "source_job_id", "source_attempt_id"],
            ["artifacts.id", "artifacts.job_id", "artifacts.attempt_id"],
            name="fk_model_registry_entry_index_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_model_registry_entries_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name=op.f("fk_model_registry_entries_approved_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by"],
            ["users.id"],
            name=op.f("fk_model_registry_entries_revoked_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_registry_entries")),
        sa.UniqueConstraint(
            "experiment_id",
            "active_slot",
            name="uq_model_registry_entry_active_slot",
        ),
        sa.UniqueConstraint(
            "model_artifact_id",
            name="uq_model_registry_entry_model_artifact",
        ),
        sa.UniqueConstraint(
            "id",
            "experiment_id",
            name="uq_model_registry_entry_id_experiment",
        ),
    )
    op.create_index(
        "ix_model_registry_entry_experiment_created",
        "model_registry_entries",
        ["experiment_id", "created_at"],
        unique=False,
    )
    for column in (
        "source_job_id",
        "source_attempt_id",
        "model_artifact_id",
        "index_artifact_id",
    ):
        op.create_index(
            op.f(f"ix_model_registry_entries_{column}"),
            "model_registry_entries",
            [column],
            unique=False,
        )

    op.create_table(
        "model_registry_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("operation_type", sa.String(length=16), nullable=False),
        sa.Column("experiment_id", sa.String(length=36), nullable=False),
        sa.Column("entry_id", sa.String(length=36), nullable=False),
        sa.Column(
            "response_json",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()),
                "postgresql",
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "operation_type IN ('candidate', 'promote', 'revoke')",
            name=op.f("ck_model_registry_operations_operation_type_allowed"),
        ),
        sa.CheckConstraint(
            "length(idempotency_key_hash) = 64 "
            "AND idempotency_key_hash = lower(idempotency_key_hash)",
            name=op.f("ck_model_registry_operations_idempotency_key_hash_format"),
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64 "
            "AND request_fingerprint = lower(request_fingerprint)",
            name=op.f("ck_model_registry_operations_request_fingerprint_format"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_model_registry_operations_actor_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiment_model_registries.experiment_id"],
            name=op.f(
                "fk_model_registry_operations_experiment_id_experiment_model_registries"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["entry_id", "experiment_id"],
            ["model_registry_entries.id", "model_registry_entries.experiment_id"],
            name="fk_model_registry_operation_entry_experiment",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_registry_operations")),
        sa.UniqueConstraint(
            "actor_id",
            "idempotency_key_hash",
            name="uq_model_registry_operation_actor_key",
        ),
    )
    for column in ("actor_id", "experiment_id", "entry_id"):
        op.create_index(
            op.f(f"ix_model_registry_operations_{column}"),
            "model_registry_operations",
            [column],
            unique=False,
        )


def downgrade() -> None:
    for column in ("entry_id", "experiment_id", "actor_id"):
        op.drop_index(
            op.f(f"ix_model_registry_operations_{column}"),
            table_name="model_registry_operations",
        )
    op.drop_table("model_registry_operations")

    for column in (
        "index_artifact_id",
        "model_artifact_id",
        "source_attempt_id",
        "source_job_id",
    ):
        op.drop_index(
            op.f(f"ix_model_registry_entries_{column}"),
            table_name="model_registry_entries",
        )
    op.drop_index(
        "ix_model_registry_entry_experiment_created",
        table_name="model_registry_entries",
    )
    op.drop_table("model_registry_entries")
    op.drop_table("experiment_model_registries")

    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_job_attempts_execution_provenance_version_allowed"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_job_attempts_rvc_commit_hash_length"),
            type_="check",
        )
        batch_op.drop_column("execution_provenance_version")
        batch_op.drop_column("rvc_commit_hash")

    op.drop_index("uq_job_id_experiment", table_name="jobs")
