"""add user authentication state and access-token revocation

Revision ID: 7e9f4a2c1b6d
Revises: 229a6edc0e40
Create Date: 2026-07-11 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7e9f4a2c1b6d"
down_revision: str | Sequence[str] | None = "229a6edc0e40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "is_active",
            new_column_name="disabled",
            existing_type=sa.Boolean(),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_users_role_allowed",
            "role IN ('admin', 'user')",
        )
    # The legacy column represented the inverse meaning. Preserve existing
    # account state rather than merely renaming the field.
    op.execute(sa.text("UPDATE users SET disabled = NOT disabled"))

    bootstrap_state = op.create_table(
        "admin_bootstrap_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("admin_user_id", sa.String(length=36), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["admin_user_id"],
            ["users.id"],
            name=op.f("fk_admin_bootstrap_state_admin_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_bootstrap_state")),
        sa.UniqueConstraint(
            "admin_user_id",
            name=op.f("uq_admin_bootstrap_state_admin_user_id"),
        ),
    )
    op.bulk_insert(
        bootstrap_state,
        [{"id": 1, "admin_user_id": None, "completed_at": None, "lock_version": 0}],
    )

    op.create_table(
        "revoked_access_tokens",
        sa.Column("jti", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_revoked_access_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("jti", name=op.f("pk_revoked_access_tokens")),
    )
    op.create_index(
        op.f("ix_revoked_access_tokens_expires_at"),
        "revoked_access_tokens",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_revoked_access_tokens_user_id"),
        "revoked_access_tokens",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_revoked_access_tokens_user_id"),
        table_name="revoked_access_tokens",
    )
    op.drop_index(
        op.f("ix_revoked_access_tokens_expires_at"),
        table_name="revoked_access_tokens",
    )
    op.drop_table("revoked_access_tokens")
    op.drop_table("admin_bootstrap_state")

    op.execute(sa.text("UPDATE users SET disabled = NOT disabled"))
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("ck_users_role_allowed", type_="check")
        batch_op.alter_column(
            "disabled",
            new_column_name="is_active",
            existing_type=sa.Boolean(),
            existing_nullable=False,
        )
