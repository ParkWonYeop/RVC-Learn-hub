from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "infra/worker/runtime"
QUALIFICATION_SCRIPT = RUNTIME / "qualification.py"


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


QUALIFICATION = _load_module("runtime_qualification", QUALIFICATION_SCRIPT)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _commit(value: str) -> str:
    return hashlib.sha1(value.encode()).hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _report_bytes(case_id: str) -> bytes:
    return (
        json.dumps(
            {"case_id": case_id, "result": "passed", "summary": "synthetic fixture"},
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _write_evidence(
    path: Path,
    case_ids: list[str],
    *,
    extra_member: str | None = None,
    symlink_member: str | None = None,
    empty_member: str | None = None,
) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for case_id in case_ids:
            name = f"reports/{case_id}.json"
            content = _report_bytes(case_id)
            member = tarfile.TarInfo(name)
            member.mode = 0o444
            if name == symlink_member:
                member.type = tarfile.SYMTYPE
                member.linkname = "other.json"
                member.size = 0
                archive.addfile(member)
            elif name == empty_member:
                member.size = 0
                archive.addfile(member, io.BytesIO())
            else:
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        if extra_member is not None:
            content = b'{"unexpected":true}\n'
            member = tarfile.TarInfo(extra_member)
            member.mode = 0o444
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))


def _write_build_manifest(path: Path, runtime: dict[str, str]) -> None:
    values = {
        "RUNTIME_BUILD_FORMAT_VERSION": "1",
        "PRODUCT": "rvc-training-orchestrator",
        "COMPONENT": "worker-rvc-runtime",
        "IMAGE": f"rvc-orchestrator-worker-rvc:{runtime['release_version']}",
        "RELEASE_VERSION": runtime["release_version"],
        "ORCHESTRATOR_SOURCE_COMMIT": runtime["orchestrator_commit"],
        "BASE_IMAGE": runtime["base_image"],
        "RVC_SOURCE_COMMIT": runtime["rvc_commit"],
        "RVC_SOURCE_MANIFEST_SHA256": runtime["source_manifest_sha256"],
        "RVC_WHEELHOUSE_MANIFEST_SHA256": runtime["wheelhouse_manifest_sha256"],
        "RVC_ASSET_MANIFEST_SHA256": runtime["asset_manifest_sha256"],
        "RVC_PROJECTION_MANIFEST_SHA256": runtime["projection_manifest_sha256"],
        "RVC_FAIRSEQ_COMMIT": runtime["fairseq_commit"],
        "RVC_TORCH_VERSION": runtime["torch"],
        "RVC_CUDA_RUNTIME_VERSION": runtime["cuda"],
        "RVC_CUDNN_MAJOR": runtime["cudnn"],
        "GPU_SMOKE_VERIFIED": "false",
        "PROFILE_STAGE_SET_VERIFIED": "false",
    }
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8"
    )


