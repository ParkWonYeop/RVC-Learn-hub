# ruff: noqa: ASYNC240

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from rvc_orchestrator_contracts import (
    InferencePresetConfig,
    JobClaim,
    JobConfig,
    JobStatus,
    job_config_sha256,
)
from rvc_worker.native_inference import (
    NativeCrepeModelEvidence,
    NativeFixedTestSetInferenceDependency,
    NativeInferencePublication,
    NativeSampleInferenceError,
    NativeSampleInferenceLimits,
    _inspect_pcm_wave,
    _integer_pcm_metrics,
    build_native_sample_inference_command,
)
from rvc_worker.native_inference_driver import (
    NativeInferenceDriverError,
    _expected_model_config,
    _guarded_torch_load,
    _prebind_crepe_model,
    _require_faiss_usage,
    _validate_inference,
    _validate_request_shape,
    _verify_projection_python_sources,
    require_supported_torch_version,
)
from rvc_worker.native_inference_driver import (
    _FileEvidence as DriverFileEvidence,
)
from rvc_worker.native_inference_driver import (
    _TorchLoadPolicy as DriverTorchLoadPolicy,
)
from rvc_worker.native_runner import NativeSampleInferenceBinding
from rvc_worker.process import ProcessResult, ProcessSpec, ProcessTimedOut
from rvc_worker.runner import RvcRunContext
from rvc_worker.workspace import WorkspaceManager

from .helpers import make_claim

_IMAGE_DIGEST = "sha256:" + "a" * 64


class FakeInferenceProcessRunner:
    def __init__(self) -> None:
        self.specs: list[ProcessSpec] = []
        self.mode = "success"

    async def run(
        self,
        spec: ProcessSpec,
        cancellation: asyncio.Event,
        *,
        output_callback: Any = None,
    ) -> ProcessResult:
        del output_callback
        self.specs.append(spec)
        if self.mode == "timeout":
            raise ProcessTimedOut("injected timeout")
        if cancellation.is_set():
            raise asyncio.CancelledError
        request_path = Path(spec.argv[spec.argv.index("--request") + 1])
        result_path = Path(spec.argv[spec.argv.index("--result") + 1])
        request_sha256 = spec.argv[spec.argv.index("--request-sha256") + 1]
        if hashlib.sha256(request_path.read_bytes()).hexdigest() != request_sha256:
            raise AssertionError("request argv hash does not match canonical request bytes")
        request = json.loads(request_path.read_text(encoding="utf-8"))
        workspace = Path(request["workspace_root"])
        result_items = []
        for item in request["items"]:
            output = Path(item["output_path"])
            _write_pcm_wave(output, sample_rate=request["expected_output_sample_rate_hz"])
            inspected = _inspect_pcm_wave(
                output,
                maximum=request["limits"]["max_output_bytes"],
                include_metrics=True,
            )
            assert inspected.metrics is not None
            result_items.append(
                {
                    "test_set_item_id": item["test_set_item_id"],
                    "output_path": output.relative_to(workspace).as_posix(),
                    "size_bytes": inspected.size_bytes,
                    "sha256": inspected.sha256,
                    "sample_rate_hz": inspected.sample_rate_hz,
                    "channels": inspected.channels,
                    "sample_width_bytes": inspected.sample_width_bytes,
                    "frame_count": inspected.frame_count,
                    "duration_seconds": inspected.duration_seconds,
                    "metrics": dict(inspected.metrics),
                }
            )
        model = request["model"]
        index = request["index"]
        document: dict[str, Any] = {
            "schema_version": 1,
            "metrics_algorithm": request["metrics_algorithm"],
            "request_sha256": request_sha256,
            "model": {"size_bytes": model["size_bytes"], "sha256": model["sha256"]},
            "index": (
                {
                    "size_bytes": index["size_bytes"],
                    "sha256": index["sha256"],
                    "dimension": index["dimension"],
                    "vector_count": 2,
                }
                if index is not None
                else None
            ),
            "runtime": {
                "torch_version": "2.6.0+cu124",
                "torch_major_minor": "2.6",
                "python_version": "3.11.9",
                "platform": "test-linux",
                "device": request["device"],
                "cuda_available": request["device"].startswith("cuda"),
                "cuda_version": "12.4" if request["device"].startswith("cuda") else None,
                "model_load_trust_mode": "weights_only=True",
                "operator_asset_load_trust_mode": ("manifest-verified;weights_only=False"),
                "crepe_model": request["crepe_model"],
                "runtime_image_digest": request["runtime_image_digest"],
                "use_half": request["use_half"],
            },
            "items": result_items,
        }
        if self.mode == "bad-index" and document["index"] is not None:
            document["index"]["dimension"] = 999
        if self.mode == "bad-output-hash":
            document["items"][0]["sha256"] = "f" * 64
        if self.mode == "malformed-result":
            document = {"schema_version": 1}
        if self.mode == "bad-crepe-evidence" and document["runtime"]["crepe_model"]:
            document["runtime"]["crepe_model"]["asset_sha256"] = "f" * 64
        result_path.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        result_path.chmod(0o600)
        if self.mode == "mutate-model":
            Path(model["path"]).write_bytes(b"changed-after-driver")
        if self.mode == "mutate-crepe-asset":
            crepe_asset = workspace / "work/rvc/runtime/crepe/full.pth"
            crepe_asset.chmod(0o644)
            crepe_asset.write_bytes(b"changed-after-driver")
            crepe_asset.chmod(0o444)
        spec.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        spec.stdout_path.write_text("driver stdout\n", encoding="utf-8")
        spec.stderr_path.write_text("driver stderr\n", encoding="utf-8")
        return ProcessResult(spec.argv, 0, spec.stdout_path, spec.stderr_path)


