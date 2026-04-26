from __future__ import annotations

import asyncio
import hashlib
import json
import unittest
from collections.abc import Awaitable
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from rvc_orchestrator_contracts import JobStatus, TrainingF0Method
from rvc_worker.artifacts import ArtifactDiscoveryError
from rvc_worker.datasets import DatasetStageRunner
from rvc_worker.native_inference import NativeFixedTestSetInferenceDependency
from rvc_worker.native_runner import (
    NativeRvcRunnerError,
    NativeRvcRuntime,
    PinnedRvcRunner,
)
from rvc_worker.process import (
    OutputCallback,
    ProcessCancelled,
    ProcessResult,
    ProcessSpec,
    ProcessTimedOut,
)
from rvc_worker.runner import RvcRunContext, StageResult, create_runner
from rvc_worker.rvc_commands import RVC_REVIEWED_COMMIT
from rvc_worker.stages import StageExecutor
from rvc_worker.training_metrics import ParsedTrainingMetric
from rvc_worker.workspace import WorkspaceManager

from .helpers import make_claim


class FakeNativeProcessRunner:
    def __init__(self, *, write_small_model: bool = True) -> None:
        self.write_small_model = write_small_model
        self.specs: list[ProcessSpec] = []
        self.fail_matching: str | None = None
        self.wait_matching: str | None = None
        self.cancelled_waiter = False

    async def run(
        self,
        spec: ProcessSpec,
        cancellation: asyncio.Event,
        *,
        output_callback: OutputCallback | None = None,
    ) -> ProcessResult:
        self.specs.append(spec)
        rendered = " ".join(spec.argv)
        spec.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        spec.stdout_path.write_text("fake stdout\n", encoding="utf-8")
        spec.stderr_path.write_text("fake stderr\n", encoding="utf-8")
        if self.fail_matching and self.fail_matching in rendered:
            raise ProcessTimedOut("injected stage timeout")
        if self.wait_matching and self.wait_matching in rendered:
            await cancellation.wait()
            self.cancelled_waiter = True
            raise ProcessCancelled("peer failed or job was cancelled")
        if cancellation.is_set():
            raise ProcessCancelled("cancelled before fake stage")
        self._produce_outputs(spec)
        if output_callback is not None:
            response = output_callback("stdout", b"INFO Train Epoch: 2 [50%]\n")
            if isinstance(response, Awaitable):
                await response
            response = output_callback(
                "stdout",
                b"INFO [10, 0.0001]\nINFO loss_disc=1.0, loss_gen=2.0, ",
            )
            if isinstance(response, Awaitable):
                await response
            response = output_callback("stdout", b"loss_fm=3.0, loss_mel=4.0, loss_kl=5.0\n")
            if isinstance(response, Awaitable):
                await response
        return ProcessResult(spec.argv, 0, spec.stdout_path, spec.stderr_path)

    def _produce_outputs(self, spec: ProcessSpec) -> None:
        argv = spec.argv
        if argv[1].endswith("preprocess.py"):
            experiment = Path(argv[5])
            _write(experiment / "0_gt_wavs/voice.wav")
            _write(experiment / "1_16k_wavs/voice.wav")
            return
        if argv[1].endswith("extract_f0_print.py"):
            _write_f0_pair(Path(argv[2]), "voice")
            return
        if argv[1].endswith("extract_f0_rmvpe.py"):
            _write_f0_pair(Path(argv[5]), f"voice-{argv[3]}")
            return
        if argv[1].endswith("extract_feature_print.py"):
            experiment = Path(argv[6])
            version = argv[7]
            dimension = "256" if version == "v1" else "768"
            _write(experiment / f"3_feature{dimension}/voice.npy")
            return
        if argv[1].endswith("train.py"):
            experiment_name = argv[argv.index("-e") + 1]
            experiment = spec.cwd / "logs" / experiment_name
            _write(experiment / "G_2.pth", "generator")
            _write(experiment / "D_2.pth", "discriminator")
            _write(
                experiment / "train.log",
                "INFO Train Epoch: 2 [100%]\n"
                "INFO [10, 0.0001]\n"
                "INFO loss_disc=1.0, loss_gen=2.0, loss_fm=3.0, "
                "loss_mel=4.0, loss_kl=5.0\n",
            )
            if self.write_small_model:
                _write(spec.cwd / "assets/weights" / f"{experiment_name}.pth", "small")
            return
        if len(argv) >= 3 and argv[1:3] == ("-m", "rvc_worker.index_builder"):
            experiment = Path(argv[argv.index("--experiment-directory") + 1])
            version = argv[argv.index("--version") + 1]
            _write(experiment / "total_fea.npy", "features")
            _write(experiment / f"added_IVF1_Flat_voice_{version}.index", "index")
            return
        if len(argv) >= 3 and argv[1:3] == ("-m", "rvc_worker.small_model"):
            output = Path(argv[argv.index("--output") + 1])
            _write(output, "official-small-model")


