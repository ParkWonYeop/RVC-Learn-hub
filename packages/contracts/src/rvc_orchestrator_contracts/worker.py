from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .base import ContractModel, utc_now
from .job import (
    InferenceF0Method,
    InferencePresetConfig,
    JobConfig,
    RVCVersion,
    TrainingF0Method,
    job_config_sha256,
)
from .status import TERMINAL_JOB_STATUSES, JobStatus

_DATASET_DOWNLOAD_PATH = re.compile(
    r"^/api/v1/workers/jobs/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/dataset$"
)
_TEST_SET_ITEM_DOWNLOAD_PATH = re.compile(
    r"^/api/v1/workers/jobs/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
    r"/test-set/items/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
)
_SAFE_TRANSFER_ID = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
_UUID_ID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_WORKER_TOKEN = r"^rvcw_[A-Za-z0-9_-]{32,128}$"


class WorkerStatus(StrEnum):
    IDLE = "idle"
    BUSY = "busy"
    DRAINING = "draining"


class WorkerEngineMode(StrEnum):
    RVC_WEBUI = "rvc_webui"
    FAKE = "fake"


class GPUCapability(ContractModel):
    index: int = Field(ge=0, le=1_023)
    uuid: str | None = None
    name: str = Field(min_length=1, max_length=256)
    total_vram_mb: int = Field(gt=0)
    free_vram_mb: int = Field(ge=0)
    utilization_percent: float | None = Field(default=None, ge=0, le=100)
    temperature_c: float | None = None

    @field_validator("temperature_c")
    @classmethod
    def finite_temperature(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("GPU temperature must be finite")
        return value

    @model_validator(mode="after")
    def validate_vram(self) -> GPUCapability:
        if self.free_vram_mb > self.total_vram_mb:
            raise ValueError("free VRAM cannot exceed total VRAM")
        return self


class WorkerCapabilities(ContractModel):
    engine_mode: WorkerEngineMode = WorkerEngineMode.RVC_WEBUI
    worker_version: str = Field(min_length=1, max_length=128)
    rvc_commit_hash: str = Field(min_length=7, max_length=64)
    supported_rvc_versions: list[RVCVersion] = Field(min_length=1)
    supported_training_f0_methods: list[TrainingF0Method] = Field(min_length=1)
    supported_inference_f0_methods: list[InferenceF0Method] = Field(default_factory=list)
    fixed_test_set_inference_ready: bool = False
    gpus: list[GPUCapability] = Field(default_factory=list, max_length=64)
    disk_free_bytes: int = Field(ge=0)
    tags: list[str] = Field(default_factory=list, max_length=64)
    rvc_assets_ready: bool = False
    runtime_image_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        min_length=71,
        max_length=71,
    )
    runtime_asset_manifest_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        min_length=64,
        max_length=64,
    )
    max_concurrent_jobs: int = Field(default=1, ge=1, le=64)

    @field_validator(
        "supported_rvc_versions",
        "supported_training_f0_methods",
        "supported_inference_f0_methods",
        "tags",
    )
    @classmethod
    def unique_values(cls, value: list[object]) -> list[object]:
        if len(set(value)) != len(value):
            raise ValueError("capability values must be unique")
        return value

    @model_validator(mode="after")
    def validate_gpu_inventory_and_fixed_test_set_gate(self) -> WorkerCapabilities:
        gpu_indices = [gpu.index for gpu in self.gpus]
        if len(set(gpu_indices)) != len(gpu_indices):
            raise ValueError("GPU indexes must be unique")
        gpu_uuids = [gpu.uuid for gpu in self.gpus if gpu.uuid is not None]
        if len(set(gpu_uuids)) != len(gpu_uuids):
            raise ValueError("GPU UUIDs must be unique")
        if self.fixed_test_set_inference_ready and (
            self.engine_mode is not WorkerEngineMode.RVC_WEBUI
            or not self.rvc_assets_ready
            or not self.supported_inference_f0_methods
            or self.runtime_image_digest is None
            or self.runtime_asset_manifest_sha256 is None
        ):
            raise ValueError(
                "fixed TestSet inference readiness requires a real ready RVC runtime and "
                "an inference F0 capability"
            )
        return self


