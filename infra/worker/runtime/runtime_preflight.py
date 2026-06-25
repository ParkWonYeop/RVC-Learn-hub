#!/usr/bin/env python3
"""Verify the pinned RVC runtime before a Worker accepts real training."""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from verify_inputs import (
    CRITICAL_WHEELS,
    CUDA_WHEEL_FLAVOR,
    FAIRSEQ_REPOSITORY,
    PYTHON_VERSION,
    RVC_COMMIT,
    VerificationError,
    verify_assets,
)


class PreflightError(RuntimeError):
    """Raised when the installed runtime differs from the reviewed lock."""


def _module(name: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except Exception as exc:
        raise PreflightError(f"cannot import required runtime module: {name}") from exc


def _version(module: ModuleType, name: str, expected: str) -> str:
    actual = str(getattr(module, "__version__", ""))
    if actual != expected:
        raise PreflightError(f"{name} version differs from the reviewed runtime lock")
    return actual


def _regular_file(path: Path, label: str) -> None:
    if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
        raise PreflightError(f"required {label} is missing or unsafe")


def _tool(path: Path, label: str) -> str:
    resolved = path.resolve()
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise PreflightError(f"required {label} executable is missing")
    try:
        result = subprocess.run(
            [str(resolved), "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(f"cannot execute required {label}") from exc
    if result.returncode != 0 or not (result.stdout or result.stderr).strip():
        raise PreflightError(f"required {label} executable failed its version check")
    return (result.stdout or result.stderr).splitlines()[0][:200]


def _strict_object(path: Path) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PreflightError(f"duplicate key in installed manifest: {key}")
            result[key] = value
        return result

    if not path.is_file() or path.is_symlink():
        raise PreflightError("installed wheelhouse manifest is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreflightError("installed wheelhouse manifest is invalid") from exc
    if not isinstance(value, dict):
        raise PreflightError("installed wheelhouse manifest must be an object")
    return value


def _verify_installed_provenance(path: Path) -> str:
    manifest = _strict_object(path)
    if (
        manifest.get("schema_version") != 1
        or manifest.get("kind") != "rvc-wheelhouse"
        or manifest.get("python") != PYTHON_VERSION
        or manifest.get("cuda") != CUDA_WHEEL_FLAVOR
        or manifest.get("torch") != CRITICAL_WHEELS["torch"]
        or manifest.get("torchvision") != CRITICAL_WHEELS["torchvision"]
        or manifest.get("torchaudio") != CRITICAL_WHEELS["torchaudio"]
        or manifest.get("faiss_cpu") != CRITICAL_WHEELS["faiss-cpu"]
    ):
        raise PreflightError("installed wheelhouse provenance differs from the reviewed lock")
    fairseq = manifest.get("fairseq")
    if not isinstance(fairseq, dict) or (
        fairseq.get("version") != CRITICAL_WHEELS["fairseq"]
        or fairseq.get("repository") != FAIRSEQ_REPOSITORY
        or not isinstance(fairseq.get("commit"), str)
        or len(fairseq["commit"]) != 40
        or any(character not in "0123456789abcdef" for character in fairseq["commit"])
    ):
        raise PreflightError("installed fairseq provenance is incomplete")
    return fairseq["commit"]


def run_preflight(arguments: argparse.Namespace) -> dict[str, Any]:
    if sys.version_info[:2] != (3, 11):
        raise PreflightError("RVC runtime requires Python 3.11 exactly")
    repository = arguments.repository_root.resolve()
    if not repository.is_dir() or repository.is_symlink():
        raise PreflightError("RVC repository root is missing or unsafe")
    marker = repository / ".rvc-reviewed-commit"
    _regular_file(marker, "RVC commit marker")
    if marker.read_text(encoding="ascii").strip() != arguments.expected_commit:
        raise PreflightError("RVC source marker differs from the reviewed commit")
    for relative in (
        "requirements-py311.txt",
        "pyproject.toml",
        "infer/modules/train/train.py",
        "infer/modules/train/preprocess.py",
        "infer/modules/train/extract/extract_f0_print.py",
        "infer/modules/train/extract_feature_print.py",
    ):
        _regular_file(repository / relative, f"RVC source file {relative}")
    try:
        assets = verify_assets(arguments.asset_manifest, repository, allow_unlisted=True)
    except VerificationError as exc:
        raise PreflightError("installed RVC assets failed manifest verification") from exc
    fairseq_commit = _verify_installed_provenance(arguments.wheelhouse_manifest)

    torch = _module("torch")
    torchvision = _module("torchvision")
    torchaudio = _module("torchaudio")
    fairseq = _module("fairseq")
    faiss = _module("faiss")
    torch_version = _version(torch, "torch", CRITICAL_WHEELS["torch"])
    torchvision_version = _version(torchvision, "torchvision", CRITICAL_WHEELS["torchvision"])
    torchaudio_version = _version(torchaudio, "torchaudio", CRITICAL_WHEELS["torchaudio"])
    fairseq_version = _version(fairseq, "fairseq", CRITICAL_WHEELS["fairseq"])
    faiss_version = _version(faiss, "faiss-cpu", CRITICAL_WHEELS["faiss-cpu"])
    torch_cuda_version = str(getattr(getattr(torch, "version", None), "cuda", ""))
    if torch_cuda_version != "12.4":
        raise PreflightError("Torch CUDA runtime differs from the fixed cu124 combination")
    cudnn_value = getattr(getattr(getattr(torch, "backends", None), "cudnn", None), "version", None)
    cudnn_version = cudnn_value() if callable(cudnn_value) else None
    if not isinstance(cudnn_version, int) or cudnn_version // 10000 != 9:
        raise PreflightError("Torch cuDNN runtime is not the fixed cuDNN 9 combination")
    cuda_api = getattr(torch, "cuda", None)
    if cuda_api is None or not callable(getattr(cuda_api, "is_available", None)):
        raise PreflightError("Torch CUDA API is unavailable")
    gpu_available = bool(cuda_api.is_available())
    if not gpu_available and not arguments.allow_no_gpu:
        raise PreflightError("no CUDA GPU is available; real RVC training remains disabled")
    gpu_count = int(cuda_api.device_count()) if gpu_available else 0
    if gpu_available and gpu_count <= 0:
        raise PreflightError("Torch reports CUDA available without a visible device")

    ffmpeg = _tool(arguments.ffmpeg, "ffmpeg")
    ffprobe = _tool(arguments.ffprobe, "ffprobe")
    return {
        "status": "ready" if gpu_available else "cpu-verification-only",
        "rvc_commit": arguments.expected_commit,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "torch": torch_version,
        "torchvision": torchvision_version,
        "torchaudio": torchaudio_version,
        "torch_cuda": torch_cuda_version,
        "cudnn": cudnn_version,
        "fairseq": fairseq_version,
        "fairseq_commit": fairseq_commit,
        "faiss_cpu": faiss_version,
        "gpu_available": gpu_available,
        "gpu_count": gpu_count,
        "asset_count": assets["asset_count"],
        "asset_manifest_sha256": assets["manifest_sha256"],
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-no-gpu", action="store_true")
    parser.add_argument("--repository-root", type=Path, default=Path("/opt/rvc-webui"))
    parser.add_argument(
        "--asset-manifest",
        type=Path,
        default=Path("/opt/rvc-webui/assets-manifest.json"),
    )
    parser.add_argument(
        "--wheelhouse-manifest",
        type=Path,
        default=Path("/opt/rvc-runtime/manifests/wheelhouse-manifest.json"),
    )
    parser.add_argument("--expected-commit", default=RVC_COMMIT, choices=(RVC_COMMIT,))
    parser.add_argument("--ffmpeg", type=Path, default=Path("/usr/local/bin/ffmpeg"))
    parser.add_argument("--ffprobe", type=Path, default=Path("/usr/local/bin/ffprobe"))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = run_preflight(arguments)
    except (PreflightError, OSError, UnicodeError) as exc:
        print(f"RVC runtime preflight failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
