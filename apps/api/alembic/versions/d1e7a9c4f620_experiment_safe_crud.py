"""add safe Experiment mutation and deletion constraints

Revision ID: d1e7a9c4f620
Revises: a6c2e9f4b710
Create Date: 2026-07-12 00:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1e7a9c4f620"
down_revision: str | Sequence[str] | None = "a6c2e9f4b710"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("experiments") as batch_op:
        batch_op.add_column(
            sa.Column("row_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column("name_conflict_key", sa.String(length=128), nullable=True)
        )
        batch_op.create_check_constraint(
            op.f("ck_experiments_name_conflict_key_matches_name"),
            "name_conflict_key IS NULL OR name_conflict_key = name",
        )
        batch_op.create_unique_constraint(
            "uq_experiments_owner_name_conflict_key",
            ["created_by", "name_conflict_key"],
        )

    # Preserve every historical Experiment and Job relationship. Only names
    # that were already unique within a non-null owner namespace are bound to
    # the new conflict key. Historical duplicate/null-owner groups remain NULL
    # and are rejected by the API's pre-insert lookup without destructive data
    # cleanup.
    op.execute(
        sa.text(
            """
            UPDATE experiments
            SET name_conflict_key = name
            WHERE created_by IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM experiments AS duplicate
                  WHERE duplicate.created_by = experiments.created_by
                    AND duplicate.name = experiments.name
                    AND duplicate.id <> experiments.id
              )
            """
        )
    )

    with op.batch_alter_table("experiments") as batch_op:
        batch_op.alter_column(
            "row_version",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=None,
        )

    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_jobs_experiment_id_experiments"),
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            op.f("fk_jobs_experiment_id_experiments"),
            "experiments",
            ["experiment_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_jobs_experiment_id_experiments"),
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            op.f("fk_jobs_experiment_id_experiments"),
            "experiments",
            ["experiment_id"],
            ["id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("experiments") as batch_op:
        batch_op.drop_constraint(
            "uq_experiments_owner_name_conflict_key",
            type_="unique",
        )
        batch_op.drop_constraint(
            op.f("ck_experiments_name_conflict_key_matches_name"),
            type_="check",
        )
        batch_op.drop_column("name_conflict_key")
        batch_op.drop_column("row_version")
