"""add least-privilege maintenance parent row-lock functions

Revision ID: f5d1c8a9b240
Revises: e4c7b9d2f610
Create Date: 2026-07-13 03:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f5d1c8a9b240"
down_revision: str | Sequence[str] | None = "e4c7b9d2f610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DATASET_LOCK_FUNCTION = "rvc_maintenance_lock_dataset_parent"
TEST_SET_LOCK_FUNCTION = "rvc_maintenance_lock_test_set_parent"


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgresql():
        return

    # These functions are the only path by which the maintenance login may
    # acquire a parent Dataset/TestSet row lock.  They derive the parent from
    # a server-created upload UUID, use no dynamic SQL, and name every object
    # outside the fixed pg_catalog-only search path explicitly.
    op.execute(
        f"""
        CREATE FUNCTION public.{DATASET_LOCK_FUNCTION}(p_upload_id text)
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$
        DECLARE
            parent_id text;
        BEGIN
            SELECT upload.dataset_id
              INTO parent_id
              FROM public.dataset_upload_sessions AS upload
             WHERE upload.id = p_upload_id;
            IF parent_id IS NULL THEN
                RETURN NULL;
            END IF;

            PERFORM 1
              FROM public.datasets AS dataset
             WHERE dataset.id = parent_id
               FOR UPDATE;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;
            PERFORM 1
              FROM public.dataset_upload_sessions AS upload
             WHERE upload.id = p_upload_id
               AND upload.dataset_id = parent_id;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;
            RETURN parent_id;
        END;
        $function$;
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION public.{DATASET_LOCK_FUNCTION}(text) FROM PUBLIC"
    )
    op.execute(
        f"""
        CREATE FUNCTION public.{TEST_SET_LOCK_FUNCTION}(p_upload_id text)
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$
        DECLARE
            parent_id text;
        BEGIN
            SELECT upload.test_set_id
              INTO parent_id
              FROM public.test_set_item_upload_sessions AS upload
             WHERE upload.id = p_upload_id;
            IF parent_id IS NULL THEN
                RETURN NULL;
            END IF;

            PERFORM 1
              FROM public.test_sets AS test_set
             WHERE test_set.id = parent_id
               FOR UPDATE;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;
            PERFORM 1
              FROM public.test_set_item_upload_sessions AS upload
             WHERE upload.id = p_upload_id
               AND upload.test_set_id = parent_id;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;
            RETURN parent_id;
        END;
        $function$;
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION public.{TEST_SET_LOCK_FUNCTION}(text) FROM PUBLIC"
    )


def downgrade() -> None:
    if not _is_postgresql():
        return
    op.execute(f"DROP FUNCTION public.{TEST_SET_LOCK_FUNCTION}(text)")
    op.execute(f"DROP FUNCTION public.{DATASET_LOCK_FUNCTION}(text)")
