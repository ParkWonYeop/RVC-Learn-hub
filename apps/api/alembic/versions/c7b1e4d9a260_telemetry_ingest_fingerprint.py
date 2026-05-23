"""bind telemetry idempotency keys to canonical payloads

Revision ID: c7b1e4d9a260
Revises: b4a91d7e2c63
Create Date: 2026-07-12 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7b1e4d9a260"
down_revision: str | Sequence[str] | None = "b4a91d7e2c63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ingest_batches") as batch_op:
        batch_op.add_column(sa.Column("payload_fingerprint", sa.String(length=64), nullable=True))
        batch_op.create_check_constraint(
            op.f("ck_ingest_batches_payload_fingerprint_length"),
            "payload_fingerprint IS NULL OR length(payload_fingerprint) = 64",
        )


def downgrade() -> None:
    with op.batch_alter_table("ingest_batches") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_ingest_batches_payload_fingerprint_length"),
            type_="check",
        )
        batch_op.drop_column("payload_fingerprint")
