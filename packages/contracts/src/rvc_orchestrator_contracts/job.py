from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, field_validator, model_validator

from .base import ContractModel

SafeName = Annotated[str, Field(min_length=1, max_length=128)]


def _require_exact_integer(value: object) -> object:
    if type(value) is not int:
        raise ValueError("inference resample_sr must be an exact JSON integer")
    return value


InferenceResampleRate = Annotated[
    Literal[0] | Annotated[int, Field(ge=16_000, le=192_000)],
    BeforeValidator(_require_exact_integer),
]
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class RVCVersion(StrEnum):
    V1 = "v1"
    V2 = "v2"


class SampleRate(StrEnum):
    KHZ_40 = "40k"
    KHZ_48 = "48k"


class TrainingF0Method(StrEnum):
    PM = "pm"
    HARVEST = "harvest"
    DIO = "dio"
    RMVPE = "rmvpe"
    RMVPE_GPU = "rmvpe_gpu"


class InferenceF0Method(StrEnum):
    PM = "pm"
    HARVEST = "harvest"
    CREPE = "crepe"
    RMVPE = "rmvpe"


def feature_directory_for_version(version: RVCVersion | str) -> str:
    parsed = RVCVersion(version)
    return "3_feature256" if parsed is RVCVersion.V1 else "3_feature768"


def _parse_gpu_ids(value: object) -> object:
    if value is None or isinstance(value, list):
        return value
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        if not value.strip():
            return []
        return [int(part.strip()) for part in value.split(",")]
    return value


class RVCBackendConfig(ContractModel):
    backend_type: Literal["rvc_webui"] = "rvc_webui"
    repository: str = "RVC-Project/Retrieval-based-Voice-Conversion-WebUI"
    rvc_version: RVCVersion | None = None
    rvc_commit_hash: str | None = None


class ModelConfig(ContractModel):
    version: RVCVersion
    sample_rate: SampleRate
    use_f0: bool = True
    speaker_id: int = Field(default=0, ge=0)


class PretrainedConfig(ContractModel):
    mode: Literal["auto", "custom"] = "auto"
    g_path: str | None = None
    d_path: str | None = None
    allow_custom_override: bool = False

    @model_validator(mode="after")
    def validate_custom_paths(self) -> PretrainedConfig:
        if self.mode == "custom" and (not self.g_path or not self.d_path):
            raise ValueError("custom pretrained mode requires both g_path and d_path")
        for path in (self.g_path, self.d_path):
            if path and (PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts):
                raise ValueError("pretrained paths must be safe relative paths")
        return self


class TrainingFeatureConfig(ContractModel):
    feature_dir_policy: Literal["auto"] = "auto"
    v1_feature_dir: Literal["3_feature256"] = "3_feature256"
    v2_feature_dir: Literal["3_feature768"] = "3_feature768"


class TrainingConfig(ContractModel):
    epochs: int = Field(default=80, ge=1, le=100_000)
    batch_size_per_gpu: int = Field(default=8, ge=1, le=1024)
    save_every_epoch: int = Field(default=5, ge=1)
    save_only_latest: bool = False
    save_every_weights: bool = True
    cache_dataset_in_gpu: bool = False
    gpu_ids: list[int] = Field(default_factory=lambda: [0], min_length=1)

    _coerce_gpu_ids = field_validator("gpu_ids", mode="before")(_parse_gpu_ids)

    @field_validator("gpu_ids")
    @classmethod
    def validate_gpu_ids(cls, value: list[int]) -> list[int]:
        if any(gpu_id < 0 for gpu_id in value):
            raise ValueError("GPU IDs must be non-negative")
        if len(set(value)) != len(value):
            raise ValueError("GPU IDs must be unique")
        return value


class F0ExtractionConfig(ContractModel):
    training_f0_method: TrainingF0Method | None = TrainingF0Method.RMVPE
    rmvpe_gpu_ids: list[int] | None = None

    _coerce_gpu_ids = field_validator("rmvpe_gpu_ids", mode="before")(_parse_gpu_ids)

    @model_validator(mode="after")
    def validate_rmvpe_gpu_ids(self) -> F0ExtractionConfig:
        if self.training_f0_method is TrainingF0Method.RMVPE_GPU:
            if not self.rmvpe_gpu_ids:
                raise ValueError("rmvpe_gpu requires at least one rmvpe_gpu_id")
        elif self.rmvpe_gpu_ids:
            raise ValueError("rmvpe_gpu_ids are only valid with rmvpe_gpu")
        if self.rmvpe_gpu_ids and any(gpu_id < 0 for gpu_id in self.rmvpe_gpu_ids):
            raise ValueError("RMVPE GPU IDs must be non-negative")
        return self


class IndexConfig(ContractModel):
    build_index: bool = True
    collect_total_fea: bool = True
    collect_added_index: bool = True


