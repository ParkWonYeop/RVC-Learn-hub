from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import PurePath
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from rvc_orchestrator_contracts import (
    ArtifactType,
    InferencePresetConfig,
    JobConfig,
    JobStatus,
    LogLevel,
    WorkerCapabilities,
    WorkerEngineMode,
    WorkerStatus,
)

from .security import normalize_email


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class HealthResponse(APIModel):
    status: str
    service: str


class ReadinessResponse(APIModel):
    status: str
    checks: dict[str, str]


class MaintenanceEnqueueRequest(APIModel):
    dry_run: bool = True


class MaintenanceRunRead(APIModel):
    id: str
    task_name: Literal["dataset_staging_cleanup", "test_set_staging_cleanup"]
    job_id: str
    dry_run: bool
    status: Literal[
        "queued",
        "running",
        "retrying",
        "completed",
        "failed",
        "enqueue_failed",
    ]
    attempt_count: int
    max_attempts: int
    result: dict[str, Any]
    last_error_code: str | None
    queued_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class LoginRequest(APIModel):
    email: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=1_024)

    @field_validator("email")
    @classmethod
    def normalize_login_email(cls, value: str) -> str:
        return normalize_email(value)


class AccessTokenResponse(APIModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class UserRead(APIModel):
    id: str
    email: str
    role: Literal["admin", "user"]
    disabled: bool
    created_at: datetime
    updated_at: datetime


class AdminUserCreate(APIModel):
    email: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=16, max_length=1_024)
    role: Literal["admin", "user"] = "user"
    active: bool = Field(default=True, strict=True)

    @field_validator("email")
    @classmethod
    def normalize_user_email(cls, value: str) -> str:
        return normalize_email(value)


class AdminUserAccessUpdate(APIModel):
    expected_row_version: int = Field(strict=True, ge=1, le=2_147_483_647)
    role: Literal["admin", "user"]
    active: bool = Field(strict=True)


class AdminUserPasswordReset(APIModel):
    expected_row_version: int = Field(strict=True, ge=1, le=2_147_483_647)
    new_password: SecretStr = Field(min_length=16, max_length=1_024)


class AdminUserRead(APIModel):
    id: str
    email: str
    role: Literal["admin", "user"]
    active: bool
    row_version: int
    created_at: datetime
    updated_at: datetime


class AdminUserList(APIModel):
    items: list[AdminUserRead]
    total: int
    offset: int
    limit: int


class WorkerRead(APIModel):
    id: str
    name: str
    status: WorkerStatus
    capabilities: WorkerCapabilities
    worker_version: str
    rvc_commit_hash: str
    last_heartbeat_at: datetime | None
    current_job_id: str | None
    is_active: bool
    online: bool
    token_issued_at: datetime
    token_rotation_pending: bool
    token_rotation_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class WorkerList(APIModel):
    items: list[WorkerRead]
    total: int
    offset: int
    limit: int


class WorkerTokenRevokeRequest(APIModel):
    expected_worker_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    reason_code: Literal["suspected_compromise", "confirmed_compromise", "decommissioned"]
    force_cancel_active: bool = False


class DatasetCreate(APIModel):
    name: str = Field(min_length=1, max_length=128)
    storage_uri: str = Field(min_length=1, max_length=2_048)
    flat_storage_uri: str | None = Field(default=None, max_length=2_048)


DatasetPcmLoudnessUnavailableReason = Literal[
    "below_absolute_gate",
    "insufficient_duration",
    "unsupported_channel_layout",
    "unsupported_sample_rate",
]


