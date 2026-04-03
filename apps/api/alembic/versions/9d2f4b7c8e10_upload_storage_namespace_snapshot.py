"""bind Dataset and Artifact upload sessions to an object namespace

Revision ID: 9d2f4b7c8e10
Revises: f3a8c6d9e120
Create Date: 2026-07-11 21:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9d2f4b7c8e10"
down_revision: str | Sequence[str] | None = "f3a8c6d9e120"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UNBOUND_STORAGE_NAMESPACE_SHA256 = "0" * 64


def upgrade() -> None:
    # Historical rows cannot prove which local root or S3 endpoint held their bytes.
    # Keep them explicitly unbound until the operator adoption command re-verifies them.
    for table_name in ("dataset_upload_sessions", "artifact_upload_sessions"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "storage_namespace_sha256",
                    sa.String(length=64),
                    nullable=False,
                    server_default=UNBOUND_STORAGE_NAMESPACE_SHA256,
                )
            )
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.create_check_constraint(
                op.f(f"ck_{table_name}_storage_namespace_sha256_length"),
                "length(storage_namespace_sha256) = 64",
            )
            batch_op.alter_column(
                "storage_namespace_sha256",
                existing_type=sa.String(length=64),
                nullable=False,
                server_default=None,
            )


def downgrade() -> None:
    for table_name in ("artifact_upload_sessions", "dataset_upload_sessions"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(
                op.f(f"ck_{table_name}_storage_namespace_sha256_length"),
                type_="check",
            )
            batch_op.drop_column("storage_namespace_sha256")