class AutoInferenceSamplesConfig(ContractModel):
    enabled: bool = False
    test_set_id: str | None = None
    inference_f0_method: InferenceF0Method = InferenceF0Method.RMVPE
    transpose: int = Field(default=0, ge=-48, le=48)
    index_rate: float = Field(default=0.75, ge=0, le=1)
    filter_radius: int = Field(default=3, ge=0, le=7)
    resample_sr: InferenceResampleRate = 0
    rms_mix_rate: float = Field(default=0.25, ge=0, le=1)
    protect: float = Field(default=0.33, ge=0, le=0.5)

    @model_validator(mode="after")
    def validate_test_set(self) -> AutoInferenceSamplesConfig:
        if self.enabled and not self.test_set_id:
            raise ValueError("enabled sample generation requires test_set_id")
        if not self.enabled and self.test_set_id is not None:
            raise ValueError("disabled sample generation requires test_set_id=null")
        return self


class InferencePresetConfig(ContractModel):
    """Reusable inference settings; Jobs still persist an immutable snapshot."""

    inference_f0_method: InferenceF0Method = InferenceF0Method.RMVPE
    transpose: int = Field(default=0, ge=-48, le=48)
    index_rate: float = Field(default=0.75, ge=0, le=1)
    filter_radius: int = Field(default=3, ge=0, le=7)
    resample_sr: InferenceResampleRate = 0
    rms_mix_rate: float = Field(default=0.25, ge=0, le=1)
    protect: float = Field(default=0.33, ge=0, le=0.5)


class ArtifactCollectionConfig(ContractModel):
    collect_checkpoints: bool = True
    collect_small_model: bool = True
    extract_small_model_if_missing: bool = True
    collect_index: bool = True
    collect_tensorboard: bool = True
    collect_logs: bool = True
    collect_samples: bool = True


class ResourceRequirements(ContractModel):
    min_vram_gb: float = Field(default=0, ge=0)
    preferred_worker_tags: list[str] = Field(default_factory=list, max_length=64)
    priority: int = Field(default=5, ge=0, le=10)

    @field_validator("preferred_worker_tags")
    @classmethod
    def unique_tags(cls, value: list[str]) -> list[str]:
        stripped = [tag.strip() for tag in value]
        if any(not tag for tag in stripped):
            raise ValueError("worker tags cannot be empty")
        if len(set(stripped)) != len(stripped):
            raise ValueError("worker tags must be unique")
        return stripped


class JobConfig(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    job_name: SafeName
    experiment_id: SafeName
    dataset_id: SafeName
    rvc_backend: RVCBackendConfig = Field(default_factory=RVCBackendConfig)
    model: ModelConfig
    pretrained: PretrainedConfig = Field(default_factory=PretrainedConfig)
    training_feature: TrainingFeatureConfig = Field(default_factory=TrainingFeatureConfig)
    training: TrainingConfig = Field(default_factory=TrainingConfig)
    f0_extraction: F0ExtractionConfig = Field(default_factory=F0ExtractionConfig)
    index: IndexConfig = Field(default_factory=IndexConfig)
    auto_inference_samples: AutoInferenceSamplesConfig = Field(
        default_factory=AutoInferenceSamplesConfig
    )
    artifacts: ArtifactCollectionConfig = Field(default_factory=ArtifactCollectionConfig)
    resource: ResourceRequirements = Field(default_factory=ResourceRequirements)

    @field_validator("job_name", "experiment_id", "dataset_id")
    @classmethod
    def validate_safe_identifier(cls, value: str) -> str:
        if not _SAFE_IDENTIFIER.fullmatch(value):
            raise ValueError("identifier may contain only letters, numbers, '.', '_' and '-'")
        return value

    @model_validator(mode="after")
    def validate_cross_fields(self) -> JobConfig:
        if (
            self.rvc_backend.rvc_version is not None
            and self.rvc_backend.rvc_version is not self.model.version
        ):
            raise ValueError("backend and model RVC versions must match")
        if self.model.use_f0 and self.f0_extraction.training_f0_method is None:
            raise ValueError("use_f0=true requires training_f0_method")
        if not self.model.use_f0:
            if self.f0_extraction.training_f0_method is not None:
                raise ValueError("use_f0=false requires training_f0_method=null")
            if self.f0_extraction.rmvpe_gpu_ids:
                raise ValueError("use_f0=false cannot declare rmvpe_gpu_ids")
        if (
            self.auto_inference_samples.enabled
            and not self.index.build_index
            and self.auto_inference_samples.index_rate != 0
        ):
            raise ValueError("sample generation without an index requires index_rate=0")
        if self.auto_inference_samples.enabled:
            if not self.artifacts.collect_samples:
                raise ValueError("sample generation requires collect_samples=true")
            if not self.artifacts.collect_small_model:
                raise ValueError("sample generation requires collect_small_model=true")
            if self.auto_inference_samples.index_rate > 0 and not self.artifacts.collect_index:
                raise ValueError("sample generation with index_rate>0 requires collect_index=true")
            if self.auto_inference_samples.index_rate > 0 and not self.index.collect_added_index:
                raise ValueError(
                    "sample generation with index_rate>0 requires collect_added_index=true"
                )
        return self

    @property
    def feature_directory(self) -> str:
        return feature_directory_for_version(self.model.version)
