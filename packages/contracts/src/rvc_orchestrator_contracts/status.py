from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class JobStatus(StrEnum):
    QUEUED = "queued"
    ASSIGNED = "assigned"
    DOWNLOADING_DATASET = "downloading_dataset"
    VALIDATING_DATASET = "validating_dataset"
    PREPARING_FLAT_DATASET = "preparing_flat_dataset"
    PREPROCESSING = "preprocessing"
    EXTRACTING_F0 = "extracting_f0"
    EXTRACTING_FEATURES = "extracting_features"
    TRAINING = "training"
    SAVING_CHECKPOINT = "saving_checkpoint"
    BUILDING_INDEX = "building_index"
    COLLECTING_SMALL_MODEL = "collecting_small_model"
    GENERATING_SAMPLES = "generating_samples"
    EVALUATING = "evaluating"
    UPLOADING_ARTIFACTS = "uploading_artifacts"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYING = "retrying"


TERMINAL_JOB_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})

_failure_or_cancel = {JobStatus.FAILED, JobStatus.CANCELLED}
_transitions: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.ASSIGNED, JobStatus.CANCELLED}),
    JobStatus.ASSIGNED: frozenset({JobStatus.DOWNLOADING_DATASET, *_failure_or_cancel}),
    JobStatus.DOWNLOADING_DATASET: frozenset({JobStatus.VALIDATING_DATASET, *_failure_or_cancel}),
    JobStatus.VALIDATING_DATASET: frozenset(
        {
            JobStatus.PREPARING_FLAT_DATASET,
            JobStatus.PREPROCESSING,
            *_failure_or_cancel,
        }
    ),
    JobStatus.PREPARING_FLAT_DATASET: frozenset({JobStatus.PREPROCESSING, *_failure_or_cancel}),
    JobStatus.PREPROCESSING: frozenset(
        {
            JobStatus.EXTRACTING_F0,
            JobStatus.EXTRACTING_FEATURES,
            *_failure_or_cancel,
        }
    ),
    JobStatus.EXTRACTING_F0: frozenset({JobStatus.EXTRACTING_FEATURES, *_failure_or_cancel}),
    JobStatus.EXTRACTING_FEATURES: frozenset({JobStatus.TRAINING, *_failure_or_cancel}),
    JobStatus.TRAINING: frozenset(
        {
            JobStatus.SAVING_CHECKPOINT,
            JobStatus.BUILDING_INDEX,
            JobStatus.COLLECTING_SMALL_MODEL,
            *_failure_or_cancel,
        }
    ),
    JobStatus.SAVING_CHECKPOINT: frozenset(
        {
            JobStatus.BUILDING_INDEX,
            JobStatus.COLLECTING_SMALL_MODEL,
            *_failure_or_cancel,
        }
    ),
    JobStatus.BUILDING_INDEX: frozenset({JobStatus.COLLECTING_SMALL_MODEL, *_failure_or_cancel}),
    JobStatus.COLLECTING_SMALL_MODEL: frozenset(
        {
            JobStatus.GENERATING_SAMPLES,
            JobStatus.EVALUATING,
            JobStatus.UPLOADING_ARTIFACTS,
            *_failure_or_cancel,
        }
    ),
    JobStatus.GENERATING_SAMPLES: frozenset(
        {
            JobStatus.EVALUATING,
            JobStatus.UPLOADING_ARTIFACTS,
            *_failure_or_cancel,
        }
    ),
    JobStatus.EVALUATING: frozenset({JobStatus.UPLOADING_ARTIFACTS, *_failure_or_cancel}),
    JobStatus.UPLOADING_ARTIFACTS: frozenset({JobStatus.COMPLETED, *_failure_or_cancel}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset({JobStatus.RETRYING}),
    JobStatus.CANCELLED: frozenset(),
    JobStatus.RETRYING: frozenset({JobStatus.QUEUED}),
}

ALLOWED_JOB_TRANSITIONS: Mapping[JobStatus, frozenset[JobStatus]] = MappingProxyType(_transitions)


class InvalidJobTransition(ValueError):
    pass


def can_transition_job(
    current: JobStatus | str,
    target: JobStatus | str,
    *,
    allow_same: bool = True,
) -> bool:
    try:
        current_status = JobStatus(current)
        target_status = JobStatus(target)
    except ValueError:
        return False
    if allow_same and current_status == target_status:
        return True
    return target_status in ALLOWED_JOB_TRANSITIONS[current_status]


def validate_job_transition(
    current: JobStatus | str,
    target: JobStatus | str,
    *,
    allow_same: bool = True,
) -> JobStatus:
    try:
        current_status = JobStatus(current)
        target_status = JobStatus(target)
    except ValueError as exc:
        raise InvalidJobTransition(f"unknown job status: {exc}") from exc
    if not can_transition_job(current_status, target_status, allow_same=allow_same):
        raise InvalidJobTransition(
            f"job status cannot transition from {current_status.value!r} to {target_status.value!r}"
        )
    return target_status
