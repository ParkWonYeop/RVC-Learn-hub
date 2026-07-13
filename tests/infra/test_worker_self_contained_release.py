from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RELEASE_BUILDER = ROOT / "installers/worker/build-self-contained-release.sh"
VERSION = "9.8.7"
COMMIT = hashlib.sha1(b"factory-orchestrator").hexdigest()
RVC_COMMIT = "7ef19867780cf703841ebafb565a4e47d1ea86ff"
FAIRSEQ_COMMIT = hashlib.sha1(b"factory-fairseq").hexdigest()
BASE_IMAGE = (
    "pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime@sha256:"
    + hashlib.sha256(b"factory-base-image").hexdigest()
)
RUNTIME_IMAGE = f"rvc-orchestrator-worker:{VERSION}"
RUNTIME_ID = "sha256:" + hashlib.sha256(b"factory-runtime-image").hexdigest()
SWAPPED_RUNTIME_ID = "sha256:" + hashlib.sha256(b"swapped-runtime-image").hexdigest()
SOURCE_HASH = hashlib.sha256(b"factory-source-manifest").hexdigest()
WHEEL_HASH = hashlib.sha256(b"factory-wheelhouse-manifest").hexdigest()
ASSET_HASH = hashlib.sha256(b"factory-asset-manifest").hexdigest()
PROJECTION_HASH = hashlib.sha256(b"factory-projection-manifest").hexdigest()


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, str], dict[str, Path]]:
    repo = tmp_path / "repo"
    worker = repo / "installers/worker"
    common = repo / "installers/common"
    runtime = repo / "infra/worker/runtime"
    tools = repo / "tools"
    fake_bin = tmp_path / "fake-bin"
    for directory in (worker, common, runtime, tools, fake_bin):
        directory.mkdir(parents=True, exist_ok=True)

    shutil.copy2(RELEASE_BUILDER, worker / RELEASE_BUILDER.name)
    shutil.copy2(ROOT / "installers/common/lib.sh", common / "lib.sh")
    shutil.copy2(ROOT / "infra/worker/runtime/qualification.py", runtime / "qualification.py")
    (common / "image_bundle.py").write_text("# fixture verifier\n", encoding="utf-8")
    (tools / "verify_release_source.py").write_text(
        'print("Release source ignore closure verified (fixture)")\n',
        encoding="utf-8",
    )

    _write_executable(
        runtime / "build-runtime-image.sh",
        """#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\n' "$@" > "$FAKE_RUNTIME_ARGS"
[[ ${FAKE_RUNTIME_BUILD_FAIL:-0} == 0 ]] || exit 31
tag=
output_manifest=
output_image_id=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag) shift; tag=$1 ;;
    --output-manifest) shift; output_manifest=$1 ;;
    --output-image-id) shift; output_image_id=$1 ;;
  esac
  shift
done
mutation=${FAKE_BUILD_MANIFEST_MUTATION:-none}
{
  printf 'RUNTIME_BUILD_FORMAT_VERSION=1\n'
  if [[ $mutation != missing-product ]]; then
    printf 'PRODUCT=%s\n' "${FAKE_MANIFEST_PRODUCT:-rvc-training-orchestrator}"
  fi
  printf 'COMPONENT=worker-rvc-runtime\n'
  printf 'IMAGE=%s\n' "$tag"
  printf 'RELEASE_VERSION=%s\n' "${tag##*:}"
  printf 'ORCHESTRATOR_SOURCE_COMMIT=%s\n' "${FAKE_MANIFEST_COMMIT:-$FAKE_GIT_COMMIT}"
  printf 'BASE_IMAGE=%s\n' "$FAKE_BASE_IMAGE"
  printf 'RVC_SOURCE_COMMIT=%s\n' "$FAKE_RVC_COMMIT"
  printf 'RVC_SOURCE_MANIFEST_SHA256=%s\n' "$FAKE_SOURCE_HASH"
  printf 'RVC_WHEELHOUSE_MANIFEST_SHA256=%s\n' "$FAKE_WHEEL_HASH"
  printf 'RVC_ASSET_MANIFEST_SHA256=%s\n' "$FAKE_ASSET_HASH"
  printf 'RVC_PROJECTION_MANIFEST_SHA256=%s\n' "$FAKE_PROJECTION_HASH"
  printf 'RVC_FAIRSEQ_COMMIT=%s\n' "$FAKE_FAIRSEQ_COMMIT"
  printf 'RVC_TORCH_VERSION=2.6.0+cu124\n'
  printf 'RVC_CUDA_RUNTIME_VERSION=12.4\n'
  printf 'RVC_CUDNN_MAJOR=9\n'
  printf 'GPU_SMOKE_VERIFIED=false\n'
  printf 'PROFILE_STAGE_SET_VERIFIED=false\n'
  if [[ $mutation == extra-key ]]; then
    printf 'UNEXPECTED=value\n'
  fi
} > "$output_manifest"
touch "$FAKE_RUNTIME_READY"
printf '%s\n' "$FAKE_RUNTIME_ID" > "$output_image_id"
[[ ${FAKE_RUNTIME_POST_BUILD_FAIL:-0} == 0 ]] || exit 35
""",
    )
    _write_executable(
        worker / "build-bundle.sh",
        """#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\n' "$@" > "$FAKE_BUNDLE_ARGS"
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
printf 'private fixture Worker bundle\n' > \
  "$output_dir/rvc-worker-$version-linux-amd64.tar.gz"
if [[ ${FAKE_BUNDLE_PARTIAL_FAIL:-0} == 1 ]]; then
  exit 33
fi
[[ ${FAKE_BUNDLE_FAIL:-0} == 0 ]] || exit 32
printf 'private fixture checksum\n' > \
  "$output_dir/rvc-worker-$version-linux-amd64.tar.gz.sha256"
touch "$FAKE_BUNDLE_READY"
""",
    )
    _write_executable(
        common / "publish_release_bundle.py",
        """#!/usr/bin/env python3
import os
import pathlib
import shutil
import sys

pathlib.Path(os.environ["FAKE_PUBLISH_ARGS"]).write_text(
    "\\n".join(sys.argv[1:]) + "\\n", encoding="utf-8"
)
if os.environ.get("FAKE_PUBLISH_FAIL") == "1":
    raise SystemExit(34)
arguments = dict(zip(sys.argv[1::2], sys.argv[2::2], strict=True))
archive = pathlib.Path(arguments["--archive"])
checksum = pathlib.Path(arguments["--checksum"])
output = pathlib.Path(arguments["--output-dir"])
output.mkdir(parents=True, exist_ok=True)
shutil.copy2(archive, output / archive.name)
shutil.copy2(checksum, output / checksum.name)
""",
    )
    _write_executable(
        fake_bin / "git",
        """#!/usr/bin/env bash
set -eu
case " $* " in
  *" rev-parse "*)
    [[ ${FAKE_GIT_NO_HEAD:-0} == 0 ]] || exit 1
    printf '%s\n' "$FAKE_GIT_COMMIT"
    ;;
  *" status "*)
    [[ ${FAKE_GIT_STATUS_FAIL:-0} == 0 ]] || exit 2
    [[ ${FAKE_GIT_DIRTY:-0} == 0 ]] || printf ' M changed\n'
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
import pathlib
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")

runtime_ready = pathlib.Path(os.environ["FAKE_RUNTIME_READY"])
bundle_ready = pathlib.Path(os.environ["FAKE_BUNDLE_READY"])
bundle_swapped = os.environ.get("FAKE_SWAP_AFTER_BUNDLE") == "1" and bundle_ready.exists()
runtime_failure_swapped = (
    os.environ.get("FAKE_SWAP_AFTER_RUNTIME_FAILURE") == "1"
    and os.environ.get("FAKE_RUNTIME_POST_BUILD_FAIL") == "1"
    and runtime_ready.exists()
)
swapped = bundle_swapped or runtime_failure_swapped
current_id = os.environ["FAKE_SWAPPED_RUNTIME_ID"] if swapped else os.environ["FAKE_RUNTIME_ID"]

if args[:2] == ["info", "--format"]:
    print(os.environ.get("FAKE_DOCKER_ARCHITECTURE", "amd64"))
    raise SystemExit(0)
if args[:2] == ["image", "inspect"] and len(args) == 3:
    exists = os.environ.get("FAKE_RUNTIME_PREEXISTS") == "1" or runtime_ready.exists()
    raise SystemExit(0 if exists else 1)
if args[:3] == ["image", "inspect", "--format"]:
    template = args[3]
    if not runtime_ready.exists():
        raise SystemExit(2)
    bad = os.environ.get("FAKE_BAD_IMAGE")
    if template == "{{.Id}}":
        print(current_id)
    elif template == "{{.Os}}":
        print("windows" if bad == "os" else "linux")
    elif template == "{{.Architecture}}":
        print("arm64" if bad == "arch" else "amd64")
    elif template == '{{with index .Config "User"}}{{.}}{{end}}':
        print("0:0" if bad == "user" else "10001:10001")
    elif "org.opencontainers.image.version" in template:
        print("wrong" if bad == "version" else os.environ["FAKE_VERSION"])
    elif "org.opencontainers.image.revision" in template:
        print("0" * 40 if bad == "revision" else os.environ["FAKE_GIT_COMMIT"])
    elif "org.rvc-orchestrator.runtime" in template:
        print("fake" if bad == "kind" else "rvc")
    elif "org.rvc-orchestrator.gpu-smoke-verified" in template:
        print("true" if bad == "gpu-gate" else "false")
    elif "org.rvc-orchestrator.profile-stage-set-verified" in template:
        print("true" if bad == "profile-gate" else "false")
    else:
        raise SystemExit(2)
    raise SystemExit(0)
if args[:2] == ["image", "rm"] and len(args) == 3:
    runtime_ready.unlink(missing_ok=True)
    raise SystemExit(0)
raise SystemExit(2)
""",
    )

    inputs = tmp_path / "inputs"
    wheelhouse = inputs / "wheelhouse"
    assets = inputs / "assets"
    wheelhouse.mkdir(parents=True)
    assets.mkdir(parents=True)
    paths = {
        "source_archive": inputs / "rvc-source.tar.gz",
        "source_manifest": inputs / "source-manifest.json",
        "wheelhouse": wheelhouse,
        "wheelhouse_manifest": wheelhouse / "wheelhouse-manifest.json",
        "assets": assets,
        "asset_manifest": assets / "assets-manifest.json",
        "qualification": inputs / "qualification.json",
        "qualification_evidence": inputs / "qualification-evidence.tar.gz",
        "output": tmp_path / "output",
        "runtime_args": tmp_path / "runtime-args.txt",
        "bundle_args": tmp_path / "bundle-args.txt",
        "publish_args": tmp_path / "publish-args.txt",
        "docker_log": tmp_path / "docker.log",
        "runtime_ready": tmp_path / "runtime-ready",
        "bundle_ready": tmp_path / "bundle-ready",
    }
    for key in (
        "source_archive",
        "source_manifest",
        "wheelhouse_manifest",
        "asset_manifest",
        "qualification",
        "qualification_evidence",
    ):
        paths[key].write_text(f"fixture {key}\n", encoding="utf-8")

    environment = {
        **os.environ,
        "FAKE_ASSET_HASH": ASSET_HASH,
        "FAKE_BASE_IMAGE": BASE_IMAGE,
        "FAKE_BUNDLE_ARGS": str(paths["bundle_args"]),
        "FAKE_BUNDLE_READY": str(paths["bundle_ready"]),
        "FAKE_DOCKER_LOG": str(paths["docker_log"]),
        "FAKE_FAIRSEQ_COMMIT": FAIRSEQ_COMMIT,
        "FAKE_GIT_COMMIT": COMMIT,
        "FAKE_PROJECTION_HASH": PROJECTION_HASH,
        "FAKE_PUBLISH_ARGS": str(paths["publish_args"]),
        "FAKE_RUNTIME_ARGS": str(paths["runtime_args"]),
        "FAKE_RUNTIME_ID": RUNTIME_ID,
        "FAKE_RUNTIME_READY": str(paths["runtime_ready"]),
        "FAKE_RVC_COMMIT": RVC_COMMIT,
        "FAKE_SOURCE_HASH": SOURCE_HASH,
        "FAKE_SWAPPED_RUNTIME_ID": SWAPPED_RUNTIME_ID,
        "FAKE_VERSION": VERSION,
        "FAKE_WHEEL_HASH": WHEEL_HASH,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    return worker / RELEASE_BUILDER.name, environment, paths


def _command(script: Path, paths: dict[str, Path]) -> list[str]:
    return [
        "bash",
        str(script),
        "--version",
        VERSION,
        "--source-archive",
        str(paths["source_archive"]),
        "--source-manifest",
        str(paths["source_manifest"]),
        "--wheelhouse",
        str(paths["wheelhouse"]),
        "--wheelhouse-manifest",
        str(paths["wheelhouse_manifest"]),
        "--assets",
        str(paths["assets"]),
        "--asset-manifest",
        str(paths["asset_manifest"]),
        "--base-image",
        BASE_IMAGE,
        "--output-dir",
        str(paths["output"]),
    ]


def _run(
    tmp_path: Path,
    *,
    extra_args: list[str] | None = None,
    precreate: str | None = None,
    output_symlink: bool = False,
    **overrides: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Path]]:
    script, environment, paths = _fixture(tmp_path)
    if precreate is not None:
        paths["output"].mkdir()
        target = paths["output"] / f"rvc-worker-{VERSION}-linux-amd64.tar.gz"
        if precreate == "archive":
            target.write_text("existing\n", encoding="utf-8")
        elif precreate == "sidecar-symlink":
            Path(f"{target}.sha256").symlink_to(paths["output"] / "missing")
        else:
            raise AssertionError(f"unknown precreate mode: {precreate}")
    if output_symlink:
        real_output = tmp_path / "real-output"
        real_output.mkdir()
        paths["output"].symlink_to(real_output, target_is_directory=True)
    command = _command(script, paths)
    if extra_args:
        command.extend(extra_args)
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env={**environment, **overrides},
    )
    return result, paths


