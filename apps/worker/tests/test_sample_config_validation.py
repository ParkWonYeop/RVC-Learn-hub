from __future__ import annotations

import pytest
from pydantic import ValidationError

from rvc_orchestrator_contracts import JobClaim

from .helpers import make_claim


@pytest.mark.parametrize("resample_sr", [0, 16_000, 192_000])
def test_worker_claim_accepts_supported_inference_resample_rate(
    resample_sr: int,
) -> None:
    payload = make_claim().model_dump(mode="json")
    payload["config"]["auto_inference_samples"]["resample_sr"] = resample_sr

    claim = JobClaim.model_validate(payload)

    assert claim.config.auto_inference_samples.resample_sr == resample_sr


@pytest.mark.parametrize("resample_sr", [-1, 1, 15_999, 192_001])
def test_worker_claim_rejects_unsupported_inference_resample_rate(
    resample_sr: int,
) -> None:
    payload = make_claim().model_dump(mode="json")
    payload["config"]["auto_inference_samples"]["resample_sr"] = resample_sr

    with pytest.raises(ValidationError):
        JobClaim.model_validate(payload)


@pytest.mark.parametrize("resample_sr", [False, "16000", 16_000.0])
def test_worker_claim_rejects_coerced_inference_resample_rate(
    resample_sr: object,
) -> None:
    payload = make_claim().model_dump(mode="json")
    payload["config"]["auto_inference_samples"]["resample_sr"] = resample_sr

    with pytest.raises(ValidationError):
        JobClaim.model_validate(payload)
