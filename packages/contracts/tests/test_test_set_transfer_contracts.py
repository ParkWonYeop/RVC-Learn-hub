from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from rvc_orchestrator_contracts import (
    InferencePresetConfig,
    JobClaim,
    JobConfig,
    WorkerCapabilities,
    job_config_sha256,
    utc_now,
)
from rvc_orchestrator_contracts import (
    TestSetTransfer as TransferDescriptor,
)
from rvc_orchestrator_contracts import (
    TestSetTransferItem as TransferItemDescriptor,
)


def _config(*, samples: bool = True) -> JobConfig:
    return JobConfig.model_validate(
        {
            "job_name": "job-config",
            "experiment_id": "experiment-1",
            "dataset_id": "dataset-1",
            "model": {"version": "v2", "sample_rate": "40k"},
            "auto_inference_samples": (
                {"enabled": True, "test_set_id": "test-set-1"} if samples else {"enabled": False}
            ),
        }
    )


def _inference(config: JobConfig) -> InferencePresetConfig:
    return InferencePresetConfig.model_validate(
        config.auto_inference_samples.model_dump(mode="json", exclude={"enabled", "test_set_id"})
    )


def _hash_config(config: InferencePresetConfig) -> str:
    encoded = json.dumps(
        config.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _item(index: int = 0, *, job_id: str = "job-1") -> TransferItemDescriptor:
    item_id = f"item-{index}"
    return TransferItemDescriptor(
        test_set_item_id=item_id,
        item_key=f"key-{index}",
        sort_order=index,
        download_path=f"/api/v1/workers/jobs/{job_id}/test-set/items/{item_id}",
        filename=f"{item_id}.wav",
        size_bytes=44,
        sha256=f"{index % 16:x}" * 64,
        sample_rate_hz=16_000,
        channels=1,
        duration_seconds=1.0,
    )


def _transfer(
    config: JobConfig,
    *,
    items: list[TransferItemDescriptor] | None = None,
) -> TransferDescriptor:
    inference = _inference(config)
    return TransferDescriptor(
        test_set_id="test-set-1",
        family_id="family-1",
        revision=3,
        manifest_sha256="a" * 64,
        sample_plan_sha256="b" * 64,
        inference_config=inference,
        inference_config_sha256=_hash_config(inference),
        items=items or [_item()],
    )


def _claim(config: JobConfig, transfer: TransferDescriptor | None) -> JobClaim:
    return JobClaim(
        job_id="job-1",
        attempt_id="attempt-1",
        attempt_number=1,
        lease_id="lease-1",
        lease_expires_at=utc_now() + timedelta(minutes=5),
        config=config,
        config_sha256=job_config_sha256(config),
        test_set_transfer=transfer,
    )


def test_test_set_transfer_is_storage_neutral_and_ordered() -> None:
    config = _config()
    transfer = _transfer(config, items=[_item(0), _item(2)])
    claim = _claim(config, transfer)

    assert claim.test_set_transfer == transfer
    assert [item.sort_order for item in transfer.items] == [0, 2]
    assert transfer.family_id == "family-1"
    assert transfer.sample_plan_sha256 == "b" * 64

    item = _item().model_dump(mode="json")
    for unsafe in (
        "https://objects.example/item.wav?token=secret",
        "//objects.example/item.wav",
        "/api/v1/workers/jobs/job-1/test-set/items/../secret",
        "/api/v1/workers/jobs/job-1/test-set/items/item-0?token=secret",
    ):
        item["download_path"] = unsafe
        with pytest.raises(ValidationError):
            TransferItemDescriptor.model_validate(item)


def test_job_claim_rejects_a_config_snapshot_hash_mismatch() -> None:
    config = _config(samples=False)
    payload = _claim(config, None).model_dump(mode="json")
    payload["config"]["training"]["epochs"] += 1

    with pytest.raises(ValidationError, match="config hash does not match"):
        JobClaim.model_validate(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_test_set",
        "wrong_config",
        "wrong_job_path",
        "wrong_item_path",
    ],
)
def test_claim_binds_exact_test_set_identity_path_and_config(mutation: str) -> None:
    config = _config()
    transfer_data = _transfer(config).model_dump(mode="json")
    if mutation == "wrong_test_set":
        transfer_data["test_set_id"] = "test-set-2"
    elif mutation == "wrong_config":
        changed = InferencePresetConfig(transpose=1)
        transfer_data["inference_config"] = changed.model_dump(mode="json")
        transfer_data["inference_config_sha256"] = _hash_config(changed)
    elif mutation == "wrong_job_path":
        transfer_data["items"][0]["download_path"] = (
            "/api/v1/workers/jobs/job-2/test-set/items/item-0"
        )
    else:
        transfer_data["items"][0]["download_path"] = (
            "/api/v1/workers/jobs/job-1/test-set/items/item-9"
        )
    transfer = TransferDescriptor.model_validate(transfer_data)
    with pytest.raises(ValidationError):
        _claim(config, transfer)