def _docker_commands(paths: dict[str, Path]) -> list[list[str]]:
    if not paths["docker_log"].exists():
        return []
    return [
        json.loads(line)
        for line in paths["docker_log"].read_text(encoding="utf-8").splitlines()
    ]


def test_worker_factory_publishes_guarded_candidate_from_private_bundle(tmp_path: Path) -> None:
    result, paths = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "core-only guarded candidate" in result.stderr

    runtime_arguments = paths["runtime_args"].read_text(encoding="utf-8").splitlines()
    assert runtime_arguments[:-4] == [
        "--source-archive",
        str(paths["source_archive"]),
        "--source-manifest",
        str(paths["source_manifest"]),
        "--wheelhouse",
        str(paths["wheelhouse"]),
        "--wheelhouse-manifest",
        str(paths["wheelhouse_manifest"]),
        "--assets",
        str(paths["assets"]),
        "--asset-manifest",
        str(paths["asset_manifest"]),
        "--base-image",
        BASE_IMAGE,
        "--tag",
        RUNTIME_IMAGE,
    ]
    assert runtime_arguments[-4] == "--output-manifest"
    assert "rvc-worker-release." in runtime_arguments[-3]
    assert runtime_arguments[-2] == "--output-image-id"
    assert "rvc-worker-release." in runtime_arguments[-1]

    bundle_arguments = paths["bundle_args"].read_text(encoding="utf-8").splitlines()
    assert bundle_arguments[:2] == ["--version", VERSION]
    assert bundle_arguments[2] == "--output-dir"
    assert bundle_arguments[3] != str(paths["output"])
    assert "rvc-worker-release." in bundle_arguments[3]
    assert bundle_arguments[4:7] == [
        "--self-contained",
        "--include-rvc-runtime-image",
        RUNTIME_IMAGE,
    ]
    assert "--rvc-runtime-qualification" not in bundle_arguments

    publish_arguments = paths["publish_args"].read_text(encoding="utf-8").splitlines()
    assert publish_arguments[publish_arguments.index("--output-dir") + 1] == str(
        paths["output"]
    )
    assert publish_arguments[publish_arguments.index("--runtime-image-id") + 1] == RUNTIME_ID
    assert (paths["output"] / f"rvc-worker-{VERSION}-linux-amd64.tar.gz").is_file()
    assert (paths["output"] / f"rvc-worker-{VERSION}-linux-amd64.tar.gz.sha256").is_file()
    assert not any(command[:2] == ["image", "rm"] for command in _docker_commands(paths))


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        ({"FAKE_GIT_NO_HEAD": "1"}, "committed 40-character source revision"),
        ({"FAKE_GIT_DIRTY": "1"}, "clean source tree"),
        ({"FAKE_GIT_STATUS_FAIL": "1"}, "source status could not be inspected"),
        ({"FAKE_DOCKER_ARCHITECTURE": "arm64"}, "require an amd64 Docker daemon"),
        ({"FAKE_RUNTIME_PREEXISTS": "1"}, "runtime image tag already exists"),
    ),
)
def test_worker_factory_fails_before_runtime_build(
    tmp_path: Path, overrides: dict[str, str], expected: str
) -> None:
    result, paths = _run(tmp_path, **overrides)
    assert result.returncode != 0
    assert expected in result.stderr
    assert not paths["runtime_args"].exists()
    assert not paths["bundle_args"].exists()


