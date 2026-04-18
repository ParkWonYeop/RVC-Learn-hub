from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from rvc_worker.settings import (
    DEFAULT_RUNTIME_ACTIVATION_PATH,
    SettingsError,
    WorkerSettings,
)
from rvc_worker.workspace import WorkspaceError, WorkspaceManager, ensure_within, safe_component


class SettingsTests(unittest.TestCase):
    def test_token_file_wins_and_secret_is_not_represented(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            token = root / "token"
            token.write_text("file-secret\n", encoding="utf-8")
            settings = WorkerSettings.from_sources(
                environ={
                    "MANAGER_URL": "https://manager.example/",
                    "WORKER_NAME": "gpu-01",
                    "WORKER_TOKEN": "environment-secret",
                    "WORKER_TOKEN_FILE": str(token),
                    "DATA_ROOT": str(root / "data"),
                    "WORKER_TAGS": "nvidia,24gb,nvidia",
                }
            )
            self.assertEqual(settings.worker_token, "file-secret")
            self.assertEqual(settings.runner_mode, "fake")
            self.assertEqual(settings.worker_tags, ("nvidia", "24gb"))
            self.assertEqual(settings.telemetry_spool_max_bytes, 256 * 1024**2)
            self.assertEqual(settings.system_telemetry_interval_seconds, 60)
            self.assertNotIn("file-secret", repr(settings))
            self.assertEqual(settings.redacted()["worker_token"], "***")

    def test_system_telemetry_interval_is_separate_and_bounded(self) -> None:
        base = {
            "MANAGER_URL": "https://manager.example",
            "WORKER_NAME": "gpu-01",
            "WORKER_TOKEN": "secret",
            "DATA_ROOT": "/tmp/worker",
        }
        settings = WorkerSettings.from_sources(
            environ={**base, "SYSTEM_TELEMETRY_INTERVAL_SECONDS": "120"}
        )
        self.assertEqual(settings.system_telemetry_interval_seconds, 120)
        self.assertEqual(settings.redacted()["system_telemetry_interval_seconds"], 120)
        for invalid in ("0", "9.9", "3601", "nan", "inf"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SettingsError):
                    WorkerSettings.from_sources(
                        environ={
                            **base,
                            "SYSTEM_TELEMETRY_INTERVAL_SECONDS": invalid,
                        }
                    )

    def test_profile_mode_requires_profile(self) -> None:
        with self.assertRaises(SettingsError):
            WorkerSettings.from_sources(
                environ={
                    "MANAGER_URL": "https://manager.example",
                    "WORKER_NAME": "gpu-01",
                    "WORKER_TOKEN": "secret",
                    "DATA_ROOT": "/tmp/worker",
                    "RVC_RUNNER_MODE": "profile",
                }
            )

    def test_native_mode_loads_bounded_runtime_controls(self) -> None:
        settings = WorkerSettings.from_sources(
            environ={
                "MANAGER_URL": "https://manager.example",
                "WORKER_NAME": "gpu-01",
                "WORKER_TOKEN": "secret",
                "DATA_ROOT": "/tmp/worker",
                "RVC_RUNNER_MODE": "native",
                "RVC_NATIVE_SOURCE_ROOT": "/srv/reviewed-rvc",
                "RVC_NATIVE_PYTHON_EXECUTABLE": "/opt/conda/bin/python",
                "RVC_NATIVE_CPU_WORKERS": "8",
                "RVC_NATIVE_DEVICE": "cuda:0",
                "RVC_NATIVE_USE_HALF": "false",
                "RVC_NATIVE_TRAINING_TIMEOUT_SECONDS": "1234",
            }
        )
        self.assertEqual(settings.runner_mode, "native")
        self.assertEqual(settings.rvc_native_source_root, Path("/srv/reviewed-rvc"))
        self.assertEqual(settings.rvc_native_cpu_workers, 8)
        self.assertEqual(settings.rvc_native_device, "cuda:0")
        self.assertFalse(settings.rvc_native_use_half)
        self.assertEqual(settings.rvc_native_training_timeout_seconds, 1234)

    def test_runtime_activation_path_is_release_owned_and_cannot_be_overridden(self) -> None:
        base = {
            "MANAGER_URL": "https://manager.example",
            "WORKER_NAME": "gpu-01",
            "WORKER_TOKEN": "secret",
            "DATA_ROOT": "/tmp/worker",
        }
        settings = WorkerSettings.from_sources(environ=base)
        self.assertEqual(
            settings.rvc_runtime_activation_path,
            DEFAULT_RUNTIME_ACTIVATION_PATH,
        )
        self.assertEqual(
            settings.redacted()["rvc_runtime_activation_path"],
            str(DEFAULT_RUNTIME_ACTIVATION_PATH),
        )
        with self.assertRaisesRegex(SettingsError, "release-owned"):
            WorkerSettings.from_sources(
                environ={
                    **base,
                    "RVC_RUNTIME_ACTIVATION_PATH": "/tmp/operator-selected.json",
                }
            )
        with self.assertRaisesRegex(SettingsError, "release-owned"):
            WorkerSettings.from_sources(
                environ=base,
                overrides={"rvc_runtime_activation_path": "/tmp/operator-selected.json"},
            )

    def test_native_runtime_paths_and_values_fail_closed(self) -> None:
        base = {
            "MANAGER_URL": "https://manager.example",
            "WORKER_NAME": "gpu-01",
            "WORKER_TOKEN": "secret",
            "DATA_ROOT": "/tmp/worker",
            "RVC_RUNNER_MODE": "native",
        }
        for invalid in (
            {"RVC_NATIVE_SOURCE_ROOT": "relative/rvc"},
            {"RVC_NATIVE_PYTHON_EXECUTABLE": "python"},
            {"RVC_NATIVE_CPU_WORKERS": "257"},
            {"RVC_NATIVE_DEVICE": "cuda;touch"},
            {"RVC_NATIVE_USE_HALF": "perhaps"},
            {"RVC_NATIVE_INDEX_TIMEOUT_SECONDS": "0"},
            {"RVC_NATIVE_INDEX_TIMEOUT_SECONDS": "nan"},
            {"RVC_NATIVE_TRAINING_TIMEOUT_SECONDS": "inf"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SettingsError):
                    WorkerSettings.from_sources(environ={**base, **invalid})

    def test_telemetry_spool_size_must_be_positive(self) -> None:
        with self.assertRaises(SettingsError):
            WorkerSettings.from_sources(
                environ={
                    "MANAGER_URL": "https://manager.example",
                    "WORKER_NAME": "gpu-01",
                    "WORKER_TOKEN": "secret",
                    "DATA_ROOT": "/tmp/worker",
                    "TELEMETRY_SPOOL_MAX_BYTES": "0",
                }
            )

    def test_artifact_quota_relationships_are_validated(self) -> None:
        base = {
            "MANAGER_URL": "https://manager.example",
            "WORKER_NAME": "gpu-01",
            "WORKER_TOKEN": "secret",
            "DATA_ROOT": "/tmp/worker",
        }
        with self.assertRaises(SettingsError):
            WorkerSettings.from_sources(
                environ={
                    **base,
                    "ARTIFACT_MAX_OBJECT_BYTES": "100",
                    "ARTIFACT_MAX_TOTAL_BYTES_PER_ATTEMPT": "99",
                }
            )
        with self.assertRaises(SettingsError):
            WorkerSettings.from_sources(
                environ={
                    **base,
                    "ARTIFACT_MAX_FILES_PER_ATTEMPT": "10",
                    "ARTIFACT_CHECKPOINT_RETENTION": "6",
                }
            )

    def test_dataset_transfer_limits_are_loaded_and_cross_validated(self) -> None:
        base = {
            "MANAGER_URL": "https://manager.example",
            "WORKER_NAME": "gpu-01",
            "WORKER_TOKEN": "secret",
            "DATA_ROOT": "/tmp/worker",
        }
        settings = WorkerSettings.from_sources(
            environ={
                **base,
                "DATASET_DOWNLOAD_MAX_ATTEMPTS": "4",
                "DATASET_MAX_ARCHIVE_BYTES": "4096",
                "DATASET_MAX_ENTRIES": "12",
                "DATASET_MAX_FILE_BYTES": "1024",
                "DATASET_MAX_TOTAL_BYTES": "2048",
                "DATASET_MAX_COMPRESSION_RATIO": "20",
            }
        )
        self.assertEqual(settings.dataset_download_max_attempts, 4)
        self.assertEqual(settings.dataset_max_entries, 12)
        self.assertEqual(settings.dataset_max_compression_ratio, 20)
        with self.assertRaises(SettingsError):
            WorkerSettings.from_sources(
                environ={
                    **base,
                    "DATASET_MAX_FILE_BYTES": "2049",
                    "DATASET_MAX_TOTAL_BYTES": "2048",
                }
            )

    def test_test_set_transfer_limits_are_loaded_and_cross_validated(self) -> None:
        base = {
            "MANAGER_URL": "https://manager.example",
            "WORKER_NAME": "gpu-01",
            "WORKER_TOKEN": "secret",
            "DATA_ROOT": "/tmp/worker",
        }
        settings = WorkerSettings.from_sources(
            environ={
                **base,
                "TEST_SET_DOWNLOAD_TIMEOUT_SECONDS": "120",
                "TEST_SET_MATERIALIZATION_TIMEOUT_SECONDS": "240",
                "TEST_SET_DOWNLOAD_MAX_ATTEMPTS": "4",
                "TEST_SET_MAX_ITEMS": "12",
                "TEST_SET_MAX_ITEM_BYTES": "1024",
                "TEST_SET_MAX_TOTAL_BYTES": "2048",
                "TEST_SET_MAX_DURATION_SECONDS": "30",
                "TEST_SET_MAX_TOTAL_DURATION_SECONDS": "90",
                "TEST_SET_MIN_SAMPLE_RATE_HZ": "16000",
                "TEST_SET_MAX_SAMPLE_RATE_HZ": "48000",
                "TEST_SET_MAX_CHANNELS": "1",
                "TEST_SET_DURATION_TOLERANCE_SECONDS": "0.00001",
            }
        )
        self.assertEqual(settings.test_set_download_timeout_seconds, 120)
        self.assertEqual(settings.test_set_materialization_timeout_seconds, 240)
        self.assertEqual(settings.test_set_download_max_attempts, 4)
        self.assertEqual(settings.test_set_max_items, 12)
        self.assertEqual(settings.test_set_max_total_bytes, 2048)
        self.assertEqual(settings.test_set_max_channels, 1)
        self.assertEqual(settings.test_set_max_total_duration_seconds, 90)
        self.assertEqual(
            settings.redacted()["test_set_materialization_timeout_seconds"],
            240,
        )

        for invalid in (
            {"TEST_SET_MAX_ITEM_BYTES": "43"},
            {
                "TEST_SET_MAX_ITEM_BYTES": "2049",
                "TEST_SET_MAX_TOTAL_BYTES": "2048",
            },
            {
                "TEST_SET_MIN_SAMPLE_RATE_HZ": "48000",
                "TEST_SET_MAX_SAMPLE_RATE_HZ": "16000",
            },
            {"TEST_SET_MAX_DURATION_SECONDS": "86401"},
            {"TEST_SET_MATERIALIZATION_TIMEOUT_SECONDS": "0"},
            {"TEST_SET_MAX_TOTAL_DURATION_SECONDS": "nan"},
            {
                "TEST_SET_MAX_DURATION_SECONDS": "31",
                "TEST_SET_MAX_TOTAL_DURATION_SECONDS": "30",
            },
            {"TEST_SET_DURATION_TOLERANCE_SECONDS": "1.1"},
            {"TEST_SET_DOWNLOAD_MAX_ATTEMPTS": "11"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SettingsError):
                    WorkerSettings.from_sources(environ={**base, **invalid})


class WorkspaceTests(unittest.TestCase):
    def test_untrusted_ids_cannot_escape_root(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "jobs"
            workspace = WorkspaceManager(root).prepare("../../job", "../attempt")
            self.assertIn(root.resolve(), workspace.root.parents)
            self.assertNotIn("..", workspace.root.parts)
            self.assertTrue(workspace.outputs.is_dir())

    def test_safe_components_do_not_collide_for_different_input(self) -> None:
        self.assertNotEqual(safe_component("a/b"), safe_component("a?b"))

    def test_ensure_within_rejects_escape(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            root.mkdir()
            with self.assertRaises(WorkspaceError):
                ensure_within(root / ".." / "escape", root)


if __name__ == "__main__":
    unittest.main()
