"""bind Sample attempts to approved runtime evidence and allow content reuse

Revision ID: b8e4a1c6d230
Revises: 9d2f4b7c8e10
Create Date: 2026-07-11 22:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8e4a1c6d230"
down_revision: str | Sequence[str] | None = "9d2f4b7c8e10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.add_column(sa.Column("runtime_image_digest", sa.String(length=71), nullable=True))
        batch_op.add_column(
            sa.Column(
                "runtime_asset_manifest_sha256",
                sa.String(length=64),
                nullable=True,
            )
        )
    # An Artifact is content-addressed within an attempt. Multiple TestSet inputs
    # may legitimately produce the same PCM bytes, so Sample is many-to-one here.
    with op.batch_alter_table("samples") as batch_op:
        batch_op.add_column(
            sa.Column(
                "native_inference_manifest_sha256",
                sa.String(length=64),
                nullable=False,
                server_default="0" * 64,
            )
        )
        batch_op.add_column(
            sa.Column(
                "native_inference_request_sha256",
                sa.String(length=64),
                nullable=False,
                server_default="0" * 64,
            )
        )
        batch_op.drop_constraint("uq_sample_artifact", type_="unique")
    with op.batch_alter_table("samples") as batch_op:
        batch_op.alter_column(
            "native_inference_manifest_sha256",
            existing_type=sa.String(length=64),
            nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "native_inference_request_sha256",
            existing_type=sa.String(length=64),
            nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    # This intentionally fails rather than losing rows if an operator created
    # legitimate many-to-one Sample bindings after the upgrade.
    with op.batch_alter_table("samples") as batch_op:
        batch_op.create_unique_constraint("uq_sample_artifact", ["artifact_id"])
        batch_op.drop_column("native_inference_request_sha256")
        batch_op.drop_column("native_inference_manifest_sha256")
    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.drop_column("runtime_asset_manifest_sha256")
        batch_op.drop_column("runtime_image_digest")