class NativeInferenceTests(unittest.IsolatedAsyncioTestCase):
    def _fixture(
        self,
        root: Path,
        *,
        index_rate: float = 0,
        f0_method: str = "rmvpe",
    ) -> tuple[
        NativeFixedTestSetInferenceDependency,
        FakeInferenceProcessRunner,
        RvcRunContext,
        NativeSampleInferenceBinding,
    ]:
        input_bytes = _pcm_wave_bytes(sample_rate=8_000)
        claim = _sample_claim(
            input_bytes,
            index_rate=index_rate,
            f0_method=f0_method,
        )
        workspace = WorkspaceManager(root / "jobs").prepare(claim.job_id, claim.attempt_id)
        context = RvcRunContext(claim, workspace)
        input_path = workspace.inputs / "test_set" / "test-item-1.wav"
        input_path.parent.mkdir(mode=0o700)
        input_path.write_bytes(input_bytes)
        input_path.chmod(0o600)
        _write_transfer_marker(context)
        model = workspace.outputs / "model" / "final_small_model.pth"
        model.parent.mkdir(mode=0o700)
        model.write_bytes(b"same-attempt-model")
        model.chmod(0o600)
        if index_rate > 0:
            index = workspace.outputs / "index" / "final.index"
            index.parent.mkdir(mode=0o700)
            index.write_bytes(b"same-attempt-index")
            index.chmod(0o600)
        context.rvc_root.mkdir(parents=True, mode=0o700)
        marker = context.rvc_root / ".orchestrator-projection.json"
        if f0_method == "crepe":
            crepe_asset = context.rvc_root / "runtime/crepe/full.pth"
            crepe_asset.parent.mkdir(parents=True, mode=0o755)
            crepe_asset.write_bytes(b"manifest-pinned-crepe-state")
            crepe_asset.chmod(0o444)
            marker_document = {
                "schema_version": 1,
                "rvc_commit_hash": "7ef19867780cf703841ebafb565a4e47d1ea86ff",
                "projection_directories": [
                    "infer",
                    "configs",
                    "assets/pretrained",
                    "assets/pretrained_v2",
                    "assets/hubert",
                    "assets/rmvpe",
                    "logs/mute",
                    "runtime/crepe",
                ],
                "files": [
                    {
                        "path": "runtime/crepe/full.pth",
                        "size_bytes": crepe_asset.stat().st_size,
                        "sha256": hashlib.sha256(crepe_asset.read_bytes()).hexdigest(),
                    }
                ],
            }
            marker.write_text(
                json.dumps(marker_document, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
        else:
            marker.write_text('{"fixture":"projection"}\n', encoding="utf-8")
        marker.chmod(0o444)
        process = FakeInferenceProcessRunner()
        dependency = NativeFixedTestSetInferenceDependency(
            runtime_image_digest=_IMAGE_DIGEST,
            process_runner=process,
        )
        binding = NativeSampleInferenceBinding(
            rvc_commit_hash="7ef19867780cf703841ebafb565a4e47d1ea86ff",
            asset_manifest_sha256="b" * 64,
            projection_manifest_sha256="c" * 64,
            python_executable="/opt/rvc/bin/python",
            device="cpu",
            use_half=False,
        )
        dependency.bind_native_runtime(binding)
        return dependency, process, context, binding

    async def test_no_index_generation_replay_evaluation_and_typed_loader(self) -> None:
        with TemporaryDirectory() as temporary:
            dependency, process, context, _ = self._fixture(Path(temporary).resolve())
            generated = await dependency.run_stage(
                JobStatus.GENERATING_SAMPLES, context, asyncio.Event()
            )
            self.assertEqual(generated.metadata["sample_count"], 1)  # type: ignore[index]
            self.assertEqual(len(process.specs), 1)
            self.assertEqual(
                process.specs[0].env["rmvpe_root"],  # type: ignore[index]
                str(context.rvc_root / "assets/rmvpe"),
            )
            self.assertEqual(process.specs[0].env["HF_HUB_OFFLINE"], "1")  # type: ignore[index]
            self.assertEqual(process.specs[0].env["TRANSFORMERS_OFFLINE"], "1")  # type: ignore[index]
            self.assertIn("TORCH_HOME", process.specs[0].env)  # type: ignore[operator]
            self.assertNotIn("TORCH_FORCE_WEIGHTS_ONLY_LOAD", process.specs[0].env)  # type: ignore[operator]
            self.assertFalse(
                {
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "HF_TOKEN",
                    "HUGGING_FACE_HUB_TOKEN",
                }
                & set(process.specs[0].env or {})
            )
            manifest = context.workspace.outputs / "samples/inference_manifest.json"
            self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
            document = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertIsNone(document["index"]["path"])
            self.assertNotIn(context.claim.lease_id, manifest.read_text(encoding="utf-8"))
            self.assertEqual(document["runtime_evidence"]["runtime_image_digest"], _IMAGE_DIGEST)
            self.assertIsNone(document["runtime_evidence"]["crepe_model"])
            self.assertEqual(document["metrics_algorithm"], "pcm-normalized-v2")
            request_path = Path(process.specs[0].argv[process.specs[0].argv.index("--request") + 1])
            self.assertIsNone(json.loads(request_path.read_text(encoding="utf-8"))["crepe_model"])

            replay = await dependency.run_stage(
                JobStatus.GENERATING_SAMPLES, context, asyncio.Event()
            )
            self.assertTrue(replay.metadata["replayed"])  # type: ignore[index]
            self.assertEqual(len(process.specs), 1)

            evaluated = await dependency.run_stage(JobStatus.EVALUATING, context, asyncio.Event())
            self.assertEqual(evaluated.metadata["sample_count"], 1)  # type: ignore[index]
            publication = dependency.load_publication(context)
            self.assertIsInstance(publication, NativeInferencePublication)
            self.assertIsNone(publication.index)
            self.assertEqual(publication.samples[0].output_channels, 1)
            self.assertEqual(publication.samples[0].output_sample_width_bytes, 2)
            self.assertGreater(publication.samples[0].output_frame_count, 0)
            self.assertEqual(publication.runtime_image_digest, _IMAGE_DIGEST)
            self.assertIsNone(publication.crepe_model)
            self.assertFalse(list((context.workspace.outputs / "samples").glob("*.partial")))

    async def test_replay_rejects_sample_mode_change(self) -> None:
        with TemporaryDirectory() as temporary:
            dependency, _, context, _ = self._fixture(Path(temporary).resolve())
            await dependency.run_stage(JobStatus.GENERATING_SAMPLES, context, asyncio.Event())
            sample = context.workspace.outputs / "samples/test-item-1.wav"
            sample.chmod(0o644)
            with self.assertRaisesRegex(NativeSampleInferenceError, "permissions"):
                await dependency.run_stage(JobStatus.GENERATING_SAMPLES, context, asyncio.Event())

    async def test_index_evidence_is_required_and_dimension_checked(self) -> None:
        with TemporaryDirectory() as temporary:
            dependency, process, context, _ = self._fixture(
                Path(temporary).resolve(), index_rate=0.75
            )
            process.mode = "bad-index"
            with self.assertRaisesRegex(NativeSampleInferenceError, "FAISS"):
                await dependency.run_stage(JobStatus.GENERATING_SAMPLES, context, asyncio.Event())

    async def test_result_output_hash_and_model_toctou_fail_closed(self) -> None:
        for mode, expected in (
            ("bad-output-hash", "PCM output"),
            ("malformed-result", "fields"),
            ("mutate-model", "changed during execution"),
        ):
            with self.subTest(mode=mode), TemporaryDirectory() as temporary:
                dependency, process, context, _ = self._fixture(Path(temporary).resolve())
                process.mode = mode
                with self.assertRaisesRegex(NativeSampleInferenceError, expected):
                    await dependency.run_stage(
                        JobStatus.GENERATING_SAMPLES, context, asyncio.Event()
                    )

    async def test_total_output_limits_fail_closed_before_manifest_publication(self) -> None:
        for limits in (
            NativeSampleInferenceLimits(max_total_output_bytes=44),
            NativeSampleInferenceLimits(max_total_output_duration_seconds=0.001),
        ):
            with self.subTest(limits=limits), TemporaryDirectory() as temporary:
                dependency, _, context, _ = self._fixture(Path(temporary).resolve())
                dependency.limits = limits
                with self.assertRaisesRegex(NativeSampleInferenceError, "total limits"):
                    await dependency.run_stage(
                        JobStatus.GENERATING_SAMPLES,
                        context,
                        asyncio.Event(),
                    )
                self.assertFalse(
                    (context.workspace.outputs / "samples/inference_manifest.json").exists()
                )

    async def test_wrong_input_hash_and_symlink_are_rejected_before_process(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            dependency, process, context, _ = self._fixture(root)
            input_path = context.workspace.inputs / "test_set/test-item-1.wav"
            input_path.write_bytes(b"changed")
            input_path.chmod(0o600)
            with self.assertRaises(NativeSampleInferenceError):
                await dependency.run_stage(JobStatus.GENERATING_SAMPLES, context, asyncio.Event())
            self.assertFalse(process.specs)

        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            dependency, process, context, _ = self._fixture(root)
            model_parent = context.workspace.outputs / "model"
            relocated = context.workspace.outputs / "relocated-model"
            model_parent.rename(relocated)
            model_parent.symlink_to(relocated, target_is_directory=True)
            with self.assertRaisesRegex(NativeSampleInferenceError, "directory"):
                await dependency.run_stage(JobStatus.GENERATING_SAMPLES, context, asyncio.Event())
            self.assertFalse(process.specs)

        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            dependency, process, context, _ = self._fixture(root)
            input_path = context.workspace.inputs / "test_set/test-item-1.wav"
            outside = root / "outside.wav"
            outside.write_bytes(_pcm_wave_bytes(sample_rate=8_000))
            input_path.unlink()
            input_path.symlink_to(outside)
            with self.assertRaises(NativeSampleInferenceError):
                await dependency.run_stage(JobStatus.GENERATING_SAMPLES, context, asyncio.Event())
            self.assertFalse(process.specs)

    async def test_timeout_and_precancel_propagate_without_manifest(self) -> None:
        with TemporaryDirectory() as temporary:
            dependency, process, context, _ = self._fixture(Path(temporary).resolve())
            process.mode = "timeout"
            with self.assertRaises(ProcessTimedOut):
                await dependency.run_stage(JobStatus.GENERATING_SAMPLES, context, asyncio.Event())
            self.assertFalse(
                (context.workspace.outputs / "samples/inference_manifest.json").exists()
            )

        with TemporaryDirectory() as temporary:
            dependency, process, context, _ = self._fixture(Path(temporary).resolve())
            cancellation = asyncio.Event()
            cancellation.set()
            with self.assertRaises(asyncio.CancelledError):
                await dependency.run_stage(JobStatus.GENERATING_SAMPLES, context, cancellation)
            self.assertFalse(process.specs)

    async def test_crepe_uses_only_projected_asset_and_reaches_process(self) -> None:
        with TemporaryDirectory() as temporary:
            dependency, process, context, _ = self._fixture(
                Path(temporary).resolve(), f0_method="crepe"
            )
            await dependency.run_stage(JobStatus.GENERATING_SAMPLES, context, asyncio.Event())
            self.assertEqual(len(process.specs), 1)
            request_path = Path(process.specs[0].argv[process.specs[0].argv.index("--request") + 1])
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(request["crepe_model"]),
                {
                    "asset_size_bytes",
                    "asset_sha256",
                    "weights_only",
                    "model_capacity",
                },
            )
            self.assertTrue(request["crepe_model"]["weights_only"])
            self.assertEqual(request["crepe_model"]["model_capacity"], "full")
            self.assertNotIn("path", request["crepe_model"])
            _validate_request_shape(request)
            untrusted_path_request = json.loads(json.dumps(request))
            untrusted_path_request["crepe_model"]["path"] = "/tmp/full.pth"
            with self.assertRaisesRegex(NativeInferenceDriverError, "evidence"):
                _validate_request_shape(untrusted_path_request)
            self.assertNotIn("TORCH_FORCE_WEIGHTS_ONLY_LOAD", process.specs[0].env)  # type: ignore[operator]
            publication = dependency.load_publication(context)
            self.assertEqual(
                publication.crepe_model,
                NativeCrepeModelEvidence(
                    asset_size_bytes=request["crepe_model"]["asset_size_bytes"],
                    asset_sha256=request["crepe_model"]["asset_sha256"],
                    weights_only=True,
                    model_capacity="full",
                ),
            )

    async def test_crepe_projection_missing_tampered_extra_and_unprojected_fail_closed(
        self,
    ) -> None:
        for mode in ("missing", "tampered", "extra", "unprojected"):
            with self.subTest(mode=mode), TemporaryDirectory() as temporary:
                dependency, process, context, _ = self._fixture(
                    Path(temporary).resolve(), f0_method="crepe"
                )
                asset = context.rvc_root / "runtime/crepe/full.pth"
                marker = context.rvc_root / ".orchestrator-projection.json"
                if mode == "missing":
                    asset.unlink()
                elif mode == "tampered":
                    asset.chmod(0o644)
                    asset.write_bytes(b"tampered-crepe-state")
                    asset.chmod(0o444)
                elif mode == "extra":
                    extra = asset.parent / "unreviewed.pth"
                    extra.write_bytes(b"extra")
                    extra.chmod(0o444)
                else:
                    document = json.loads(marker.read_text(encoding="utf-8"))
                    document["files"] = []
                    marker.chmod(0o644)
                    marker.write_text(
                        json.dumps(document, sort_keys=True, separators=(",", ":")),
                        encoding="utf-8",
                    )
                    marker.chmod(0o444)
                with self.assertRaisesRegex(NativeSampleInferenceError, "CREPE"):
                    await dependency.run_stage(
                        JobStatus.GENERATING_SAMPLES,
                        context,
                        asyncio.Event(),
                    )
                self.assertFalse(process.specs)

    async def test_crepe_result_and_replay_evidence_tamper_are_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            dependency, process, context, _ = self._fixture(
                Path(temporary).resolve(), f0_method="crepe"
            )
            process.mode = "bad-crepe-evidence"
            with self.assertRaisesRegex(NativeSampleInferenceError, "runtime evidence"):
                await dependency.run_stage(
                    JobStatus.GENERATING_SAMPLES,
                    context,
                    asyncio.Event(),
                )

        with TemporaryDirectory() as temporary:
            dependency, process, context, _ = self._fixture(
                Path(temporary).resolve(), f0_method="crepe"
            )
            process.mode = "mutate-crepe-asset"
            with self.assertRaisesRegex(NativeSampleInferenceError, "changed"):
                await dependency.run_stage(
                    JobStatus.GENERATING_SAMPLES,
                    context,
                    asyncio.Event(),
                )
            self.assertFalse(
                (context.workspace.outputs / "samples/inference_manifest.json").exists()
            )

        with TemporaryDirectory() as temporary:
            dependency, _, context, _ = self._fixture(Path(temporary).resolve(), f0_method="crepe")
            await dependency.run_stage(
                JobStatus.GENERATING_SAMPLES,
                context,
                asyncio.Event(),
            )
            manifest = context.workspace.outputs / "samples/inference_manifest.json"
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["runtime_evidence"]["crepe_model"]["asset_sha256"] = "f" * 64
            manifest.chmod(0o600)
            manifest.write_text(
                json.dumps(document, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(NativeSampleInferenceError, "runtime binding"):
                await dependency.run_stage(
                    JobStatus.GENERATING_SAMPLES,
                    context,
                    asyncio.Event(),
                )

    def test_limits_runtime_digest_command_and_torch_version_are_strict(self) -> None:
        with self.assertRaises(ValueError):
            NativeSampleInferenceLimits(max_output_bytes=256 * 1024**2 + 1)
        with self.assertRaises(ValueError):
            NativeSampleInferenceLimits(max_output_duration_seconds=600.1)
        with self.assertRaises(ValueError):
            NativeSampleInferenceLimits(max_total_output_bytes=2 * 1024**3 + 1)
        with self.assertRaises(ValueError):
            NativeSampleInferenceLimits(max_total_output_duration_seconds=3_600.1)
        with self.assertRaises(ValueError):
            NativeSampleInferenceLimits(max_items=129)
        with self.assertRaises(ValueError):
            NativeSampleInferenceLimits(max_crepe_asset_bytes=True)
        with self.assertRaisesRegex(NativeSampleInferenceError, "image digest"):
            NativeFixedTestSetInferenceDependency(runtime_image_digest="latest")
        with self.assertRaisesRegex(NativeSampleInferenceError, "asset manifest digest"):
            NativeFixedTestSetInferenceDependency(
                runtime_image_digest=_IMAGE_DIGEST,
                expected_asset_manifest_sha256="latest",
            )
        asset_bound_dependency = NativeFixedTestSetInferenceDependency(
            runtime_image_digest=_IMAGE_DIGEST,
            expected_asset_manifest_sha256="d" * 64,
        )
        with self.assertRaisesRegex(NativeSampleInferenceError, "release activation"):
            asset_bound_dependency.bind_native_runtime(
                NativeSampleInferenceBinding(
                    rvc_commit_hash="7ef19867780cf703841ebafb565a4e47d1ea86ff",
                    asset_manifest_sha256="b" * 64,
                    projection_manifest_sha256="c" * 64,
                    python_executable="/opt/rvc/bin/python",
                    device="cpu",
                    use_half=False,
                )
            )

        command = build_native_sample_inference_command(
            "/opt/rvc/bin/python",
            Path("/attempt/work/request.json"),
            Path("/attempt/work/result.json"),
            "d" * 64,
        )
        self.assertEqual(command[1:3], ("-m", "rvc_worker.native_inference_driver"))
        self.assertNotIn("infer-web.py", command)
        self.assertNotIn("tools/infer_cli.py", command)
        self.assertEqual(require_supported_torch_version("2.6.0+cu124"), (2, 6))
        with self.assertRaises(NativeInferenceDriverError):
            require_supported_torch_version("2.5.1")

    def test_f0_config_validation_accepts_only_explicit_methods(self) -> None:
        base = {
            "transpose": 0,
            "index_rate": 0,
            "filter_radius": 3,
            "resample_sr": 0,
            "rms_mix_rate": 0.25,
            "protect": 0.33,
        }
        for method in ("pm", "harvest", "rmvpe", "crepe"):
            parsed = _validate_inference({**base, "inference_f0_method": method})
            self.assertEqual(parsed["inference_f0_method"], method)
        with self.assertRaisesRegex(NativeInferenceDriverError, "config fields"):
            _validate_inference({**base, "inference_f0_method": "fallback"})

    def test_pcm_v2_integer_rail_metrics_for_all_supported_widths(self) -> None:
        for width in (1, 2, 3, 4):
            with self.subTest(sample_width_bytes=width):
                scale = 1 << (width * 8 - 1)
                metrics = _integer_pcm_metrics(
                    [-scale, 0, scale - 1],
                    sample_width_bytes=width,
                )
                self.assertEqual(metrics["peak_amplitude"], 1.0)
                self.assertAlmostEqual(metrics["clipping_ratio"], 2 / 3)
                self.assertAlmostEqual(metrics["silence_ratio"], 1 / 3)
                expected_rms = math.sqrt((1.0 + ((scale - 1) / scale) ** 2) / 3)
                self.assertAlmostEqual(metrics["rms"], expected_rms)

    def test_silent_upstream_index_fallback_is_rejected(self) -> None:
        with self.assertRaisesRegex(NativeInferenceDriverError, "retrieval usage"):
            _require_faiss_usage((0, 0, 0), (1, 0, 0), index_enabled=True)
        with self.assertRaisesRegex(NativeInferenceDriverError, "index_rate=0"):
            _require_faiss_usage((0, 0, 0), (1, 1, 1), index_enabled=False)
        _require_faiss_usage((0, 0, 0), (1, 1, 2), index_enabled=True)

    def test_reviewed_model_configs_are_exact_for_version_and_rate(self) -> None:
        expected_upsampling = {
            ("v1", "40k"): ([10, 10, 2, 2], [16, 16, 4, 4]),
            ("v2", "40k"): ([10, 10, 2, 2], [16, 16, 4, 4]),
            ("v1", "48k"): ([10, 6, 2, 2, 2], [16, 16, 4, 4, 4]),
            ("v2", "48k"): ([12, 10, 2, 2], [24, 20, 4, 4]),
        }
        for (version, rate), (upsample_rates, kernels) in expected_upsampling.items():
            with self.subTest(version=version, rate=rate):
                config = _expected_model_config(version, rate)
                self.assertEqual(len(config), 18)
                self.assertEqual(config[12], upsample_rates)
                self.assertEqual(config[14], kernels)
                self.assertEqual(config[-3], 109)
                self.assertEqual(config[-2], 256)
                self.assertEqual(config[-1], 40_000 if rate == "40k" else 48_000)

    def test_projected_python_source_is_rehashed_before_use(self) -> None:
        with TemporaryDirectory() as temporary:
            projection = Path(temporary).resolve()
            source = projection / "infer/example.py"
            source.parent.mkdir()
            source.write_text("VALUE = 1\n", encoding="utf-8")
            expected = DriverFileEvidence(
                size_bytes=source.stat().st_size,
                sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            )
            _verify_projection_python_sources(
                projection,
                {"infer/example.py": expected},
            )
            source.write_text("VALUE = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(NativeInferenceDriverError, "Python source"):
                _verify_projection_python_sources(
                    projection,
                    {"infer/example.py": expected},
                )

    def test_operator_asset_torch_load_reads_verified_descriptor(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            asset = root / "asset.pt"
            asset.write_bytes(b"reviewed-asset")
            asset.chmod(0o444)
            expected = DriverFileEvidence(
                size_bytes=asset.stat().st_size,
                sha256=hashlib.sha256(asset.read_bytes()).hexdigest(),
            )

            def replace_path_then_read(source: Any, **kwargs: Any) -> bytes:
                self.assertFalse(kwargs["weights_only"])
                replacement = root / "replacement.pt"
                replacement.write_bytes(b"unreviewed-byte")
                replacement.replace(asset)
                return source.read()

            guarded = _guarded_torch_load(
                replace_path_then_read,
                {
                    asset: DriverTorchLoadPolicy(
                        evidence=expected,
                        weights_only=False,
                        required_mode=0o444,
                    )
                },
            )
            self.assertEqual(guarded(str(asset)), b"reviewed-asset")
            with self.assertRaisesRegex(NativeInferenceDriverError, "trust mode"):
                guarded(str(asset), weights_only=True)

    def test_crepe_prebind_uses_strict_weights_only_verified_stream(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            asset = root / "full.pth"
            asset.write_bytes(b"reviewed-crepe-state")
            asset.chmod(0o444)
            expected = DriverFileEvidence(
                size_bytes=asset.stat().st_size,
                sha256=hashlib.sha256(asset.read_bytes()).hexdigest(),
            )
            load_calls: list[dict[str, Any]] = []

            def load_verified(source: Any, **kwargs: Any) -> dict[str, object]:
                load_calls.append(dict(kwargs))
                self.assertEqual(source.read(), b"reviewed-crepe-state")
                return {"layer.weight": object()}

            guarded = _guarded_torch_load(
                load_verified,
                {
                    asset: DriverTorchLoadPolicy(
                        evidence=expected,
                        weights_only=True,
                        required_mode=0o444,
                        map_location="cpu",
                    )
                },
            )

            class FakeCrepeModel:
                def __init__(self, capacity: str) -> None:
                    self.capacity = capacity
                    self.strict: bool | None = None
                    self.device: str | None = None
                    self.evaluated = False

                def load_state_dict(self, state_dict: Any, *, strict: bool) -> SimpleNamespace:
                    self.strict = strict
                    self.state_dict = state_dict
                    return SimpleNamespace(missing_keys=(), unexpected_keys=())

                def eval(self) -> FakeCrepeModel:
                    self.evaluated = True
                    return self

                def to(self, device: str) -> FakeCrepeModel:
                    self.device = device
                    return self

            infer = SimpleNamespace()
            torchcrepe = SimpleNamespace(Crepe=FakeCrepeModel, infer=infer)
            model = _prebind_crepe_model(torchcrepe, guarded, asset, "cpu")
            self.assertEqual(model.capacity, "full")
            self.assertTrue(model.strict)
            self.assertTrue(model.evaluated)
            self.assertEqual(model.device, "cpu")
            self.assertIs(infer.model, model)
            self.assertEqual(infer.capacity, "full")
            self.assertEqual(
                load_calls,
                [{"map_location": "cpu", "weights_only": True}],
            )
            with self.assertRaisesRegex(NativeInferenceDriverError, "trust mode"):
                guarded(str(asset), map_location="cpu", weights_only=False)
            with self.assertRaisesRegex(NativeInferenceDriverError, "unreviewed"):
                guarded(str(root / "other.pth"), map_location="cpu", weights_only=True)


def _sample_claim(
    input_bytes: bytes,
    *,
    index_rate: float,
    f0_method: str,
) -> JobClaim:
    payload = make_claim(samples=True).model_dump(mode="json")
    payload["config"]["auto_inference_samples"].update(
        {"index_rate": index_rate, "inference_f0_method": f0_method}
    )
    inference = InferencePresetConfig.model_validate(
        {
            key: value
            for key, value in payload["config"]["auto_inference_samples"].items()
            if key not in {"enabled", "test_set_id"}
        }
    )
    canonical = json.dumps(
        inference.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    payload["test_set_transfer"]["inference_config"] = inference.model_dump(mode="json")
    payload["test_set_transfer"]["inference_config_sha256"] = hashlib.sha256(canonical).hexdigest()
    item = payload["test_set_transfer"]["items"][0]
    item.update(
        {
            "size_bytes": len(input_bytes),
            "sha256": hashlib.sha256(input_bytes).hexdigest(),
            "sample_rate_hz": 8_000,
            "channels": 1,
            "duration_seconds": 0.01,
        }
    )
    payload["config_sha256"] = job_config_sha256(JobConfig.model_validate(payload["config"]))
    return JobClaim.model_validate(payload)


def _write_transfer_marker(context: RvcRunContext) -> None:
    transfer = context.claim.test_set_transfer
    assert transfer is not None
    marker = context.workspace.outputs / "test_set_transfer.json"
    document = {
        "schema_version": 1,
        "test_set_id": transfer.test_set_id,
        "family_id": transfer.family_id,
        "revision": transfer.revision,
        "manifest_sha256": transfer.manifest_sha256,
        "sample_plan_sha256": transfer.sample_plan_sha256,
        "sample_plan_revalidation": "manager_claim_snapshot",
        "inference_config": transfer.inference_config.model_dump(mode="json"),
        "inference_config_sha256": transfer.inference_config_sha256,
        "items": [
            {
                "test_set_item_id": item.test_set_item_id,
                "item_key": item.item_key,
                "sort_order": item.sort_order,
                "filename": item.filename,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "sample_rate_hz": item.sample_rate_hz,
                "channels": item.channels,
                "duration_seconds": item.duration_seconds,
            }
            for item in transfer.items
        ],
    }
    marker.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    marker.chmod(0o600)


def _pcm_wave_bytes(*, sample_rate: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes((b"\x00\x00\x00\x10\x00\xf0\x00\x00") * 20)
    return output.getvalue()


def _write_pcm_wave(path: Path, *, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(_pcm_wave_bytes(sample_rate=sample_rate))
    path.chmod(0o600)


if __name__ == "__main__":
    unittest.main()
