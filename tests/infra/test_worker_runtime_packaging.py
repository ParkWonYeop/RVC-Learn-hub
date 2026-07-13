from __future__ import annotations

import argparse
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
import types
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "infra/worker/runtime"
VERIFY_SCRIPT = RUNTIME / "verify_inputs.py"
RVC_COMMIT = "7ef19867780cf703841ebafb565a4e47d1ea86ff"
FAIRSEQ_COMMIT = "1234567890abcdef1234567890abcdef12345678"


def _load_runtime_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


VERIFY = _load_runtime_module("verify_inputs", VERIFY_SCRIPT)


def test_release_runtime_lock_uses_torch_26_cu124_but_preserves_upstream_markers() -> None:
    lock = dict(
        line.split("=", 1)
        for line in (RUNTIME / "runtime.lock.env").read_text(encoding="utf-8").splitlines()
        if line
    )
    assert lock["RVC_TORCH_VERSION"] == "2.6.0+cu124"
    assert lock["RVC_TORCHVISION_VERSION"] == "0.21.0+cu124"
    assert lock["RVC_TORCHAUDIO_VERSION"] == "2.6.0+cu124"
    assert lock["RVC_CUDA_WHEEL_FLAVOR"] == "cu124"
    assert lock["RVC_CUDA_RUNTIME_VERSION"] == "12.4"
    assert lock["RVC_BASE_IMAGE_PREFIX"] == (
        "pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime@sha256:"
    )
    assert VERIFY.CRITICAL_WHEELS == {
        "torch": "2.6.0+cu124",
        "torchvision": "0.21.0+cu124",
        "torchaudio": "2.6.0+cu124",
        "fairseq": "0.12.2",
        "faiss-cpu": "1.7.4",
    }
    verifier_source = VERIFY_SCRIPT.read_text(encoding="utf-8")
    for upstream_marker in (
        'torch = "2.4.0"',
        'torchaudio = "2.4.0"',
        'torchvision = "0.19.0"',
    ):
        assert upstream_marker in verifier_source


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _rewrite_bundle_checksums(root: Path) -> None:
    files = sorted(path for path in root.rglob("*") if path.is_file() and path.name != "SHA256SUMS")
    (root / "SHA256SUMS").write_text(
        "".join(f"{_sha256(path)}  {path.relative_to(root).as_posix()}\n" for path in files),
        encoding="utf-8",
    )


