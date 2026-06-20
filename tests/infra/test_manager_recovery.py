from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.parse import parse_qsl

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MANAGER_INSTALLERS = ROOT / "installers" / "manager"


FAKE_COMPOSE = r"""#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import sys

args = sys.argv[1:]
state = pathlib.Path(os.environ["FAKE_COMPOSE_STATE"])
state.mkdir(parents=True, exist_ok=True)
with (state / "commands.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\n")
joined = "\0".join(args)

if args and args[0] == "stop" and os.environ.get("FAKE_STOP_FAILURE_ONCE") == "1":
    marker = state / "stop-failed-once"
    if not marker.exists():
        marker.write_text("failed", encoding="utf-8")
        sys.exit(44)

if args[:3] == ["exec", "-T", "postgres"]:
    if "pg_dump" in joined:
        payload = b"MLFLOW_CUSTOM_DUMP" if "MLFLOW_POSTGRES" in joined else b"MANAGER_CUSTOM_DUMP"
        sys.stdout.buffer.write(payload)
    elif "pg_restore" in joined:
        if os.environ.get("FAKE_RESTORE_FAILURE") == "1":
            sys.exit(43)
        name = (
            "restored-mlflow.pgdump"
            if "MLFLOW_POSTGRES" in joined
            else "restored-manager.pgdump"
        )
        (state / name).write_bytes(sys.stdin.buffer.read())
    elif "to_regclass" in joined:
        print("t" if os.environ.get("FAKE_ACTIVE_UPLOADS") else "f")
    elif "pending" in joined and "finalizing" in joined:
        print(os.environ.get("FAKE_ACTIVE_UPLOADS", "0"))
    sys.exit(0)

if args[:3] == ["exec", "-T", "api"] and "alembic" in args:
    if "heads" in args or "current" in args:
        revision = "a4f8c2d9137e"
        mismatch_version = os.environ.get("FAKE_MISMATCH_SCHEMA_VERSION")
        if (
            "heads" in args
            and mismatch_version
            and os.environ.get("ORCHESTRATOR_VERSION") == mismatch_version
        ):
            revision = "different_target_head"
        print(f"{revision} (head)")
    sys.exit(0)

if args[:2] == ["config", "--services"]:
    print("postgres\nredis\nminio\nminio-init\nmlflow\napi-migrate\napi\nweb\nproxy")
    sys.exit(0)

if args[:3] == ["ps", "--format", "json"]:
    print(
        json.dumps(
            [
                {"Service": service, "State": "running", "Health": "healthy"}
                for service in ("postgres", "redis", "minio", "mlflow", "api", "web", "proxy")
            ]
        )
    )
    sys.exit(0)

if args and args[0] == "run" and "object-recovery" in args:
    volume = args[args.index("--volume") + 1]
    if ":/snapshot" in volume and ":/snapshot:ro" not in volume:
        if os.environ.get("FAKE_MINIO_BACKUP_FAILURE") == "1":
            sys.exit(42)
        host = pathlib.Path(volume.split(":/snapshot", 1)[0])
        (host / "data" / "manager").mkdir(parents=True, exist_ok=True)
        (host / "data" / "mlflow").mkdir(parents=True, exist_ok=True)
        manager_body = b"manager-object"
        mlflow_body = b"mlflow-object"
        (host / "data" / "manager" / "model.bin").write_bytes(manager_body)
        (host / "data" / "mlflow" / "run.bin").write_bytes(mlflow_body)

        def record(key, data_file, body, metadata, tags):
            return {
                "key": key,
                "data_file": data_file,
                "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "etag": "fake-etag",
                "metadata": metadata,
                "tags": tags,
                "headers": {
                    "cache_control": None,
                    "content_disposition": None,
                    "content_encoding": None,
                    "content_language": None,
                    "content_type": "application/octet-stream",
                    "expires": None,
                    "server_side_encryption": None,
                    "storage_class": "STANDARD",
                    "website_redirect_location": None,
                },
                "source_checksums": {},
                "version_id": None,
            }

        (host / "inventory.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "rvc-s3-object-snapshot",
                    "version_semantics": "unversioned-current-object",
                    "buckets": [
                        {
                            "label": "manager",
                            "bucket": "rvc-orchestrator",
                            "versioning": "disabled",
                            "objects": [
                                record(
                                    "model.pth",
                                    "data/manager/model.bin",
                                    manager_body,
                                    {"verified": "true"},
                                    [{"Key": "type", "Value": "model"}],
                                )
                            ],
                        },
                        {
                            "label": "mlflow",
                            "bucket": "rvc-mlflow",
                            "versioning": "disabled",
                            "objects": [
                                record(
                                    "run.bin",
                                    "data/mlflow/run.bin",
                                    mlflow_body,
                                    {},
                                    [],
                                )
                            ],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
    elif ":/snapshot:ro" in volume:
        host = pathlib.Path(volume.split(":/snapshot:ro", 1)[0])
        (state / "restored-manager-object").write_bytes(
            (host / "data" / "manager" / "model.bin").read_bytes()
        )
        (state / "restored-mlflow-object").write_bytes(
            (host / "data" / "mlflow" / "run.bin").read_bytes()
        )
    sys.exit(0)

if args and args[0] == "run" and "api" in args and "alembic" in args:
    if "heads" in args or "current" in args:
        revision = "a4f8c2d9137e"
        mismatch_version = os.environ.get("FAKE_MISMATCH_SCHEMA_VERSION")
        if (
            "heads" in args
            and mismatch_version
            and os.environ.get("ORCHESTRATOR_VERSION") == mismatch_version
        ):
            revision = "different_target_head"
        print(f"{revision} (head)")
    sys.exit(0)

if args[:3] == ["exec", "-T", "api"] and "python" in args:
    install_root = pathlib.Path(os.environ["RVC_INSTALL_ROOT"])
    version = (install_root / "current" / "VERSION").read_text(encoding="utf-8").strip()
    if version == os.environ.get("FAKE_NOT_READY_VERSION"):
        sys.exit(1)
    sys.exit(0)

sys.exit(0)
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_module(name: str, path: Path) -> object:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write_release(install_root: Path, version: str, compatibility: str) -> Path:
    release = install_root / "releases" / version
    (release / "infra" / "compose").mkdir(parents=True)
    (release / "infra" / "compose" / "manager.compose.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    (release / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (release / "manifest.env").write_text(
        "\n".join(
            (
                "BUNDLE_FORMAT_VERSION=1",
                "PRODUCT=rvc-training-orchestrator",
                "COMPONENT=manager",
                f"VERSION={version}",
                f"SCHEMA_COMPATIBILITY={compatibility}",
                "",
            )
        ),
        encoding="utf-8",
    )
    entries: list[str] = []
    for path in sorted(release.rglob("*")):
        if path.is_file() and path.name != "RELEASE_SHA256SUMS":
            entries.append(f"{_sha256(path)}  {path.relative_to(release)}")
    ledger = release / "RELEASE_SHA256SUMS"
    ledger.write_text("\n".join(entries) + "\n", encoding="utf-8")
    ledger.chmod(0o444)
    return release


def _layout(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    install_root = tmp_path / "install"
    config_root = tmp_path / "config"
    state = tmp_path / "compose-state"
    (install_root / "bin").mkdir(parents=True)
    (install_root / "lib").mkdir()
    (config_root / "secrets").mkdir(parents=True)
    current = _write_release(install_root, "1.2.3", "schema-v1")
    (install_root / "current").symlink_to(current.relative_to(install_root))

    shutil.copy2(ROOT / "installers" / "common" / "lib.sh", install_root / "lib" / "common.sh")
    shutil.copy2(
        ROOT / "installers" / "common" / "image_bundle.py",
        install_root / "lib" / "image_bundle.py",
    )
    shutil.copy2(MANAGER_INSTALLERS / "backup.sh", install_root / "bin" / "backup")
    (install_root / "bin" / "backup").chmod(0o755)
    compose = install_root / "bin" / "manager-compose"
    compose.write_text(FAKE_COMPOSE, encoding="utf-8")
    compose.chmod(0o755)

    (config_root / "manager.env").write_text(
        "\n".join(
            (
                "POSTGRES_DB=rvc_orchestrator",
                "POSTGRES_USER=rvc_manager",
                "MLFLOW_POSTGRES_DB=rvc_mlflow",
                "MLFLOW_POSTGRES_USER=rvc_mlflow",
                "S3_BUCKET=rvc-orchestrator",
                "MLFLOW_S3_BUCKET=rvc-mlflow",
                "",
            )
        ),
        encoding="utf-8",
    )
    for name in (
        "postgres_password",
        "maintenance_postgres_password",
        "mlflow_postgres_password",
        "maintenance_redis_password",
        "maintenance_s3_access_key",
        "maintenance_s3_secret_key",
        "minio_root_user",
        "minio_root_password",
    ):
        (config_root / "secrets" / name).write_text(f"do-not-leak-{name}\n", encoding="utf-8")

    environment = {
        **os.environ,
        "RVC_INSTALL_ALLOW_NON_ROOT": "1",
        "RVC_INSTALL_ROOT": str(install_root),
        "RVC_CONFIG_ROOT": str(config_root),
        "FAKE_COMPOSE_STATE": str(state),
        "RVC_RESTORE_READY_ATTEMPTS": "1",
        "RVC_RESTORE_READY_INTERVAL_SECONDS": "0",
        "RVC_ROLLBACK_READY_ATTEMPTS": "1",
        "RVC_ROLLBACK_READY_INTERVAL_SECONDS": "0",
    }
    return install_root, config_root, state, environment


def _run(
    script: str, arguments: list[str], environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(MANAGER_INSTALLERS / script), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _published_path(result: subprocess.CompletedProcess[str], key: str = "BACKUP_PATH") -> Path:
    prefix = f"{key}="
    values = [
        line.removeprefix(prefix) for line in result.stdout.splitlines() if line.startswith(prefix)
    ]
    assert values, result.stdout + result.stderr
    return Path(values[-1])


def _commands(state: Path) -> list[list[str]]:
    log = state / "commands.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_manager_backup_is_atomic_versioned_and_secret_free(tmp_path: Path) -> None:
    _, config_root, state, environment = _layout(tmp_path)
    destination = tmp_path / "backups"
    environment["RVC_BACKUP_TIMESTAMP"] = "20260711T010101Z"

    result = _run("backup.sh", ["--destination", str(destination)], environment)

    assert result.returncode == 0, result.stdout + result.stderr
    published = _published_path(result)
    assert published.name == "rvc-manager-backup-1.2.3-20260711T010101Z"
    archive = published / f"{published.name}.tar.gz"
    checksum = Path(f"{archive}.sha256")
    assert stat.S_IMODE(published.stat().st_mode) == 0o700
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    expected = checksum.read_text(encoding="utf-8").split()[0]
    assert _sha256(archive) == expected
    with tarfile.open(archive, "r:gz") as bundle:
        names = set(bundle.getnames())
        manifest = bundle.extractfile(f"{published.name}/manifest.env")
        assert manifest is not None
        manifest_text = manifest.read().decode()
    assert f"{published.name}/databases/manager.pgdump" in names
    assert f"{published.name}/objects/data/manager/model.bin" in names
    assert f"{published.name}/objects/inventory.json" in names
    assert "INCLUDES_CONFIG=false" in manifest_text
    assert "INCLUDES_SECRETS=false" in manifest_text
    assert "SCHEMA_COMPATIBILITY=schema-v1" in manifest_text
    assert "CONSISTENCY_MODE=maintenance-quiesced" in manifest_text
    assert "OBJECT_SNAPSHOT_FORMAT=s3-object-inventory-v1" in manifest_text
    assert all("manager.env" not in name and "/secrets/" not in name for name in names)
    observed = result.stdout + result.stderr + json.dumps(_commands(state))
    for secret in (config_root / "secrets").iterdir():
        assert secret.read_text(encoding="utf-8").strip() not in observed


def test_manager_backup_never_publishes_partial_or_overwrites(tmp_path: Path) -> None:
    _, _, _, environment = _layout(tmp_path)
    destination = tmp_path / "backups"
    environment["RVC_BACKUP_TIMESTAMP"] = "20260711T020202Z"
    environment["FAKE_MINIO_BACKUP_FAILURE"] = "1"

    failed = _run("backup.sh", ["--destination", str(destination)], environment)

    assert failed.returncode != 0
    assert list(destination.glob("rvc-manager-backup-*")) == []
    assert list(destination.glob(".*.staging.*")) == []
    assert list(destination.glob(".*.publish-lock")) == []

    environment.pop("FAKE_MINIO_BACKUP_FAILURE")
    first = _run("backup.sh", ["--destination", str(destination)], environment)
    second = _run("backup.sh", ["--destination", str(destination)], environment)
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode != 0
    assert "already exists" in second.stderr
    assert len(list(destination.glob("rvc-manager-backup-*"))) == 1


def test_manager_backup_refuses_active_presigned_upload_sessions(tmp_path: Path) -> None:
    _, _, state, environment = _layout(tmp_path)
    destination = tmp_path / "backups"
    environment["RVC_BACKUP_TIMESTAMP"] = "20260711T020203Z"
    environment["FAKE_ACTIVE_UPLOADS"] = "1"

    refused = _run("backup.sh", ["--destination", str(destination)], environment)

    assert refused.returncode != 0
    assert "active sessions" in refused.stderr
    assert not list(destination.glob("rvc-manager-backup-*"))
    commands = _commands(state)
    assert any(command[0] == "stop" for command in commands)
    assert any(command[:3] == ["up", "-d", "--remove-orphans"] for command in commands)


def test_manager_restore_validates_then_restores_with_safety_backup(tmp_path: Path) -> None:
    _, _, state, environment = _layout(tmp_path)
    destination = tmp_path / "backups"
    environment["RVC_BACKUP_TIMESTAMP"] = "20260711T030303Z"
    original = _run("backup.sh", ["--destination", str(destination)], environment)
    assert original.returncode == 0, original.stdout + original.stderr
    backup = _published_path(original)

    (state / "commands.jsonl").unlink()
    unconfirmed = _run("restore.sh", ["--backup", str(backup)], environment)
    assert unconfirmed.returncode != 0
    assert "--confirm-destructive-restore" in unconfirmed.stderr
    assert _commands(state) == []

    tampered = tmp_path / "tampered"
    shutil.copytree(backup, tampered)
    tampered_archive = next(tampered.glob("*.tar.gz"))
    with tampered_archive.open("ab") as stream:
        stream.write(b"tampered")
    rejected = _run(
        "restore.sh",
        ["--backup", str(tampered), "--confirm-destructive-restore"],
        environment,
    )
    assert rejected.returncode != 0
    assert "checksum mismatch" in rejected.stderr
    assert _commands(state) == []

    environment["RVC_BACKUP_TIMESTAMP"] = "20260711T040404Z"
    pre_restore_destination = tmp_path / "pre-restore"
    environment["FAKE_RESTORE_FAILURE"] = "1"
    failed_restore = _run(
        "restore.sh",
        [
            "--backup",
            str(backup),
            "--confirm-destructive-restore",
            "--pre-restore-destination",
            str(pre_restore_destination),
        ],
        environment,
    )
    assert failed_restore.returncode != 0
    assert "write services were left stopped" in failed_restore.stderr
    assert "--skip-pre-restore-backup" in failed_restore.stderr

    environment.pop("FAKE_RESTORE_FAILURE")
    environment["RVC_BACKUP_TIMESTAMP"] = "20260711T050505Z"
    restored = _run(
        "restore.sh",
        [
            "--backup",
            str(backup),
            "--confirm-destructive-restore",
            "--pre-restore-destination",
            str(pre_restore_destination),
        ],
        environment,
    )

    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert _published_path(restored, "PRE_RESTORE_BACKUP_PATH").is_dir()
    assert (state / "restored-manager.pgdump").read_bytes() == b"MANAGER_CUSTOM_DUMP"
    assert (state / "restored-mlflow.pgdump").read_bytes() == b"MLFLOW_CUSTOM_DUMP"
    assert (state / "restored-manager-object").read_bytes() == b"manager-object"
    commands = _commands(state)
    stop_index = next(index for index, command in enumerate(commands) if command[0] == "stop")
    restore_index = next(
        index
        for index, command in enumerate(commands)
        if command[:3] == ["exec", "-T", "postgres"] and "pg_restore" in " ".join(command)
    )
    assert stop_index < restore_index
    assert any(
        command[:3] == ["exec", "-T", "postgres"] and "dropdb" in " ".join(command)
        for command in commands
    )
    assert any(
        command[:3] == ["exec", "-T", "redis"] and "FLUSHDB" in " ".join(command)
        for command in commands
    )
    assert any(
        "dataset-ingestion-init" in command
        and "/var/lib/rvc-dataset-ingestion" in " ".join(command)
        for command in commands
    )
    assert any(command[:3] == ["up", "-d", "--remove-orphans"] for command in commands)


def test_manager_rollback_enforces_schema_marker_and_reverts_failed_target(tmp_path: Path) -> None:
    install_root, config_root, state, environment = _layout(tmp_path)
    _write_release(install_root, "1.1.0", "schema-v1")
    _write_release(install_root, "1.0.0", "schema-v0")
    _write_release(install_root, "0.9.0", "schema-v1")

    environment["FAKE_STOP_FAILURE_ONCE"] = "1"
    interrupted = _run("rollback.sh", ["--to-version", "1.1.0"], environment)
    assert interrupted.returncode != 0
    assert (install_root / "current" / "VERSION").read_text().strip() == "1.2.3"
    assert any(command[:3] == ["up", "-d", "--remove-orphans"] for command in _commands(state))
    environment.pop("FAKE_STOP_FAILURE_ONCE")

    mismatch = _run("rollback.sh", ["--to-version", "1.0.0"], environment)
    assert mismatch.returncode != 0
    assert "SCHEMA_COMPATIBILITY" in mismatch.stderr
    assert (install_root / "current" / "VERSION").read_text().strip() == "1.2.3"

    successful = _run("rollback.sh", ["--to-version", "1.1.0"], environment)
    assert successful.returncode == 0, successful.stdout + successful.stderr
    assert (install_root / "current" / "VERSION").read_text().strip() == "1.1.0"
    active_environment = (config_root / "manager.env").read_text(encoding="utf-8")
    assert "ORCHESTRATOR_VERSION=1.1.0" in active_environment
    assert "API_IMAGE=rvc-orchestrator-api:1.1.0" in active_environment

    environment["FAKE_NOT_READY_VERSION"] = "0.9.0"
    failed = _run("rollback.sh", ["--to-version", "0.9.0"], environment)
    assert failed.returncode != 0
    assert "previous version 1.1.0 was restored and is ready" in failed.stderr
    assert (install_root / "current" / "VERSION").read_text().strip() == "1.1.0"
    assert (config_root / "manager.env").read_text(encoding="utf-8") == active_environment
    assert not list(install_root.glob(".current.rollback.*"))
    assert any(command[0] == "stop" for command in _commands(state))


def test_manager_rollback_schema_override_requires_confirmation_and_backup(
    tmp_path: Path,
) -> None:
    install_root, _, _, environment = _layout(tmp_path)
    _write_release(install_root, "1.1.0", "schema-v1")
    environment["FAKE_MISMATCH_SCHEMA_VERSION"] = "1.1.0"

    refused = _run("rollback.sh", ["--to-version", "1.1.0"], environment)
    assert refused.returncode != 0
    assert "actual database revision set" in refused.stderr

    unconfirmed = _run(
        "rollback.sh",
        ["--to-version", "1.1.0", "--allow-schema-mismatch"],
        environment,
    )
    assert unconfirmed.returncode != 0
    assert "I_UNDERSTAND_NO_DATABASE_DOWNGRADE" in unconfirmed.stderr

    destination = tmp_path / "pre-rollback"
    environment["RVC_BACKUP_TIMESTAMP"] = "20260711T060606Z"
    accepted = _run(
        "rollback.sh",
        [
            "--to-version",
            "1.1.0",
            "--allow-schema-mismatch",
            "--confirm-schema-mismatch-risk",
            "I_UNDERSTAND_NO_DATABASE_DOWNGRADE",
            "--pre-rollback-backup-destination",
            str(destination),
        ],
        environment,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert list(destination.glob("rvc-manager-backup-*"))


def test_manager_rollback_rejects_release_file_missing_from_checksums(tmp_path: Path) -> None:
    install_root, _, _, environment = _layout(tmp_path)
    target = _write_release(install_root, "1.1.0", "schema-v1")
    (target / "unlisted-runtime.sh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    refused = _run("rollback.sh", ["--to-version", "1.1.0"], environment)

    assert refused.returncode != 0
    assert "checksum inventory differs" in refused.stderr
    assert (install_root / "current" / "VERSION").read_text().strip() == "1.2.3"


def test_recovery_archive_helper_rejects_links_and_resource_bombs(tmp_path: Path) -> None:
    archive_module = _load_module(
        "recovery_archive_test", MANAGER_INSTALLERS / "recovery_archive.py"
    )
    source = tmp_path / "source.bin"
    source.write_bytes(b"reviewed")
    link = tmp_path / "source-link"
    link.symlink_to(source.name)
    with pytest.raises(archive_module.RecoveryArchiveError):
        archive_module.snapshot_regular_file(link, tmp_path / "snapshot.bin", 1024)

    tree = tmp_path / "tree"
    (tree / "backup-root").mkdir(parents=True)
    (tree / "backup-root" / "large.bin").write_bytes(b"x" * 64)
    archive = tmp_path / "backup.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(tree / "backup-root", arcname="backup-root")
    extraction = tmp_path / "extraction"
    extraction.mkdir()
    with pytest.raises(archive_module.RecoveryArchiveError, match="unpacked limit"):
        archive_module.extract_archive(
            archive,
            extraction,
            "backup-root",
            100,
            32,
            1,
            1,
        )


def test_object_snapshot_round_trip_preserves_bytes_metadata_headers_and_tags(
    tmp_path: Path,
) -> None:
    snapshot_module = _load_module(
        "recovery_object_snapshot_test", ROOT / "infra/runtime/recovery_object_snapshot.py"
    )

    class Paginator:
        def __init__(self, client: FakeS3, operation: str) -> None:
            self.client = client
            self.operation = operation

        def paginate(self, *, Bucket: str) -> list[dict[str, object]]:
            keys = sorted(key for bucket, key in self.client.objects if bucket == Bucket)
            if self.operation == "list_object_versions":
                return [{"Versions": [{"Key": key, "VersionId": "null"} for key in keys]}]
            return [{"Contents": [{"Key": key} for key in keys]}]

    class FakeS3:
        def __init__(self) -> None:
            self.objects: dict[tuple[str, str], dict[str, object]] = {}

        def get_bucket_versioning(self, *, Bucket: str) -> dict[str, object]:
            return {}

        def get_paginator(self, operation: str) -> Paginator:
            return Paginator(self, operation)

        def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
            item = self.objects[(Bucket, Key)]
            body = item["body"]
            assert isinstance(body, bytes)
            return {
                "ContentLength": len(body),
                "ETag": hashlib.md5(body, usedforsecurity=False).hexdigest(),
                "Metadata": item.get("Metadata", {}),
                "ContentType": item.get("ContentType"),
                "CacheControl": item.get("CacheControl"),
                "StorageClass": "STANDARD",
            }

        def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
            body = self.objects[(Bucket, Key)]["body"]
            assert isinstance(body, bytes)
            return {"Body": io.BytesIO(body)}

        def get_object_tagging(self, *, Bucket: str, Key: str) -> dict[str, object]:
            return {"TagSet": self.objects[(Bucket, Key)].get("tags", [])}

        def delete_objects(self, *, Bucket: str, Delete: dict[str, object]) -> dict[str, object]:
            entries = Delete["Objects"]
            assert isinstance(entries, list)
            for entry in entries:
                assert isinstance(entry, dict)
                self.objects.pop((Bucket, str(entry["Key"])), None)
            return {}

        def put_object(self, **kwargs: object) -> dict[str, object]:
            stream = kwargs.pop("Body")
            assert hasattr(stream, "read")
            body = stream.read()
            bucket = str(kwargs.pop("Bucket"))
            key = str(kwargs.pop("Key"))
            kwargs.pop("ContentLength")
            tagging = str(kwargs.pop("Tagging", ""))
            self.objects[(bucket, key)] = {
                "body": body,
                **kwargs,
                "tags": [{"Key": key, "Value": value} for key, value in parse_qsl(tagging)],
            }
            return {}

    client = FakeS3()
    client.objects[("rvc-orchestrator", "models/final.pth")] = {
        "body": b"model-bytes",
        "Metadata": {"sha256": "reviewed", "verified": "true"},
        "ContentType": "application/octet-stream",
        "CacheControl": "private, max-age=0",
        "tags": [{"Key": "type", "Value": "model"}],
    }
    client.objects[("rvc-mlflow", "runs/metrics.bin")] = {
        "body": b"metric-bytes",
        "Metadata": {"run": "one"},
        "ContentType": "application/octet-stream",
        "tags": [],
    }
    root = tmp_path / "objects"
    mappings = [("manager", "rvc-orchestrator"), ("mlflow", "rvc-mlflow")]
    snapshot_module.backup(client, root, mappings)
    client.objects = {("rvc-orchestrator", "unexpected"): {"body": b"future", "tags": []}}
    snapshot_module.restore(client, root, mappings)

    manager = client.objects[("rvc-orchestrator", "models/final.pth")]
    assert manager["body"] == b"model-bytes"
    assert manager["Metadata"] == {"sha256": "reviewed", "verified": "true"}
    assert manager["ContentType"] == "application/octet-stream"
    assert manager["CacheControl"] == "private, max-age=0"
    assert manager["tags"] == [{"Key": "type", "Value": "model"}]
    assert ("rvc-orchestrator", "unexpected") not in client.objects


def test_object_snapshot_hashes_large_sparse_payload_in_chunks(tmp_path: Path) -> None:
    snapshot_module = _load_module(
        "recovery_object_snapshot_sparse_test",
        ROOT / "infra/runtime/recovery_object_snapshot.py",
    )
    size = 128 * 1024 * 1024
    sparse = tmp_path / "large-model.bin"
    with sparse.open("wb") as stream:
        stream.seek(size - 1)
        stream.write(b"\0")
    expected = hashlib.sha256()
    zero_chunk = b"\0" * (1024 * 1024)
    for _ in range(size // len(zero_chunk)):
        expected.update(zero_chunk)

    assert snapshot_module._hash_file(sparse) == expected.hexdigest()


def test_manager_bundle_contains_recovery_commands_and_schema_marker(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "bash",
            str(MANAGER_INSTALLERS / "build-bundle.sh"),
            "--version",
            "9.8.7",
            "--schema-compatibility",
            "schema-v9",
            "--output-dir",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    archive = tmp_path / "rvc-manager-9.8.7-linux-amd64.tar.gz"
    with tarfile.open(archive, "r:gz") as bundle:
        names = set(bundle.getnames())
        manifest = bundle.extractfile("rvc-manager-9.8.7-linux-amd64/manifest.env")
        assert manifest is not None
        manifest_text = manifest.read().decode()
        environment_example = bundle.extractfile(
            "rvc-manager-9.8.7-linux-amd64/.env.example"
        )
        assert environment_example is not None
        environment_example_text = environment_example.read().decode()
        manager_compose = bundle.extractfile(
            "rvc-manager-9.8.7-linux-amd64/infra/compose/manager.compose.yml"
        )
        assert manager_compose is not None
        manager_compose_text = manager_compose.read().decode()
        proxy_entrypoint = bundle.getmember(
            "rvc-manager-9.8.7-linux-amd64/infra/runtime/proxy-entrypoint.sh"
        )
        sbom = bundle.extractfile(
            "rvc-manager-9.8.7-linux-amd64/supply-chain/sbom.cdx.json"
        )
        assert sbom is not None
        sbom_document = json.loads(sbom.read())
    for command in ("backup.sh", "restore.sh", "rollback.sh", "recovery_archive.py"):
        assert f"rvc-manager-9.8.7-linux-amd64/{command}" in names
    assert "rvc-manager-9.8.7-linux-amd64/supply-chain/sbom.cdx.json" in names
    assert (
        "rvc-manager-9.8.7-linux-amd64/supply-chain/third-party-licenses.json" in names
    )
    assert not any("__pycache__" in name.split("/") for name in names)
    assert not any(name.endswith((".pyc", ".pyo", "/.DS_Store")) for name in names)
    assert "SCHEMA_COMPATIBILITY=schema-v9" in manifest_text
    assert "SBOM_STATUS=partial-release-gates-open" in manifest_text
    assert "EXPERIMENT_JSON_MAX_BYTES=16384" in environment_example_text
    assert "USER_LIFECYCLE_JSON_MAX_BYTES=16384" in environment_example_text
    assert "PUBLIC_SCHEME=http" in environment_example_text
    assert proxy_entrypoint.mode & stat.S_IXUSR
    assert "PUBLIC_SCHEME: ${PUBLIC_SCHEME:-http}" in manager_compose_text
    assert (
        "EXPERIMENT_JSON_MAX_BYTES: ${EXPERIMENT_JSON_MAX_BYTES:-16384}"
        in manager_compose_text
    )
    assert (
        "USER_LIFECYCLE_JSON_MAX_BYTES: ${USER_LIFECYCLE_JSON_MAX_BYTES:-16384}"
        in manager_compose_text
    )
    assert sbom_document["metadata"]["component"]["version"] == "9.8.7"
    installer = (MANAGER_INSTALLERS / "install.sh").read_text(encoding="utf-8")
    assert "backup:backup" in installer
    assert "restore:restore" in installer
    assert "rollback:rollback" in installer
    assert "--public-scheme" in installer


def test_manager_install_and_upgrade_place_recovery_commands_and_switch_images(
    tmp_path: Path,
) -> None:
    bundles = tmp_path / "bundles"
    extracted = tmp_path / "extracted"
    install_root = tmp_path / "installed"
    config_root = tmp_path / "configuration"
    systemd_root = tmp_path / "systemd"
    fake_bin = tmp_path / "fake-bin"
    bundles.mkdir()
    extracted.mkdir()
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    docker.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "RVC_INSTALL_ALLOW_NON_ROOT": "1",
        "RVC_MANAGER_MINIMUM_DISK_GB": "0",
    }

    for version in ("8.7.6", "8.7.7"):
        built = subprocess.run(
            [
                "bash",
                str(MANAGER_INSTALLERS / "build-bundle.sh"),
                "--version",
                version,
                "--schema-compatibility",
                "schema-v8",
                "--output-dir",
                str(bundles),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert built.returncode == 0, built.stdout + built.stderr
        archive = bundles / f"rvc-manager-{version}-linux-amd64.tar.gz"
        unpacked = subprocess.run(
            ["tar", "-xzf", str(archive), "-C", str(extracted)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert unpacked.returncode == 0, unpacked.stderr

    def install(version: str) -> subprocess.CompletedProcess[str]:
        bundle = extracted / f"rvc-manager-{version}-linux-amd64"
        return subprocess.run(
            [
                "bash",
                str(bundle / "install.sh"),
                "--install-root",
                str(install_root),
                "--config-root",
                str(config_root),
                "--systemd-dir",
                str(systemd_root),
                "--allow-unsupported-os",
                "--skip-daemon-check",
                "--no-start",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    first = install("8.7.6")
    assert first.returncode == 0, first.stdout + first.stderr
    for command in ("backup", "restore", "rollback", "bootstrap-admin"):
        assert (install_root / "bin" / command).stat().st_mode & stat.S_IXUSR
    jwt_secret = (config_root / "secrets" / "jwt_secret").read_bytes()
    manager_env = config_root / "manager.env"
    manager_env.write_text(
        manager_env.read_text(encoding="utf-8") + "CUSTOM_SETTING=preserved\n",
        encoding="utf-8",
    )

    upgraded = install("8.7.7")
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    assert (install_root / "current" / "VERSION").read_text().strip() == "8.7.7"
    environment_text = manager_env.read_text(encoding="utf-8")
    assert "CUSTOM_SETTING=preserved" in environment_text
    assert "ORCHESTRATOR_VERSION=8.7.7" in environment_text
    assert "API_IMAGE=rvc-orchestrator-api:8.7.7" in environment_text
    assert (config_root / "secrets" / "jwt_secret").read_bytes() == jwt_secret
    release = install_root / "releases" / "8.7.7"
    assert (release / "manifest.env").is_file()
    assert (release / "RELEASE_SHA256SUMS").is_file()


def test_docker_volume_recovery_drill_is_isolated_and_opt_in() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    check_rule = next(line for line in makefile.splitlines() if line.startswith("check:"))
    assert "test-manager-recovery-docker:" in makefile
    assert "test-manager-recovery-docker" not in check_rule

    fixture_path = ROOT / "tests/recovery/fixtures/manager-recovery.compose.yml"
    fixture = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
    assert set(fixture["volumes"]) == {
        "artifact_spool",
        "dataset_ingestion",
        "minio_data",
        "postgres_data",
        "redis_data",
    }
    assert fixture["services"]["postgres"]["image"].endswith("postgres:16-alpine}")
    assert fixture["services"]["minio"]["image"].startswith("${MINIO_IMAGE")
    assert fixture["services"]["redis"]["image"].startswith("${REDIS_IMAGE")
    assert fixture["services"]["object-recovery"]["image"].startswith(
        "${RVC_RECOVERY_DRILL_API_IMAGE"
    )
    assert all("ports" not in service for service in fixture["services"].values())

    drill = (ROOT / "tests/recovery/manager_volume_drill.sh").read_text(encoding="utf-8")
    assert '--project-name "$project"' in drill
    assert "compose down --volumes --remove-orphans" in drill
    assert "docker volume rm" not in drill
    assert "docker volume prune" not in drill
