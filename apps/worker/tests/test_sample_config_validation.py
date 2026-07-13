from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from rvc_orchestrator_contracts import JobClaim, JobClaimRequest, job_config_sha256
from rvc_worker.client import HttpManagerClient, ManagerClientError

from .helpers import make_claim


@pytest.mark.parametrize("resample_sr", [0, 16_000, 192_000])
def test_worker_claim_accepts_supported_inference_resample_rate(
    resample_sr: int,
) -> None:
    payload = make_claim().model_dump(mode="json")
    payload["config"]["auto_inference_samples"]["resample_sr"] = resample_sr
    payload["config_sha256"] = job_config_sha256(payload["config"])

    claim = JobClaim.model_validate(payload)

    assert claim.config.auto_inference_samples.resample_sr == resample_sr


@pytest.mark.parametrize("resample_sr", [-1, 1, 15_999, 192_001])
def test_worker_claim_rejects_unsupported_inference_resample_rate(
    resample_sr: int,
) -> None:
    payload = make_claim().model_dump(mode="json")
    payload["config"]["auto_inference_samples"]["resample_sr"] = resample_sr
    payload["config_sha256"] = job_config_sha256(payload["config"])

    with pytest.raises(ValidationError):
        JobClaim.model_validate(payload)


@pytest.mark.parametrize("resample_sr", [False, "16000", 16_000.0])
def test_worker_claim_rejects_coerced_inference_resample_rate(
    resample_sr: object,
) -> None:
    payload = make_claim().model_dump(mode="json")
    payload["config"]["auto_inference_samples"]["resample_sr"] = resample_sr
    payload["config_sha256"] = job_config_sha256(payload["config"])

    with pytest.raises(ValidationError):
        JobClaim.model_validate(payload)


@pytest.mark.asyncio
async def test_http_manager_client_rejects_missing_or_mismatched_job_config_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HttpManagerClient(
        "https://manager.example",
        "bootstrap-token",
        worker_token="worker-token",
    )
    valid_payload = make_claim().model_dump(mode="json")

    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args: (200, json.dumps(valid_payload).encode("utf-8")),
    )
    accepted = await client.claim_job(JobClaimRequest())
    assert accepted is not None
    assert accepted.config_sha256 == valid_payload["config_sha256"]

    for mutation in ("missing", "mismatched"):
        payload = dict(valid_payload)
        if mutation == "missing":
            payload.pop("config_sha256")
        else:
            payload["config_sha256"] = "f" * 64
        monkeypatch.setattr(
            client,
            "_request",
            lambda *_args, payload=payload: (200, json.dumps(payload).encode("utf-8")),
        )
        with pytest.raises(ManagerClientError, match="invalid JobClaim"):
            await client.claim_job(JobClaimRequest())
