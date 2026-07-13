from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
QUALIFIED_BUILDER = ROOT / "installers/worker/build-qualified-release.sh"
VERSION = "9.8.7"
COMMIT = hashlib.sha1(b"qualified-factory-orchestrator").hexdigest()
RVC_COMMIT = "7ef19867780cf703841ebafb565a4e47d1ea86ff"
FAIRSEQ_COMMIT = hashlib.sha1(b"qualified-factory-fairseq").hexdigest()
BASE_IMAGE = (
    "pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime@sha256:"
    + hashlib.sha256(b"qualified-factory-base").hexdigest()
)
RUNTIME_IMAGE = f"rvc-orchestrator-worker:{VERSION}"
RUNTIME_ID = "sha256:" + hashlib.sha256(b"qualified-runtime-image").hexdigest()
SWAPPED_RUNTIME_ID = "sha256:" + hashlib.sha256(
    b"qualified-runtime-image-swapped"
).hexdigest()


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _runtime_build_manifest() -> str:
    digest = hashlib.sha256
    return "\n".join(
        (
            "RUNTIME_BUILD_FORMAT_VERSION=1",
            "PRODUCT=rvc-training-orchestrator",
            "COMPONENT=worker-rvc-runtime",
            f"IMAGE={RUNTIME_IMAGE}",
            f"RELEASE_VERSION={VERSION}",
            f"ORCHESTRATOR_SOURCE_COMMIT={COMMIT}",
            f"BASE_IMAGE={BASE_IMAGE}",
            f"RVC_SOURCE_COMMIT={RVC_COMMIT}",
            f"RVC_SOURCE_MANIFEST_SHA256={digest(b'source').hexdigest()}",
            f"RVC_WHEELHOUSE_MANIFEST_SHA256={digest(b'wheels').hexdigest()}",
            f"RVC_ASSET_MANIFEST_SHA256={digest(b'assets').hexdigest()}",
            f"RVC_PROJECTION_MANIFEST_SHA256={digest(b'projection').hexdigest()}",
            f"RVC_FAIRSEQ_COMMIT={FAIRSEQ_COMMIT}",
            "RVC_TORCH_VERSION=2.6.0+cu124",
            "RVC_CUDA_RUNTIME_VERSION=12.4",
            "RVC_CUDNN_MAJOR=9",
            "GPU_SMOKE_VERIFIED=false",
            "PROFILE_STAGE_SET_VERIFIED=false",
            "",
        )
    )


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, str], dict[str, Path]]:
    repo = tmp_path / "repo"
    worker = repo / "installers/worker"
    common = repo / "installers/common"
    runtime = repo / "infra/worker/runtime"
    tools = repo / "tools"
    fake_bin = tmp_path / "fake-bin"
    for directory in (worker, common, runtime, tools, fake_bin):
        directory.mkdir(parents=True, exist_ok=True)

    shutil.copy2(QUALIFIED_BUILDER, worker / QUALIFIED_BUILDER.name)
    shutil.copy2(ROOT / "installers/common/lib.sh", common / "lib.sh")
    shutil.copy2(
        ROOT / "infra/worker/runtime/qualification.py",
        runtime / "qualification.py",
    )
    (common / "image_bundle.py").write_text(
        "# qualified factory verifier fixture\n", encoding="utf-8"
    )
    (tools / "verify_release_source.py").write_text(
        'print("Release source ignore closure verified (fixture)")\n',
        encoding="utf-8",
    )

    _write_executable(
        runtime / "build-runtime-image.sh",
        """#!/usr/bin/env bash
set -Eeuo pipefail
touch "$FAKE_RUNTIME_BUILDER_CALLED"
exit 97
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
[[ ${FAKE_BUNDLE_FAIL:-0} == 0 ]] || exit 31
mkdir -p "$output_dir"
archive="$output_dir/rvc-worker-$version-linux-amd64.tar.gz"
printf 'private qualified Worker bundle\n' > "$archive"
[[ ${FAKE_BUNDLE_PARTIAL_FAIL:-0} == 0 ]] || exit 32
printf 'private qualified Worker checksum\n' > "$archive.sha256"
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
arguments = dict(zip(sys.argv[1::2], sys.argv[2::2], strict=True))
output = pathlib.Path(arguments["--output-dir"])
if os.environ.get("FAKE_PUBLISH_FAIL") == "1":
    raise SystemExit(33)
if os.environ.get("FAKE_PUBLISH_PARTIAL_FAIL") == "1":
    (output / ".qualified-publish.partial").write_text("partial\\n", encoding="utf-8")
    raise SystemExit(34)
archive = pathlib.Path(arguments["--archive"])
checksum = pathlib.Path(arguments["--checksum"])
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
    printf '%s\n' "$FAKE_GIT_COMMIT"
    ;;
  *" status "*)
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

arguments = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(arguments) + "\\n")

if arguments[:2] == ["info", "--format"]:
    print(os.environ.get("FAKE_DOCKER_ARCHITECTURE", "amd64"))
    raise SystemExit(0)
if arguments[:2] == ["image", "inspect"] and len(arguments) == 3:
    raise SystemExit(0 if os.environ.get("FAKE_IMAGE_EXISTS", "1") == "1" else 1)
if arguments[:3] == ["image", "inspect", "--format"]:
    if os.environ.get("FAKE_IMAGE_EXISTS", "1") != "1":
        raise SystemExit(2)
    template = arguments[3]
    bundle_ready = pathlib.Path(os.environ["FAKE_BUNDLE_READY"]).exists()
    swapped = os.environ.get("FAKE_SWAP_AFTER_BUNDLE") == "1" and bundle_ready
    bad = os.environ.get("FAKE_BAD_IMAGE")
    if template == "{{.Id}}":
        if bad == "id":
            print("not-a-runtime-digest")
        elif swapped:
            print(os.environ["FAKE_SWAPPED_RUNTIME_ID"])
        else:
            print(os.environ["FAKE_RUNTIME_ID"])
    elif template == "{{.Os}}":
        print("windows" if bad == "os" else "linux")
    elif template == "{{.Architecture}}":
        print("arm64" if bad == "arch" else "amd64")
    elif template == '{{with index .Config "User"}}{{.}}{{end}}':
        print("0:0" if bad == "user" else "10001:10001")
    elif "org.opencontainers.image.version" in template:
        print("wrong-version" if bad == "version" else os.environ["FAKE_VERSION"])
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
raise SystemExit(98)
""",
    )

    inputs = tmp_path / "inputs"
    assets = inputs / "assets"
    assets.mkdir(parents=True)
    paths = {
        "runtime_build_manifest": inputs / "runtime-build-manifest.env",
        "assets": assets,
        "asset_manifest": assets / "assets-manifest.json",
        "qualification": inputs / "qualification.json",
        "qualification_evidence": inputs / "qualification-evidence.tar.gz",
        "output": tmp_path / "output",
        "bundle_args": tmp_path / "bundle-args.txt",
        "publish_args": tmp_path / "publish-args.txt",
        "docker_log": tmp_path / "docker.log",
        "bundle_ready": tmp_path / "bundle-ready",
        "runtime_builder_called": tmp_path / "runtime-builder-called",
    }
    paths["runtime_build_manifest"].write_text(
        _runtime_build_manifest(), encoding="utf-8"
    )
    paths["asset_manifest"].write_text("fixture asset manifest\n", encoding="utf-8")
    paths["qualification"].write_text("fixture qualification\n", encoding="utf-8")
    paths["qualification_evidence"].write_text(
        "fixture qualification evidence\n", encoding="utf-8"
    )

    environment = {
        **os.environ,
        "FAKE_BUNDLE_ARGS": str(paths["bundle_args"]),
        "FAKE_BUNDLE_READY": str(paths["bundle_ready"]),
        "FAKE_DOCKER_LOG": str(paths["docker_log"]),
        "FAKE_GIT_COMMIT": COMMIT,
        "FAKE_PUBLISH_ARGS": str(paths["publish_args"]),
        "FAKE_RUNTIME_BUILDER_CALLED": str(paths["runtime_builder_called"]),
        "FAKE_RUNTIME_ID": RUNTIME_ID,
        "FAKE_SWAPPED_RUNTIME_ID": SWAPPED_RUNTIME_ID,
        "FAKE_VERSION": VERSION,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    return worker / QUALIFIED_BUILDER.name, environment, paths


def _command(
    script: Path,
    paths: dict[str, Path],
    *,
    runtime_image_id: str,
) -> list[str]:
    return [
        "bash",
        str(script),
        "--version",
        VERSION,
        "--runtime-image-id",
        runtime_image_id,
        "--runtime-build-manifest",
        str(paths["runtime_build_manifest"]),
        "--assets",
        str(paths["assets"]),
        "--asset-manifest",
        str(paths["asset_manifest"]),
        "--qualification",
        str(paths["qualification"]),
        "--qualification-evidence",
        str(paths["qualification_evidence"]),
        "--output-dir",
        str(paths["output"]),
    ]


def _run(
    tmp_path: Path,
    *,
    missing_input: str | None = None,
    manifest_mutation: str | None = None,
    output_collision: str | None = None,
    runtime_image_id: str = RUNTIME_ID,
    **overrides: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Path]]:
    script, environment, paths = _fixture(tmp_path)
    if missing_input is not None:
        target = paths[missing_input]
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    if manifest_mutation == "product":
        content = paths["runtime_build_manifest"].read_text(encoding="utf-8")
        paths["runtime_build_manifest"].write_text(
            content.replace(
                "PRODUCT=rvc-training-orchestrator", "PRODUCT=unexpected-product"
            ),
            encoding="utf-8",
        )
    elif manifest_mutation == "commit":
        content = paths["runtime_build_manifest"].read_text(encoding="utf-8")
        paths["runtime_build_manifest"].write_text(
            content.replace(COMMIT, hashlib.sha1(b"wrong-commit").hexdigest()),
            encoding="utf-8",
        )
    elif manifest_mutation is not None:
        raise AssertionError(f"unknown manifest mutation: {manifest_mutation}")
    if output_collision is not None:
        paths["output"].mkdir()
        archive = paths["output"] / f"rvc-worker-{VERSION}-linux-amd64.tar.gz"
        target = archive if output_collision == "archive" else Path(f"{archive}.sha256")
        target.write_text("existing output\n", encoding="utf-8")
    result = subprocess.run(
        _command(script, paths, runtime_image_id=runtime_image_id),
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


def _assert_existing_image_untouched(paths: dict[str, Path]) -> None:
    commands = _docker_commands(paths)
    assert not any(command[:2] == ["image", "rm"] for command in commands)
    assert not any(command[:2] == ["image", "tag"] for command in commands)
    assert not any(command[:1] == ["tag"] for command in commands)
    assert not paths["runtime_builder_called"].exists()


def _assert_final_pair_absent(paths: dict[str, Path]) -> None:
    archive = paths["output"] / f"rvc-worker-{VERSION}-linux-amd64.tar.gz"
    assert not archive.exists()
    assert not Path(f"{archive}.sha256").exists()


def test_qualified_factory_reuses_image_and_publishes_verified_pair(
    tmp_path: Path,
) -> None:
    result, paths = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "qualified Worker bundle candidate created" in result.stdout

    bundle_arguments = paths["bundle_args"].read_text(encoding="utf-8").splitlines()
    assert bundle_arguments[:2] == ["--version", VERSION]
    assert bundle_arguments[bundle_arguments.index("--output-dir") + 1] != str(
        paths["output"]
    )
    assert bundle_arguments[
        bundle_arguments.index("--include-rvc-runtime-image") + 1
    ] == RUNTIME_IMAGE
    expected_inputs = {
        "--rvc-runtime-build-manifest": paths["runtime_build_manifest"],
        "--rvc-runtime-assets": paths["assets"],
        "--rvc-runtime-asset-manifest": paths["asset_manifest"],
        "--rvc-runtime-qualification": paths["qualification"],
        "--rvc-runtime-qualification-evidence": paths["qualification_evidence"],
    }
    for option, expected in expected_inputs.items():
        assert bundle_arguments[bundle_arguments.index(option) + 1] == str(expected)

    publish_arguments = paths["publish_args"].read_text(encoding="utf-8").splitlines()
    assert publish_arguments[publish_arguments.index("--output-dir") + 1] == str(
        paths["output"]
    )
    assert publish_arguments[publish_arguments.index("--component") + 1] == "worker"
    assert publish_arguments[publish_arguments.index("--version") + 1] == VERSION
    assert publish_arguments[publish_arguments.index("--source-commit") + 1] == COMMIT
    assert publish_arguments[publish_arguments.index("--runtime-image-id") + 1] == RUNTIME_ID
    archive = paths["output"] / f"rvc-worker-{VERSION}-linux-amd64.tar.gz"
    assert archive.is_file()
    assert Path(f"{archive}.sha256").is_file()
    _assert_existing_image_untouched(paths)


@pytest.mark.parametrize(
    "missing_input",
    (
        "runtime_build_manifest",
        "assets",
        "asset_manifest",
        "qualification",
        "qualification_evidence",
    ),
)
def test_qualified_factory_rejects_missing_input(
    tmp_path: Path, missing_input: str
) -> None:
    result, paths = _run(tmp_path, missing_input=missing_input)
    assert result.returncode != 0
    assert "input is missing or unsafe" in result.stderr
    assert not paths["bundle_args"].exists()
    assert not paths["publish_args"].exists()
    _assert_existing_image_untouched(paths)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        ({"FAKE_IMAGE_EXISTS": "0"}, "requires the existing core candidate"),
        ({"FAKE_GIT_DIRTY": "1"}, "require a clean source tree"),
        ({"FAKE_DOCKER_ARCHITECTURE": "arm64"}, "require an amd64 Docker daemon"),
    ),
)
def test_qualified_factory_rejects_invalid_host_state_before_bundle(
    tmp_path: Path, overrides: dict[str, str], expected: str
) -> None:
    result, paths = _run(tmp_path, **overrides)
    assert result.returncode != 0
    assert expected in result.stderr
    assert not paths["bundle_args"].exists()
    assert not paths["publish_args"].exists()
    _assert_existing_image_untouched(paths)


