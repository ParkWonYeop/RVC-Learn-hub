from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "infra/worker/runtime"
READINESS_SCRIPT = RUNTIME / "release_readiness.py"


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


READINESS = _load_module("worker_release_readiness", READINESS_SCRIPT)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _review_manifest(
    root: Path,
    *,
    evidence_types: list[str],
    runtime_digest: str,
    base_image: str,
) -> Path:
    records: list[dict[str, object]] = []
    for evidence_type in evidence_types:
        evidence = root / "reports" / f"{evidence_type}.json"
        evidence.parent.mkdir(exist_ok=True)
        evidence.write_text(
            json.dumps(
                {
                    "evidence_type": evidence_type,
                    "runtime_image_digest": runtime_digest,
                    "synthetic_fixture": True,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        records.append(
            {
                "type": evidence_type,
                "path": evidence.relative_to(root).as_posix(),
                "size": evidence.stat().st_size,
                "sha256": _sha256_file(evidence),
                "result": READINESS.REQUIRED_REVIEW_EVIDENCE[evidence_type],
            }
        )
    manifest = root / "release-review.json"
    _write_json(
        manifest,
        {
            "format_version": 1,
            "kind": "rvc-worker-release-review",
            "runtime_image": {
                "digest": runtime_digest,
                "os": "linux",
                "architecture": "amd64",
                "user": "10001:10001",
            },
            "base_image": {
                "reference": base_image,
                "os": "linux",
                "architecture": "amd64",
            },
            "evidence": records,
            "review": {
                "reviewed_at": "2026-07-12T03:04:05Z",
                "reviewer": "release-reviewer@example.test",
            },
        },
    )
    return manifest


def _empty_arguments(**overrides: object) -> Any:
    values: dict[str, object] = {
        "source_manifest": None,
        "source_archive": None,
        "wheelhouse_manifest": None,
        "wheelhouse_root": None,
        "asset_manifest": None,
        "asset_root": None,
        "runtime_build_manifest": None,
        "runtime_image_digest": None,
        "qualification_manifest": None,
        "qualification_evidence": None,
        "release_review": None,
        "review_evidence_root": None,
    }
    values.update(overrides)
    return READINESS.Arguments(**values)


def _checks(report: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {check["id"]: check for check in report["checks"]}


def test_cli_without_inputs_enumerates_every_gate_and_is_fail_closed() -> None:
    result = subprocess.run(
        [sys.executable, str(READINESS_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["decision"] == "blocked"
    assert report["activation_permitted"] is False
    assert report["activation_projection_written"] is False
    assert report["required_qualification_case_count"] == 49
    assert report["verified"] == []
    assert report["invalid"] == []
    assert set(report["missing"]) == set(READINESS.CHECK_IDS)


def test_partial_review_enumerates_missing_scan_license_and_lifecycle_evidence(
    tmp_path: Path,
) -> None:
    runtime_digest = "sha256:" + _sha256_bytes(b"runtime-image")
    base_image = READINESS.qualification.BASE_IMAGE_PREFIX + _sha256_bytes(b"amd64-base")
    review = _review_manifest(
        tmp_path,
        evidence_types=["runtime-sbom", "vulnerability-scan"],
        runtime_digest=runtime_digest,
        base_image=base_image,
    )

    report = READINESS.build_report(
        _empty_arguments(
            runtime_image_digest=runtime_digest,
            release_review=review,
            review_evidence_root=tmp_path,
        )
    )
    checks = _checks(report)

    assert report["decision"] == "blocked"
    assert report["activation_permitted"] is False
    assert checks["release-review-manifest"]["status"] == "verified"
    assert checks["base-image-amd64-digest"]["status"] == "verified"
    assert checks["runtime-image-linux-amd64-user"]["status"] == "verified"
    assert checks["runtime-sbom"]["status"] == "verified"
    assert checks["vulnerability-scan"]["status"] == "verified"
    for missing in (
        "container-scan",
        "secret-scan",
        "sast-scan",
        "license-review",
        "clean-host-lifecycle",
    ):
        assert checks[missing]["status"] == "missing"


def test_review_evidence_hash_mismatch_is_scoped_to_its_check(tmp_path: Path) -> None:
    runtime_digest = "sha256:" + _sha256_bytes(b"runtime-image")
    base_image = READINESS.qualification.BASE_IMAGE_PREFIX + _sha256_bytes(b"amd64-base")
    review = _review_manifest(
        tmp_path,
        evidence_types=list(READINESS.REQUIRED_REVIEW_EVIDENCE),
        runtime_digest=runtime_digest,
        base_image=base_image,
    )
    secret_report = tmp_path / "reports/secret-scan.json"
    secret_report.write_text('{"changed":true}\n', encoding="utf-8")

    report = READINESS.build_report(
        _empty_arguments(
            runtime_image_digest=runtime_digest,
            release_review=review,
            review_evidence_root=tmp_path,
        )
    )
    checks = _checks(report)

    assert checks["secret-scan"]["status"] == "invalid"
    assert "secret-scan" in report["invalid"]
    assert checks["container-scan"]["status"] == "verified"
    assert report["activation_permitted"] is False


def test_review_rejects_non_amd64_runtime_identity(tmp_path: Path) -> None:
    runtime_digest = "sha256:" + _sha256_bytes(b"runtime-image")
    base_image = READINESS.qualification.BASE_IMAGE_PREFIX + _sha256_bytes(b"amd64-base")
    review = _review_manifest(
        tmp_path,
        evidence_types=[],
        runtime_digest=runtime_digest,
        base_image=base_image,
    )
    document = json.loads(review.read_text(encoding="utf-8"))
    document["runtime_image"]["architecture"] = "arm64"
    _write_json(review, document)

    report = READINESS.build_report(
        _empty_arguments(
            runtime_image_digest=runtime_digest,
            release_review=review,
            review_evidence_root=tmp_path,
        )
    )
    checks = _checks(report)

    assert checks["release-review-manifest"]["status"] == "invalid"
    assert checks["runtime-image-linux-amd64-user"]["status"] == "blocked-dependency"
    assert report["decision"] == "blocked"


def test_review_evidence_parent_symlink_is_rejected(tmp_path: Path) -> None:
    runtime_digest = "sha256:" + _sha256_bytes(b"runtime-image")
    base_image = READINESS.qualification.BASE_IMAGE_PREFIX + _sha256_bytes(b"amd64-base")
    outside = tmp_path / "outside"
    outside.mkdir()
    evidence = outside / "runtime-sbom.json"
    evidence.write_text('{"bomFormat":"CycloneDX"}\n', encoding="utf-8")
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    review = tmp_path / "release-review.json"
    _write_json(
        review,
        {
            "format_version": 1,
            "kind": "rvc-worker-release-review",
            "runtime_image": {
                "digest": runtime_digest,
                "os": "linux",
                "architecture": "amd64",
                "user": "10001:10001",
            },
            "base_image": {
                "reference": base_image,
                "os": "linux",
                "architecture": "amd64",
            },
            "evidence": [
                {
                    "type": "runtime-sbom",
                    "path": "linked/runtime-sbom.json",
                    "size": evidence.stat().st_size,
                    "sha256": _sha256_file(evidence),
                    "result": "complete",
                }
            ],
            "review": {
                "reviewed_at": "2026-07-12T03:04:05Z",
                "reviewer": "release-reviewer@example.test",
            },
        },
    )

    report = READINESS.build_report(
        _empty_arguments(
            runtime_image_digest=runtime_digest,
            release_review=review,
            review_evidence_root=tmp_path,
        )
    )

    assert _checks(report)["runtime-sbom"]["status"] == "invalid"


def test_complete_cross_bound_inventory_is_reported_but_never_activates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_manifest = tmp_path / "source-manifest.json"
    source_archive = tmp_path / "source.tar.gz"
    wheelhouse_root = tmp_path / "wheelhouse"
    wheelhouse_root.mkdir()
    wheelhouse_manifest = wheelhouse_root / "wheelhouse-manifest.json"
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    asset_manifest = asset_root / "assets-manifest.json"
    build_manifest = tmp_path / "runtime-build.env"
    qualification_manifest = tmp_path / "qualification.json"
    qualification_evidence = tmp_path / "qualification.tar.gz"
    for path, content in (
        (source_manifest, b'{"source":true}\n'),
        (source_archive, b"source-archive"),
        (wheelhouse_manifest, b'{"wheelhouse":true}\n'),
        (asset_manifest, b'{"assets":true}\n'),
        (build_manifest, b"build=true\n"),
        (qualification_manifest, b'{"qualification":true}\n'),
        (qualification_evidence, b"qualification-evidence"),
    ):
        path.write_bytes(content)

    runtime_digest = "sha256:" + _sha256_bytes(b"runtime-image")
    base_image = READINESS.qualification.BASE_IMAGE_PREFIX + _sha256_bytes(b"amd64-base")
    review = _review_manifest(
        tmp_path,
        evidence_types=list(READINESS.REQUIRED_REVIEW_EVIDENCE),
        runtime_digest=runtime_digest,
        base_image=base_image,
    )

    source_hash = _sha256_file(source_manifest)
    wheel_hash = _sha256_file(wheelhouse_manifest)
    asset_hash = _sha256_file(asset_manifest)
    qualification_calls: list[str] = []
    monkeypatch.setattr(
        READINESS.verify_inputs,
        "verify_source",
        lambda *_: {"commit": READINESS.qualification.RVC_COMMIT},
    )
    monkeypatch.setattr(
        READINESS.verify_inputs,
        "verify_wheelhouse",
        lambda *_: {"manifest_sha256": wheel_hash},
    )
    monkeypatch.setattr(
        READINESS.verify_inputs,
        "verify_assets",
        lambda *_: {"manifest_sha256": asset_hash},
    )
    monkeypatch.setattr(
        READINESS.qualification,
        "load_runtime_build_manifest",
        lambda _: {
            "RVC_SOURCE_MANIFEST_SHA256": source_hash,
            "RVC_WHEELHOUSE_MANIFEST_SHA256": wheel_hash,
            "RVC_ASSET_MANIFEST_SHA256": asset_hash,
            "BASE_IMAGE": base_image,
        },
    )

    def verify_qualification_evidence(**_: object) -> dict[str, str]:
        qualification_calls.append("called")
        return {"image_digest": runtime_digest}

    monkeypatch.setattr(
        READINESS.qualification,
        "verify_qualification_evidence",
        verify_qualification_evidence,
    )

    report = READINESS.build_report(
        _empty_arguments(
            source_manifest=source_manifest,
            source_archive=source_archive,
            wheelhouse_manifest=wheelhouse_manifest,
            wheelhouse_root=wheelhouse_root,
            asset_manifest=asset_manifest,
            asset_root=asset_root,
            runtime_build_manifest=build_manifest,
            runtime_image_digest=runtime_digest,
            qualification_manifest=qualification_manifest,
            qualification_evidence=qualification_evidence,
            release_review=review,
            review_evidence_root=tmp_path,
        )
    )

    assert qualification_calls == ["called"]
    assert report["decision"] == "evidence-inputs-verified"
    assert report["missing"] == []
    assert report["invalid"] == []
    assert len(report["verified"]) == len(READINESS.CHECK_IDS)
    assert report["activation_permitted"] is False
    assert report["activation_projection_written"] is False
    assert not list(tmp_path.rglob("*activation*"))