def test_worker_core_factory_rejects_qualification_inputs_before_docker(tmp_path: Path) -> None:
    result, paths = _run(
        tmp_path,
        extra_args=[
            "--qualification",
            str(tmp_path / "inputs/qualification.json"),
            "--qualification-evidence",
            str(tmp_path / "inputs/qualification-evidence.tar.gz"),
        ],
    )
    assert result.returncode != 0
    assert "unknown Worker release option: --qualification" in result.stderr
    assert not paths["docker_log"].exists()
    assert not paths["runtime_args"].exists()


@pytest.mark.parametrize(
    ("precreate", "output_symlink", "expected"),
    (
        ("archive", False, "output already exists"),
        ("sidecar-symlink", False, "output already exists"),
        (None, True, "output directory must be a real directory"),
    ),
)
def test_worker_factory_rejects_unsafe_final_output_before_docker(
    tmp_path: Path,
    precreate: str | None,
    output_symlink: bool,
    expected: str,
) -> None:
    result, paths = _run(
        tmp_path,
        precreate=precreate,
        output_symlink=output_symlink,
    )
    assert result.returncode != 0
    assert expected in result.stderr
    assert not paths["docker_log"].exists()
    assert not paths["runtime_args"].exists()


@pytest.mark.parametrize(
    ("bad_image", "expected"),
    (
        ("os", "must be linux/amd64"),
        ("arch", "must be linux/amd64"),
        ("user", "user must be 10001:10001"),
        ("version", "release labels differ"),
        ("revision", "release labels differ"),
        ("kind", "pre-qualification gates are invalid"),
        ("gpu-gate", "pre-qualification gates are invalid"),
        ("profile-gate", "pre-qualification gates are invalid"),
    ),
)
def test_worker_factory_rejects_runtime_identity_before_bundle(
    tmp_path: Path, bad_image: str, expected: str
) -> None:
    result, paths = _run(tmp_path, FAKE_BAD_IMAGE=bad_image)
    assert result.returncode != 0
    assert expected in result.stderr
    assert paths["runtime_args"].is_file()
    assert not paths["bundle_args"].exists()
    assert ["image", "rm", RUNTIME_IMAGE] in _docker_commands(paths)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        ({"FAKE_BUILD_MANIFEST_MUTATION": "missing-product"}, "fields differ"),
        ({"FAKE_BUILD_MANIFEST_MUTATION": "extra-key"}, "fields differ"),
        ({"FAKE_MANIFEST_PRODUCT": "wrong-product"}, "identity is unsupported"),
        ({"FAKE_MANIFEST_COMMIT": hashlib.sha1(b"wrong").hexdigest()}, "differs from requested"),
    ),
)
def test_worker_factory_rejects_noncanonical_build_manifest_and_cleans_tag(
    tmp_path: Path, overrides: dict[str, str], expected: str
) -> None:
    result, paths = _run(tmp_path, **overrides)
    assert result.returncode != 0
    assert expected in result.stderr
    assert not paths["bundle_args"].exists()
    assert ["image", "rm", RUNTIME_IMAGE] in _docker_commands(paths)


