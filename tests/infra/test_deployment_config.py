from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_PORT_BINDING = re.compile(
    r"^\$\{[A-Z0-9_]+:-(?P<host_ip>[^}]+)\}:"
    r"\$\{[A-Z0-9_]+:-(?P<published>[0-9]+)\}:"
    r"(?P<target>[0-9]+)$"
)


def _load_yaml(relative: str) -> dict[str, object]:
    document = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _run_uninstaller(
    tmp_path: Path,
    component: str,
    *,
    systemctl_exit: int,
    compose_exit: int,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    case_root = tmp_path / f"{component}-{systemctl_exit}-{compose_exit}"
    fake_bin = case_root / "bin"
    install_root = case_root / "install"
    config_root = case_root / "config"
    call_log = case_root / "calls.log"
    fake_bin.mkdir(parents=True)
    (install_root / "bin").mkdir(parents=True)
    config_root.mkdir()

    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        'printf \'systemctl:%s\\n\' "$*" >> "$RVC_UNINSTALL_CALL_LOG"\n'
        'exit "$RVC_SYSTEMCTL_EXIT"\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    compose = install_root / "bin" / f"{component}-compose"
    compose.write_text(
        "#!/bin/sh\n"
        'printf \'compose:%s\\n\' "$*" >> "$RVC_UNINSTALL_CALL_LOG"\n'
        'exit "$RVC_COMPOSE_EXIT"\n',
        encoding="utf-8",
    )
    compose.chmod(0o755)

    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "RVC_INSTALL_ROOT": str(install_root),
        "RVC_CONFIG_ROOT": str(config_root),
        "WORKER_DATA_ROOT": str(case_root / "data"),
        "RVC_UNINSTALL_CALL_LOG": str(call_log),
        "RVC_SYSTEMCTL_EXIT": str(systemctl_exit),
        "RVC_COMPOSE_EXIT": str(compose_exit),
    }
    result = subprocess.run(
        ["bash", str(ROOT / f"installers/{component}/uninstall.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    calls = call_log.read_text(encoding="utf-8").splitlines()
    return result, calls


def _render_default_port_binding(value: object) -> dict[str, object]:
    assert isinstance(value, str)
    match = _DEFAULT_PORT_BINDING.fullmatch(value)
    assert match is not None, value
    return {
        "host_ip": match.group("host_ip"),
        "published": match.group("published"),
        "target": int(match.group("target")),
    }


def test_manager_compose_contains_complete_control_plane() -> None:
    compose = _load_yaml("infra/compose/manager.compose.yml")
    services = compose["services"]
    assert isinstance(services, dict)
    assert {
        "postgres",
        "redis",
        "minio",
        "minio-init",
        "artifact-spool-init",
        "dataset-ingestion-init",
        "manager-secrets-init",
        "maintenance-db-authz",
        "mlflow",
        "api-migrate",
        "api",
        "rq-worker",
        "web",
        "proxy",
    }.issubset(services)
    for name, service in services.items():
        assert isinstance(service, dict), name
        assert service.get("privileged") is not True, name
        volumes = service.get("volumes", [])
        assert all("docker.sock" not in str(volume) for volume in volumes), name


def test_only_loopback_published_storage_services_join_host_access_network() -> None:
    compose = _load_yaml("infra/compose/manager.compose.yml")
    services = compose["services"]  # type: ignore[index]
    networks = compose["networks"]  # type: ignore[index]

    assert networks["backend"]["internal"] is True
    assert networks["storage"]["internal"] is True
    assert networks["host-access"] is None

    host_access_services = {
        service_name
        for service_name, service in services.items()
        if isinstance(service, dict) and "host-access" in service.get("networks", [])
    }
    assert host_access_services == {"minio", "mlflow"}

    expected_default_ports = {
        "minio": [
            {"host_ip": "127.0.0.1", "published": "9000", "target": 9000},
            {"host_ip": "127.0.0.1", "published": "9001", "target": 9001},
        ],
        "mlflow": [
            {"host_ip": "127.0.0.1", "published": "5000", "target": 5000},
        ],
    }
    for service_name, expected_ports in expected_default_ports.items():
        service = services[service_name]
        assert service["networks"] == ["storage", "host-access"]
        assert [_render_default_port_binding(port) for port in service["ports"]] == expected_ports


def test_every_runtime_image_has_an_explicit_offline_pull_policy() -> None:
    for relative in (
        "infra/compose/manager.compose.yml",
        "infra/compose/worker.compose.yml",
    ):
        compose = _load_yaml(relative)
        services = compose["services"]
        assert isinstance(services, dict)
        image_services = {
            name: service
            for name, service in services.items()
            if isinstance(service, dict) and "image" in service
        }
        assert image_services
        for name, service in image_services.items():
            assert service["pull_policy"] == "${RVC_IMAGE_PULL_POLICY:-missing}", name


def test_installers_stage_then_start_oneshot_units_after_release_switch() -> None:
    for component in ("manager", "worker"):
        installer = (ROOT / f"installers/{component}/install.sh").read_text(encoding="utf-8")
        unit = f"rvc-orchestrator-{component}.service"
        assert f"systemctl enable {unit}" in installer
        assert f"systemctl stop {unit}" in installer
        assert f"systemctl start {unit}" in installer
        assert f"systemctl restart {unit}" not in installer
        assert f"systemctl enable --now {unit}" not in installer
        prevalidate = installer.index("config --quiet\n\nif [[ $no_start == 0 ]]")
        activate_environment = installer.index('mv -f -- "$pending_env" "$env_file"')
        activate_release = installer.index('rvc_switch_current_release "$INSTALL_ROOT" "$version"')
        start = installer.index(f"systemctl start {unit}", activate_release)
        assert prevalidate < activate_environment < activate_release < start


def test_uninstallers_fail_closed_and_attempt_both_stop_paths(tmp_path: Path) -> None:
    cases = (
        ("manager", "Manager", "Manager services are stopped and disabled."),
        ("worker", "Worker", "Worker service is stopped and disabled."),
    )
    for component, title, success_message in cases:
        expected_calls = [
            f"systemctl:disable --now rvc-orchestrator-{component}.service",
            "compose:down --remove-orphans",
        ]

        for systemctl_exit, compose_exit in ((23, 0), (0, 29), (23, 29)):
            result, calls = _run_uninstaller(
                tmp_path,
                component,
                systemctl_exit=systemctl_exit,
                compose_exit=compose_exit,
            )

            assert result.returncode == 1
            assert calls == expected_calls
            assert success_message not in result.stdout + result.stderr
            assert f"{title} uninstall is incomplete" in result.stderr


def test_uninstallers_report_success_only_after_both_stop_paths_succeed(
    tmp_path: Path,
) -> None:
    cases = (
        ("manager", "Manager services are stopped and disabled."),
        ("worker", "Worker service is stopped and disabled."),
    )
    for component, success_message in cases:
        result, calls = _run_uninstaller(
            tmp_path,
            component,
            systemctl_exit=0,
            compose_exit=0,
        )

        assert result.returncode == 0, result.stderr
        assert calls == [
            f"systemctl:disable --now rvc-orchestrator-{component}.service",
            "compose:down --remove-orphans",
        ]
        assert success_message in result.stdout
        assert "uninstall is incomplete" not in result.stdout + result.stderr


def test_manager_compose_refreshes_runtime_secret_projections_before_start_actions() -> None:
    wrapper = (ROOT / "installers/manager/compose.sh").read_text(encoding="utf-8")

    assert "up|start|restart|run|create)" in wrapper
    refresh = '"${compose[@]}" run --rm --no-deps manager-secrets-init'
    execute = 'exec "${compose[@]}" "$@"'
    assert refresh in wrapper
    assert execute in wrapper
    assert wrapper.index(refresh) < wrapper.index(execute)
    assert "start|restart)" in wrapper
    assert 'exec "${compose[@]}" up -d --force-recreate --remove-orphans' in wrapper
    assert "does not accept service-scoped arguments" in wrapper


def test_release_images_receive_version_and_source_commit_labels() -> None:
    expected_users = {
        "apps/api/Dockerfile": "USER 10001:10001",
        "apps/web/Dockerfile": "USER nextjs",
        "infra/mlflow/Dockerfile": "USER 10002:10002",
        "apps/worker/Dockerfile": "USER 10001:10001",
        "apps/worker/Dockerfile.rvc": "USER 10001:10001",
    }
    for relative, expected_user in expected_users.items():
        dockerfile = (ROOT / relative).read_text(encoding="utf-8")
        assert "ARG RVC_RELEASE_VERSION=dev" in dockerfile
        assert "ARG RVC_SOURCE_COMMIT=uncommitted" in dockerfile
        assert 'org.opencontainers.image.version="${RVC_RELEASE_VERSION}"' in dockerfile
        assert 'org.opencontainers.image.revision="${RVC_SOURCE_COMMIT}"' in dockerfile
        assert expected_user in dockerfile

    verifier = (ROOT / "installers/common/image_bundle.py").read_text(encoding="utf-8")
    assert (
        "_docker_field(docker, reference, "
        "'{{with index .Config \"User\"}}{{.}}{{end}}')"
        in verifier
    )
    assert '("manager", "api"): "10001:10001"' in verifier
    assert '("manager", "web"): "nextjs"' in verifier
    assert '("manager", "mlflow"): "10002:10002"' in verifier
    assert '("worker", "runtime"): "10001:10001"' in verifier
    assert "Docker image archive Config content digest differs" in verifier


def test_self_contained_builders_require_release_source_ignore_closure() -> None:
    for component in ("manager", "worker"):
        builder = (ROOT / f"installers/{component}/build-bundle.sh").read_text(encoding="utf-8")
        assert 'tools/verify_release_source.py" --repo-root "$REPO_ROOT"' in builder
        assert "complete non-ignored source closure" in builder

    for relative in (
        "infra/compose/manager.compose.build.yml",
        "infra/compose/worker.compose.build.yml",
    ):
        override = (ROOT / relative).read_text(encoding="utf-8")
        assert "RVC_RELEASE_VERSION: ${ORCHESTRATOR_VERSION:-dev}" in override
        assert "RVC_SOURCE_COMMIT: ${GIT_COMMIT:-uncommitted}" in override


def test_manager_images_have_no_worker_or_rvc_execution_boundary() -> None:
    compose = _load_yaml("infra/compose/manager.compose.yml")
    services = compose["services"]
    assert isinstance(services, dict)
    assert "worker" not in services
    for service_name in ("api-migrate", "api", "web", "proxy"):
        service = services[service_name]
        assert isinstance(service, dict)
        assert "gpus" not in service
        assert "runtime" not in service
        assert "device_requests" not in service
        assert all("nvidia" not in str(value).lower() for value in service.values())

    api_dockerfile = (ROOT / "apps/api/Dockerfile").read_text(encoding="utf-8").lower()
    api_project = (ROOT / "apps/api/pyproject.toml").read_text(encoding="utf-8").lower()
    for forbidden in (
        "apps/worker",
        "dockerfile.rvc",
        "/opt/rvc-webui",
        "nvidia",
        "pytorch",
        "torch==",
        "faiss",
        "rvc-orchestrator-worker",
    ):
        assert forbidden not in api_dockerfile
        assert forbidden not in api_project


def test_rq_worker_is_internal_non_root_and_allowlisted() -> None:
    compose = _load_yaml("infra/compose/manager.compose.yml")
    services = compose["services"]  # type: ignore[index]
    worker = services["rq-worker"]
    assert worker["image"] == "${API_IMAGE:-rvc-orchestrator-api:dev}"
    assert worker["entrypoint"] == ["/opt/rvc/rq-worker-entrypoint.sh"]
    assert worker["command"] == ["rvc-manager-rq-worker"]
    assert worker["networks"] == ["storage"]
    assert "ports" not in worker
    assert "gpus" not in worker
    assert "runtime" not in worker
    assert "device_requests" not in worker
    assert worker.get("privileged") is not True
    assert worker["read_only"] is True
    assert worker["cap_drop"] == ["ALL"]
    assert worker["pids_limit"] == 64
    assert all("docker.sock" not in str(value) for value in worker.get("volumes", []))
    assert worker["environment"]["PROCESS_ROLE"] == "maintenance"
    assert worker["environment"]["RQ_ENABLED"] == "true"
    assert worker["environment"]["MLFLOW_ENABLED"] == "false"
    assert worker["environment"]["RATE_LIMIT_ENABLED"] == "false"
    assert worker["environment"]["MAINTENANCE_TASK_HEARTBEAT_SECONDS"] == (
        "${MAINTENANCE_TASK_HEARTBEAT_SECONDS:-15}"
    )
    assert worker["environment"]["MAINTENANCE_POSTGRES_USER"] == (
        "${MAINTENANCE_POSTGRES_USER:-rvc_maintenance}"
    )
    assert worker["environment"]["MAINTENANCE_REDIS_USER"] == (
        "${MAINTENANCE_REDIS_USER:-rvc_maintenance}"
    )
    assert "POSTGRES_USER" not in worker["environment"]
    assert "secrets" not in worker
    assert "maintenance_runtime_secrets:/run/secrets:ro" in worker["volumes"]
    assert all("api_runtime_secrets" not in str(value) for value in worker["volumes"])
    assert all("mlflow_runtime_secrets" not in str(value) for value in worker["volumes"])
    assert "JWT_SECRET_FILE" not in worker["environment"]
    assert "S3_PRESIGN_ENDPOINT_URL" not in worker["environment"]
    entrypoint = (ROOT / "infra/runtime/rq-worker-entrypoint.sh").read_text(encoding="utf-8")
    assert os.access(ROOT / "infra/runtime/rq-worker-entrypoint.sh", os.X_OK)
    assert "WORKER_BOOTSTRAP_TOKEN" not in entrypoint
    assert "WORKER_TOKEN_PEPPER" not in entrypoint
    assert "jwt_secret" not in entrypoint
    assert "maintenance_postgres_password" in entrypoint
    assert "maintenance_redis_password" in entrypoint
    assert "maintenance_s3_access_key" in entrypoint
    assert "maintenance_s3_secret_key" in entrypoint
    assert "/opt/rvc/maintenance-db-authz.py verify-runtime" in entrypoint
    for api_secret in (
        "/current/postgres_password",
        "/current/redis_password",
        "/current/minio_app_access_key",
        "/current/minio_app_secret_key",
    ):
        assert api_secret not in entrypoint
    dockerfile = (ROOT / "apps/api/Dockerfile").read_text(encoding="utf-8")
    assert "USER 10001:10001" in dockerfile
    assert compose["networks"]["storage"]["internal"] is True  # type: ignore[index]


def test_mlflow_is_explicitly_non_root_and_read_only() -> None:
    compose = _load_yaml("infra/compose/manager.compose.yml")
    services = compose["services"]  # type: ignore[index]
    mlflow = services["mlflow"]

    assert mlflow["user"] == "10002:10002"
    assert mlflow["read_only"] is True
    assert mlflow["cap_drop"] == ["ALL"]
    assert mlflow["pids_limit"] == 128
    assert mlflow.get("privileged") is not True
    assert "gpus" not in mlflow
    assert "runtime" not in mlflow
    assert "device_requests" not in mlflow
    assert mlflow["networks"] == ["storage", "host-access"]
    assert "secrets" not in mlflow
    assert "mlflow_runtime_secrets:/run/secrets:ro" in mlflow["volumes"]
    assert mlflow["depends_on"]["manager-secrets-init"] == {
        "condition": "service_completed_successfully"
    }
    assert all("docker.sock" not in str(value) for value in mlflow.get("volumes", []))
    assert mlflow["tmpfs"] == [
        "/tmp:rw,noexec,nosuid,nodev,size=128m,mode=0700,uid=10002,gid=10002"
    ]

    dockerfile = (ROOT / "infra/mlflow/Dockerfile").read_text(encoding="utf-8")
    assert "groupadd --gid 10002 rvc-mlflow" in dockerfile
    assert "--uid 10002" in dockerfile
    assert "--gid 10002" in dockerfile
    assert "USER 10002:10002" in dockerfile
    assert "HOME=/tmp" in dockerfile
    assert "XDG_CACHE_HOME=/tmp/.cache" in dockerfile


def test_non_root_manager_services_use_atomic_least_privilege_secret_projections() -> None:
    compose = _load_yaml("infra/compose/manager.compose.yml")
    services = compose["services"]  # type: ignore[index]
    initializer = services["manager-secrets-init"]

    assert initializer["image"] == "${API_IMAGE:-rvc-orchestrator-api:dev}"
    assert initializer["user"] == "0:0"
    assert initializer["restart"] == "no"
    assert initializer["network_mode"] == "none"
    assert initializer["read_only"] is True
    assert initializer["cap_drop"] == ["ALL"]
    assert initializer["cap_add"] == ["CHOWN"]
    assert initializer["pids_limit"] == 32
    assert initializer["entrypoint"] == [
        "python",
        "/opt/rvc/manager-secrets-init.py",
    ]
    assert initializer["secrets"] == [
        "postgres_password",
        "redis_password",
        "minio_app_access_key",
        "minio_app_secret_key",
        "maintenance_postgres_password",
        "maintenance_redis_password",
        "maintenance_s3_access_key",
        "maintenance_s3_secret_key",
        "worker_bootstrap_token",
        "worker_token_pepper",
        "jwt_secret",
        "mlflow_postgres_password",
        "mlflow_s3_access_key",
        "mlflow_s3_secret_key",
    ]
    assert initializer["volumes"] == [
        "../runtime/manager-secrets-init.py:/opt/rvc/manager-secrets-init.py:ro",
        "api_runtime_secrets:/prepared/api",
        "maintenance_runtime_secrets:/prepared/maintenance",
        "mlflow_runtime_secrets:/prepared/mlflow",
        "database_authz_runtime_secrets:/prepared/database-authz",
    ]

    expected_volume = {
        "api-migrate": "api_runtime_secrets:/run/secrets:ro",
        "api": "api_runtime_secrets:/run/secrets:ro",
        "rq-worker": "maintenance_runtime_secrets:/run/secrets:ro",
        "maintenance-db-authz": "database_authz_runtime_secrets:/run/secrets:ro",
        "mlflow": "mlflow_runtime_secrets:/run/secrets:ro",
    }
    for service_name, runtime_volume in expected_volume.items():
        service = services[service_name]
        assert "secrets" not in service
        assert runtime_volume in service["volumes"]
        assert service["depends_on"]["manager-secrets-init"] == {
            "condition": "service_completed_successfully"
        }

    for volume_name in (
        "api_runtime_secrets",
        "maintenance_runtime_secrets",
        "database_authz_runtime_secrets",
        "mlflow_runtime_secrets",
    ):
        assert compose["volumes"][volume_name]["labels"] == {  # type: ignore[index]
            "org.rvc-orchestrator.component": "manager-sensitive-runtime"
        }

    projection = (ROOT / "infra/runtime/manager-secrets-init.py").read_text(encoding="utf-8")
    assert "os.O_NOFOLLOW" in projection
    assert "os.fsync" in projection
    assert "os.fchmod(descriptor, 0o400)" in projection
    assert "os.fchown(descriptor, profile.uid, profile.gid)" in projection
    assert projection.index("os.fchmod(descriptor, 0o400)") < projection.index(
        "os.fchown(descriptor, profile.uid, profile.gid)"
    )
    assert 'os.replace(temporary_path, profile.target_root / "current")' in projection
    assert 'name="database_authz"' in projection
    assert 'target_root=Path("/prepared/database-authz")' in projection
    assert '"maintenance_postgres_password"' in projection
    assert '"maintenance_redis_password"' in projection
    assert '"maintenance_s3_access_key"' in projection
    assert '"maintenance_s3_secret_key"' in projection
    assert "maintenance credential must be distinct from API credential" in projection

    api_entrypoint = (ROOT / "infra/runtime/api-entrypoint.sh").read_text(encoding="utf-8")
    maintenance_entrypoint = (ROOT / "infra/runtime/rq-worker-entrypoint.sh").read_text(
        encoding="utf-8"
    )
    mlflow_entrypoint = (ROOT / "infra/mlflow/entrypoint.sh").read_text(encoding="utf-8")
    for entrypoint in (api_entrypoint, maintenance_entrypoint, mlflow_entrypoint):
        assert "/run/secrets/current/" in entrypoint

    api_dockerfile = (ROOT / "apps/api/Dockerfile").read_text(encoding="utf-8")
    assert "--uid 10001" in api_dockerfile
    assert "--gid 10001" in api_dockerfile
    assert "USER 10001:10001" in api_dockerfile


def test_maintenance_database_authz_is_non_root_and_precedes_rq() -> None:
    compose = _load_yaml("infra/compose/manager.compose.yml")
    services = compose["services"]  # type: ignore[index]
    initializer = services["maintenance-db-authz"]
    worker = services["rq-worker"]

    assert initializer["image"] == "${API_IMAGE:-rvc-orchestrator-api:dev}"
    assert initializer["restart"] == "no"
    assert initializer["user"] == "10001:10001"
    assert initializer["read_only"] is True
    assert initializer["cap_drop"] == ["ALL"]
    assert initializer["pids_limit"] == 32
    assert initializer["entrypoint"] == [
        "python",
        "/opt/rvc/maintenance-db-authz.py",
        "apply",
    ]
    assert initializer["environment"] == {
        "POSTGRES_HOST": "postgres",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "${POSTGRES_DB:-rvc_orchestrator}",
        "POSTGRES_USER": "${POSTGRES_USER:-rvc_manager}",
        "MAINTENANCE_POSTGRES_USER": (
            "${MAINTENANCE_POSTGRES_USER:-rvc_maintenance}"
        ),
        "MAINTENANCE_TASK_TIMEOUT_SECONDS": "${MAINTENANCE_TASK_TIMEOUT_SECONDS:-300}",
    }
    assert initializer["volumes"] == [
        "../runtime/maintenance-db-authz.py:/opt/rvc/maintenance-db-authz.py:ro",
        "database_authz_runtime_secrets:/run/secrets:ro",
    ]
    assert initializer["networks"] == ["storage"]
    assert "secrets" not in initializer
    assert initializer["depends_on"]["api-migrate"] == {
        "condition": "service_completed_successfully"
    }
    assert worker["depends_on"]["maintenance-db-authz"] == {
        "condition": "service_completed_successfully"
    }
    assert "../runtime/maintenance-db-authz.py:/opt/rvc/maintenance-db-authz.py:ro" in worker[
        "volumes"
    ]


def test_proxy_health_uses_ipv4_loopback() -> None:
    compose = _load_yaml("infra/compose/manager.compose.yml")
    proxy = compose["services"]["proxy"]  # type: ignore[index]
    healthcheck = proxy["healthcheck"]["test"]  # type: ignore[index]
    assert "http://127.0.0.1/healthz" in healthcheck
    assert all("http://localhost/healthz" not in str(item) for item in healthcheck)


def test_proxy_exposes_dependency_aware_readiness_separately_from_liveness() -> None:
    template = (ROOT / "infra/proxy/templates/default.conf.template").read_text(encoding="utf-8")
    tls_example = (ROOT / "infra/proxy/examples/tls.conf.example").read_text(encoding="utf-8")

    assert "location = /healthz" in template
    assert "location = /readyz" in template
    assert "proxy_pass http://manager_api/ready;" in template
    assert "location = /healthz" in tls_example
    assert "location = /readyz" in tls_example
    assert "proxy_pass http://api:8000/ready;" in tls_example


def test_proxy_preserves_external_host_port_for_origin_validation() -> None:
    template = (ROOT / "infra/proxy/templates/default.conf.template").read_text(encoding="utf-8")
    tls_example = (ROOT / "infra/proxy/examples/tls.conf.example").read_text(encoding="utf-8")

    for configuration in (template, tls_example):
        assert configuration.count("proxy_set_header Host $http_host;") == 3
        assert configuration.count("proxy_set_header X-Forwarded-Host $http_host;") == 3
        assert "location ~ ^/api/v1/workers/jobs/[^/]+/samples$" in configuration
        assert "client_max_body_size 64k;" in configuration
        assert "proxy_set_header Host $host;" not in configuration

    # Session endpoints belong to the web BFF and therefore must continue to
    # fall through the general web location rather than the FastAPI /api route.
    assert "location /session" not in template
    assert "location /api/" in template
    assert "location / {" in template


def test_proxy_uses_operator_owned_scheme_and_discards_client_forwarding_headers() -> None:
    compose = _load_yaml("infra/compose/manager.compose.yml")
    proxy = compose["services"]["proxy"]  # type: ignore[index]
    api = compose["services"]["api"]  # type: ignore[index]
    web = compose["services"]["web"]  # type: ignore[index]
    template = (ROOT / "infra/proxy/templates/default.conf.template").read_text(encoding="utf-8")
    tls_example = (ROOT / "infra/proxy/examples/tls.conf.example").read_text(encoding="utf-8")
    entrypoint_path = ROOT / "infra/runtime/proxy-entrypoint.sh"
    entrypoint = entrypoint_path.read_text(encoding="utf-8")

    assert proxy["entrypoint"] == ["/opt/rvc/proxy-entrypoint.sh"]
    assert proxy["command"] == ["nginx", "-g", "daemon off;"]
    assert proxy["environment"]["ENVIRONMENT"] == "${ENVIRONMENT:-production}"
    assert proxy["environment"]["PUBLIC_SCHEME"] == "${PUBLIC_SCHEME:-http}"
    assert api["environment"]["PUBLIC_SCHEME"] == "${PUBLIC_SCHEME:-http}"
    assert web["environment"]["PUBLIC_SCHEME"] == "${PUBLIC_SCHEME:-http}"
    assert "PUBLIC_SCHEME" in proxy["environment"]["NGINX_ENVSUBST_FILTER"]
    assert "PUBLIC_HSTS_HEADER" in proxy["environment"]["NGINX_ENVSUBST_FILTER"]
    assert any(
        str(mount).endswith("proxy-entrypoint.sh:/opt/rvc/proxy-entrypoint.sh:ro")
        for mount in proxy["volumes"]
    )

    assert template.count("proxy_set_header X-Forwarded-Proto ${PUBLIC_SCHEME};") == 3
    assert template.count("proxy_set_header X-Forwarded-For $remote_addr;") == 3
    assert "$scheme" not in template
    assert "$proxy_add_x_forwarded_for" not in template
    assert "$http_x_forwarded_proto" not in template
    assert "${PUBLIC_HSTS_HEADER}" in template
    assert "proxy_hide_header Strict-Transport-Security;" in template
    assert tls_example.count("proxy_set_header X-Forwarded-Proto https;") == 3
    assert tls_example.count("proxy_set_header X-Forwarded-For $remote_addr;") == 3
    assert "$proxy_add_x_forwarded_for" not in tls_example
    assert 'add_header Strict-Transport-Security "max-age=31536000" always;' in tls_example

    assert os.access(entrypoint_path, os.X_OK)
    assert 'case "${PUBLIC_SCHEME:-}" in' in entrypoint
    assert "PUBLIC_HSTS_HEADER='max-age=31536000'" in entrypoint
    assert "set -- nginx -g 'daemon off;'" in entrypoint
    invalid = subprocess.run(
        [str(entrypoint_path), "nginx"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PUBLIC_SCHEME": "https; include /tmp/injected.conf"},
    )
    assert invalid.returncode != 0
    assert "must be exactly http or https" in invalid.stderr
    insecure_production = subprocess.run(
        [str(entrypoint_path), "nginx"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "ENVIRONMENT": "production", "PUBLIC_SCHEME": "http"},
    )
    assert insecure_production.returncode != 0
    assert "production proxy requires PUBLIC_SCHEME=https" in insecure_production.stderr


def test_manager_start_wrapper_requires_one_trusted_production_scheme(tmp_path: Path) -> None:
    install_root = tmp_path / "install"
    config_root = tmp_path / "config"
    (install_root / "current/infra/compose").mkdir(parents=True)
    config_root.mkdir()
    (install_root / "current/infra/compose/manager.compose.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    wrapper = ROOT / "installers/manager/compose.sh"

    for environment_text, expected in (
        ("ENVIRONMENT=production\nPUBLIC_SCHEME=http\n", "requires PUBLIC_SCHEME=https"),
        (
            "ENVIRONMENT=production\nPUBLIC_SCHEME=https\nPUBLIC_SCHEME=http\n",
            "exactly one PUBLIC_SCHEME",
        ),
        ("ENVIRONMENT=production\nPUBLIC_SCHEME=ftp\n", "exactly http or https"),
    ):
        (config_root / "manager.env").write_text(environment_text, encoding="utf-8")
        result = subprocess.run(
            [str(wrapper), "up", "-d"],
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "RVC_INSTALL_ROOT": str(install_root),
                "RVC_CONFIG_ROOT": str(config_root),
            },
        )
        assert result.returncode != 0
        assert expected in result.stderr


def test_redis_secret_config_survives_user_drop() -> None:
    entrypoint = (ROOT / "infra/redis/entrypoint.sh").read_text(encoding="utf-8")
    compose = _load_yaml("infra/compose/manager.compose.yml")
    redis = compose["services"]["redis"]  # type: ignore[index]
    assert "mktemp /tmp/rvc-redis.XXXXXX" in entrypoint
    assert "XXXXXX.conf" not in entrypoint
    assert 'chown redis:redis "$config"' in entrypoint
    assert redis["secrets"] == ["redis_password", "maintenance_redis_password"]
    assert redis["environment"] == {
        "MAINTENANCE_REDIS_USER": "${MAINTENANCE_REDIS_USER:-rvc_maintenance}",
        "MAINTENANCE_REDIS_PASSWORD_FILE": "/run/secrets/maintenance_redis_password",
        "RQ_QUEUE_NAME": "${RQ_QUEUE_NAME:-rvc-maintenance}",
    }
    assert 'user %s on >%s resetkeys resetchannels' in entrypoint
    assert '~rvc:maintenance:*' in entrypoint
    assert '~rq:queue:%s' in entrypoint
    assert "+flushdb" not in entrypoint
    assert "+flushall" not in entrypoint
    assert "+config" not in entrypoint


def test_worker_compose_uses_scoped_secret_and_no_privileged_mode() -> None:
    compose = _load_yaml("infra/compose/worker.compose.yml")
    worker = compose["services"]["worker"]  # type: ignore[index]
    assert worker.get("privileged") is not True
    assert worker["environment"]["WORKER_TOKEN_FILE"] == "/run/secrets/worker_token"
    assert worker["environment"]["WORKER_CA_BUNDLE_PATH"] == ("${WORKER_CA_BUNDLE_PATH:-}")
    assert worker["environment"]["DATASET_MAX_ARCHIVE_BYTES"] == (
        "${DATASET_MAX_ARCHIVE_BYTES:-5368709120}"
    )
    assert worker["environment"]["DATASET_DOWNLOAD_MAX_ATTEMPTS"] == (
        "${DATASET_DOWNLOAD_MAX_ATTEMPTS:-3}"
    )
    assert worker["environment"]["RVC_NATIVE_SOURCE_ROOT"] == (
        "${RVC_NATIVE_SOURCE_ROOT:-/opt/rvc-webui}"
    )
    assert worker["environment"]["RVC_NATIVE_TRAINING_TIMEOUT_SECONDS"] == (
        "${RVC_NATIVE_TRAINING_TIMEOUT_SECONDS:-604800}"
    )
    assert worker["environment"]["RVC_NATIVE_UNVERIFIED_GPU_ACKNOWLEDGED"] == (
        "${RVC_NATIVE_UNVERIFIED_GPU_ACKNOWLEDGED:-false}"
    )
    assert worker["environment"]["SYSTEM_TELEMETRY_INTERVAL_SECONDS"] == (
        "${SYSTEM_TELEMETRY_INTERVAL_SECONDS:-60}"
    )
    assert "RVC_RUNTIME_ACTIVATION_PATH" not in worker["environment"]
    assert (
        "../worker/runtime/runtime-activation.json:/run/rvc-release/runtime-activation.json:ro"
    ) in worker["volumes"]
    assert (
        "${WORKER_CA_BUNDLE_HOST_DIR:-/etc/rvc-orchestrator/worker/ca}:/etc/rvc-worker/ca:ro"
    ) in worker["volumes"]
    assert all(
        "${" not in str(volume)
        for volume in worker["volumes"]
        if "runtime-activation.json" in str(volume)
    )
    assert worker["secrets"] == ["worker_token"]
    assert all("docker.sock" not in str(volume) for volume in worker["volumes"])
    installer = (ROOT / "installers/worker/install.sh").read_text(encoding="utf-8")
    assert "SYSTEM_TELEMETRY_INTERVAL_SECONDS=60" in installer
    assert "--ca-bundle-file" in installer
    assert '--required-source-uid "$CONFIG_OWNER_UID"' in installer
    assert "WORKER_CA_BUNDLE_PATH=$CUSTOM_CA_CONTAINER_PATH" not in installer
    assert "target_ca_container_path=$CUSTOM_CA_CONTAINER_PATH" in installer


def test_worker_custom_ca_contract_is_fixed_read_only_and_revalidated_before_start() -> None:
    settings = (ROOT / "apps/worker/src/rvc_worker/settings.py").read_text(encoding="utf-8")
    tls = (ROOT / "apps/worker/src/rvc_worker/tls.py").read_text(encoding="utf-8")
    client = (ROOT / "apps/worker/src/rvc_worker/client.py").read_text(encoding="utf-8")
    wrapper = (ROOT / "installers/worker/compose.sh").read_text(encoding="utf-8")
    builder = (ROOT / "installers/worker/build-bundle.sh").read_text(encoding="utf-8")
    bundle_readme = (ROOT / "installers/worker/BUNDLE_README.md").read_text(encoding="utf-8")
    bundle_testing = (ROOT / "installers/common/BUNDLE_TESTING.md").read_text(encoding="utf-8")

    fixed_path = "/etc/rvc-worker/ca/custom-ca.pem"
    assert "DEFAULT_CUSTOM_CA_BUNDLE_PATH" in settings
    assert fixed_path in tls
    assert fixed_path in wrapper
    assert "verify=False" not in client
    assert "verify=false" not in client.lower()
    assert "ProxyHandler({})" in client
    assert "HTTPSHandler(context=self._ssl_context)" in client
    assert client.count("trust_env=False") >= 7
    assert "create_worker_ssl_context(ca_bundle_path)" in client
    assert "verify_worker_ca_before_start" in wrapper
    assert "up|start|restart|run|create)" in wrapper
    assert '"$stage/common/worker_ca.py"' in builder
    for guide in (bundle_readme, bundle_testing):
        assert "--ca-bundle-file /root/rvc-worker-custom-ca.pem" in guide
        assert "Public CA" in guide
        assert "set -Eeuo pipefail" in guide
        assert "native mode requires a Worker bundle with a verified offline RVC runtime" in guide
        assert "RVC_RUNNER_MODE=fake" in guide
        assert "systemctl is-active --quiet rvc-orchestrator-worker.service" in guide


def test_example_environment_contains_no_secret_value() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    forbidden_assignments = (
        "POSTGRES_PASSWORD=",
        "REDIS_PASSWORD=",
        "MAINTENANCE_POSTGRES_PASSWORD=",
        "MAINTENANCE_REDIS_PASSWORD=",
        "MAINTENANCE_S3_ACCESS_KEY=",
        "MAINTENANCE_S3_SECRET_KEY=",
        "MINIO_ROOT_PASSWORD=",
        "WORKER_TOKEN=",
        "JWT_SECRET=",
    )
    assert not any(item in example for item in forbidden_assignments)


def test_manager_projects_file_backed_jwt_secret_for_api_and_migrations() -> None:
    compose = _load_yaml("infra/compose/manager.compose.yml")
    services = compose["services"]  # type: ignore[index]
    for service_name in ("api-migrate", "api"):
        service = services[service_name]
        assert service["environment"]["PROCESS_ROLE"] == "api"
        assert service["environment"]["JWT_SECRET_FILE"] == ("/run/secrets/current/jwt_secret")
        assert "secrets" not in service
        assert "api_runtime_secrets:/run/secrets:ro" in service["volumes"]
    assert "jwt_secret" in services["manager-secrets-init"]["secrets"]
    assert compose["secrets"]["jwt_secret"]["file"].endswith("/jwt_secret")  # type: ignore[index]


def test_minio_service_users_have_exact_bucket_scoped_policies() -> None:
    script = (ROOT / "infra/minio/init.sh").read_text(encoding="utf-8")
    compose = _load_yaml("infra/compose/manager.compose.yml")
    initializer = compose["services"]["minio-init"]  # type: ignore[index]

    assert 'write_bucket_policy "$app_policy_path" "$S3_BUCKET"' in script
    assert 'write_bucket_policy "$mlflow_policy_path" "$MLFLOW_S3_BUCKET"' in script
    assert 'mc admin policy create local rvc-manager-app "$app_policy_path"' in script
    assert 'mc admin policy create local rvc-mlflow-artifacts "$mlflow_policy_path"' in script
    assert 'attach_exact_policy "$app_access_key" rvc-manager-app' in script
    assert 'attach_exact_policy "$mlflow_access_key" rvc-mlflow-artifacts' in script
    assert "s3:GetBucketLocation" in script
    assert "s3:ListBucket" in script
    assert "s3:GetObject" in script
    assert "s3:PutObject" in script
    assert "s3:DeleteObject" in script
    assert "mc admin policy attach local readwrite" not in script
    assert 'mc admin policy detach local "$built_in_policy"' in script
    assert '"Resource": ["arn:aws:s3:::$bucket"]' in script
    assert '"Resource": ["arn:aws:s3:::$bucket/*"]' in script
    assert "write_maintenance_cleanup_policy" in script
    assert '"s3:DeleteObject"' in script
    assert 'arn:aws:s3:::$bucket/datasets/staging/*' in script
    assert 'arn:aws:s3:::$bucket/test-sets/staging/*' in script
    assert "rvc-maintenance-staging-cleanup" in script
    assert initializer["environment"]["MAINTENANCE_S3_ACCESS_KEY_FILE"] == (
        "/run/secrets/maintenance_s3_access_key"
    )
    assert initializer["environment"]["MAINTENANCE_S3_SECRET_KEY_FILE"] == (
        "/run/secrets/maintenance_s3_secret_key"
    )
    assert "maintenance_s3_access_key" in initializer["secrets"]
    assert "maintenance_s3_secret_key" in initializer["secrets"]


def test_api_uses_redacted_structured_request_logging() -> None:
    compose = _load_yaml("infra/compose/manager.compose.yml")
    api = compose["services"]["api"]  # type: ignore[index]
    assert "--no-access-log" in api["command"]
    assert api["environment"]["LOG_LEVEL"] == "${LOG_LEVEL:-INFO}"
    assert api["environment"]["WORKER_OFFLINE_SECONDS"] == ("${WORKER_OFFLINE_SECONDS:-180}")
    assert api["environment"]["LEASE_RECOVERY_MAX_ATTEMPTS"] == (
        "${LEASE_RECOVERY_MAX_ATTEMPTS:-3}"
    )
    assert api["environment"]["RATE_LIMIT_ENABLED"] == "${RATE_LIMIT_ENABLED:-true}"
    assert api["environment"]["RATE_LIMIT_FAIL_CLOSED"] == ("${RATE_LIMIT_FAIL_CLOSED:-true}")
    assert api["environment"]["MLFLOW_ENABLED"] == "${MLFLOW_ENABLED:-true}"
    assert api["environment"]["MLFLOW_FAIL_CLOSED"] == ("${MLFLOW_FAIL_CLOSED:-false}")
    assert api["environment"]["MLFLOW_READINESS_TIMEOUT_SECONDS"] == (
        "${MLFLOW_READINESS_TIMEOUT_SECONDS:-1}"
    )
    assert api["environment"]["RQ_ENABLED"] == "${RQ_ENABLED:-true}"
    assert api["environment"]["MAINTENANCE_CLEANUP_GRACE_SECONDS"] == (
        "${MAINTENANCE_CLEANUP_GRACE_SECONDS:-604800}"
    )
    assert api["environment"]["MAINTENANCE_TASK_HEARTBEAT_SECONDS"] == (
        "${MAINTENANCE_TASK_HEARTBEAT_SECONDS:-15}"
    )
    assert api["environment"]["EXPERIMENT_JSON_MAX_BYTES"] == (
        "${EXPERIMENT_JSON_MAX_BYTES:-16384}"
    )
    assert api["environment"]["USER_LIFECYCLE_JSON_MAX_BYTES"] == (
        "${USER_LIFECYCLE_JSON_MAX_BYTES:-16384}"
    )
    assert api["environment"]["WORKER_TELEMETRY_JSON_MAX_BYTES"] == (
        "${WORKER_TELEMETRY_JSON_MAX_BYTES:-2097152}"
    )
    assert "mlflow" not in api["depends_on"]


def test_manager_object_storage_is_remote_worker_reachable_and_spooled() -> None:
    compose = _load_yaml("infra/compose/manager.compose.yml")
    services = compose["services"]  # type: ignore[index]
    for service_name in ("api-migrate", "api"):
        environment = services[service_name]["environment"]
        assert environment["STORAGE_BACKEND"] == "${STORAGE_BACKEND:-s3}"
        assert environment["S3_ENDPOINT_URL"] == "http://minio:9000"
        assert "S3_PRESIGN_ENDPOINT_URL" in environment
        assert environment["ARTIFACT_MAX_BYTES"] == "${ARTIFACT_MAX_BYTES:-5368709120}"
        assert environment["ARTIFACT_ATTEMPT_MAX_SESSIONS"] == (
            "${ARTIFACT_ATTEMPT_MAX_SESSIONS:-256}"
        )
        assert environment["ARTIFACT_ATTEMPT_MAX_BYTES"] == (
            "${ARTIFACT_ATTEMPT_MAX_BYTES:-107374182400}"
        )
    api = services["api"]
    assert api["environment"]["DATASET_DOWNLOAD_TTL_SECONDS"] == (
        "${DATASET_DOWNLOAD_TTL_SECONDS:-60}"
    )
    assert api["environment"]["ARTIFACT_VERIFICATION_SPOOL_DIR"].endswith("/verify")
    assert "artifact_spool:/var/lib/rvc-artifact-spool" in api["volumes"]
    assert api["depends_on"]["artifact-spool-init"]["condition"] == (
        "service_completed_successfully"
    )
    initializer = services["artifact-spool-init"]
    assert initializer["network_mode"] == "none"
    assert initializer["user"] == "0:0"
    assert initializer.get("privileged") is not True
    assert "artifact_spool" in compose["volumes"]  # type: ignore[operator]


def test_dataset_ingestion_workspace_is_private_and_rvc_owned() -> None:
    compose = _load_yaml("infra/compose/manager.compose.yml")
    services = compose["services"]  # type: ignore[index]
    api = services["api"]
    assert api["environment"]["DATASET_INGESTION_ROOT"] == ("/var/lib/rvc-dataset-ingestion")
    assert "dataset_ingestion:/var/lib/rvc-dataset-ingestion" in api["volumes"]
    assert api["depends_on"]["dataset-ingestion-init"]["condition"] == (
        "service_completed_successfully"
    )
    initializer = services["dataset-ingestion-init"]
    assert initializer["network_mode"] == "none"
    assert initializer["user"] == "0:0"
    assert initializer["entrypoint"] == ["/usr/bin/install"]
    assert "0700" in initializer["command"]
    assert "rvc" in initializer["command"]
    assert initializer.get("privileged") is not True
    assert "dataset_ingestion" in compose["volumes"]  # type: ignore[operator]


def test_example_environment_declares_artifact_transfer_limits() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for assignment in (
        "STORAGE_BACKEND=s3",
        "S3_PRESIGN_ENDPOINT_URL=http://127.0.0.1:9000",
        "ARTIFACT_UPLOAD_TTL_SECONDS=3600",
        "ARTIFACT_MAX_BYTES=5368709120",
        "ARTIFACT_FINALIZING_STALE_SECONDS=7200",
        "ARTIFACT_ATTEMPT_MAX_SESSIONS=256",
        "ARTIFACT_ATTEMPT_MAX_BYTES=107374182400",
        "DATASET_UPLOAD_TTL_SECONDS=3600",
        "DATASET_DOWNLOAD_TTL_SECONDS=60",
        "DATASET_UPLOAD_MAX_BYTES=5368709120",
        "DATASET_OWNER_MAX_SESSIONS=8",
        "DATASET_OWNER_MAX_BYTES=21474836480",
        "DATASET_FINALIZING_STALE_SECONDS=1800",
        "DATASET_FINALIZING_HEARTBEAT_SECONDS=30",
        "DATASET_MAX_ENTRIES=10000",
        "DATASET_MAX_TOTAL_UNCOMPRESSED_BYTES=21474836480",
        "EXPERIMENT_JSON_MAX_BYTES=16384",
        "USER_LIFECYCLE_JSON_MAX_BYTES=16384",
        "WORKER_TELEMETRY_JSON_MAX_BYTES=2097152",
        "TELEMETRY_SPOOL_MAX_BYTES=268435456",
        "SYSTEM_TELEMETRY_INTERVAL_SECONDS=60",
        "ARTIFACT_UPLOAD_TIMEOUT_SECONDS=3600",
        "ARTIFACT_UPLOAD_MAX_ATTEMPTS=3",
        "DATASET_DOWNLOAD_TIMEOUT_SECONDS=3600",
        "DATASET_DOWNLOAD_MAX_ATTEMPTS=3",
        "DATASET_MAX_ARCHIVE_BYTES=5368709120",
        "DATASET_MAX_FILE_BYTES=2147483648",
        "DATASET_MAX_TOTAL_BYTES=21474836480",
        "RVC_NATIVE_SOURCE_ROOT=/opt/rvc-webui",
        "RVC_NATIVE_TRAINING_TIMEOUT_SECONDS=604800",
        "RVC_GPU_SMOKE_VERIFIED=false",
        "RVC_PROFILE_STAGE_SET_VERIFIED=false",
        "RVC_NATIVE_UNVERIFIED_GPU_ACKNOWLEDGED=false",
        "LOG_LEVEL=INFO",
        "WORKER_OFFLINE_SECONDS=180",
        "LEASE_RECOVERY_MAX_ATTEMPTS=3",
        "RATE_LIMIT_ENABLED=true",
        "RATE_LIMIT_FAIL_CLOSED=true",
        "MLFLOW_ENABLED=true",
        "MLFLOW_FAIL_CLOSED=false",
        "MLFLOW_READINESS_TIMEOUT_SECONDS=1",
    ):
        assert assignment in example


def test_manager_admin_bootstrap_uses_a_temporary_read_only_password_mount() -> None:
    script = (ROOT / "installers/manager/bootstrap-admin.sh").read_text(encoding="utf-8")
    assert "--password-file" in script
    assert "--volume" in script
    assert ":ro" in script
    assert "ADMIN_BOOTSTRAP_PASSWORD=" not in script
    assert "--user 0:0" in script


def test_manager_admin_bootstrap_never_passes_password_value(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    config_root = tmp_path / "config"
    binary_root = install_root / "bin"
    binary_root.mkdir(parents=True)
    config_root.mkdir()
    (config_root / "manager.env").write_text("ENVIRONMENT=production\n", encoding="utf-8")
    password = tmp_path / "administrator-password"
    password.write_text("never-appear-in-process-arguments\n", encoding="utf-8")
    password.chmod(0o600)
    capture = tmp_path / "compose-arguments"
    compose = binary_root / "manager-compose"
    compose.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$CAPTURE"\n',
        encoding="utf-8",
    )
    compose.chmod(0o755)
    environment = {
        **os.environ,
        "RVC_INSTALL_ROOT": str(install_root),
        "RVC_CONFIG_ROOT": str(config_root),
        "CAPTURE": str(capture),
    }

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "installers/manager/bootstrap-admin.sh"),
            "--email",
            "admin@example.test",
            "--password-file",
            str(password),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    observed = capture.read_text(encoding="utf-8")
    assert "never-appear-in-process-arguments" not in observed + result.stdout + result.stderr
    assert "--user\n0:0" in observed
    assert f"{password}:/run/rvc-bootstrap/admin-password:ro" in observed
    assert "--password-file\n/run/rvc-bootstrap/admin-password" in observed


def test_installer_common_library_rejects_symlink_secrets_and_checksum_traversal() -> None:
    library = (ROOT / "installers/common/lib.sh").read_text(encoding="utf-8")
    verifier = (ROOT / "installers/common/image_bundle.py").read_text(encoding="utf-8")
    assert "! -L $path" in library
    assert "! -L $source" in library
    assert "verify-ledger --root" in library
    assert "follow_symlinks=False" in verifier
    assert "checksum ledger contains an unsafe path" in verifier
    assert "checksum inventory contains a non-regular entry" in verifier
    assert "rvc_is_exact_source_tree_root" in library
    assert "only an exact Git source root may omit SHA256SUMS" in library
    for component in ("manager", "worker"):
        installer = (ROOT / f"installers/{component}/install.sh").read_text(encoding="utf-8")
        assert "source_tree_install=0" in installer
        assert "source_tree_install=1" in installer
        assert 'rvc_verify_bundle_checksums "$BUNDLE_ROOT" "$source_tree_install"' in installer


def test_manager_full_stack_smoke_is_isolated_and_checks_real_boundaries() -> None:
    script = (ROOT / "tests/infra/manager_full_stack_smoke.sh").read_text(encoding="utf-8")

    assert 'PROJECT="rvc-manager-stack-smoke-$$"' in script
    assert "RVC_STACK_SMOKE_SKIP_BUILD" in script
    assert "RVC_STACK_SMOKE_API_IMAGE" in script
    assert "RVC_STACK_SMOKE_WEB_IMAGE" in script
    assert "RVC_STACK_SMOKE_MLFLOW_IMAGE" in script
    assert "RVC_STACK_SMOKE_VERSION" in script
    assert "RVC_STACK_SMOKE_REVISION" in script
    assert "release-image smoke requires explicit API, Web, and MLflow images" in script
    assert 'if [ "$SKIP_BUILD" != 1 ]; then' in script
    assert "RVC_STACK_SMOKE_WORK_PARENT" in script
    assert 'WORK_PARENT="$ROOT/.rvc-stack-smoke"' in script
    assert 'WORK_ROOT=$(CDPATH= cd -- "$WORK_ROOT" && pwd -P)' in script
    assert "compose down --volumes --remove-orphans" in script
    assert "RVC_STACK_SMOKE_KEEP" in script
    assert "compose up -d --remove-orphans" in script
    assert "wait_for_url" in script
    assert 'wait_for_url "Manager proxy health"' in script
    assert 'wait_for_url "MLflow health"' in script
    assert 'wait_for_url "MinIO readiness"' in script
    assert "/readyz" in script
    assert "/healthz" in script
    assert "/minio/health/ready" in script
    assert "compose exec -T api id -u" in script
    assert "compose exec -T rq-worker id -u" in script
    assert "compose exec -T mlflow id -u" in script
    assert "compose exec -T web id -u" in script
    assert 'test -f "/app/.next/server/app/bff/artifacts/' in script
    assert 'test -f "/app/.next/server/app/bff/jobs/' in script
    assert "compose exec -T --user 0:0 api python" in script
    assert "compose exec -T --user 0:0 rq-worker python" in script
    assert "compose exec -T --user 0:0 mlflow python" in script
    assert 'getattr(os, "O_NOFOLLOW", 0)' in script
    assert "assert os.read(descriptor, 1), name" in script
    assert "stat.S_IMODE(base_info.st_mode) == 0o711" in script
    assert "stat.S_IMODE(generation_info.st_mode) == 0o710" in script
    assert "assert stat.S_ISREG(info.st_mode)" in script
    assert "except PermissionError:" in script
    assert "unexpectedly enumerated runtime secrets" in script
    assert '"jwt_secret", "minio_app_access_key"' in script
    assert '"mlflow_postgres_password", "mlflow_s3_access_key"' in script
    assert "rvc-manager-app" in script
    assert "rvc-mlflow-artifacts" in script
    assert 'if mc ls "app/$MLFLOW_S3_BUCKET"' in script
    assert 'if mc ls "flow/$S3_BUCKET"' in script
    assert '! mc ls "app/$MLFLOW_S3_BUCKET"' not in script
    assert '! mc ls "flow/$S3_BUCKET"' not in script
    assert "Manager full Compose stack smoke: PASS" in script
