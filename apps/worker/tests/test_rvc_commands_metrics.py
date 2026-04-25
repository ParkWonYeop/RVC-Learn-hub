from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from rvc_orchestrator_contracts import TrainingF0Method
from rvc_worker.pretrained import PretrainedPair
from rvc_worker.rvc_commands import (
    RVC_REVIEWED_COMMIT,
    RvcCliRuntime,
    RvcCommandError,
    build_command_plan,
    build_f0_extraction_commands,
)
from rvc_worker.training_inputs import TrainingInputError, prepare_training_inputs
from rvc_worker.training_metrics import (
    TrainingLogParser,
    normalize_tensorboard_scalar,
    parse_training_log,
)

from .helpers import make_job_config


class RvcCommandBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = Path("/opt/rvc-webui")
        self.runtime = RvcCliRuntime(
            python_executable="/opt/rvc-python/bin/python",
            repository_root=self.repository,
            cpu_workers=4,
            available_gpu_ids=(0, 1, 2),
        )
        self.dataset = Path("/srv/rvc-job/inputs/prepared_flat")
        self.experiment = Path("/srv/rvc-job/work/rvc/logs/speaker-a-run-1")
        self.pretrained = PretrainedPair(
            self.repository / "assets/pretrained_v2/f0G40k.pth",
            self.repository / "assets/pretrained_v2/f0D40k.pth",
        )

    def test_reviewed_commit_is_a_full_sha(self) -> None:
        self.assertEqual(len(RVC_REVIEWED_COMMIT), 40)
        int(RVC_REVIEWED_COMMIT, 16)

    def test_v2_rmvpe_gpu_command_plan_matches_upstream_cli(self) -> None:
        config = make_job_config()
        config.training.gpu_ids = [0, 2]
        config.f0_extraction.training_f0_method = TrainingF0Method.RMVPE_GPU
        config.f0_extraction.rmvpe_gpu_ids = [0, 1]
        config = type(config).model_validate(config.model_dump())

        plan = build_command_plan(
            config,
            self.runtime,
            self.dataset,
            self.experiment,
            pretrained=self.pretrained,
        )

        self.assertEqual(
            plan.preprocessing,
            (
                "/opt/rvc-python/bin/python",
                "/opt/rvc-webui/infer/modules/train/preprocess.py",
                "/srv/rvc-job/inputs/prepared_flat",
                "40000",
                "4",
                "/srv/rvc-job/work/rvc/logs/speaker-a-run-1",
                "False",
                "3.7",
            ),
        )
        self.assertEqual(len(plan.f0_extraction), 2)
        self.assertEqual(
            plan.f0_extraction[1],
            (
                "/opt/rvc-python/bin/python",
                "/opt/rvc-webui/infer/modules/train/extract/extract_f0_rmvpe.py",
                "2",
                "1",
                "1",
                "/srv/rvc-job/work/rvc/logs/speaker-a-run-1",
                "True",
            ),
        )
        self.assertEqual(len(plan.feature_extraction), 2)
        self.assertEqual(plan.feature_extraction[0][6], self.experiment.as_posix())
        self.assertEqual(plan.feature_extraction[0][7:], ("v2", "True"))
        self.assertIn("0-2", plan.training)
        self.assertEqual(plan.training[-2:], ("-v", "v2"))

    def test_all_cpu_training_f0_methods_are_forwarded_verbatim(self) -> None:
        for method in ("pm", "harvest", "dio", "rmvpe"):
            with self.subTest(method=method):
                config = make_job_config()
                config.f0_extraction.training_f0_method = TrainingF0Method(method)
                config = type(config).model_validate(config.model_dump())
                commands = build_f0_extraction_commands(config, self.runtime, self.experiment)
                self.assertEqual(len(commands), 1)
                self.assertEqual(commands[0][-1], method)
                self.assertTrue(commands[0][1].endswith("extract_f0_print.py"))

    def test_non_f0_job_has_no_f0_command_and_uses_non_f0_pretrained(self) -> None:
        config = make_job_config(use_f0=False, version="v1")
        plan = build_command_plan(config, self.runtime, self.dataset, self.experiment)
        self.assertEqual(plan.f0_extraction, ())
        generator_index = plan.training.index("-pg") + 1
        self.assertEqual(
            plan.training[generator_index],
            "/opt/rvc-webui/assets/pretrained/G40k.pth",
        )
        self.assertIn("v1", plan.feature_extraction[0])

    def test_unreported_gpu_is_rejected_before_subprocess_creation(self) -> None:
        config = make_job_config()
        config.training.gpu_ids = [7]
        config = type(config).model_validate(config.model_dump())
        with self.assertRaisesRegex(RvcCommandError, "not visible"):
            build_command_plan(config, self.runtime, self.dataset, self.experiment)