@pytest.mark.parametrize(
    "failure", ("runtime", "runtime-post-build", "bundle", "bundle-partial", "publisher")
)
def test_worker_factory_failure_never_publishes_final_pair(
    tmp_path: Path, failure: str
) -> None:
    overrides = {
        "runtime": {"FAKE_RUNTIME_BUILD_FAIL": "1"},
        "runtime-post-build": {"FAKE_RUNTIME_POST_BUILD_FAIL": "1"},
        "bundle": {"FAKE_BUNDLE_FAIL": "1"},
        "bundle-partial": {"FAKE_BUNDLE_PARTIAL_FAIL": "1"},
        "publisher": {"FAKE_PUBLISH_FAIL": "1"},
    }[failure]
    result, paths = _run(tmp_path, **overrides)
    assert result.returncode != 0
    assert not (paths["output"] / f"rvc-worker-{VERSION}-linux-amd64.tar.gz").exists()
    assert not (paths["output"] / f"rvc-worker-{VERSION}-linux-amd64.tar.gz.sha256").exists()
    removed = ["image", "rm", RUNTIME_IMAGE] in _docker_commands(paths)
    assert removed is (failure != "runtime")


def test_worker_factory_preserves_swapped_tag_after_runtime_builder_failure(
    tmp_path: Path,
) -> None:
    result, paths = _run(
        tmp_path,
        FAKE_RUNTIME_POST_BUILD_FAIL="1",
        FAKE_SWAP_AFTER_RUNTIME_FAILURE="1",
    )
    assert result.returncode != 0
    assert "runtime image builder failed" in result.stderr
    assert "refusing failure cleanup" in result.stderr
    assert ["image", "rm", RUNTIME_IMAGE] not in _docker_commands(paths)
    assert not (paths["output"] / f"rvc-worker-{VERSION}-linux-amd64.tar.gz").exists()


def test_worker_factory_refuses_cleanup_after_runtime_tag_swap(tmp_path: Path) -> None:
    result, paths = _run(tmp_path, FAKE_SWAP_AFTER_BUNDLE="1")
    assert result.returncode != 0
    assert "runtime image tag changed" in result.stderr
    assert "refusing failure cleanup" in result.stderr
    assert ["image", "rm", RUNTIME_IMAGE] not in _docker_commands(paths)
    assert not (paths["output"] / f"rvc-worker-{VERSION}-linux-amd64.tar.gz").exists()
