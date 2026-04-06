"""add versioned Dataset PCM integrated loudness

Revision ID: d8f2a6c4b901
Revises: ca8d3e7f4b10
Create Date: 2026-07-12 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8f2a6c4b901"
down_revision: str | Sequence[str] | None = "ca8d3e7f4b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_LOUDNESS_CONSTRAINT = (
    "(pcm_loudness_algorithm IS NULL "
    "AND pcm_loudness_analyzed_file_count IS NULL "
    "AND pcm_loudness_block_count IS NULL "
    "AND pcm_loudness_gated_block_count IS NULL "
    "AND pcm_integrated_lufs IS NULL "
    "AND pcm_loudness_unavailable_reason IS NULL) OR "
    "(pcm_loudness_algorithm = 'itu-r-bs1770-4-mono-stereo-v1' "
    "AND pcm_loudness_analyzed_file_count IS NOT NULL "
    "AND pcm_loudness_block_count IS NOT NULL "
    "AND pcm_loudness_gated_block_count IS NOT NULL "
    "AND pcm_loudness_analyzed_file_count >= 0 "
    "AND pcm_loudness_analyzed_file_count <= 10000 "
    "AND pcm_loudness_block_count >= 0 "
    "AND pcm_loudness_block_count <= 9007199254740991 "
    "AND pcm_loudness_gated_block_count >= 0 "
    "AND pcm_loudness_gated_block_count <= 9007199254740991 "
    "AND pcm_loudness_gated_block_count <= pcm_loudness_block_count "
    "AND ((pcm_integrated_lufs IS NOT NULL "
    "AND pcm_integrated_lufs >= -70 AND pcm_integrated_lufs <= 10 "
    "AND pcm_loudness_unavailable_reason IS NULL "
    "AND pcm_loudness_analyzed_file_count > 0 "
    "AND pcm_loudness_block_count > 0 "
    "AND pcm_loudness_gated_block_count > 0) OR "
    "(pcm_integrated_lufs IS NULL "
    "AND pcm_loudness_gated_block_count = 0 "
    "AND ((pcm_loudness_unavailable_reason IN "
    "('unsupported_channel_layout', 'unsupported_sample_rate') "
    "AND pcm_loudness_analyzed_file_count = 0 "
    "AND pcm_loudness_block_count = 0) OR "
    "(pcm_loudness_unavailable_reason = 'insufficient_duration' "
    "AND pcm_loudness_analyzed_file_count > 0 "
    "AND pcm_loudness_block_count = 0) OR "
    "(pcm_loudness_unavailable_reason = 'below_absolute_gate' "
    "AND pcm_loudness_analyzed_file_count > 0 "
    "AND pcm_loudness_block_count > 0)))))"
)


def upgrade() -> None:
    with op.batch_alter_table("datasets") as batch_op:
        batch_op.add_column(sa.Column("pcm_loudness_algorithm", sa.String(length=48)))
        batch_op.add_column(sa.Column("pcm_loudness_analyzed_file_count", sa.Integer()))
        batch_op.add_column(sa.Column("pcm_loudness_block_count", sa.BigInteger()))
        batch_op.add_column(sa.Column("pcm_loudness_gated_block_count", sa.BigInteger()))
        batch_op.add_column(sa.Column("pcm_integrated_lufs", sa.Float()))
        batch_op.add_column(
            sa.Column("pcm_loudness_unavailable_reason", sa.String(length=48))
        )
        batch_op.create_check_constraint(
            op.f("ck_datasets_pcm_loudness_complete_and_bounded"),
            _LOUDNESS_CONSTRAINT,
        )
        batch_op.create_check_constraint(
            op.f("ck_datasets_pcm_loudness_file_count_within_pcm"),
            "pcm_loudness_analyzed_file_count IS NULL OR "
            "(pcm_validated_file_count IS NOT NULL AND "
            "pcm_loudness_analyzed_file_count <= pcm_validated_file_count)",
        )


def downgrade() -> None:
    with op.batch_alter_table("datasets") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_datasets_pcm_loudness_file_count_within_pcm"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_datasets_pcm_loudness_complete_and_bounded"),
            type_="check",
        )
        batch_op.drop_column("pcm_loudness_unavailable_reason")
        batch_op.drop_column("pcm_integrated_lufs")
        batch_op.drop_column("pcm_loudness_gated_block_count")
        batch_op.drop_column("pcm_loudness_block_count")
        batch_op.drop_column("pcm_loudness_analyzed_file_count")
        batch_op.drop_column("pcm_loudness_algorithm")
