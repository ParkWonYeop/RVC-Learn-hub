"""Isolated reviewed RVC Pipeline driver for fixed-TestSet sample inference.

Heavy runtime modules are imported only from ``main`` so Worker control-plane
imports and dependency-free unit tests never require torch, faiss, numpy, or RVC.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import stat
import sys
import wave
from array import array
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4


class NativeInferenceDriverError(RuntimeError):
    """The isolated driver rejected an input or upstream runtime result."""


@dataclass(frozen=True, slots=True)
class _FileEvidence:
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _OpenedFile:
    descriptor: int
    metadata: os.stat_result


@dataclass(frozen=True, slots=True)
class _TorchLoadPolicy:
    evidence: _FileEvidence
    weights_only: bool
    required_mode: int | None
    map_location: str | None = None


@dataclass(slots=True)
class _FaissUsage:
    reads: int = 0
    reconstructs: int = 0
    searches: int = 0


class _VerifiedFaissIndex:
    def __init__(self, inner: Any, reconstructed: Any, usage: _FaissUsage) -> None:
        self._inner = inner
        self._reconstructed = reconstructed
        self._usage = usage
        self.d = int(inner.d)
        self.ntotal = int(inner.ntotal)
        self.is_trained = bool(inner.is_trained)

    def reconstruct_n(self, start: int, count: int) -> Any:
        if start != 0 or count != self.ntotal:
            raise NativeInferenceDriverError("FAISS reconstruction request is invalid")
        self._usage.reconstructs += 1
        return self._reconstructed

    def search(self, *args: Any, **kwargs: Any) -> Any:
        result = self._inner.search(*args, **kwargs)
        self._usage.searches += 1
        return result


_REVIEWED_COMMIT = "7ef19867780cf703841ebafb565a4e47d1ea86ff"
_F0_METHODS = frozenset({"pm", "harvest", "crepe", "rmvpe"})
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PCM_METRICS_ALGORITHM = "pcm-normalized-v2"
_CLIPPING_THRESHOLD = 0.999
_SILENCE_THRESHOLD = 0.0001
_MAX_REQUEST_BYTES = 16 * 1024**2
_MAX_PROJECTION_MARKER_BYTES = 64 * 1024**2
_MAX_MODEL_STATE_KEYS = 100_000
_MAX_MODEL_TENSOR_ELEMENTS = 2_000_000_000
_MAX_TOTAL_OUTPUT_BYTES = 2 * 1024**3
_MAX_TOTAL_OUTPUT_DURATION_SECONDS = 3_600.0
_CREPE_MODEL_RELATIVE_PATH = "runtime/crepe/full.pth"
_CREPE_MODEL_CAPACITY = "full"
_PROJECTION_BASE_DIRECTORIES = (
    "infer",
    "configs",
    "assets/pretrained",
    "assets/pretrained_v2",
    "assets/hubert",
    "assets/rmvpe",
    "logs/mute",
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--request-sha256", required=True)
    try:
        arguments = parser.parse_args(argv)
        request_path = _absolute_normalized_path(arguments.request, "request")
        result_path = _absolute_normalized_path(arguments.result, "result")
        if not _is_sha256(arguments.request_sha256):
            raise NativeInferenceDriverError("request hash argument is invalid")
        request = _load_json(
            request_path,
            _MAX_REQUEST_BYTES,
            required_mode=0o600,
            expected_sha256=arguments.request_sha256,
        )
        _validate_request_shape(request)

        # These imports must remain inside main. The Worker package is intentionally
        # usable without the native GPU runtime installed.
        import faiss  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
        import torch  # type: ignore[import-not-found]
        from scipy import signal  # type: ignore[import-untyped]

        torchcrepe = None
        if _mapping(request["inference"])["inference_f0_method"] == "crepe":
            import torchcrepe as loaded_torchcrepe  # type: ignore[import-not-found]

            torchcrepe = loaded_torchcrepe

        require_supported_torch_version(str(torch.__version__))
        result = _execute(
            request,
            arguments.request_sha256,
            result_path,
            torch,
            faiss,
            np,
            signal,
            torchcrepe,
        )
        _write_json_atomic(result_path, result, maximum=_request_limit(request, "max_result_bytes"))
        return 0
    except Exception:
        # Paths, upstream tracebacks, model metadata, and lease material must not be
        # copied into Worker logs or Manager-facing errors.
        print("native sample inference failed closed", file=sys.stderr)
        return 1


def require_supported_torch_version(version: str) -> tuple[int, int]:
    """Reject versions before torch 2.6, where weights_only was not the safe default."""

    match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", version)
    if match is None:
        raise NativeInferenceDriverError("torch version evidence is invalid")
    major_minor = int(match.group(1)), int(match.group(2))
    if major_minor < (2, 6):
        raise NativeInferenceDriverError("torch 2.6 or newer is required")
    return major_minor


def _execute(
    request: Mapping[str, Any],
    request_sha256: str,
    result_path: Path,
    torch: Any,
    faiss: Any,
    np: Any,
    signal: Any,
    torchcrepe: Any | None,
) -> dict[str, Any]:
    _validate_request_shape(request)
    workspace = _absolute_normalized_path(request["workspace_root"], "workspace")
    projection = _absolute_normalized_path(request["projection_root"], "projection")
    _require_within(projection, workspace)
    _require_within(result_path, workspace)
    _require_directory(result_path.parent, workspace)
    if projection != workspace / "work" / "rvc":
        raise NativeInferenceDriverError("projection root is not attempt-private")
    _require_directory(projection, workspace)
    projection_marker = projection / ".orchestrator-projection.json"
    marker_evidence = _snapshot_file(
        projection_marker,
        maximum=_MAX_PROJECTION_MARKER_BYTES,
    )
    if marker_evidence.sha256 != request["projection_marker_sha256"]:
        raise NativeInferenceDriverError("projection marker hash changed")
    marker = _load_json(
        projection_marker,
        _MAX_PROJECTION_MARKER_BYTES,
        required_mode=0o444,
        expected_sha256=request["projection_marker_sha256"],
    )
    projection_files = _validate_projection_marker(marker)

    model_request = _mapping(request["model"])
    model_path = _absolute_normalized_path(model_request["path"], "model")
    if model_path != workspace / "outputs" / "model" / "final_small_model.pth":
        raise NativeInferenceDriverError("model path is not the same-attempt deployable model")
    _require_directory(model_path.parent, workspace)
    model_expected = _expected_evidence(model_request)
    model_open = _open_verified(
        model_path,
        model_expected,
        maximum=2 * 1024**3,
        require_mode=None,
    )

    index_request = request["index"]
    index_open: _OpenedFile | None = None
    index_evidence: dict[str, Any] | None = None
    faiss_usage = _FaissUsage()
    verified_faiss_index: _VerifiedFaissIndex | None = None
    original_faiss_read_index = faiss.read_index
    try:
        inference = _validate_inference(_mapping(request["inference"]))
        device = str(request["device"])
        use_half = _exact_bool(request["use_half"], "use_half")
        if device.startswith("cuda") and not bool(torch.cuda.is_available()):
            raise NativeInferenceDriverError("requested CUDA runtime is unavailable")
        if use_half and not device.startswith("cuda"):
            raise NativeInferenceDriverError("half inference requires CUDA")
        crepe_request = _validate_crepe_evidence(request.get("crepe_model"))
        crepe_enabled = inference["inference_f0_method"] == "crepe"
        if crepe_enabled != (crepe_request is not None) or crepe_enabled != (
            torchcrepe is not None
        ):
            raise NativeInferenceDriverError("CREPE runtime binding is inconsistent")
        if inference["index_rate"] == 0:
            if index_request is not None:
                raise NativeInferenceDriverError("index path is forbidden at index_rate=0")
            index_fd_path = ""
        else:
            index_mapping = _mapping(index_request)
            index_path = _absolute_normalized_path(index_mapping["path"], "index")
            if index_path != workspace / "outputs" / "index" / "final.index":
                raise NativeInferenceDriverError("index path is not the same-attempt index")
            _require_directory(index_path.parent, workspace)
            index_open = _open_verified(
                index_path,
                _expected_evidence(index_mapping),
                maximum=16 * 1024**3,
                require_mode=None,
            )
            index_fd_path = _descriptor_path(index_open.descriptor)
            expected_dimension = _exact_int(index_mapping["dimension"], "index dimension")
            try:
                loaded_index = original_faiss_read_index(index_fd_path)
                actual_dimension = int(loaded_index.d)
                vector_count = int(loaded_index.ntotal)
                is_trained = bool(loaded_index.is_trained)
            except Exception as exc:
                raise NativeInferenceDriverError("FAISS index could not be loaded") from exc
            if (
                expected_dimension not in {256, 768}
                or actual_dimension != expected_dimension
                or vector_count <= 0
                or not is_trained
            ):
                raise NativeInferenceDriverError("FAISS index metadata is invalid")
            reconstructed_elements = vector_count * actual_dimension
            if reconstructed_elements > (2 * 1024**3) // 4:
                raise NativeInferenceDriverError(
                    "FAISS reconstructed vectors exceed the memory boundary"
                )
            try:
                reconstructed = np.asarray(
                    loaded_index.reconstruct_n(0, vector_count)
                )
            except Exception as exc:
                raise NativeInferenceDriverError(
                    "FAISS vectors could not be reconstructed"
                ) from exc
            if (
                reconstructed.shape != (vector_count, actual_dimension)
                or reconstructed.dtype != np.dtype("float32")
                or reconstructed.size != reconstructed_elements
                or not np.isfinite(reconstructed).all()
            ):
                raise NativeInferenceDriverError(
                    "FAISS reconstructed vectors are invalid"
                )
            verified_faiss_index = _VerifiedFaissIndex(
                loaded_index,
                reconstructed,
                faiss_usage,
            )
            index_evidence = {
                "size_bytes": index_open.metadata.st_size,
                "sha256": index_mapping["sha256"],
                "dimension": actual_dimension,
                "vector_count": vector_count,
            }

        asset_policies: dict[Path, _TorchLoadPolicy] = {}
        asset_paths = [projection / "assets" / "hubert" / "hubert_base.pt"]
        if inference["inference_f0_method"] == "rmvpe":
            asset_paths.append(projection / "assets" / "rmvpe" / "rmvpe.pt")
        crepe_path: Path | None = None
        if crepe_enabled:
            crepe_path = projection.joinpath(*_CREPE_MODEL_RELATIVE_PATH.split("/"))
            _require_exact_crepe_inventory(crepe_path, projection)
            asset_paths.append(crepe_path)
        for asset_path in asset_paths:
            _require_directory(asset_path.parent, projection)
            relative = asset_path.relative_to(projection).as_posix()
            expected = projection_files.get(relative)
            if expected is None:
                raise NativeInferenceDriverError("required operator asset is not projected")
            opened_asset = _open_verified(
                asset_path,
                expected,
                maximum=8 * 1024**3,
                require_mode=0o444,
            )
            try:
                _require_open_identity(opened_asset)
            finally:
                os.close(opened_asset.descriptor)
            weights_only = asset_path == crepe_path
            asset_policies[asset_path] = _TorchLoadPolicy(
                evidence=expected,
                weights_only=weights_only,
                required_mode=0o444,
                map_location=device if weights_only else None,
            )
        if crepe_request is not None and crepe_path is not None:
            projected_crepe = projection_files[_CREPE_MODEL_RELATIVE_PATH]
            if crepe_request != _crepe_evidence_document(projected_crepe):
                raise NativeInferenceDriverError("CREPE request evidence changed")

        with os.fdopen(os.dup(model_open.descriptor), "rb") as model_stream:
            try:
                checkpoint = torch.load(
                    model_stream,
                    map_location="cpu",
                    weights_only=True,
                )
            except Exception as exc:
                raise NativeInferenceDriverError(
                    "same-attempt model failed weights-only loading"
                ) from exc
        model_parts = _validate_model_checkpoint(checkpoint, model_request, torch)

        original_torch_load = torch.load
        torch.load = _guarded_torch_load(original_torch_load, asset_policies)
        faiss.read_index = _guarded_faiss_reader(
            index_fd_path,
            verified_faiss_index,
            faiss_usage,
        )
        try:
            _verify_projection_python_sources(projection, projection_files)
            prebound_crepe_model: Any | None = None
            if crepe_path is not None:
                assert torchcrepe is not None
                prebound_crepe_model = _prebind_crepe_model(
                    torchcrepe,
                    torch.load,
                    crepe_path,
                    device,
                )
            # The reviewed wrapper imports and configures exact upstream primitives;
            # it never executes infer-web.py or tools/infer_cli.py.
            from infer.lib.infer_pack.models import (  # type: ignore[import-not-found]
                SynthesizerTrnMs256NSFsid,
                SynthesizerTrnMs256NSFsid_nono,
                SynthesizerTrnMs768NSFsid,
                SynthesizerTrnMs768NSFsid_nono,
            )
            from infer.modules.vc.pipeline import Pipeline  # type: ignore[import-not-found]
            from infer.modules.vc.utils import load_hubert  # type: ignore[import-not-found]

            config = _pipeline_config(device, use_half)
            try:
                hubert_model = load_hubert(config)
            except Exception as exc:
                raise NativeInferenceDriverError(
                    "reviewed Hubert asset could not be loaded"
                ) from exc

            model_classes = {
                ("v1", 1): SynthesizerTrnMs256NSFsid,
                ("v1", 0): SynthesizerTrnMs256NSFsid_nono,
                ("v2", 1): SynthesizerTrnMs768NSFsid,
                ("v2", 0): SynthesizerTrnMs768NSFsid_nono,
            }
            model_class = model_classes[(model_parts["version"], model_parts["f0"])]
            try:
                net_g = model_class(*model_parts["config"], is_half=use_half)
                if not hasattr(net_g, "enc_q"):
                    raise NativeInferenceDriverError("RVC model encoder boundary is invalid")
                del net_g.enc_q
                net_g.load_state_dict(model_parts["weight"], strict=True)
                net_g.eval().to(device)
                net_g = net_g.half() if use_half else net_g.float()
                pipeline = Pipeline(model_parts["target_sample_rate_hz"], config)
            except NativeInferenceDriverError:
                raise
            except Exception as exc:
                raise NativeInferenceDriverError(
                    "RVC model state is not strictly compatible"
                ) from exc

            result_items = []
            total_output_bytes = 0
            total_output_duration = 0.0
            items = _list(request["items"])
            for raw_item in items:
                item = _mapping(raw_item)
                item_id = _safe_item_id(item["test_set_item_id"])
                input_path = _absolute_normalized_path(item["input_path"], "input")
                output_path = _absolute_normalized_path(item["output_path"], "output")
                if input_path != workspace / "inputs" / "test_set" / f"{item_id}.wav":
                    raise NativeInferenceDriverError("TestSet input path is not deterministic")
                if output_path != workspace / "outputs" / "samples" / f"{item_id}.wav":
                    raise NativeInferenceDriverError("sample output path is not deterministic")
                _require_directory(input_path.parent, workspace)
                _require_directory(output_path.parent, workspace)
                input_expected = _expected_evidence(item)
                pcm = _read_verified_pcm(
                    input_path,
                    input_expected,
                    expected_sample_rate=_exact_int(item["sample_rate_hz"], "input rate"),
                    expected_channels=_exact_int(item["channels"], "input channels"),
                    expected_duration=_finite_number(item["duration_seconds"], "duration"),
                    np=np,
                    signal=signal,
                )
                times = [0.0, 0.0, 0.0]
                usage_before = (
                    faiss_usage.reads,
                    faiss_usage.reconstructs,
                    faiss_usage.searches,
                )
                try:
                    converted = pipeline.pipeline(
                        hubert_model,
                        net_g,
                        model_parts["speaker_id"],
                        pcm,
                        str(input_path),
                        times,
                        inference["transpose"],
                        inference["inference_f0_method"],
                        index_fd_path,
                        inference["index_rate"],
                        model_parts["f0"],
                        inference["filter_radius"],
                        model_parts["target_sample_rate_hz"],
                        inference["resample_sr"],
                        inference["rms_mix_rate"],
                        model_parts["version"],
                        inference["protect"],
                        None,
                    )
                except Exception as exc:
                    raise NativeInferenceDriverError(
                        "reviewed RVC Pipeline rejected a TestSet item"
                    ) from exc
                if prebound_crepe_model is not None:
                    assert torchcrepe is not None
                    _require_crepe_prebind(torchcrepe, prebound_crepe_model)
                usage_after = (
                    faiss_usage.reads,
                    faiss_usage.reconstructs,
                    faiss_usage.searches,
                )
                _require_faiss_usage(
                    usage_before,
                    usage_after,
                    index_enabled=index_open is not None,
                )
                audio = np.asarray(converted)
                if audio.ndim != 1 or audio.size <= 0 or audio.dtype != np.dtype("int16"):
                    raise NativeInferenceDriverError("RVC Pipeline output is not mono PCM16")
                output_rate = _exact_int(
                    request["expected_output_sample_rate_hz"], "output sample rate"
                )
                duration = int(audio.size) / output_rate
                expected_output_bytes = int(audio.nbytes) + 44
                next_total_output_bytes = total_output_bytes + expected_output_bytes
                next_total_output_duration = math.fsum(
                    (total_output_duration, duration)
                )
                if (
                    expected_output_bytes > _request_limit(request, "max_output_bytes")
                    or duration > _request_float_limit(
                        request, "max_output_duration_seconds"
                    )
                    or next_total_output_bytes
                    > _request_limit(request, "max_total_output_bytes")
                    or next_total_output_duration
                    > _request_float_limit(
                        request, "max_total_output_duration_seconds"
                    )
                ):
                    raise NativeInferenceDriverError("RVC Pipeline output exceeds limits")
                output_bytes = audio.astype("<i2", copy=False).tobytes(order="C")
                _write_pcm16_atomic(output_path, output_bytes, output_rate, workspace)
                output_evidence = _snapshot_file(
                    output_path,
                    maximum=_request_limit(request, "max_output_bytes"),
                )
                if output_evidence.size_bytes != expected_output_bytes:
                    raise NativeInferenceDriverError("sample WAV size is not canonical")
                total_output_bytes = next_total_output_bytes
                total_output_duration = next_total_output_duration
                result_items.append(
                    {
                        "test_set_item_id": item_id,
                        "output_path": output_path.relative_to(workspace).as_posix(),
                        "size_bytes": output_evidence.size_bytes,
                        "sha256": output_evidence.sha256,
                        "sample_rate_hz": output_rate,
                        "channels": 1,
                        "sample_width_bytes": 2,
                        "frame_count": int(audio.size),
                        "duration_seconds": duration,
                        "metrics": _pcm16_metrics(output_bytes),
                    }
                )

            for asset_path, policy in asset_policies.items():
                opened_asset = _open_verified(
                    asset_path,
                    policy.evidence,
                    maximum=8 * 1024**3,
                    require_mode=policy.required_mode,
                )
                try:
                    _require_open_identity(opened_asset)
                finally:
                    os.close(opened_asset.descriptor)
                if asset_path == crepe_path:
                    _require_exact_crepe_inventory(asset_path, projection)
                if projection_files[asset_path.relative_to(projection).as_posix()] != (
                    policy.evidence
                ):
                    raise NativeInferenceDriverError("operator asset changed during inference")
            if prebound_crepe_model is not None:
                assert torchcrepe is not None
                _require_crepe_prebind(torchcrepe, prebound_crepe_model)
            _verify_projection_python_sources(projection, projection_files)
        finally:
            torch.load = original_torch_load
            faiss.read_index = original_faiss_read_index

        _require_open_identity(model_open)
        if index_open is not None:
            _require_open_identity(index_open)
        _load_json(
            projection_marker,
            _MAX_PROJECTION_MARKER_BYTES,
            required_mode=0o444,
            expected_sha256=request["projection_marker_sha256"],
        )
        marker_after = _snapshot_file(
            projection_marker,
            maximum=_MAX_PROJECTION_MARKER_BYTES,
        )
        if marker_after != marker_evidence:
            raise NativeInferenceDriverError("projection changed during inference")
        torch_major, torch_minor = require_supported_torch_version(str(torch.__version__))
        runtime = {
            "torch_version": str(torch.__version__),
            "torch_major_minor": f"{torch_major}.{torch_minor}",
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "device": str(request["device"]),
            "cuda_available": bool(torch.cuda.is_available()),
            "cuda_version": (
                str(torch.version.cuda) if torch.version.cuda is not None else None
            ),
            "model_load_trust_mode": "weights_only=True",
            "operator_asset_load_trust_mode": "manifest-verified;weights_only=False",
            "crepe_model": crepe_request,
            "runtime_image_digest": request["runtime_image_digest"],
            "use_half": _exact_bool(request["use_half"], "use_half"),
        }
        return {
            "schema_version": 1,
            "metrics_algorithm": _PCM_METRICS_ALGORITHM,
            "request_sha256": request_sha256,
            "model": {
                "size_bytes": model_open.metadata.st_size,
                "sha256": model_request["sha256"],
            },
            "index": index_evidence,
            "runtime": runtime,
            "items": result_items,
        }
    finally:
        faiss.read_index = original_faiss_read_index
        os.close(model_open.descriptor)
        if index_open is not None:
            os.close(index_open.descriptor)


def _validate_request_shape(request: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "rvc_commit_hash",
        "workspace_root",
        "projection_root",
        "projection_marker_sha256",
        "crepe_model",
        "model",
        "index",
        "inference",
        "expected_output_sample_rate_hz",
        "device",
        "use_half",
        "runtime_image_digest",
        "metrics_algorithm",
        "limits",
        "items",
    }
    if (
        set(request) != required
        or request.get("schema_version") != 1
        or request.get("rvc_commit_hash") != _REVIEWED_COMMIT
        or not _is_sha256(request.get("projection_marker_sha256"))
        or not isinstance(request.get("device"), str)
        or request.get("device")
        not in {"cpu", "mps", "cuda", *(f"cuda:{index}" for index in range(256))}
        or not isinstance(request.get("items"), list)
        or not 1 <= len(request["items"]) <= 128
        or request.get("metrics_algorithm") != _PCM_METRICS_ALGORITHM
        or not isinstance(request.get("runtime_image_digest"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", request["runtime_image_digest"])
        is None
    ):
        raise NativeInferenceDriverError("native inference request is invalid")
    _exact_bool(request["use_half"], "use_half")
    limits = _mapping(request.get("limits"))
    if set(limits) != {
        "max_output_bytes",
        "max_output_duration_seconds",
        "max_total_output_bytes",
        "max_total_output_duration_seconds",
        "max_result_bytes",
    }:
        raise NativeInferenceDriverError("native inference limits are invalid")
    _request_limit(request, "max_output_bytes")
    _request_limit(request, "max_total_output_bytes")
    _request_limit(request, "max_result_bytes")
    _request_float_limit(request, "max_output_duration_seconds")
    _request_float_limit(request, "max_total_output_duration_seconds")
    inference = _validate_inference(_mapping(request["inference"]))
    crepe_model = _validate_crepe_evidence(request.get("crepe_model"))
    if (inference["inference_f0_method"] == "crepe") != (crepe_model is not None):
        raise NativeInferenceDriverError("CREPE request evidence is inconsistent")
    output_rate = _exact_int(
        request["expected_output_sample_rate_hz"], "output sample rate"
    )
    if not 16_000 <= output_rate <= 192_000:
        raise NativeInferenceDriverError("output sample rate is invalid")
    total_bytes = 0
    total_duration = 0.0
    for raw_item in _list(request["items"]):
        item = _mapping(raw_item)
        total_bytes += _exact_int(item.get("size_bytes"), "input size")
        total_duration += _finite_number(item.get("duration_seconds"), "duration")
        if total_bytes > 2 * 1024**3 or total_duration > 3_600:
            raise NativeInferenceDriverError("TestSet exceeds fixed inference limits")


def _validate_projection_marker(
    marker: Mapping[str, Any],
) -> dict[str, _FileEvidence]:
    if (
        set(marker) != {
            "schema_version",
            "rvc_commit_hash",
            "projection_directories",
            "files",
        }
        or marker.get("schema_version") != 1
        or marker.get("rvc_commit_hash") != _REVIEWED_COMMIT
        or not isinstance(marker.get("projection_directories"), list)
    ):
        raise NativeInferenceDriverError("projection marker is invalid")
    result: dict[str, _FileEvidence] = {}
    for raw in _list(marker["files"]):
        record = _mapping(raw)
        if set(record) != {"path", "size_bytes", "sha256"}:
            raise NativeInferenceDriverError("projection file record is invalid")
        path = record["path"]
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or path in result
        ):
            raise NativeInferenceDriverError("projection file path is invalid")
        result[path] = _expected_evidence(record)
    crepe_paths = {path for path in result if path.startswith("runtime/crepe/")}
    if crepe_paths not in (set(), {_CREPE_MODEL_RELATIVE_PATH}):
        raise NativeInferenceDriverError("CREPE projection inventory is not exact")
    expected_directories = [
        *_PROJECTION_BASE_DIRECTORIES,
        *(["runtime/crepe"] if crepe_paths else []),
    ]
    if marker["projection_directories"] != expected_directories:
        raise NativeInferenceDriverError("projection directory inventory is invalid")
    for required in (
        "infer/lib/audio.py",
        "infer/lib/rmvpe.py",
        "infer/lib/infer_pack/models.py",
        "infer/modules/vc/modules.py",
        "infer/modules/vc/pipeline.py",
        "infer/modules/vc/utils.py",
        "assets/hubert/hubert_base.pt",
        "assets/rmvpe/rmvpe.pt",
    ):
        if required not in result:
            raise NativeInferenceDriverError("reviewed inference projection is incomplete")
    return result


def _verify_projection_python_sources(
    projection: Path,
    files: Mapping[str, _FileEvidence],
) -> None:
    verified = 0
    for relative, expected in sorted(files.items()):
        if not relative.startswith("infer/") or not relative.endswith(".py"):
            continue
        path = projection.joinpath(*relative.split("/"))
        _require_directory(path.parent, projection)
        if _snapshot_file(path, maximum=16 * 1024**2) != expected:
            raise NativeInferenceDriverError(
                "reviewed RVC Python source differs from its projection manifest"
            )
        verified += 1
    if verified == 0:
        raise NativeInferenceDriverError("reviewed RVC Python source inventory is empty")


def _validate_model_checkpoint(
    checkpoint: Any,
    request: Mapping[str, Any],
    torch: Any,
) -> dict[str, Any]:
    if not isinstance(checkpoint, dict):
        raise NativeInferenceDriverError("model checkpoint root is invalid")
    required = {"weight", "config", "info", "sr", "f0", "version"}
    allowed = required | {"embedder_name", "embedder_output_layer"}
    if not required.issubset(checkpoint) or not set(checkpoint).issubset(allowed):
        raise NativeInferenceDriverError("model checkpoint metadata fields are invalid")
    version = checkpoint["version"]
    expected_version = request["version"]
    if version not in {"v1", "v2"} or version != expected_version:
        raise NativeInferenceDriverError("model version metadata is invalid")
    f0 = checkpoint["f0"]
    expected_f0 = 1 if _exact_bool(request["use_f0"], "model use_f0") else 0
    if type(f0) is not int or f0 not in {0, 1} or f0 != expected_f0:
        raise NativeInferenceDriverError("model F0 metadata is invalid")
    sample_rate = checkpoint["sr"]
    if sample_rate not in {"40k", "48k"} or sample_rate != request["sample_rate"]:
        raise NativeInferenceDriverError("model sample-rate metadata is invalid")
    config = checkpoint["config"]
    if not isinstance(config, (list, tuple)) or len(config) != 18:
        raise NativeInferenceDriverError("model config metadata is invalid")
    config = list(config)
    _validate_bounded_metadata(config, depth=0)
    target_rate = _exact_int(request["sample_rate_hz"], "model sample rate")
    if type(config[-1]) is not int or config[-1] != target_rate:
        raise NativeInferenceDriverError("model config sample rate does not match")
    weight = checkpoint["weight"]
    if not isinstance(weight, dict) or not 1 <= len(weight) <= _MAX_MODEL_STATE_KEYS:
        raise NativeInferenceDriverError("model state dictionary is invalid")
    total_elements = 0
    for key, value in weight.items():
        if not isinstance(key, str) or not key or len(key) > 512:
            raise NativeInferenceDriverError("model state key is invalid")
        if not isinstance(value, torch.Tensor):
            raise NativeInferenceDriverError("model state contains a non-tensor value")
        allowed_dtypes = {
            torch.float16,
            torch.float32,
            torch.bfloat16,
            torch.int64,
            torch.int32,
            torch.bool,
        }
        if (
            bool(value.is_sparse)
            or value.layout != torch.strided
            or value.device.type != "cpu"
            or value.dtype not in allowed_dtypes
            or value.ndim > 8
            or any(int(dimension) <= 0 or int(dimension) > 1_000_000 for dimension in value.shape)
        ):
            raise NativeInferenceDriverError("model state tensor metadata is invalid")
        total_elements += int(value.numel())
        if total_elements > _MAX_MODEL_TENSOR_ELEMENTS:
            raise NativeInferenceDriverError("model state exceeds the tensor limit")
    speaker_weight = weight.get("emb_g.weight")
    if speaker_weight is None or speaker_weight.ndim != 2 or speaker_weight.shape[0] <= 0:
        raise NativeInferenceDriverError("model speaker embedding is invalid")
    speaker_count = int(speaker_weight.shape[0])
    if (
        type(config[-3]) is not int
        or config[-3] != 109
        or speaker_count != 109
        or json.dumps(config, separators=(",", ":"))
        != json.dumps(
            _expected_model_config(version, sample_rate),
            separators=(",", ":"),
        )
    ):
        raise NativeInferenceDriverError("model speaker-count metadata is invalid")
    speaker_id = _exact_int(request["speaker_id"], "speaker id")
    if not 0 <= speaker_id < speaker_count:
        raise NativeInferenceDriverError("speaker id is outside the model")
    info = checkpoint["info"]
    if not isinstance(info, str) or len(info.encode("utf-8")) > 4 * 1024:
        raise NativeInferenceDriverError("model info metadata is invalid")
    if "embedder_name" in checkpoint and (
        not isinstance(checkpoint["embedder_name"], str)
        or len(checkpoint["embedder_name"]) > 128
    ):
        raise NativeInferenceDriverError("model embedder metadata is invalid")
    if "embedder_output_layer" in checkpoint and (
        type(checkpoint["embedder_output_layer"]) is not int
        or not 0 <= checkpoint["embedder_output_layer"] <= 128
    ):
        raise NativeInferenceDriverError("model embedder layer metadata is invalid")
    return {
        "version": version,
        "f0": f0,
        "config": config,
        "weight": weight,
        "target_sample_rate_hz": target_rate,
        "speaker_id": speaker_id,
    }


def _expected_model_config(version: str, sample_rate: str) -> list[Any]:
    prefix: list[Any] = [
        1025,
        32,
        192,
        192,
        768,
        2,
        6,
        3,
        0,
        "1",
        [3, 7, 11],
        [[1, 3, 5], [1, 3, 5], [1, 3, 5]],
    ]
    if sample_rate == "40k":
        suffix: list[Any] = [[10, 10, 2, 2], 512, [16, 16, 4, 4], 109, 256, 40_000]
    elif version == "v1":
        suffix = [[10, 6, 2, 2, 2], 512, [16, 16, 4, 4, 4], 109, 256, 48_000]
    else:
        suffix = [[12, 10, 2, 2], 512, [24, 20, 4, 4], 109, 256, 48_000]
    return [*prefix, *suffix]


def _validate_bounded_metadata(value: Any, *, depth: int) -> None:
    if depth > 8:
        raise NativeInferenceDriverError("model config nesting is too deep")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and len(value) > 1_024:
            raise NativeInferenceDriverError("model config string is too long")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NativeInferenceDriverError("model config number is non-finite")
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 10_000:
            raise NativeInferenceDriverError("model config collection is too large")
        for item in value:
            _validate_bounded_metadata(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 10_000:
            raise NativeInferenceDriverError("model config mapping is too large")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise NativeInferenceDriverError("model config mapping key is invalid")
            _validate_bounded_metadata(item, depth=depth + 1)
        return
    raise NativeInferenceDriverError("model config contains an unsupported value")


def _validate_inference(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "inference_f0_method",
        "transpose",
        "index_rate",
        "filter_radius",
        "resample_sr",
        "rms_mix_rate",
        "protect",
    }
    if set(value) != required or value.get("inference_f0_method") not in _F0_METHODS:
        raise NativeInferenceDriverError("inference config fields are invalid")
    transpose = _exact_int(value["transpose"], "transpose")
    filter_radius = _exact_int(value["filter_radius"], "filter radius")
    resample_sr = _exact_int(value["resample_sr"], "resample rate")
    index_rate = _finite_number(value["index_rate"], "index rate")
    rms_mix_rate = _finite_number(value["rms_mix_rate"], "RMS mix rate")
    protect = _finite_number(value["protect"], "protect")
    if (
        not -48 <= transpose <= 48
        or not 0 <= filter_radius <= 7
        or resample_sr != 0
        and not 16_000 <= resample_sr <= 192_000
        or not 0 <= index_rate <= 1
        or not 0 <= rms_mix_rate <= 1
        or not 0 <= protect <= 0.5
    ):
        raise NativeInferenceDriverError("inference config value is out of bounds")
    return {
        "inference_f0_method": value["inference_f0_method"],
        "transpose": transpose,
        "index_rate": index_rate,
        "filter_radius": filter_radius,
        "resample_sr": resample_sr,
        "rms_mix_rate": rms_mix_rate,
        "protect": protect,
    }


def _pipeline_config(device: str, use_half: bool) -> SimpleNamespace:
    if use_half:
        x_pad, x_query, x_center, x_max = 3, 10, 60, 65
    else:
        x_pad, x_query, x_center, x_max = 1, 6, 38, 41
    return SimpleNamespace(
        device=device,
        is_half=use_half,
        x_pad=x_pad,
        x_query=x_query,
        x_center=x_center,
        x_max=x_max,
        dml=False,
    )


def _validate_crepe_evidence(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "asset_size_bytes",
        "asset_sha256",
        "weights_only",
        "model_capacity",
    }:
        raise NativeInferenceDriverError("CREPE model evidence is invalid")
    size = value.get("asset_size_bytes")
    digest = value.get("asset_sha256")
    if (
        type(size) is not int
        or not 0 < size <= 8 * 1024**3
        or not _is_sha256(digest)
        or value.get("weights_only") is not True
        or value.get("model_capacity") != _CREPE_MODEL_CAPACITY
    ):
        raise NativeInferenceDriverError("CREPE model evidence is invalid")
    return {
        "asset_size_bytes": size,
        "asset_sha256": digest,
        "weights_only": True,
        "model_capacity": _CREPE_MODEL_CAPACITY,
    }


def _crepe_evidence_document(evidence: _FileEvidence) -> dict[str, Any]:
    return {
        "asset_size_bytes": evidence.size_bytes,
        "asset_sha256": evidence.sha256,
        "weights_only": True,
        "model_capacity": _CREPE_MODEL_CAPACITY,
    }


def _require_exact_crepe_inventory(asset_path: Path, projection: Path) -> None:
    expected = projection.joinpath(*_CREPE_MODEL_RELATIVE_PATH.split("/"))
    if asset_path != expected:
        raise NativeInferenceDriverError("CREPE asset path is not fixed")
    _require_directory(asset_path.parent, projection)
    try:
        actual_names = {entry.name for entry in os.scandir(asset_path.parent)}
    except OSError as exc:
        raise NativeInferenceDriverError("CREPE projection is unreadable") from exc
    if actual_names != {asset_path.name}:
        raise NativeInferenceDriverError("CREPE projection inventory is not exact")


def _prebind_crepe_model(
    torchcrepe: Any,
    torch_load: Callable[..., Any],
    asset_path: Path,
    device: str,
) -> Any:
    constructor = getattr(torchcrepe, "Crepe", None)
    infer = getattr(torchcrepe, "infer", None)
    if not callable(constructor) or infer is None:
        raise NativeInferenceDriverError("torchcrepe runtime surface is invalid")
    try:
        model = constructor(_CREPE_MODEL_CAPACITY)
        state_dict = torch_load(
            str(asset_path),
            map_location=device,
            weights_only=True,
        )
        if (
            not isinstance(state_dict, Mapping)
            or not state_dict
            or any(not isinstance(key, str) or not key for key in state_dict)
        ):
            raise NativeInferenceDriverError("CREPE state dictionary is invalid")
        load_state_dict = getattr(model, "load_state_dict", None)
        evaluate = getattr(model, "eval", None)
        move = getattr(model, "to", None)
        if not callable(load_state_dict) or not callable(evaluate) or not callable(move):
            raise NativeInferenceDriverError("CREPE model surface is invalid")
        incompatible = load_state_dict(state_dict, strict=True)
        if getattr(incompatible, "missing_keys", ()) or getattr(
            incompatible, "unexpected_keys", ()
        ):
            raise NativeInferenceDriverError("CREPE state dictionary is not strict")
        evaluate()
        move(device)
        infer.model = model
        infer.capacity = _CREPE_MODEL_CAPACITY
    except NativeInferenceDriverError:
        raise
    except Exception as exc:
        raise NativeInferenceDriverError("CREPE model could not be prebound") from exc
    _require_crepe_prebind(torchcrepe, model)
    return model


def _require_crepe_prebind(torchcrepe: Any, model: Any) -> None:
    infer = getattr(torchcrepe, "infer", None)
    if (
        infer is None
        or getattr(infer, "model", None) is not model
        or getattr(infer, "capacity", None) != _CREPE_MODEL_CAPACITY
    ):
        raise NativeInferenceDriverError("CREPE model prebind did not persist")


def _guarded_faiss_reader(
    expected_descriptor_path: str,
    verified_index: _VerifiedFaissIndex | None,
    usage: _FaissUsage,
) -> Callable[[str], Any]:
    def guarded(path: str) -> Any:
        if verified_index is None or path != expected_descriptor_path:
            raise NativeInferenceDriverError("unreviewed FAISS index read is forbidden")
        usage.reads += 1
        return verified_index

    return guarded


def _require_faiss_usage(
    before: tuple[int, int, int],
    after: tuple[int, int, int],
    *,
    index_enabled: bool,
) -> None:
    if index_enabled:
        if (
            after[0] != before[0] + 1
            or after[1] != before[1] + 1
            or after[2] <= before[2]
        ):
            raise NativeInferenceDriverError(
                "RVC Pipeline did not prove FAISS retrieval usage"
            )
    elif after != before:
        raise NativeInferenceDriverError(
            "RVC Pipeline attempted an index at index_rate=0"
        )


def _guarded_torch_load(
    original: Callable[..., Any],
    trusted_assets: Mapping[Path, _TorchLoadPolicy],
) -> Callable[..., Any]:
    normalized = {
        Path(os.path.abspath(str(path))): policy
        for path, policy in trusted_assets.items()
    }

    def guarded(source: Any, *args: Any, **kwargs: Any) -> Any:
        raw_name = (
            source
            if isinstance(source, (str, os.PathLike))
            else getattr(source, "name", None)
        )
        if not isinstance(raw_name, (str, os.PathLike)):
            raise NativeInferenceDriverError("operator asset load source is not explicit")
        path = Path(os.path.abspath(os.fspath(raw_name)))
        policy = normalized.get(path)
        if policy is None:
            raise NativeInferenceDriverError("unreviewed torch.load source is forbidden")
        if args or "pickle_module" in kwargs:
            raise NativeInferenceDriverError("operator asset load arguments are forbidden")
        requested_mode = kwargs.get("weights_only")
        if policy.weights_only:
            if requested_mode is not True:
                raise NativeInferenceDriverError("operator asset trust mode conflicts")
        elif requested_mode not in {None, False}:
            raise NativeInferenceDriverError("operator asset trust mode conflicts")
        if policy.map_location is not None and kwargs.get("map_location") != (
            policy.map_location
        ):
            raise NativeInferenceDriverError("operator asset map location conflicts")
        kwargs["weights_only"] = policy.weights_only
        opened = _open_verified(
            path,
            policy.evidence,
            maximum=8 * 1024**3,
            require_mode=policy.required_mode,
        )
        try:
            os.lseek(opened.descriptor, 0, os.SEEK_SET)
            with os.fdopen(os.dup(opened.descriptor), "rb") as verified_stream:
                result = original(verified_stream, *args, **kwargs)
            _require_open_identity(opened)
            return result
        finally:
            os.close(opened.descriptor)

    return guarded


def _read_verified_pcm(
    path: Path,
    expected: _FileEvidence,
    *,
    expected_sample_rate: int,
    expected_channels: int,
    expected_duration: float,
    np: Any,
    signal: Any,
) -> Any:
    opened = _open_verified(
        path,
        expected,
        maximum=2 * 1024**3,
        require_mode=0o600,
    )
    try:
        os.lseek(opened.descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(opened.descriptor), "rb") as source:
            try:
                with wave.open(source, "rb") as audio:
                    channels = audio.getnchannels()
                    sample_rate = audio.getframerate()
                    sample_width = audio.getsampwidth()
                    frame_count = audio.getnframes()
                    if (
                        audio.getcomptype() != "NONE"
                        or channels != expected_channels
                        or sample_rate != expected_sample_rate
                        or sample_width not in {1, 2, 3, 4}
                        or frame_count <= 0
                    ):
                        raise NativeInferenceDriverError("TestSet item PCM metadata is invalid")
                    raw = audio.readframes(frame_count)
                    if (
                        len(raw) != frame_count * channels * sample_width
                        or audio.readframes(1)
                    ):
                        raise NativeInferenceDriverError("TestSet item PCM frames are truncated")
            except (EOFError, wave.Error) as exc:
                raise NativeInferenceDriverError("TestSet item WAV structure is invalid") from exc
        duration = frame_count / sample_rate
        tolerance = max(0.000001, 1 / sample_rate)
        if abs(duration - expected_duration) > tolerance:
            raise NativeInferenceDriverError("TestSet item PCM duration changed")
        _require_open_identity(opened)
        audio_values = _decode_pcm(raw, sample_width, np)
        audio_values = audio_values.reshape(frame_count, channels).mean(axis=1)
        if sample_rate != 16_000:
            divisor = math.gcd(sample_rate, 16_000)
            audio_values = signal.resample_poly(
                audio_values,
                16_000 // divisor,
                sample_rate // divisor,
            )
        audio_values = np.asarray(audio_values, dtype=np.float32)
        if audio_values.ndim != 1 or audio_values.size <= 0 or not np.isfinite(audio_values).all():
            raise NativeInferenceDriverError("TestSet PCM decode is invalid")
        audio_max = float(np.abs(audio_values).max()) / 0.95
        if audio_max > 1:
            audio_values /= audio_max
        return audio_values
    finally:
        os.close(opened.descriptor)


def _decode_pcm(raw: bytes, sample_width: int, np: Any) -> Any:
    if sample_width == 1:
        return (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if sample_width == 2:
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if sample_width == 3:
        octets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = octets[:, 0] | (octets[:, 1] << 8) | (octets[:, 2] << 16)
        values = (values ^ 0x800000) - 0x800000
        return values.astype(np.float32) / 8388608.0
    if sample_width == 4:
        return np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    raise NativeInferenceDriverError("unsupported PCM sample width")


def _write_pcm16_atomic(path: Path, content: bytes, sample_rate: int, boundary: Path) -> None:
    _require_within(path, boundary)
    _require_directory(path.parent, boundary)
    temporary_name = f".{path.name}.{uuid4().hex}.partial"
    parent_descriptor: int | None = None
    descriptor: int | None = None
    try:
        parent_descriptor = _open_absolute_nofollow(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        with os.fdopen(os.dup(descriptor), "wb") as target:
            with wave.open(target, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(sample_rate)
                audio.writeframes(content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
    except OSError as exc:
        raise NativeInferenceDriverError("sample output could not be published") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_descriptor is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            os.close(parent_descriptor)


def _pcm16_metrics(content: bytes) -> dict[str, float]:
    if not content or len(content) % 2:
        raise NativeInferenceDriverError("sample output has no PCM values")
    count = 0
    square_sum = 0.0
    peak = 0.0
    clipped = 0
    silent = 0
    chunk_bytes = 65_536 * 2
    for offset in range(0, len(content), chunk_bytes):
        values = array("h")
        values.frombytes(content[offset : offset + chunk_bytes])
        if sys.byteorder != "little":
            values.byteswap()
        chunk = _accumulate_integer_pcm(values, sample_width_bytes=2)
        count += chunk[0]
        square_sum += chunk[1]
        peak = max(peak, chunk[2])
        clipped += chunk[3]
        silent += chunk[4]
    return _finish_integer_pcm_metrics(count, square_sum, peak, clipped, silent)


def _integer_pcm_metrics(
    values: Sequence[int], *, sample_width_bytes: int
) -> dict[str, float]:
    return _finish_integer_pcm_metrics(
        *_accumulate_integer_pcm(values, sample_width_bytes=sample_width_bytes)
    )


def _accumulate_integer_pcm(
    values: Sequence[int], *, sample_width_bytes: int
) -> tuple[int, float, float, int, int]:
    if sample_width_bytes not in {1, 2, 3, 4} or not values:
        raise NativeInferenceDriverError("sample output PCM metrics input is invalid")
    scale = 1 << (sample_width_bytes * 8 - 1)
    minimum = -scale
    maximum = scale - 1
    negative_clip_threshold = -math.ceil(scale * _CLIPPING_THRESHOLD)
    positive_clip_threshold = math.ceil(maximum * _CLIPPING_THRESHOLD)
    count = 0
    square_sum = 0.0
    peak = 0.0
    clipped = 0
    silent = 0
    for value in values:
        if type(value) is not int or not minimum <= value <= maximum:
            raise NativeInferenceDriverError("sample output PCM value is invalid")
        normalized = value / scale
        amplitude = abs(normalized)
        count += 1
        square_sum += normalized * normalized
        peak = max(peak, amplitude)
        clipped += value <= negative_clip_threshold or value >= positive_clip_threshold
        silent += amplitude <= _SILENCE_THRESHOLD
    return count, square_sum, peak, clipped, silent


def _finish_integer_pcm_metrics(
    count: int,
    square_sum: float,
    peak: float,
    clipped: int,
    silent: int,
) -> dict[str, float]:
    if count <= 0:
        raise NativeInferenceDriverError("sample output PCM metrics input is empty")
    return {
        "peak_amplitude": peak,
        "rms": math.sqrt(square_sum / count),
        "clipping_ratio": clipped / count,
        "silence_ratio": silent / count,
    }


def _open_verified(
    path: Path,
    expected: _FileEvidence,
    *,
    maximum: int,
    require_mode: int | None,
) -> _OpenedFile:
    try:
        descriptor = _open_absolute_nofollow(path, os.O_RDONLY)
    except OSError as exc:
        raise NativeInferenceDriverError("inference input cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > maximum
            or metadata.st_size != expected.size_bytes
            or require_mode is not None
            and stat.S_IMODE(metadata.st_mode) != require_mode
        ):
            raise NativeInferenceDriverError("inference input metadata is invalid")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024**2):
            digest.update(chunk)
        if digest.hexdigest() != expected.sha256:
            raise NativeInferenceDriverError("inference input checksum is invalid")
        final = os.fstat(descriptor)
        if _stat_identity(metadata) != _stat_identity(final):
            raise NativeInferenceDriverError("inference input changed during verification")
        return _OpenedFile(descriptor, metadata)
    except BaseException:
        os.close(descriptor)
        raise


def _snapshot_file(path: Path, *, maximum: int) -> _FileEvidence:
    try:
        descriptor = _open_absolute_nofollow(path, os.O_RDONLY)
    except OSError as exc:
        raise NativeInferenceDriverError("runtime file cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
            raise NativeInferenceDriverError("runtime file metadata is invalid")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024**2):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after):
            raise NativeInferenceDriverError("runtime file changed during verification")
        return _FileEvidence(before.st_size, digest.hexdigest())
    finally:
        os.close(descriptor)


def _require_open_identity(opened: _OpenedFile) -> None:
    if _stat_identity(opened.metadata) != _stat_identity(os.fstat(opened.descriptor)):
        raise NativeInferenceDriverError("opened inference input changed during use")


def _descriptor_path(descriptor: int) -> str:
    path = Path("/proc/self/fd") / str(descriptor)
    if not path.exists():
        raise NativeInferenceDriverError("verified descriptor paths are unavailable")
    return str(path)


def _load_json(
    path: Path,
    maximum: int,
    *,
    required_mode: int,
    expected_sha256: str,
) -> Mapping[str, Any]:
    try:
        descriptor = _open_absolute_nofollow(path, os.O_RDONLY)
    except OSError as exc:
        raise NativeInferenceDriverError("JSON input cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not 0 < metadata.st_size <= maximum
            or stat.S_IMODE(metadata.st_mode) != required_mode
        ):
            raise NativeInferenceDriverError("JSON input metadata is invalid")
        content = bytearray()
        while chunk := os.read(descriptor, min(1024**2, maximum + 1 - len(content))):
            content.extend(chunk)
            if len(content) > maximum:
                raise NativeInferenceDriverError("JSON input exceeds its limit")
        if _stat_identity(metadata) != _stat_identity(os.fstat(descriptor)):
            raise NativeInferenceDriverError("JSON input changed during read")
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise NativeInferenceDriverError("JSON input checksum is invalid")
        document = json.loads(
            bytes(content).decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise NativeInferenceDriverError("JSON input is invalid") from exc
    finally:
        os.close(descriptor)
    if not isinstance(document, dict):
        raise NativeInferenceDriverError("JSON root is invalid")
    return document


def _write_json_atomic(path: Path, document: Mapping[str, Any], *, maximum: int) -> None:
    content = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if not content or len(content) > maximum:
        raise NativeInferenceDriverError("driver result exceeds its limit")
    temporary_name = f".{path.name}.{uuid4().hex}.partial"
    parent_descriptor: int | None = None
    descriptor: int | None = None
    try:
        parent_descriptor = _open_absolute_nofollow(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
    except OSError as exc:
        raise NativeInferenceDriverError("driver result could not be published") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_descriptor is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            os.close(parent_descriptor)


def _expected_evidence(value: Mapping[str, Any]) -> _FileEvidence:
    size = _exact_int(value.get("size_bytes"), "file size")
    digest = value.get("sha256")
    if size <= 0 or not isinstance(digest, str) or not _is_sha256(digest):
        raise NativeInferenceDriverError("file evidence is invalid")
    return _FileEvidence(size, digest)


def _request_limit(request: Mapping[str, Any], key: str) -> int:
    limits = _mapping(request.get("limits"))
    value = _exact_int(limits.get(key), key)
    hard_maximum = {
        "max_output_bytes": 256 * 1024**2,
        "max_total_output_bytes": _MAX_TOTAL_OUTPUT_BYTES,
        "max_result_bytes": 64 * 1024**2,
    }.get(key)
    if hard_maximum is None:
        raise NativeInferenceDriverError("request byte limit name is invalid")
    if not 1 <= value <= hard_maximum:
        raise NativeInferenceDriverError("request byte limit is invalid")
    return value


def _request_float_limit(request: Mapping[str, Any], key: str) -> float:
    limits = _mapping(request.get("limits"))
    value = _finite_number(limits.get(key), key)
    hard_maximum = {
        "max_output_duration_seconds": 600.0,
        "max_total_output_duration_seconds": _MAX_TOTAL_OUTPUT_DURATION_SECONDS,
    }.get(key)
    if hard_maximum is None:
        raise NativeInferenceDriverError("request duration limit name is invalid")
    if not 0 < value <= hard_maximum:
        raise NativeInferenceDriverError("request duration limit is invalid")
    return value


def _absolute_normalized_path(value: Any, label: str) -> Path:
    del label
    if not isinstance(value, str) or not value or "\x00" in value:
        raise NativeInferenceDriverError("runtime path is invalid")
    path = Path(value)
    if not path.is_absolute() or path != Path(os.path.abspath(value)):
        raise NativeInferenceDriverError("runtime path is not absolute and normalized")
    return path


def _open_absolute_nofollow(path: Path, final_flags: int) -> int:
    """Open an absolute path with O_NOFOLLOW on every ancestor component."""

    rendered = str(path)
    if not path.is_absolute() or path != Path(os.path.abspath(rendered)):
        raise NativeInferenceDriverError("runtime path is not absolute and normalized")
    components = path.parts[1:]
    if not components:
        raise NativeInferenceDriverError("runtime path cannot be the filesystem root")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    current = os.open("/", directory_flags)
    try:
        for component in components[:-1]:
            following = os.open(component, directory_flags, dir_fd=current)
            os.close(current)
            current = following
        return os.open(
            components[-1],
            final_flags
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=current,
        )
    finally:
        os.close(current)


def _require_within(path: Path, boundary: Path) -> None:
    try:
        common = os.path.commonpath((str(path), str(boundary)))
    except ValueError as exc:
        raise NativeInferenceDriverError("runtime path boundary is invalid") from exc
    if common != str(boundary):
        raise NativeInferenceDriverError("runtime path escapes the attempt workspace")


def _require_directory(path: Path, boundary: Path) -> None:
    _require_within(path, boundary)
    try:
        relative = path.relative_to(boundary)
        current = boundary
        metadata = current.stat(follow_symlinks=False)
        if current.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise NativeInferenceDriverError("runtime directory ancestry is unsafe")
        for component in relative.parts:
            current /= component
            metadata = current.stat(follow_symlinks=False)
            if current.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                raise NativeInferenceDriverError("runtime directory ancestry is unsafe")
    except (OSError, ValueError) as exc:
        raise NativeInferenceDriverError("runtime directory is unavailable") from exc
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise NativeInferenceDriverError("runtime directory is unsafe")


def _safe_item_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value)
        or ".." in value
    ):
        raise NativeInferenceDriverError("TestSet item ID is invalid")
    return value


def _exact_int(value: Any, label: str) -> int:
    del label
    if type(value) is not int:
        raise NativeInferenceDriverError("exact integer value is required")
    return value


def _exact_bool(value: Any, label: str) -> bool:
    del label
    if type(value) is not bool:
        raise NativeInferenceDriverError("exact boolean value is required")
    return value


def _finite_number(value: Any, label: str) -> float:
    del label
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NativeInferenceDriverError("finite numeric value is required")
    result = float(value)
    if not math.isfinite(result):
        raise NativeInferenceDriverError("finite numeric value is required")
    return result


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise NativeInferenceDriverError("JSON object is required")
    return value


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise NativeInferenceDriverError("JSON list is required")
    return value


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NativeInferenceDriverError("JSON contains duplicate keys")
        result[key] = value
    return result


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


if __name__ == "__main__":
    raise SystemExit(main())
