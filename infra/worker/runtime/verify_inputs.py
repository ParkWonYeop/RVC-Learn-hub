#!/usr/bin/env python3
"""Fail-closed verification for offline RVC runtime build inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any

RVC_REPOSITORY = "https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI"
RVC_COMMIT = "7ef19867780cf703841ebafb565a4e47d1ea86ff"
FAIRSEQ_REPOSITORY = "https://github.com/One-sixth/fairseq"
PYTHON_VERSION = "3.11"
CUDA_WHEEL_FLAVOR = "cu124"
CRITICAL_WHEELS = {
    "torch": "2.6.0+cu124",
    "torchvision": "0.21.0+cu124",
    "torchaudio": "2.6.0+cu124",
    "fairseq": "0.12.2",
    "faiss-cpu": "1.7.4",
}
REQUIRED_TRAINING_PROJECTS = {
    "ffmpeg-python",
    "joblib",
    "librosa",
    "llvmlite",
    "matplotlib",
    "numba",
    "numpy",
    "pillow",
    "praat-parselmouth",
    "pydub",
    "pyworld",
    "resampy",
    "scikit-learn",
    "scipy",
    "soundfile",
    "sympy",
    "tensorboard",
    "tensorboardx",
    "torchcrepe",
    "tqdm",
}
REQUIRED_HTTPX_PROJECTS = {
    "anyio",
    "certifi",
    "h11",
    "httpcore",
    "httpx",
    "idna",
    "sniffio",
    "typing-extensions",
}
REQUIRED_RUNTIME_PROJECTS = (
    set(CRITICAL_WHEELS)
    | REQUIRED_TRAINING_PROJECTS
    | REQUIRED_HTTPX_PROJECTS
    | {
        "hatchling",
        "pydantic",
        "pyyaml",
        "setuptools",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SPDX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+()_-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}$")
_REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9.!+_-]+)(.*)$")
_HASH_OPTION = re.compile(r"(?:^|\s)--hash=sha256:([0-9a-f]{64})(?=\s|$)")
_UNREVIEWED_LICENSES = {"unknown", "noassertion", "tbd", "unreviewed"}


def _required_assets() -> set[str]:
    paths = {
        "assets/hubert/hubert_base.pt",
        "assets/rmvpe/rmvpe.pt",
        "runtime/crepe/full.pth",
        "logs/mute/0_gt_wavs/mute40k.wav",
        "logs/mute/0_gt_wavs/mute48k.wav",
        "logs/mute/3_feature256/mute.npy",
        "logs/mute/3_feature768/mute.npy",
        "logs/mute/2a_f0/mute.wav.npy",
        "logs/mute/2b-f0nsf/mute.wav.npy",
        "runtime/bin/ffmpeg",
        "runtime/bin/ffprobe",
    }
    for root in ("assets/pretrained", "assets/pretrained_v2"):
        for rate in ("40k", "48k"):
            for prefix in ("G", "D", "f0G", "f0D"):
                paths.add(f"{root}/{prefix}{rate}.pth")
    return paths


REQUIRED_ASSETS = _required_assets()
PROJECTION_DIRECTORIES = (
    "infer",
    "configs",
    "assets/pretrained",
    "assets/pretrained_v2",
    "assets/hubert",
    "assets/rmvpe",
    "logs/mute",
    "runtime/crepe",
)
PROJECTION_SUFFIXES = {
    ".json",
    ".npy",
    ".npz",
    ".pth",
    ".pt",
    ".py",
    ".txt",
    ".wav",
    ".yaml",
    ".yml",
}


class VerificationError(RuntimeError):
    """Raised when an offline runtime input is incomplete or unsafe."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VerificationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise VerificationError(f"manifest is missing or unsafe: {path}")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"cannot read strict JSON manifest: {path.name}") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"manifest must be a JSON object: {path.name}")
    return value


