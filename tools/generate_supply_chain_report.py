#!/usr/bin/env python3
"""Create a deterministic, explicitly partial CycloneDX dependency inventory.

The report intentionally distinguishes exact version locks from distribution
hashes, image digests, vulnerability scans, and legal review.  It must never be
used to turn those missing release gates into an implied attestation.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
PYTHON_LOCKS = {
    "manager": (
        ROOT / "apps/api/requirements.lock",
        ROOT / "infra/mlflow/requirements.lock",
    ),
    "worker": (ROOT / "apps/worker/requirements.lock",),
}
IMAGE_KEYS = {
    "manager": (
        "API_IMAGE",
        "WEB_IMAGE",
        "MLFLOW_IMAGE",
        "POSTGRES_IMAGE",
        "REDIS_IMAGE",
        "MINIO_IMAGE",
        "MINIO_CLIENT_IMAGE",
        "NGINX_IMAGE",
    ),
    "worker": ("WORKER_IMAGE",),
}
DOCKERFILES = {
    "manager": (
        ROOT / "apps/api/Dockerfile",
        ROOT / "apps/web/Dockerfile",
        ROOT / "infra/mlflow/Dockerfile",
    ),
    "worker": (
        ROOT / "apps/worker/Dockerfile",
        ROOT / "apps/worker/Dockerfile.rvc",
    ),
}
LOCK_ENTRY = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)$")
RELEASE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DOCKER_ARG = re.compile(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=([^\s]+))?$", re.IGNORECASE)
DOCKER_VARIABLE = re.compile(r"^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))$")


class SupplyChainError(RuntimeError):
    """Raised when a lock cannot be represented without silently omitting data."""


def canonical_python_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SupplyChainError(f"JSON document must be an object: {path}")
    return document


def parse_python_lock(path: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = LOCK_ENTRY.fullmatch(line)
        if match is None:
            raise SupplyChainError(
                f"runtime lock must contain only exact name==version entries: {path}:{line_number}"
            )
        name = canonical_python_name(match.group(1))
        if name in seen:
            raise SupplyChainError(f"duplicate Python package in {path}: {name}")
        seen.add(name)
        entries.append((name, match.group(2)))
    if not entries:
        raise SupplyChainError(f"runtime lock is empty: {path}")
    return sorted(entries)


def python_components(
    component: str,
    license_catalog: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    components_by_ref: dict[str, dict[str, Any]] = {}
    notices_by_ref: dict[str, dict[str, Any]] = {}
    locks: list[dict[str, str]] = []
    for path in PYTHON_LOCKS[component]:
        relative = path.relative_to(ROOT).as_posix()
        locks.append({"path": relative, "sha256": sha256_file(path)})
        for name, version in parse_python_lock(path):
            key = f"{name}=={version}"
            license_expression = license_catalog.get(key)
            if not isinstance(license_expression, str) or not license_expression:
                raise SupplyChainError(f"missing reviewed declaration for {key}")
            purl = f"pkg:pypi/{quote(name, safe='')}@{quote(version, safe='.+')}"
            component_entry = components_by_ref.get(purl)
            if component_entry is None:
                component_entry = {
                    "type": "library",
                    "bom-ref": purl,
                    "name": name,
                    "version": version,
                    "purl": purl,
                    "licenses": [{"expression": license_expression}],
                    "properties": [
                        {"name": "rvc.lock.path", "value": relative},
                        {
                            "name": "rvc.distribution.hash.status",
                            "value": "missing-release-gate",
                        },
                        {
                            "name": "rvc.license.review.status",
                            "value": "declared-metadata-not-legal-review",
                        },
                    ],
                }
                components_by_ref[purl] = component_entry
            else:
                properties = component_entry["properties"]
                lock_property = {"name": "rvc.lock.path", "value": relative}
                if lock_property not in properties:
                    properties.append(lock_property)
                    properties.sort(key=lambda item: (item["name"], item["value"]))
            notice = notices_by_ref.get(purl)
            if notice is None:
                notice = {
                    "ecosystem": "PyPI",
                    "name": name,
                    "version": version,
                    "license_expression": license_expression,
                    "source": relative,
                    "sources": [relative],
                    "review_status": "declared-metadata-not-legal-review",
                }
                notices_by_ref[purl] = notice
            elif relative not in notice["sources"]:
                notice["sources"].append(relative)
                notice["sources"].sort()
    return (
        sorted(components_by_ref.values(), key=lambda item: str(item["bom-ref"])),
        sorted(notices_by_ref.values(), key=lambda item: str(item["name"])),
        locks,
    )


def npm_name_from_path(path: str) -> str:
    name = path.rsplit("node_modules/", 1)[-1]
    if not name or "/node_modules/" in name:
        raise SupplyChainError(f"cannot derive npm package name from lock path: {path}")
    return name


def npm_purl(name: str, version: str) -> str:
    if name.startswith("@") and "/" in name:
        namespace, package = name[1:].split("/", 1)
        encoded_name = f"%40{quote(namespace, safe='')}/{quote(package, safe='')}"
    else:
        encoded_name = quote(name, safe="")
    return f"pkg:npm/{encoded_name}@{quote(version, safe='.+')}"


def integrity_hash(integrity: str) -> dict[str, str]:
    try:
        algorithm, encoded = integrity.split("-", 1)
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise SupplyChainError("invalid npm integrity value") from exc
    algorithms = {"sha256": "SHA-256", "sha384": "SHA-384", "sha512": "SHA-512"}
    cyclone_algorithm = algorithms.get(algorithm.lower())
    if cyclone_algorithm is None:
        raise SupplyChainError(f"unsupported npm integrity algorithm: {algorithm}")
    return {"alg": cyclone_algorithm, "content": raw.hex()}


def node_components() -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, str]]:
    path = ROOT / "apps/web/package-lock.json"
    lock = load_json(path)
    if lock.get("lockfileVersion") != 3:
        raise SupplyChainError("apps/web/package-lock.json must use lockfileVersion 3")
    raw_packages = lock.get("packages")
    if not isinstance(raw_packages, dict):
        raise SupplyChainError("npm lock has no packages object")
    by_ref: dict[str, dict[str, Any]] = {}
    notice_by_ref: dict[str, dict[str, str]] = {}
    for package_path, raw in raw_packages.items():
        if not isinstance(package_path, str) or not package_path.startswith("node_modules/"):
            continue
        if not isinstance(raw, dict):
            raise SupplyChainError(f"invalid npm lock entry: {package_path}")
        version = raw.get("version")
        license_expression = raw.get("license")
        integrity = raw.get("integrity")
        if (
            not isinstance(version, str)
            or not version
            or not isinstance(license_expression, str)
            or not license_expression
            or not isinstance(integrity, str)
            or not integrity
        ):
            raise SupplyChainError(f"npm entry lacks version/license/integrity: {package_path}")
        name = npm_name_from_path(package_path)
        purl = npm_purl(name, version)
        scope = "excluded" if raw.get("dev") is True else "required"
        candidate = {
            "type": "library",
            "bom-ref": purl,
            "name": name,
            "version": version,
            "scope": scope,
            "purl": purl,
            "hashes": [integrity_hash(integrity)],
            "licenses": [{"expression": license_expression}],
            "properties": [
                {"name": "rvc.lock.path", "value": "apps/web/package-lock.json"},
                {
                    "name": "rvc.license.review.status",
                    "value": "package-lock-declaration-not-legal-review",
                },
            ],
        }
        existing = by_ref.get(purl)
        if existing is None or (existing.get("scope") == "excluded" and scope == "required"):
            by_ref[purl] = candidate
            notice_by_ref[purl] = {
                "ecosystem": "npm",
                "name": name,
                "version": version,
                "license_expression": license_expression,
                "source": "apps/web/package-lock.json",
                "review_status": "package-lock-declaration-not-legal-review",
            }
    return (
        sorted(by_ref.values(), key=lambda item: str(item["bom-ref"])),
        sorted(notice_by_ref.values(), key=lambda item: (item["name"], item["version"])),
        {"path": path.relative_to(ROOT).as_posix(), "sha256": sha256_file(path)},
    )


def environment_values() -> dict[str, str]:
    values: dict[str, str] = {}
    path = ROOT / ".env.example"
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in values:
            raise SupplyChainError(f"duplicate environment assignment: {path}:{line_number}: {key}")
        values[key] = value
    return values


def dockerfile_base_references(path: Path) -> list[str]:
    arguments: dict[str, str] = {}
    references: list[str] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        argument = DOCKER_ARG.fullmatch(line)
        if argument is not None:
            name, default = argument.groups()
            if default is not None:
                arguments[name] = default
            continue
        parts = line.split()
        if not parts or parts[0].upper() != "FROM":
            continue
        index = 1
        while index < len(parts) and parts[index].startswith("--"):
            index += 1
        if index >= len(parts):
            raise SupplyChainError(f"Dockerfile FROM has no image: {path}:{line_number}")
        reference = parts[index]
        variable = DOCKER_VARIABLE.fullmatch(reference)
        if variable is not None:
            name = variable.group(1) or variable.group(2)
            resolved = arguments.get(name)
            if resolved is None:
                raise SupplyChainError(
                    f"Dockerfile FROM variable has no fixed default: {path}:{line_number}: {name}"
                )
            reference = resolved
        if "$" in reference or any(character.isspace() for character in reference):
            raise SupplyChainError(
                f"Dockerfile FROM image is not a fixed reference: {path}:{line_number}"
            )
        references.append(reference)
    if not references:
        raise SupplyChainError(f"Dockerfile has no base image: {path}")
    return references


def container_component(reference: str, source: str) -> dict[str, Any]:
    digest_present = "@sha256:" in reference
    without_digest = reference.split("@", 1)[0]
    name, separator, version = without_digest.rpartition(":")
    if not separator or "/" in version:
        name, version = without_digest, "latest"
    purl = f"pkg:docker/{quote(name, safe='/')}@{quote(version, safe='.-_')}"
    properties = [
        {"name": "rvc.image.reference", "value": reference},
        {
            "name": "rvc.image.digest.status",
            "value": "verified" if digest_present else "missing-release-gate",
        },
        {"name": "rvc.image.reference.source", "value": source},
    ]
    component: dict[str, Any] = {
        "type": "container",
        "bom-ref": f"{purl}?source={quote(source, safe='')}",
        "name": name,
        "version": version,
        "purl": purl,
        "properties": properties,
    }
    if digest_present:
        digest = reference.split("@sha256:", 1)[1]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SupplyChainError(f"invalid image SHA-256 digest: {reference}")
        component["hashes"] = [{"alg": "SHA-256", "content": digest}]
    return component


def container_components(component: str) -> list[dict[str, Any]]:
    values = environment_values()
    result = [
        container_component(values[key], f".env.example:{key}")
        for key in IMAGE_KEYS[component]
        if key in values
    ]
    if len(result) != len(IMAGE_KEYS[component]):
        raise SupplyChainError(f"missing image references for {component}")
    for dockerfile in DOCKERFILES[component]:
        relative = dockerfile.relative_to(ROOT).as_posix()
        for reference in dockerfile_base_references(dockerfile):
            result.append(
                container_component(
                    reference,
                    relative,
                )
            )
    unique = {str(item["bom-ref"]): item for item in result}
    return sorted(unique.values(), key=lambda item: str(item["bom-ref"]))


def build_report(component: str, version: str) -> tuple[dict[str, Any], dict[str, Any]]:
    catalog = load_json(ROOT / "supply-chain/python-runtime-licenses.json")
    python, python_notices, python_locks = python_components(component, catalog)
    node: list[dict[str, Any]] = []
    node_notices: list[dict[str, str]] = []
    lock_documents = list(python_locks)
    if component == "manager":
        node, node_notices, node_lock = node_components()
        lock_documents.append(node_lock)
    containers = container_components(component)
    if RELEASE_VERSION.fullmatch(version) is None:
        raise SupplyChainError("invalid release version")
    root_ref = f"urn:rvc-orchestrator:{component}:{version}"
    all_components = sorted([*python, *node, *containers], key=lambda item: str(item["bom-ref"]))
    sbom = {
        "$schema": "https://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": root_ref,
                "name": f"rvc-orchestrator-{component}",
                "version": version,
            },
            "properties": [
                {
                    "name": "rvc.report.status",
                    "value": "partial-release-gates-open",
                },
                {
                    "name": "rvc.vulnerability.scan.status",
                    "value": "not-run",
                },
                {
                    "name": "rvc.license.legal-review.status",
                    "value": "not-complete",
                },
            ],
        },
        "components": all_components,
        "dependencies": [
            {
                "ref": root_ref,
                "dependsOn": [str(item["bom-ref"]) for item in all_components],
            }
        ],
    }
    licenses = {
        "format_version": 1,
        "component": component,
        "status": "declared-metadata-not-legal-review",
        "lock_documents": sorted(lock_documents, key=lambda item: item["path"]),
        "packages": sorted(
            [*python_notices, *node_notices],
            key=lambda item: (item["ecosystem"], item["name"], item["version"]),
        ),
        "containers": [
            {
                "reference": str(
                    next(
                        property_["value"]
                        for property_ in item["properties"]
                        if property_["name"] == "rvc.image.reference"
                    )
                ),
                "source": str(
                    next(
                        property_["value"]
                        for property_ in item["properties"]
                        if property_["name"] == "rvc.image.reference.source"
                    )
                ),
                "digest_status": str(
                    next(
                        property_["value"]
                        for property_ in item["properties"]
                        if property_["name"] == "rvc.image.digest.status"
                    )
                ),
                "review_status": "container-license-not-reviewed",
            }
            for item in containers
        ],
    }
    return sbom, licenses


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", choices=("manager", "worker"), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    sbom, licenses = build_report(args.component, args.version)
    write_json(output_dir / "sbom.cdx.json", sbom)
    write_json(output_dir / "third-party-licenses.json", licenses)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
