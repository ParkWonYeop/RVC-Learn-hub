from __future__ import annotations

import os
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

MANAGER_MAINTENANCE_SECRET_NAMES = {
    "maintenance_postgres_password",
    "maintenance_redis_password",
    "maintenance_s3_access_key",
    "maintenance_s3_secret_key",
}


def _build_bundle(tmp_path: Path, component: str, version: str) -> Path:
    output = tmp_path / "bundles"
    output.mkdir(exist_ok=True)
    command = [
        "bash",
        str(ROOT / f"installers/{component}/build-bundle.sh"),
        "--version",
        version,
        "--output-dir",
        str(output),
    ]
    if component == "manager":
        command.extend(("--schema-compatibility", "installer-activation-v1"))
    built = subprocess.run(command, check=False, capture_output=True, text=True)
    assert built.returncode == 0, built.stdout + built.stderr
    archive = output / f"rvc-{component}-{version}-linux-amd64.tar.gz"
    extracted = tmp_path / f"extracted-{component}-{version}"
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(extracted, filter="data")
    return extracted / f"rvc-{component}-{version}-linux-amd64"


def _write_fake_docker(tmp_path: Path) -> Path:
    binary_root = tmp_path / "fake-bin"
    binary_root.mkdir()
    docker = binary_root / "docker"
    docker.write_text(
        """#!/bin/sh
set -eu
if [ "${1:-}" != compose ]; then
  exit 0
fi
environment_file=
previous=
for argument in "$@"; do
  if [ "$previous" = --env-file ]; then
    environment_file=$argument
    break
  fi
  previous=$argument
done
if [ -n "${FAIL_COMPOSE_VERSION:-}" ] && [ -n "$environment_file" ] &&
   grep -qx "ORCHESTRATOR_VERSION=$FAIL_COMPOSE_VERSION" "$environment_file"; then
  echo "injected prospective Compose failure" >&2
  exit 47
fi
exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return binary_root


def _installer_arguments(
    component: str,
    install_root: Path,
    config_root: Path,
    systemd_root: Path,
    data_root: Path,
    token: Path,
    *,
    first_install: bool,
) -> list[str]:
    common = [
        "--install-root",
        str(install_root),
        "--config-root",
        str(config_root),
        "--systemd-dir",
        str(systemd_root),
        "--allow-unsupported-os",
        "--skip-daemon-check",
        "--no-start",
    ]
    if component == "manager":
        return common
    worker = [
        "--runner-mode",
        "fake",
        "--allow-fake-dev",
        "--data-root",
        str(data_root),
        "--skip-gpu-check",
        *common,
    ]
    if first_install:
        worker = [
            "--manager-url",
            "https://manager.example.test",
            "--worker-name",
            "gpu-test-01",
            "--token-file",
            str(token),
            *worker,
        ]
    return worker


@pytest.mark.parametrize("component", ("manager", "worker"))
def test_upgrade_prevalidation_preserves_active_release_and_rejects_downgrade(
    tmp_path: Path, component: str
) -> None:
    old_bundle = _build_bundle(tmp_path, component, "1.0.0")
    new_bundle = _build_bundle(tmp_path, component, "2.0.0")
    fake_bin = _write_fake_docker(tmp_path)
    install_root = tmp_path / component / "install"
    config_root = tmp_path / component / "config"
    systemd_root = tmp_path / component / "systemd"
    data_root = tmp_path / component / "data"
    token = tmp_path / component / "worker-token"
    token.parent.mkdir(parents=True)
    token.write_text("test-worker-token", encoding="utf-8")
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "RVC_INSTALL_ALLOW_NON_ROOT": "1",
        "RVC_MANAGER_MINIMUM_DISK_GB": "0",
        "RVC_WORKER_MINIMUM_DISK_GB": "0",
    }

    first = subprocess.run(
        [
            "bash",
            str(old_bundle / "install.sh"),
            *_installer_arguments(
                component,
                install_root,
                config_root,
                systemd_root,
                data_root,
                token,
                first_install=True,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    env_path = config_root / f"{component}.env"
    old_environment = env_path.read_bytes()
    assert (install_root / "current").readlink() == Path("releases/1.0.0")
    manager_legacy_secrets: dict[str, bytes] = {}
    if component == "manager":
        secret_root = config_root / "secrets"
        assert {path.name for path in secret_root.iterdir()} == {
            "postgres_password",
            "maintenance_postgres_password",
            "mlflow_postgres_password",
            "redis_password",
            "maintenance_redis_password",
            "minio_root_user",
            "minio_root_password",
            "minio_app_access_key",
            "minio_app_secret_key",
            "maintenance_s3_access_key",
            "maintenance_s3_secret_key",
            "mlflow_s3_access_key",
            "mlflow_s3_secret_key",
            "worker_bootstrap_token",
            "worker_token_pepper",
            "jwt_secret",
        }
        for path in secret_root.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            if path.name not in MANAGER_MAINTENANCE_SECRET_NAMES:
                manager_legacy_secrets[path.name] = path.read_bytes()
        # Model an upgrade from the pre-dedicated-credential layout. The new
        # installer must add only the four missing identities.
        for name in MANAGER_MAINTENANCE_SECRET_NAMES:
            (secret_root / name).unlink()

    rejected = subprocess.run(
        [
            "bash",
            str(new_bundle / "upgrade.sh"),
            *_installer_arguments(
                component,
                install_root,
                config_root,
                systemd_root,
                data_root,
                token,
                first_install=False,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**environment, "FAIL_COMPOSE_VERSION": "2.0.0"},
    )
    assert rejected.returncode == 47, rejected.stdout + rejected.stderr
    assert "injected prospective Compose failure" in rejected.stderr
    assert (install_root / "current").readlink() == Path("releases/1.0.0")
    assert env_path.read_bytes() == old_environment
    assert not list(config_root.glob(f".{component}.env.pending.*"))
    if component == "manager":
        secret_root = config_root / "secrets"
        assert MANAGER_MAINTENANCE_SECRET_NAMES.issubset(
            {path.name for path in secret_root.iterdir()}
        )
        assert {
            name: (secret_root / name).read_bytes() for name in manager_legacy_secrets
        } == manager_legacy_secrets
        for path in secret_root.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        manager_secrets_after_rejected_upgrade = {
            path.name: path.read_bytes() for path in secret_root.iterdir()
        }

    upgraded = subprocess.run(
        [
            "bash",
            str(new_bundle / "upgrade.sh"),
            *_installer_arguments(
                component,
                install_root,
                config_root,
                systemd_root,
                data_root,
                token,
                first_install=False,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    assert (install_root / "current").readlink() == Path("releases/2.0.0")
    upgraded_environment = env_path.read_bytes()
    if component == "manager":
        assert {
            path.name: path.read_bytes() for path in (config_root / "secrets").iterdir()
        } == manager_secrets_after_rejected_upgrade
        unexpected = config_root / "secrets/unexpected_operator_secret"
        unexpected.write_text("must-not-be-enumerated", encoding="utf-8")
        unexpected.chmod(0o600)
        rejected_inventory = subprocess.run(
            [
                "bash",
                str(new_bundle / "install.sh"),
                *_installer_arguments(
                    component,
                    install_root,
                    config_root,
                    systemd_root,
                    data_root,
                    token,
                    first_install=False,
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert rejected_inventory.returncode != 0
        assert "source secret inventory is not exact" in rejected_inventory.stderr
        assert (install_root / "current").readlink() == Path("releases/2.0.0")
        assert env_path.read_bytes() == upgraded_environment
        unexpected.unlink()

    downgrade = subprocess.run(
        [
            "bash",
            str(old_bundle / "upgrade.sh"),
            *_installer_arguments(
                component,
                install_root,
                config_root,
                systemd_root,
                data_root,
                token,
                first_install=False,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert downgrade.returncode != 0
    assert "refusing non-forward release transition from 2.0.0 to 1.0.0" in downgrade.stderr
    assert (install_root / "current").readlink() == Path("releases/2.0.0")
    assert env_path.read_bytes() == upgraded_environment


def test_semver_forward_transition_handles_development_prerelease_order() -> None:
    command = 'source "$1"; rvc_semver_strictly_precedes "0.1.0-dev.14" "0.1.0-dev.15"'
    accepted = subprocess.run(
        ["bash", "-c", command, "bash", str(ROOT / "installers/common/lib.sh")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert accepted.returncode == 0, accepted.stderr

    rejected = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; rvc_semver_strictly_precedes "0.1.0" "0.1.0-dev.15"',
            "bash",
            str(ROOT / "installers/common/lib.sh"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0


def test_worker_custom_ca_is_atomically_installed_preserved_and_start_validated(
    tmp_path: Path,
) -> None:
    old_bundle = _build_bundle(tmp_path, "worker", "1.0.0")
    new_bundle = _build_bundle(tmp_path, "worker", "2.0.0")
    fake_bin = _write_fake_docker(tmp_path)
    install_root = tmp_path / "worker-ca" / "install"
    config_root = tmp_path / "worker-ca" / "config"
    systemd_root = tmp_path / "worker-ca" / "systemd"
    data_root = tmp_path / "worker-ca" / "data"
    token = tmp_path / "worker-ca" / "worker-token"
    source_ca = tmp_path / "worker-ca" / "source-ca.pem"
    token.parent.mkdir(parents=True)
    token.write_text("test-worker-token", encoding="utf-8")
    source_ca.write_bytes((ROOT / "tests/fixtures/custom-ca.pem").read_bytes())
    source_ca.chmod(0o644)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "RVC_INSTALL_ALLOW_NON_ROOT": "1",
        "RVC_WORKER_MINIMUM_DISK_GB": "0",
    }

    first = subprocess.run(
        [
            "bash",
            str(old_bundle / "install.sh"),
            *_installer_arguments(
                "worker",
                install_root,
                config_root,
                systemd_root,
                data_root,
                token,
                first_install=True,
            ),
            "--ca-bundle-file",
            str(source_ca),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    installed_ca = config_root / "ca/custom-ca.pem"
    expected_ca = source_ca.read_bytes()
    assert installed_ca.read_bytes() == expected_ca
    assert stat.S_IMODE(installed_ca.stat().st_mode) == 0o444
    worker_env = (config_root / "worker.env").read_text(encoding="utf-8")
    assert "WORKER_CA_BUNDLE_HOST_DIR=" + str(config_root / "ca") in worker_env
    assert "WORKER_CA_BUNDLE_PATH=/etc/rvc-worker/ca/custom-ca.pem" in worker_env
    assert not list((config_root / "ca").glob(".custom-ca.*"))

    replacement_ca = tmp_path / "worker-ca" / "replacement-ca.pem"
    replacement_ca.write_bytes(expected_ca + expected_ca)
    replacement_ca.chmod(0o644)
    rejected = subprocess.run(
        [
            "bash",
            str(new_bundle / "upgrade.sh"),
            *_installer_arguments(
                "worker",
                install_root,
                config_root,
                systemd_root,
                data_root,
                token,
                first_install=False,
            ),
            "--ca-bundle-file",
            str(replacement_ca),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**environment, "FAIL_COMPOSE_VERSION": "2.0.0"},
    )
    assert rejected.returncode == 47, rejected.stdout + rejected.stderr
    assert installed_ca.read_bytes() == expected_ca
    assert not list((config_root / "ca").glob(".custom-ca.*"))

    upgraded = subprocess.run(
        [
            "bash",
            str(new_bundle / "upgrade.sh"),
            *_installer_arguments(
                "worker",
                install_root,
                config_root,
                systemd_root,
                data_root,
                token,
                first_install=False,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    assert installed_ca.read_bytes() == expected_ca
    assert stat.S_IMODE(installed_ca.stat().st_mode) == 0o444

    wrapper_environment = {
        **environment,
        "RVC_INSTALL_ROOT": str(install_root),
        "RVC_CONFIG_ROOT": str(config_root),
    }
    started = subprocess.run(
        [str(install_root / "bin/worker-compose"), "start"],
        check=False,
        capture_output=True,
        text=True,
        env=wrapper_environment,
    )
    assert started.returncode == 0, started.stdout + started.stderr

    installed_ca.chmod(0o600)
    rejected = subprocess.run(
        [str(install_root / "bin/worker-compose"), "start"],
        check=False,
        capture_output=True,
        text=True,
        env=wrapper_environment,
    )
    assert rejected.returncode != 0
    assert "mode must be 0444 or 0644" in rejected.stderr
