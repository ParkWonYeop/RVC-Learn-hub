from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "infra/runtime/maintenance-db-authz.py"


def _load_script() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "maintenance_db_authz",
        SCRIPT_PATH,
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def test_maintenance_db_authz_contract_is_exact_and_secret_free() -> None:
    module = _load_script()
    maintenance = module.MAINTENANCE_COLUMN_PRIVILEGES
    assert set(maintenance) == {
        "maintenance_task_runs",
        "dataset_upload_sessions",
        "test_set_item_upload_sessions",
        "audit_events",
    }
    assert maintenance["audit_events"] == {"INSERT": module.AUDIT_INSERT}
    assert "queued_at" not in module.RUN_UPDATE
    assert "DELETE" not in repr(maintenance)
    assert "canonical_object_key" not in repr(maintenance)
    assert "original_object_key" not in repr(maintenance)
    assert "users" not in maintenance
    assert "jobs" not in maintenance
    assert "artifacts" not in maintenance
    assert "model_registry_entries" not in maintenance
    assert module.FUNCTION_OWNER_COLUMN_PRIVILEGES["datasets"] == {
        "SELECT": frozenset({"id"}),
        "UPDATE": frozenset({"id"}),
    }
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "verify-runtime" in source
    assert "REVOKE ALL ON FUNCTION" in source
    assert "ALTER FUNCTION" in source and "OWNER TO" in source
    assert "PUBLIC" in source
    assert "PASSWORD %L" in source
    assert "metadata.st_uid != 10001" in source
    assert "metadata.st_gid != 10001" in source


def test_maintenance_db_authz_configuration_rejects_role_or_host_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    monkeypatch.setenv("POSTGRES_USER", "rvc_manager")
    monkeypatch.setenv("MAINTENANCE_POSTGRES_USER", "rvc_maintenance")
    config = module.Configuration.from_environment(require_admin=True)
    assert config.database == "rvc_orchestrator"
    assert config.maintenance_user == "rvc_maintenance"

    monkeypatch.setenv("MAINTENANCE_POSTGRES_USER", "rvc_maintenance;drop_role")
    with pytest.raises(ValueError, match="MAINTENANCE_POSTGRES_USER"):
        module.Configuration.from_environment(require_admin=True)
    monkeypatch.setenv("MAINTENANCE_POSTGRES_USER", "rvc_maintenance")
    monkeypatch.setenv("POSTGRES_HOST", "postgres/unsafe")
    with pytest.raises(ValueError, match="POSTGRES_HOST"):
        module.Configuration.from_environment(require_admin=True)


def test_maintenance_db_authz_secret_reader_rejects_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    target = tmp_path / "credential"
    target.write_text("safe-password\n", encoding="utf-8")
    target.chmod(0o400)
    link = tmp_path / "credential-link"
    link.symlink_to(target)

    real_fstat = os.fstat

    def projected_fstat(descriptor: int) -> SimpleNamespace:
        metadata = real_fstat(descriptor)
        return SimpleNamespace(
            st_mode=metadata.st_mode,
            st_uid=10001,
            st_gid=10001,
            st_size=metadata.st_size,
        )

    monkeypatch.setattr(module.os, "fstat", projected_fstat)
    assert module._read_secret(target) == "safe-password"
    with pytest.raises(OSError):
        module._read_secret(link)


class _PasswordConnection:
    def __init__(self) -> None:
        self.fetch_arguments: tuple[object, ...] | None = None
        self.executed: list[str] = []

    async def fetchval(self, query: str, *args: object) -> str:
        self.fetch_arguments = args
        assert "%I" in query and "%L" in query
        return 'ALTER ROLE "rvc_maintenance" PASSWORD \'redacted-by-server\''

    async def execute(self, query: str, *args: object) -> str:
        assert not args
        self.executed.append(query)
        return "ALTER ROLE"


@pytest.mark.asyncio
async def test_password_ddl_is_formatted_by_postgresql_not_interpolated() -> None:
    module = _load_script()
    connection = _PasswordConnection()
    secret = "operator-provided-secret"
    await module._set_password(
        connection,
        role="rvc_maintenance",
        password=secret,
    )
    assert connection.fetch_arguments == ("rvc_maintenance", secret)
    assert all(secret not in statement for statement in connection.executed)


def test_maintenance_heartbeat_configuration_is_bounded() -> None:
    from rvc_manager_api.config import Settings

    assert Settings().maintenance_task_heartbeat_seconds == 15
    with pytest.raises(ValueError, match="MAINTENANCE_TASK_HEARTBEAT_SECONDS"):
        Settings(
            maintenance_task_timeout_seconds=30,
            maintenance_task_heartbeat_seconds=30,
        )
