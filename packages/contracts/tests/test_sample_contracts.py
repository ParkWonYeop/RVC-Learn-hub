from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from rvc_orchestrator_contracts import (
    RVC_REVIEWED_COMMIT,
    SAMPLE_MAX_OUTPUT_BYTES,
    SAMPLE_MAX_OUTPUT_DURATION_SECONDS,
    SampleMetricValues,
    SampleRegistrationRequest,
)


def _payload() -> dict[str, object]:
    return {
        "lease_id": "10000000-0000-4000-8000-000000000001",
        "attempt_id": "10000000-0000-4000-8000-000000000002",
        "test_set_id": "10000000-0000-4000-8000-000000000003",
        "test_set_item_id": "10000000-0000-4000-8000-000000000004",
        "artifact_id": "10000000-0000-4000-8000-000000000005",
        "sample_plan_sha256": "1" * 64,
        "input_sha256": "2" * 64,
        "model_sha256": "3" * 64,
        "index_sha256": "4" * 64,
        "inference_f0_method": "rmvpe",
        "inference_config_sha256": "5" * 64,
        "native_inference_manifest_sha256": "9" * 64,
        "native_inference_request_sha256": "a" * 64,
        "output_size_bytes": 32044,
        "output_sha256": "6" * 64,
        "output_sample_rate_hz": 40000,
        "output_channels": 1,
        "output_duration_seconds": 0.4,
        "metrics": {
            "peak_amplitude": 0.5,
            "rms": 0.2,
            "clipping_ratio": 0.0,
            "silence_ratio": 0.1,
        },
        "rvc_commit_hash": RVC_REVIEWED_COMMIT,
        "runtime_image_digest": "sha256:" + "7" * 64,
        "runtime_asset_manifest_sha256": "8" * 64,
    }


def test_sample_registration_contract_is_strict_and_bounded() -> None:
    parsed = SampleRegistrationRequest.model_validate(_payload())
    assert parsed.rvc_commit_hash == RVC_REVIEWED_COMMIT
    assert parsed.metrics.rms == 0.2
    assert len(parsed.model_dump_json().encode()) < 4096

    for key, invalid in (
        ("attempt_id", "not-a-uuid"),
        ("input_sha256", "A" * 64),
        ("runtime_image_digest", "7" * 64),
        ("output_size_bytes", True),
        ("output_sample_rate_hz", 40000.0),
        ("output_channels", "1"),
        ("output_duration_seconds", "0.4"),
        ("output_size_bytes", SAMPLE_MAX_OUTPUT_BYTES + 1),
        ("output_channels", 3),
        ("output_duration_seconds", SAMPLE_MAX_OUTPUT_DURATION_SECONDS + 0.001),
    ):
        with pytest.raises(ValidationError):
            SampleRegistrationRequest.model_validate({**_payload(), key: invalid})


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, -0.1, 1.1, "0.1", True])
def test_sample_metrics_reject_non_finite_unbounded_or_coerced_values(
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        SampleMetricValues.model_validate(
            {
                "peak_amplitude": value,
                "rms": 0.1,
                "clipping_ratio": 0.0,
                "silence_ratio": 0.0,
            }
        )