def _source_input(root: Path) -> tuple[Path, Path]:
    tree_root = root / f"Retrieval-based-Voice-Conversion-WebUI-{RVC_COMMIT}"
    tree_root.mkdir(parents=True)
    requirements = "\n".join(
        (
            "joblib>=1.1.0",
            "numba",
            "numpy",
            "scipy",
            "librosa==0.9.1",
            "llvmlite",
            "fairseq @ git+https://github.com/One-sixth/fairseq.git",
            "faiss-cpu",
            "",
        )
    )
    pyproject = "\n".join(
        (
            "[tool.poetry.dependencies]",
            'torch = "2.4.0"',
            'torchaudio = "2.4.0"',
            'torchvision = "0.19.0"',
            "",
        )
    )
    (tree_root / "requirements-py311.txt").write_text(requirements, encoding="utf-8")
    (tree_root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (tree_root / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    for relative in (
        "infer/modules/train/train.py",
        "infer/modules/train/preprocess.py",
        "infer/modules/train/extract/extract_f0_print.py",
        "infer/modules/train/extract_feature_print.py",
    ):
        path = tree_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# reviewed training entrypoint\n", encoding="utf-8")
    unpacked_size = sum(path.stat().st_size for path in tree_root.rglob("*") if path.is_file())
    archive = root / "rvc-source.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(tree_root, arcname=tree_root.name)
    manifest = root / "source-manifest.json"
    _write_json(
        manifest,
        {
            "schema_version": 1,
            "kind": "rvc-source",
            "repository": "https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI",
            "commit": RVC_COMMIT,
            "archive": {
                "file": archive.name,
                "root": tree_root.name,
                "sha256": _sha256(archive),
                "size": archive.stat().st_size,
                "unpacked_size": unpacked_size,
                "source": (
                    "https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/"
                    f"archive/{RVC_COMMIT}.tar.gz"
                ),
            },
            "license": {
                "spdx": "MIT",
                "source": (
                    "https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/"
                    f"blob/{RVC_COMMIT}/LICENSE"
                ),
            },
        },
    )
    return archive, manifest


def _wheel(path: Path, project: str, version: str, *, compiled: bool) -> None:
    distribution = project.replace("-", "_")
    tag = "cp311-cp311-linux_x86_64" if compiled else "py3-none-any"
    dist_info = f"{distribution}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {project}\nVersion: {version}\n",
        )
        bundle.writestr(
            f"{dist_info}/WHEEL",
            f"Wheel-Version: 1.0\nRoot-Is-Purelib: {'false' if compiled else 'true'}\nTag: {tag}\n",
        )
        bundle.writestr(f"{distribution}/__init__.py", f'__version__ = "{version}"\n')


def _wheelhouse(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True)
    projects = {
        "anyio": "4.8.0",
        "certifi": "2025.1.31",
        "torch": "2.6.0+cu124",
        "torchvision": "0.21.0+cu124",
        "torchaudio": "2.6.0+cu124",
        "fairseq": "0.12.2",
        "faiss-cpu": "1.7.4",
        "h11": "0.14.0",
        "hatchling": "1.27.0",
        "httpcore": "1.0.7",
        "httpx": "0.27.2",
        "idna": "3.10",
        "pydantic": "2.10.6",
        "pyyaml": "6.0.2",
        "setuptools": "75.8.0",
        "sniffio": "1.3.1",
        "typing-extensions": "4.12.2",
    }
    projects.update(
        {
            project: f"1.0.{index}"
            for index, project in enumerate(sorted(VERIFY.REQUIRED_TRAINING_PROJECTS), start=1)
        }
    )
    records: list[dict[str, object]] = []
    lock_lines: list[str] = []
    compiled_projects = {"torch", "torchvision", "torchaudio", "faiss-cpu"}
    for project, version in projects.items():
        distribution = project.replace("-", "_")
        tag = "cp311-cp311-linux_x86_64" if project in compiled_projects else "py3-none-any"
        filename = f"{distribution}-{version}-{tag}.whl"
        wheel_path = root / filename
        _wheel(wheel_path, project, version, compiled=project in compiled_projects)
        digest = _sha256(wheel_path)
        source = f"https://files.pythonhosted.org/packages/{filename}"
        if project == "fairseq":
            source = f"https://github.com/One-sixth/fairseq/tree/{FAIRSEQ_COMMIT}"
        records.append(
            {
                "file": filename,
                "project": project,
                "version": version,
                "sha256": digest,
                "size": wheel_path.stat().st_size,
                "license": "LicenseRef-Runtime-Test-Reviewed",
                "source": source,
            }
        )
        lock_lines.append(f"{project}=={version} --hash=sha256:{digest}")
    lock = root / "requirements.lock"
    lock.write_text("\n".join(lock_lines) + "\n", encoding="utf-8")
    manifest = root / "wheelhouse-manifest.json"
    _write_json(
        manifest,
        {
            "schema_version": 1,
            "kind": "rvc-wheelhouse",
            "python": "3.11",
            "platform": "linux_x86_64",
            "cuda": "cu124",
            "torch": "2.6.0+cu124",
            "torchvision": "0.21.0+cu124",
            "torchaudio": "2.6.0+cu124",
            "fairseq": {
                "version": "0.12.2",
                "repository": "https://github.com/One-sixth/fairseq",
                "commit": FAIRSEQ_COMMIT,
            },
            "faiss_cpu": "1.7.4",
            "requirements": {
                "file": lock.name,
                "sha256": _sha256(lock),
                "size": lock.stat().st_size,
            },
            "wheels": records,
        },
    )
    return root, manifest


def _asset_input(root: Path, *, executable_tools: bool = False) -> tuple[Path, Path]:
    root.mkdir(parents=True)
    records: list[dict[str, object]] = []
    for relative in sorted(VERIFY.REQUIRED_ASSETS):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        executable = relative in {"runtime/bin/ffmpeg", "runtime/bin/ffprobe"}
        if executable and executable_tools:
            path.write_text(
                f"#!/bin/sh\necho '{path.name} reviewed-test-build'\n", encoding="utf-8"
            )
        else:
            path.write_bytes(f"reviewed-test-asset:{relative}".encode())
        path.chmod(0o755 if executable else 0o644)
        records.append(
            {
                "path": relative,
                "sha256": _sha256(path),
                "size": path.stat().st_size,
                "license": "LicenseRef-Runtime-Test-Reviewed",
                "source": f"https://example.test/rvc-assets/{relative}",
                "executable": executable,
            }
        )
    manifest = root / "assets-manifest.json"
    _write_json(
        manifest,
        {
            "schema_version": 1,
            "kind": "rvc-assets",
            "rvc_commit": RVC_COMMIT,
            "assets": records,
        },
    )
    return root, manifest


def _verified_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
    source_root = tmp_path / "source"
    source_root.mkdir()
    archive, source_manifest = _source_input(source_root)
    wheelhouse, wheel_manifest = _wheelhouse(tmp_path / "wheelhouse")
    assets, asset_manifest = _asset_input(tmp_path / "assets")
    return archive, source_manifest, wheelhouse, wheel_manifest, assets, asset_manifest


def test_runtime_input_verifier_and_verify_only_builder_accept_complete_pins(
    tmp_path: Path,
) -> None:
    archive, source_manifest, wheelhouse, wheel_manifest, assets, asset_manifest = _verified_inputs(
        tmp_path
    )
    result = subprocess.run(
        [
            sys.executable,
            str(VERIFY_SCRIPT),
            "all",
            "--source-manifest",
            str(source_manifest),
            "--source-archive",
            str(archive),
            "--wheelhouse-manifest",
            str(wheel_manifest),
            "--wheelhouse-root",
            str(wheelhouse),
            "--asset-manifest",
            str(asset_manifest),
            "--asset-root",
            str(assets),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    document = json.loads(result.stdout)
    assert document["source"]["commit"] == RVC_COMMIT
    assert document["wheelhouse"]["fairseq_commit"] == FAIRSEQ_COMMIT
    assert document["assets"]["asset_count"] == len(VERIFY.REQUIRED_ASSETS)

    verified_build = subprocess.run(
        [
            "bash",
            str(RUNTIME / "build-runtime-image.sh"),
            "--source-archive",
            str(archive),
            "--source-manifest",
            str(source_manifest),
            "--wheelhouse",
            str(wheelhouse),
            "--wheelhouse-manifest",
            str(wheel_manifest),
            "--assets",
            str(assets),
            "--asset-manifest",
            str(asset_manifest),
            "--verify-only",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert verified_build.returncode == 0, verified_build.stdout + verified_build.stderr
    assert "no image was built" in verified_build.stdout


def test_runtime_verifier_rejects_unpinned_or_tampered_inputs(tmp_path: Path) -> None:
    archive, source_manifest, wheelhouse, wheel_manifest, assets, asset_manifest = _verified_inputs(
        tmp_path
    )
    source_document = json.loads(source_manifest.read_text(encoding="utf-8"))
    source_document["commit"] = "0" * 40
    _write_json(source_manifest, source_document)
    assert (
        subprocess.run(
            [
                sys.executable,
                str(VERIFY_SCRIPT),
                "source",
                "--manifest",
                str(source_manifest),
                "--archive",
                str(archive),
            ],
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )

    (assets / "assets/hubert/hubert_base.pt").write_bytes(b"tampered")
    assert (
        subprocess.run(
            [
                sys.executable,
                str(VERIFY_SCRIPT),
                "assets",
                "--manifest",
                str(asset_manifest),
                "--root",
                str(assets),
            ],
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )

    lock = wheelhouse / "requirements.lock"
    lock.write_text(
        lock.read_text(encoding="utf-8").replace(
            "fairseq==0.12.2", "fairseq @ https://example.test/fairseq.whl"
        ),
        encoding="utf-8",
    )
    wheel_document = json.loads(wheel_manifest.read_text(encoding="utf-8"))
    wheel_document["requirements"]["sha256"] = _sha256(lock)
    wheel_document["requirements"]["size"] = lock.stat().st_size
    _write_json(wheel_manifest, wheel_document)
    assert (
        subprocess.run(
            [
                sys.executable,
                str(VERIFY_SCRIPT),
                "wheelhouse",
                "--manifest",
                str(wheel_manifest),
                "--root",
                str(wheelhouse),
            ],
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )


def test_runtime_preflight_passes_cpu_level_stubs_but_requires_gpu_by_default(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repository, asset_manifest = _asset_input(tmp_path / "repository", executable_tools=True)
    for relative in (
        "requirements-py311.txt",
        "pyproject.toml",
        "infer/modules/train/train.py",
        "infer/modules/train/preprocess.py",
        "infer/modules/train/extract/extract_f0_print.py",
        "infer/modules/train/extract_feature_print.py",
    ):
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# reviewed test source\n", encoding="utf-8")
    (repository / ".rvc-reviewed-commit").write_text(f"{RVC_COMMIT}\n", encoding="ascii")
    _, wheel_manifest = _wheelhouse(tmp_path / "wheelhouse")

    class FakeVersionInfo(tuple[int, int, int]):
        @property
        def major(self) -> int:
            return self[0]

        @property
        def minor(self) -> int:
            return self[1]

        @property
        def micro(self) -> int:
            return self[2]

    torch = types.ModuleType("torch")
    torch.__version__ = "2.6.0+cu124"  # type: ignore[attr-defined]
    torch.version = types.SimpleNamespace(cuda="12.4")  # type: ignore[attr-defined]
    torch.backends = types.SimpleNamespace(  # type: ignore[attr-defined]
        cudnn=types.SimpleNamespace(version=lambda: 90100)
    )
    torch.cuda = types.SimpleNamespace(  # type: ignore[attr-defined]
        is_available=lambda: False, device_count=lambda: 0
    )
    for name, version in (
        ("torch", "2.6.0+cu124"),
        ("torchvision", "0.21.0+cu124"),
        ("torchaudio", "2.6.0+cu124"),
        ("fairseq", "0.12.2"),
        ("faiss", "1.7.4"),
    ):
        module = torch if name == "torch" else types.ModuleType(name)
        module.__version__ = version  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.syspath_prepend(str(RUNTIME))
    preflight = _load_runtime_module("runtime_preflight", RUNTIME / "runtime_preflight.py")
    monkeypatch.setattr(preflight.sys, "version_info", FakeVersionInfo((3, 11, 9)))
    arguments = argparse.Namespace(
        allow_no_gpu=True,
        repository_root=repository,
        asset_manifest=asset_manifest,
        wheelhouse_manifest=wheel_manifest,
        expected_commit=RVC_COMMIT,
        ffmpeg=repository / "runtime/bin/ffmpeg",
        ffprobe=repository / "runtime/bin/ffprobe",
    )
    result = preflight.run_preflight(arguments)
    assert result["status"] == "cpu-verification-only"
    assert result["gpu_available"] is False
    arguments.allow_no_gpu = False
    try:
        preflight.run_preflight(arguments)
    except preflight.PreflightError as exc:
        assert "no CUDA GPU" in str(exc)
    else:
        raise AssertionError("GPU-required preflight unexpectedly passed")


def test_real_runtime_dockerfile_and_builder_are_network_closed() -> None:
    dockerfile = (ROOT / "apps/worker/Dockerfile.rvc").read_text(encoding="utf-8")
    builder = (RUNTIME / "build-runtime-image.sh").read_text(encoding="utf-8")
    assert "PIP_NO_INDEX=1" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "--no-index" in dockerfile
    assert "runtime_preflight.py" in dockerfile
    assert "apt-get" not in dockerfile
    assert "curl " not in dockerfile
    assert "git clone" not in dockerfile
    assert "--network=none" in builder
    assert "--pull=false" in builder
    assert "@sha256:[0-9a-f]{64}" in builder
    assert "{{.Os}}/{{.Architecture}}" in builder
    assert "base_platform == linux/amd64" in builder
    assert "import httpx, pydantic, rvc_orchestrator_contracts, rvc_worker, yaml" in dockerfile
    assert 'org.rvc-orchestrator.profile-stage-set-verified="false"' in dockerfile
    assert "org.rvc-orchestrator.rvc.projection.sha256" in dockerfile
    assert "RVC_PROJECTION_MANIFEST_SHA256" in builder


def test_projection_manifest_binds_code_config_and_assets(tmp_path: Path) -> None:
    source_parent = tmp_path / "source"
    source_parent.mkdir()
    _, source_manifest = _source_input(source_parent)
    root = source_parent / f"Retrieval-based-Voice-Conversion-WebUI-{RVC_COMMIT}"
    assets, asset_manifest = _asset_input(tmp_path / "assets")
    for path in assets.rglob("*"):
        if not path.is_file() or path == asset_manifest:
            continue
        destination = root / path.relative_to(assets)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        destination.chmod(path.stat().st_mode & 0o7777)
    installed_asset_manifest = root / "assets-manifest.json"
    shutil.copyfile(asset_manifest, installed_asset_manifest)
    config = root / "configs/v1/40k.json"
    config.parent.mkdir(parents=True)
    config.write_text("{}\n", encoding="utf-8")
    output = root / "projection-manifest.json"

    result = VERIFY.generate_projection_manifest(
        root,
        source_manifest,
        installed_asset_manifest,
        output,
    )

    document = json.loads(output.read_text(encoding="utf-8"))
    paths = {record["path"] for record in document["files"]}
    assert "infer/modules/train/train.py" in paths
    assert "assets/hubert/hubert_base.pt" in paths
    assert "runtime/crepe/full.pth" in paths
    assert "configs" in VERIFY.PROJECTION_DIRECTORIES
    assert "runtime/crepe" in VERIFY.PROJECTION_DIRECTORIES
    assert "runtime/crepe/full.pth" in VERIFY.REQUIRED_ASSETS
    assert result["manifest_sha256"] == _sha256(output)


def test_runtime_builder_rejects_input_changed_after_initial_verification(
    tmp_path: Path,
) -> None:
    archive, source_manifest, wheelhouse, wheel_manifest, assets, asset_manifest = _verified_inputs(
        tmp_path
    )
    wrapper = tmp_path / "verify-python"
    wrapper.write_text(
        """#!/bin/sh
set -u
"$REAL_PYTHON" "$@"
status=$?
if [ "$status" -eq 0 ] && [ ! -e "$MUTATION_MARKER" ]; then
  case " $* " in
    *"verify_inputs.py all"*)
      : > "$MUTATION_MARKER"
      printf 'changed-after-verification' >> "$MUTATE_PATH"
      ;;
  esac
fi
exit "$status"
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            str(RUNTIME / "build-runtime-image.sh"),
            "--source-archive",
            str(archive),
            "--source-manifest",
            str(source_manifest),
            "--wheelhouse",
            str(wheelhouse),
            "--wheelhouse-manifest",
            str(wheel_manifest),
            "--assets",
            str(assets),
            "--asset-manifest",
            str(asset_manifest),
            "--verify-only",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "MUTATE_PATH": str(archive),
            "MUTATION_MARKER": str(tmp_path / "mutated"),
            "REAL_PYTHON": sys.executable,
            "RVC_RUNTIME_VERIFY_PYTHON": str(wrapper),
        },
    )
    assert result.returncode != 0
    assert "source archive" in result.stderr


def test_worker_bundle_can_include_only_a_matching_verified_runtime(tmp_path: Path) -> None:
    assets, asset_manifest = _asset_input(tmp_path / "assets")
    asset_hash = _sha256(asset_manifest)
    source_hash = "a" * 64
    wheel_hash = "b" * 64
    projection_hash = "c" * 64
    base_image = "pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime@sha256:" + "d" * 64
    image = "rvc-orchestrator-worker:9.8.7"
    build_manifest = tmp_path / "runtime-build.env"
    build_manifest.write_text(
        "\n".join(
            (
                "RUNTIME_BUILD_FORMAT_VERSION=1",
                "PRODUCT=rvc-training-orchestrator",
                "COMPONENT=worker-rvc-runtime",
                f"IMAGE={image}",
                f"BASE_IMAGE={base_image}",
                f"RVC_SOURCE_COMMIT={RVC_COMMIT}",
                f"RVC_SOURCE_MANIFEST_SHA256={source_hash}",
                f"RVC_WHEELHOUSE_MANIFEST_SHA256={wheel_hash}",
                f"RVC_ASSET_MANIFEST_SHA256={asset_hash}",
                f"RVC_PROJECTION_MANIFEST_SHA256={projection_hash}",
                f"RVC_FAIRSEQ_COMMIT={FAIRSEQ_COMMIT}",
                "RVC_TORCH_VERSION=2.6.0+cu124",
                "RVC_CUDA_RUNTIME_VERSION=12.4",
                "RVC_CUDNN_MAJOR=9",
                "GPU_SMOKE_VERIFIED=false",
                "PROFILE_STAGE_SET_VERIFIED=false",
                "",
            )
        ),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import hashlib
import io
import json
import os
import sys
import tarfile

image = "rvc-orchestrator-worker:9.8.7"
labels = {
    "org.opencontainers.image.version": "9.8.7",
    "org.opencontainers.image.revision": "uncommitted",
    "org.rvc-orchestrator.runtime": "rvc",
    "org.rvc-orchestrator.rvc.commit": os.environ["RVC_SOURCE_COMMIT"],
    "org.rvc-orchestrator.rvc.python": "3.11",
    "org.rvc-orchestrator.rvc.torch": "2.6.0+cu124",
    "org.rvc-orchestrator.rvc.cuda": "12.4",
    "org.rvc-orchestrator.rvc.cudnn": "9",
    "org.rvc-orchestrator.rvc.base": os.environ["RVC_BASE_IMAGE"],
    "org.rvc-orchestrator.rvc.source.sha256": os.environ["RVC_SOURCE_HASH"],
    "org.rvc-orchestrator.rvc.wheelhouse.sha256": os.environ["RVC_WHEEL_HASH"],
    "org.rvc-orchestrator.rvc.assets.sha256": os.environ["RVC_ASSET_HASH"],
    "org.rvc-orchestrator.rvc.projection.sha256": os.environ["RVC_PROJECTION_HASH"],
    "org.rvc-orchestrator.rvc.fairseq.commit": os.environ["RVC_FAIRSEQ_COMMIT"],
    "org.rvc-orchestrator.gpu-smoke-verified": "false",
    "org.rvc-orchestrator.profile-stage-set-verified": "false",
}
config = json.dumps(
    {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Labels": labels, "User": "10001:10001"},
    },
    sort_keys=True,
    separators=(",", ":"),
).encode()
digest = hashlib.sha256(config).hexdigest()

if sys.argv[1:3] == ["image", "inspect"]:
    template = sys.argv[4]
    if template == "{{.Id}}":
        print(f"sha256:{digest}")
    elif template == "{{.Os}}":
        print("linux")
    elif template == "{{.Architecture}}":
        print("amd64")
    elif template == '{{with index .Config "User"}}{{.}}{{end}}':
        print("10001:10001")
    elif template == "{{json .RepoTags}}":
        print(json.dumps([image]))
    elif template == "{{json .RepoDigests}}":
        print("[]")
    else:
        matched = next((value for key, value in labels.items() if key in template), None)
        if matched is None:
            raise SystemExit(2)
        print(matched)
    raise SystemExit(0)

if sys.argv[1] == "save":
    manifest = json.dumps(
        [{"Config": f"{digest}.json", "RepoTags": [image], "Layers": []}],
        separators=(",", ":"),
    ).encode()
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        for name, payload in (("manifest.json", manifest), (f"{digest}.json", config)):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    raise SystemExit(0)

raise SystemExit(2)
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    git = fake_bin / "git"
    git.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    git.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "RVC_SOURCE_COMMIT": RVC_COMMIT,
        "RVC_BASE_IMAGE": base_image,
        "RVC_SOURCE_HASH": source_hash,
        "RVC_WHEEL_HASH": wheel_hash,
        "RVC_ASSET_HASH": asset_hash,
        "RVC_PROJECTION_HASH": projection_hash,
        "RVC_FAIRSEQ_COMMIT": FAIRSEQ_COMMIT,
    }
    command = [
        "bash",
        str(ROOT / "installers/worker/build-bundle.sh"),
        "--version",
        "9.8.7",
        "--output-dir",
        str(tmp_path),
        "--include-rvc-runtime-image",
        image,
        "--rvc-runtime-assets",
        str(assets),
        "--rvc-runtime-asset-manifest",
        str(asset_manifest),
        "--rvc-runtime-build-manifest",
        str(build_manifest),
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    archive = tmp_path / "rvc-worker-9.8.7-linux-amd64.tar.gz"
    with tarfile.open(archive, "r:gz") as bundle:
        names = set(bundle.getnames())
        manifest = bundle.extractfile("rvc-worker-9.8.7-linux-amd64/manifest.env")
        assert manifest is not None
        manifest_text = manifest.read().decode()
        sbom = bundle.extractfile("rvc-worker-9.8.7-linux-amd64/supply-chain/sbom.cdx.json")
        assert sbom is not None
        sbom_document = json.loads(sbom.read())
    assert "rvc-worker-9.8.7-linux-amd64/images/rvc-runtime-image.tar.gz" in names
    assert "rvc-worker-9.8.7-linux-amd64/runtime/assets-manifest.json" in names
    assert "rvc-worker-9.8.7-linux-amd64/supply-chain/sbom.cdx.json" in names
    assert "rvc-worker-9.8.7-linux-amd64/supply-chain/third-party-licenses.json" in names
    assert "RVC_RUNTIME_INCLUDED=true" in manifest_text
    assert "RVC_NATIVE_RUNNER_AVAILABLE=true" in manifest_text
    assert "SBOM_STATUS=partial-release-gates-open" in manifest_text
    assert sbom_document["metadata"]["component"]["version"] == "9.8.7"
    assert f"RVC_ASSET_MANIFEST_SHA256={asset_hash}" in manifest_text
    assert f"RVC_PROJECTION_MANIFEST_SHA256={projection_hash}" in manifest_text
    assert "RVC_PROFILE_STAGE_SET_VERIFIED=false" in manifest_text
    assert "RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=false" in manifest_text

    reviewed_build_manifest = build_manifest.read_text(encoding="utf-8")
    build_manifest.write_text(
        reviewed_build_manifest.replace(
            "RVC_TORCH_VERSION=2.6.0+cu124",
            "RVC_TORCH_VERSION=2.5.1+cu124",
        ),
        encoding="utf-8",
    )
    mismatched_runtime = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert mismatched_runtime.returncode != 0
    assert "Torch/CUDA/cuDNN lock" in mismatched_runtime.stderr
    build_manifest.write_text(reviewed_build_manifest, encoding="utf-8")

    extracted = tmp_path / "installer-mode-mismatch"
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(extracted, filter="data")
    bundle_root = extracted / "rvc-worker-9.8.7-linux-amd64"
    config_root = tmp_path / "existing-worker-config"
    config_root.mkdir()
    (config_root / "worker.env").write_text(
        "\n".join(
            (
                "MANAGER_URL=https://manager.example",
                "WORKER_NAME=gpu-01",
                "RVC_RUNNER_MODE=profile",
                "RVC_NATIVE_UNVERIFIED_GPU_ACKNOWLEDGED=false",
                "",
            )
        ),
        encoding="utf-8",
    )
    mismatch = subprocess.run(
        [
            "bash",
            str(bundle_root / "install.sh"),
            "--runner-mode",
            "native",
            "--allow-unverified-gpu-runtime",
            "--install-root",
            str(tmp_path / "mode-mismatch-install"),
            "--config-root",
            str(config_root),
            "--data-root",
            str(tmp_path / "mode-mismatch-data"),
            "--systemd-dir",
            str(tmp_path / "mode-mismatch-systemd"),
            "--no-start",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**environment, "RVC_INSTALL_ALLOW_NON_ROOT": "1"},
    )
    assert mismatch.returncode != 0
    assert "differs from preserved worker.env" in mismatch.stderr

    (config_root / "worker.env").write_text(
        "\n".join(
            (
                "MANAGER_URL=https://manager.example",
                "WORKER_NAME=gpu-01",
                "RVC_RUNNER_MODE=native",
                "RVC_NATIVE_UNVERIFIED_GPU_ACKNOWLEDGED=false",
                "",
            )
        ),
        encoding="utf-8",
    )
    stale_ack = subprocess.run(
        [
            "bash",
            str(bundle_root / "install.sh"),
            "--allow-unverified-gpu-runtime",
            "--install-root",
            str(tmp_path / "stale-ack-install"),
            "--config-root",
            str(config_root),
            "--data-root",
            str(tmp_path / "stale-ack-data"),
            "--systemd-dir",
            str(tmp_path / "stale-ack-systemd"),
            "--no-start",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**environment, "RVC_INSTALL_ALLOW_NON_ROOT": "1"},
    )
    assert stale_ack.returncode != 0
    assert "preserved native worker.env lacks" in stale_ack.stderr


def test_complete_synthetic_qualification_projects_bundle_activation(
    tmp_path: Path,
) -> None:
    version = "5.6.7"
    image = f"rvc-orchestrator-worker:{version}"
    orchestrator_commit = hashlib.sha1(b"qualified-orchestrator").hexdigest()
    fairseq_commit = hashlib.sha1(b"qualified-fairseq").hexdigest()
    base_image = (
        "pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime@sha256:"
        + hashlib.sha256(b"qualified-amd64-base").hexdigest()
    )
    source_hash = hashlib.sha256(b"qualified-source-manifest").hexdigest()
    wheel_hash = hashlib.sha256(b"qualified-wheelhouse-manifest").hexdigest()
    projection_hash = hashlib.sha256(b"qualified-projection-manifest").hexdigest()
    assets, asset_manifest = _asset_input(tmp_path / "assets")
    asset_hash = _sha256(asset_manifest)
    labels = {
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.revision": orchestrator_commit,
        "org.rvc-orchestrator.runtime": "rvc",
        "org.rvc-orchestrator.rvc.commit": RVC_COMMIT,
        "org.rvc-orchestrator.rvc.python": "3.11",
        "org.rvc-orchestrator.rvc.torch": "2.6.0+cu124",
        "org.rvc-orchestrator.rvc.cuda": "12.4",
        "org.rvc-orchestrator.rvc.cudnn": "9",
        "org.rvc-orchestrator.rvc.base": base_image,
        "org.rvc-orchestrator.rvc.source.sha256": source_hash,
        "org.rvc-orchestrator.rvc.wheelhouse.sha256": wheel_hash,
        "org.rvc-orchestrator.rvc.assets.sha256": asset_hash,
        "org.rvc-orchestrator.rvc.projection.sha256": projection_hash,
        "org.rvc-orchestrator.rvc.fairseq.commit": fairseq_commit,
        "org.rvc-orchestrator.gpu-smoke-verified": "false",
        "org.rvc-orchestrator.profile-stage-set-verified": "false",
    }
    config = json.dumps(
        {
            "architecture": "amd64",
            "os": "linux",
            "config": {"Labels": labels, "User": "10001:10001"},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    image_digest = "sha256:" + hashlib.sha256(config).hexdigest()

    build_manifest = tmp_path / "runtime-build.env"
    build_manifest.write_text(
        "".join(
            f"{key}={value}\n"
            for key, value in {
                "RUNTIME_BUILD_FORMAT_VERSION": "1",
                "PRODUCT": "rvc-training-orchestrator",
                "COMPONENT": "worker-rvc-runtime",
                "IMAGE": image,
                "RELEASE_VERSION": version,
                "ORCHESTRATOR_SOURCE_COMMIT": orchestrator_commit,
                "BASE_IMAGE": base_image,
                "RVC_SOURCE_COMMIT": RVC_COMMIT,
                "RVC_SOURCE_MANIFEST_SHA256": source_hash,
                "RVC_WHEELHOUSE_MANIFEST_SHA256": wheel_hash,
                "RVC_ASSET_MANIFEST_SHA256": asset_hash,
                "RVC_PROJECTION_MANIFEST_SHA256": projection_hash,
                "RVC_FAIRSEQ_COMMIT": fairseq_commit,
                "RVC_TORCH_VERSION": "2.6.0+cu124",
                "RVC_CUDA_RUNTIME_VERSION": "12.4",
                "RVC_CUDNN_MAJOR": "9",
                "GPU_SMOKE_VERIFIED": "false",
                "PROFILE_STAGE_SET_VERIFIED": "false",
            }.items()
        ),
        encoding="utf-8",
    )

    qualification_module = _load_runtime_module(
        "runtime_qualification_for_bundle",
        RUNTIME / "qualification.py",
    )
    case_ids = sorted(qualification_module.REQUIRED_CASE_IDS)
    evidence = tmp_path / "qualified-runtime-evidence.tar.gz"
    report_hashes: dict[str, str] = {}
    with tarfile.open(evidence, "w:gz") as archive:
        for case_id in case_ids:
            report = json.dumps(
                {"case_id": case_id, "result": "passed", "fixture": True},
                sort_keys=True,
            ).encode()
            report_path = f"reports/{case_id}.json"
            report_hashes[report_path] = hashlib.sha256(report).hexdigest()
            member = tarfile.TarInfo(report_path)
            member.mode = 0o444
            member.size = len(report)
            archive.addfile(member, io.BytesIO(report))
    qualification = tmp_path / "runtime-qualification.json"
    _write_json(
        qualification,
        {
            "format_version": 1,
            "kind": "rvc-native-runtime-qualification",
            "runtime": {
                "image_digest": image_digest,
                "release_version": version,
                "orchestrator_commit": orchestrator_commit,
                "rvc_commit": RVC_COMMIT,
                "base_image": base_image,
                "source_manifest_sha256": source_hash,
                "wheelhouse_manifest_sha256": wheel_hash,
                "asset_manifest_sha256": asset_hash,
                "projection_manifest_sha256": projection_hash,
                "fairseq_commit": fairseq_commit,
                "torch": "2.6.0+cu124",
                "torchvision": "0.21.0+cu124",
                "torchaudio": "2.6.0+cu124",
                "cuda": "12.4",
                "cudnn": "9",
            },
            "cases": [
                {
                    "case_id": case_id,
                    "result": "passed",
                    "report_path": f"reports/{case_id}.json",
                    "report_sha256": report_hashes[f"reports/{case_id}.json"],
                }
                for case_id in case_ids
            ],
            "evidence_archive": {
                "file": evidence.name,
                "size": evidence.stat().st_size,
                "sha256": _sha256(evidence),
            },
            "review": {
                "reviewed_at": "2026-07-12T01:02:03Z",
                "reviewer": "synthetic-release-reviewer",
            },
        },
    )

    fake_bin = tmp_path / "qualified-bin"
    fake_bin.mkdir()
    state = tmp_path / "qualified-docker-state.json"
    _write_json(state, {"image": image, "labels": labels})
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import hashlib
import io
import json
import os
import sys
import tarfile

with open(os.environ["FAKE_DOCKER_STATE"], encoding="utf-8") as stream:
    state = json.load(stream)
image = state["image"]
labels = state["labels"]
config = json.dumps(
    {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Labels": labels, "User": "10001:10001"},
    },
    sort_keys=True,
    separators=(",", ":"),
).encode()
digest = hashlib.sha256(config).hexdigest()
args = sys.argv[1:]
if args[:2] == ["image", "inspect"]:
    template = args[3]
    if template == "{{.Id}}": print("sha256:" + digest)
    elif template == "{{.Os}}": print("linux")
    elif template == "{{.Architecture}}": print("amd64")
    elif template == '{{with index .Config "User"}}{{.}}{{end}}': print("10001:10001")
    elif template == "{{json .RepoTags}}": print(json.dumps([image]))
    elif template == "{{json .RepoDigests}}": print("[]")
    else:
        value = next((value for key, value in labels.items() if key in template), None)
        if value is None: raise SystemExit(2)
        print(value)
    raise SystemExit(0)
if args and args[0] == "save":
    manifest = json.dumps(
        [{"Config": digest + ".json", "RepoTags": [image], "Layers": []}],
        separators=(",", ":"),
    ).encode()
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        for name, payload in (("manifest.json", manifest), (digest + ".json", config)):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    raise SystemExit(0)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    git = fake_bin / "git"
    git.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'case " $* " in\n'
        f"  *\" rev-parse \"*) printf '%s\\n' '{orchestrator_commit}' ;;\n"
        '  *" status "*) : ;;\n'
        '  *" check-ignore "*) cat >/dev/null; exit 1 ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    git.chmod(0o755)

    output = tmp_path / "qualified-bundle"
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "installers/worker/build-bundle.sh"),
            "--version",
            version,
            "--self-contained",
            "--include-rvc-runtime-image",
            image,
            "--rvc-runtime-assets",
            str(assets),
            "--rvc-runtime-asset-manifest",
            str(asset_manifest),
            "--rvc-runtime-build-manifest",
            str(build_manifest),
            "--rvc-runtime-qualification",
            str(qualification),
            "--rvc-runtime-qualification-evidence",
            str(evidence),
            "--output-dir",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_DOCKER_STATE": str(state),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    bundle_path = output / f"rvc-worker-{version}-linux-amd64.tar.gz"
    with tarfile.open(bundle_path, "r:gz") as bundle:
        root = f"rvc-worker-{version}-linux-amd64"
        manifest_file = bundle.extractfile(f"{root}/manifest.env")
        activation_member = bundle.getmember(f"{root}/infra/worker/runtime/runtime-activation.json")
        activation_file = bundle.extractfile(activation_member)
        assert manifest_file is not None and activation_file is not None
        manifest = manifest_file.read().decode()
        activation = json.load(activation_file)
        names = set(bundle.getnames())
    assert "RVC_GPU_SMOKE_VERIFIED=true" in manifest
    assert "RVC_PROFILE_STAGE_SET_VERIFIED=true" in manifest
    assert "RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=true" in manifest
    assert activation["runtime_image_digest"] == image_digest
    assert activation["runtime_asset_manifest_sha256"] == asset_hash
    assert activation["supported_inference_f0_methods"] == [
        "pm",
        "harvest",
        "crepe",
        "rmvpe",
    ]
    assert activation_member.mode & 0o222 == 0
    assert f"{root}/runtime/qualification/qualification.json" in names
    assert f"{root}/runtime/qualification/{evidence.name}" in names

    extracted = tmp_path / "qualified-extracted"
    with tarfile.open(bundle_path, "r:gz") as bundle:
        bundle.extractall(extracted, filter="data")
    release = extracted / root
    installed_activation = release / "infra/worker/runtime/runtime-activation.json"
    installed_activation.chmod(0o444)
    installed_evidence = release / "runtime/qualification" / evidence.name
    installed_evidence.write_bytes(installed_evidence.read_bytes() + b"tamper")
    release_environment = tmp_path / "qualified-worker.env"
    release_environment.write_text(
        f"WORKER_IMAGE={image}\nRVC_IMAGE_PULL_POLICY=never\n",
        encoding="utf-8",
    )
    tampered = subprocess.run(
        [
            sys.executable,
            str(ROOT / "installers/common/image_bundle.py"),
            "verify-environment",
            "--root",
            str(release),
            "--component",
            "worker",
            "--version",
            version,
            "--source-commit",
            orchestrator_commit,
            "--environment",
            str(release_environment),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert tampered.returncode != 0
    assert "evidence archive digest or size differs" in tampered.stderr


def test_native_runtime_and_installer_keep_unverified_gpu_gate_closed() -> None:
    dockerfile = (ROOT / "apps/worker/Dockerfile.rvc").read_text(encoding="utf-8")
    entrypoint = ROOT / "infra/worker/runtime/runtime-entrypoint.sh"
    installer = (ROOT / "installers/worker/install.sh").read_text(encoding="utf-8")

    assert "RVC_RUNNER_MODE=native" in dockerfile
    assert "RVC_GPU_SMOKE_VERIFIED=false" in dockerfile
    assert "RVC_PROFILE_STAGE_SET_VERIFIED=false" in dockerfile
    assert "chown -R rvc-worker:rvc-worker /opt/rvc-webui/logs" not in dockerfile
    assert "--allow-unverified-gpu-runtime" in installer
    assert "RVC_NATIVE_RUNNER_AVAILABLE" in installer
    assert "RVC_RUNTIME_INCLUDED" in installer

    rejected = subprocess.run(
        ["sh", str(entrypoint)],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "RVC_RUNNER_MODE": "native",
            "RVC_GPU_SMOKE_VERIFIED": "false",
            "RVC_NATIVE_UNVERIFIED_GPU_ACKNOWLEDGED": "false",
        },
    )
    assert rejected.returncode != 0
    assert "explicit operator acknowledgement" in rejected.stderr


def test_worker_installer_rejects_native_without_runtime_bundle(tmp_path: Path) -> None:
    built = subprocess.run(
        [
            "bash",
            str(ROOT / "installers/worker/build-bundle.sh"),
            "--version",
            "1.2.3",
            "--output-dir",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    archive = tmp_path / "rvc-worker-1.2.3-linux-amd64.tar.gz"
    with tarfile.open(archive, "r:gz") as bundle:
        names = set(bundle.getnames())
        assert not any("__pycache__" in name.split("/") for name in names)
        assert not any(name.endswith((".pyc", ".pyo", "/.DS_Store")) for name in names)
        bundle.extractall(tmp_path / "extracted", filter="data")
    root = tmp_path / "extracted/rvc-worker-1.2.3-linux-amd64"
    token = tmp_path / "bootstrap-token"
    token.write_text("fixture-token", encoding="utf-8")

    installed = subprocess.run(
        [
            "bash",
            str(root / "install.sh"),
            "--manager-url",
            "https://manager.example",
            "--worker-name",
            "gpu-01",
            "--token-file",
            str(token),
            "--runner-mode",
            "native",
            "--allow-unverified-gpu-runtime",
            "--install-root",
            str(tmp_path / "install"),
            "--config-root",
            str(tmp_path / "config"),
            "--data-root",
            str(tmp_path / "data"),
            "--systemd-dir",
            str(tmp_path / "systemd"),
            "--no-start",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "RVC_INSTALL_ALLOW_NON_ROOT": "1"},
    )
    assert installed.returncode != 0
    assert (
        "[rvc-installer] error: native mode requires a Worker bundle with a verified offline "
        "RVC runtime"
    ) in installed.stderr.splitlines()
    for rejected_path in ("install", "config", "data", "systemd"):
        assert not (tmp_path / rejected_path).exists()


def test_worker_upgrade_refreshes_release_env_and_preserves_user_state(tmp_path: Path) -> None:
    bundle_output = tmp_path / "bundles"
    extracted_roots: dict[str, Path] = {}
    for version in ("1.0.0", "2.0.0"):
        built = subprocess.run(
            [
                "bash",
                str(ROOT / "installers/worker/build-bundle.sh"),
                "--version",
                version,
                "--output-dir",
                str(bundle_output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert built.returncode == 0, built.stdout + built.stderr
        archive = bundle_output / f"rvc-worker-{version}-linux-amd64.tar.gz"
        destination = tmp_path / f"extracted-{version}"
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(destination, filter="data")
        extracted_roots[version] = destination / f"rvc-worker-{version}-linux-amd64"

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        '#!/bin/sh\nset -eu\nif [ "${1:-}" = compose ]; then exit 0; fi\nexit 0\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    install_root = tmp_path / "install"
    config_root = tmp_path / "config"
    data_root = tmp_path / "data"
    systemd_root = tmp_path / "systemd"
    token_source = tmp_path / "bootstrap-token"
    token_source.write_text("persistent-worker-token", encoding="utf-8")
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "RVC_INSTALL_ALLOW_NON_ROOT": "1",
        "RVC_WORKER_MINIMUM_DISK_GB": "0",
    }

    common = [
        "--runner-mode",
        "fake",
        "--allow-fake-dev",
        "--install-root",
        str(install_root),
        "--config-root",
        str(config_root),
        "--data-root",
        str(data_root),
        "--systemd-dir",
        str(systemd_root),
        "--allow-unsupported-os",
        "--skip-daemon-check",
        "--skip-gpu-check",
        "--no-start",
    ]
    first = subprocess.run(
        [
            "bash",
            str(extracted_roots["1.0.0"] / "install.sh"),
            "--manager-url",
            "https://manager.example",
            "--worker-name",
            "gpu-01",
            "--token-file",
            str(token_source),
            *common,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    first_ledger = install_root / "releases/1.0.0/RELEASE_SHA256SUMS"
    assert first_ledger.is_file()
    assert stat.S_IMODE(first_ledger.stat().st_mode) == 0o444

    env_path = config_root / "worker.env"
    profile = config_root / "rvc-profile.yaml"
    token = config_root / "secrets/worker_token"
    assert stat.S_IMODE(data_root.stat().st_mode) == 0o700
    assert data_root.stat().st_uid == os.getuid()
    assert data_root.stat().st_gid == os.getgid()
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    assert profile.stat().st_uid == os.getuid()
    assert profile.stat().st_gid == os.getgid()
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert token.stat().st_uid == os.getuid()
    assert token.stat().st_gid == os.getgid()
    initial = env_path.read_text(encoding="utf-8")
    initial = initial.replace("RVC_GPU_SMOKE_VERIFIED=false", "RVC_GPU_SMOKE_VERIFIED=true")
    initial = initial.replace(
        "RVC_PROFILE_STAGE_SET_VERIFIED=false",
        "RVC_PROFILE_STAGE_SET_VERIFIED=true",
    )
    initial = initial.replace(
        "RVC_NATIVE_UNVERIFIED_GPU_ACKNOWLEDGED=false",
        "RVC_NATIVE_UNVERIFIED_GPU_ACKNOWLEDGED=true",
    )
    env_path.write_text(initial + "CUSTOM_SETTING=preserve-me\n", encoding="utf-8")
    profile.write_text("custom-profile: preserve-me\n", encoding="utf-8")
    token_before = token.read_bytes()
    data_marker = data_root / "jobs/preserve-me"
    data_marker.parent.mkdir(parents=True, exist_ok=True)
    data_marker.write_text("job-data", encoding="utf-8")

    second = subprocess.run(
        ["bash", str(extracted_roots["2.0.0"] / "install.sh"), *common],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert second.returncode == 0, second.stdout + second.stderr

    upgraded = env_path.read_text(encoding="utf-8")
    assert upgraded.count("ORCHESTRATOR_VERSION=") == 1
    assert upgraded.count("WORKER_IMAGE=") == 1
    assert "ORCHESTRATOR_VERSION=2.0.0" in upgraded
    assert "WORKER_IMAGE=rvc-orchestrator-worker:2.0.0" in upgraded
    assert "RVC_GPU_SMOKE_VERIFIED=false" in upgraded
    assert "RVC_PROFILE_STAGE_SET_VERIFIED=false" in upgraded
    assert "RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=false" in upgraded
    assert "RVC_RUNTIME_INCLUDED=false" in upgraded
    assert "RVC_NATIVE_RUNNER_AVAILABLE=false" in upgraded
    assert "RVC_RUNTIME_IMAGE=none" in upgraded
    assert "RVC_SOURCE_COMMIT=none" in upgraded
    assert "RVC_NATIVE_UNVERIFIED_GPU_ACKNOWLEDGED=true" in upgraded
    assert "CUSTOM_SETTING=preserve-me" in upgraded
    assert token.read_bytes() == token_before
    assert profile.read_text(encoding="utf-8") == "custom-profile: preserve-me\n"
    assert data_marker.read_text(encoding="utf-8") == "job-data"
    assert (install_root / "current").readlink() == Path("releases/2.0.0")
    second_ledger = install_root / "releases/2.0.0/RELEASE_SHA256SUMS"
    assert second_ledger.is_file()
    assert stat.S_IMODE(second_ledger.stat().st_mode) == 0o444
    unlisted_release_file = install_root / "releases/2.0.0/unlisted-after-install.txt"
    unlisted_release_file.write_text("tamper\n", encoding="utf-8")
    refused_start = subprocess.run(
        [str(install_root / "bin/worker-compose"), "up", "-d"],
        check=False,
        capture_output=True,
        text=True,
        env={
            **environment,
            "RVC_INSTALL_ROOT": str(install_root),
            "RVC_CONFIG_ROOT": str(config_root),
        },
    )
    assert refused_start.returncode != 0
    assert "checksum inventory differs" in refused_start.stderr
    unlisted_release_file.unlink()
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            "RVC_GPU_SMOKE_VERIFIED=false", "RVC_GPU_SMOKE_VERIFIED=true"
        ),
        encoding="utf-8",
    )
    refused_provenance = subprocess.run(
        [str(install_root / "bin/worker-compose"), "up", "-d"],
        check=False,
        capture_output=True,
        text=True,
        env={
            **environment,
            "RVC_INSTALL_ROOT": str(install_root),
            "RVC_CONFIG_ROOT": str(config_root),
        },
    )
    assert refused_provenance.returncode != 0
    assert "provenance differs for RVC_GPU_SMOKE_VERIFIED" in refused_provenance.stderr
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            "RVC_GPU_SMOKE_VERIFIED=true", "RVC_GPU_SMOKE_VERIFIED=false"
        ),
        encoding="utf-8",
    )

    release_manifest = install_root / "releases/2.0.0/manifest.env"
    release_lines = release_manifest.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("GIT_COMMIT=") for line in release_lines) == 1
    release_manifest.write_text(
        "\n".join(
            "GIT_COMMIT=forged-same-version-release" if line.startswith("GIT_COMMIT=") else line
            for line in release_lines
        )
        + "\n",
        encoding="utf-8",
    )
    same_version = subprocess.run(
        ["bash", str(extracted_roots["2.0.0"] / "install.sh"), *common],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert same_version.returncode != 0
    assert "release manifest differs" in same_version.stderr


def test_worker_installer_rejects_duplicate_manifest_and_symlink_env(tmp_path: Path) -> None:
    built = subprocess.run(
        [
            "bash",
            str(ROOT / "installers/worker/build-bundle.sh"),
            "--version",
            "3.0.0",
            "--output-dir",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    archive = tmp_path / "rvc-worker-3.0.0-linux-amd64.tar.gz"
    duplicate_root = tmp_path / "duplicate"
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(duplicate_root, filter="data")
    duplicate_bundle = duplicate_root / "rvc-worker-3.0.0-linux-amd64"
    with (duplicate_bundle / "manifest.env").open("a", encoding="utf-8") as stream:
        stream.write("WORKER_IMAGE=rvc-orchestrator-worker:attacker\n")
    _rewrite_bundle_checksums(duplicate_bundle)
    duplicate = subprocess.run(
        ["bash", str(duplicate_bundle / "install.sh"), "--no-start"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "RVC_INSTALL_ALLOW_NON_ROOT": "1"},
    )
    assert duplicate.returncode != 0
    assert "invalid or duplicate assignment" in duplicate.stderr

    invalid_root = tmp_path / "invalid-image"
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(invalid_root, filter="data")
    invalid_bundle = invalid_root / "rvc-worker-3.0.0-linux-amd64"
    manifest_path = invalid_bundle / "manifest.env"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "WORKER_IMAGE=rvc-orchestrator-worker:3.0.0",
            "WORKER_IMAGE=registry.example/attacker:latest",
        ),
        encoding="utf-8",
    )
    _rewrite_bundle_checksums(invalid_bundle)
    invalid = subprocess.run(
        ["bash", str(invalid_bundle / "install.sh"), "--no-start"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "RVC_INSTALL_ALLOW_NON_ROOT": "1"},
    )
    assert invalid.returncode != 0
    assert "invalid image reference" in invalid.stderr

    symlink_root = tmp_path / "symlink"
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(symlink_root, filter="data")
    symlink_bundle = symlink_root / "rvc-worker-3.0.0-linux-amd64"
    config_root = tmp_path / "symlink-config"
    config_root.mkdir()
    outside = tmp_path / "outside-worker.env"
    outside.write_text("RVC_RUNNER_MODE=fake\n", encoding="utf-8")
    (config_root / "worker.env").symlink_to(outside)
    unsafe = subprocess.run(
        [
            "bash",
            str(symlink_bundle / "install.sh"),
            "--config-root",
            str(config_root),
            "--no-start",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "RVC_INSTALL_ALLOW_NON_ROOT": "1"},
    )
    assert unsafe.returncode != 0
    assert "Worker environment is missing or unsafe" in unsafe.stderr
