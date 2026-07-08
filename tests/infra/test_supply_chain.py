from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "tools/generate_supply_chain_report.py"


def _generate(component: str, destination: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    result = subprocess.run(
        [
            "python3",
            str(GENERATOR),
            "--component",
            component,
            "--version",
            "0.1.0",
            "--output-dir",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    sbom = json.loads((destination / "sbom.cdx.json").read_text(encoding="utf-8"))
    licenses = json.loads((destination / "third-party-licenses.json").read_text(encoding="utf-8"))
    assert isinstance(sbom, dict)
    assert isinstance(licenses, dict)
    return sbom, licenses


def _property(component: dict[str, Any], name: str) -> str | None:
    for item in component.get("properties", []):
        if item.get("name") == name:
            return str(item.get("value"))
    return None


def _canonical_requirement(requirement: str) -> str:
    name, version = requirement.lower().split("==", 1)
    return f"{re.sub(r'[-_.]+', '-', name)}=={version}"


def test_manager_inventory_is_deterministic_and_discloses_open_release_gates(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    sbom, licenses = _generate("manager", first)
    _generate("manager", second)

    assert (first / "sbom.cdx.json").read_bytes() == (second / "sbom.cdx.json").read_bytes()
    assert (first / "third-party-licenses.json").read_bytes() == (
        second / "third-party-licenses.json"
    ).read_bytes()
    assert sbom["$schema"] == "https://cyclonedx.org/schema/bom-1.6.schema.json"
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.6"
    assert "timestamp" not in sbom["metadata"]
    properties = {item["name"]: item["value"] for item in sbom["metadata"]["properties"]}
    assert properties == {
        "rvc.license.legal-review.status": "not-complete",
        "rvc.report.status": "partial-release-gates-open",
        "rvc.vulnerability.scan.status": "not-run",
    }
    components = sbom["components"]
    references = [item["bom-ref"] for item in components]
    assert len(references) == len(set(references))
    assert any(item.get("purl", "").startswith("pkg:pypi/fastapi@") for item in components)
    npm = next(item for item in components if item.get("purl", "").startswith("pkg:npm/next@"))
    assert npm["hashes"][0]["alg"] == "SHA-512"
    assert len(npm["hashes"][0]["content"]) == 128
    tagged_image = next(
        item
        for item in components
        if item["type"] == "container"
        and _property(item, "rvc.image.digest.status") == "missing-release-gate"
    )
    assert tagged_image
    container_coverage = {
        (
            _property(item, "rvc.image.reference"),
            _property(item, "rvc.image.reference.source"),
        )
        for item in components
        if item["type"] == "container"
    }
    assert {
        ("python:3.13-slim", "apps/api/Dockerfile"),
        ("node:24-alpine", "apps/web/Dockerfile"),
        ("ghcr.io/mlflow/mlflow:v3.1.1", "infra/mlflow/Dockerfile"),
        ("rvc-orchestrator-api:dev", ".env.example:API_IMAGE"),
        ("rvc-orchestrator-web:dev", ".env.example:WEB_IMAGE"),
        ("rvc-orchestrator-mlflow:dev", ".env.example:MLFLOW_IMAGE"),
    }.issubset(container_coverage)
    assert licenses["status"] == "declared-metadata-not-legal-review"
    assert len(licenses["packages"]) > 400
    assert {item["path"] for item in licenses["lock_documents"]} >= {
        "apps/api/requirements.lock",
        "apps/web/package-lock.json",
        "infra/mlflow/requirements.lock",
    }
    boto3_notice = next(
        item
        for item in licenses["packages"]
        if item["name"] == "boto3" and item["version"] == "1.43.46"
    )
    assert boto3_notice["sources"] == [
        "apps/api/requirements.lock",
        "infra/mlflow/requirements.lock",
    ]
    psycopg_notice = next(
        item for item in licenses["packages"] if item["name"] == "psycopg2-binary"
    )
    assert psycopg_notice["version"] == "2.9.10"
    assert psycopg_notice["license_expression"] == "LGPL-3.0-or-later"
    assert psycopg_notice["sources"] == ["infra/mlflow/requirements.lock"]
    assert {
        (item["reference"], item["source"]) for item in licenses["containers"]
    } == container_coverage
    assert {item["review_status"] for item in licenses["containers"]} == {
        "container-license-not-reviewed"
    }
    mlflow_dockerfile = (ROOT / "infra/mlflow/Dockerfile").read_text(encoding="utf-8")
    assert "COPY infra/mlflow/requirements.lock" in mlflow_dockerfile
    assert "--no-deps" in mlflow_dockerfile
    assert "--only-binary=:all:" in mlflow_dockerfile
    assert "--requirement /tmp/rvc-mlflow-requirements.lock" in mlflow_dockerfile


def test_worker_inventory_covers_exact_agent_lock_and_docker_uses_it(tmp_path: Path) -> None:
    sbom, licenses = _generate("worker", tmp_path)
    references = [item["bom-ref"] for item in sbom["components"]]
    assert len(references) == len(set(references))
    lock_entries = {
        _canonical_requirement(line.strip())
        for line in (ROOT / "apps/worker/requirements.lock")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.startswith("#")
    }
    python_components = {
        f"{item['name']}=={item['version']}".lower()
        for item in sbom["components"]
        if str(item.get("purl", "")).startswith("pkg:pypi/")
    }
    assert python_components == lock_entries
    assert {item["name"] for item in licenses["packages"]} == {
        entry.split("==", 1)[0] for entry in lock_entries
    }
    container_coverage = {
        (
            _property(item, "rvc.image.reference"),
            _property(item, "rvc.image.reference.source"),
        )
        for item in sbom["components"]
        if item["type"] == "container"
    }
    assert {
        ("python:3.11-slim-bookworm", "apps/worker/Dockerfile"),
        (
            "offline-rvc-base-image-must-be-provided",
            "apps/worker/Dockerfile.rvc",
        ),
        ("rvc-orchestrator-worker:dev", ".env.example:WORKER_IMAGE"),
    }.issubset(container_coverage)
    assert {
        (item["reference"], item["source"]) for item in licenses["containers"]
    } == container_coverage

    dockerfile = (ROOT / "apps/worker/Dockerfile").read_text(encoding="utf-8")
    assert "COPY apps/worker/requirements.lock apps/worker/requirements.lock" in dockerfile
    assert "--requirement ./apps/worker/requirements.lock" in dockerfile
    assert "--no-index" in dockerfile
    assert "--find-links=/wheels" in dockerfile


def test_bundle_builders_embed_checksum_covered_partial_reports() -> None:
    for component in ("manager", "worker"):
        script = (ROOT / f"installers/{component}/build-bundle.sh").read_text(encoding="utf-8")
        assert "generate_supply_chain_report.py" in script
        assert f"--component {component}" in script
        assert '--version "$version"' in script
        assert "SBOM_FORMAT=cyclonedx-1.6" in script
        assert "SBOM_PATH=supply-chain/sbom.cdx.json" in script
        assert "SBOM_STATUS=partial-release-gates-open" in script
        assert "THIRD_PARTY_LICENSES_PATH=supply-chain/third-party-licenses.json" in script
        assert "find . -type f ! -name SHA256SUMS" in script


def test_installer_rejects_missing_or_uncovered_supply_chain_report(tmp_path: Path) -> None:
    report_dir = tmp_path / "supply-chain"
    report_dir.mkdir()
    (tmp_path / "manifest.env").write_text(
        "\n".join(
            (
                "BUNDLE_FORMAT_VERSION=1",
                "SBOM_FORMAT=cyclonedx-1.6",
                "SBOM_PATH=supply-chain/sbom.cdx.json",
                "SBOM_STATUS=partial-release-gates-open",
                "THIRD_PARTY_LICENSES_PATH=supply-chain/third-party-licenses.json",
                "",
            )
        ),
        encoding="utf-8",
    )
    (report_dir / "sbom.cdx.json").write_text("{}\n", encoding="utf-8")
    (report_dir / "third-party-licenses.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "SHA256SUMS").write_text(
        "0" * 64 + "  supply-chain/sbom.cdx.json\n",
        encoding="utf-8",
    )
    command = 'source "$1"; rvc_validate_supply_chain_files "$2"'
    library = ROOT / "installers/common/lib.sh"

    rejected = subprocess.run(
        ["bash", "-c", command, "bash", str(library), str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "checksums do not cover" in rejected.stderr

    with (tmp_path / "SHA256SUMS").open("a", encoding="utf-8") as checksums:
        checksums.write("0" * 64 + "  supply-chain/third-party-licenses.json\n")
    accepted = subprocess.run(
        ["bash", "-c", command, "bash", str(library), str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert accepted.returncode == 0, accepted.stderr