class DatasetPcmLoudnessRead(APIModel):
    algorithm: Literal["itu-r-bs1770-4-mono-stereo-v1"]
    scope: Literal["global-gate-over-per-file-complete-blocks-v1"]
    block_duration_ms: Literal[400]
    block_overlap_percent: Literal[75]
    absolute_gate_lufs: float = Field(strict=True, ge=-70, le=-70, allow_inf_nan=False)
    relative_gate_lu: float = Field(strict=True, ge=-10, le=-10, allow_inf_nan=False)
    analyzed_file_count: int = Field(strict=True, ge=0, le=10_000)
    block_count: int = Field(strict=True, ge=0, le=9_007_199_254_740_991)
    gated_block_count: int = Field(strict=True, ge=0, le=9_007_199_254_740_991)
    integrated_lufs: float | None = Field(
        default=None,
        strict=True,
        ge=-70,
        le=10,
        allow_inf_nan=False,
    )
    unavailable_reason: DatasetPcmLoudnessUnavailableReason | None = None

    @model_validator(mode="after")
    def validate_loudness_state(self) -> DatasetPcmLoudnessRead:
        if self.gated_block_count > self.block_count:
            raise ValueError("gated loudness blocks cannot exceed all loudness blocks")
        if self.integrated_lufs is None:
            if self.unavailable_reason is None or self.gated_block_count != 0:
                raise ValueError(
                    "unavailable integrated loudness needs a reason and zero gated blocks"
                )
        elif self.unavailable_reason is not None or self.gated_block_count == 0:
            raise ValueError(
                "available integrated loudness needs gated blocks and no unavailable reason"
            )
        if self.unavailable_reason in {
            "unsupported_channel_layout",
            "unsupported_sample_rate",
        } and (self.analyzed_file_count != 0 or self.block_count != 0):
            raise ValueError("unsupported loudness inputs cannot expose partial aggregates")
        if self.unavailable_reason == "insufficient_duration" and (
            self.analyzed_file_count == 0 or self.block_count != 0
        ):
            raise ValueError("insufficient-duration loudness state is inconsistent")
        if self.unavailable_reason == "below_absolute_gate" and (
            self.analyzed_file_count == 0 or self.block_count == 0
        ):
            raise ValueError("below-gate loudness state is inconsistent")
        if self.integrated_lufs is not None and (
            self.analyzed_file_count == 0 or self.block_count == 0
        ):
            raise ValueError("integrated loudness requires analyzed files and blocks")
        return self


class DatasetPcmQualityRead(APIModel):
    algorithm: Literal["pcm-sample-weighted-v1"]
    validated_file_count: int = Field(strict=True, ge=1, le=10_000)
    sample_count: int = Field(strict=True, ge=1)
    clipping_ratio: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    silence_ratio: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    rms_ratio: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    silence_threshold_dbfs: float = Field(
        strict=True,
        ge=-120,
        lt=0,
        allow_inf_nan=False,
    )
    # ``None`` is reserved for rows finalized before the LUFS migration. New
    # canonical PCM reports always persist a complete state, including an
    # explicit unavailable reason when no finite integrated value exists.
    loudness: DatasetPcmLoudnessRead | None = None

    @model_validator(mode="after")
    def validate_loudness_file_count(self) -> DatasetPcmQualityRead:
        if (
            self.loudness is not None
            and self.loudness.analyzed_file_count > self.validated_file_count
        ):
            raise ValueError("loudness analyzed files cannot exceed validated PCM files")
        return self


class DatasetRead(APIModel):
    id: str
    name: str
    status: Literal[
        "legacy_imported",
        "upload_pending",
        "processing",
        "ready",
        "decoder_pending",
        "failed",
        "deleting",
        "delete_failed",
    ]
    original_filename: str | None
    original_size_bytes: int | None
    original_sha256: str | None
    original_mime_type: str | None
    prepared_flat_size_bytes: int | None
    prepared_flat_sha256: str | None
    manifest_sha256: str | None
    quality_report_sha256: str | None
    duration_sec: float | None
    file_count: int | None
    sample_rate: int | None
    decoder_pending_count: int
    source_file_entry_count: int | None = Field(default=None, strict=True, ge=0, le=10_000)
    skipped_file_count: int | None = Field(default=None, strict=True, ge=0, le=10_000)
    rejected_file_count: int | None = Field(default=None, strict=True, ge=0, le=10_000)
    duplicate_file_count: int | None = Field(default=None, strict=True, ge=0, le=10_000)
    pcm_quality: DatasetPcmQualityRead | None = None
    is_usable: bool
    failure_code: str | None
    retryable: bool
    created_at: datetime
    updated_at: datetime


class DatasetList(APIModel):
    items: list[DatasetRead]
    total: int
    offset: int
    limit: int


