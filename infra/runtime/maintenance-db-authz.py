#!/usr/bin/env python3
"""Provision and verify the exact PostgreSQL maintenance authorization boundary."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

ADMIN_PASSWORD_FILE = Path("/run/secrets/current/postgres_password")
MAINTENANCE_PASSWORD_FILE = Path(
    "/run/secrets/current/maintenance_postgres_password"
)
FUNCTION_OWNER_ROLE = "rvc_maintenance_function_owner"
LOCK_FUNCTIONS = (
    "public.rvc_maintenance_lock_dataset_parent(text)",
    "public.rvc_maintenance_lock_test_set_parent(text)",
)
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
MAX_SECRET_BYTES = 16 * 1024

RUN_SELECT = frozenset(
    {
        "id",
        "task_name",
        "job_id",
        "dry_run",
        "status",
        "attempt_count",
        "max_attempts",
        "result_json",
        "started_at",
        "heartbeat_at",
        "created_at",
        "updated_at",
    }
)
RUN_UPDATE = frozenset(
    {
        "status",
        "attempt_count",
        "result_json",
        "last_error_code",
        "started_at",
        "heartbeat_at",
        "completed_at",
        "updated_at",
    }
)
UPLOAD_COMMON_SELECT = frozenset(
    {
        "id",
        "generation",
        "temporary_object_key",
        "storage_backend",
        "storage_namespace_sha256",
        "status",
        "upload_write_token",
        "upload_heartbeat_at",
        "finalization_token",
        "finalization_heartbeat_at",
        "expires_at",
        "failure_code",
        "cleanup_claim_run_id",
        "cleanup_claimed_at",
        "cleanup_claim_generation",
        "cleanup_first_deleted_at",
        "cleanup_completed_at",
        "created_at",
        "updated_at",
    }
)
UPLOAD_UPDATE = frozenset(
    {
        "status",
        "upload_write_token",
        "upload_heartbeat_at",
        "finalization_token",
        "finalization_heartbeat_at",
        "failure_code",
        "cleanup_claim_run_id",
        "cleanup_claimed_at",
        "cleanup_claim_generation",
        "cleanup_first_deleted_at",
        "cleanup_completed_at",
        "updated_at",
    }
)
AUDIT_INSERT = frozenset(
    {
        "id",
        "actor_type",
        "actor_id",
        "action",
        "resource_type",
        "resource_id",
        "details_json",
        "occurred_at",
    }
)

MAINTENANCE_COLUMN_PRIVILEGES: dict[str, dict[str, frozenset[str]]] = {
    "maintenance_task_runs": {"SELECT": RUN_SELECT, "UPDATE": RUN_UPDATE},
    "dataset_upload_sessions": {
        "SELECT": UPLOAD_COMMON_SELECT | {"dataset_id"},
        "UPDATE": UPLOAD_UPDATE,
    },
    "test_set_item_upload_sessions": {
        "SELECT": UPLOAD_COMMON_SELECT | {"test_set_id"},
        "UPDATE": UPLOAD_UPDATE,
    },
    "audit_events": {"INSERT": AUDIT_INSERT},
}
FUNCTION_OWNER_COLUMN_PRIVILEGES: dict[str, dict[str, frozenset[str]]] = {
    "dataset_upload_sessions": {"SELECT": frozenset({"id", "dataset_id"})},
    "datasets": {
        "SELECT": frozenset({"id"}),
        "UPDATE": frozenset({"id"}),
    },
    "test_set_item_upload_sessions": {
        "SELECT": frozenset({"id", "test_set_id"})
    },
    "test_sets": {
        "SELECT": frozenset({"id"}),
        "UPDATE": frozenset({"id"}),
    },
}


class Connection(Protocol):
    async def execute(self, query: str, *args: object) -> str: ...

    async def fetch(self, query: str, *args: object) -> list[Any]: ...

    async def fetchrow(self, query: str, *args: object) -> Any: ...

    async def fetchval(self, query: str, *args: object) -> Any: ...

    def transaction(self) -> Any: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Configuration:
    host: str
    port: int
    database: str
    admin_user: str | None
    maintenance_user: str
    task_timeout_seconds: int

    @classmethod
    def from_environment(cls, *, require_admin: bool) -> Configuration:
        host = os.environ.get("POSTGRES_HOST", "postgres")
        if not HOST.fullmatch(host) or ".." in host:
            raise ValueError("POSTGRES_HOST is invalid")
        try:
            port = int(os.environ.get("POSTGRES_PORT", "5432"))
            timeout = int(os.environ.get("MAINTENANCE_TASK_TIMEOUT_SECONDS", "300"))
        except ValueError as exc:
            raise ValueError("PostgreSQL port or maintenance timeout is invalid") from exc
        if not 1 <= port <= 65535 or not 30 <= timeout <= 3600:
            raise ValueError("PostgreSQL port or maintenance timeout is out of range")
        database = _identifier(os.environ.get("POSTGRES_DB", "rvc_orchestrator"), "POSTGRES_DB")
        maintenance_user = _identifier(
            os.environ.get("MAINTENANCE_POSTGRES_USER", "rvc_maintenance"),
            "MAINTENANCE_POSTGRES_USER",
        )
        admin_value = os.environ.get("POSTGRES_USER")
        admin_user = _identifier(admin_value, "POSTGRES_USER") if admin_value else None
        if require_admin and admin_user is None:
            raise ValueError("POSTGRES_USER is required in apply mode")
        if admin_user == maintenance_user or maintenance_user == FUNCTION_OWNER_ROLE:
            raise ValueError("PostgreSQL authorization roles must be distinct")
        return cls(host, port, database, admin_user, maintenance_user, timeout)


def _identifier(value: str | None, name: str) -> str:
    if value is None or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} is invalid")
    return value


def _quote_identifier(value: str) -> str:
    _identifier(value, "PostgreSQL identifier")
    return f'"{value}"'


def _read_secret(path: Path) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("PostgreSQL credential is not a regular file")
        if (
            metadata.st_uid != 10001
            or metadata.st_gid != 10001
            or stat.S_IMODE(metadata.st_mode) != 0o400
        ):
            raise ValueError("PostgreSQL credential ownership or mode is invalid")
        if metadata.st_size <= 0 or metadata.st_size > MAX_SECRET_BYTES:
            raise ValueError("PostgreSQL credential size is invalid")
        value = os.read(descriptor, MAX_SECRET_BYTES + 1)
    finally:
        os.close(descriptor)
    value = value.replace(b"\r", b"").replace(b"\n", b"")
    if not value or len(value) > MAX_SECRET_BYTES or b"\x00" in value:
        raise ValueError("PostgreSQL credential content is invalid")
    return value.decode("utf-8", errors="strict")


async def _connect(config: Configuration, *, user: str, password: str) -> Connection:
    import asyncpg  # type: ignore[import-untyped]

    return cast(
        Connection,
        await asyncpg.connect(
            host=config.host,
            port=config.port,
            database=config.database,
            user=user,
            password=password,
            ssl=False,
            timeout=10,
            command_timeout=min(30, config.task_timeout_seconds),
            server_settings={"application_name": "rvc-maintenance-db-authz"},
        ),
    )


async def _ensure_role(
    connection: Connection,
    *,
    role: str,
    login: bool,
    connection_limit: int,
) -> None:
    quoted = _quote_identifier(role)
    exists = await connection.fetchval("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = $1", role)
    if exists is None:
        await connection.execute(f"CREATE ROLE {quoted}")
    login_clause = "LOGIN" if login else "NOLOGIN"
    await connection.execute(
        f"ALTER ROLE {quoted} WITH {login_clause} NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS "
        f"CONNECTION LIMIT {connection_limit}"
    )
    memberships = await connection.fetch(
        """
        SELECT parent.rolname
          FROM pg_catalog.pg_auth_members AS membership
          JOIN pg_catalog.pg_roles AS parent ON parent.oid = membership.roleid
          JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
         WHERE member.rolname = $1
        """,
        role,
    )
    for membership in memberships:
        parent = _identifier(str(membership["rolname"]), "inherited role")
        await connection.execute(f"REVOKE {_quote_identifier(parent)} FROM {quoted}")


async def _set_password(connection: Connection, *, role: str, password: str) -> None:
    statement = await connection.fetchval(
        "SELECT pg_catalog.format('ALTER ROLE %I PASSWORD %L', $1::text, $2::text)",
        role,
        password,
    )
    if not isinstance(statement, str) or not statement.startswith("ALTER ROLE "):
        raise RuntimeError("could not construct PostgreSQL password statement")
    await connection.execute(statement)


async def _public_tables(connection: Connection) -> dict[str, frozenset[str]]:
    rows = await connection.fetch(
        """
        SELECT table_name, column_name
          FROM information_schema.columns
         WHERE table_schema = 'public'
         ORDER BY table_name, ordinal_position
        """
    )
    tables: dict[str, set[str]] = {}
    for row in rows:
        table = _identifier(str(row["table_name"]), "table name")
        column = _identifier(str(row["column_name"]), "column name")
        tables.setdefault(table, set()).add(column)
    return {table: frozenset(columns) for table, columns in tables.items()}


def _columns(columns: frozenset[str]) -> str:
    if not columns:
        raise ValueError("column privilege set must not be empty")
    return ", ".join(_quote_identifier(column) for column in sorted(columns))


async def _revoke_role_acl(
    connection: Connection,
    *,
    role: str,
    tables: dict[str, frozenset[str]],
) -> None:
    quoted_role = _quote_identifier(role)
    await connection.execute(f"REVOKE ALL ON SCHEMA public FROM {quoted_role}")
    await connection.execute(
        f"REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM {quoted_role}"
    )
    await connection.execute(
        f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {quoted_role}"
    )
    for table, columns in tables.items():
        qualified = f"public.{_quote_identifier(table)}"
        await connection.execute(
            f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM {quoted_role}"
        )
        await connection.execute(
            f"REVOKE ALL PRIVILEGES ({_columns(columns)}) ON TABLE {qualified} "
            f"FROM {quoted_role}"
        )


async def _grant_column_acl(
    connection: Connection,
    *,
    role: str,
    privileges: dict[str, dict[str, frozenset[str]]],
) -> None:
    quoted_role = _quote_identifier(role)
    await connection.execute(f"GRANT USAGE ON SCHEMA public TO {quoted_role}")
    for table, grants in privileges.items():
        qualified = f"public.{_quote_identifier(table)}"
        for privilege, columns in grants.items():
            await connection.execute(
                f"GRANT {privilege} ({_columns(columns)}) ON TABLE {qualified} "
                f"TO {quoted_role}"
            )


async def _verify_role_attributes(
    connection: Connection,
    *,
    role: str,
    login: bool,
) -> None:
    attributes = await connection.fetchrow(
        """
        SELECT rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,
               rolreplication, rolbypassrls
          FROM pg_catalog.pg_roles
         WHERE rolname = $1
        """,
        role,
    )
    expected = (False, False, False, False, login, False, False)
    if attributes is None or tuple(attributes.values()) != expected:
        raise RuntimeError(f"PostgreSQL role attributes are unsafe: {role}")
    memberships = await connection.fetchval(
        """
        SELECT pg_catalog.count(*)
          FROM pg_catalog.pg_auth_members AS membership
          JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
         WHERE member.rolname = $1
        """,
        role,
    )
    if memberships != 0:
        raise RuntimeError(f"PostgreSQL role inherits an unexpected role: {role}")


async def _verify_exact_column_acl(
    connection: Connection,
    *,
    role: str,
    tables: dict[str, frozenset[str]],
    expected: dict[str, dict[str, frozenset[str]]],
) -> None:
    for table, columns in tables.items():
        qualified = f"public.{table}"
        expected_table = expected.get(table, {})
        for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
            wanted = expected_table.get(privilege, frozenset())
            for column in columns:
                actual = await connection.fetchval(
                    "SELECT pg_catalog.has_column_privilege($1, $2, $3, $4)",
                    role,
                    qualified,
                    column,
                    privilege,
                )
                if bool(actual) != (column in wanted):
                    raise RuntimeError(
                        f"unexpected {privilege} privilege on {qualified}.{column}"
                    )
        for privilege in ("DELETE", "TRUNCATE", "TRIGGER"):
            actual = await connection.fetchval(
                "SELECT pg_catalog.has_table_privilege($1, $2, $3)",
                role,
                qualified,
                privilege,
            )
            if actual is True:
                raise RuntimeError(f"unexpected {privilege} privilege on {qualified}")


async def _verify_functions(
    connection: Connection,
    *,
    maintenance_role: str,
) -> None:
    for signature in LOCK_FUNCTIONS:
        row = await connection.fetchrow(
            """
            SELECT owner.rolname AS owner_name,
                   procedure.prosecdef,
                   procedure.proconfig,
                   pg_catalog.has_function_privilege($1, procedure.oid, 'EXECUTE') AS can_execute,
                   ARRAY(
                       SELECT COALESCE(grantee.rolname, 'PUBLIC')
                         FROM pg_catalog.aclexplode(
                              COALESCE(
                                  procedure.proacl,
                                  pg_catalog.acldefault('f', procedure.proowner)
                              )
                         ) AS acl
                         LEFT JOIN pg_catalog.pg_roles AS grantee
                           ON grantee.oid = acl.grantee
                        WHERE acl.privilege_type = 'EXECUTE'
                        ORDER BY COALESCE(grantee.rolname, 'PUBLIC')
                   ) AS execute_grantees
              FROM pg_catalog.pg_proc AS procedure
              JOIN pg_catalog.pg_roles AS owner ON owner.oid = procedure.proowner
             WHERE procedure.oid = pg_catalog.to_regprocedure($2)
            """,
            maintenance_role,
            signature,
        )
        if row is None:
            raise RuntimeError(f"maintenance lock function is missing: {signature}")
        config = set(row["proconfig"] or [])
        execute_grantees = set(row["execute_grantees"] or [])
        if (
            row["owner_name"] != FUNCTION_OWNER_ROLE
            or row["prosecdef"] is not True
            or config != {"search_path=pg_catalog, pg_temp"}
            or row["can_execute"] is not True
            # Function ownership is verified separately and is an implicit
            # privilege.  Keep the explicit ACL normalized to the maintenance
            # login alone so first apply and reapply converge identically.
            or execute_grantees != {maintenance_role}
        ):
            raise RuntimeError(f"maintenance lock function is unsafe: {signature}")


async def _verify_no_sequence_acl(connection: Connection, *, role: str) -> None:
    rows = await connection.fetch(
        """
        SELECT sequence_name
          FROM information_schema.sequences
         WHERE sequence_schema = 'public'
        """
    )
    for row in rows:
        sequence = f"public.{_identifier(str(row['sequence_name']), 'sequence name')}"
        for privilege in ("SELECT", "UPDATE", "USAGE"):
            if await connection.fetchval(
                "SELECT pg_catalog.has_sequence_privilege($1, $2, $3)",
                role,
                sequence,
                privilege,
            ):
                raise RuntimeError(f"unexpected {privilege} privilege on {sequence}")


async def _verify_boundary(
    connection: Connection,
    *,
    config: Configuration,
    require_current_user: bool,
) -> None:
    if require_current_user:
        current_user = await connection.fetchval("SELECT current_user")
        if current_user != config.maintenance_user:
            raise RuntimeError("maintenance database login identity mismatch")
    await _verify_role_attributes(
        connection,
        role=config.maintenance_user,
        login=True,
    )
    await _verify_role_attributes(
        connection,
        role=FUNCTION_OWNER_ROLE,
        login=False,
    )
    tables = await _public_tables(connection)
    missing = set(MAINTENANCE_COLUMN_PRIVILEGES) - set(tables)
    if missing:
        raise RuntimeError("maintenance authorization tables are missing")
    await _verify_exact_column_acl(
        connection,
        role=config.maintenance_user,
        tables=tables,
        expected=MAINTENANCE_COLUMN_PRIVILEGES,
    )
    await _verify_exact_column_acl(
        connection,
        role=FUNCTION_OWNER_ROLE,
        tables=tables,
        expected=FUNCTION_OWNER_COLUMN_PRIVILEGES,
    )
    if not await connection.fetchval(
        "SELECT pg_catalog.has_database_privilege($1, $2, 'CONNECT')",
        config.maintenance_user,
        config.database,
    ):
        raise RuntimeError("maintenance role lacks database CONNECT")
    if await connection.fetchval(
        "SELECT pg_catalog.has_database_privilege($1, $2, 'TEMPORARY')",
        config.maintenance_user,
        config.database,
    ):
        raise RuntimeError("maintenance role has database TEMPORARY")
    if await connection.fetchval(
        "SELECT pg_catalog.has_schema_privilege($1, 'public', 'CREATE')",
        config.maintenance_user,
    ):
        raise RuntimeError("maintenance role has schema CREATE")
    await _verify_no_sequence_acl(connection, role=config.maintenance_user)
    await _verify_functions(connection, maintenance_role=config.maintenance_user)


async def apply(config: Configuration) -> None:
    if config.admin_user is None:
        raise ValueError("POSTGRES_USER is required in apply mode")
    admin_password = _read_secret(ADMIN_PASSWORD_FILE)
    maintenance_password = _read_secret(MAINTENANCE_PASSWORD_FILE)
    connection = await _connect(config, user=config.admin_user, password=admin_password)
    try:
        async with connection.transaction():
            await _ensure_role(
                connection,
                role=config.maintenance_user,
                login=True,
                connection_limit=16,
            )
            await _set_password(
                connection,
                role=config.maintenance_user,
                password=maintenance_password,
            )
            await _ensure_role(
                connection,
                role=FUNCTION_OWNER_ROLE,
                login=False,
                connection_limit=-1,
            )
            quoted_database = _quote_identifier(config.database)
            quoted_admin = _quote_identifier(config.admin_user)
            quoted_maintenance = _quote_identifier(config.maintenance_user)
            await connection.execute(
                f"REVOKE CONNECT, TEMPORARY ON DATABASE {quoted_database} FROM PUBLIC"
            )
            await connection.execute(
                f"GRANT CONNECT ON DATABASE {quoted_database} TO {quoted_admin}, "
                f"{quoted_maintenance}"
            )
            await connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            tables = await _public_tables(connection)
            for role in (config.maintenance_user, FUNCTION_OWNER_ROLE):
                await _revoke_role_acl(connection, role=role, tables=tables)
            for signature in LOCK_FUNCTIONS:
                exists = await connection.fetchval(
                    "SELECT pg_catalog.to_regprocedure($1)", signature
                )
                if exists is None:
                    raise RuntimeError(f"maintenance lock function is missing: {signature}")
                await connection.execute(
                    f"ALTER FUNCTION {signature} OWNER TO "
                    f"{_quote_identifier(FUNCTION_OWNER_ROLE)}"
                )
                grantees = await connection.fetch(
                    """
                    SELECT grantee.rolname
                      FROM pg_catalog.pg_proc AS procedure
                      CROSS JOIN LATERAL pg_catalog.aclexplode(
                          COALESCE(
                              procedure.proacl,
                              pg_catalog.acldefault('f', procedure.proowner)
                          )
                      ) AS acl
                      JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
                     WHERE procedure.oid = pg_catalog.to_regprocedure($1)
                       AND acl.privilege_type = 'EXECUTE'
                    """,
                    signature,
                )
                for grantee in grantees:
                    role = _identifier(str(grantee["rolname"]), "function grantee")
                    await connection.execute(
                        f"REVOKE ALL ON FUNCTION {signature} FROM "
                        f"{_quote_identifier(role)}"
                    )
                await connection.execute(
                    f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC"
                )
                await connection.execute(
                    f"GRANT EXECUTE ON FUNCTION {signature} TO {quoted_maintenance}"
                )
            await _grant_column_acl(
                connection,
                role=config.maintenance_user,
                privileges=MAINTENANCE_COLUMN_PRIVILEGES,
            )
            await _grant_column_acl(
                connection,
                role=FUNCTION_OWNER_ROLE,
                privileges=FUNCTION_OWNER_COLUMN_PRIVILEGES,
            )
            await connection.execute(
                f"ALTER ROLE {quoted_maintenance} IN DATABASE {quoted_database} "
                "SET search_path = pg_catalog, public"
            )
            await connection.execute(
                f"ALTER ROLE {quoted_maintenance} IN DATABASE {quoted_database} "
                f"SET statement_timeout = '{config.task_timeout_seconds}s'"
            )
            await connection.execute(
                f"ALTER ROLE {quoted_maintenance} IN DATABASE {quoted_database} "
                "SET idle_in_transaction_session_timeout = '60s'"
            )
            await _verify_boundary(
                connection,
                config=config,
                require_current_user=False,
            )
    finally:
        await connection.close()
    await verify_runtime(config, maintenance_password=maintenance_password)


async def verify_runtime(
    config: Configuration,
    *,
    maintenance_password: str | None = None,
) -> None:
    password = maintenance_password or _read_secret(MAINTENANCE_PASSWORD_FILE)
    connection = await _connect(config, user=config.maintenance_user, password=password)
    try:
        await _verify_boundary(
            connection,
            config=config,
            require_current_user=True,
        )
    finally:
        await connection.close()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("apply", "verify-runtime"))
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    config = Configuration.from_environment(require_admin=arguments.mode == "apply")
    if arguments.mode == "apply":
        asyncio.run(apply(config))
    else:
        asyncio.run(verify_runtime(config))


if __name__ == "__main__":
    main()
