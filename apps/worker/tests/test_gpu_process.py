from __future__ import annotations

import asyncio
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from rvc_worker.gpu import NvidiaSmiCollector
from rvc_worker.process import ProcessCancelled, ProcessSpec, SafeSubprocessRunner
from rvc_worker.telemetry import TelemetrySpoolError


class GpuCollectorTests(unittest.TestCase):
    def test_parses_nvidia_smi_without_shell(self) -> None:
        command = Mock(
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="0, GPU-uuid, NVIDIA RTX, 24576, 1024, 87, 71\n",
                stderr="",
            )
        )
        collection = NvidiaSmiCollector(
            executable=Path("/usr/bin/nvidia-smi"), run_command=command
        ).collect()
        self.assertTrue(collection.available)
        self.assertEqual(collection.gpus[0].uuid, "GPU-uuid")
        self.assertEqual(collection.gpus[0].memory_total_mb, 24576)
        self.assertFalse(command.call_args.kwargs["shell"])
        self.assertIn("--format=csv,noheader,nounits", command.call_args.args[0])

    def test_nonzero_exit_is_reported_without_stderr_leak(self) -> None:
        command = Mock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=9, stdout="", stderr="potential secret"
            )
        )
        result = NvidiaSmiCollector(
            executable=Path("/usr/bin/nvidia-smi"), run_command=command
        ).collect()
        self.assertFalse(result.available)
        self.assertNotIn("potential secret", result.error or "")

    def test_successful_empty_inventory_is_available_zero_gpu_observation(self) -> None:
        command = Mock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="\n", stderr=""
            )
        )

        result = NvidiaSmiCollector(
            executable=Path("/usr/bin/nvidia-smi"), run_command=command
        ).collect()

        self.assertTrue(result.available)
        self.assertEqual(result.gpus, ())
        self.assertIsNone(result.error)

    def test_semantically_invalid_inventory_fails_safe_without_partial_gpus(self) -> None:
        valid = "0, GPU-0, NVIDIA RTX, 24576, 1024, 87, 71"
        cases = {
            "duplicate-index": f"{valid}\n0, GPU-1, NVIDIA RTX, 24576, 0, 1, 30\n",
            "duplicate-uuid": f"{valid}\n1, GPU-0, NVIDIA RTX, 24576, 0, 1, 30\n",
            "negative-index": "-1, GPU-0, NVIDIA RTX, 24576, 0, 1, 30\n",
            "negative-memory": "0, GPU-0, NVIDIA RTX, 24576, -1, 1, 30\n",
            "used-over-total": "0, GPU-0, NVIDIA RTX, 10, 11, 1, 30\n",
            "nan-utilization": "0, GPU-0, NVIDIA RTX, 24576, 0, nan, 30\n",
            "utilization-over-100": "0, GPU-0, NVIDIA RTX, 24576, 0, 101, 30\n",
            "infinite-temperature": "0, GPU-0, NVIDIA RTX, 24576, 0, 1, inf\n",
            "too-many-gpus": "\n".join(
                f"{index}, GPU-{index}, NVIDIA RTX, 24576, 0, 1, 30"
                for index in range(65)
            ),
        }
        for label, stdout in cases.items():
            with self.subTest(label=label):
                command = Mock(
                    return_value=subprocess.CompletedProcess(
                        args=[], returncode=0, stdout=stdout, stderr="secret"
                    )
                )

                result = NvidiaSmiCollector(
                    executable=Path("/usr/bin/nvidia-smi"), run_command=command
                ).collect()

                self.assertFalse(result.available)
                self.assertEqual(result.gpus, ())
                self.assertIn("invalid nvidia-smi output", result.error or "")
                self.assertNotIn("secret", result.error or "")


class ProcessRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_executes_argv_literally_and_captures_output(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "must-not-exist"
            payload = f"; touch {marker}"
            result = await SafeSubprocessRunner().run(
                ProcessSpec(
                    argv=(sys.executable, "-c", "import sys; print(sys.argv[1])", payload),
                    cwd=root,
                    workspace_root=root,
                    stdout_path=root / "logs" / "stdout.log",
                    stderr_path=root / "logs" / "stderr.log",
                ),
                asyncio.Event(),
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn(payload, result.stdout_path.read_text(encoding="utf-8"))
            self.assertFalse(marker.exists())

    async def test_cooperative_cancellation_terminates_process(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            cancellation = asyncio.Event()
            task = asyncio.create_task(
                SafeSubprocessRunner().run(
                    ProcessSpec(
                        argv=(sys.executable, "-c", "import time; time.sleep(60)"),
                        cwd=root,
                        workspace_root=root,
                        stdout_path=root / "stdout.log",
                        stderr_path=root / "stderr.log",
                        terminate_grace_seconds=0.2,
                    ),
                    cancellation,
                )
            )
            await asyncio.sleep(0.05)
            cancellation.set()
            with self.assertRaises(ProcessCancelled):
                await asyncio.wait_for(task, timeout=3)

    async def test_output_callback_failure_terminates_process_without_waiting_for_timeout(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)

            async def fail_after_durable_boundary(channel: str, chunk: bytes) -> None:
                del channel, chunk
                raise TelemetrySpoolError("injected spool backpressure")

            with self.assertRaises(TelemetrySpoolError):
                await asyncio.wait_for(
                    SafeSubprocessRunner().run(
                        ProcessSpec(
                            argv=(
                                sys.executable,
                                "-c",
                                ("import time; print('ready', flush=True); time.sleep(60)"),
                            ),
                            cwd=root,
                            workspace_root=root,
                            stdout_path=root / "stdout.log",
                            stderr_path=root / "stderr.log",
                            timeout_seconds=30,
                            terminate_grace_seconds=0.2,
                        ),
                        asyncio.Event(),
                        output_callback=fail_after_durable_boundary,
                    ),
                    timeout=3,
                )


if __name__ == "__main__":
    unittest.main()