@pytest.mark.parametrize("collision", ("archive", "checksum"))
def test_qualified_factory_rejects_output_collision_before_docker(
    tmp_path: Path, collision: str
) -> None:
    result, paths = _run(tmp_path, output_collision=collision)
    assert result.returncode != 0
    assert "output already exists" in result.stderr
    assert not paths["docker_log"].exists()
    assert not paths["bundle_args"].exists()
    assert not paths["publish_args"].exists()
    _assert_existing_image_untouched(paths)


@pytest.mark.parametrize(
    ("manifest_mutation", "expected"),
    (
        ("product", "identity is unsupported"),
        ("commit", "differs from requested orchestrator_source_commit"),
    ),
)
def test_qualified_factory_rejects_build_manifest_mismatch(
    tmp_path: Path, manifest_mutation: str, expected: str
) -> None:
    result, paths = _run(tmp_path, manifest_mutation=manifest_mutation)
    assert result.returncode != 0
    assert expected in result.stderr
    assert not paths["bundle_args"].exists()
    assert not paths["publish_args"].exists()
    _assert_existing_image_untouched(paths)


def test_qualified_factory_rejects_requested_runtime_image_id_mismatch(
    tmp_path: Path,
) -> None:
    result, paths = _run(tmp_path, runtime_image_id=SWAPPED_RUNTIME_ID)
    assert result.returncode != 0
    assert "differs from the qualified core candidate ID" in result.stderr
    assert not paths["bundle_args"].exists()
    assert not paths["publish_args"].exists()
    _assert_existing_image_untouched(paths)


