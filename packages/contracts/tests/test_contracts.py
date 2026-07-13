import pytest
from pydantic import ValidationError

from rvc_orchestrator_contracts import (
    DatasetTransfer,
    F0ExtractionConfig,
    GPUCapability,
    InferenceF0Method,
    InferencePresetConfig,
    JobConfig,
    JobStatus,
    JobStatusUpdate,
    LogBatch,
    ModelConfig,
    RVCVersion,
    SampleRate,
    TrainingF0Method,
    WorkerCapabilities,
    WorkerEngineMode,
    WorkerRegisterResponse,
    WorkerTokenRotationPrepareResponse,
    WorkerTokenRotationStatus,
    can_transition_job,
    canonical_job_config_bytes,
    feature_directory_for_version,
    job_config_sha256,
    validate_job_transition,
)


def test_worker_gpu_inventory_is_finite_bounded_and_unambiguous() -> None:
    gpu = GPUCapability(
        index=0,
        uuid="GPU-0",
        name="Fixture GPU",
        total_vram_mb=24_576,
        free_vram_mb=12_288,
        utilization_percent=50,
        temperature_c=60,
    )
    base = {
        "engine_mode": WorkerEngineMode.FAKE,
        "worker_version": "test",
        "rvc_commit_hash": "fake-runner",
        "supported_rvc_versions": [RVCVersion.V1, RVCVersion.V2],
        "supported_training_f0_methods": list(TrainingF0Method),
        "disk_free_bytes": 1,
    }
    capabilities = WorkerCapabilities(**base, gpus=[gpu])  # type: ignore[arg-type]
    assert capabilities.gpus == [gpu]

    with pytest.raises(ValidationError, match="temperature must be finite"):
        GPUCapability(
            index=0,
            name="Fixture GPU",
            total_vram_mb=1,
            free_vram_mb=1,
            temperature_c=float("nan"),
        )
    with pytest.raises(ValidationError, match="indexes must be unique"):
        WorkerCapabilities(**base, gpus=[gpu, gpu])  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        WorkerCapabilities(**base, gpus=[gpu] * 65)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        GPUCapability(
            index=1_024,
            name="Invalid GPU",
            total_vram_mb=1,
            free_vram_mb=1,
        )


def test_terminal_status_telemetry_counts_are_bounded_and_paired() -> None:
    accepted = JobStatusUpdate(
        lease_id="lease-1",
        status=JobStatus.COMPLETED,
        telemetry_log_count=0,
        telemetry_metric_count=2_147_483_647,
    )
    assert accepted.telemetry_log_count == 0
    assert accepted.telemetry_metric_count == 2_147_483_647

    invalid_payloads = (
        {
            "lease_id": "lease-1",
            "status": "completed",
            "telemetry_log_count": 1,
        },
        {
            "lease_id": "lease-1",
            "status": "training",
            "telemetry_log_count": 1,
            "telemetry_metric_count": 1,
        },
        {
            "lease_id": "lease-1",
            "status": "failed",
            "error_message": "failed",
            "telemetry_log_count": 2_147_483_648,
            "telemetry_metric_count": 1,
        },
    )
    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            JobStatusUpdate.model_validate(payload)


def make_config(**overrides: object) -> JobConfig:
    values = {
        "job_name": "speaker-a-v2",
        "experiment_id": "experiment-1",
        "dataset_id": "dataset-1",
        "model": ModelConfig(version=RVCVersion.V2, sample_rate=SampleRate.KHZ_40),
    }
    values.update(overrides)
    return JobConfig.model_validate(values)


def test_version_controls_feature_directory() -> None:
    assert feature_directory_for_version(RVCVersion.V1) == "3_feature256"
    assert make_config().feature_directory == "3_feature768"


def test_job_config_hash_is_canonical_and_includes_normalized_defaults() -> None:
    config = make_config()
    normalized = config.model_dump(mode="json")
    reversed_keys = dict(reversed(tuple(normalized.items())))

    assert job_config_sha256(normalized) == job_config_sha256(reversed_keys)
    assert job_config_sha256(config) == job_config_sha256(normalized)
    assert job_config_sha256(config) == (
        "756dea9f7db3ed8f23b705f65d4711a4de1f9c39c16d991e4c7f32ad9ec3d175"
    )
    assert job_config_sha256(
        {
            "job_name": config.job_name,
            "experiment_id": config.experiment_id,
            "dataset_id": config.dataset_id,
            "model": config.model.model_dump(mode="json"),
        }
    ) != job_config_sha256(config)


