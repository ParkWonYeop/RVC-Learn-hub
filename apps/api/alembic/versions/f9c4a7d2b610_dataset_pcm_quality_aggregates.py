"""add authoritative Dataset PCM quality aggregates

Revision ID: f9c4a7d2b610
Revises: e2f8b4c6a930
Create Date: 2026-07-11 23:50:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9c4a7d2b610"
down_revision: str | Sequence[str] | None = "e2f8b4c6a930"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("datasets") as batch_op:
        batch_op.add_column(sa.Column("source_file_entry_count", sa.Integer()))
        batch_op.add_column(sa.Column("skipped_file_count", sa.Integer()))
        batch_op.add_column(sa.Column("rejected_file_count", sa.Integer()))
        batch_op.add_column(sa.Column("duplicate_file_count", sa.Integer()))
        batch_op.add_column(sa.Column("pcm_quality_algorithm", sa.String(length=32)))
        batch_op.add_column(sa.Column("pcm_validated_file_count", sa.Integer()))
        batch_op.add_column(sa.Column("pcm_sample_count", sa.BigInteger()))
        batch_op.add_column(sa.Column("pcm_clipping_ratio", sa.Float()))
        batch_op.add_column(sa.Column("pcm_silence_ratio", sa.Float()))
        batch_op.add_column(sa.Column("pcm_rms_ratio", sa.Float()))
        batch_op.add_column(sa.Column("pcm_silence_threshold_dbfs", sa.Float()))
        batch_op.create_check_constraint(
            op.f("ck_datasets_source_file_entry_count_nonnegative"),
            "source_file_entry_count IS NULL OR source_file_entry_count >= 0",
        )
        batch_op.create_check_constraint(
            op.f("ck_datasets_skipped_file_count_nonnegative"),
            "skipped_file_count IS NULL OR skipped_file_count >= 0",
        )
        batch_op.create_check_constraint(
            op.f("ck_datasets_rejected_file_count_nonnegative"),
            "rejected_file_count IS NULL OR rejected_file_count >= 0",
        )
        batch_op.create_check_constraint(
            op.f("ck_datasets_duplicate_file_count_nonnegative"),
            "duplicate_file_count IS NULL OR duplicate_file_count >= 0",
        )
        batch_op.create_check_constraint(
            op.f("ck_datasets_pcm_quality_complete_and_bounded"),
            "(pcm_quality_algorithm IS NULL AND pcm_validated_file_count IS NULL "
            "AND pcm_sample_count IS NULL AND pcm_clipping_ratio IS NULL "
            "AND pcm_silence_ratio IS NULL AND pcm_rms_ratio IS NULL "
            "AND pcm_silence_threshold_dbfs IS NULL) OR "
            "(pcm_quality_algorithm = 'pcm-sample-weighted-v1' "
            "AND pcm_validated_file_count IS NOT NULL "
            "AND pcm_sample_count IS NOT NULL "
            "AND pcm_clipping_ratio IS NOT NULL "
            "AND pcm_silence_ratio IS NOT NULL "
            "AND pcm_rms_ratio IS NOT NULL "
            "AND pcm_silence_threshold_dbfs IS NOT NULL "
            "AND pcm_validated_file_count > 0 AND pcm_sample_count > 0 "
            "AND pcm_clipping_ratio >= 0 AND pcm_clipping_ratio <= 1 "
            "AND pcm_silence_ratio >= 0 AND pcm_silence_ratio <= 1 "
            "AND pcm_rms_ratio >= 0 AND pcm_rms_ratio <= 1 "
            "AND pcm_silence_threshold_dbfs >= -120 "
            "AND pcm_silence_threshold_dbfs < 0)",
        )
        batch_op.create_check_constraint(
            op.f("ck_datasets_pcm_validated_file_count_within_dataset"),
            "pcm_validated_file_count IS NULL OR "
            "(file_count IS NOT NULL AND pcm_validated_file_count <= file_count)",
        )


def downgrade() -> None:
    with op.batch_alter_table("datasets") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_datasets_pcm_validated_file_count_within_dataset"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_datasets_pcm_quality_complete_and_bounded"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_datasets_duplicate_file_count_nonnegative"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_datasets_rejected_file_count_nonnegative"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_datasets_skipped_file_count_nonnegative"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_datasets_source_file_entry_count_nonnegative"),
            type_="check",
        )
        batch_op.drop_column("pcm_silence_threshold_dbfs")
        batch_op.drop_column("pcm_rms_ratio")
        batch_op.drop_column("pcm_silence_ratio")
        batch_op.drop_column("pcm_clipping_ratio")
        batch_op.drop_column("pcm_sample_count")
        batch_op.drop_column("pcm_validated_file_count")
        batch_op.drop_column("pcm_quality_algorithm")
        batch_op.drop_column("duplicate_file_count")
        batch_op.drop_column("rejected_file_count")
        batch_op.drop_column("skipped_file_count")
        batch_op.drop_column("source_file_entry_count")
