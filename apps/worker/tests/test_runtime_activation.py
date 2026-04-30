from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from rvc_worker.runtime_activation import (
    QualifiedNativeSampleRuntime,
    RuntimeActivationError,
    load_runtime_activation,
)

_IMAGE_DIGEST = "sha256:" + "a" * 64
_EVIDENCE_DIGEST = "b" * 64


def test_missing_and_exact_disabled_activation_remain_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "reviewed-rvc"
    source.mkdir()

    assert (
        load_runtime_activation(
            tmp_path / "missing.json",
            native_source_root=source,
        )
        is None
    )

    activation = tmp_path / "runtime-activation.json"
    _write_read_only(activation, _disabled_document())
    assert load_runtime_activation(activation, native_source_root=source) is None


def test_fully_qualified_activation_is_bound_to_actual_asset_manifest(
    tmp_path: Path,
) -> None:
    source, asset_sha256 = _source_fixture(tmp_path)
    activation = tmp_path / "runtime-activation.json"
    _write_read_only(activation, _qualified_document(asset_sha256))

    evidence = load_runtime_activation(activation, native_source_root=source)

    assert evidence == QualifiedNativeSampleRuntime(
        runtime_image_digest=_IMAGE_DIGEST,
        runtime_asset_manifest_sha256=asset_sha256,
        qualification_evidence_sha256=_EVIDENCE_DIGEST,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"gpu_smoke_verified": False},
        {"profile_stage_set_verified": False},
        {"native_sample_inference_verified": False},
        {"runtime_image_digest": None},
        {"runtime_image_digest": "worker:latest"},
        {"runtime_asset_manifest_sha256": None},
        {"qualification_evidence_sha256": "not-a-sha256"},
        {"supported_inference_f0_methods": ["pm", "harvest", "rmvpe", "crepe"]},
        {"supported_inference_f0_methods": []},
    ],
)
def test_partial_or_non_exact_activation_is_rejected(
    tmp_path: Path,
    mutation: dict[str, object],
) -> None:
    source, asset_sha256 = _source_fixture(tmp_path)
    document = _qualified_document(asset_sha256)
    document.update(mutation)
    activation = tmp_path / "runtime-activation.json"
    _write_read_only(activation, document)

    with pytest.raises(RuntimeActivationError, match="fully qualified"):
        load_runtime_activation(activation, native_source_root=source)


def test_activation_requires_exact_keys_and_rejects_duplicate_json_keys(
    tmp_path: Path,
) -> None:
    source, asset_sha256 = _source_fixture(tmp_path)
    activation = tmp_path / "runtime-activation.json"
    document = _qualified_document(asset_sha256)
    document["unexpected"] = True
    _write_read_only(activation, document)
    with pytest.raises(RuntimeActivationError, match="fields"):
        load_runtime_activation(activation, native_source_root=source)

    duplicate = json.dumps(_qualified_document(asset_sha256)).replace(
        '"format_version": 1',
        '"format_version": 1, "format_version": 1',
        1,
    )
    _write_read_only_bytes(activation, duplicate.encode("utf-8"))
    with pytest.raises(RuntimeActivationError, match="duplicate"):
        load_runtime_activation(activation, native_source_root=source)


def test_activation_rejects_invalid_utf8_size_nonregular_and_writable_files(
    tmp_path: Path,
) -> None:
    source, asset_sha256 = _source_fixture(tmp_path)
    activation = tmp_path / "runtime-activation.json"

    _write_read_only_bytes(activation, b"\xff")
    with pytest.raises(RuntimeActivationError, match="UTF-8"):
        load_runtime_activation(activation, native_source_root=source)

    _write_read_only_bytes(activation, b"{" + b" " * (64 * 1024))
    with pytest.raises(RuntimeActivationError, match="metadata"):
        load_runtime_activation(activation, native_source_root=source)

    activation.chmod(0o644)
    activation.write_text(json.dumps(_qualified_document(asset_sha256)), encoding="utf-8")
    with pytest.raises(RuntimeActivationError, match="read-only"):
        load_runtime_activation(activation, native_source_root=source)

    activation.unlink()
    activation.mkdir()
    with pytest.raises(RuntimeActivationError, match="metadata"):
        load_runtime_activation(activation, native_source_root=source)


def test_activation_rejects_symlink_and_asset_manifest_mismatch(tmp_path: Path) -> None:
    source, asset_sha256 = _source_fixture(tmp_path)
    target = tmp_path / "target.json"
    _write_read_only(target, _qualified_document(asset_sha256))
    activation = tmp_path / "runtime-activation.json"
    activation.symlink_to(target)

    with pytest.raises(RuntimeActivationError, match="opened safely"):
        load_runtime_activation(activation, native_source_root=source)

    activation.unlink()
    mismatch = _qualified_document("c" * 64)
    _write_read_only(activation, mismatch)
    with pytest.raises(RuntimeActivationError, match="does not match"):
        load_runtime_activation(activation, native_source_root=source)


def _source_fixture(root: Path) -> tuple[Path, str]:
    source = root / "reviewed-rvc"
    source.mkdir(exist_ok=True)
    asset_manifest = source / "assets-manifest.json"
    asset_manifest.write_text('{"fixture":true}\n', encoding="utf-8")
    return source, hashlib.sha256(asset_manifest.read_bytes()).hexdigest()


def _disabled_document() -> dict[str, object]:
    return {
        "format_version": 1,
        "kind": "rvc-runtime-activation",
        "runtime_image_digest": None,
        "runtime_asset_manifest_sha256": None,
        "qualification_evidence_sha256": None,
        "gpu_smoke_verified": False,
        "profile_stage_set_verified": False,
        "native_sample_inference_verified": False,
        "supported_inference_f0_methods": [],
    }


def _qualified_document(asset_sha256: str) -> dict[str, object]:
    return {
        "format_version": 1,
        "kind": "rvc-runtime-activation",
        "runtime_image_digest": _IMAGE_DIGEST,
        "runtime_asset_manifest_sha256": asset_sha256,
        "qualification_evidence_sha256": _EVIDENCE_DIGEST,
        "gpu_smoke_verified": True,
        "profile_stage_set_verified": True,
        "native_sample_inference_verified": True,
        "supported_inference_f0_methods": ["pm", "harvest", "crepe", "rmvpe"],
    }


def _write_read_only(path: Path, document: dict[str, object]) -> None:
    _write_read_only_bytes(path, json.dumps(document, sort_keys=True).encode("utf-8"))


def _write_read_only_bytes(path: Path, content: bytes) -> None:
    if path.exists():
        path.chmod(0o644)
    path.write_bytes(content)
    path.chmod(0o444)
