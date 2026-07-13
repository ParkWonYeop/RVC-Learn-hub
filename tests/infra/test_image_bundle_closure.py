from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANAGER_BUILDER = ROOT / "installers/manager/build-bundle.sh"
VERIFIER = ROOT / "installers/common/image_bundle.py"
VERSION = "4.5.6"
COMMIT = "1234567890abcdef1234567890abcdef12345678"

MANAGER_SOURCES = {
    "api": f"rvc-orchestrator-api:{VERSION}",
    "web": f"rvc-orchestrator-web:{VERSION}",
    "mlflow": f"rvc-orchestrator-mlflow:{VERSION}",
    "postgres": "postgres:16-alpine",
    "redis": "redis:7.4-alpine",
    "minio": "minio/minio:RELEASE.2025-04-22T22-12-26Z",
    "minio-client": "minio/mc:RELEASE.2025-04-16T18-13-26Z",
    "nginx": "nginx:1.27-alpine",
}
MANAGER_RUNTIME = {
    **MANAGER_SOURCES,
    "postgres": f"rvc-orchestrator-postgres:{VERSION}",
    "redis": f"rvc-orchestrator-redis:{VERSION}",
    "minio": f"rvc-orchestrator-minio:{VERSION}",
    "minio-client": f"rvc-orchestrator-minio-client:{VERSION}",
    "nginx": f"rvc-orchestrator-nginx:{VERSION}",
}
MANAGER_USERS = {
    "api": "10001:10001",
    "web": "nextjs",
    "mlflow": "10002:10002",
    "postgres": "",
    "redis": "",
    "minio": "",
    "minio-client": "",
    "nginx": "",
}


