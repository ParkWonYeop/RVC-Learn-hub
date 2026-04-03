"""fence Dataset upload writers and confirm staging cleanup

Revision ID: e2f8b4c6a930
Revises: d1e7a9c4f620
Create Date: 2026-07-12 00:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f8b4c6a930"
down_revision: str | Sequence[str] | None = "d1e7a9c4f620"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("dataset_upload_sessions") as batch_op:
        batch_op.add_column(sa.Column("upload_write_token", sa.String(length=36)))
        batch_op.add_column(
            sa.Column("upload_heartbeat_at", sa.DateTime(timezone=True))
        )
        batch_op.add_column(
            sa.Column("finalization_heartbeat_at", sa.DateTime(timezone=True))
        )
        batch_op.add_column(sa.Column("cleanup_claim_generation", sa.Integer()))
        batch_op.add_column(
            sa.Column("cleanup_first_deleted_at", sa.DateTime(timezone=True))
        )
        batch_op.create_check_constraint(
            op.f("ck_dataset_upload_sessions_cleanup_claim_generation_positive"),
            "cleanup_claim_generation IS NULL OR cleanup_claim_generation > 0",
        )

    # Active rows created by older binaries contain dataset-wide canonical
    # keys. They must never be finalized by the new session-scoped publisher:
    # expire them so an idempotent init creates a fresh generation with isolated
    # keys. Expired rows are excluded from the active-session/byte quotas.
    upload_sessions = sa.table(
        "dataset_upload_sessions",
        sa.column("dataset_id", sa.String(length=36)),
        sa.column("status", sa.String(length=32)),
        sa.column("upload_write_token", sa.String(length=36)),
        sa.column("upload_heartbeat_at", sa.DateTime(timezone=True)),
        sa.column("finalization_token", sa.String(length=36)),
        sa.column("finalization_heartbeat_at", sa.DateTime(timezone=True)),
        sa.column("failure_code", sa.String(length=64)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    datasets = sa.table(
        "datasets",
        sa.column("id", sa.String(length=36)),
        sa.column("status", sa.String(length=32)),
        sa.column("is_usable", sa.Boolean()),
        sa.column("failure_code", sa.String(length=64)),
        sa.column("retryable", sa.Boolean()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    active_dataset_ids = sa.select(upload_sessions.c.dataset_id).where(
        upload_sessions.c.status.in_(("pending", "finalizing"))
    )
    op.execute(
        datasets.update()
        .where(datasets.c.id.in_(active_dataset_ids))
        .values(
            status="upload_pending",
            is_usable=False,
            failure_code="upload_fencing_upgrade_required",
            retryable=True,
            updated_at=sa.func.now(),
        )
    )
    op.execute(
        upload_sessions.update()
        .where(upload_sessions.c.status.in_(("pending", "finalizing")))
        .values(
            status="expired",
            upload_write_token=None,
            upload_heartbeat_at=None,
            finalization_token=None,
            finalization_heartbeat_at=None,
            failure_code="upload_fencing_upgrade_required",
            updated_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("dataset_upload_sessions") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_dataset_upload_sessions_cleanup_claim_generation_positive"),
            type_="check",
        )
        batch_op.drop_column("cleanup_first_deleted_at")
        batch_op.drop_column("cleanup_claim_generation")
        batch_op.drop_column("finalization_heartbeat_at")
        batch_op.drop_column("upload_heartbeat_at")
        batch_op.drop_column("upload_write_token")