def _require_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VerificationError(f"{label} must be an object")
    keys = set(value)
    if keys != expected:
        missing = ",".join(sorted(expected - keys)) or "none"
        extra = ",".join(sorted(keys - expected)) or "none"
        raise VerificationError(f"{label} fields differ (missing={missing}; extra={extra})")
    return value


def _safe_relative(value: object, label: str, *, basename_only: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise VerificationError(f"{label} is not a safe relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise VerificationError(f"{label} is not a safe relative path")
    if basename_only and len(path.parts) != 1:
        raise VerificationError(f"{label} must be a basename")
    return value


def _validate_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise VerificationError(f"{label} must be a lowercase SHA-256")
    return value


def _validate_size(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise VerificationError(f"{label} must be a positive integer")
    return value


def _validate_url(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("https://")
        or any(character.isspace() for character in value)
    ):
        raise VerificationError(f"{label} must be an HTTPS provenance URL")
    return value


def _validate_license(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SPDX.fullmatch(value):
        raise VerificationError(f"{label} must be a reviewed SPDX or LicenseRef identifier")
    if value.lower() in _UNREVIEWED_LICENSES:
        raise VerificationError(f"{label} has not been reviewed")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_recorded_file(path: Path, record: dict[str, Any], label: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise VerificationError(f"{label} is missing or unsafe: {path}")
    expected_size = _validate_size(record.get("size"), f"{label}.size")
    expected_hash = _validate_hash(record.get("sha256"), f"{label}.sha256")
    if path.stat().st_size != expected_size:
        raise VerificationError(f"{label} byte size does not match its manifest")
    if _sha256(path) != expected_hash:
        raise VerificationError(f"{label} SHA-256 does not match its manifest")


def _normalized_project(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _supported_httpx_version(value: str) -> bool:
    """Match the Worker's declared httpx>=0.27,<1 runtime dependency."""
    match = re.fullmatch(r"0\.(\d+)\.(\d+)(?:\.post\d+)?", value)
    return match is not None and int(match.group(1)) >= 27


def verify_source(manifest_path: Path, archive_path: Path | None = None) -> dict[str, Any]:
    manifest = _require_keys(
        _load_json(manifest_path),
        {"schema_version", "kind", "repository", "commit", "archive", "license"},
        "source manifest",
    )
    if manifest["schema_version"] != 1 or manifest["kind"] != "rvc-source":
        raise VerificationError("unsupported source manifest schema or kind")
    if manifest["repository"] != RVC_REPOSITORY or manifest["commit"] != RVC_COMMIT:
        raise VerificationError("source manifest does not identify the reviewed RVC commit")
    archive = _require_keys(
        manifest["archive"],
        {"file", "root", "sha256", "size", "unpacked_size", "source"},
        "source archive",
    )
    archive_name = _safe_relative(archive["file"], "source archive.file", basename_only=True)
    root = _safe_relative(archive["root"], "source archive.root", basename_only=True)
    _validate_url(archive["source"], "source archive.source")
    if RVC_COMMIT not in archive["source"]:
        raise VerificationError("source archive URL does not contain the reviewed commit")
    license_record = _require_keys(manifest["license"], {"spdx", "source"}, "source license")
    if _validate_license(license_record["spdx"], "source license.spdx") != "MIT":
        raise VerificationError("reviewed RVC source must retain its MIT license declaration")
    license_url = _validate_url(license_record["source"], "source license.source")
    if RVC_COMMIT not in license_url:
        raise VerificationError("source license URL is not pinned to the reviewed commit")

    selected_archive = archive_path or manifest_path.parent / archive_name
    if selected_archive.name != archive_name:
        raise VerificationError("source archive filename differs from its manifest")
    _verify_recorded_file(selected_archive, archive, "source archive")
    expected_unpacked = _validate_size(archive["unpacked_size"], "source archive.unpacked_size")
    required = {
        f"{root}/requirements-py311.txt",
        f"{root}/pyproject.toml",
        f"{root}/LICENSE",
        f"{root}/infer/modules/train/train.py",
        f"{root}/infer/modules/train/preprocess.py",
        f"{root}/infer/modules/train/extract/extract_f0_print.py",
        f"{root}/infer/modules/train/extract_feature_print.py",
    }
    discovered: set[str] = set()
    contents: dict[str, str] = {}
    unpacked_size = 0
    try:
        with tarfile.open(selected_archive, mode="r:*") as bundle:
            for member in bundle.getmembers():
                name = _safe_relative(member.name.rstrip("/"), "source archive member")
                if name != root and not name.startswith(f"{root}/"):
                    raise VerificationError("source archive member escapes its declared root")
                if not (member.isdir() or member.isfile()):
                    raise VerificationError("source archive contains a link or special file")
                if member.isfile():
                    unpacked_size += member.size
                    discovered.add(name)
                    if name in required:
                        if member.size > 1024 * 1024:
                            raise VerificationError(
                                "source dependency metadata is unexpectedly large"
                            )
                        stream = bundle.extractfile(member)
                        if stream is None:
                            raise VerificationError("cannot read source dependency metadata")
                        contents[name] = stream.read().decode("utf-8")
    except (tarfile.TarError, OSError, UnicodeError) as exc:
        raise VerificationError("source archive is not a safe readable tar archive") from exc
    if unpacked_size != expected_unpacked:
        raise VerificationError("source archive unpacked byte size differs from its manifest")
    if not required.issubset(discovered):
        raise VerificationError("source archive lacks reviewed dependency or training entrypoints")

    requirements = contents[f"{root}/requirements-py311.txt"]
    pyproject = contents[f"{root}/pyproject.toml"]
    if "fairseq @ git+https://github.com/One-sixth/fairseq.git" not in requirements:
        raise VerificationError(
            "source Python 3.11 requirements no longer match the reviewed fairseq form"
        )
    for unpinned in ("numba", "numpy", "scipy", "llvmlite", "faiss-cpu"):
        if not re.search(rf"(?m)^{re.escape(unpinned)}\s*$", requirements):
            raise VerificationError(
                f"reviewed Python 3.11 requirement difference is missing: {unpinned}"
            )
    for marker in ('torch = "2.4.0"', 'torchaudio = "2.4.0"', 'torchvision = "0.19.0"'):
        if marker not in pyproject:
            raise VerificationError(f"reviewed pyproject marker is missing: {marker}")
    return {"commit": RVC_COMMIT, "root": root, "archive_sha256": archive["sha256"]}


def _logical_requirements(content: str) -> list[str]:
    logical: list[str] = []
    current = ""
    for raw in content.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1].strip() + " "
            continue
        logical.append((current + stripped).strip())
        current = ""
    if current:
        raise VerificationError("requirements lock ends with an incomplete continuation")
    return logical


def _parse_requirements_lock(path: Path) -> dict[str, tuple[str, set[str]]]:
    try:
        lines = _logical_requirements(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise VerificationError("cannot read requirements lock") from exc
    parsed: dict[str, tuple[str, set[str]]] = {}
    for line in lines:
        if any(token in line for token in ("http://", "https://", " @ ", ";")):
            raise VerificationError("requirements lock contains a URL, direct reference, or marker")
        match = _REQUIREMENT.fullmatch(line)
        if match is None:
            raise VerificationError("every requirements lock entry must use name==version")
        project = _normalized_project(match.group(1))
        version = match.group(2)
        remainder = match.group(3)
        hashes = set(_HASH_OPTION.findall(remainder))
        if not hashes or _HASH_OPTION.sub("", remainder).strip():
            raise VerificationError(
                "every pinned requirement must contain only SHA-256 hash options"
            )
        if project in parsed:
            raise VerificationError(f"duplicate locked requirement: {project}")
        parsed[project] = (version, hashes)
    if not parsed:
        raise VerificationError("requirements lock is empty")
    return parsed


def _verify_wheel(path: Path, project: str, version: str) -> None:
    try:
        with zipfile.ZipFile(path) as wheel:
            metadata_files: list[str] = []
            wheel_files: list[str] = []
            for info in wheel.infolist():
                _safe_relative(info.filename.rstrip("/"), "wheel member")
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise VerificationError("wheel contains a symbolic link")
                if info.filename.endswith(".dist-info/METADATA"):
                    metadata_files.append(info.filename)
                if info.filename.endswith(".dist-info/WHEEL"):
                    wheel_files.append(info.filename)
            if len(metadata_files) != 1 or len(wheel_files) != 1:
                raise VerificationError("wheel must contain one METADATA and one WHEEL file")
            metadata = Parser().parsestr(wheel.read(metadata_files[0]).decode("utf-8"))
            if _normalized_project(metadata.get("Name", "")) != project:
                raise VerificationError("wheel METADATA project differs from its manifest")
            if metadata.get("Version") != version:
                raise VerificationError("wheel METADATA version differs from its manifest")
            wheel_metadata = wheel.read(wheel_files[0]).decode("utf-8")
            tags = [
                line.split(":", 1)[1].strip()
                for line in wheel_metadata.splitlines()
                if line.startswith("Tag:")
            ]
            if not tags:
                raise VerificationError("wheel has no compatibility tag")
            compiled = {"torch", "torchvision", "torchaudio", "faiss-cpu"}
            compatible = any("cp311" in tag and "x86_64" in tag for tag in tags)
            if project not in compiled:
                compatible = compatible or "py3-none-any" in tags
            if not compatible:
                raise VerificationError("wheel is not compatible with Python 3.11 linux_x86_64")
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise VerificationError(f"invalid wheel archive: {path.name}") from exc


def verify_wheelhouse(manifest_path: Path, wheelhouse_root: Path | None = None) -> dict[str, Any]:
    root = wheelhouse_root or manifest_path.parent
    if not root.is_dir() or root.is_symlink() or manifest_path.parent.resolve() != root.resolve():
        raise VerificationError("wheelhouse root or manifest location is unsafe")
    manifest = _require_keys(
        _load_json(manifest_path),
        {
            "schema_version",
            "kind",
            "python",
            "platform",
            "cuda",
            "torch",
            "torchvision",
            "torchaudio",
            "fairseq",
            "faiss_cpu",
            "requirements",
            "wheels",
        },
        "wheelhouse manifest",
    )
    if manifest["schema_version"] != 1 or manifest["kind"] != "rvc-wheelhouse":
        raise VerificationError("unsupported wheelhouse manifest schema or kind")
    if manifest["python"] != PYTHON_VERSION or manifest["platform"] != "linux_x86_64":
        raise VerificationError("wheelhouse must target Python 3.11 linux_x86_64")
    if manifest["cuda"] != CUDA_WHEEL_FLAVOR:
        raise VerificationError("wheelhouse must use the fixed PyTorch cu124 flavor")
    for field, project in (
        ("torch", "torch"),
        ("torchvision", "torchvision"),
        ("torchaudio", "torchaudio"),
        ("faiss_cpu", "faiss-cpu"),
    ):
        if manifest[field] != CRITICAL_WHEELS[project]:
            raise VerificationError(f"wheelhouse {field} differs from the reviewed runtime lock")
    fairseq = _require_keys(
        manifest["fairseq"], {"version", "repository", "commit"}, "fairseq provenance"
    )
    if fairseq["version"] != CRITICAL_WHEELS["fairseq"]:
        raise VerificationError("fairseq version differs from the reviewed runtime lock")
    if (
        fairseq["repository"] != FAIRSEQ_REPOSITORY
        or not isinstance(fairseq["commit"], str)
        or not _COMMIT.fullmatch(fairseq["commit"])
    ):
        raise VerificationError("fairseq must identify an exact reviewed One-sixth source commit")

    requirements = _require_keys(
        manifest["requirements"], {"file", "sha256", "size"}, "requirements lock"
    )
    requirements_name = _safe_relative(
        requirements["file"], "requirements lock.file", basename_only=True
    )
    if requirements_name != "requirements.lock":
        raise VerificationError("wheelhouse requirements file must be named requirements.lock")
    requirements_path = root / requirements_name
    _verify_recorded_file(requirements_path, requirements, "requirements lock")
    locked = _parse_requirements_lock(requirements_path)

    raw_wheels = manifest["wheels"]
    if not isinstance(raw_wheels, list) or not raw_wheels:
        raise VerificationError("wheelhouse manifest must list at least one wheel")
    records: dict[str, dict[str, Any]] = {}
    filenames: set[str] = set()
    for index, raw in enumerate(raw_wheels):
        record = _require_keys(
            raw,
            {"file", "project", "version", "sha256", "size", "license", "source"},
            f"wheel[{index}]",
        )
        filename = _safe_relative(record["file"], f"wheel[{index}].file", basename_only=True)
        if not filename.endswith(".whl") or filename in filenames:
            raise VerificationError("wheel filenames must be unique .whl basenames")
        project_value = record["project"]
        version_value = record["version"]
        if not isinstance(project_value, str) or not isinstance(version_value, str):
            raise VerificationError("wheel project and version must be strings")
        project = _normalized_project(project_value)
        if project_value != project or not _VERSION.fullmatch(version_value) or project in records:
            raise VerificationError("wheel project/version is not canonical or is duplicated")
        wheel_name_parts = filename.removesuffix(".whl").split("-")
        if (
            len(wheel_name_parts) < 5
            or _normalized_project(wheel_name_parts[0]) != project
            or wheel_name_parts[1] != version_value
        ):
            raise VerificationError("wheel filename does not match its project and version")
        _validate_license(record["license"], f"wheel[{index}].license")
        source = _validate_url(record["source"], f"wheel[{index}].source")
        if project == "fairseq" and fairseq["commit"] not in source:
            raise VerificationError("fairseq wheel source URL is not pinned to its reviewed commit")
        wheel_path = root / filename
        _verify_recorded_file(wheel_path, record, f"wheel {filename}")
        _verify_wheel(wheel_path, project, version_value)
        records[project] = record
        filenames.add(filename)

    if set(records) != set(locked):
        raise VerificationError("wheelhouse files and requirements lock projects differ")
    for project, record in records.items():
        locked_version, hashes = locked[project]
        if locked_version != record["version"] or record["sha256"] not in hashes:
            raise VerificationError(f"locked version/hash differs for wheel project: {project}")
    for project, version in CRITICAL_WHEELS.items():
        if project not in records or records[project]["version"] != version:
            raise VerificationError(f"wheelhouse is missing reviewed critical wheel: {project}")
    missing_runtime_projects = REQUIRED_RUNTIME_PROJECTS - set(records)
    if missing_runtime_projects:
        raise VerificationError(
            f"wheelhouse is missing required runtime/build project: "
            f"{sorted(missing_runtime_projects)[0]}"
        )
    if not _supported_httpx_version(records["httpx"]["version"]):
        raise VerificationError("httpx wheel must satisfy the Worker runtime range >=0.27,<1")

    allowed_files = {manifest_path.name, requirements_name, *filenames}
    discovered_files: set[str] = set()
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file():
            raise VerificationError("wheelhouse may contain only listed regular files")
        discovered_files.add(path.name)
    if discovered_files != allowed_files:
        raise VerificationError("wheelhouse contains an unlisted or missing file")
    return {
        "wheel_count": len(records),
        "manifest_sha256": _sha256(manifest_path),
        "fairseq_commit": fairseq["commit"],
    }


def verify_assets(
    manifest_path: Path,
    assets_root: Path | None = None,
    *,
    allow_unlisted: bool = False,
) -> dict[str, Any]:
    root = assets_root or manifest_path.parent
    if not root.is_dir() or root.is_symlink() or manifest_path.parent.resolve() != root.resolve():
        raise VerificationError("asset root or manifest location is unsafe")
    manifest = _require_keys(
        _load_json(manifest_path),
        {"schema_version", "kind", "rvc_commit", "assets"},
        "asset manifest",
    )
    if manifest["schema_version"] != 1 or manifest["kind"] != "rvc-assets":
        raise VerificationError("unsupported asset manifest schema or kind")
    if manifest["rvc_commit"] != RVC_COMMIT:
        raise VerificationError("asset manifest does not target the reviewed RVC commit")
    raw_assets = manifest["assets"]
    if not isinstance(raw_assets, list) or not raw_assets:
        raise VerificationError("asset manifest must contain records")
    records: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_assets):
        record = _require_keys(
            raw,
            {"path", "sha256", "size", "license", "source", "executable"},
            f"asset[{index}]",
        )
        relative = _safe_relative(record["path"], f"asset[{index}].path")
        if relative in records:
            raise VerificationError(f"duplicate asset path: {relative}")
        _validate_license(record["license"], f"asset[{index}].license")
        _validate_url(record["source"], f"asset[{index}].source")
        if not isinstance(record["executable"], bool):
            raise VerificationError(f"asset[{index}].executable must be boolean")
        if relative in {"runtime/bin/ffmpeg", "runtime/bin/ffprobe"}:
            if record["executable"] is not True:
                raise VerificationError("ffmpeg and ffprobe assets must be executable")
        elif relative in REQUIRED_ASSETS and record["executable"] is not False:
            raise VerificationError("model and mute assets must not be executable")
        _verify_recorded_file(root / relative, record, f"asset {relative}")
        mode = (root / relative).stat().st_mode
        has_execute_bit = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        if has_execute_bit != record["executable"]:
            raise VerificationError(f"asset executable mode differs from manifest: {relative}")
        records[relative] = record
    missing = REQUIRED_ASSETS - set(records)
    if missing:
        raise VerificationError(f"required runtime asset is missing: {sorted(missing)[0]}")

    discovered: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise VerificationError("asset root contains a symbolic link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise VerificationError("asset root contains a special file")
        relative = path.relative_to(root).as_posix()
        if path.resolve() == manifest_path.resolve():
            continue
        discovered.add(relative)
    if not allow_unlisted and discovered != set(records):
        raise VerificationError("asset root contains an unlisted or missing file")
    return {
        "asset_count": len(records),
        "manifest_sha256": _sha256(manifest_path),
        "files": [(path, records[path]["executable"]) for path in sorted(records)],
    }


def generate_projection_manifest(
    root: Path,
    source_manifest_path: Path,
    asset_manifest_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Bind every private-projection input to the verified source/archive manifests."""

    if not root.is_dir() or root.is_symlink():
        raise VerificationError("projection input root is missing or unsafe")
    source_manifest = _load_json(source_manifest_path)
    asset_manifest = _load_json(asset_manifest_path)
    if (
        source_manifest.get("schema_version") != 1
        or source_manifest.get("kind") != "rvc-source"
        or source_manifest.get("repository") != RVC_REPOSITORY
        or source_manifest.get("commit") != RVC_COMMIT
    ):
        raise VerificationError("projection source manifest is not the reviewed source")
    if (
        asset_manifest.get("schema_version") != 1
        or asset_manifest.get("kind") != "rvc-assets"
        or asset_manifest.get("rvc_commit") != RVC_COMMIT
    ):
        raise VerificationError("projection asset manifest is not the reviewed asset set")
    if output_path.exists() or output_path.is_symlink():
        raise VerificationError("projection manifest output already exists")

    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for relative_root in PROJECTION_DIRECTORIES:
        directory = root / relative_root
        if not directory.is_dir() or directory.is_symlink():
            raise VerificationError(f"projection directory is missing or unsafe: {relative_root}")
        for current_root, directories, filenames in os.walk(directory, followlinks=False):
            current = Path(current_root)
            for name in directories:
                child = current / name
                if child.is_symlink() or not child.is_dir():
                    raise VerificationError("projection source contains a linked directory")
            for name in filenames:
                path = current / name
                relative = path.relative_to(root).as_posix()
                file_stat = path.stat(follow_symlinks=False)
                if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
                    raise VerificationError("projection source contains a link or special file")
                if path.suffix.lower() not in PROJECTION_SUFFIXES:
                    continue
                if relative in seen:
                    raise VerificationError("projection source contains a duplicate path")
                seen.add(relative)
                records.append(
                    {
                        "path": relative,
                        "size": file_stat.st_size,
                        "sha256": _sha256(path),
                        "mode": stat.S_IMODE(file_stat.st_mode),
                    }
                )
    records.sort(key=lambda record: str(record["path"]))
    if not records:
        raise VerificationError("projection manifest would contain no files")
    document: dict[str, Any] = {
        "schema_version": 1,
        "kind": "rvc-projection-inputs",
        "rvc_commit": RVC_COMMIT,
        "source_manifest_sha256": _sha256(source_manifest_path),
        "asset_manifest_sha256": _sha256(asset_manifest_path),
        "files": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o644)
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"file_count": len(records), "manifest_sha256": _sha256(output_path)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    source = subparsers.add_parser("source")
    source.add_argument("--manifest", type=Path, required=True)
    source.add_argument("--archive", type=Path)
    source.add_argument("--emit-root", action="store_true")
    wheels = subparsers.add_parser("wheelhouse")
    wheels.add_argument("--manifest", type=Path, required=True)
    wheels.add_argument("--root", type=Path)
    assets = subparsers.add_parser("assets")
    assets.add_argument("--manifest", type=Path, required=True)
    assets.add_argument("--root", type=Path)
    assets.add_argument("--emit-files", action="store_true")
    projection = subparsers.add_parser("projection")
    projection.add_argument("--root", type=Path, required=True)
    projection.add_argument("--source-manifest", type=Path, required=True)
    projection.add_argument("--asset-manifest", type=Path, required=True)
    projection.add_argument("--output", type=Path, required=True)
    all_inputs = subparsers.add_parser("all")
    all_inputs.add_argument("--source-manifest", type=Path, required=True)
    all_inputs.add_argument("--source-archive", type=Path, required=True)
    all_inputs.add_argument("--wheelhouse-manifest", type=Path, required=True)
    all_inputs.add_argument("--wheelhouse-root", type=Path, required=True)
    all_inputs.add_argument("--asset-manifest", type=Path, required=True)
    all_inputs.add_argument("--asset-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "source":
            result = verify_source(arguments.manifest, arguments.archive)
            if arguments.emit_root:
                print(result["root"])
            else:
                print(json.dumps(result, sort_keys=True))
        elif arguments.command == "wheelhouse":
            result = verify_wheelhouse(arguments.manifest, arguments.root)
            print(json.dumps(result, sort_keys=True))
        elif arguments.command == "assets":
            result = verify_assets(arguments.manifest, arguments.root)
            if arguments.emit_files:
                for path, executable in result["files"]:
                    print(f"{path}\t{1 if executable else 0}")
            else:
                public = {key: value for key, value in result.items() if key != "files"}
                print(json.dumps(public, sort_keys=True))
        elif arguments.command == "projection":
            result = generate_projection_manifest(
                arguments.root,
                arguments.source_manifest,
                arguments.asset_manifest,
                arguments.output,
            )
            print(json.dumps(result, sort_keys=True))
        else:
            result = {
                "source": verify_source(arguments.source_manifest, arguments.source_archive),
                "wheelhouse": verify_wheelhouse(
                    arguments.wheelhouse_manifest, arguments.wheelhouse_root
                ),
                "assets": verify_assets(arguments.asset_manifest, arguments.asset_root),
            }
            result["assets"].pop("files", None)
            print(json.dumps(result, sort_keys=True))
    except VerificationError as exc:
        print(f"runtime input verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