def test_job_config_rejects_non_finite_minimum_vram() -> None:
    with pytest.raises(ValidationError, match="minimum VRAM must be finite"):
        make_config(resource={"min_vram_gb": float("inf")})


def test_job_config_hash_normalizes_signed_zero_for_jsonb_round_trips() -> None:
    negative_zero = make_config(resource={"min_vram_gb": -0.0})
    positive_zero = make_config(resource={"min_vram_gb": 0.0})

    assert job_config_sha256(negative_zero) == job_config_sha256(positive_zero)
    assert b"-0.0" not in canonical_job_config_bytes(negative_zero)


def test_training_and_inference_f0_enums_are_distinct() -> None:
    assert "dio" in {item.value for item in TrainingF0Method}
    assert "dio" not in {item.value for item in InferenceF0Method}
    assert "crepe" in {item.value for item in InferenceF0Method}
    assert "crepe" not in {item.value for item in TrainingF0Method}


def test_f0_disabled_requires_no_training_method() -> None:
    config = make_config(
        model={"version": "v1", "sample_rate": "48k", "use_f0": False},
        f0_extraction={"training_f0_method": None},
    )
    assert config.model.use_f0 is False
    with pytest.raises(ValidationError):
        make_config(model={"version": "v1", "sample_rate": "48k", "use_f0": False})


def test_rmvpe_gpu_requires_gpu_ids() -> None:
    with pytest.raises(ValidationError):
        F0ExtractionConfig(training_f0_method=TrainingF0Method.RMVPE_GPU)
    parsed = F0ExtractionConfig(
        training_f0_method=TrainingF0Method.RMVPE_GPU,
        rmvpe_gpu_ids="0,1",  # type: ignore[arg-type]
    )
    assert parsed.rmvpe_gpu_ids == [0, 1]


def test_state_machine_rejects_skipping_to_completed() -> None:
    assert can_transition_job(JobStatus.QUEUED, JobStatus.ASSIGNED)
    assert not can_transition_job(JobStatus.TRAINING, JobStatus.COMPLETED)
    with pytest.raises(ValueError):
        validate_job_transition(JobStatus.TRAINING, JobStatus.COMPLETED)


def test_batch_sequences_must_be_unique() -> None:
    with pytest.raises(ValidationError):
        LogBatch.model_validate(
            {
                "lease_id": "lease-1",
                "attempt_id": "attempt-1",
                "idempotency_key": "log-batch-1",
                "entries": [
                    {"sequence": 1, "message": "one"},
                    {"sequence": 1, "message": "duplicate"},
                ],
            }
        )


def test_worker_token_rotation_contract_hides_secret_and_requires_complete_status() -> None:
    rotation_id = "12345678-1234-4123-8123-123456789abc"
    registered = WorkerRegisterResponse(worker_id="worker-1", worker_token="register-secret")
    prepared = WorkerTokenRotationPrepareResponse(
        worker_id="worker-1",
        rotation_id=rotation_id,
        worker_token="rvcw_" + "a" * 43,
        expires_at="2026-07-11T00:10:00Z",  # type: ignore[arg-type]
    )
    assert "register-secret" not in repr(registered)
    assert prepared.worker_token not in repr(prepared)
    with pytest.raises(ValidationError, match="provided together"):
        WorkerTokenRotationStatus(
            worker_id="worker-1",
            token_issued_at="2026-07-11T00:00:00Z",  # type: ignore[arg-type]
            pending=True,
            rotation_id=rotation_id,
        )


def test_dataset_transfer_is_relative_verified_metadata_not_storage_uri() -> None:
    transfer = DatasetTransfer(
        dataset_id="dataset-1",
        download_path="/api/v1/workers/jobs/job-1/dataset",
        size_bytes=123,
        sha256="a" * 64,
    )
    assert transfer.filename == "prepared_flat.zip"
    assert transfer.content_type == "application/zip"
    for unsafe in (
        "https://objects.example/prepared.zip?signature=secret",
        "//objects.example/prepared.zip",
        "/api/v1/../storage/object",
        "/api/v1/workers/jobs/job-1/dataset?token=secret",
    ):
        with pytest.raises(ValidationError):
            DatasetTransfer(
                dataset_id="dataset-1",
                download_path=unsafe,
                size_bytes=123,
                sha256="a" * 64,
            )