_DATASET_CONTENT_TYPES: dict[str, frozenset[str]] = {
    ".zip": frozenset({"application/zip", "application/x-zip-compressed"}),
    ".wav": frozenset({"audio/wav", "audio/x-wav", "audio/wave"}),
    ".flac": frozenset({"audio/flac", "audio/x-flac"}),
    ".mp3": frozenset({"audio/mpeg"}),
    ".m4a": frozenset({"audio/mp4", "audio/x-m4a"}),
    ".ogg": frozenset({"audio/ogg", "application/ogg"}),
    ".aac": frozenset({"audio/aac", "audio/x-aac"}),
}


class DatasetUploadInitRequest(APIModel):
    name: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=3, max_length=255)
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("dataset name must not contain control characters")
        return normalized

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if (
            value in {".", ".."}
            or PurePath(value).name != value
            or "\\" in value
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("dataset filename must not contain a path or control characters")
        return value

    @field_validator("content_type")
    @classmethod
    def normalize_content_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if ";" in normalized or "/" not in normalized:
            raise ValueError("content_type must be a media type without parameters")
        return normalized

    @field_validator("sha256")
    @classmethod
    def normalize_sha256(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def validate_extension_and_content_type(self) -> DatasetUploadInitRequest:
        extension = PurePath(self.filename).suffix.lower()
        allowed = _DATASET_CONTENT_TYPES.get(extension)
        if allowed is None:
            raise ValueError("dataset must be ZIP or a supported audio file")
        if self.content_type not in allowed:
            raise ValueError(f"content_type {self.content_type!r} is not allowed for {extension}")
        return self


class DatasetUploadInitResponse(APIModel):
    upload_session_id: str
    dataset_id: str
    status: Literal["pending", "finalizing", "completed", "failed", "expired"]
    method: Literal["PUT"] | None = None
    upload_url: str | None = None
    upload_headers: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime
    dataset: DatasetRead | None = None
    failure_code: str | None = None
    retryable: bool = False
    retry_after_seconds: int | None = None


class ExperimentCreate(APIModel):
    name: str = Field(min_length=1, max_length=128)
    dataset_id: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=8_192)

    @field_validator("name")
    @classmethod
    def name_is_not_a_path(cls, value: str) -> str:
        normalized = value.strip()
        if (
            not normalized
            or "/" in normalized
            or "\\" in normalized
            or normalized in {".", ".."}
            or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
        ):
            raise ValueError("experiment name must be a safe display name")
        return normalized

    @field_validator("description")
    @classmethod
    def description_has_no_binary_controls(cls, value: str | None) -> str | None:
        if value is not None and any(
            (ord(character) < 32 and character not in "\t\r\n") or ord(character) == 127
            for character in value
        ):
            raise ValueError("experiment description contains a control character")
        return value


class ExperimentUpdate(APIModel):
    expected_row_version: int = Field(ge=1, le=2_147_483_647)
    description: str | None = Field(default=None, max_length=8_192)

    @field_validator("description")
    @classmethod
    def description_has_no_binary_controls(cls, value: str | None) -> str | None:
        return ExperimentCreate.description_has_no_binary_controls(value)

    @model_validator(mode="after")
    def description_is_explicit(self) -> ExperimentUpdate:
        if "description" not in self.model_fields_set:
            raise ValueError("description is required for an experiment update")
        return self


class ExperimentRead(APIModel):
    id: str
    row_version: int
    name: str
    dataset_id: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class ExperimentList(APIModel):
    items: list[ExperimentRead]
    total: int
    offset: int
    limit: int


ModelRegistryEntryStatus = Literal["candidate", "approved", "revoked"]
ModelRegistryRevokeReason = Literal[
    "quality_rejected",
    "security_issue",
    "operator_request",
]


class ModelRegistryArtifactRead(APIModel):
    id: str
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelRegistryEntryRead(APIModel):
    id: str
    experiment_id: str
    row_version: int = Field(ge=1)
    status: ModelRegistryEntryStatus
    is_active: bool
    source_job_id: str
    source_attempt_id: str
    source_job_name: str = Field(min_length=1, max_length=128)
    source_attempt_number: int = Field(ge=1)
    engine_mode: Literal["rvc_webui"]
    job_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rvc_commit_hash: str = Field(pattern=r"^[0-9a-f]{40}$")
    runtime_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runtime_asset_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: ModelRegistryArtifactRead
    index: ModelRegistryArtifactRead | None
    created_at: datetime
    approved_at: datetime | None
    revoked_at: datetime | None
    revoke_reason: ModelRegistryRevokeReason | None


class ModelRegistryRead(APIModel):
    experiment_id: str
    registry_row_version: int = Field(ge=0)
    active_entry_id: str | None
    can_manage: bool
    items: list[ModelRegistryEntryRead]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)


