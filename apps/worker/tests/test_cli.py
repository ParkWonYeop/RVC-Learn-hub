from __future__ import annotations

import json
from pathlib import Path

import pytest

from rvc_worker.cli import _run_check, build_parser
from rvc_worker.gpu import GpuCollection
from rvc_worker.native_runner import NativeSampleInferenceRuntimeEvidence
from rvc_worker.runner import FakeRvcRunner
from rvc_worker.settings import WorkerSettings


def test_parser_exposes_test_set_materialization_bounds() -> None:
    args = build_parser().parse_args(
        [
            "--test-set-materialization-timeout-seconds",
            "123",
            "--test-set-max-total-duration-seconds",
            "45",
        ]
    )

    assert args.test_set_materialization_timeout_seconds == 123
    assert args.test_set_max_total_duration_seconds == 45


def test_parser_exposes_explicit_token_rotation_and_reenrollment_modes() -> None:
    rotated = build_parser().parse_args(["--rotate-token"])
    reenrolled = build_parser().parse_args(["--re-enroll"])
    assert rotated.rotate_token is True
    assert rotated.re_enroll is False
    assert reenrolled.re_enroll is True
    assert reenrolled.rotate_token is False


def test_check_reports_fail_closed_sample_capability_and_test_set_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = WorkerSettings(
        manager_url="https://manager.example",
        worker_name="gpu-01",
        worker_token="secret",
        data_root=tmp_path,
        test_set_materialization_timeout_seconds=321,
        test_set_max_duration_seconds=60,
        test_set_max_total_duration_seconds=123,
    )
    monkeypatch.setattr(
        "rvc_worker.cli.NvidiaSmiCollector.collect",
        lambda _self: GpuCollection((), False, "test has no GPU"),
    )

    assert _run_check(settings, FakeRvcRunner()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["fixed_test_set_inference_ready"] is False
    assert payload["gpu_available"] is False
    assert payload["gpu_telemetry_available"] is False
    assert payload["supported_inference_f0_methods"] == []
    assert payload["test_set_limits"]["materialization_timeout_seconds"] == 321
    assert payload["test_set_limits"]["max_total_duration_seconds"] == 123


def test_check_reports_only_runner_verified_sample_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_image_digest = "sha256:" + "a" * 64
    asset_manifest_sha256 = "b" * 64

    class QualifiedRunner:
        verified_commit_hash = "c" * 40
        assets_ready = True
        sample_inference_runtime_evidence = NativeSampleInferenceRuntimeEvidence(
            runtime_image_digest=runtime_image_digest,
            runtime_asset_manifest_sha256=asset_manifest_sha256,
        )

        async def run_stage(self, stage, context, cancellation):
            raise AssertionError((stage, context, cancellation))

    settings = WorkerSettings(
        manager_url="https://manager.example",
        worker_name="gpu-01",
        worker_token="secret",
        data_root=tmp_path,
        runner_mode="native",
    )
    monkeypatch.setattr(
        "rvc_worker.cli.NvidiaSmiCollector.collect",
        lambda _self: GpuCollection((), False, "test has no GPU"),
    )

    assert _run_check(settings, QualifiedRunner()) == 0  # type: ignore[arg-type]
    payload = json.loads(capsys.readouterr().out)

    assert payload["fixed_test_set_inference_ready"] is True
    assert payload["supported_inference_f0_methods"] == [
        "pm",
        "harvest",
        "crepe",
        "rmvpe",
    ]
    assert payload["runtime_image_digest"] == runtime_image_digest
    assert payload["runtime_asset_manifest_sha256"] == asset_manifest_sha256