def _fixture(tmp_path: Path) -> dict[str, Any]:
    asset_manifest = tmp_path / "assets-manifest.json"
    _write_json(
        asset_manifest,
        {
            "schema_version": 1,
            "kind": "rvc-assets",
            "rvc_commit": QUALIFICATION.RVC_COMMIT,
            "assets": [
                {
                    "path": "runtime/crepe/full.pth",
                    "sha256": _sha256_bytes(b"reviewed-crepe-fixture"),
                    "size": len(b"reviewed-crepe-fixture"),
                }
            ],
        },
    )
    runtime = {
        "image_digest": f"sha256:{_sha256_bytes(b'qualified-runtime-image')}",
        "release_version": "1.0.0-rc.1",
        "orchestrator_commit": _commit("orchestrator"),
        "rvc_commit": QUALIFICATION.RVC_COMMIT,
        "base_image": (
            QUALIFICATION.BASE_IMAGE_PREFIX + _sha256_bytes(b"reviewed-amd64-base")
        ),
        "source_manifest_sha256": _sha256_bytes(b"source-manifest"),
        "wheelhouse_manifest_sha256": _sha256_bytes(b"wheelhouse-manifest"),
        "asset_manifest_sha256": _sha256_file(asset_manifest),
        "projection_manifest_sha256": _sha256_bytes(b"projection-manifest"),
        "fairseq_commit": _commit("fairseq"),
        "torch": QUALIFICATION.TORCH_VERSION,
        "torchvision": QUALIFICATION.TORCHVISION_VERSION,
        "torchaudio": QUALIFICATION.TORCHAUDIO_VERSION,
        "cuda": QUALIFICATION.CUDA_VERSION,
        "cudnn": QUALIFICATION.CUDNN_VERSION,
    }
    case_ids = sorted(QUALIFICATION.REQUIRED_CASE_IDS)
    evidence = tmp_path / "runtime-qualification-evidence.tar.gz"
    _write_evidence(evidence, case_ids)
    cases = [
        {
            "case_id": case_id,
            "result": "passed",
            "report_path": f"reports/{case_id}.json",
            "report_sha256": _sha256_bytes(_report_bytes(case_id)),
        }
        for case_id in case_ids
    ]
    qualification = {
        "format_version": 1,
        "kind": "rvc-native-runtime-qualification",
        "runtime": runtime,
        "cases": cases,
        "evidence_archive": {
            "file": evidence.name,
            "size": evidence.stat().st_size,
            "sha256": _sha256_file(evidence),
        },
        "review": {
            "reviewed_at": "2026-07-12T01:02:03.456Z",
            "reviewer": "release-reviewer@example.test",
        },
    }
    qualification_path = tmp_path / "runtime-qualification.json"
    _write_json(qualification_path, qualification)
    build_manifest = tmp_path / "runtime-build.manifest"
    _write_build_manifest(build_manifest, runtime)
    return {
        "asset_manifest": asset_manifest,
        "build_manifest": build_manifest,
        "case_ids": case_ids,
        "evidence": evidence,
        "qualification": qualification,
        "qualification_path": qualification_path,
        "runtime": runtime,
    }