def test_sample_enabled_and_disabled_claims_fail_closed() -> None:
    enabled = _config()
    with pytest.raises(ValidationError):
        _claim(enabled, None)
    disabled = _config(samples=False)
    with pytest.raises(ValidationError):
        _claim(disabled, _transfer(enabled))


def test_transfer_rejects_hash_mismatch_duplicate_and_nonascending_items() -> None:
    config = _config()
    base = _transfer(config).model_dump(mode="json")
    base["inference_config_sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        TransferDescriptor.model_validate(base)

    for items in (
        [_item(0), _item(0)],
        [_item(0), _item(1).model_copy(update={"item_key": "key-0"})],
        [_item(1), _item(0)],
        [
            _item(0),
            _item(1).model_copy(update={"sort_order": 0}),
        ],
    ):
        with pytest.raises(ValidationError):
            _transfer(config, items=items)


def test_transfer_requires_family_and_sample_plan_snapshot_hashes() -> None:
    base = _transfer(_config()).model_dump(mode="json")
    for field in ("family_id", "sample_plan_sha256"):
        invalid = dict(base)
        invalid.pop(field)
        with pytest.raises(ValidationError):
            TransferDescriptor.model_validate(invalid)
    base["sample_plan_sha256"] = "not-a-hash"
    with pytest.raises(ValidationError):
        TransferDescriptor.model_validate(base)


def test_transfer_rejects_wire_resource_totals_above_worker_policy() -> None:
    items = [_item(index).model_copy(update={"size_bytes": 2 * 1024**3}) for index in range(2)]
    with pytest.raises(ValidationError):
        _transfer(_config(), items=items)

    long_items = [
        _item(index).model_copy(update={"duration_seconds": 1_801.0}) for index in range(2)
    ]
    with pytest.raises(ValidationError):
        _transfer(_config(), items=long_items)


def _capabilities(**updates: object) -> WorkerCapabilities:
    values: dict[str, object] = {
        "worker_version": "0.1.0",
        "rvc_commit_hash": "abcdef0",
        "supported_rvc_versions": ["v1", "v2"],
        "supported_training_f0_methods": ["rmvpe"],
        "disk_free_bytes": 1,
    }
    values.update(updates)
    return WorkerCapabilities.model_validate(values)


def test_fixed_test_set_capability_defaults_closed_and_has_strict_gate() -> None:
    defaults = _capabilities()
    assert defaults.supported_inference_f0_methods == []
    assert defaults.fixed_test_set_inference_ready is False

    for invalid in (
        {
            "engine_mode": "fake",
            "rvc_assets_ready": True,
            "supported_inference_f0_methods": ["rmvpe"],
            "fixed_test_set_inference_ready": True,
        },
        {
            "rvc_assets_ready": False,
            "supported_inference_f0_methods": ["rmvpe"],
            "fixed_test_set_inference_ready": True,
        },
        {
            "rvc_assets_ready": True,
            "supported_inference_f0_methods": [],
            "fixed_test_set_inference_ready": True,
        },
    ):
        with pytest.raises(ValidationError):
            _capabilities(**invalid)

    ready = _capabilities(
        engine_mode="rvc_webui",
        rvc_assets_ready=True,
        supported_inference_f0_methods=["rmvpe"],
        fixed_test_set_inference_ready=True,
        runtime_image_digest="sha256:" + "1" * 64,
        runtime_asset_manifest_sha256="2" * 64,
    )
    assert ready.fixed_test_set_inference_ready is True
