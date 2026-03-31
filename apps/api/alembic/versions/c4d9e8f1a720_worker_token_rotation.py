"""add fail-closed two-phase Worker token rotation

Revision ID: c4d9e8f1a720
Revises: b8e4a1c6d230
Create Date: 2026-07-11 23:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d9e8f1a720"
down_revision: str | Sequence[str] | None = "b8e4a1c6d230"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.add_column(
            sa.Column("row_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column("token_issued_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("token_rotation_id", sa.String(length=36)))
        batch_op.add_column(sa.Column("pending_token_hash", sa.String(length=64)))
        batch_op.add_column(
            sa.Column("token_rotation_started_at", sa.DateTime(timezone=True))
        )
        batch_op.add_column(
            sa.Column("token_rotation_expires_at", sa.DateTime(timezone=True))
        )
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(
            sa.Column("row_version", sa.Integer(), nullable=False, server_default="1")
        )
    op.execute(sa.text("UPDATE workers SET token_issued_at = created_at"))
    with op.batch_alter_table("workers") as batch_op:
        batch_op.alter_column(
            "row_version",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "token_issued_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "token_rotation_fields_together",
            "(token_rotation_id IS NULL AND pending_token_hash IS NULL "
            "AND token_rotation_started_at IS NULL AND token_rotation_expires_at IS NULL) "
            "OR (token_rotation_id IS NOT NULL AND pending_token_hash IS NOT NULL "
            "AND token_rotation_started_at IS NOT NULL "
            "AND token_rotation_expires_at IS NOT NULL)",
        )
        batch_op.create_index(
            "uq_workers_pending_token_hash",
            ["pending_token_hash"],
            unique=True,
        )
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.alter_column(
            "row_version",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("row_version")
    with op.batch_alter_table("workers") as batch_op:
        batch_op.drop_index("uq_workers_pending_token_hash")
        batch_op.drop_constraint("token_rotation_fields_together", type_="check")
        batch_op.drop_column("token_rotation_expires_at")
        batch_op.drop_column("token_rotation_started_at")
        batch_op.drop_column("pending_token_hash")
        batch_op.drop_column("token_rotation_id")
        batch_op.drop_column("token_issued_at")
        batch_op.drop_column("row_version")