class ModelRegistryCandidateCreate(APIModel):
    expected_registry_row_version: int = Field(ge=0, le=2_147_483_647)
    source_job_id: UUID
    source_attempt_id: UUID
    model_artifact_id: UUID


class ModelRegistryEntryPromote(APIModel):
    expected_registry_row_version: int = Field(ge=1, le=2_147_483_647)
    expected_entry_row_version: int = Field(ge=1, le=2_147_483_647)


class ModelRegistryEntryRevoke(APIModel):
    expected_registry_row_version: int = Field(ge=1, le=2_147_483_647)
    expected_entry_row_version: int = Field(ge=1, le=2_147_483_647)
    reason_code: ModelRegistryRevokeReason


class ModelRegistryMutationRead(APIModel):
    experiment_id: str
    registry_row_version: int = Field(ge=1)
    active_entry_id: str | None
    entry: ModelRegistryEntryRead


class ExperimentComparisonAttemptRead(APIModel):
    id: str
    attempt_number: int = Field(ge=1)
    engine_mode: WorkerEngineMode
    status: JobStatus
    started_at: datetime
    finished_at: datetime | None


class ExperimentComparisonMetricPoint(APIModel):
    sequence: int = Field(ge=0)
    epoch: int | None = Field(default=None, ge=0)
    step: int | None = Field(default=None, ge=0)
    value: float
    occurred_at: datetime


class ExperimentComparisonMetricSeries(APIModel):
    key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    total_points: int = Field(ge=1)
    truncated: bool
    points: list[ExperimentComparisonMetricPoint] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def truncation_matches_total(self) -> ExperimentComparisonMetricSeries:
        if self.total_points < len(self.points):
            raise ValueError("metric series total cannot be smaller than returned points")
        if self.truncated != (self.total_points > len(self.points)):
            raise ValueError("metric series truncation flag does not match its total")
        return self


class ExperimentComparisonArtifactRead(APIModel):
    id: str
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExperimentComparisonSampleRead(APIModel):
    id: str
    test_set_item_id: str
    output_size_bytes: int = Field(gt=0)
    output_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    output_sample_rate_hz: int = Field(gt=0)
    output_channels: int = Field(gt=0)
    output_duration_seconds: float = Field(gt=0)
    created_at: datetime


class ExperimentComparisonAvailability(APIModel):
    final_model: ExperimentComparisonArtifactRead | None
    final_index: ExperimentComparisonArtifactRead | None
    samples: list[ExperimentComparisonSampleRead] = Field(max_length=128)


class ExperimentComparisonJobRead(APIModel):
    id: str
    job_name: str
    status: JobStatus
    config: JobConfig
    current_epoch: int | None
    total_epoch: int
    current_attempt: ExperimentComparisonAttemptRead | None
    metrics: list[ExperimentComparisonMetricSeries]
    availability: ExperimentComparisonAvailability


class ExperimentComparisonRead(APIModel):
    experiment: ExperimentRead
    jobs: list[ExperimentComparisonJobRead] = Field(min_length=2, max_length=16)
    metric_point_limit_per_key: Literal[200] = 200


class TestSetCreate(APIModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=8_192)

    @field_validator("name")
    @classmethod
    def normalize_test_set_name(cls, value: str) -> str:
        normalized = value.strip()
        if (
            not normalized
            or normalized in {".", ".."}
            or "/" in normalized
            or "\\" in normalized
            or any(ord(character) < 32 for character in normalized)
        ):
            raise ValueError("test set name must be a safe display name")
        return normalized


class TestSetRevisionCreate(APIModel):
    description: str | None = Field(default=None, max_length=8_192)


_TEST_SET_WAV_CONTENT_TYPES = frozenset({"audio/wav", "audio/x-wav", "audio/wave"})
_OPAQUE_RECORD_REFERENCE = re.compile(r"^[a-z][a-z0-9._-]{0,63}:[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_RECORD_REFERENCE_NAMESPACES = frozenset(
    {
        "license",
        "license-record",
        "rights",
        "rights-record",
        "spdx",
        "provenance",
        "provenance-record",
        "consent",
        "consent-record",
        "record",
    }
)