@pytest.mark.parametrize(
    ("bad_image", "expected"),
    (
        ("id", "image ID is not a SHA-256 digest"),
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
def test_qualified_factory_rejects_runtime_image_identity_mismatch(
    tmp_path: Path, bad_image: str, expected: str
) -> None:
    result, paths = _run(tmp_path, FAKE_BAD_IMAGE=bad_image)
    assert result.returncode != 0
    assert expected in result.stderr
    assert not paths["bundle_args"].exists()
    assert not paths["publish_args"].exists()
    _assert_existing_image_untouched(paths)


@pytest.mark.parametrize(
    "failure",
    ("bundle", "bundle-partial", "publisher", "publisher-partial"),
)
def test_qualified_factory_failure_never_publishes_final_pair(
    tmp_path: Path, failure: str
) -> None:
    overrides = {
        "bundle": {"FAKE_BUNDLE_FAIL": "1"},
        "bundle-partial": {"FAKE_BUNDLE_PARTIAL_FAIL": "1"},
        "publisher": {"FAKE_PUBLISH_FAIL": "1"},
        "publisher-partial": {"FAKE_PUBLISH_PARTIAL_FAIL": "1"},
    }[failure]
    result, paths = _run(tmp_path, **overrides)
    assert result.returncode != 0
    _assert_final_pair_absent(paths)
    _assert_existing_image_untouched(paths)


def test_qualified_factory_rejects_runtime_tag_swap_without_publication(
    tmp_path: Path,
) -> None:
    result, paths = _run(tmp_path, FAKE_SWAP_AFTER_BUNDLE="1")
    assert result.returncode != 0
    assert "runtime image tag changed" in result.stderr
    assert not paths["publish_args"].exists()
    _assert_final_pair_absent(paths)
    _assert_existing_image_untouched(paths)