class TrainingMetricParserTests(unittest.TestCase):
    def test_parses_epoch_step_learning_rate_and_losses(self) -> None:
        metrics = parse_training_log(
            (
                "INFO Train Epoch: 3 [42%]",
                "INFO [1234, 0.0001]",
                "INFO loss_disc=1.200, loss_gen=2.000, loss_fm=3.000,"
                "loss_mel=4.000, loss_kl=5.000",
                "INFO ====> Epoch: 3 (0:00:10)",
            )
        )
        by_key = {metric.key: metric for metric in metrics}
        self.assertEqual(by_key["current_epoch"].value, 3.0)
        self.assertEqual(by_key["learning_rate"].step, 1234)
        self.assertAlmostEqual(by_key["loss_g_total"].value, 14.0)
        self.assertEqual(by_key["loss_mel"].epoch, 3)
        self.assertEqual(by_key["epoch_completed"].value, 3.0)

    def test_ignores_unstructured_lines_and_normalizes_tensorboard(self) -> None:
        parser = TrainingLogParser()
        self.assertEqual(parser.feed("Loading pretrained model"), ())
        metric = normalize_tensorboard_scalar("loss/g/total", 2.5, 99)
        assert metric is not None
        self.assertEqual((metric.key, metric.value, metric.step, metric.source), (
            "loss_g_total",
            2.5,
            99,
            "tensorboard",
        ))
        self.assertIsNone(normalize_tensorboard_scalar("image/mel", 1.0, 1))


class TrainingInputPreparationTests(unittest.TestCase):
    def test_creates_deterministic_v2_f0_filelist_and_config(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            experiment = root / "job/work/rvc/logs/speaker-a-run-1"
            self._make_repository_assets(repository, "40k", "768")
            self._write(experiment / "0_gt_wavs/a.wav")
            self._write(experiment / "3_feature768/a.npy")
            self._write(experiment / "2a_f0/a.wav.npy")
            self._write(experiment / "2b-f0nsf/a.wav.npy")

            prepared = prepare_training_inputs(
                make_job_config(), repository, experiment, use_half=False
            )

            rows = prepared.filelist_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(prepared.training_example_count, 1)
            self.assertEqual(prepared.mute_example_count, 2)
            self.assertEqual(len(rows), 3)
            self.assertEqual(len(rows[0].split("|")), 5)
            self.assertEqual(rows[1], rows[2])
            document = json.loads(prepared.config_path.read_text(encoding="utf-8"))
            self.assertFalse(document["train"]["fp16_run"])
            self.assertEqual(prepared.config_template, "v1/40k.json")

    def test_rejects_incomplete_examples_instead_of_training_misaligned_data(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            experiment = root / "experiment"
            self._make_repository_assets(repository, "40k", "768")
            self._write(experiment / "0_gt_wavs/a.wav")
            (experiment / "3_feature768").mkdir(parents=True)
            (experiment / "2a_f0").mkdir(parents=True)
            (experiment / "2b-f0nsf").mkdir(parents=True)
            with self.assertRaisesRegex(TrainingInputError, "no complete"):
                prepare_training_inputs(make_job_config(), repository, experiment, use_half=True)

    @staticmethod
    def _write(path: Path, content: str = "fixture") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    @classmethod
    def _make_repository_assets(cls, repository: Path, rate: str, dimension: str) -> None:
        cls._write(
            repository / "configs/v1/40k.json",
            json.dumps({"train": {"fp16_run": True}, "data": {}, "model": {}}),
        )
        cls._write(repository / f"logs/mute/0_gt_wavs/mute{rate}.wav")
        cls._write(repository / f"logs/mute/3_feature{dimension}/mute.npy")
        cls._write(repository / "logs/mute/2a_f0/mute.wav.npy")
        cls._write(repository / "logs/mute/2b-f0nsf/mute.wav.npy")


if __name__ == "__main__":
    unittest.main()