class TestSetItemUploadInitRequest(APIModel):
    item_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$",
    )
    display_name: str = Field(min_length=1, max_length=255)
    sort_order: int = Field(ge=0, le=999_999)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=3, max_length=255)
    size_bytes: int = Field(ge=44)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    license_reference: str = Field(min_length=3, max_length=320)
    provenance_reference: str = Field(min_length=3, max_length=320)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("item_key")
    @classmethod
    def safe_item_key(cls, value: str) -> str:
        if value in {".", ".."} or ".." in value:
            raise ValueError("item_key must not contain path-like segments")
        return value

    @field_validator("display_name")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("value must not be blank or contain control characters")
        return normalized

    @field_validator("license_reference", "provenance_reference")
    @classmethod
    def opaque_record_reference(cls, value: str) -> str:
        normalized = value.strip()
        namespace, separator, _ = normalized.partition(":")
        if (
            not separator
            or namespace not in _RECORD_REFERENCE_NAMESPACES
            or not _OPAQUE_RECORD_REFERENCE.fullmatch(normalized)
        ):
            raise ValueError(
                "reference must be an opaque namespaced record ID without a path or query"
            )
        return normalized

    @field_validator("filename")
    @classmethod
    def safe_wav_filename(cls, value: str) -> str:
        if (
            value in {".", ".."}
            or PurePath(value).name != value
            or "\\" in value
            or any(ord(character) < 32 for character in value)
            or PurePath(value).suffix.lower() != ".wav"
        ):
            raise ValueError("test set filename must be a basename ending in .wav")
        return value

    @field_validator("content_type")
    @classmethod
    def normalize_wav_content_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _TEST_SET_WAV_CONTENT_TYPES:
            raise ValueError("test set item content_type must be WAV audio")
        return normalized

    @field_validator("sha256")
    @classmethod
    def normalize_item_sha256(cls, value: str) -> str:
        return value.lower()


class TestSetItemRead(APIModel):
    id: str
    item_key: str
    display_name: str
    sort_order: int
    original_filename: str
    size_bytes: int
    sha256: str
    mime_type: str
    sample_rate_hz: int
    channels: int
    duration_seconds: float
    license_reference: str
    provenance_reference: str
    created_at: datetime


class TestSetItemUploadInitResponse(APIModel):
    upload_session_id: str
    test_set_id: str
    status: Literal["pending", "finalizing", "completed", "failed", "expired"]
    method: Literal["PUT"] | None = None
    upload_url: str | None = None
    upload_headers: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime
    item: TestSetItemRead | None = None
    failure_code: str | None = None


class TestSetRead(APIModel):
    id: str
    family_id: str
    name: str
    revision: int
    description: str | None
    status: Literal["draft", "ready", "failed"]
    manifest_sha256: str | None
    item_count: int
    failure_code: str | None
    items_included: bool
    items: list[TestSetItemRead] = Field(default_factory=list)
    finalized_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TestSetList(APIModel):
    items: list[TestSetRead]
    total: int
    offset: int
    limit: int


class PresetCreate(APIModel):
    name: str = Field(min_length=1, max_length=128)
    config: InferencePresetConfig

    @field_validator("name")
    @classmethod
    def normalize_preset_name(cls, value: str) -> str:
        normalized = value.strip()
        if (
            not normalized
            or normalized in {".", ".."}
            or "/" in normalized
            or "\\" in normalized
            or any(ord(character) < 32 for character in normalized)
        ):
            raise ValueError("preset name must be a safe display name")
        return normalized


class PresetRevisionCreate(APIModel):
    config: InferencePresetConfig


class PresetRead(APIModel):
    id: str
    family_id: str
    name: str
    revision: int
    config: InferencePresetConfig
    config_sha256: str
    created_at: datetime
    updated_at: datetime


class PresetList(APIModel):
    items: list[PresetRead]
    total: int
    offset: int
    limit: int


class JobRead(APIModel):
    id: str
    experiment_id: str
    dataset_id: str
    worker_id: str | None
    job_name: str
    status: JobStatus
    config: JobConfig
    config_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    test_set_id: str | None
    preset_id: str | None
    sample_plan_sha256: str | None
    priority: int
    current_epoch: int | None
    total_epoch: int
    attempt_count: int
    current_attempt_id: str | None
    current_attempt_engine_mode: WorkerEngineMode | None
    cancel_requested_at: datetime | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class JobList(APIModel):
    items: list[JobRead]
    total: int
    offset: int
    limit: int


