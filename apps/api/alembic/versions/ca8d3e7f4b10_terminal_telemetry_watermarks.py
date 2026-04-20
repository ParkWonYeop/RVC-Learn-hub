"""store terminal telemetry sequence watermarks

Revision ID: ca8d3e7f4b10
Revises: c7b1e4d9a260
Create Date: 2026-07-12 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ca8d3e7f4b10"
down_revision: str | Sequence[str] | None = "c7b1e4d9a260"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.add_column(sa.Column("telemetry_log_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("telemetry_metric_count", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            op.f("ck_job_attempts_telemetry_counts_all_null_or_present"),
            "(telemetry_log_count IS NULL AND telemetry_metric_count IS NULL) OR "
            "(telemetry_log_count IS NOT NULL AND telemetry_metric_count IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            op.f("ck_job_attempts_telemetry_log_count_range"),
            "telemetry_log_count IS NULL OR "
            "(telemetry_log_count >= 0 AND telemetry_log_count <= 2147483647)",
        )
        batch_op.create_check_constraint(
            op.f("ck_job_attempts_telemetry_metric_count_range"),
            "telemetry_metric_count IS NULL OR "
            "(telemetry_metric_count >= 0 AND telemetry_metric_count <= 2147483647)",
        )
        batch_op.create_check_constraint(
            op.f("ck_job_attempts_telemetry_counts_terminal_only"),
            "telemetry_log_count IS NULL OR status IN ('completed', 'failed', 'cancelled')",
        )


def downgrade() -> None:
    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_job_attempts_telemetry_counts_terminal_only"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_job_attempts_telemetry_metric_count_range"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_job_attempts_telemetry_log_count_range"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_job_attempts_telemetry_counts_all_null_or_present"),
            type_="check",
        )
        batch_op.drop_column("telemetry_metric_count")
        batch_op.drop_column("telemetry_log_count")