class WorkerRegisterRequest(ContractModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    capabilities: WorkerCapabilities


class WorkerReEnrollRequest(WorkerRegisterRequest):
    worker_id: str = Field(
        strict=True,
        min_length=36,
        max_length=36,
        pattern=_UUID_ID,
    )


class WorkerRegisterResponse(ContractModel):
    worker_id: str
    worker_token: str = Field(repr=False)
    issued_at: datetime = Field(default_factory=utc_now)


class WorkerTokenRotationRequest(ContractModel):
    rotation_id: str = Field(
        strict=True,
        min_length=36,
        max_length=36,
        pattern=_UUID_ID,
    )


class WorkerTokenRotationPrepareResponse(ContractModel):
    """One-time secret returned only when a new pending rotation is created."""

    worker_id: str
    rotation_id: str = Field(min_length=36, max_length=36, pattern=_UUID_ID)
    worker_token: str = Field(
        min_length=37,
        max_length=133,
        pattern=_WORKER_TOKEN,
        repr=False,
    )
    expires_at: datetime


class WorkerTokenRotationStatus(ContractModel):
    worker_id: str
    token_issued_at: datetime
    pending: bool
    rotation_id: str | None = Field(
        default=None,
        min_length=36,
        max_length=36,
        pattern=_UUID_ID,
    )
    started_at: datetime | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_pending_fields(self) -> WorkerTokenRotationStatus:
        values = (self.rotation_id, self.started_at, self.expires_at)
        if self.pending != all(value is not None for value in values):
            raise ValueError("pending token rotation fields must be provided together")
        return self


class WorkerTokenRotationActivated(ContractModel):
    worker_id: str
    rotation_id: str = Field(min_length=36, max_length=36, pattern=_UUID_ID)
    token_issued_at: datetime


class WorkerSessionResponse(ContractModel):
    worker_id: str
    name: str
    status: WorkerStatus
    current_job_id: str | None = None
    last_heartbeat_at: datetime | None = None


class WorkerHeartbeatRequest(ContractModel):
    status: WorkerStatus
    capabilities: WorkerCapabilities
    current_job_id: str | None = None
    current_lease_id: str | None = None
    sent_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_current_assignment(self) -> WorkerHeartbeatRequest:
        if bool(self.current_job_id) is not bool(self.current_lease_id):
            raise ValueError("current_job_id and current_lease_id must be provided together")
        if self.status is WorkerStatus.BUSY and not self.current_job_id:
            raise ValueError("busy heartbeat requires a current job and lease")
        if self.status is WorkerStatus.IDLE and self.current_job_id:
            raise ValueError("idle heartbeat cannot include a current assignment")
        return self


class WorkerHeartbeatResponse(ContractModel):
    server_time: datetime = Field(default_factory=utc_now)
    lease_expires_at: datetime | None = None
    cancel_job_ids: list[str] = Field(default_factory=list)


class JobClaimRequest(ContractModel):
    capabilities: WorkerCapabilities | None = None
    max_wait_seconds: int = Field(default=0, ge=0, le=30)


class DatasetTransfer(ContractModel):
    """Server-verified canonical Dataset archive offered to one Job attempt.

    The Manager returns an authenticated relative path rather than an internal
    object URI or a presigned URL.  S3 redirects, when required, are created only
    after the Worker proves its current lease at that path.
    """

    dataset_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    download_path: str = Field(min_length=1, max_length=512)
    filename: Literal["prepared_flat.zip"] = "prepared_flat.zip"
    content_type: Literal["application/zip"] = "application/zip"
    # The Worker enforces its configured (normally lower) archive limit before
    # opening a connection. This broad wire bound prevents a prepared archive
    # larger than the original compressed upload from crashing claim parsing.
    size_bytes: int = Field(gt=0, le=100 * 1024**3)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("download_path")
    @classmethod
    def validate_manager_relative_path(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            _DATASET_DOWNLOAD_PATH.fullmatch(value) is None
            or value.startswith("//")
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/")[1:])
        ):
            raise ValueError("dataset download_path must be a safe Manager-relative API path")
        return value


class TestSetTransferItem(ContractModel):
    """One server-verified canonical PCM WAV bound to a Job attempt."""

    test_set_item_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=_SAFE_TRANSFER_ID,
    )
    item_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$",
    )
    sort_order: int = Field(ge=0, le=999_999)
    download_path: str = Field(min_length=1, max_length=512)
    filename: str = Field(min_length=5, max_length=132)
    content_type: Literal["audio/wav"] = "audio/wav"
    size_bytes: int = Field(ge=44, le=2 * 1024**3)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sample_rate_hz: int = Field(ge=1, le=384_000)
    channels: int = Field(ge=1, le=32)
    duration_seconds: float = Field(gt=0, le=86_400)

    @model_validator(mode="after")
    def validate_item_descriptor(self) -> TestSetTransferItem:
        parsed = urlsplit(self.download_path)
        if (
            _TEST_SET_ITEM_DOWNLOAD_PATH.fullmatch(self.download_path) is None
            or self.download_path.startswith("//")
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or "\\" in self.download_path
            or any(part in {"", ".", ".."} for part in self.download_path.split("/")[1:])
        ):
            raise ValueError("TestSet item download_path must be a safe Manager-relative API path")
        if self.filename != f"{self.test_set_item_id}.wav":
            raise ValueError("TestSet item filename must be derived from its immutable ID")
        return self


