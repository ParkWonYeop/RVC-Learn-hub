from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from rvc_orchestrator_contracts import (
    DatasetTransfer,
    InferencePresetConfig,
    JobClaim,
    JobConfig,
    TestSetTransfer,
    TestSetTransferItem,
    utc_now,
)


def make_job_config(
    *,
    version: str = "v2",
    use_f0: bool = True,
    build_index: bool = True,
    samples: bool = False,
) -> JobConfig:
    f0 = {"training_f0_method": "rmvpe"} if use_f0 else {"training_f0_method": None}
    sample_config = {"enabled": True, "test_set_id": "fixed-v1"} if samples else {"enabled": False}
    return JobConfig.model_validate(
        {
            "job_name": "speaker-a-run-1",
            "experiment_id": "speaker-a-experiment",
            "dataset_id": "speaker-a-dataset",
            "model": {
                "version": version,
                "sample_rate": "40k",
                "use_f0": use_f0,
            },
            "training": {"epochs": 2, "batch_size_per_gpu": 1, "gpu_ids": [0]},
            "f0_extraction": f0,
            "index": {"build_index": build_index},
            "auto_inference_samples": sample_config,
        }
    )


def make_claim(**config_options: object) -> JobClaim:
    config = make_job_config(**config_options)
    test_set_transfer = None
    if config.auto_inference_samples.enabled:
        inference = InferencePresetConfig.model_validate(
            config.auto_inference_samples.model_dump(
                mode="json",
                exclude={"enabled", "test_set_id"},
            )
        )
        canonical = json.dumps(
            inference.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        test_set_transfer = TestSetTransfer(
            test_set_id="fixed-v1",
            family_id="fixed-family",
            revision=1,
            manifest_sha256="1" * 64,
            sample_plan_sha256="2" * 64,
            inference_config=inference,
            inference_config_sha256=hashlib.sha256(canonical).hexdigest(),
            items=[
                TestSetTransferItem(
                    test_set_item_id="test-item-1",
                    item_key="test-item-1",
                    sort_order=0,
                    download_path=(
                        "/api/v1/workers/jobs/job-1/test-set/items/test-item-1"
                    ),
                    filename="test-item-1.wav",
                    size_bytes=44,
                    sha256="3" * 64,
                    sample_rate_hz=8_000,
                    channels=1,
                    duration_seconds=0.000125,
                )
            ],
        )
    return JobClaim(
        job_id="job-1",
        attempt_id="attempt-1",
        attempt_number=1,
        lease_id="lease-1",
        lease_expires_at=utc_now() + timedelta(minutes=5),
        config=config,
        dataset_transfer=DatasetTransfer(
            dataset_id="speaker-a-dataset",
            download_path="/api/v1/workers/jobs/job-1/dataset",
            size_bytes=128,
            sha256="0" * 64,
        ),
        test_set_transfer=test_set_transfer,
    )
