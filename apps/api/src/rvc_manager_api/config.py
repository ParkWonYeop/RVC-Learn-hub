from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "RVC Training Orchestrator Manager"
    environment: Literal["development", "test", "production"] = "development"
    public_scheme: Literal["http", "https"] = "http"
    process_role: Literal["api", "maintenance"] = "api"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite+aiosqlite:///./rvc_manager.db"
    redis_url: str | None = None
    readiness_check_redis: bool = False
    rq_enabled: bool = False
    rq_queue_name: str = Field(default="rvc-maintenance", pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    rq_readiness_timeout_seconds: float = Field(default=2.0, ge=0.1, le=5.0)
    rq_worker_heartbeat_max_age_seconds: int = Field(default=75, ge=15, le=600)
    rq_worker_ttl_seconds: int = Field(default=45, ge=30, le=600)
    maintenance_cleanup_grace_seconds: int = Field(default=604_800, ge=0, le=31_536_000)
    maintenance_cleanup_batch_size: int = Field(default=250, ge=1, le=1_000)
    maintenance_task_timeout_seconds: int = Field(default=300, ge=30, le=3_600)
    maintenance_task_heartbeat_seconds: int = Field(default=15, ge=1, le=300)
    maintenance_task_max_attempts: int = Field(default=3, ge=1, le=10)
    maintenance_retry_backoff_seconds: int = Field(default=30, ge=1, le=3_600)
    maintenance_retry_backoff_max_seconds: int = Field(default=300, ge=1, le=3_600)
    maintenance_cleanup_claim_stale_seconds: int = Field(default=900, ge=60, le=86_400)
    maintenance_reconcile_enabled: bool = True
    maintenance_reconcile_interval_seconds: float = Field(default=15.0, ge=0.25, le=300.0)
    maintenance_reconcile_stale_seconds: int = Field(default=120, ge=5, le=3_600)
    maintenance_reconcile_batch_size: int = Field(default=100, ge=1, le=1_000)
    auto_create_schema: bool = False

    jwt_secret: SecretStr = SecretStr("development-only-jwt-secret-change-me")
    jwt_secret_file: Path | None = None
    jwt_issuer: str = "rvc-training-orchestrator"
    jwt_audience: str = "rvc-manager-api"
    jwt_access_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    jwt_leeway_seconds: int = Field(default=5, ge=0, le=60)

    worker_bootstrap_token: SecretStr | None = None
    worker_token_pepper: SecretStr = SecretStr("development-only-worker-token-pepper-change-me")
    worker_token_rotation_ttl_seconds: int = Field(default=600, ge=60, le=3_600)
    lease_seconds: int = Field(default=120, ge=15, le=3600)
    worker_offline_seconds: int = Field(default=180, ge=30, le=7200)
    lease_recovery_max_attempts: int = Field(default=3, ge=0, le=20)
    allow_fake_workers: bool = False

    storage_backend: Literal["auto", "local", "s3"] = "auto"
    local_storage_root: Path = Path("./var/object-storage")
    public_api_base_url: str | None = None
    artifact_upload_ttl_seconds: int = Field(default=3600, ge=300, le=7200)
    artifact_download_ttl_seconds: int = Field(default=60, ge=15, le=300)
    artifact_max_bytes: int = Field(default=5 * 1024**3, ge=1, le=5 * 1024**3)
    artifact_stream_chunk_bytes: int = Field(default=1024**2, ge=64 * 1024, le=8 * 1024**2)
    artifact_verification_spool_dir: Path | None = None
    artifact_upload_write_heartbeat_seconds: int = Field(default=15, ge=1, le=300)
    artifact_staging_cleanup_grace_seconds: int = Field(default=7_200, ge=300, le=86_400)
    artifact_cleanup_confirmation_grace_seconds: int = Field(default=60, ge=30, le=3_600)
    artifact_cleanup_claim_stale_seconds: int = Field(default=300, ge=60, le=7_200)
    artifact_cleanup_heartbeat_seconds: int = Field(default=15, ge=1, le=300)
    artifact_cleanup_reconcile_enabled: bool = True
    artifact_cleanup_reconcile_interval_seconds: float = Field(
        default=30.0,
        ge=0.25,
        le=300.0,
    )
    artifact_cleanup_reconcile_stale_seconds: int = Field(default=120, ge=5, le=3_600)
    artifact_cleanup_reconcile_batch_size: int = Field(default=32, ge=1, le=256)
    artifact_finalizing_stale_seconds: int = Field(default=7200, ge=60, le=86_400)
    artifact_retry_after_seconds: int = Field(default=5, ge=1, le=60)
    artifact_attempt_max_sessions: int = Field(default=256, ge=1, le=10_000)
    artifact_attempt_max_bytes: int = Field(default=100 * 1024**3, ge=1, le=500 * 1024**3)
    dataset_upload_ttl_seconds: int = Field(default=3600, ge=300, le=7200)
    dataset_download_ttl_seconds: int = Field(default=60, ge=15, le=300)
    dataset_upload_max_bytes: int = Field(default=5 * 1024**3, ge=1, le=5 * 1024**3)
    dataset_owner_max_sessions: int = Field(default=8, ge=1, le=256)
    dataset_owner_max_bytes: int = Field(default=20 * 1024**3, ge=1, le=500 * 1024**3)
    dataset_upload_write_stale_seconds: int = Field(default=300, ge=30, le=7_200)
    dataset_upload_write_heartbeat_seconds: int = Field(default=15, ge=1, le=300)
    dataset_finalizing_stale_seconds: int = Field(default=1800, ge=60, le=86_400)
    dataset_finalizing_heartbeat_seconds: int = Field(default=30, ge=1, le=300)
    dataset_cleanup_late_writer_grace_seconds: int = Field(
        default=7_200,
        ge=300,
        le=86_400,
    )
    dataset_cleanup_confirmation_grace_seconds: int = Field(default=60, ge=1, le=300)
    dataset_retry_after_seconds: int = Field(default=5, ge=1, le=60)
    dataset_ingestion_root: Path = Path("./var/dataset-ingestion")
    dataset_max_entries: int = Field(default=10_000, ge=1, le=100_000)
    dataset_max_file_uncompressed_bytes: int = Field(default=2 * 1024**3, ge=1)
    dataset_max_total_uncompressed_bytes: int = Field(default=20 * 1024**3, ge=1)
    dataset_max_compression_ratio: float = Field(default=200.0, ge=1.0, le=10_000.0)
    test_set_upload_ttl_seconds: int = Field(default=1800, ge=300, le=7200)
    test_set_upload_write_stale_seconds: int = Field(default=300, ge=30, le=7_200)
    test_set_upload_write_heartbeat_seconds: int = Field(default=15, ge=1, le=300)
    test_set_finalizing_stale_seconds: int = Field(default=1800, ge=60, le=86_400)
    test_set_finalizing_heartbeat_seconds: int = Field(default=30, ge=1, le=300)
    test_set_cleanup_late_writer_grace_seconds: int = Field(
        default=7_200,
        ge=300,
        le=86_400,
    )
    test_set_cleanup_confirmation_grace_seconds: int = Field(default=60, ge=1, le=300)
    test_set_item_max_bytes: int = Field(default=256 * 1024**2, ge=44, le=2 * 1024**3)
    test_set_owner_max_sessions: int = Field(default=64, ge=1, le=1_000)
    test_set_owner_max_bytes: int = Field(default=2 * 1024**3, ge=44, le=100 * 1024**3)
    test_set_max_items: int = Field(default=128, ge=1, le=128)
    test_set_max_total_bytes: int = Field(
        default=2 * 1024**3,
        ge=44,
        le=2 * 1024**3,
    )
    test_set_max_duration_seconds: float = Field(default=600.0, gt=0, le=86_400)
    test_set_max_total_duration_seconds: float = Field(
        default=3_600.0,
        gt=0,
        le=3_600.0,
    )
    test_set_min_sample_rate_hz: int = Field(default=8_000, ge=1, le=192_000)
    test_set_max_sample_rate_hz: int = Field(default=192_000, ge=8_000, le=384_000)
    test_set_max_channels: int = Field(default=2, ge=1, le=32)
    sample_max_bytes: int = Field(default=256 * 1024**2, ge=44, le=256 * 1024**2)
    sample_max_duration_seconds: float = Field(default=600.0, gt=0, le=600.0)
    sample_max_channels: int = Field(default=2, ge=1, le=2)
    sample_registration_json_max_bytes: int = Field(default=64 * 1024, ge=4 * 1024, le=64 * 1024)
    experiment_json_max_bytes: int = Field(default=16 * 1024, ge=8 * 1024, le=64 * 1024)
    user_lifecycle_json_max_bytes: int = Field(default=16 * 1024, ge=4 * 1024, le=64 * 1024)
    worker_telemetry_json_max_bytes: int = Field(
        default=2 * 1024**2,
        ge=64 * 1024,
        le=8 * 1024**2,
    )
    sample_verification_timeout_seconds: float = Field(default=120.0, ge=1.0, le=600.0)
    sample_verification_max_concurrency: int = Field(default=2, ge=1, le=16)
    sample_approved_runtime_bundles: str = ""
    auto_sample_jobs_enabled: bool = False
    log_stream_poll_interval_seconds: float = Field(default=1.0, ge=0.05, le=10.0)
    log_stream_heartbeat_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    log_stream_max_connection_seconds: float = Field(default=300.0, ge=5.0, le=600.0)
    log_stream_batch_limit: int = Field(default=100, ge=1, le=500)
    s3_endpoint_url: str | None = None
    s3_presign_endpoint_url: str | None = None
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_bucket: str = "rvc-orchestrator"
    s3_region: str = "us-east-1"
    s3_addressing_style: Literal["path", "virtual"] = "path"
    s3_verify_tls: bool = True
    s3_presign_bind_checksum: bool = False
    mlflow_enabled: bool = False
    mlflow_fail_closed: bool = False
    mlflow_tracking_uri: str | None = None
    mlflow_tracking_token: SecretStr | None = None
    mlflow_tracking_token_file: Path | None = None
    mlflow_request_timeout_seconds: float = Field(default=5.0, ge=0.5, le=30.0)
    mlflow_readiness_timeout_seconds: float = Field(default=1.0, ge=0.1, le=5.0)
    mlflow_sync_interval_seconds: float = Field(default=5.0, ge=0.25, le=300.0)
    mlflow_sync_batch_size: int = Field(default=20, ge=1, le=200)
    mlflow_processing_stale_seconds: int = Field(default=120, ge=15, le=3600)
    mlflow_retry_max_seconds: int = Field(default=300, ge=5, le=3600)
    cors_origins: str = ""
    rate_limit_enabled: bool = False
    rate_limit_fail_closed: bool = True
    rate_limit_default_requests_per_minute: int = Field(default=600, ge=1, le=100_000)
    rate_limit_login_requests_per_minute: int = Field(default=10, ge=1, le=10_000)
    rate_limit_register_requests_per_minute: int = Field(default=10, ge=1, le=10_000)
    rate_limit_worker_token_rotation_requests_per_minute: int = Field(
        default=6,
        ge=1,
        le=1_000,
    )
    rate_limit_upload_requests_per_minute: int = Field(default=120, ge=1, le=10_000)
    rate_limit_finalize_requests_per_minute: int = Field(default=30, ge=1, le=10_000)
    rate_limit_sample_requests_per_minute: int = Field(default=30, ge=1, le=1_000)
    rate_limit_sample_download_requests_per_minute: int = Field(
        default=60,
        ge=1,
        le=10_000,
    )

    @model_validator(mode="after")
    def production_secrets_are_explicit(self) -> Settings:
        if self.rate_limit_enabled and not self.redis_url:
            raise ValueError("REDIS_URL is required when RATE_LIMIT_ENABLED=true")
        if self.rq_enabled and not self.redis_url:
            raise ValueError("REDIS_URL is required when RQ_ENABLED=true")
        if self.maintenance_retry_backoff_max_seconds < self.maintenance_retry_backoff_seconds:
            raise ValueError(
                "MAINTENANCE_RETRY_BACKOFF_MAX_SECONDS must be greater than or equal to "
                "MAINTENANCE_RETRY_BACKOFF_SECONDS"
            )
        if self.maintenance_cleanup_claim_stale_seconds <= self.maintenance_task_timeout_seconds:
            raise ValueError(
                "MAINTENANCE_CLEANUP_CLAIM_STALE_SECONDS must exceed "
                "MAINTENANCE_TASK_TIMEOUT_SECONDS"
            )
        if (
            self.maintenance_task_heartbeat_seconds * 3
            >= self.maintenance_task_timeout_seconds
        ):
            raise ValueError(
                "MAINTENANCE_TASK_HEARTBEAT_SECONDS must be less than one third of "
                "MAINTENANCE_TASK_TIMEOUT_SECONDS"
            )
        if (
            self.maintenance_reconcile_stale_seconds
            <= self.maintenance_reconcile_interval_seconds * 2
        ):
            raise ValueError(
                "MAINTENANCE_RECONCILE_STALE_SECONDS must exceed twice "
                "MAINTENANCE_RECONCILE_INTERVAL_SECONDS"
            )
        if self.maintenance_cleanup_grace_seconds < self.dataset_upload_ttl_seconds:
            raise ValueError(
                "MAINTENANCE_CLEANUP_GRACE_SECONDS must be greater than or equal to "
                "DATASET_UPLOAD_TTL_SECONDS"
            )
        if self.process_role == "maintenance" and (
            self.jwt_secret_file is not None
            or self.jwt_secret.get_secret_value() != "development-only-jwt-secret-change-me"
            or self.worker_bootstrap_token is not None
            or self.worker_token_pepper.get_secret_value()
            != "development-only-worker-token-pepper-change-me"
            or self.mlflow_tracking_token is not None
            or self.mlflow_tracking_token_file is not None
        ):
            raise ValueError("maintenance process must not receive unrelated auth secrets")
        if self.process_role == "maintenance" and (
            not self.rq_enabled or self.rate_limit_enabled or self.mlflow_enabled
        ):
            raise ValueError(
                "maintenance process requires RQ_ENABLED=true and forbids rate limit/MLflow roles"
            )
        if self.mlflow_tracking_token_file is not None:
            try:
                mlflow_token = self.mlflow_tracking_token_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ValueError("MLFLOW_TRACKING_TOKEN_FILE is not readable") from exc
            if not mlflow_token:
                raise ValueError("MLFLOW_TRACKING_TOKEN_FILE is empty")
            self.mlflow_tracking_token = SecretStr(mlflow_token)
        if self.mlflow_enabled and not self.mlflow_tracking_uri:
            raise ValueError("MLFLOW_TRACKING_URI is required when MLFLOW_ENABLED=true")
        if self.mlflow_tracking_uri is not None:
            tracking_uri = self.mlflow_tracking_uri
            if tracking_uri != tracking_uri.strip() or any(
                character.isspace() for character in tracking_uri
            ):
                raise ValueError("MLFLOW_TRACKING_URI must not contain whitespace")
            try:
                parsed_mlflow = urlsplit(tracking_uri)
                _ = parsed_mlflow.port
            except ValueError as exc:
                raise ValueError("MLFLOW_TRACKING_URI must be a valid absolute URL") from exc
            if (
                parsed_mlflow.scheme not in {"http", "https"}
                or parsed_mlflow.hostname is None
                or parsed_mlflow.username is not None
                or parsed_mlflow.password is not None
                or parsed_mlflow.query
                or parsed_mlflow.fragment
                or "?" in tracking_uri
                or "#" in tracking_uri
            ):
                raise ValueError(
                    "MLFLOW_TRACKING_URI must be an absolute HTTP(S) URL without "
                    "credentials, query, or fragment"
                )
        if self.jwt_secret_file is not None:
            try:
                jwt_secret = self.jwt_secret_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ValueError("JWT_SECRET_FILE is not readable") from exc
            if not jwt_secret:
                raise ValueError("JWT_SECRET_FILE is empty")
            self.jwt_secret = SecretStr(jwt_secret)
        jwt_secret_value = self.jwt_secret.get_secret_value()
        if len(jwt_secret_value) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 characters")
        if self.dataset_max_file_uncompressed_bytes > self.dataset_max_total_uncompressed_bytes:
            raise ValueError(
                "DATASET_MAX_FILE_UNCOMPRESSED_BYTES must not exceed "
                "DATASET_MAX_TOTAL_UNCOMPRESSED_BYTES"
            )
        if self.artifact_upload_write_heartbeat_seconds * 3 >= self.artifact_upload_ttl_seconds:
            raise ValueError(
                "ARTIFACT_UPLOAD_WRITE_HEARTBEAT_SECONDS must be less than one third of "
                "ARTIFACT_UPLOAD_TTL_SECONDS"
            )
        if self.artifact_staging_cleanup_grace_seconds < self.artifact_upload_ttl_seconds:
            raise ValueError(
                "ARTIFACT_STAGING_CLEANUP_GRACE_SECONDS must be at least "
                "ARTIFACT_UPLOAD_TTL_SECONDS"
            )
        if (
            self.artifact_cleanup_heartbeat_seconds * 3
            >= self.artifact_cleanup_claim_stale_seconds
        ):
            raise ValueError(
                "ARTIFACT_CLEANUP_HEARTBEAT_SECONDS must be less than one third of "
                "ARTIFACT_CLEANUP_CLAIM_STALE_SECONDS"
            )
        if (
            self.artifact_cleanup_confirmation_grace_seconds
            < self.artifact_cleanup_reconcile_interval_seconds
        ):
            raise ValueError(
                "ARTIFACT_CLEANUP_CONFIRMATION_GRACE_SECONDS must be at least "
                "ARTIFACT_CLEANUP_RECONCILE_INTERVAL_SECONDS"
            )
        if (
            self.artifact_cleanup_reconcile_stale_seconds
            <= self.artifact_cleanup_reconcile_interval_seconds * 2
        ):
            raise ValueError(
                "ARTIFACT_CLEANUP_RECONCILE_STALE_SECONDS must be greater than twice "
                "ARTIFACT_CLEANUP_RECONCILE_INTERVAL_SECONDS"
            )
        if self.dataset_finalizing_heartbeat_seconds * 3 >= self.dataset_finalizing_stale_seconds:
            raise ValueError(
                "DATASET_FINALIZING_HEARTBEAT_SECONDS must be less than one third of "
                "DATASET_FINALIZING_STALE_SECONDS"
            )
        if (
            self.dataset_upload_write_heartbeat_seconds * 3
            >= self.dataset_upload_write_stale_seconds
        ):
            raise ValueError(
                "DATASET_UPLOAD_WRITE_HEARTBEAT_SECONDS must be less than one third of "
                "DATASET_UPLOAD_WRITE_STALE_SECONDS"
            )
        if self.dataset_cleanup_late_writer_grace_seconds < self.dataset_upload_ttl_seconds:
            raise ValueError(
                "DATASET_CLEANUP_LATE_WRITER_GRACE_SECONDS must be greater than or equal "
                "to DATASET_UPLOAD_TTL_SECONDS"
            )
        if (
            self.dataset_cleanup_confirmation_grace_seconds * 3
            >= self.maintenance_task_timeout_seconds
        ):
            raise ValueError(
                "DATASET_CLEANUP_CONFIRMATION_GRACE_SECONDS must be less than one third "
                "of MAINTENANCE_TASK_TIMEOUT_SECONDS"
            )
        if (
            self.test_set_upload_write_heartbeat_seconds * 3
            >= self.test_set_upload_write_stale_seconds
        ):
            raise ValueError(
                "TEST_SET_UPLOAD_WRITE_HEARTBEAT_SECONDS must be less than one third of "
                "TEST_SET_UPLOAD_WRITE_STALE_SECONDS"
            )
        if self.test_set_finalizing_heartbeat_seconds * 3 >= self.test_set_finalizing_stale_seconds:
            raise ValueError(
                "TEST_SET_FINALIZING_HEARTBEAT_SECONDS must be less than one third of "
                "TEST_SET_FINALIZING_STALE_SECONDS"
            )
        if self.test_set_cleanup_late_writer_grace_seconds < self.test_set_upload_ttl_seconds:
            raise ValueError(
                "TEST_SET_CLEANUP_LATE_WRITER_GRACE_SECONDS must be greater than or equal "
                "to TEST_SET_UPLOAD_TTL_SECONDS"
            )
        if (
            self.test_set_cleanup_confirmation_grace_seconds * 3
            >= self.maintenance_task_timeout_seconds
        ):
            raise ValueError(
                "TEST_SET_CLEANUP_CONFIRMATION_GRACE_SECONDS must be less than one third "
                "of MAINTENANCE_TASK_TIMEOUT_SECONDS"
            )
        if self.test_set_item_max_bytes > self.test_set_owner_max_bytes:
            raise ValueError("TEST_SET_ITEM_MAX_BYTES must not exceed TEST_SET_OWNER_MAX_BYTES")
        if self.test_set_min_sample_rate_hz > self.test_set_max_sample_rate_hz:
            raise ValueError(
                "TEST_SET_MIN_SAMPLE_RATE_HZ must not exceed TEST_SET_MAX_SAMPLE_RATE_HZ"
            )
        if self.sample_max_bytes > self.test_set_item_max_bytes:
            raise ValueError("SAMPLE_MAX_BYTES must not exceed TEST_SET_ITEM_MAX_BYTES")
        if self.sample_max_duration_seconds > self.test_set_max_duration_seconds:
            raise ValueError(
                "SAMPLE_MAX_DURATION_SECONDS must not exceed TEST_SET_MAX_DURATION_SECONDS"
            )
        if self.sample_max_channels > self.test_set_max_channels:
            raise ValueError("SAMPLE_MAX_CHANNELS must not exceed TEST_SET_MAX_CHANNELS")
        if self.artifact_attempt_max_sessions < self.test_set_max_items + 8:
            raise ValueError(
                "ARTIFACT_ATTEMPT_MAX_SESSIONS must reserve at least eight non-Sample "
                "sessions above TEST_SET_MAX_ITEMS"
            )
        for bundle in self.approved_sample_runtime_bundles:
            image_digest, asset_manifest_sha256 = bundle
            if (
                len(image_digest) != 71
                or not image_digest.startswith("sha256:")
                or any(character not in "0123456789abcdef" for character in image_digest[7:])
                or len(asset_manifest_sha256) != 64
                or any(character not in "0123456789abcdef" for character in asset_manifest_sha256)
            ):
                raise ValueError(
                    "SAMPLE_APPROVED_RUNTIME_BUNDLES must contain lowercase "
                    "sha256:<64hex>@<64hex> entries"
                )
        if self.auto_sample_jobs_enabled and not self.approved_sample_runtime_bundles:
            raise ValueError("AUTO_SAMPLE_JOBS_ENABLED requires SAMPLE_APPROVED_RUNTIME_BUNDLES")
        if self.s3_presign_endpoint_url is not None:
            endpoint = self.s3_presign_endpoint_url
            if endpoint != endpoint.strip() or any(character.isspace() for character in endpoint):
                raise ValueError("S3_PRESIGN_ENDPOINT_URL must not contain whitespace")
            try:
                parsed_endpoint = urlsplit(endpoint)
                _ = parsed_endpoint.port
            except ValueError as exc:
                raise ValueError("S3_PRESIGN_ENDPOINT_URL must be a valid absolute URL") from exc
            if (
                parsed_endpoint.scheme not in {"http", "https"}
                or parsed_endpoint.hostname is None
                or parsed_endpoint.username is not None
                or parsed_endpoint.password is not None
                or parsed_endpoint.query
                or parsed_endpoint.fragment
                or "?" in endpoint
                or "#" in endpoint
            ):
                raise ValueError(
                    "S3_PRESIGN_ENDPOINT_URL must be an absolute HTTP(S) URL without "
                    "credentials, query, or fragment"
                )
            if self.environment == "production" and parsed_endpoint.scheme != "https":
                raise ValueError("production S3_PRESIGN_ENDPOINT_URL must use HTTPS")
        if self.environment == "production":
            if self.allow_fake_workers:
                raise ValueError("ALLOW_FAKE_WORKERS cannot be enabled in production")
            if self.process_role == "api":
                if not self.artifact_cleanup_reconcile_enabled:
                    raise ValueError(
                        "ARTIFACT_CLEANUP_RECONCILE_ENABLED cannot be disabled "
                        "for the production API"
                    )
                if jwt_secret_value == "development-only-jwt-secret-change-me":
                    raise ValueError("JWT_SECRET must be overridden in production")
                if self.worker_bootstrap_token is None:
                    raise ValueError("WORKER_BOOTSTRAP_TOKEN is required in production")
                if (
                    self.worker_token_pepper.get_secret_value()
                    == "development-only-worker-token-pepper-change-me"
                ):
                    raise ValueError("WORKER_TOKEN_PEPPER must be overridden in production")
                if jwt_secret_value in {
                    self.worker_token_pepper.get_secret_value(),
                    self.worker_bootstrap_token.get_secret_value(),
                }:
                    raise ValueError("JWT_SECRET must be distinct from Worker credentials")
            if not self.database_url.startswith("postgresql+"):
                raise ValueError("production DATABASE_URL must use PostgreSQL")
            if self.resolved_storage_backend != "s3":
                raise ValueError("production object storage must use S3/MinIO")
            if self.s3_access_key_id is None or self.s3_secret_access_key is None:
                raise ValueError("S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY are required")
            if (
                self.process_role == "api"
                and self.s3_endpoint_url
                and not self.s3_presign_endpoint_url
            ):
                raise ValueError(
                    "S3_PRESIGN_ENDPOINT_URL is required with a custom production endpoint"
                )
        return self

    @cached_property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def approved_sample_runtime_bundles(self) -> frozenset[tuple[str, str]]:
        entries: set[tuple[str, str]] = set()
        for raw_entry in self.sample_approved_runtime_bundles.split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            if entry.count("@") != 1:
                # The model validator turns this sentinel into a stable startup error.
                entries.add((entry, ""))
                continue
            image_digest, asset_manifest_sha256 = entry.split("@", 1)
            entries.add((image_digest, asset_manifest_sha256))
        return frozenset(entries)

    @property
    def resolved_storage_backend(self) -> Literal["local", "s3"]:
        if self.storage_backend != "auto":
            return self.storage_backend
        if (
            self.s3_endpoint_url
            and self.s3_access_key_id is not None
            and self.s3_secret_access_key is not None
        ):
            return "s3"
        return "local"
