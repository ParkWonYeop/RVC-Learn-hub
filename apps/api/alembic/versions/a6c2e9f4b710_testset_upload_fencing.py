"""fence TestSet upload writers and add staging cleanup claims

Revision ID: a6c2e9f4b710
Revises: c4d9e8f1a720
Create Date: 2026-07-11 23:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a6c2e9f4b710"
down_revision: str | Sequence[str] | None = "c4d9e8f1a720"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("maintenance_task_runs") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_maintenance_task_runs_task_name_allowed"),
            type_="check",
        )
        batch_op.create_check_constraint(
            op.f("ck_maintenance_task_runs_task_name_allowed"),
            "task_name IN ('dataset_staging_cleanup', 'test_set_staging_cleanup')",
        )

    with op.batch_alter_table("test_set_item_upload_sessions") as batch_op:
        batch_op.add_column(sa.Column("upload_write_token", sa.String(length=36)))
        batch_op.add_column(
            sa.Column("upload_heartbeat_at", sa.DateTime(timezone=True))
        )
        batch_op.add_column(
            sa.Column("finalization_heartbeat_at", sa.DateTime(timezone=True))
        )
        batch_op.add_column(
            sa.Column("cleanup_claim_run_id", sa.String(length=36))
        )
        batch_op.add_column(
            sa.Column("cleanup_claimed_at", sa.DateTime(timezone=True))
        )
        batch_op.add_column(sa.Column("cleanup_claim_generation", sa.Integer()))
        batch_op.add_column(
            sa.Column("cleanup_first_deleted_at", sa.DateTime(timezone=True))
        )
        batch_op.add_column(
            sa.Column("cleanup_completed_at", sa.DateTime(timezone=True))
        )
        batch_op.create_check_constraint(
            op.f("ck_test_set_item_upload_sessions_cleanup_claim_generation_positive"),
            "cleanup_claim_generation IS NULL OR cleanup_claim_generation > 0",
        )
        batch_op.create_foreign_key(
            op.f(
                "fk_test_set_item_upload_sessions_cleanup_claim_run_id_maintenance_task_runs"
            ),
            "maintenance_task_runs",
            ["cleanup_claim_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            op.f("ix_test_set_item_upload_sessions_cleanup_claim_run_id"),
            ["cleanup_claim_run_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("test_set_item_upload_sessions") as batch_op:
        batch_op.drop_index(
            op.f("ix_test_set_item_upload_sessions_cleanup_claim_run_id")
        )
        batch_op.drop_constraint(
            op.f(
                "fk_test_set_item_upload_sessions_cleanup_claim_run_id_maintenance_task_runs"
            ),
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            op.f("ck_test_set_item_upload_sessions_cleanup_claim_generation_positive"),
            type_="check",
        )
        batch_op.drop_column("cleanup_completed_at")
        batch_op.drop_column("cleanup_first_deleted_at")
        batch_op.drop_column("cleanup_claim_generation")
        batch_op.drop_column("cleanup_claimed_at")
        batch_op.drop_column("cleanup_claim_run_id")
        batch_op.drop_column("finalization_heartbeat_at")
        batch_op.drop_column("upload_heartbeat_at")
        batch_op.drop_column("upload_write_token")

    with op.batch_alter_table("maintenance_task_runs") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_maintenance_task_runs_task_name_allowed"),
            type_="check",
        )
        batch_op.create_check_constraint(
            op.f("ck_maintenance_task_runs_task_name_allowed"),
            "task_name IN ('dataset_staging_cleanup')",
        )