def test_auto_sample_test_set_id_matches_enabled_state() -> None:
    with pytest.raises(ValidationError):
        make_config(auto_inference_samples={"enabled": True, "test_set_id": None})
    with pytest.raises(ValidationError):
        make_config(auto_inference_samples={"enabled": False, "test_set_id": "test-set-1"})
    parsed = make_config(auto_inference_samples={"enabled": True, "test_set_id": "test-set-1"})
    assert parsed.auto_inference_samples.test_set_id == "test-set-1"
    with pytest.raises(ValidationError):
        make_config(
            auto_inference_samples={
                "enabled": True,
                "test_set_id": "test-set-1",
                "resample_sr": 192_001,
            }
        )


def test_auto_samples_without_index_require_zero_index_rate() -> None:
    with pytest.raises(ValidationError, match="index_rate=0"):
        make_config(
            index={"build_index": False},
            auto_inference_samples={
                "enabled": True,
                "test_set_id": "test-set-1",
                "index_rate": 0.75,
            },
        )
    parsed = make_config(
        index={"build_index": False},
        auto_inference_samples={
            "enabled": True,
            "test_set_id": "test-set-1",
            "index_rate": 0,
        },
        artifacts={"collect_index": False},
    )
    assert parsed.auto_inference_samples.index_rate == 0


@pytest.mark.parametrize(
    "artifact_overrides",
    [
        {"collect_samples": False},
        {"collect_small_model": False},
        {"collect_index": False},
    ],
)
def test_auto_samples_require_every_registration_artifact(
    artifact_overrides: dict[str, bool],
) -> None:
    with pytest.raises(ValidationError):
        make_config(
            auto_inference_samples={
                "enabled": True,
                "test_set_id": "test-set-1",
                "index_rate": 0.75,
            },
            artifacts=artifact_overrides,
        )


def test_auto_samples_with_retrieval_require_added_index_collection() -> None:
    with pytest.raises(ValidationError, match="collect_added_index=true"):
        make_config(
            index={"build_index": True, "collect_added_index": False},
            auto_inference_samples={
                "enabled": True,
                "test_set_id": "test-set-1",
                "index_rate": 0.75,
            },
        )


@pytest.mark.parametrize("resample_sr", [0, 16_000, 192_000])
def test_inference_resample_rate_accepts_disabled_or_supported_rate(
    resample_sr: int,
) -> None:
    config = make_config(auto_inference_samples={"enabled": False, "resample_sr": resample_sr})
    preset = InferencePresetConfig(resample_sr=resample_sr)

    assert config.auto_inference_samples.resample_sr == resample_sr
    assert preset.resample_sr == resample_sr


@pytest.mark.parametrize("resample_sr", [-1, 1, 15_999, 192_001])
def test_inference_resample_rate_rejects_unsupported_rate(resample_sr: int) -> None:
    with pytest.raises(ValidationError):
        make_config(auto_inference_samples={"enabled": False, "resample_sr": resample_sr})
    with pytest.raises(ValidationError):
        InferencePresetConfig(resample_sr=resample_sr)


@pytest.mark.parametrize("resample_sr", [False, "16000", 16_000.0])
def test_inference_resample_rate_rejects_coerced_values(resample_sr: object) -> None:
    with pytest.raises(ValidationError):
        make_config(auto_inference_samples={"enabled": False, "resample_sr": resample_sr})
    with pytest.raises(ValidationError):
        InferencePresetConfig(resample_sr=resample_sr)  # type: ignore[arg-type]


def test_inference_resample_rate_schema_exposes_disjoint_range() -> None:
    job_schema = JobConfig.model_json_schema()
    sample_schema = job_schema["$defs"]["AutoInferenceSamplesConfig"]["properties"]["resample_sr"]
    preset_schema = InferencePresetConfig.model_json_schema()["properties"]["resample_sr"]

    expected = [
        {"const": 0, "type": "integer"},
        {"maximum": 192_000, "minimum": 16_000, "type": "integer"},
    ]
    assert sample_schema["anyOf"] == expected
    assert preset_schema["anyOf"] == expected
