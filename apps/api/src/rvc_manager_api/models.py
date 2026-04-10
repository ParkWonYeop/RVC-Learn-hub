from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from rvc_orchestrator_contracts import JobStatus, WorkerStatus, utc_now

from .database import Base

JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")


def new_id() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'user')", name="role_allowed"),
        CheckConstraint("row_version >= 1", name="row_version_positive"),
        CheckConstraint(
            "access_token_version >= 1",
            name="access_token_version_positive",
        ),
        Index(
            "ix_users_role_disabled_created_at",
            "role",
            "disabled",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __mapper_args__ = {"version_id_col": row_version}
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="user", nullable=False)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    access_token_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class AdminUserOperation(Base):
    """Durable, secret-free idempotency ledger for administrator user writes."""

    __tablename__ = "admin_user_operations"
    __table_args__ = (
        CheckConstraint(
            "operation_type IN ('create', 'access_update', 'password_reset')",
            name="operation_type_allowed",
        ),
        UniqueConstraint(
            "actor_id",
            "idempotency_key_hash",
            name="uq_admin_user_operation_actor_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class RevokedAccessToken(Base):
    __tablename__ = "revoked_access_tokens"

    jti: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AdminBootstrapState(Base):
    __tablename__ = "admin_bootstrap_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), unique=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Worker(Base, TimestampMixin):
    __tablename__ = "workers"
    __table_args__ = (
        CheckConstraint(
            "(token_rotation_id IS NULL AND pending_token_hash IS NULL "
            "AND token_rotation_started_at IS NULL AND token_rotation_expires_at IS NULL) "
            "OR (token_rotation_id IS NOT NULL AND pending_token_hash IS NOT NULL "
            "AND token_rotation_started_at IS NOT NULL "
            "AND token_rotation_expires_at IS NOT NULL)",
            name="token_rotation_fields_together",
        ),
        Index(
            "uq_workers_pending_token_hash",
            "pending_token_hash",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __mapper_args__ = {"version_id_col": row_version}
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    token_issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    token_rotation_id: Mapped[str | None] = mapped_column(String(36))
    pending_token_hash: Mapped[str | None] = mapped_column(String(64))
    token_rotation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_rotation_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(32), default=WorkerStatus.IDLE.value, nullable=False, index=True
    )
    capabilities_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    worker_version: Mapped[str] = mapped_column(String(128), nullable=False)
    rvc_commit_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_job_id: Mapped[str | None] = mapped_column(String(36), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Dataset(Base, TimestampMixin):
    __tablename__ = "datasets"
    __table_args__ = (
        CheckConstraint(
            "source_file_entry_count IS NULL OR source_file_entry_count >= 0",
            name="source_file_entry_count_nonnegative",
        ),
        CheckConstraint(
            "skipped_file_count IS NULL OR skipped_file_count >= 0",
            name="skipped_file_count_nonnegative",
        ),
        CheckConstraint(
            "rejected_file_count IS NULL OR rejected_file_count >= 0",
            name="rejected_file_count_nonnegative",
        ),
        CheckConstraint(
            "duplicate_file_count IS NULL OR duplicate_file_count >= 0",
            name="duplicate_file_count_nonnegative",
        ),
        CheckConstraint(
            "(pcm_quality_algorithm IS NULL AND pcm_validated_file_count IS NULL "
            "AND pcm_sample_count IS NULL AND pcm_clipping_ratio IS NULL "
            "AND pcm_silence_ratio IS NULL AND pcm_rms_ratio IS NULL "
            "AND pcm_silence_threshold_dbfs IS NULL) OR "
            "(pcm_quality_algorithm = 'pcm-sample-weighted-v1' "
            "AND pcm_validated_file_count IS NOT NULL "
            "AND pcm_sample_count IS NOT NULL "
            "AND pcm_clipping_ratio IS NOT NULL "
            "AND pcm_silence_ratio IS NOT NULL "
            "AND pcm_rms_ratio IS NOT NULL "
            "AND pcm_silence_threshold_dbfs IS NOT NULL "
            "AND pcm_validated_file_count > 0 AND pcm_sample_count > 0 "
            "AND pcm_clipping_ratio >= 0 AND pcm_clipping_ratio <= 1 "
            "AND pcm_silence_ratio >= 0 AND pcm_silence_ratio <= 1 "
            "AND pcm_rms_ratio >= 0 AND pcm_rms_ratio <= 1 "
            "AND pcm_silence_threshold_dbfs >= -120 "
            "AND pcm_silence_threshold_dbfs < 0)",
            name="pcm_quality_complete_and_bounded",
        ),
        CheckConstraint(
            "pcm_validated_file_count IS NULL OR "
            "(file_count IS NOT NULL AND pcm_validated_file_count <= file_count)",
            name="pcm_validated_file_count_within_dataset",
        ),
        CheckConstraint(
            "(pcm_loudness_algorithm IS NULL "
            "AND pcm_loudness_analyzed_file_count IS NULL "
            "AND pcm_loudness_block_count IS NULL "
            "AND pcm_loudness_gated_block_count IS NULL "
            "AND pcm_integrated_lufs IS NULL "
            "AND pcm_loudness_unavailable_reason IS NULL) OR "
            "(pcm_loudness_algorithm = 'itu-r-bs1770-4-mono-stereo-v1' "
            "AND pcm_loudness_analyzed_file_count IS NOT NULL "
            "AND pcm_loudness_block_count IS NOT NULL "
            "AND pcm_loudness_gated_block_count IS NOT NULL "
            "AND pcm_loudness_analyzed_file_count >= 0 "
            "AND pcm_loudness_analyzed_file_count <= 10000 "
            "AND pcm_loudness_block_count >= 0 "
            "AND pcm_loudness_block_count <= 9007199254740991 "
            "AND pcm_loudness_gated_block_count >= 0 "
            "AND pcm_loudness_gated_block_count <= 9007199254740991 "
            "AND pcm_loudness_gated_block_count <= pcm_loudness_block_count "
            "AND ((pcm_integrated_lufs IS NOT NULL "
            "AND pcm_integrated_lufs >= -70 AND pcm_integrated_lufs <= 10 "
            "AND pcm_loudness_unavailable_reason IS NULL "
            "AND pcm_loudness_analyzed_file_count > 0 "
            "AND pcm_loudness_block_count > 0 "
            "AND pcm_loudness_gated_block_count > 0) OR "
            "(pcm_integrated_lufs IS NULL "
            "AND pcm_loudness_gated_block_count = 0 "
            "AND ((pcm_loudness_unavailable_reason IN "
            "('unsupported_channel_layout', 'unsupported_sample_rate') "
            "AND pcm_loudness_analyzed_file_count = 0 "
            "AND pcm_loudness_block_count = 0) OR "
            "(pcm_loudness_unavailable_reason = 'insufficient_duration' "
            "AND pcm_loudness_analyzed_file_count > 0 "
            "AND pcm_loudness_block_count = 0) OR "
            "(pcm_loudness_unavailable_reason = 'below_absolute_gate' "
            "AND pcm_loudness_analyzed_file_count > 0 "
            "AND pcm_loudness_block_count > 0)))))",
            name="pcm_loudness_complete_and_bounded",
        ),
        CheckConstraint(
            "pcm_loudness_analyzed_file_count IS NULL OR "
            "(pcm_validated_file_count IS NOT NULL AND "
            "pcm_loudness_analyzed_file_count <= pcm_validated_file_count)",
            name="pcm_loudness_file_count_within_pcm",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    flat_storage_uri: Mapped[str | None] = mapped_column(String(2048))
    duration_sec: Mapped[float | None] = mapped_column(Float)
    file_count: Mapped[int | None] = mapped_column(Integer)
    sample_rate: Mapped[int | None] = mapped_column(Integer)
    quality_report_json: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    source_file_entry_count: Mapped[int | None] = mapped_column(Integer)
    skipped_file_count: Mapped[int | None] = mapped_column(Integer)
    rejected_file_count: Mapped[int | None] = mapped_column(Integer)
    duplicate_file_count: Mapped[int | None] = mapped_column(Integer)
    pcm_quality_algorithm: Mapped[str | None] = mapped_column(String(32))
    pcm_validated_file_count: Mapped[int | None] = mapped_column(Integer)
    pcm_sample_count: Mapped[int | None] = mapped_column(BigInteger)
    pcm_clipping_ratio: Mapped[float | None] = mapped_column(Float)
    pcm_silence_ratio: Mapped[float | None] = mapped_column(Float)
    pcm_rms_ratio: Mapped[float | None] = mapped_column(Float)
    pcm_silence_threshold_dbfs: Mapped[float | None] = mapped_column(Float)
    pcm_loudness_algorithm: Mapped[str | None] = mapped_column(String(48))
    pcm_loudness_analyzed_file_count: Mapped[int | None] = mapped_column(Integer)
    pcm_loudness_block_count: Mapped[int | None] = mapped_column(BigInteger)
    pcm_loudness_gated_block_count: Mapped[int | None] = mapped_column(BigInteger)
    pcm_integrated_lufs: Mapped[float | None] = mapped_column(Float)
    pcm_loudness_unavailable_reason: Mapped[str | None] = mapped_column(String(48))
    is_usable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="legacy_imported", nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    original_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    original_sha256: Mapped[str | None] = mapped_column(String(64))
    original_mime_type: Mapped[str | None] = mapped_column(String(255))
    prepared_flat_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    prepared_flat_sha256: Mapped[str | None] = mapped_column(String(64))
    manifest_storage_uri: Mapped[str | None] = mapped_column(String(2048))
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    quality_report_storage_uri: Mapped[str | None] = mapped_column(String(2048))
    quality_report_sha256: Mapped[str | None] = mapped_column(String(64))
    decoder_pending_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    retryable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ingestion_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class DatasetUploadSession(Base, TimestampMixin):
    __tablename__ = "dataset_upload_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'finalizing', 'completed', 'failed', 'expired')",
            name="status_allowed",
        ),
        UniqueConstraint(
            "owner_id",
            "idempotency_key",
            "generation",
            name="uq_dataset_upload_owner_idempotency_generation",
        ),
        CheckConstraint(
            "length(storage_namespace_sha256) = 64",
            name="storage_namespace_sha256_length",
        ),
        CheckConstraint(
            "cleanup_claim_generation IS NULL OR cleanup_claim_generation > 0",
            name="cleanup_claim_generation_positive",
        ),
        Index("ix_dataset_upload_status_expiry", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    expected_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    temporary_object_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    original_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    prepared_flat_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    manifest_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    quality_report_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_namespace_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    upload_write_token: Mapped[str | None] = mapped_column(String(36))
    upload_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalization_token: Mapped[str | None] = mapped_column(String(36))
    finalization_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    upload_token_hash: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    cleanup_claim_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("maintenance_task_runs.id", ondelete="SET NULL"), index=True
    )
    cleanup_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_claim_generation: Mapped[int | None] = mapped_column(Integer)
    cleanup_first_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TestSet(Base, TimestampMixin):
    __tablename__ = "test_sets"
    __table_args__ = (
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("item_count >= 0", name="item_count_non_negative"),
        CheckConstraint("status IN ('draft', 'ready', 'failed')", name="status_allowed"),
        UniqueConstraint("family_id", "revision", name="uq_test_set_family_revision"),
        UniqueConstraint("created_by", "name", "revision", name="uq_test_set_owner_name_revision"),
        Index("ix_test_set_owner_name", "created_by", "name", "revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    family_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False, index=True)
    manifest_storage_uri: Mapped[str | None] = mapped_column(String(2048))
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TestSetItemUploadSession(Base, TimestampMixin):
    __tablename__ = "test_set_item_upload_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'finalizing', 'completed', 'failed', 'expired')",
            name="status_allowed",
        ),
        CheckConstraint("generation > 0", name="generation_positive"),
        CheckConstraint("sort_order >= 0", name="sort_order_non_negative"),
        CheckConstraint("expected_size_bytes > 0", name="expected_size_positive"),
        CheckConstraint(
            "length(storage_namespace_sha256) = 64",
            name="storage_namespace_sha256_length",
        ),
        CheckConstraint(
            "cleanup_claim_generation IS NULL OR cleanup_claim_generation > 0",
            name="cleanup_claim_generation_positive",
        ),
        UniqueConstraint(
            "test_set_id",
            "owner_id",
            "idempotency_key",
            "generation",
            name="uq_test_set_item_upload_idempotency_generation",
        ),
        Index("ix_test_set_item_upload_status_expiry", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    test_set_id: Mapped[str] = mapped_column(
        ForeignKey("test_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    item_key: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    expected_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    license_reference: Mapped[str] = mapped_column(String(320), nullable=False)
    provenance_reference: Mapped[str] = mapped_column(String(320), nullable=False)
    temporary_object_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    canonical_object_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_namespace_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    upload_write_token: Mapped[str | None] = mapped_column(String(36))
    upload_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalization_token: Mapped[str | None] = mapped_column(String(36))
    finalization_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    upload_token_hash: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    cleanup_claim_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("maintenance_task_runs.id", ondelete="SET NULL"), index=True
    )
    cleanup_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_claim_generation: Mapped[int | None] = mapped_column(Integer)
    cleanup_first_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TestSetItem(Base):
    __tablename__ = "test_set_items"
    __table_args__ = (
        CheckConstraint("sort_order >= 0", name="sort_order_non_negative"),
        CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
        CheckConstraint("sample_rate_hz > 0", name="sample_rate_positive"),
        CheckConstraint("channels > 0", name="channels_positive"),
        CheckConstraint("duration_seconds > 0", name="duration_positive"),
        UniqueConstraint("test_set_id", "item_key", name="uq_test_set_item_key"),
        UniqueConstraint("test_set_id", "sort_order", name="uq_test_set_item_order"),
        UniqueConstraint("id", "test_set_id", name="uq_test_set_item_id_test_set"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    test_set_id: Mapped[str] = mapped_column(
        ForeignKey("test_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_key: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    sample_rate_hz: Mapped[int] = mapped_column(Integer, nullable=False)
    channels: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    license_reference: Mapped[str] = mapped_column(String(320), nullable=False)
    provenance_reference: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Preset(Base, TimestampMixin):
    __tablename__ = "presets"
    __table_args__ = (
        CheckConstraint("revision > 0", name="revision_positive"),
        UniqueConstraint("family_id", "revision", name="uq_preset_family_revision"),
        UniqueConstraint("created_by", "name", "revision", name="uq_preset_owner_name_revision"),
        Index("ix_preset_owner_name", "created_by", "name", "revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    family_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )


class MaintenanceTaskRun(Base, TimestampMixin):
    """PostgreSQL ledger entry for one allowlisted central maintenance task."""

    __tablename__ = "maintenance_task_runs"
    __table_args__ = (
        CheckConstraint(
            "task_name IN ('dataset_staging_cleanup', 'test_set_staging_cleanup')",
            name="task_name_allowed",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'retrying', 'completed', 'failed', 'enqueue_failed')",
            name="status_allowed",
        ),
        Index("ix_maintenance_task_run_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_name: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Experiment(Base, TimestampMixin):
    __tablename__ = "experiments"
    __table_args__ = (
        CheckConstraint(
            "name_conflict_key IS NULL OR name_conflict_key = name",
            name="name_conflict_key_matches_name",
        ),
        UniqueConstraint(
            "created_by",
            "name_conflict_key",
            name="uq_experiments_owner_name_conflict_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __mapper_args__ = {"version_id_col": row_version}
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # NULL is reserved for pre-migration duplicate/null-owner history. New API
    # rows always bind this key to the normalized display name.
    name_conflict_key: Mapped[str | None] = mapped_column(String(128))
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("experiment_id", "job_name", name="uq_jobs_experiment_job_name"),
        Index("uq_job_id_experiment", "id", "experiment_id", unique=True),
        UniqueConstraint("id", "test_set_id", name="uq_job_test_set_snapshot"),
        Index("ix_jobs_claim_order", "status", "priority", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __mapper_args__ = {"version_id_col": row_version}
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    worker_id: Mapped[str | None] = mapped_column(
        ForeignKey("workers.id", ondelete="SET NULL"), index=True
    )
    job_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=JobStatus.QUEUED.value, nullable=False, index=True
    )
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    test_set_id: Mapped[str | None] = mapped_column(
        ForeignKey("test_sets.id", ondelete="RESTRICT"), index=True
    )
    preset_id: Mapped[str | None] = mapped_column(
        ForeignKey("presets.id", ondelete="RESTRICT"), index=True
    )
    sample_plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    sample_plan_sha256: Mapped[str | None] = mapped_column(String(64))
    priority: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    current_epoch: Mapped[int | None] = mapped_column(Integer)
    total_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_job_attempt_number"),
        UniqueConstraint("id", "job_id", name="uq_job_attempt_id_job"),
        CheckConstraint(
            "(telemetry_log_count IS NULL AND telemetry_metric_count IS NULL) OR "
            "(telemetry_log_count IS NOT NULL AND telemetry_metric_count IS NOT NULL)",
            name="telemetry_counts_all_null_or_present",
        ),
        CheckConstraint(
            "telemetry_log_count IS NULL OR "
            "(telemetry_log_count >= 0 AND telemetry_log_count <= 2147483647)",
            name="telemetry_log_count_range",
        ),
        CheckConstraint(
            "telemetry_metric_count IS NULL OR "
            "(telemetry_metric_count >= 0 AND telemetry_metric_count <= 2147483647)",
            name="telemetry_metric_count_range",
        ),
        CheckConstraint(
            "telemetry_log_count IS NULL OR status IN ('completed', 'failed', 'cancelled')",
            name="telemetry_counts_terminal_only",
        ),
        CheckConstraint(
            "rvc_commit_hash IS NULL OR "
            "(length(rvc_commit_hash) >= 7 AND length(rvc_commit_hash) <= 64)",
            name="rvc_commit_hash_length",
        ),
        CheckConstraint(
            "execution_provenance_version IS NULL OR "
            "execution_provenance_version = 'worker-claim-v1'",
            name="execution_provenance_version_allowed",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    worker_id: Mapped[str] = mapped_column(
        ForeignKey("workers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    engine_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    rvc_commit_hash: Mapped[str | None] = mapped_column(String(64))
    execution_provenance_version: Mapped[str | None] = mapped_column(String(32))
    runtime_image_digest: Mapped[str | None] = mapped_column(String(71))
    runtime_asset_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    telemetry_log_count: Mapped[int | None] = mapped_column(Integer)
    telemetry_metric_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobLease(Base):
    __tablename__ = "job_leases"
    __table_args__ = (Index("ix_job_leases_active_expiry", "active", "expires_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("job_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    worker_id: Mapped[str] = mapped_column(
        ForeignKey("workers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_renewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class JobStatusEvent(Base):
    __tablename__ = "job_status_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_attempts.id", ondelete="SET NULL"), index=True
    )
    previous_status: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)


class IngestBatch(Base):
    __tablename__ = "ingest_batches"
    __table_args__ = (
        UniqueConstraint("attempt_id", "batch_type", "idempotency_key", name="uq_ingest_batch_key"),
        CheckConstraint(
            "payload_fingerprint IS NULL OR length(payload_fingerprint) = 64",
            name="payload_fingerprint_length",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("job_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    batch_type: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # Historical rows predate payload-bound idempotency.  New log/metric
    # batches always set this field; a NULL row is intentionally not treated
    # as a verifiable replay.
    payload_fingerprint: Mapped[str | None] = mapped_column(String(64))
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class JobLog(Base):
    __tablename__ = "job_logs"
    __table_args__ = (
        UniqueConstraint("attempt_id", "sequence", name="uq_job_log_attempt_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("job_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    fields_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Metric(Base):
    __tablename__ = "metrics"
    __table_args__ = (
        UniqueConstraint("attempt_id", "sequence", name="uq_metric_attempt_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("job_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    epoch: Mapped[int | None] = mapped_column(Integer)
    step: Mapped[int | None] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id", "artifact_type", "sha256", name="uq_artifact_attempt_type_sha"
        ),
        UniqueConstraint("id", "job_id", "attempt_id", name="uq_artifact_id_job_attempt"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("job_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ArtifactUploadSession(Base, TimestampMixin):
    __tablename__ = "artifact_upload_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'finalizing', 'completed', 'failed', 'expired')",
            name="status_allowed",
        ),
        UniqueConstraint(
            "attempt_id",
            "idempotency_key",
            "generation",
            name="uq_artifact_upload_attempt_idempotency_generation",
        ),
        UniqueConstraint("dedupe_key", name="uq_artifact_upload_dedupe_key"),
        UniqueConstraint("artifact_id", name="uq_artifact_upload_artifact_id"),
        CheckConstraint(
            "length(storage_namespace_sha256) = 64",
            name="storage_namespace_sha256_length",
        ),
        Index("ix_artifact_upload_status_expiry", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("job_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lease_id: Mapped[str] = mapped_column(
        ForeignKey("job_leases.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    worker_id: Mapped[str] = mapped_column(
        ForeignKey("workers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id", ondelete="SET NULL"))
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    expected_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(255))
    temporary_object_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    canonical_object_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_namespace_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    upload_token_hash: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))


class ExperimentModelRegistry(Base, TimestampMixin):
    """Versioned mutation fence for one Experiment's reviewed model entries."""

    __tablename__ = "experiment_model_registries"
    __table_args__ = (
        CheckConstraint("row_version >= 1", name="row_version_positive"),
    )

    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="RESTRICT"), primary_key=True
    )
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __mapper_args__ = {"version_id_col": row_version}


class ModelRegistryEntry(Base, TimestampMixin):
    """Immutable artifact/provenance snapshot with a versioned review state."""

    __tablename__ = "model_registry_entries"
    __table_args__ = (
        CheckConstraint("row_version >= 1", name="row_version_positive"),
        CheckConstraint(
            "status IN ('candidate', 'approved', 'revoked')",
            name="status_allowed",
        ),
        CheckConstraint(
            "active_slot IS NULL OR active_slot = 1",
            name="active_slot_allowed",
        ),
        CheckConstraint(
            "((status = 'candidate' AND active_slot IS NULL "
            "AND approved_by IS NULL AND revoked_by IS NULL "
            "AND approved_at IS NULL AND revoked_at IS NULL AND revoke_reason IS NULL) "
            "OR (status = 'approved' AND approved_at IS NOT NULL "
            "AND approved_by IS NOT NULL AND revoked_by IS NULL "
            "AND revoked_at IS NULL AND revoke_reason IS NULL) "
            "OR (status = 'revoked' AND active_slot IS NULL "
            "AND revoked_by IS NOT NULL AND revoked_at IS NOT NULL "
            "AND revoke_reason IS NOT NULL))",
            name="status_timestamps_consistent",
        ),
        CheckConstraint(
            "active_slot IS NULL OR status = 'approved'",
            name="active_slot_requires_approved",
        ),
        CheckConstraint(
            "(approved_by IS NULL AND approved_at IS NULL) OR "
            "(approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="approval_actor_timestamp_together",
        ),
        CheckConstraint(
            "revoke_reason IS NULL OR revoke_reason IN "
            "('quality_rejected', 'security_issue', 'operator_request')",
            name="revoke_reason_allowed",
        ),
        CheckConstraint("source_attempt_number >= 1", name="source_attempt_number_positive"),
        CheckConstraint("model_size_bytes > 0", name="model_size_positive"),
        CheckConstraint(
            "length(model_sha256) = 64 AND model_sha256 = lower(model_sha256) "
            "AND length(job_config_sha256) = 64 "
            "AND job_config_sha256 = lower(job_config_sha256)",
            name="required_sha256_lengths",
        ),
        CheckConstraint(
            "engine_mode = 'rvc_webui'",
            name="engine_mode_rvc_webui",
        ),
        CheckConstraint(
            "execution_provenance_version = 'worker-claim-v1'",
            name="execution_provenance_version_worker_claim_v1",
        ),
        CheckConstraint(
            "length(rvc_commit_hash) = 40 AND rvc_commit_hash = lower(rvc_commit_hash)",
            name="rvc_commit_hash_reviewed_format",
        ),
        CheckConstraint(
            "length(runtime_image_digest) = 71 "
            "AND substr(runtime_image_digest, 1, 7) = 'sha256:' "
            "AND runtime_image_digest = lower(runtime_image_digest) "
            "AND length(runtime_asset_manifest_sha256) = 64 "
            "AND runtime_asset_manifest_sha256 = lower(runtime_asset_manifest_sha256)",
            name="runtime_provenance_format",
        ),
        CheckConstraint(
            "(index_artifact_id IS NULL AND index_filename IS NULL "
            "AND index_size_bytes IS NULL AND index_sha256 IS NULL) OR "
            "(index_artifact_id IS NOT NULL AND index_filename IS NOT NULL "
            "AND index_size_bytes > 0 AND length(index_sha256) = 64 "
            "AND index_sha256 = lower(index_sha256))",
            name="index_snapshot_together",
        ),
        UniqueConstraint(
            "model_artifact_id",
            name="uq_model_registry_entry_model_artifact",
        ),
        UniqueConstraint(
            "id",
            "experiment_id",
            name="uq_model_registry_entry_id_experiment",
        ),
        UniqueConstraint(
            "experiment_id",
            "active_slot",
            name="uq_model_registry_entry_active_slot",
        ),
        ForeignKeyConstraint(
            ["source_job_id", "experiment_id"],
            ["jobs.id", "jobs.experiment_id"],
            name="fk_model_registry_entry_job_experiment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_attempt_id", "source_job_id"],
            ["job_attempts.id", "job_attempts.job_id"],
            name="fk_model_registry_entry_attempt_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["model_artifact_id", "source_job_id", "source_attempt_id"],
            ["artifacts.id", "artifacts.job_id", "artifacts.attempt_id"],
            name="fk_model_registry_entry_model_artifact",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["index_artifact_id", "source_job_id", "source_attempt_id"],
            ["artifacts.id", "artifacts.job_id", "artifacts.attempt_id"],
            name="fk_model_registry_entry_index_artifact",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_model_registry_entry_experiment_created",
            "experiment_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiment_model_registries.experiment_id", ondelete="RESTRICT"),
        nullable=False,
    )
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __mapper_args__ = {"version_id_col": row_version}
    status: Mapped[str] = mapped_column(String(16), default="candidate", nullable=False)
    active_slot: Mapped[int | None] = mapped_column(Integer)
    source_job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source_attempt_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source_job_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    engine_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    job_config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    rvc_commit_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_provenance_version: Mapped[str] = mapped_column(String(32), nullable=False)
    runtime_image_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    runtime_asset_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_artifact_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    model_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    model_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    model_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    index_artifact_id: Mapped[str | None] = mapped_column(String(36), index=True)
    index_filename: Mapped[str | None] = mapped_column(String(255))
    index_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    index_sha256: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approved_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    revoked_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(String(32))


class ModelRegistryOperation(Base):
    """Actor-scoped, secret-free idempotency ledger for registry mutations."""

    __tablename__ = "model_registry_operations"
    __table_args__ = (
        CheckConstraint(
            "operation_type IN ('candidate', 'promote', 'revoke')",
            name="operation_type_allowed",
        ),
        UniqueConstraint(
            "actor_id",
            "idempotency_key_hash",
            name="uq_model_registry_operation_actor_key",
        ),
        CheckConstraint(
            "length(idempotency_key_hash) = 64 "
            "AND idempotency_key_hash = lower(idempotency_key_hash)",
            name="idempotency_key_hash_format",
        ),
        CheckConstraint(
            "length(request_fingerprint) = 64 "
            "AND request_fingerprint = lower(request_fingerprint)",
            name="request_fingerprint_format",
        ),
        ForeignKeyConstraint(
            ["entry_id", "experiment_id"],
            ["model_registry_entries.id", "model_registry_entries.experiment_id"],
            name="fk_model_registry_operation_entry_experiment",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiment_model_registries.experiment_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    entry_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Sample(Base):
    __tablename__ = "samples"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "test_set_item_id",
            "inference_config_sha256",
            name="uq_sample_attempt_item_config",
        ),
        CheckConstraint("output_size_bytes > 0", name="output_size_positive"),
        CheckConstraint("output_sample_rate_hz > 0", name="output_sample_rate_positive"),
        CheckConstraint("output_channels > 0", name="output_channels_positive"),
        CheckConstraint("output_duration_seconds > 0", name="output_duration_positive"),
        CheckConstraint(
            "inference_f0_method IN ('pm', 'harvest', 'crepe', 'rmvpe')",
            name="inference_f0_method_allowed",
        ),
        ForeignKeyConstraint(
            ["attempt_id", "job_id"],
            ["job_attempts.id", "job_attempts.job_id"],
            name="fk_sample_attempt_job",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["job_id", "test_set_id"],
            ["jobs.id", "jobs.test_set_id"],
            name="fk_sample_job_test_set",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["test_set_item_id", "test_set_id"],
            ["test_set_items.id", "test_set_items.test_set_id"],
            name="fk_sample_item_test_set",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["artifact_id", "job_id", "attempt_id"],
            ["artifacts.id", "artifacts.job_id", "artifacts.attempt_id"],
            name="fk_sample_artifact_job_attempt",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    test_set_id: Mapped[str] = mapped_column(
        ForeignKey("test_sets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    test_set_item_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    index_sha256: Mapped[str | None] = mapped_column(String(64))
    inference_f0_method: Mapped[str] = mapped_column(String(16), nullable=False)
    inference_config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    native_inference_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    native_inference_request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    output_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    output_sample_rate_hz: Mapped[int] = mapped_column(Integer, nullable=False)
    output_channels: Mapped[int] = mapped_column(Integer, nullable=False)
    output_duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    rvc_commit_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_image_digest: Mapped[str] = mapped_column(String(255), nullable=False)
    runtime_asset_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class MlflowSyncEvent(Base, TimestampMixin):
    """Durable, idempotent projection event for the optional MLflow read model."""

    __tablename__ = "mlflow_sync_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'synced')",
            name="status_allowed",
        ),
        Index("ix_mlflow_sync_ready", "status", "next_attempt_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(36), index=True)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