def _run_project(
    fixture: dict[str, Any],
    output: Path,
    *,
    image_digest: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(QUALIFICATION_SCRIPT),
            "project",
            "--qualification",
            str(fixture["qualification_path"]),
            "--evidence-archive",
            str(fixture["evidence"]),
            "--runtime-build-manifest",
            str(fixture["build_manifest"]),
            "--asset-manifest",
            str(fixture["asset_manifest"]),
            "--runtime-image-digest",
            image_digest or fixture["runtime"]["image_digest"],
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def _refresh_archive_record(fixture: dict[str, Any]) -> None:
    evidence = fixture["evidence"]
    fixture["qualification"]["evidence_archive"] = {
        "file": evidence.name,
        "size": evidence.stat().st_size,
        "sha256": _sha256_file(evidence),
    }
    _write_json(fixture["qualification_path"], fixture["qualification"])


def test_verify_api_checks_complete_chain_without_writing_activation(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    before = {path.name for path in tmp_path.iterdir()}

    runtime = QUALIFICATION.verify_qualification_evidence(
        qualification_path=fixture["qualification_path"],
        evidence_archive_path=fixture["evidence"],
        runtime_build_manifest_path=fixture["build_manifest"],
        asset_manifest_path=fixture["asset_manifest"],
        runtime_image_digest=fixture["runtime"]["image_digest"],
    )

    assert runtime == fixture["runtime"]
    assert {path.name for path in tmp_path.iterdir()} == before
    assert not list(tmp_path.rglob("*activation*"))


def test_valid_complete_matrix_projects_exact_read_only_activation(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    output = tmp_path / "runtime-activation.json"

    result = _run_project(fixture, output)

    assert result.returncode == 0, result.stdout + result.stderr
    activation = json.loads(output.read_text(encoding="utf-8"))
    assert activation == {
        "format_version": 1,
        "kind": "rvc-runtime-activation",
        "runtime_image_digest": fixture["runtime"]["image_digest"],
        "runtime_asset_manifest_sha256": fixture["runtime"]["asset_manifest_sha256"],
        "qualification_evidence_sha256": _sha256_file(fixture["qualification_path"]),
        "gpu_smoke_verified": True,
        "profile_stage_set_verified": True,
        "native_sample_inference_verified": True,
        "supported_inference_f0_methods": ["pm", "harvest", "crepe", "rmvpe"],
    }
    assert output.stat().st_mode & 0o777 == 0o444


@pytest.mark.parametrize("mutation", ["missing", "extra", "failed", "duplicate"])
def test_incomplete_failed_or_duplicate_case_set_is_rejected(
    tmp_path: Path, mutation: str
) -> None:
    fixture = _fixture(tmp_path)
    cases = fixture["qualification"]["cases"]
    if mutation == "missing":
        cases.pop()
    elif mutation == "extra":
        cases.append(
            {
                "case_id": "sample-v3-40k-index-off-pm",
                "result": "passed",
                "report_path": "reports/sample-v3-40k-index-off-pm.json",
                "report_sha256": _sha256_bytes(b"extra"),
            }
        )
    elif mutation == "failed":
        cases[0]["result"] = "failed"
    else:
        cases.append(dict(cases[0]))
    _write_json(fixture["qualification_path"], fixture["qualification"])

    result = _run_project(fixture, tmp_path / "activation.json")

    assert result.returncode == 1
    assert "qualification error:" in result.stderr
    assert not (tmp_path / "activation.json").exists()


def test_archive_hash_tamper_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["evidence"].write_bytes(fixture["evidence"].read_bytes() + b"tamper")
    fixture["qualification"]["evidence_archive"]["size"] = fixture["evidence"].stat().st_size
    _write_json(fixture["qualification_path"], fixture["qualification"])

    result = _run_project(fixture, tmp_path / "activation.json")

    assert result.returncode == 1
    assert "archive SHA-256" in result.stderr


def test_report_path_and_case_identity_must_match(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["qualification"]["cases"][0]["report_path"] = "reports/../escape.json"
    _write_json(fixture["qualification_path"], fixture["qualification"])

    result = _run_project(fixture, tmp_path / "activation.json")

    assert result.returncode == 1
    assert "safe relative path" in result.stderr


def test_archive_with_extra_member_is_rejected_before_unbounded_iteration(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    _write_evidence(
        fixture["evidence"],
        fixture["case_ids"],
        extra_member="reports/unlisted.json",
    )
    _refresh_archive_record(fixture)

    result = _run_project(fixture, tmp_path / "activation.json")

    assert result.returncode == 1
    assert "too many members" in result.stderr


def test_archive_symlink_report_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    symlink_name = fixture["qualification"]["cases"][0]["report_path"]
    _write_evidence(
        fixture["evidence"],
        fixture["case_ids"],
        symlink_member=symlink_name,
    )
    _refresh_archive_record(fixture)

    result = _run_project(fixture, tmp_path / "activation.json")

    assert result.returncode == 1
    assert "not a regular report" in result.stderr


def test_archive_empty_report_is_rejected_by_per_report_limit(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    empty_name = fixture["qualification"]["cases"][0]["report_path"]
    _write_evidence(
        fixture["evidence"],
        fixture["case_ids"],
        empty_member=empty_name,
    )
    _refresh_archive_record(fixture)

    result = _run_project(fixture, tmp_path / "activation.json")

    assert result.returncode == 1
    assert "report has unsafe size" in result.stderr


def test_report_byte_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["qualification"]["cases"][0]["report_sha256"] = _sha256_bytes(
        b"different report"
    )
    _write_json(fixture["qualification_path"], fixture["qualification"])

    result = _run_project(fixture, tmp_path / "activation.json")

    assert result.returncode == 1
    assert "report SHA-256 mismatch" in result.stderr


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda fixture: fixture["runtime"].__setitem__(
                "release_version", "1.0.0-rc.2"
            ),
            "identity mismatch: release_version",
        ),
        (
            lambda fixture: fixture["build_manifest"].write_text(
                fixture["build_manifest"]
                .read_text(encoding="utf-8")
                .replace(
                    f"RVC_SOURCE_MANIFEST_SHA256={fixture['runtime']['source_manifest_sha256']}",
                    f"RVC_SOURCE_MANIFEST_SHA256={_sha256_bytes(b'other-source')}",
                ),
                encoding="utf-8",
            ),
            "identity mismatch: source_manifest_sha256",
        ),
        (
            lambda fixture: fixture["asset_manifest"].write_text(
                fixture["asset_manifest"].read_text(encoding="utf-8") + " ",
                encoding="utf-8",
            ),
            "asset manifest byte hash",
        ),
    ],
)
def test_runtime_build_and_asset_identity_mismatch_is_rejected(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    error: str,
) -> None:
    fixture = _fixture(tmp_path)
    mutate(fixture)
    _write_json(fixture["qualification_path"], fixture["qualification"])

    result = _run_project(fixture, tmp_path / "activation.json")

    assert result.returncode == 1
    assert error in result.stderr


def test_runtime_image_digest_argument_must_match_reviewed_identity(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    result = _run_project(
        fixture,
        tmp_path / "activation.json",
        image_digest=f"sha256:{_sha256_bytes(b'other-image')}",
    )

    assert result.returncode == 1
    assert "identity mismatch: image_digest" in result.stderr


def test_duplicate_json_key_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    original = fixture["qualification_path"].read_text(encoding="utf-8")
    fixture["qualification_path"].write_text(
        original.replace(
            '  "format_version": 1,',
            '  "format_version": 1,\n  "format_version": 1,',
            1,
        ),
        encoding="utf-8",
    )

    result = _run_project(fixture, tmp_path / "activation.json")

    assert result.returncode == 1
    assert "duplicate JSON key: format_version" in result.stderr


def test_unknown_json_field_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["qualification"]["unexpected_gate"] = True
    _write_json(fixture["qualification_path"], fixture["qualification"])

    result = _run_project(fixture, tmp_path / "activation.json")

    assert result.returncode == 1
    assert "extra=unexpected_gate" in result.stderr


def test_placeholder_hash_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["qualification"]["runtime"]["source_manifest_sha256"] = "0" * 64
    _write_json(fixture["qualification_path"], fixture["qualification"])

    result = _run_project(fixture, tmp_path / "activation.json")

    assert result.returncode == 1
    assert "placeholder hash" in result.stderr


def test_disabled_command_matches_template_and_refuses_existing_or_symlink(
    tmp_path: Path,
) -> None:
    output = tmp_path / "runtime-activation.json"
    result = subprocess.run(
        [
            sys.executable,
            str(QUALIFICATION_SCRIPT),
            "disabled",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_bytes() == (RUNTIME / "runtime-activation.json").read_bytes()
    assert output.stat().st_mode & 0o777 == 0o444

    existing = subprocess.run(
        [
            sys.executable,
            str(QUALIFICATION_SCRIPT),
            "disabled",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert existing.returncode == 1
    assert "already exists" in existing.stderr

    target = tmp_path / "target"
    target.write_text("preserve", encoding="utf-8")
    symlink = tmp_path / "activation-link.json"
    os.symlink(target, symlink)
    linked = subprocess.run(
        [
            sys.executable,
            str(QUALIFICATION_SCRIPT),
            "disabled",
            "--output",
            str(symlink),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert linked.returncode == 1
    assert "already exists or is a symlink" in linked.stderr
    assert target.read_text(encoding="utf-8") == "preserve"
