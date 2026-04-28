from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from rvc_orchestrator_contracts import RVCVersion, SampleRate
from rvc_worker.artifacts import (
    ArtifactDiscoveryError,
    discover_artifacts,
    latest_generator_checkpoint,
    select_final_index,
)
from rvc_worker.pretrained import feature_directory, resolve_pretrained
from rvc_worker.runner import RvcRunnerError, create_runner


class PretrainedTests(unittest.TestCase):
    def test_resolves_all_version_f0_branches(self) -> None:
        root = Path("/opt/rvc")
        self.assertEqual(feature_directory(RVCVersion.V1), "3_feature256")
        self.assertEqual(feature_directory(RVCVersion.V2), "3_feature768")
        self.assertEqual(
            resolve_pretrained(root, RVCVersion.V1, SampleRate.KHZ_40, True).generator,
            root / "assets/pretrained/f0G40k.pth",
        )
        self.assertEqual(
            resolve_pretrained(root, RVCVersion.V2, SampleRate.KHZ_48, False).discriminator,
            root / "assets/pretrained_v2/D48k.pth",
        )

    def test_real_runner_refuses_to_run_without_profile(self) -> None:
        with self.assertRaises(RvcRunnerError):
            create_runner("profile", profile_path=None)


class ArtifactTests(unittest.TestCase):
    def test_discovers_versioned_features_and_sorted_checkpoints(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            logs = root / "logs" / "experiment"
            weights = root / "weights"
            (logs / "3_feature768").mkdir(parents=True)
            weights.mkdir()
            for name in ("G_10.pth", "G_2.pth", "D_10.pth", "added_test.index", "total_fea.npy"):
                (logs / name).write_bytes(b"x")
            (weights / "experiment.pth").write_bytes(b"small")
            artifacts = discover_artifacts(root / "logs", weights, "experiment", "v2")
            self.assertEqual(artifacts.feature_directory.name, "3_feature768")
            self.assertEqual([item.epoch for item in artifacts.generator_checkpoints], [2, 10])
            self.assertEqual(
                latest_generator_checkpoint(artifacts.generator_checkpoints).name,
                "G_10.pth",
            )
            self.assertEqual(
                select_final_index(artifacts.index_candidates).name,
                "added_test.index",
            )

    def test_ambiguous_index_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidates = [root / "added_a.index", root / "added_b.index"]
            with self.assertRaises(ArtifactDiscoveryError):
                select_final_index(candidates)


if __name__ == "__main__":
    unittest.main()