class TestSetTransfer(ContractModel):
    """Storage-neutral immutable TestSet snapshot offered to one Job attempt."""

    test_set_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=_SAFE_TRANSFER_ID,
    )
    family_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=_SAFE_TRANSFER_ID,
    )
    revision: int = Field(ge=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sample_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inference_config: InferencePresetConfig
    inference_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: list[TestSetTransferItem] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_snapshot(self) -> TestSetTransfer:
        canonical = json.dumps(
            self.inference_config.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        if hashlib.sha256(canonical).hexdigest() != self.inference_config_sha256:
            raise ValueError("TestSet inference config hash does not match its snapshot")
        item_ids = [item.test_set_item_id for item in self.items]
        item_keys = [item.item_key for item in self.items]
        sort_orders = [item.sort_order for item in self.items]
        if len(set(item_ids)) != len(item_ids) or len(set(item_keys)) != len(item_keys):
            raise ValueError("TestSet transfer items must have unique IDs and keys")
        if len(set(sort_orders)) != len(sort_orders) or sort_orders != sorted(sort_orders):
            raise ValueError("TestSet transfer items must have unique ascending sort order")
        if sum(item.size_bytes for item in self.items) > 2 * 1024**3:
            raise ValueError("TestSet transfer exceeds the wire total-size limit")
        if math.fsum(item.duration_seconds for item in self.items) > 3_600:
            raise ValueError("TestSet transfer exceeds the wire total-duration limit")
        return self


class JobClaim(ContractModel):
    job_id: str = Field(pattern=_SAFE_TRANSFER_ID)
    attempt_id: str = Field(pattern=_SAFE_TRANSFER_ID)
    attempt_number: int = Field(ge=1)
    lease_id: str = Field(pattern=_SAFE_TRANSFER_ID)
    lease_expires_at: datetime
    config: JobConfig
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Only non-production Fake Worker compatibility claims may omit this. Real
    # Workers always receive a verified transfer and fail closed without it.
    dataset_transfer: DatasetTransfer | None = None
    test_set_transfer: TestSetTransfer | None = None

    @model_validator(mode="after")
    def validate_dataset_transfer(self) -> JobClaim:
        if job_config_sha256(self.config) != self.config_sha256:
            raise ValueError("Job config hash does not match its snapshot")
        if (
            self.dataset_transfer is not None
            and self.dataset_transfer.dataset_id != self.config.dataset_id
        ):
            raise ValueError("dataset transfer does not match Job config")
        if (
            self.dataset_transfer is not None
            and self.dataset_transfer.download_path != f"/api/v1/workers/jobs/{self.job_id}/dataset"
        ):
            raise ValueError("dataset transfer path does not match claimed Job")
        sample_config = self.config.auto_inference_samples
        if not sample_config.enabled:
            if self.test_set_transfer is not None:
                raise ValueError("sample-disabled Job cannot include a TestSet transfer")
            return self
        if self.test_set_transfer is None:
            raise ValueError("sample-enabled Job requires a verified TestSet transfer")
        if self.test_set_transfer.test_set_id != sample_config.test_set_id:
            raise ValueError("TestSet transfer does not match Job config")
        expected_inference = InferencePresetConfig.model_validate(
            sample_config.model_dump(
                mode="json",
                exclude={"enabled", "test_set_id"},
            )
        )
        if self.test_set_transfer.inference_config != expected_inference:
            raise ValueError("TestSet transfer inference config does not match Job config")
        for item in self.test_set_transfer.items:
            expected_path = (
                f"/api/v1/workers/jobs/{self.job_id}/test-set/items/{item.test_set_item_id}"
            )
            if item.download_path != expected_path:
                raise ValueError("TestSet item path does not match claimed Job and item")
        return self


class LeaseRenewRequest(ContractModel):
    lease_id: str


class LeaseRenewResponse(ContractModel):
    lease_id: str
    lease_expires_at: datetime


class JobStatusUpdate(ContractModel):
    lease_id: str
    status: JobStatus
    occurred_at: datetime = Field(default_factory=utc_now)
    current_epoch: int | None = Field(default=None, ge=0)
    telemetry_log_count: int | None = Field(default=None, ge=0, le=2_147_483_647)
    telemetry_metric_count: int | None = Field(default=None, ge=0, le=2_147_483_647)
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=8_192)

    @model_validator(mode="after")
    def validate_error(self) -> JobStatusUpdate:
        if self.status is JobStatus.FAILED and not self.error_message:
            raise ValueError("failed status requires error_message")
        if self.status is not JobStatus.FAILED and (self.error_code or self.error_message):
            raise ValueError("error fields are only valid for failed status")
        counts = (self.telemetry_log_count, self.telemetry_metric_count)
        if (counts[0] is None) != (counts[1] is None):
            raise ValueError("terminal telemetry counts must be provided together")
        if counts[0] is not None and self.status not in TERMINAL_JOB_STATUSES:
            raise ValueError("telemetry counts are only valid for terminal status")
        return self
