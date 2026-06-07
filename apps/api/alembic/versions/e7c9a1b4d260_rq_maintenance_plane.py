"""add durable RQ maintenance task ledger and Dataset cleanup claims

Revision ID: e7c9a1b4d260
Revises: d6f41e92ab30
Create Date: 2026-07-11 23:58:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e7c9a1b4d260"
down_revision: str | Sequence[str] | None = "d6f41e92ab30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "maintenance_task_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_name", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "result_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "task_name IN ('dataset_staging_cleanup')",
            name=op.f("ck_maintenance_task_runs_task_name_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retrying', 'completed', 'failed', "
            "'enqueue_failed')",
            name=op.f("ck_maintenance_task_runs_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_maintenance_task_runs_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_maintenance_task_runs")),
        sa.UniqueConstraint(
            "idempotency_key_hash",
            name=op.f("uq_maintenance_task_runs_idempotency_key_hash"),
        ),
        sa.UniqueConstraint("job_id", name=op.f("uq_maintenance_task_runs_job_id")),
    )
    op.create_index(
        op.f("ix_maintenance_task_runs_created_by"),
        "maintenance_task_runs",
        ["created_by"],
        unique=False,
    )
    op.create_index(
        "ix_maintenance_task_run_status_created",
        "maintenance_task_runs",
        ["status", "created_at"],
        unique=False,
    )
    with op.batch_alter_table("dataset_upload_sessions") as batch_op:
        batch_op.add_column(
            sa.Column("cleanup_claim_run_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("cleanup_claimed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("cleanup_completed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_foreign_key(
            op.f(
                "fk_dataset_upload_sessions_cleanup_claim_run_id_maintenance_task_runs"
            ),
            "maintenance_task_runs",
            ["cleanup_claim_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            op.f("ix_dataset_upload_sessions_cleanup_claim_run_id"),
            ["cleanup_claim_run_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("dataset_upload_sessions") as batch_op:
        batch_op.drop_index(
            op.f("ix_dataset_upload_sessions_cleanup_claim_run_id")
        )
        batch_op.drop_constraint(
            op.f(
                "fk_dataset_upload_sessions_cleanup_claim_run_id_maintenance_task_runs"
            ),
            type_="foreignkey",
        )
        batch_op.drop_column("cleanup_completed_at")
        batch_op.drop_column("cleanup_claimed_at")
        batch_op.drop_column("cleanup_claim_run_id")
    op.drop_index(
        "ix_maintenance_task_run_status_created",
        table_name="maintenance_task_runs",
    )
    op.drop_index(
        op.f("ix_maintenance_task_runs_created_by"),
        table_name="maintenance_task_runs",
    )
    op.drop_table("maintenance_task_runs")
