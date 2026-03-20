from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from pathlib import PurePath
from typing import Any

from pydantic import Field, field_validator

from .base import ContractModel, utc_now


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class LogEntry(ContractModel):
    sequence: int = Field(ge=0)
    level: LogLevel = LogLevel.INFO
    message: str = Field(min_length=1, max_length=32_768)
    occurred_at: datetime = Field(default_factory=utc_now)
    fields: dict[str, Any] = Field(default_factory=dict)


class LogBatch(ContractModel):
    lease_id: str
    attempt_id: str
    idempotency_key: str = Field(min_length=8, max_length=128)
    entries: list[LogEntry] = Field(min_length=1, max_length=1_000)

    @field_validator("entries")
    @classmethod
    def unique_sequences(cls, value: list[LogEntry]) -> list[LogEntry]:
        if len({entry.sequence for entry in value}) != len(value):
            raise ValueError("log sequences must be unique within a batch")
        return value


class MetricEntry(ContractModel):
    sequence: int = Field(ge=0)
    key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    value: float
    epoch: int | None = Field(default=None, ge=0)
    step: int | None = Field(default=None, ge=0)
    occurred_at: datetime = Field(default_factory=utc_now)

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("metric values must be finite")
        return value


class MetricBatch(ContractModel):
    lease_id: str
    attempt_id: str
    idempotency_key: str = Field(min_length=8, max_length=128)
    entries: list[MetricEntry] = Field(min_length=1, max_length=1_000)

    @field_validator("entries")
    @classmethod
    def unique_sequences(cls, value: list[MetricEntry]) -> list[MetricEntry]:
        if len({entry.sequence for entry in value}) != len(value):
            raise ValueError("metric sequences must be unique within a batch")
        return value


class ArtifactType(StrEnum):
    FINAL_SMALL_MODEL = "final_small_model"
    FINAL_INDEX = "final_index"
    TOTAL_FEATURES = "total_features"
    GENERATOR_CHECKPOINT = "generator_checkpoint"
    DISCRIMINATOR_CHECKPOINT = "discriminator_checkpoint"
    TRAIN_LOG = "train_log"
    TENSORBOARD = "tensorboard"
    SAMPLE = "sample"
    ENVIRONMENT = "environment"
    CONFIG = "config"
    DATASET_REPORT = "dataset_report"


class ArtifactItem(ContractModel):
    artifact_type: ArtifactType
    filename: str = Field(min_length=1, max_length=255)
    storage_uri: str = Field(min_length=1, max_length=2_048)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    mime_type: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if value in {".", ".."} or PurePath(value).name != value or "\\" in value:
            raise ValueError("artifact filename must not contain a path")
        return value

    @field_validator("sha256")
    @classmethod
    def normalize_sha256(cls, value: str) -> str:
        return value.lower()


class ArtifactBatch(ContractModel):
    lease_id: str
    attempt_id: str
    idempotency_key: str = Field(min_length=8, max_length=128)
    artifacts: list[ArtifactItem] = Field(min_length=1, max_length=100)

    @field_validator("artifacts")
    @classmethod
    def unique_artifacts(cls, value: list[ArtifactItem]) -> list[ArtifactItem]:
        keys = {(item.artifact_type, item.sha256) for item in value}
        if len(keys) != len(value):
            raise ValueError("artifact type and checksum pairs must be unique within a batch")
        return value
