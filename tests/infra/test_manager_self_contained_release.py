from __future__ import annotations

import json
import os
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RELEASE_BUILDER = ROOT / "installers/manager/build-self-contained-release.sh"
VERSION = "9.8.7"
COMMIT = "1234567890abcdef1234567890abcdef12345678"

SOURCE_IMAGES = {
    "api": f"rvc-orchestrator-api:{VERSION}",
    "web": f"rvc-orchestrator-web:{VERSION}",
    "mlflow": f"rvc-orchestrator-mlflow:{VERSION}",
    "postgres": "postgres:16-alpine",
    "redis": "redis:7.4-alpine",
    "minio": "minio/minio:RELEASE.2025-04-22T22-12-26Z",
    "minio-client": "minio/mc:RELEASE.2025-04-16T18-13-26Z",
    "nginx": "nginx:1.27-alpine",
}
IMAGE_USERS = {
    "api": "10001:10001",
    "web": "nextjs",
    "mlflow": "10002:10002",
    "postgres": "postgres",
    "redis": "redis",
    "minio": "1000:1000",
    "minio-client": "",
    "nginx": "nginx",
}


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    repo = tmp_path / "repo"
    manager = repo / "installers/manager"
    common = repo / "installers/common"
    tools = repo / "tools"
    fake_bin = tmp_path / "fake-bin"
    for directory in (manager, common, tools, fake_bin):
        directory.mkdir(parents=True, exist_ok=True)

    shutil.copy2(RELEASE_BUILDER, manager / RELEASE_BUILDER.name)
    shutil.copy2(ROOT / "installers/common/lib.sh", common / "lib.sh")
    (tools / "verify_release_source.py").write_text(
        'print("Release source ignore closure verified (fixture)")\n',
        encoding="utf-8",
    )

    _write_executable(
        manager / "build-bundle.sh",
        """#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\\n' "$@" > "$FAKE_BUNDLE_ARGS"
version=
output_dir=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) shift; version=$1 ;;
    --output-dir) shift; output_dir=$1 ;;
  esac
  shift
done
mkdir -p "$output_dir"
printf 'fixture bundle\\n' > "$output_dir/rvc-manager-$version-linux-amd64.tar.gz"
printf 'fixture checksum\\n' > "$output_dir/rvc-manager-$version-linux-amd64.tar.gz.sha256"
""",
    )
    _write_executable(
        fake_bin / "git",
        """#!/usr/bin/env bash
set -eu
case " $* " in
  *" rev-parse "*)
    [[ ${FAKE_GIT_NO_HEAD:-0} == 0 ]] || exit 1
    printf '%s\\n' "$FAKE_GIT_COMMIT"
    ;;
  *" status "*)
    [[ ${FAKE_GIT_DIRTY:-0} == 0 ]] || printf ' M changed\\n'
    ;;
  *) exit 2 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")

if args[:2] == ["buildx", "version"]:
    raise SystemExit(0 if os.environ.get("FAKE_BUILDX_MISSING") != "1" else 2)
if args[:2] == ["buildx", "build"]:
    tag = args[args.index("--tag") + 1]
    if os.environ.get("FAKE_BUILD_FAIL_REFERENCE") == tag:
        raise SystemExit(2)
    raise SystemExit(0)
if args[:1] == ["build"]:
    tag = args[args.index("--tag") + 1]
    if os.environ.get("FAKE_BUILD_FAIL_REFERENCE") == tag:
        raise SystemExit(2)
    raise SystemExit(0)
if args and args[0] == "pull":
    reference = args[-1]
    if os.environ.get("FAKE_PULL_FAIL_REFERENCE") == reference:
        raise SystemExit(2)
    raise SystemExit(0)
if args[:2] == ["image", "inspect"]:
    template = args[3]
    reference = args[4]
    version = os.environ["FAKE_RELEASE_VERSION"]
    sources = {
        "api": f"rvc-orchestrator-api:{version}",
        "web": f"rvc-orchestrator-web:{version}",
        "mlflow": f"rvc-orchestrator-mlflow:{version}",
        "postgres": "postgres:16-alpine",
        "redis": "redis:7.4-alpine",
        "minio": "minio/minio:RELEASE.2025-04-22T22-12-26Z",
        "minio-client": "minio/mc:RELEASE.2025-04-16T18-13-26Z",
        "nginx": "nginx:1.27-alpine",
    }
    users = {
        "api": "10001:10001",
        "web": "nextjs",
        "mlflow": "10002:10002",
        "postgres": "postgres",
        "redis": "redis",
        "minio": "1000:1000",
        "minio-client": "",
        "nginx": "nginx",
    }
    role = next((key for key, value in sources.items() if value == reference), None)
    if role is None:
        raise SystemExit(2)
    if template == "{{.Id}}":
        print("sha256:" + f"{list(sources).index(role) + 1:064x}")
    elif template == "{{.Os}}":
        print("linux")
    elif template == "{{.Architecture}}":
        print("arm64" if os.environ.get("FAKE_BAD_ARCH_ROLE") == role else "amd64")
    elif template == "{{.Config.User}}":
        print("0:0" if os.environ.get("FAKE_BAD_USER_ROLE") == role else users[role])
    elif "org.opencontainers.image.version" in template:
        print("wrong" if os.environ.get("FAKE_BAD_LABEL_ROLE") == role else version)
    elif "org.opencontainers.image.revision" in template:
        print(os.environ["FAKE_GIT_COMMIT"])
    else:
        raise SystemExit(2)
    raise SystemExit(0)
raise SystemExit(2)
""",
    )

    docker_log = tmp_path / "docker.log"
    bundle_args = tmp_path / "bundle-args.txt"
    environment = {
        **os.environ,
        "FAKE_BUNDLE_ARGS": str(bundle_args),
        "FAKE_DOCKER_LOG": str(docker_log),
        "FAKE_GIT_COMMIT": COMMIT,
        "FAKE_RELEASE_VERSION": VERSION,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    return manager / RELEASE_BUILDER.name, docker_log, bundle_args, environment


def _command(script: Path, output: Path) -> list[str]:
    return [
        "bash",
        str(script),
        "--version",
        VERSION,
        "--schema-compatibility",
        "schema-v9",
        "--output-dir",
        str(output),
    ]


def _run(
    tmp_path: Path, **overrides: str
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    script, docker_log, bundle_args, environment = _fixture(tmp_path)
    output = tmp_path / "output"
    result = subprocess.run(
        _command(script, output),
        check=False,
        capture_output=True,
        text=True,
        env={**environment, **overrides},
    )
    return result, output, docker_log, bundle_args


def test_release_builder_builds_and_pulls_exact_amd64_closure(tmp_path: Path) -> None:
    result, output, docker_log, bundle_args = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr

    docker_commands = [
        json.loads(line) for line in docker_log.read_text(encoding="utf-8").splitlines()
    ]
    build_commands = [command for command in docker_commands if command[:2] == ["buildx", "build"]]
    assert len(build_commands) == 3
    expected_dockerfiles = (
        "apps/api/Dockerfile",
        "apps/web/Dockerfile",
        "infra/mlflow/Dockerfile",
    )
    for command, role, dockerfile in zip(
        build_commands,
        ("api", "web", "mlflow"),
        expected_dockerfiles,
        strict=True,
    ):
        assert command[command.index("--platform") + 1] == "linux/amd64"
        assert "--pull" in command
        assert "--load" in command
        assert command[command.index("--tag") + 1] == SOURCE_IMAGES[role]
        assert command[command.index("--file") + 1].endswith(dockerfile)
        assert f"RVC_RELEASE_VERSION={VERSION}" in command
        assert f"RVC_SOURCE_COMMIT={COMMIT}" in command
    assert "MLFLOW_BASE_IMAGE=ghcr.io/mlflow/mlflow:v3.1.1" in build_commands[2]

    pulls = [command for command in docker_commands if command[:1] == ["pull"]]
    assert pulls == [
        ["pull", "--platform", "linux/amd64", SOURCE_IMAGES[role]]
        for role in ("postgres", "redis", "minio", "minio-client", "nginx")
    ]

    expected_bundle_arguments = [
        "--version",
        VERSION,
        "--schema-compatibility",
        "schema-v9",
        "--output-dir",
        str(output),
        "--self-contained",
    ]
    for role, reference in SOURCE_IMAGES.items():
        expected_bundle_arguments.extend(("--include-image", f"{role}={reference}"))
    assert bundle_args.read_text(encoding="utf-8").splitlines() == expected_bundle_arguments
    assert (output / f"rvc-manager-{VERSION}-linux-amd64.tar.gz").is_file()
    assert (output / f"rvc-manager-{VERSION}-linux-amd64.tar.gz.sha256").is_file()


def test_release_builder_falls_back_to_platform_docker_build_without_buildx(
    tmp_path: Path,
) -> None:
    result, output, docker_log, bundle_args = _run(
        tmp_path,
        FAKE_BUILDX_MISSING="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "using docker build with final platform verification" in result.stderr

    docker_commands = [
        json.loads(line) for line in docker_log.read_text(encoding="utf-8").splitlines()
    ]
    build_commands = [command for command in docker_commands if command[:1] == ["build"]]
    assert len(build_commands) == 3
    for command, role in zip(build_commands, ("api", "web", "mlflow"), strict=True):
        assert command[command.index("--platform") + 1] == "linux/amd64"
        assert "--pull" in command
        assert "--load" not in command
        assert command[command.index("--tag") + 1] == SOURCE_IMAGES[role]
        assert f"RVC_RELEASE_VERSION={VERSION}" in command
        assert f"RVC_SOURCE_COMMIT={COMMIT}" in command
    assert bundle_args.is_file()
    assert (output / f"rvc-manager-{VERSION}-linux-amd64.tar.gz").is_file()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        ({"FAKE_GIT_NO_HEAD": "1"}, "committed 40-character source revision"),
        ({"FAKE_GIT_DIRTY": "1"}, "clean source tree"),
    ),
)
def test_release_builder_rejects_uncommitted_or_dirty_source_before_docker(
    tmp_path: Path, overrides: dict[str, str], expected: str
) -> None:
    result, _, docker_log, bundle_args = _run(tmp_path, **overrides)
    assert result.returncode != 0
    assert expected in result.stderr
    assert not docker_log.exists()
    assert not bundle_args.exists()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        ({"FAKE_BAD_ARCH_ROLE": "redis"}, "must be linux/amd64"),
        ({"FAKE_BAD_USER_ROLE": "api"}, "image user mismatch for role api"),
        ({"FAKE_BAD_USER_ROLE": "web"}, "image user mismatch for role web"),
        ({"FAKE_BAD_USER_ROLE": "mlflow"}, "image user mismatch for role mlflow"),
        ({"FAKE_BAD_LABEL_ROLE": "api"}, "release label mismatch for role api"),
    ),
)
def test_release_builder_rejects_platform_user_or_label_mismatch_before_bundle(
    tmp_path: Path, overrides: dict[str, str], expected: str
) -> None:
    result, _, _, bundle_args = _run(tmp_path, **overrides)
    assert result.returncode != 0
    assert expected in result.stderr
    assert not bundle_args.exists()


def test_release_builder_stops_when_dependency_pull_fails(tmp_path: Path) -> None:
    result, _, _, bundle_args = _run(
        tmp_path,
        FAKE_PULL_FAIL_REFERENCE=SOURCE_IMAGES["minio"],
    )
    assert result.returncode != 0
    assert not bundle_args.exists()


def test_release_builder_contract_matches_image_bundle_verifier() -> None:
    verifier = runpy.run_path(str(ROOT / "installers/common/image_bundle.py"))
    expected_source_reference = verifier["_expected_source_reference"]
    release_builder = RELEASE_BUILDER.read_text(encoding="utf-8")
    for role, reference in SOURCE_IMAGES.items():
        assert expected_source_reference("manager", VERSION, role) == reference
        assert reference.replace(VERSION, "$version") in release_builder, role
    for role, user in IMAGE_USERS.items():
        if role in {"api", "web", "mlflow"}:
            assert f'verify_image {role} "${role}_image" {user}' in release_builder