class BlockingTrainingProcessRunner(FakeNativeProcessRunner):
    def __init__(self) -> None:
        super().__init__()
        self.training_started = asyncio.Event()
        self.release_training = asyncio.Event()

    async def run(
        self,
        spec: ProcessSpec,
        cancellation: asyncio.Event,
        *,
        output_callback: OutputCallback | None = None,
    ) -> ProcessResult:
        if not spec.argv[1].endswith("train.py"):
            return await super().run(
                spec,
                cancellation,
                output_callback=output_callback,
            )
        self.specs.append(spec)
        spec.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        spec.stdout_path.write_text("training is still running\n", encoding="utf-8")
        spec.stderr_path.write_text("", encoding="utf-8")
        self._produce_outputs(spec)
        self.training_started.set()
        release_task = asyncio.create_task(self.release_training.wait())
        cancellation_task = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                {release_task, cancellation_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_task in done:
                raise ProcessCancelled("cancelled blocking training fixture")
        finally:
            release_task.cancel()
            cancellation_task.cancel()
            await asyncio.gather(
                release_task,
                cancellation_task,
                return_exceptions=True,
            )
        return ProcessResult(spec.argv, 0, spec.stdout_path, spec.stderr_path)


class OutsideDependency:
    def __init__(self, outside: Path) -> None:
        self.outside = outside

    async def run_stage(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        _write(self.outside, stage.value)
        return StageResult((self.outside,))


class FixtureDatasetMaterializer:
    async def run_stage(
        self,
        stage: JobStatus,
        context: RvcRunContext,
        cancellation: asyncio.Event,
    ) -> StageResult:
        if cancellation.is_set():
            raise asyncio.CancelledError
        if stage is JobStatus.DOWNLOADING_DATASET:
            archive = context.workspace.inputs / "prepared_flat.zip"
            _write(archive, "verified-archive-fixture")
            return StageResult((archive,))
        if stage is JobStatus.VALIDATING_DATASET:
            report = context.workspace.outputs / "dataset_report.json"
            _write(report, "{}")
            return StageResult((report,))
        if stage is JobStatus.PREPARING_FLAT_DATASET:
            audio = context.workspace.inputs / "prepared_flat/000001.wav"
            _write(audio, "audio")
            return StageResult((audio,))
        raise AssertionError(stage)


class RecordingTrainingTelemetrySink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.finished = 0
        self.train_log_seen = asyncio.Event()

    async def record_training_event(
        self,
        *,
        source: str,
        event_key: str,
        message: str | None,
        metrics: tuple[ParsedTrainingMetric, ...] = (),
        channel: str | None = None,
    ) -> None:
        self.events.append(
            {
                "source": source,
                "event_key": event_key,
                "message": message,
                "metrics": metrics,
                "channel": channel,
            }
        )
        if source == "train_log":
            self.train_log_seen.set()

    async def finish_training(self) -> None:
        self.finished += 1


class NativeRunnerTests(unittest.IsolatedAsyncioTestCase):
    def _fixture(
        self,
        root: Path,
        *,
        version: str = "v2",
        use_f0: bool = True,
        process_runner: FakeNativeProcessRunner | None = None,
    ) -> tuple[PinnedRvcRunner, FakeNativeProcessRunner, RvcRunContext]:
        source = root / "reviewed-rvc"
        _make_source_tree(source)
        asset_manifest = _write_asset_manifest(source)
        _write_projection_manifest(source, asset_manifest)
        worker_process = process_runner or FakeNativeProcessRunner()
        runner = PinnedRvcRunner(
            NativeRvcRuntime(
                source_root=source,
                asset_manifest_path=asset_manifest,
                python_executable="/opt/rvc-python/bin/python",
                cpu_workers=2,
                available_gpu_ids=(0, 1),
            ),
            process_runner=worker_process,
            revision_reader=lambda _: RVC_REVIEWED_COMMIT,
        )
        workspace = WorkspaceManager(root / "jobs").prepare("job", "attempt")
        claim = make_claim(version=version, use_f0=use_f0)
        context = RvcRunContext(claim, workspace)
        _write(workspace.inputs / "prepared_flat/voice.wav", "audio")
        return runner, worker_process, context

    async def test_v1_no_f0_runs_typed_stages_without_writing_shared_source(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            runner, process, context = self._fixture(root, version="v1", use_f0=False)
            source_snapshot = _tree_hashes(root / "reviewed-rvc")

            await runner.run_stage(JobStatus.PREPROCESSING, context, asyncio.Event())
            await runner.run_stage(JobStatus.EXTRACTING_FEATURES, context, asyncio.Event())
            training = await runner.run_stage(JobStatus.TRAINING, context, asyncio.Event())
            checkpoint = await runner.run_stage(
                JobStatus.SAVING_CHECKPOINT, context, asyncio.Event()
            )
            await runner.run_stage(JobStatus.BUILDING_INDEX, context, asyncio.Event())
            collected = await runner.run_stage(
                JobStatus.COLLECTING_SMALL_MODEL, context, asyncio.Event()
            )
            manifest = await runner.run_stage(
                JobStatus.UPLOADING_ARTIFACTS, context, asyncio.Event()
            )

            self.assertEqual(source_snapshot, _tree_hashes(root / "reviewed-rvc"))
            self.assertFalse((root / "reviewed-rvc/logs/speaker-a-run-1").exists())
            self.assertFalse((root / "reviewed-rvc/assets/weights").exists())
            self.assertEqual(checkpoint.metadata["epoch"], 2)  # type: ignore[index]
            self.assertTrue(
                any(path.name == "final_small_model.pth" for path in collected.created_paths)
            )
            self.assertTrue(
                any(path.name == "artifact_manifest.json" for path in manifest.created_paths)
            )
            metrics = training.metadata["metrics"]  # type: ignore[index]
            self.assertTrue(any(item["key"] == "loss_g_total" for item in metrics))
            self.assertTrue(all(spec.cwd == context.rvc_root for spec in process.specs))
            self.assertTrue(
                all(str(root / "reviewed-rvc") not in " ".join(spec.argv) for spec in process.specs)
            )

    async def test_v2_multi_shard_f0_and_features_are_aligned(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            runner, process, context = self._fixture(root)
            config = context.claim.config.model_copy(deep=True)
            config.f0_extraction.training_f0_method = TrainingF0Method.RMVPE_GPU
            config.f0_extraction.rmvpe_gpu_ids = [0, 1]
            config.training.gpu_ids = [0, 1]
            context = RvcRunContext(
                context.claim.model_copy(update={"config": type(config).model_validate(config)}),
                context.workspace,
            )
            await runner.run_stage(JobStatus.PREPROCESSING, context, asyncio.Event())
            f0 = await runner.run_stage(JobStatus.EXTRACTING_F0, context, asyncio.Event())
            features = await runner.run_stage(
                JobStatus.EXTRACTING_FEATURES, context, asyncio.Event()
            )

            self.assertEqual(f0.metadata["command_count"], 2)  # type: ignore[index]
            self.assertEqual(features.metadata["feature_dimension"], 768)  # type: ignore[index]
            rmvpe_specs = [
                spec for spec in process.specs if spec.argv[1].endswith("extract_f0_rmvpe.py")
            ]
            self.assertEqual(len(rmvpe_specs), 2)

    async def test_live_training_sink_observes_stdout_train_log_and_tensorboard(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            runner, _, context = self._fixture(root)
            sink = RecordingTrainingTelemetrySink()
            runner.bind_training_telemetry(context.claim, sink)
            await runner.run_stage(JobStatus.PREPROCESSING, context, asyncio.Event())
            await runner.run_stage(JobStatus.EXTRACTING_F0, context, asyncio.Event())
            await runner.run_stage(
                JobStatus.EXTRACTING_FEATURES,
                context,
                asyncio.Event(),
            )
            event_file = context.experiment_logs / "events.out.tfevents.fixture"
            _write(event_file, "bounded fixture")
            tensorboard_metric = ParsedTrainingMetric(
                key="loss_g_total",
                value=9.5,
                step=10,
                source="tensorboard",
            )

            with patch(
                "rvc_worker.native_runner.read_tensorboard_scalars",
                return_value=(tensorboard_metric,),
            ):
                await runner.run_stage(JobStatus.TRAINING, context, asyncio.Event())
            runner.bind_training_telemetry(None, None)

            sources = {str(event["source"]) for event in sink.events}
            self.assertEqual(sources, {"stdout", "train_log", "tensorboard"})
            self.assertEqual(sink.finished, 1)
            self.assertTrue(
                any(
                    metric.key == "current_epoch"
                    for event in sink.events
                    for metric in event["metrics"]  # type: ignore[union-attr]
                )
            )
            self.assertEqual(
                len({str(event["event_key"]) for event in sink.events}),
                len(sink.events),
            )

    async def test_train_log_is_emitted_while_training_process_is_still_running(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            process = BlockingTrainingProcessRunner()
            runner, _, context = self._fixture(root, process_runner=process)
            sink = RecordingTrainingTelemetrySink()
            runner.bind_training_telemetry(context.claim, sink)
            await runner.run_stage(JobStatus.PREPROCESSING, context, asyncio.Event())
            await runner.run_stage(JobStatus.EXTRACTING_F0, context, asyncio.Event())
            await runner.run_stage(
                JobStatus.EXTRACTING_FEATURES,
                context,
                asyncio.Event(),
            )

            training = asyncio.create_task(
                runner.run_stage(JobStatus.TRAINING, context, asyncio.Event())
            )
            await asyncio.wait_for(process.training_started.wait(), timeout=1)
            await asyncio.wait_for(sink.train_log_seen.wait(), timeout=2)

            self.assertFalse(training.done())
            process.release_training.set()
            await asyncio.wait_for(training, timeout=2)
            runner.bind_training_telemetry(None, None)

    async def test_training_sink_rejects_a_different_claim_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            runner, _, context = self._fixture(root)
            sink = RecordingTrainingTelemetrySink()
            runner.bind_training_telemetry(context.claim, sink)
            mismatched = context.claim.model_copy(update={"attempt_id": "other-attempt"})

            with self.assertRaisesRegex(NativeRvcRunnerError, "current claim"):
                runner._training_telemetry_sink(  # noqa: SLF001
                    RvcRunContext(mismatched, context.workspace)
                )

            runner.bind_training_telemetry(None, None)

    async def test_missing_and_ambiguous_checkpoint_pairs_fail_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            runner, _, context = self._fixture(root)
            await runner.run_stage(JobStatus.PREPROCESSING, context, asyncio.Event())
            _write(context.experiment_logs / "G_2.pth", "generator")
            with self.assertRaisesRegex(ArtifactDiscoveryError, "both generator"):
                await runner.run_stage(JobStatus.SAVING_CHECKPOINT, context, asyncio.Event())

            _write(context.experiment_logs / "D_1.pth", "discriminator")
            with self.assertRaisesRegex(ArtifactDiscoveryError, "unambiguous pair"):
                await runner.run_stage(JobStatus.SAVING_CHECKPOINT, context, asyncio.Event())

    async def test_internal_index_and_official_small_model_fallback_are_invoked(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            process = FakeNativeProcessRunner(write_small_model=False)
            runner, _, context = self._fixture(root, process_runner=process)
            await runner.run_stage(JobStatus.PREPROCESSING, context, asyncio.Event())
            await runner.run_stage(JobStatus.EXTRACTING_F0, context, asyncio.Event())
            await runner.run_stage(JobStatus.EXTRACTING_FEATURES, context, asyncio.Event())
            await runner.run_stage(JobStatus.TRAINING, context, asyncio.Event())
            await runner.run_stage(JobStatus.BUILDING_INDEX, context, asyncio.Event())
            result = await runner.run_stage(
                JobStatus.COLLECTING_SMALL_MODEL, context, asyncio.Event()
            )

            small_specs = [
                spec
                for spec in process.specs
                if len(spec.argv) >= 3 and spec.argv[1:3] == ("-m", "rvc_worker.small_model")
            ]
            self.assertEqual(len(small_specs), 1)
            self.assertIn("--allow-reviewed-projection", small_specs[0].argv)
            self.assertTrue(result.metadata["extracted_from_checkpoint"])  # type: ignore[index]
            self.assertTrue((context.workspace.outputs / "index/final.index").is_file())

    async def test_parallel_failure_cancels_peer_and_timeout_is_preserved(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            runner, process, context = self._fixture(root)
            config = context.claim.config.model_copy(deep=True)
            config.f0_extraction.training_f0_method = TrainingF0Method.RMVPE_GPU
            config.f0_extraction.rmvpe_gpu_ids = [0, 1]
            context = RvcRunContext(
                context.claim.model_copy(update={"config": type(config).model_validate(config)}),
                context.workspace,
            )
            await runner.run_stage(JobStatus.PREPROCESSING, context, asyncio.Event())
            process.fail_matching = " 0 0 "
            process.wait_matching = " 1 1 "
            with self.assertRaises(ProcessTimedOut):
                await runner.run_stage(JobStatus.EXTRACTING_F0, context, asyncio.Event())
            self.assertTrue(process.cancelled_waiter)

    async def test_job_cancellation_and_workspace_escape_are_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            runner, process, context = self._fixture(root)
            await runner.run_stage(JobStatus.PREPROCESSING, context, asyncio.Event())
            process.wait_matching = "extract_feature_print.py"
            cancellation = asyncio.Event()
            task = asyncio.create_task(
                runner.run_stage(JobStatus.EXTRACTING_FEATURES, context, cancellation)
            )
            await asyncio.sleep(0)
            cancellation.set()
            with self.assertRaises(ProcessCancelled):
                await task

            outside = root / "outside-artifact"
            escaped_runner = PinnedRvcRunner(
                runner.runtime,
                process_runner=process,
                stage_dependency=OutsideDependency(outside),
                revision_reader=lambda _: RVC_REVIEWED_COMMIT,
            )
            with self.assertRaisesRegex(Exception, "escapes workspace"):
                await escaped_runner.run_stage(
                    JobStatus.DOWNLOADING_DATASET, context, asyncio.Event()
                )

    async def test_dataset_and_sample_stages_fail_without_reviewed_dependency(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            runner, _, context = self._fixture(root)
            for stage in (
                JobStatus.DOWNLOADING_DATASET,
                JobStatus.VALIDATING_DATASET,
                JobStatus.PREPARING_FLAT_DATASET,
                JobStatus.GENERATING_SAMPLES,
            ):
                with self.subTest(stage=stage):
                    with self.assertRaisesRegex(NativeRvcRunnerError, "explicit reviewed"):
                        await runner.run_stage(stage, context, asyncio.Event())

            sample_claim = make_claim(samples=True)
            sample_context = RvcRunContext(sample_claim, context.workspace)
            with self.assertRaisesRegex(NativeRvcRunnerError, "TestSet"):
                runner.validate_claim(sample_claim, (0, 1))
            for stage in (JobStatus.GENERATING_SAMPLES, JobStatus.EVALUATING):
                with self.subTest(sample_stage=stage):
                    with self.assertRaisesRegex(NativeRvcRunnerError, "explicit reviewed"):
                        await runner.run_stage(stage, sample_context, asyncio.Event())

    async def test_no_sample_job_runs_full_dataset_to_manifest_stage_plan(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            runner, _, context = self._fixture(root, version="v1", use_f0=False)
            wrapped = DatasetStageRunner(runner, FixtureDatasetMaterializer())  # type: ignore[arg-type]
            statuses: list[JobStatus] = []

            async def update_status(job_id: str, update: object) -> None:
                del job_id
                statuses.append(update.status)  # type: ignore[attr-defined]

            summary = await StageExecutor(wrapped, update_status).execute(
                context.claim,
                context.workspace,
                asyncio.Event(),
            )

            self.assertEqual(summary.final_status, JobStatus.COMPLETED)
            self.assertEqual(statuses[-1], JobStatus.COMPLETED)
            self.assertTrue((context.workspace.outputs / "artifact_manifest.json").is_file())
            evaluation = json.loads(
                (context.workspace.outputs / "metrics/evaluation.json").read_text(encoding="utf-8")
            )
            self.assertFalse(evaluation["sample_evaluation_performed"])

    async def test_claim_revalidates_gpu_commit_and_asset_bytes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            runner, _, context = self._fixture(root)
            runner.validate_claim(context.claim, (0, 1))

            mismatch = context.claim.model_copy(deep=True)
            mismatch.config.rvc_backend.rvc_commit_hash = "f" * 40
            with self.assertRaisesRegex(NativeRvcRunnerError, "commit"):
                runner.validate_claim(mismatch, (0, 1))

            with self.assertRaisesRegex(Exception, "not visible"):
                runner.validate_claim(context.claim, (1,))

            asset = root / "reviewed-rvc/assets/hubert/hubert_base.pt"
            asset.write_text("changed-after-start", encoding="utf-8")
            with self.assertRaisesRegex(NativeRvcRunnerError, "checksum"):
                runner.validate_claim(context.claim, (0, 1))

    def test_create_runner_native_uses_fixed_manifest_and_commit(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            source = root / "reviewed-rvc"
            _make_source_tree(source)
            asset_manifest = _write_asset_manifest(source)
            _write_projection_manifest(source, asset_manifest)
            _write(source / ".rvc-reviewed-commit", RVC_REVIEWED_COMMIT)

            runner = create_runner(
                "native",
                native_source_root=source,
                native_python_executable="/opt/rvc-python/bin/python",
            )

            self.assertIsInstance(runner, PinnedRvcRunner)
            self.assertEqual(runner.verified_commit_hash, RVC_REVIEWED_COMMIT)  # type: ignore[attr-defined]
            self.assertIsNone(runner.sample_inference_runtime_evidence)  # type: ignore[attr-defined]

    def test_create_runner_injects_sample_dependency_only_for_qualified_activation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            source = root / "reviewed-rvc"
            _make_source_tree(source)
            asset_manifest = _write_asset_manifest(source)
            _write_projection_manifest(source, asset_manifest)
            _write(source / ".rvc-reviewed-commit", RVC_REVIEWED_COMMIT)
            activation = root / "runtime-activation.json"
            _write_runtime_activation(activation, asset_manifest, qualified=True)

            runner = create_runner(
                "native",
                native_source_root=source,
                native_python_executable="/opt/rvc-python/bin/python",
                runtime_activation_path=activation,
            )

            evidence = runner.sample_inference_runtime_evidence  # type: ignore[attr-defined]
            self.assertIsNotNone(evidence)
            assert evidence is not None
            self.assertEqual(evidence.runtime_image_digest, "sha256:" + "a" * 64)
            self.assertEqual(
                evidence.runtime_asset_manifest_sha256,
                hashlib.sha256(asset_manifest.read_bytes()).hexdigest(),
            )

            activation.chmod(0o644)
            _write_runtime_activation(activation, asset_manifest, qualified=False)
            disabled = create_runner(
                "native",
                native_source_root=source,
                native_python_executable="/opt/rvc-python/bin/python",
                runtime_activation_path=activation,
            )
            self.assertIsNone(  # type: ignore[attr-defined]
                disabled.sample_inference_runtime_evidence
            )

    def test_explicit_bound_sample_dependency_opens_only_runner_claim_gate(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            source = root / "reviewed-rvc"
            _make_source_tree(source)
            asset_manifest = _write_asset_manifest(source)
            _write_projection_manifest(source, asset_manifest)
            dependency = NativeFixedTestSetInferenceDependency(
                runtime_image_digest="sha256:" + "a" * 64
            )
            runner = PinnedRvcRunner(
                NativeRvcRuntime(
                    source_root=source,
                    asset_manifest_path=asset_manifest,
                    python_executable="/opt/rvc-python/bin/python",
                    available_gpu_ids=(0, 1),
                ),
                sample_inference_dependency=dependency,
                revision_reader=lambda _: RVC_REVIEWED_COMMIT,
            )

            runner.validate_claim(make_claim(samples=True), (0, 1))
            evidence = runner.sample_inference_runtime_evidence
            self.assertIsNotNone(evidence)
            assert evidence is not None
            self.assertEqual(evidence.runtime_image_digest, "sha256:" + "a" * 64)
            self.assertEqual(
                evidence.runtime_asset_manifest_sha256,
                runner.asset_manifest_sha256,
            )

    async def test_optional_crepe_asset_is_projected_at_only_the_fixed_path(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            source = root / "reviewed-rvc"
            _make_source_tree(source)
            _write(source / "runtime/crepe/full.pth", "reviewed-crepe-state")
            asset_manifest = _write_asset_manifest(source)
            _write_projection_manifest(source, asset_manifest)
            runner = PinnedRvcRunner(
                NativeRvcRuntime(
                    source_root=source,
                    asset_manifest_path=asset_manifest,
                    python_executable="/opt/rvc-python/bin/python",
                    available_gpu_ids=(0, 1),
                ),
                process_runner=FakeNativeProcessRunner(),
                revision_reader=lambda _: RVC_REVIEWED_COMMIT,
            )
            workspace = WorkspaceManager(root / "jobs").prepare("job", "attempt")
            context = RvcRunContext(make_claim(), workspace)
            _write(workspace.inputs / "prepared_flat/voice.wav", "audio")

            await runner.run_stage(JobStatus.PREPROCESSING, context, asyncio.Event())

            projected = context.rvc_root / "runtime/crepe/full.pth"
            self.assertEqual(projected.read_text(encoding="utf-8"), "reviewed-crepe-state")
            self.assertEqual(projected.stat().st_mode & 0o777, 0o444)
            marker = json.loads(
                (context.rvc_root / ".orchestrator-projection.json").read_text(encoding="utf-8")
            )
            self.assertIn("runtime/crepe", marker["projection_directories"])
            self.assertEqual(
                [
                    item["path"]
                    for item in marker["files"]
                    if item["path"].startswith("runtime/crepe/")
                ],
                ["runtime/crepe/full.pth"],
            )

    def test_optional_crepe_projection_rejects_an_extra_asset(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            source = root / "reviewed-rvc"
            _make_source_tree(source)
            _write(source / "runtime/crepe/full.pth", "reviewed-crepe-state")
            _write(source / "runtime/crepe/extra.pth", "unreviewed-extra")
            asset_manifest = _write_asset_manifest(source)
            _write_projection_manifest(source, asset_manifest)

            with self.assertRaisesRegex(NativeRvcRunnerError, "CREPE"):
                PinnedRvcRunner(
                    NativeRvcRuntime(
                        source_root=source,
                        asset_manifest_path=asset_manifest,
                        python_executable="/opt/rvc-python/bin/python",
                    ),
                    revision_reader=lambda _: RVC_REVIEWED_COMMIT,
                )

    async def test_projection_refuses_source_symlink(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            runner, _, context = self._fixture(root)
            target = root / "outside.py"
            _write(target)
            source_file = root / "reviewed-rvc/infer/modules/train/preprocess.py"
            source_file.unlink()
            source_file.symlink_to(target)
            with self.assertRaisesRegex(NativeRvcRunnerError, "cannot open|manifest"):
                await runner.run_stage(JobStatus.PREPROCESSING, context, asyncio.Event())

    async def test_projection_copy_rejects_mutation_after_claim_validation(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            runner, process, context = self._fixture(root)
            runner.validate_claim(context.claim, (0, 1))
            source = root / "reviewed-rvc/infer/modules/train/preprocess.py"
            source.write_text("# changed after claim validation\n", encoding="utf-8")

            with self.assertRaisesRegex(NativeRvcRunnerError, "manifest"):
                await runner.run_stage(JobStatus.PREPROCESSING, context, asyncio.Event())

            self.assertFalse(process.specs)

    async def test_existing_private_projection_is_reverified_against_build_manifest(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _resolved_path(temporary)
            runner, process, context = self._fixture(root)
            await runner.run_stage(JobStatus.PREPROCESSING, context, asyncio.Event())
            projected = context.rvc_root / "infer/modules/train/train.py"
            original = projected.read_bytes()
            forged = bytes([original[0] ^ 1]) + original[1:]
            projected.chmod(0o644)
            projected.write_bytes(forged)
            projected.chmod(0o444)

            with self.assertRaisesRegex(NativeRvcRunnerError, "bytes changed"):
                await runner.run_stage(JobStatus.EXTRACTING_F0, context, asyncio.Event())

            self.assertEqual(len(process.specs), 1)


def _make_source_tree(source: Path) -> None:
    required = (
        "infer/lib/audio.py",
        "infer/lib/rmvpe.py",
        "infer/lib/infer_pack/models.py",
        "infer/modules/vc/modules.py",
        "infer/modules/vc/pipeline.py",
        "infer/modules/vc/utils.py",
        "infer/modules/train/preprocess.py",
        "infer/modules/train/extract/extract_f0_print.py",
        "infer/modules/train/extract/extract_f0_rmvpe.py",
        "infer/modules/train/extract_feature_print.py",
        "infer/modules/train/train.py",
        "infer/lib/train/process_ckpt.py",
    )
    for relative in required:
        _write(source / relative, "# reviewed fixture")
    for relative in ("configs/v1/40k.json", "configs/v1/48k.json", "configs/v2/48k.json"):
        _write(
            source / relative,
            json.dumps({"train": {"fp16_run": True}, "data": {}, "model": {}}),
        )
    for directory in ("assets/pretrained", "assets/pretrained_v2"):
        for rate in ("40k", "48k"):
            for prefix in ("", "f0"):
                for kind in ("G", "D"):
                    _write(source / directory / f"{prefix}{kind}{rate}.pth", "weight")
    _write(source / "assets/hubert/hubert_base.pt", "hubert")
    _write(source / "assets/rmvpe/rmvpe.pt", "rmvpe")
    for rate in ("40k", "48k"):
        _write(source / f"logs/mute/0_gt_wavs/mute{rate}.wav", "mute")
    for dimension in ("256", "768"):
        _write(source / f"logs/mute/3_feature{dimension}/mute.npy", "mute")
    _write(source / "logs/mute/2a_f0/mute.wav.npy", "mute")
    _write(source / "logs/mute/2b-f0nsf/mute.wav.npy", "mute")
    for tool in ("ffmpeg", "ffprobe"):
        path = source / "runtime/bin" / tool
        _write(path, f"#!/bin/sh\necho {tool}\n")
        path.chmod(0o755)


def _write_asset_manifest(source: Path) -> Path:
    roots = [
        source / "assets/pretrained",
        source / "assets/pretrained_v2",
        source / "assets/hubert",
        source / "assets/rmvpe",
        source / "logs/mute",
        source / "runtime/bin",
    ]
    if (source / "runtime/crepe").is_dir():
        roots.append(source / "runtime/crepe")
    files = sorted(path for root in roots for path in root.rglob("*") if path.is_file())
    records = []
    for path in files:
        relative = path.relative_to(source).as_posix()
        records.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
                "license": "LicenseRef-Test-Reviewed",
                "source": f"https://example.test/assets/{relative}",
                "executable": relative in {"runtime/bin/ffmpeg", "runtime/bin/ffprobe"},
            }
        )
    manifest = source / "assets-manifest.json"
    _write(
        manifest,
        json.dumps(
            {
                "schema_version": 1,
                "kind": "rvc-assets",
                "rvc_commit": RVC_REVIEWED_COMMIT,
                "assets": records,
            }
        ),
    )
    return manifest


def _write_projection_manifest(source: Path, asset_manifest: Path) -> Path:
    source_manifest = source / "source-manifest.json"
    _write(
        source_manifest,
        json.dumps(
            {
                "schema_version": 1,
                "kind": "rvc-source",
                "repository": (
                    "https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI"
                ),
                "commit": RVC_REVIEWED_COMMIT,
                "archive": {},
                "license": {},
            },
            sort_keys=True,
        ),
    )
    directories = [
        "infer",
        "configs",
        "assets/pretrained",
        "assets/pretrained_v2",
        "assets/hubert",
        "assets/rmvpe",
        "logs/mute",
    ]
    if (source / "runtime/crepe").is_dir():
        directories.append("runtime/crepe")
    suffixes = {
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
    files = sorted(
        path
        for directory in directories
        for path in (source / directory).rglob("*")
        if path.is_file() and path.suffix.lower() in suffixes
    )
    records = [
        {
            "path": path.relative_to(source).as_posix(),
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": path.stat().st_mode & 0o7777,
        }
        for path in files
    ]
    manifest = source / "projection-manifest.json"
    _write(
        manifest,
        json.dumps(
            {
                "schema_version": 1,
                "kind": "rvc-projection-inputs",
                "rvc_commit": RVC_REVIEWED_COMMIT,
                "source_manifest_sha256": hashlib.sha256(source_manifest.read_bytes()).hexdigest(),
                "asset_manifest_sha256": hashlib.sha256(asset_manifest.read_bytes()).hexdigest(),
                "files": records,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )
    _write(
        source / "projection-manifest.sha256",
        hashlib.sha256(manifest.read_bytes()).hexdigest() + "\n",
    )
    return manifest


def _write_runtime_activation(
    path: Path,
    asset_manifest: Path,
    *,
    qualified: bool,
) -> None:
    document = {
        "format_version": 1,
        "kind": "rvc-runtime-activation",
        "runtime_image_digest": "sha256:" + "a" * 64 if qualified else None,
        "runtime_asset_manifest_sha256": (
            hashlib.sha256(asset_manifest.read_bytes()).hexdigest() if qualified else None
        ),
        "qualification_evidence_sha256": "b" * 64 if qualified else None,
        "gpu_smoke_verified": qualified,
        "profile_stage_set_verified": qualified,
        "native_sample_inference_verified": qualified,
        "supported_inference_f0_methods": (
            ["pm", "harvest", "crepe", "rmvpe"] if qualified else []
        ),
    }
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    path.chmod(0o444)


def _resolved_path(value: str) -> Path:
    return Path(value).resolve()


def _write(path: Path, content: str = "fixture") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_f0_pair(experiment: Path, stem: str) -> None:
    _write(experiment / f"2a_f0/{stem}.wav.npy", "f0")
    _write(experiment / f"2b-f0nsf/{stem}.wav.npy", "f0nsf")


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


if __name__ == "__main__":
    unittest.main()
