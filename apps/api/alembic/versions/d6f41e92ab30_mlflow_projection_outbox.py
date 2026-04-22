"""add durable MLflow projection outbox

Revision ID: d6f41e92ab30
Revises: c2b7d4e8f901
Create Date: 2026-07-11 23:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d6f41e92ab30"
down_revision: str | Sequence[str] | None = "c2b7d4e8f901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mlflow_sync_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("aggregate_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", sa.String(length=36), nullable=False),
        sa.Column(
            "payload_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'synced')",
            name=op.f("ck_mlflow_sync_events_status_allowed"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mlflow_sync_events")),
        sa.UniqueConstraint("event_key", name=op.f("uq_mlflow_sync_events_event_key")),
    )
    op.create_index(
        op.f("ix_mlflow_sync_events_event_type"),
        "mlflow_sync_events",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mlflow_sync_events_aggregate_id"),
        "mlflow_sync_events",
        ["aggregate_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mlflow_sync_events_next_attempt_at"),
        "mlflow_sync_events",
        ["next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_mlflow_sync_ready",
        "mlflow_sync_events",
        ["status", "next_attempt_at", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_mlflow_sync_ready", table_name="mlflow_sync_events")
    op.drop_index(
        op.f("ix_mlflow_sync_events_next_attempt_at"),
        table_name="mlflow_sync_events",
    )
    op.drop_index(
        op.f("ix_mlflow_sync_events_aggregate_id"),
        table_name="mlflow_sync_events",
    )
    op.drop_index(
        op.f("ix_mlflow_sync_events_event_type"),
        table_name="mlflow_sync_events",
    )
    op.drop_table("mlflow_sync_events")
