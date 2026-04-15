"""add fail-closed administrator user lifecycle

Revision ID: b4a91d7e2c63
Revises: f9c4a7d2b610
Create Date: 2026-07-12 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b4a91d7e2c63"
down_revision: str | Sequence[str] | None = "f9c4a7d2b610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column("row_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column(
                "access_token_version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_users_row_version_positive"),
            "row_version >= 1",
        )
        batch_op.create_check_constraint(
            op.f("ck_users_access_token_version_positive"),
            "access_token_version >= 1",
        )
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "row_version",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "access_token_version",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=None,
        )
    op.create_index(
        "ix_users_role_disabled_created_at",
        "users",
        ["role", "disabled", "created_at"],
        unique=False,
    )

    op.create_table(
        "admin_user_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("operation_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=False),
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
            "operation_type IN ('create', 'access_update', 'password_reset')",
            name=op.f("ck_admin_user_operations_operation_type_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_admin_user_operations_actor_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resource_id"],
            ["users.id"],
            name=op.f("fk_admin_user_operations_resource_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_user_operations")),
        sa.UniqueConstraint(
            "actor_id",
            "idempotency_key_hash",
            name="uq_admin_user_operation_actor_key",
        ),
    )
    op.create_index(
        op.f("ix_admin_user_operations_actor_id"),
        "admin_user_operations",
        ["actor_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_user_operations_resource_id"),
        "admin_user_operations",
        ["resource_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_admin_user_operations_resource_id"),
        table_name="admin_user_operations",
    )
    op.drop_index(
        op.f("ix_admin_user_operations_actor_id"),
        table_name="admin_user_operations",
    )
    op.drop_table("admin_user_operations")
    op.drop_index("ix_users_role_disabled_created_at", table_name="users")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_users_access_token_version_positive"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_users_row_version_positive"),
            type_="check",
        )
        batch_op.drop_column("access_token_version")
        batch_op.drop_column("row_version")