def _config_bytes(role: str, labels: dict[str, str], user: str | None = None) -> bytes:
    resolved_user = MANAGER_USERS.get(role, "") if user is None else user
    return json.dumps(
        {
            "architecture": "amd64",
            "config": {"Labels": labels, "User": resolved_user},
            "os": "linux",
            "rvc_fake_role": role,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _initial_state() -> dict[str, Any]:
    references: dict[str, Any] = {}
    for role, reference in MANAGER_SOURCES.items():
        labels = {}
        if role in {"api", "web", "mlflow"}:
            labels = {
                "org.opencontainers.image.revision": COMMIT,
                "org.opencontainers.image.version": VERSION,
            }
        config = _config_bytes(role, labels)
        references[reference] = {
            "architecture": "amd64",
            "id": f"sha256:{hashlib.sha256(config).hexdigest()}",
            "labels": labels,
            "os": "linux",
            "role": role,
            "user": MANAGER_USERS[role],
        }
    return {"refs": references}


def _write_fake_commands(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    state = tmp_path / "docker-state.json"
    state.write_text(json.dumps(_initial_state()), encoding="utf-8")
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import hashlib
import io
import json
import os
import sys
import tarfile

state_path = os.environ["FAKE_DOCKER_STATE"]

def read_state():
    with open(state_path, encoding="utf-8") as stream:
        return json.load(stream)

def write_state(state):
    temporary = state_path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(state, stream, sort_keys=True)
    os.replace(temporary, state_path)

def config_bytes(meta):
    return json.dumps(
        {
            "architecture": meta["architecture"],
            "config": {"Labels": meta["labels"], "User": meta["user"]},
            "os": meta["os"],
            "rvc_fake_role": meta["role"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

args = sys.argv[1:]
if args[:2] == ["compose", "version"] or (args and args[0] == "compose"):
    raise SystemExit(0)
if args[:2] == ["image", "inspect"]:
    state = read_state()
    template = args[3]
    reference = args[4]
    meta = state["refs"].get(reference)
    if meta is None:
        raise SystemExit(2)
    role = meta["role"]
    config_id = meta["id"]
    image_id = "sha256:" + hashlib.sha256(("oci-index:" + config_id).encode()).hexdigest()
    architecture = meta["architecture"]
    labels = dict(meta["labels"])
    if os.environ.get("FAKE_DOCKER_ARCH_ROLE") == role:
        architecture = "arm64"
    if os.environ.get("FAKE_DOCKER_BAD_LABEL_ROLE") == role:
        labels["org.opencontainers.image.version"] = "wrong"
    user = meta["user"]
    if os.environ.get("FAKE_DOCKER_BAD_USER_ROLE") == role:
        user = "0:0"
    if template == "{{.Id}}":
        print(image_id)
    elif template == "{{.Os}}":
        print(meta["os"])
    elif template == "{{.Architecture}}":
        print(architecture)
    elif template == '{{with index .Config "User"}}{{.}}{{end}}':
        print(user)
    elif template == "{{json .RepoTags}}":
        tags = sorted(
            tag for tag, candidate in state["refs"].items() if candidate["id"] == config_id
        )
        print(json.dumps(tags))
    elif template == "{{json .RepoDigests}}":
        print("[]")
    else:
        value = next((value for key, value in labels.items() if key in template), None)
        if value is None:
            raise SystemExit(2)
        print(value)
    raise SystemExit(0)
if args and args[0] == "tag":
    state = read_state()
    source, target = args[1:3]
    if source not in state["refs"]:
        raise SystemExit(2)
    state["refs"][target] = dict(state["refs"][source])
    write_state(state)
    raise SystemExit(0)
if args and args[0] == "save":
    state = read_state()
    manifest = []
    configs = {}
    for reference in args[1:]:
        meta = state["refs"].get(reference)
        if meta is None:
            raise SystemExit(2)
        config = config_bytes(meta)
        digest = hashlib.sha256(config).hexdigest()
        if meta["id"] != "sha256:" + digest:
            raise SystemExit(2)
        configs[digest + ".json"] = config
        manifest.append(
            {"Config": digest + ".json", "RepoTags": [reference], "Layers": []}
        )
    payload = json.dumps(manifest, separators=(",", ":")).encode()
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        for name, data in [("manifest.json", payload), *sorted(configs.items())]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    raise SystemExit(0)
if args and args[0] == "load":
    if len(args) >= 3 and args[1] == "-i":
        with open(args[2], "rb") as stream:
            payload = stream.read()
    else:
        payload = sys.stdin.buffer.read()
    state = {"refs": {}}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
        for entry in manifest:
            config = json.load(archive.extractfile(entry["Config"]))
            digest = entry["Config"].removesuffix(".json")
            role = config["rvc_fake_role"]
            image_id = "sha256:" + digest
            if os.environ.get("FAKE_DOCKER_POST_LOAD_MISMATCH_ROLE") == role:
                image_id = "sha256:" + "f" * 64
            meta = {
                "architecture": config["architecture"],
                "id": image_id,
                "labels": config["config"]["Labels"],
                "os": config["os"],
                "role": role,
                "user": config["config"].get("User", ""),
            }
            for reference in entry["RepoTags"]:
                state["refs"][reference] = dict(meta)
    write_state(state)
    print("Loaded fake image archive")
    raise SystemExit(0)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    git = fake_bin / "git"
    git.write_text(
        """#!/bin/sh
set -eu
case " $* " in
  *" rev-parse "*) printf '%s\n' "$FAKE_GIT_COMMIT" ;;
  *" status "*) [ "${FAKE_GIT_DIRTY:-0}" = 0 ] || printf ' M dirty\n' ;;
  *" check-ignore "*) cat >/dev/null; exit 1 ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    git.chmod(0o755)
    return fake_bin, state


def _environment(fake_bin: Path, state: Path, **overrides: str) -> dict[str, str]:
    return {
        **os.environ,
        "FAKE_DOCKER_STATE": str(state),
        "FAKE_GIT_COMMIT": COMMIT,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        **overrides,
    }


def _manager_build_command(output: Path, roles: list[str] | None = None) -> list[str]:
    selected = roles if roles is not None else list(MANAGER_SOURCES)
    command = [
        "bash",
        str(MANAGER_BUILDER),
        "--version",
        VERSION,
        "--schema-compatibility",
        "schema-test",
        "--self-contained",
        "--output-dir",
        str(output),
    ]
    for role in selected:
        command.extend(("--include-image", f"{role}={MANAGER_SOURCES[role]}"))
    return command


def _build_manager_bundle(
    tmp_path: Path, *, variant_role: str | None = None
) -> tuple[Path, Path, Path, dict[str, str]]:
    fake_bin, state = _write_fake_commands(tmp_path)
    if variant_role is not None:
        state_document = json.loads(state.read_text(encoding="utf-8"))
        reference = MANAGER_SOURCES[variant_role]
        metadata = state_document["refs"][reference]
        metadata["labels"] = {"fixture.variant": "second"}
        config = _config_bytes(variant_role, metadata["labels"])
        metadata["id"] = f"sha256:{hashlib.sha256(config).hexdigest()}"
        state.write_text(json.dumps(state_document), encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    environment = _environment(fake_bin, state)
    result = subprocess.run(
        _manager_build_command(output),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    archive = output / f"rvc-manager-{VERSION}-linux-amd64.tar.gz"
    extracted = tmp_path / "extracted"
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(extracted, filter="data")
    return extracted / f"rvc-manager-{VERSION}-linux-amd64", fake_bin, state, environment


def _build_partial_bundle(tmp_path: Path, component: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    output = tmp_path / f"{component}-output"
    output.mkdir()
    command = [
        "bash",
        str(ROOT / f"installers/{component}/build-bundle.sh"),
        "--version",
        VERSION,
        "--output-dir",
        str(output),
    ]
    if component == "manager":
        command.extend(("--schema-compatibility", "schema-test"))
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    archive = output / f"rvc-{component}-{VERSION}-linux-amd64.tar.gz"
    extracted = tmp_path / f"{component}-extracted"
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(extracted, filter="data")
    root = extracted / f"rvc-{component}-{VERSION}-linux-amd64"
    if component == "worker":
        (root / "infra/worker/runtime/runtime-activation.json").chmod(0o444)
    return root


def _verify_release_environment(
    root: Path, component: str, environment: Path
) -> subprocess.CompletedProcess[str]:
    release_manifest = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in (root / "manifest.env").read_text(encoding="utf-8").splitlines()
        if "=" in line
    }
    return subprocess.run(
        [
            "python3",
            str(VERIFIER),
            "verify-environment",
            "--root",
            str(root),
            "--component",
            component,
            "--version",
            VERSION,
            "--source-commit",
            release_manifest["GIT_COMMIT"],
            "--environment",
            str(environment),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_manager_self_contained_bundle_has_exact_versioned_closure(tmp_path: Path) -> None:
    bundle, _, _, _ = _build_manager_bundle(tmp_path)
    manifest = json.loads((bundle / "images-manifest.json").read_text(encoding="utf-8"))
    assert manifest["format_version"] == 2
    assert manifest["component"] == "manager"
    assert manifest["self_contained"] is True
    assert {image["role"] for image in manifest["images"]} == set(MANAGER_SOURCES)
    assert {image["reference"] for image in manifest["images"]} == set(MANAGER_RUNTIME.values())
    for image in manifest["images"]:
        assert image["source_reference"] == MANAGER_SOURCES[image["role"]]
        assert image["reference"] == MANAGER_RUNTIME[image["role"]]
        assert image["user"] == MANAGER_USERS[image["role"]]
        assert image["image_id"] != image["config_digest"]
        if image["role"] in {"api", "web", "mlflow"}:
            assert image["release_labels"] == {
                "org.opencontainers.image.revision": COMMIT,
                "org.opencontainers.image.version": VERSION,
            }
        else:
            assert image["release_labels"] == {}
    checksums = (bundle / "SHA256SUMS").read_text(encoding="utf-8")
    assert "images-manifest.json" in checksums
    assert "images/manager-images.tar.gz" in checksums
    bundle_manifest = (bundle / "manifest.env").read_text(encoding="utf-8")
    assert "BUNDLE_FORMAT_VERSION=2" in bundle_manifest
    assert "SELF_CONTAINED=true" in bundle_manifest
    assert f"POSTGRES_IMAGE=rvc-orchestrator-postgres:{VERSION}" in bundle_manifest


@pytest.mark.parametrize(
    ("roles", "environment_override", "expected"),
    [
        (list(MANAGER_SOURCES)[:-1], {}, "missing=nginx"),
        (list(MANAGER_SOURCES), {"FAKE_DOCKER_ARCH_ROLE": "redis"}, "linux/amd64"),
        (
            list(MANAGER_SOURCES),
            {"FAKE_DOCKER_BAD_LABEL_ROLE": "api"},
            "release label mismatch",
        ),
        (
            list(MANAGER_SOURCES),
            {"FAKE_DOCKER_BAD_USER_ROLE": "api"},
            "image user mismatch",
        ),
    ],
)
def test_manager_self_contained_builder_rejects_incomplete_or_mismatched_images(
    tmp_path: Path,
    roles: list[str],
    environment_override: dict[str, str],
    expected: str,
) -> None:
    fake_bin, state = _write_fake_commands(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    result = subprocess.run(
        _manager_build_command(output, roles),
        check=False,
        capture_output=True,
        text=True,
        env=_environment(fake_bin, state, **environment_override),
    )
    assert result.returncode != 0
    assert expected in result.stderr
    assert not list(output.glob("*.tar.gz"))


def test_self_contained_builder_rejects_duplicate_roles_and_dirty_source(
    tmp_path: Path,
) -> None:
    fake_bin, state = _write_fake_commands(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    duplicate = _manager_build_command(output)
    duplicate.extend(("--include-image", f"api={MANAGER_SOURCES['api']}"))
    duplicate_result = subprocess.run(
        duplicate,
        check=False,
        capture_output=True,
        text=True,
        env=_environment(fake_bin, state),
    )
    assert duplicate_result.returncode != 0
    assert "duplicate image role" in duplicate_result.stderr

    dirty_result = subprocess.run(
        _manager_build_command(output),
        check=False,
        capture_output=True,
        text=True,
        env=_environment(fake_bin, state, FAKE_GIT_DIRTY="1"),
    )
    assert dirty_result.returncode != 0
    assert "clean source tree" in dirty_result.stderr


def test_self_contained_builder_rejects_extra_role_and_wrong_source_tag(
    tmp_path: Path,
) -> None:
    fake_bin, state = _write_fake_commands(tmp_path)
    state_document = json.loads(state.read_text(encoding="utf-8"))
    labels: dict[str, str] = {}
    config = _config_bytes("extra", labels)
    state_document["refs"]["busybox:latest"] = {
        "architecture": "amd64",
        "id": f"sha256:{hashlib.sha256(config).hexdigest()}",
        "labels": labels,
        "os": "linux",
        "role": "extra",
        "user": "",
    }
    wrong_api = "rvc-orchestrator-api:wrong"
    state_document["refs"][wrong_api] = dict(state_document["refs"][MANAGER_SOURCES["api"]])
    state.write_text(json.dumps(state_document), encoding="utf-8")

    extra_output = tmp_path / "extra-output"
    extra_output.mkdir()
    extra_command = _manager_build_command(extra_output)
    extra_command.extend(("--include-image", "extra=busybox:latest"))
    extra = subprocess.run(
        extra_command,
        check=False,
        capture_output=True,
        text=True,
        env=_environment(fake_bin, state),
    )
    assert extra.returncode != 0
    assert "extra=extra" in extra.stderr

    wrong_output = tmp_path / "wrong-output"
    wrong_output.mkdir()
    wrong_command = _manager_build_command(wrong_output)
    api_argument = f"api={MANAGER_SOURCES['api']}"
    wrong_command[wrong_command.index(api_argument)] = f"api={wrong_api}"
    wrong = subprocess.run(
        wrong_command,
        check=False,
        capture_output=True,
        text=True,
        env=_environment(fake_bin, state),
    )
    assert wrong.returncode != 0
    assert "source tag/reference mismatch for role api" in wrong.stderr


def _rewrite_image_archive(
    archive_path: Path,
    mutate: Callable[[list[dict[str, Any]], dict[str, bytes]], None],
) -> None:
    files: dict[str, bytes] = {}
    with tarfile.open(archive_path, "r:gz") as source:
        for member in source:
            if member.isfile():
                stream = source.extractfile(member)
                assert stream is not None
                files[member.name] = stream.read()
    manifest = json.loads(files.pop("manifest.json"))
    mutate(manifest, files)
    files["manifest.json"] = json.dumps(manifest, separators=(",", ":")).encode()
    with tarfile.open(archive_path, "w:gz") as destination:
        for name, payload in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            destination.addfile(info, io.BytesIO(payload))


def _refresh_archive_record(bundle: Path) -> None:
    archive = bundle / "images/manager-images.tar.gz"
    manifest_path = bundle / "images-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["archives"][0]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest["archives"][0]["size"] = archive.stat().st_size
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _refresh_bundle_checksums(bundle: Path) -> None:
    lines = []
    for path in sorted(bundle.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.relative_to(bundle)}")
    (bundle / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize("mutation", ("extra-tag", "extra-config", "wrong-config", "unused-config"))
def test_preload_rejects_extra_tags_configs_and_config_digest_mismatch(
    tmp_path: Path, mutation: str
) -> None:
    bundle, _, _, _ = _build_manager_bundle(tmp_path)
    archive = bundle / "images/manager-images.tar.gz"

    def mutate(manifest: list[dict[str, Any]], files: dict[str, bytes]) -> None:
        if mutation == "extra-tag":
            manifest[0]["RepoTags"].append("unexpected:image")
        elif mutation == "extra-config":
            payload = b"{}"
            digest = hashlib.sha256(payload).hexdigest()
            files[f"{digest}.json"] = payload
            manifest.append(
                {
                    "Config": f"{digest}.json",
                    "Layers": [],
                    "RepoTags": ["unexpected:image"],
                }
            )
        elif mutation == "wrong-config":
            old_config = manifest[0]["Config"]
            payload = files.pop(old_config)
            digest = "e" * 64
            files[f"{digest}.json"] = payload
            manifest[0]["Config"] = f"{digest}.json"
        else:
            files[f"{'d' * 64}.json"] = b"unused"

    _rewrite_image_archive(archive, mutate)
    _refresh_archive_record(bundle)
    result = subprocess.run(
        [
            "python3",
            str(VERIFIER),
            "verify-bundle",
            "--root",
            str(bundle),
            "--component",
            "manager",
            "--version",
            VERSION,
            "--source-commit",
            COMMIT,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Docker image archive" in result.stderr


def test_preload_rejects_config_bytes_that_do_not_match_digest_name(tmp_path: Path) -> None:
    bundle, _, _, _ = _build_manager_bundle(tmp_path)
    archive = bundle / "images/manager-images.tar.gz"

    def mutate(manifest: list[dict[str, Any]], files: dict[str, bytes]) -> None:
        config_name = manifest[0]["Config"]
        config = json.loads(files[config_name])
        config["config"]["User"] = "0:0"
        files[config_name] = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()

    _rewrite_image_archive(archive, mutate)
    _refresh_archive_record(bundle)
    result = subprocess.run(
        [
            "python3",
            str(VERIFIER),
            "verify-bundle",
            "--root",
            str(bundle),
            "--component",
            "manager",
            "--version",
            VERSION,
            "--source-commit",
            COMMIT,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Config content digest differs" in result.stderr


def test_manifest_parser_rejects_duplicate_keys_traversal_and_extra_archive(
    tmp_path: Path,
) -> None:
    bundle, _, _, _ = _build_manager_bundle(tmp_path)
    pristine = bundle / "images-manifest.json"
    original = pristine.read_text(encoding="utf-8")
    cases: list[tuple[str, str]] = [
        (original.replace('"archives":', '"archives": [], "archives":', 1), "duplicate JSON key"),
        (original.replace("images/manager-images.tar.gz", "../manager-images.tar.gz"), "unsafe"),
    ]
    for content, expected in cases:
        pristine.write_text(content, encoding="utf-8")
        result = subprocess.run(
            [
                "python3",
                str(VERIFIER),
                "verify-bundle",
                "--root",
                str(bundle),
                "--component",
                "manager",
                "--version",
                VERSION,
                "--source-commit",
                COMMIT,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert expected in result.stderr
        pristine.write_text(original, encoding="utf-8")
    (bundle / "images/extra.tar").write_bytes(b"extra")
    extra = subprocess.run(
        [
            "python3",
            str(VERIFIER),
            "verify-bundle",
            "--root",
            str(bundle),
            "--component",
            "manager",
            "--version",
            VERSION,
            "--source-commit",
            COMMIT,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert extra.returncode != 0
    assert "extra=images/extra.tar" in extra.stderr


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("extra", "checksum inventory differs"),
        ("duplicate", "duplicate path"),
        ("symlink", "non-regular entry"),
        ("writeable-ledger", "must be read-only"),
    ),
)
def test_release_checksum_ledger_is_exact_and_read_only(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    release = tmp_path / mutation
    release.mkdir()
    payload = release / "payload.txt"
    payload.write_text("release payload\n", encoding="utf-8")
    created = subprocess.run(
        [
            "python3",
            str(VERIFIER),
            "create-ledger",
            "--root",
            str(release),
            "--ledger-name",
            "RELEASE_SHA256SUMS",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    ledger = release / "RELEASE_SHA256SUMS"
    assert ledger.stat().st_mode & 0o777 == 0o444

    if mutation == "extra":
        (release / "unlisted.txt").write_text("not in ledger\n", encoding="utf-8")
    elif mutation == "duplicate":
        original = ledger.read_text(encoding="utf-8")
        ledger.chmod(0o644)
        ledger.write_text(original + original, encoding="utf-8")
        ledger.chmod(0o444)
    elif mutation == "symlink":
        (release / "payload-link").symlink_to("payload.txt")
    else:
        ledger.chmod(0o644)

    verified = subprocess.run(
        [
            "python3",
            str(VERIFIER),
            "verify-ledger",
            "--root",
            str(release),
            "--ledger-name",
            "RELEASE_SHA256SUMS",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert verified.returncode != 0
    assert expected in verified.stderr


def test_bundle_checksum_verifier_rejects_unlisted_regular_file(tmp_path: Path) -> None:
    bundle = _build_partial_bundle(tmp_path, "manager")
    (bundle / "infra/compose/unlisted.txt").write_text("unexpected\n", encoding="utf-8")
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; rvc_verify_bundle_checksums "$2" 0',
            "bash",
            str(bundle / "common/lib.sh"),
            str(bundle),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "extra=infra/compose/unlisted.txt" in result.stderr


@pytest.mark.parametrize("remove_manifest", (False, True))
def test_bundle_checksum_verifier_rejects_missing_ledger_even_without_manifest(
    tmp_path: Path, remove_manifest: bool
) -> None:
    bundle = _build_partial_bundle(tmp_path, "manager")
    (bundle / "SHA256SUMS").unlink()
    if remove_manifest:
        (bundle / "manifest.env").unlink()
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; rvc_verify_bundle_checksums "$2" 0',
            "bash",
            str(bundle / "common/lib.sh"),
            str(bundle),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "only an exact Git source root may omit SHA256SUMS" in result.stderr


def test_bundle_checksum_verifier_allows_only_actual_git_source_root() -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; rvc_verify_bundle_checksums "$2" 1',
            "bash",
            str(ROOT / "installers/common/lib.sh"),
            str(ROOT),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "exact Git source root" in result.stderr


def test_partial_environment_is_bound_to_release_owned_provenance(tmp_path: Path) -> None:
    manager = _build_partial_bundle(tmp_path / "manager", "manager")
    manager_manifest = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in (manager / "manifest.env").read_text(encoding="utf-8").splitlines()
        if "=" in line
    }
    manager_environment = tmp_path / "manager.env"
    manager_keys = (
        "API_IMAGE",
        "WEB_IMAGE",
        "MLFLOW_IMAGE",
        "POSTGRES_IMAGE",
        "REDIS_IMAGE",
        "MINIO_IMAGE",
        "MINIO_CLIENT_IMAGE",
        "NGINX_IMAGE",
    )
    manager_environment.write_text(
        "\n".join(
            [
                f"ORCHESTRATOR_VERSION={VERSION}",
                "RVC_IMAGE_PULL_POLICY=missing",
                *(f"{key}={manager_manifest[key]}" for key in manager_keys),
                "CUSTOM_SETTING=preserved",
                "",
            ]
        ),
        encoding="utf-8",
    )
    valid_manager = _verify_release_environment(manager, "manager", manager_environment)
    assert valid_manager.returncode == 0, valid_manager.stdout + valid_manager.stderr
    manager_environment.write_text(
        manager_environment.read_text(encoding="utf-8").replace(
            manager_manifest["API_IMAGE"], "attacker.example/api:evil"
        ),
        encoding="utf-8",
    )
    invalid_manager = _verify_release_environment(manager, "manager", manager_environment)
    assert invalid_manager.returncode != 0
    assert "image reference differs for role api" in invalid_manager.stderr

    worker = _build_partial_bundle(tmp_path / "worker", "worker")
    worker_manifest = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in (worker / "manifest.env").read_text(encoding="utf-8").splitlines()
        if "=" in line
    }
    worker_keys = (
        "WORKER_IMAGE",
        "RVC_RUNTIME_INCLUDED",
        "RVC_NATIVE_RUNNER_AVAILABLE",
        "RVC_RUNTIME_IMAGE",
        "RVC_SOURCE_COMMIT",
        "RVC_BASE_IMAGE",
        "RVC_FAIRSEQ_COMMIT",
        "RVC_SOURCE_MANIFEST_SHA256",
        "RVC_WHEELHOUSE_MANIFEST_SHA256",
        "RVC_ASSET_MANIFEST_SHA256",
        "RVC_PROJECTION_MANIFEST_SHA256",
        "RVC_GPU_SMOKE_VERIFIED",
        "RVC_PROFILE_STAGE_SET_VERIFIED",
        "RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED",
    )
    worker_environment = tmp_path / "worker.env"
    worker_environment.write_text(
        "\n".join(
            [
                f"ORCHESTRATOR_VERSION={VERSION}",
                "RVC_IMAGE_PULL_POLICY=missing",
                *(f"{key}={worker_manifest[key]}" for key in worker_keys),
                "RVC_NATIVE_UNVERIFIED_GPU_ACKNOWLEDGED=true",
                "CUSTOM_SETTING=preserved",
                "",
            ]
        ),
        encoding="utf-8",
    )
    valid_worker = _verify_release_environment(worker, "worker", worker_environment)
    assert valid_worker.returncode == 0, valid_worker.stdout + valid_worker.stderr
    worker_environment.write_text(
        worker_environment.read_text(encoding="utf-8").replace(
            "RVC_GPU_SMOKE_VERIFIED=false", "RVC_GPU_SMOKE_VERIFIED=true"
        ),
        encoding="utf-8",
    )
    invalid_worker = _verify_release_environment(worker, "worker", worker_environment)
    assert invalid_worker.returncode != 0
    assert "provenance differs for RVC_GPU_SMOKE_VERIFIED" in invalid_worker.stderr


def _install_manager(
    bundle: Path,
    fake_bin: Path,
    state: Path,
    root: Path,
    **environment_overrides: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(bundle / "install.sh"),
            "--install-root",
            str(root / "install"),
            "--config-root",
            str(root / "config"),
            "--systemd-dir",
            str(root / "systemd"),
            "--allow-unsupported-os",
            "--skip-daemon-check",
            "--no-start",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_environment(
            fake_bin,
            state,
            RVC_INSTALL_ALLOW_NON_ROOT="1",
            RVC_MANAGER_MINIMUM_DISK_GB="0",
            **environment_overrides,
        ),
    )


def test_install_verifies_loaded_identity_before_activation_and_pins_no_pull(
    tmp_path: Path,
) -> None:
    bundle, fake_bin, state, _ = _build_manager_bundle(tmp_path)
    state.write_text('{"refs": {}}', encoding="utf-8")
    failed_root = tmp_path / "failed-install"
    failed = _install_manager(
        bundle,
        fake_bin,
        state,
        failed_root,
        FAKE_DOCKER_POST_LOAD_MISMATCH_ROLE="api",
    )
    assert failed.returncode != 0
    assert "loaded container image" in failed.stderr
    assert not (failed_root / "install").exists()
    assert not (failed_root / "config").exists()

    state.write_text('{"refs": {}}', encoding="utf-8")
    installed_root = tmp_path / "successful-install"
    installed = _install_manager(bundle, fake_bin, state, installed_root)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    environment = (installed_root / "config/manager.env").read_text(encoding="utf-8")
    assert "RVC_IMAGE_PULL_POLICY=never" in environment
    for role in ("postgres", "redis", "minio", "minio-client", "nginx"):
        key = role.upper().replace("-", "_") + "_IMAGE"
        if role == "minio-client":
            key = "MINIO_CLIENT_IMAGE"
        assert f"{key}={MANAGER_RUNTIME[role]}" in environment
    release = installed_root / f"install/releases/{VERSION}"
    assert (release / "images-manifest.json").is_file()
    release_ledger = release / "RELEASE_SHA256SUMS"
    assert release_ledger.is_file()
    assert release_ledger.stat().st_mode & 0o777 == 0o444
    assert (installed_root / "install/lib/image_bundle.py").is_file()

    conflicting_parent = tmp_path / "conflicting-build"
    conflicting_parent.mkdir()
    conflicting_bundle, _, _, _ = _build_manager_bundle(conflicting_parent, variant_role="nginx")
    (conflicting_bundle / "manifest.env").write_bytes((release / "manifest.env").read_bytes())
    _refresh_bundle_checksums(conflicting_bundle)
    state_before_conflict = state.read_text(encoding="utf-8")
    conflict = _install_manager(
        conflicting_bundle,
        fake_bin,
        state,
        installed_root,
    )
    assert conflict.returncode != 0
    assert "existing release image provenance differs" in conflict.stderr
    assert state.read_text(encoding="utf-8") == state_before_conflict

    env_path = installed_root / "config/manager.env"
    env_path.write_text(
        env_path.read_text(encoding="utf-8")
        .replace("RVC_IMAGE_PULL_POLICY=never", "RVC_IMAGE_PULL_POLICY=missing")
        .replace("PUBLIC_SCHEME=http", "PUBLIC_SCHEME=https"),
        encoding="utf-8",
    )
    start = subprocess.run(
        [str(installed_root / "install/bin/manager-compose"), "up", "-d"],
        check=False,
        capture_output=True,
        text=True,
        env=_environment(
            fake_bin,
            state,
            RVC_INSTALL_ROOT=str(installed_root / "install"),
            RVC_CONFIG_ROOT=str(installed_root / "config"),
        ),
    )
    assert start.returncode != 0
    assert "pull policy differs" in start.stderr

    (release / "unlisted-after-install.txt").write_text("tamper\n", encoding="utf-8")
    integrity_start = subprocess.run(
        [str(installed_root / "install/bin/manager-compose"), "up", "-d"],
        check=False,
        capture_output=True,
        text=True,
        env=_environment(
            fake_bin,
            state,
            RVC_INSTALL_ROOT=str(installed_root / "install"),
            RVC_CONFIG_ROOT=str(installed_root / "config"),
        ),
    )
    assert integrity_start.returncode != 0
    assert "checksum inventory differs" in integrity_start.stderr


def test_worker_self_contained_mode_requires_exact_verified_runtime(tmp_path: Path) -> None:
    fake_bin, state = _write_fake_commands(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "installers/worker/build-bundle.sh"),
            "--version",
            VERSION,
            "--self-contained",
            "--output-dir",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_environment(fake_bin, state),
    )
    assert result.returncode != 0
    assert "verified runtime image" in result.stderr


def test_unknown_or_symlink_bundle_manifest_fails_closed(tmp_path: Path) -> None:
    library = ROOT / "installers/common/lib.sh"
    manifest = tmp_path / "manifest.env"
    manifest.write_text("BUNDLE_FORMAT_VERSION=99\n", encoding="utf-8")
    command = 'source "$1"; rvc_validate_supply_chain_files "$2"'
    unknown = subprocess.run(
        ["bash", "-c", command, "bash", str(library), str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert unknown.returncode != 0
    assert "unsupported bundle manifest format" in unknown.stderr
    manifest.unlink()
    manifest.symlink_to(tmp_path / "missing.env")
    unsafe = subprocess.run(
        ["bash", "-c", command, "bash", str(library), str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert unsafe.returncode != 0
    assert "non-symlink" in unsafe.stderr


def test_start_and_rollback_reverify_installed_image_identity() -> None:
    manager_compose = (ROOT / "installers/manager/compose.sh").read_text(encoding="utf-8")
    worker_compose = (ROOT / "installers/worker/compose.sh").read_text(encoding="utf-8")
    rollback = (ROOT / "installers/manager/rollback.sh").read_text(encoding="utf-8")
    for wrapper in (manager_compose, worker_compose):
        assert "verify-ledger" in wrapper
        assert "verify-environment" in wrapper
        assert "verify-loaded" in wrapper
        assert "up|start|restart|run|create" in wrapper
    assert 'verify_release_image_identity "$release" "$version"' in rollback
    assert 'RVC_IMAGE_PULL_POLICY="$RELEASE_PULL_POLICY"' in rollback
    transition = rollback.index('rvc_log "current release switched')
    persist = rollback.index(
        'persist_release_environment "$target_release" "$target_version"', transition
    )
    start = rollback.index('if start_release "$target_release" "$target_version"', transition)
    assert persist < start


def test_preflight_declares_image_verifier_runtime_dependencies() -> None:
    for component in ("manager", "worker"):
        preflight = (ROOT / f"installers/{component}/preflight.sh").read_text(encoding="utf-8")
        assert "rvc_require_command python3" in preflight
        assert "rvc_require_command gzip" in preflight


def test_partial_bundles_remain_explicitly_non_self_contained(tmp_path: Path) -> None:
    for component in ("manager", "worker"):
        output = tmp_path / component
        output.mkdir()
        command = [
            "bash",
            str(ROOT / f"installers/{component}/build-bundle.sh"),
            "--version",
            VERSION,
            "--output-dir",
            str(output),
        ]
        if component == "manager":
            command.extend(("--schema-compatibility", "schema-test"))
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        archive = output / f"rvc-{component}-{VERSION}-linux-amd64.tar.gz"
        with tarfile.open(archive, "r:gz") as bundle:
            prefix = f"rvc-{component}-{VERSION}-linux-amd64"
            readme = bundle.extractfile(f"{prefix}/README.md")
            testing = bundle.extractfile(f"{prefix}/TESTING.md")
            result_template = bundle.extractfile(f"{prefix}/TEST_RESULT_TEMPLATE.md")
            assert readme is not None
            assert testing is not None
            assert result_template is not None
            readme_text = readme.read().decode("utf-8")
            testing_text = testing.read().decode("utf-8")
            result_template_text = result_template.read().decode("utf-8")
            assert VERSION in readme_text
            assert "{{VERSION}}" not in readme_text
            assert f"{component} bundle {VERSION}" in testing_text
            assert "{{COMPONENT}}" not in testing_text
            assert "sha256sum -c SHA256SUMS" in testing_text
            assert "verify-ledger --root . --ledger-name SHA256SUMS" in testing_text
            assert "image_bundle.py verify-bundle" in testing_text
            for marker in ("PASS", "FAIL", "BLOCKED"):
                assert marker in result_template_text
            manifest_file = bundle.extractfile(f"{prefix}/images-manifest.json")
            assert manifest_file is not None
            manifest = json.load(manifest_file)
            if component == "worker":
                activation_name = (
                    f"rvc-worker-{VERSION}-linux-amd64/infra/worker/runtime/runtime-activation.json"
                )
                activation_member = bundle.getmember(activation_name)
                activation_file = bundle.extractfile(activation_member)
                assert activation_file is not None
                activation = json.load(activation_file)
                assert activation_member.mode & 0o222 == 0
                assert activation == {
                    "format_version": 1,
                    "gpu_smoke_verified": False,
                    "kind": "rvc-runtime-activation",
                    "native_sample_inference_verified": False,
                    "profile_stage_set_verified": False,
                    "qualification_evidence_sha256": None,
                    "runtime_asset_manifest_sha256": None,
                    "runtime_image_digest": None,
                    "supported_inference_f0_methods": [],
                }
        assert manifest["self_contained"] is False
        assert manifest["archives"] == []
        assert manifest["images"] == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("writeable", "must be read-only"),
        ("unknown-field", "keys differ"),
        ("partial-gate", "partially enabled"),
        ("digest-in-disabled", "must not carry runtime digests"),
        ("manifest-mismatch", "disagree on RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED"),
    ),
)
def test_worker_image_verifier_rejects_forged_runtime_activation(
    tmp_path: Path,
    mutation: str,
    expected: str,
) -> None:
    output = tmp_path / "bundle"
    output.mkdir()
    built = subprocess.run(
        [
            "bash",
            str(ROOT / "installers/worker/build-bundle.sh"),
            "--version",
            VERSION,
            "--output-dir",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    archive = output / f"rvc-worker-{VERSION}-linux-amd64.tar.gz"
    extracted = tmp_path / "extracted"
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(extracted, filter="data")
    root = extracted / f"rvc-worker-{VERSION}-linux-amd64"
    activation_path = root / "infra/worker/runtime/runtime-activation.json"
    manifest_path = root / "manifest.env"

    if mutation == "manifest-mismatch":
        activation_path.chmod(0o444)
        manifest_path.write_text(
            manifest_path.read_text(encoding="utf-8").replace(
                "RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=false",
                "RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=true",
            ),
            encoding="utf-8",
        )
    else:
        activation_path.chmod(0o644)
        activation = json.loads(activation_path.read_text(encoding="utf-8"))
        if mutation == "unknown-field":
            activation["operator_override"] = True
        elif mutation == "partial-gate":
            activation["gpu_smoke_verified"] = True
        elif mutation == "digest-in-disabled":
            activation["runtime_image_digest"] = "sha256:" + "2" * 64
        activation_path.write_text(
            json.dumps(activation, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if mutation != "writeable":
            activation_path.chmod(0o444)

    release_environment = root / "worker.env"
    release_environment.write_text("RVC_IMAGE_PULL_POLICY=missing\n", encoding="utf-8")
    verified = subprocess.run(
        [
            "python3",
            str(VERIFIER),
            "verify-environment",
            "--root",
            str(root),
            "--component",
            "worker",
            "--version",
            VERSION,
            "--source-commit",
            "uncommitted",
            "--environment",
            str(release_environment),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert verified.returncode != 0
    assert expected in verified.stderr