class OperationAck(APIModel):
    accepted: int = 0
    duplicate: bool = False


class StatusAck(APIModel):
    job_id: str
    status: JobStatus
    lease_expires_at: datetime | None = None


_ARTIFACT_CONTENT_TYPES: dict[ArtifactType, frozenset[str]] = {
    ArtifactType.FINAL_SMALL_MODEL: frozenset(
        {"application/octet-stream", "application/x-pytorch"}
    ),
    ArtifactType.FINAL_INDEX: frozenset({"application/octet-stream"}),
    ArtifactType.TOTAL_FEATURES: frozenset({"application/octet-stream"}),
    ArtifactType.GENERATOR_CHECKPOINT: frozenset(
        {"application/octet-stream", "application/x-pytorch"}
    ),
    ArtifactType.DISCRIMINATOR_CHECKPOINT: frozenset(
        {"application/octet-stream", "application/x-pytorch"}
    ),
    ArtifactType.TRAIN_LOG: frozenset({"text/plain", "application/octet-stream"}),
    ArtifactType.TENSORBOARD: frozenset({"application/octet-stream"}),
    ArtifactType.SAMPLE: frozenset({"audio/wav", "audio/x-wav", "audio/flac", "audio/mpeg"}),
    ArtifactType.ENVIRONMENT: frozenset({"application/json"}),
    ArtifactType.CONFIG: frozenset({"application/json"}),
    ArtifactType.DATASET_REPORT: frozenset({"application/json"}),
}


class ArtifactRead(APIModel):
    id: str
    job_id: str
    attempt_id: str
    artifact_type: ArtifactType
    filename: str
    size_bytes: int
    sha256: str
    mime_type: str | None
    metadata_json: dict[str, Any]
    created_at: datetime


class ArtifactList(APIModel):
    items: list[ArtifactRead]
    total: int
    offset: int
    limit: int


class JobLogRead(APIModel):
    id: str
    job_id: str
    attempt_id: str
    attempt_number: int
    sequence: int
    level: LogLevel
    message: str
    fields: dict[str, Any]
    occurred_at: datetime


class JobLogList(APIModel):
    items: list[JobLogRead]
    total: int
    limit: int
    has_more: bool
    next_cursor: str | None


class MetricRead(APIModel):
    id: str
    job_id: str
    attempt_id: str
    attempt_number: int
    sequence: int
    epoch: int | None
    step: int | None
    key: str
    value: float
    occurred_at: datetime


class MetricList(APIModel):
    items: list[MetricRead]
    total: int
    offset: int
    limit: int


class ArtifactUploadInitRequest(APIModel):
    lease_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)
    artifact_type: ArtifactType
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=3, max_length=255)
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if (
            value in {".", ".."}
            or PurePath(value).name != value
            or "\\" in value
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("artifact filename must not contain a path or control characters")
        return value

    @field_validator("content_type")
    @classmethod
    def normalize_content_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if ";" in normalized or "/" not in normalized:
            raise ValueError("content_type must be a media type without parameters")
        return normalized

    @field_validator("sha256")
    @classmethod
    def normalize_sha256(cls, value: str) -> str:
        return value.lower()

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("artifact metadata must be JSON serializable") from exc
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise ValueError("artifact metadata exceeds 16 KiB")
        return value

    @model_validator(mode="after")
    def validate_content_type_for_artifact(self) -> ArtifactUploadInitRequest:
        allowed = _ARTIFACT_CONTENT_TYPES[self.artifact_type]
        if self.content_type not in allowed:
            raise ValueError(
                f"content_type {self.content_type!r} is not allowed for {self.artifact_type.value}"
            )
        return self


class ArtifactUploadFinalizeRequest(APIModel):
    lease_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)


class ArtifactUploadInitResponse(APIModel):
    upload_session_id: str
    status: Literal["pending", "finalizing", "completed", "failed", "expired"]
    method: Literal["PUT"] | None = None
    upload_url: str | None = None
    upload_headers: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime
    artifact: ArtifactRead | None = None
    failure_code: str | None = None
    retryable: bool = False
    retry_after_seconds: int | None = None
