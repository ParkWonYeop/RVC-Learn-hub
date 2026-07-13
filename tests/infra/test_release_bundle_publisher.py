from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUBLISHER = ROOT / "installers/common/publish_release_bundle.py"
VERSION = "9.8.7"
COMMIT = hashlib.sha1(b"publisher-source").hexdigest()
RUNTIME_ID = "sha256:" + hashlib.sha256(b"publisher-runtime").hexdigest()
ARCHIVE_NAME = f"rvc-worker-{VERSION}-linux-amd64.tar.gz"


def _load_module(name: str, path: Path) -> types.ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


PUBLISH = _load_module("release_bundle_publisher", PUBLISHER)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_python(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fixture(
    tmp_path: Path,
    *,
    activation_mode: int = 0o444,
    runtime_image_id: str = RUNTIME_ID,
) -> dict[str, Path]:
    private = tmp_path / "private"
    output = tmp_path / "output"
    source = tmp_path / "source" / ARCHIVE_NAME.removesuffix(".tar.gz")
    activation = source / "infra/worker/runtime/runtime-activation.json"
    activation.parent.mkdir(parents=True)
    output.mkdir()
    private.mkdir()
    (source / "manifest.env").write_text(
        "\n".join(
            (
                "BUNDLE_FORMAT_VERSION=2",
                "PRODUCT=rvc-training-orchestrator",
                "COMPONENT=worker",
                f"VERSION={VERSION}",
                "PLATFORM=linux-amd64",
                f"GIT_COMMIT={COMMIT}",
                "SELF_CONTAINED=true",
                "RVC_RUNTIME_INCLUDED=true",
                "RVC_NATIVE_RUNNER_AVAILABLE=true",
                "",
            )
        ),
        encoding="utf-8",
    )
    (source / "images-manifest.json").write_text(
        json.dumps(
            {
                "images": [
                    {
                        "role": "runtime",
                        "image_id": runtime_image_id,
                    }
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    activation.write_text(
        json.dumps(
            {
                "format_version": 1,
                "kind": "rvc-runtime-activation",
                "gpu_smoke_verified": False,
                "profile_stage_set_verified": False,
                "native_sample_inference_verified": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    activation.chmod(activation_mode)
    (source / "SHA256SUMS").write_text("fixture ledger\n", encoding="utf-8")

    archive = private / ARCHIVE_NAME
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(source, arcname=source.name, recursive=True)
    checksum = Path(f"{archive}.sha256")
    checksum.write_text(f"{_sha256(archive)}  {archive.name}\n", encoding="ascii")
    verifier = tmp_path / "verifier.py"
    _write_python(
        verifier,
        """#!/usr/bin/env python3
import os
import pathlib
import sys

with open(os.environ["FAKE_VERIFIER_LOG"], "a", encoding="utf-8") as stream:
    stream.write(" ".join(sys.argv[1:]) + "\\n")
if os.environ.get("FAKE_VERIFIER_FAIL") == sys.argv[1]:
    raise SystemExit(23)
swap = os.environ.get("FAKE_SWAP_VERIFIER")
if swap and sys.argv[1] == "verify-ledger":
    replacement = pathlib.Path(f"{swap}.replacement")
    replacement.write_text("raise SystemExit(91)\\n", encoding="utf-8")
    os.replace(replacement, swap)
source_archive = os.environ.get("FAKE_MUTATE_SOURCE_ARCHIVE")
if source_archive and sys.argv[1] == "verify-ledger":
    pathlib.Path(source_archive).write_bytes(b"mutated after private snapshot")
race = os.environ.get("FAKE_RACE_ARCHIVE")
if race and sys.argv[1] == "verify-bundle":
    pathlib.Path(race).write_text("competitor\\n", encoding="utf-8")
""",
    )
    return {
        "archive": archive,
        "checksum": checksum,
        "output": output,
        "verifier": verifier,
        "verifier_log": tmp_path / "verifier.log",
    }


def _command(paths: dict[str, Path]) -> list[str]:
    return [
        sys.executable,
        str(PUBLISHER),
        "--archive",
        str(paths["archive"]),
        "--checksum",
        str(paths["checksum"]),
        "--output-dir",
        str(paths["output"]),
        "--verifier",
        str(paths["verifier"]),
        "--component",
        "worker",
        "--version",
        VERSION,
        "--source-commit",
        COMMIT,
        "--runtime-image-id",
        RUNTIME_ID,
    ]


def _run(
    paths: dict[str, Path],
    **environment: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _command(paths),
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "FAKE_VERIFIER_LOG": str(paths["verifier_log"]),
            **environment,
        },
    )


def test_publisher_verifies_and_no_clobber_publishes_pair(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    result = _run(paths)
    assert result.returncode == 0, result.stdout + result.stderr
    final_archive = paths["output"] / ARCHIVE_NAME
    final_checksum = Path(f"{final_archive}.sha256")
    assert final_archive.read_bytes() == paths["archive"].read_bytes()
    assert final_checksum.read_bytes() == paths["checksum"].read_bytes()
    assert not list(paths["output"].glob(".*.tmp.*"))
    verifier_lines = paths["verifier_log"].read_text(encoding="utf-8").splitlines()
    assert len(verifier_lines) == 2
    assert verifier_lines[0].startswith("verify-ledger --root ")
    assert "--ledger-name SHA256SUMS" in verifier_lines[0]
    assert verifier_lines[1].startswith("verify-bundle --root ")
    assert f"--component worker --version {VERSION} --source-commit {COMMIT}" in verifier_lines[1]

    original = final_archive.read_bytes()
    repeated = _run(paths)
    assert repeated.returncode != 0
    assert final_archive.read_bytes() == original


def test_publisher_executes_the_already_opened_verifier_bytes(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    result = _run(paths, FAKE_SWAP_VERIFIER=str(paths["verifier"]))
    assert result.returncode == 0, result.stdout + result.stderr
    verifier_lines = paths["verifier_log"].read_text(encoding="utf-8").splitlines()
    assert len(verifier_lines) == 2
    assert (paths["output"] / ARCHIVE_NAME).is_file()


def test_publisher_uses_a_stable_private_archive_snapshot(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    expected_archive = paths["archive"].read_bytes()
    result = _run(paths, FAKE_MUTATE_SOURCE_ARCHIVE=str(paths["archive"]))
    assert result.returncode == 0, result.stdout + result.stderr
    assert paths["archive"].read_bytes() != expected_archive
    assert (paths["output"] / ARCHIVE_NAME).read_bytes() == expected_archive


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("checksum", "checksum sidecar does not match"),
        ("activation-mode", "activation must be a read-only"),
        ("runtime-id", "runtime image ID differs"),
        ("verifier", "verifier rejected verify-ledger"),
    ),
)
def test_publisher_rejects_invalid_candidate_before_output(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    paths = _fixture(
        tmp_path,
        activation_mode=0o644 if mutation == "activation-mode" else 0o444,
        runtime_image_id=(
            "sha256:" + hashlib.sha256(b"wrong-runtime").hexdigest()
            if mutation == "runtime-id"
            else RUNTIME_ID
        ),
    )
    environment: dict[str, str] = {}
    if mutation == "checksum":
        paths["checksum"].write_text(f"{'0' * 64}  {ARCHIVE_NAME}\n", encoding="ascii")
    if mutation == "verifier":
        environment["FAKE_VERIFIER_FAIL"] = "verify-ledger"
    result = _run(paths, **environment)
    assert result.returncode != 0
    assert expected in result.stderr
    assert not list(paths["output"].iterdir())


@pytest.mark.parametrize("unsafe_type", ("traversal", "symlink"))
def test_publisher_rejects_unsafe_tar_members(tmp_path: Path, unsafe_type: str) -> None:
    paths = _fixture(tmp_path)
    root_name = ARCHIVE_NAME.removesuffix(".tar.gz")
    with tarfile.open(paths["archive"], "w:gz") as bundle:
        root = tarfile.TarInfo(root_name)
        root.type = tarfile.DIRTYPE
        bundle.addfile(root)
        member = tarfile.TarInfo(
            f"{root_name}/../../escape" if unsafe_type == "traversal" else f"{root_name}/link"
        )
        if unsafe_type == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "target"
        bundle.addfile(member)
    paths["checksum"].write_text(f"{_sha256(paths['archive'])}  {ARCHIVE_NAME}\n", encoding="ascii")
    result = _run(paths)
    assert result.returncode != 0
    assert "release archive" in result.stderr
    assert not list(paths["output"].iterdir())


def test_publisher_rejects_output_symlink_and_preserves_competitor(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    real_output = paths["output"]
    symlink_output = tmp_path / "output-link"
    symlink_output.symlink_to(real_output, target_is_directory=True)
    command = _command(paths)
    command[command.index(str(real_output))] = str(symlink_output)
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "FAKE_VERIFIER_LOG": str(paths["verifier_log"])},
    )
    assert result.returncode != 0
    assert "output directory must be a real directory" in result.stderr

    competitor = real_output / ARCHIVE_NAME
    raced = _run(paths, FAKE_RACE_ARCHIVE=str(competitor))
    assert raced.returncode != 0
    assert competitor.read_text(encoding="utf-8") == "competitor\n"
    assert not Path(f"{competitor}.sha256").exists()


def test_pair_publication_rolls_back_only_its_sidecar_on_archive_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / ARCHIVE_NAME
    checksum = tmp_path / f"{ARCHIVE_NAME}.sha256"
    output = tmp_path / "output"
    archive.write_bytes(b"private archive")
    checksum.write_bytes(b"private checksum")
    output.mkdir()
    archive_descriptor = os.open(archive, os.O_RDONLY)
    checksum_descriptor = os.open(checksum, os.O_RDONLY)
    real_link = os.link
    call_count = 0

    def racing_link(*args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            (output / ARCHIVE_NAME).write_bytes(b"competitor")
        real_link(*args, **kwargs)

    monkeypatch.setattr(PUBLISH.os, "link", racing_link)
    try:
        with pytest.raises(PUBLISH.PublicationError):
            PUBLISH._publish_pair(
                archive_descriptor,
                os.fstat(archive_descriptor),
                checksum_descriptor,
                os.fstat(checksum_descriptor),
                output_dir=output,
                archive_name=ARCHIVE_NAME,
            )
    finally:
        os.close(archive_descriptor)
        os.close(checksum_descriptor)
    assert (output / ARCHIVE_NAME).read_bytes() == b"competitor"
    assert not (output / f"{ARCHIVE_NAME}.sha256").exists()
    assert not list(output.glob(".*.tmp.*"))
