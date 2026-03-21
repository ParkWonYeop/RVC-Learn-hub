from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field, field_validator, model_validator

from .base import ContractModel
from .job import InferenceF0Method

RVC_REVIEWED_COMMIT = "7ef19867780cf703841ebafb565a4e47d1ea86ff"
SAMPLE_MAX_OUTPUT_BYTES: Final = 256 * 1024**2
SAMPLE_MAX_OUTPUT_CHANNELS: Final = 2
SAMPLE_MAX_OUTPUT_DURATION_SECONDS: Final = 600.0
SAMPLE_MAX_TOTAL_OUTPUT_BYTES: Final = 2 * 1024**3
SAMPLE_MAX_TOTAL_OUTPUT_DURATION_SECONDS: Final = 3_600.0
SAMPLE_PCM_METRICS_ALGORITHM: Final[Literal["pcm-normalized-v2"]] = "pcm-normalized-v2"
SAMPLE_PCM_CLIPPING_THRESHOLD = 0.999
SAMPLE_PCM_SILENCE_THRESHOLD = 0.0001

_UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IMAGE_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"


def _exact_integer(value: object) -> object:
    if type(value) is not int:
        raise ValueError("value must be an exact JSON integer")
    return value


def _exact_number(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("value must be a JSON number")
    return value


PositiveSize = Annotated[
    int,
    BeforeValidator(_exact_integer),
    Field(gt=0, le=SAMPLE_MAX_OUTPUT_BYTES),
]
PositiveSampleRate = Annotated[
    int,
    BeforeValidator(_exact_integer),
    Field(ge=8_000, le=192_000),
]
PositiveChannels = Annotated[
    int,
    BeforeValidator(_exact_integer),
    Field(ge=1, le=SAMPLE_MAX_OUTPUT_CHANNELS),
]
PositiveDuration = Annotated[
    float,
    BeforeValidator(_exact_number),
    Field(gt=0, le=SAMPLE_MAX_OUTPUT_DURATION_SECONDS),
]
UnitMetric = Annotated[
    float,
    BeforeValidator(_exact_number),
    Field(ge=0, le=1),
]


class SampleMetricValues(ContractModel):
    """Deterministic normalized PCM metrics, over every interleaved sample."""

    peak_amplitude: UnitMetric
    rms: UnitMetric
    clipping_ratio: UnitMetric
    silence_ratio: UnitMetric

    @field_validator("peak_amplitude", "rms", "clipping_ratio", "silence_ratio")
    @classmethod
    def finite_metric(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("sample metrics must be finite")
        return value


class SampleMetricsEvidence(ContractModel):
    algorithm: Literal["pcm-normalized-v2"] = "pcm-normalized-v2"
    authoritative_source: Literal["manager_computed"] = "manager_computed"
    clipping_threshold: float = Field(default=SAMPLE_PCM_CLIPPING_THRESHOLD, ge=0, le=1)
    silence_threshold: float = Field(default=SAMPLE_PCM_SILENCE_THRESHOLD, ge=0, le=1)
    worker_reported: SampleMetricValues
    manager_computed: SampleMetricValues
    worker_reported_duration_seconds: PositiveDuration
    manager_computed_sample_rate_hz: PositiveSampleRate
    manager_computed_channels: PositiveChannels
    manager_computed_duration_seconds: PositiveDuration

    @model_validator(mode="after")
    def fixed_algorithm_parameters(self) -> SampleMetricsEvidence:
        if (
            self.clipping_threshold != SAMPLE_PCM_CLIPPING_THRESHOLD
            or self.silence_threshold != SAMPLE_PCM_SILENCE_THRESHOLD
        ):
            raise ValueError("sample metric algorithm parameters are immutable")
        return self


class SampleRegistrationRequest(ContractModel):
    lease_id: str = Field(strict=True, pattern=_UUID_PATTERN, min_length=36, max_length=36)
    attempt_id: str = Field(strict=True, pattern=_UUID_PATTERN, min_length=36, max_length=36)
    test_set_id: str = Field(strict=True, pattern=_UUID_PATTERN, min_length=36, max_length=36)
    test_set_item_id: str = Field(strict=True, pattern=_UUID_PATTERN, min_length=36, max_length=36)
    artifact_id: str = Field(strict=True, pattern=_UUID_PATTERN, min_length=36, max_length=36)
    sample_plan_sha256: str = Field(
        strict=True, pattern=_SHA256_PATTERN, min_length=64, max_length=64
    )
    input_sha256: str = Field(strict=True, pattern=_SHA256_PATTERN, min_length=64, max_length=64)
    model_sha256: str = Field(strict=True, pattern=_SHA256_PATTERN, min_length=64, max_length=64)
    index_sha256: str | None = Field(
        default=None,
        strict=True,
        pattern=_SHA256_PATTERN,
        min_length=64,
        max_length=64,
    )
    inference_f0_method: InferenceF0Method
    inference_config_sha256: str = Field(
        strict=True,
        pattern=_SHA256_PATTERN,
        min_length=64,
        max_length=64,
    )
    native_inference_manifest_sha256: str = Field(
        strict=True,
        pattern=_SHA256_PATTERN,
        min_length=64,
        max_length=64,
    )
    native_inference_request_sha256: str = Field(
        strict=True,
        pattern=_SHA256_PATTERN,
        min_length=64,
        max_length=64,
    )
    output_size_bytes: PositiveSize
    output_sha256: str = Field(strict=True, pattern=_SHA256_PATTERN, min_length=64, max_length=64)
    output_sample_rate_hz: PositiveSampleRate
    output_channels: PositiveChannels
    output_duration_seconds: PositiveDuration
    metrics: SampleMetricValues
    rvc_commit_hash: str = Field(
        strict=True, pattern=r"^[0-9a-f]{40}$", min_length=40, max_length=40
    )
    runtime_image_digest: str = Field(
        pattern=_IMAGE_DIGEST_PATTERN,
        strict=True,
        min_length=71,
        max_length=71,
    )
    runtime_asset_manifest_sha256: str = Field(
        pattern=_SHA256_PATTERN,
        strict=True,
        min_length=64,
        max_length=64,
    )

    @model_validator(mode="after")
    def bounded_registration(self) -> SampleRegistrationRequest:
        if not math.isfinite(self.output_duration_seconds):
            raise ValueError("output duration must be finite")
        # The schema is fixed-width apart from the optional index hash; this guards
        # future extensions from silently turning registration into an unbounded
        # metadata transport.
        if len(self.model_dump_json().encode("utf-8")) > 4 * 1024:
            raise ValueError("sample registration exceeds 4 KiB")
        return self


class SampleRead(ContractModel):
    id: str
    job_id: str
    attempt_id: str
    test_set_id: str
    test_set_item_id: str
    artifact_id: str
    input_sha256: str
    model_sha256: str
    index_sha256: str | None
    inference_f0_method: InferenceF0Method
    inference_config_sha256: str
    native_inference_manifest_sha256: str
    native_inference_request_sha256: str
    output_size_bytes: int
    output_sha256: str
    output_sample_rate_hz: int
    output_channels: int
    output_duration_seconds: float
    metrics: SampleMetricsEvidence
    rvc_commit_hash: str
    runtime_image_digest: str
    runtime_asset_manifest_sha256: str
    created_at: datetime


class SampleList(ContractModel):
    items: list[SampleRead]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
