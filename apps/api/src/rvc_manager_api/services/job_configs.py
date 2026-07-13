from __future__ import annotations

import re

from rvc_orchestrator_contracts import JobConfig, job_config_sha256

from ..models import Job, JobAttempt

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class InvalidJobConfigLedger(ValueError):
    """The stored JobConfig cannot be proven to match its immutable ledger."""


def validated_job_config(
    job: Job,
    *,
    attempt: JobAttempt | None = None,
) -> JobConfig:
    stored_sha256 = job.config_sha256
    if stored_sha256 is None or _SHA256.fullmatch(stored_sha256) is None:
        raise InvalidJobConfigLedger("Job config hash is missing or malformed")
    try:
        raw_sha256 = job_config_sha256(job.config_json)
        config = JobConfig.model_validate(job.config_json)
        normalized_sha256 = job_config_sha256(config)
    except (TypeError, ValueError) as exc:
        raise InvalidJobConfigLedger("Job config snapshot is invalid") from exc
    if raw_sha256 != stored_sha256 or normalized_sha256 != stored_sha256:
        raise InvalidJobConfigLedger("Job config snapshot does not match its hash")
    if (
        config.job_name != job.job_name
        or config.experiment_id != job.experiment_id
        or config.dataset_id != job.dataset_id
        or config.training.epochs != job.total_epoch
        or config.resource.priority != job.priority
        or config.auto_inference_samples.test_set_id != job.test_set_id
    ):
        raise InvalidJobConfigLedger("Job config snapshot does not match the Job ledger")
    if attempt is not None and (
        attempt.job_id != job.id or attempt.job_config_sha256 != stored_sha256
    ):
        raise InvalidJobConfigLedger("Job attempt does not match the Job config snapshot")
    return config
