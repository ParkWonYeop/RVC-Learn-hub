"""Worker configuration loaded from YAML and environment variables."""

from __future__ import annotations

import math
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .tls import (
    DEFAULT_CUSTOM_CA_BUNDLE_PATH,
    CustomCABundleError,
    read_custom_ca_bundle,
)


class SettingsError(ValueError):
    """Raised when worker configuration is incomplete or unsafe."""


_ENV_TO_FIELD = {
    "MANAGER_URL": "manager_url",
    "WORKER_NAME": "worker_name",
    "WORKER_TOKEN": "worker_token",
    "WORKER_TOKEN_FILE": "worker_token_file",
    "WORKER_CREDENTIAL_PATH": "credential_path",
    "WORKER_CA_BUNDLE_PATH": "ca_bundle_path",
    "DATA_ROOT": "data_root",
    "RVC_RUNNER_MODE": "runner_mode",
    "RVC_PROFILE_PATH": "rvc_profile_path",
    "RVC_NATIVE_SOURCE_ROOT": "rvc_native_source_root",
    "RVC_NATIVE_PYTHON_EXECUTABLE": "rvc_native_python_executable",
    "RVC_NATIVE_CPU_WORKERS": "rvc_native_cpu_workers",
    "RVC_NATIVE_DEVICE": "rvc_native_device",
    "RVC_NATIVE_USE_HALF": "rvc_native_use_half",
    "RVC_NATIVE_PREPROCESS_TIMEOUT_SECONDS": "rvc_native_preprocess_timeout_seconds",
    "RVC_NATIVE_EXTRACTION_TIMEOUT_SECONDS": "rvc_native_extraction_timeout_seconds",
    "RVC_NATIVE_TRAINING_TIMEOUT_SECONDS": "rvc_native_training_timeout_seconds",
    "RVC_NATIVE_INDEX_TIMEOUT_SECONDS": "rvc_native_index_timeout_seconds",
    "RVC_NATIVE_SMALL_MODEL_TIMEOUT_SECONDS": "rvc_native_small_model_timeout_seconds",
    "HEARTBEAT_INTERVAL_SECONDS": "heartbeat_interval_seconds",
    "SYSTEM_TELEMETRY_INTERVAL_SECONDS": "system_telemetry_interval_seconds",
    "POLL_INTERVAL_SECONDS": "poll_interval_seconds",
    "LEASE_RENEW_INTERVAL_SECONDS": "lease_renew_interval_seconds",
    "REQUEST_TIMEOUT_SECONDS": "request_timeout_seconds",
    "ARTIFACT_UPLOAD_TIMEOUT_SECONDS": "artifact_upload_timeout_seconds",
    "ARTIFACT_UPLOAD_MAX_ATTEMPTS": "artifact_upload_max_attempts",
    "ARTIFACT_MAX_OBJECT_BYTES": "artifact_max_object_bytes",
    "ARTIFACT_MAX_FILES_PER_ATTEMPT": "artifact_max_files_per_attempt",
    "ARTIFACT_MAX_TOTAL_BYTES_PER_ATTEMPT": "artifact_max_total_bytes_per_attempt",
    "ARTIFACT_CHECKPOINT_RETENTION": "artifact_checkpoint_retention",
    "DATASET_DOWNLOAD_TIMEOUT_SECONDS": "dataset_download_timeout_seconds",
    "DATASET_DOWNLOAD_MAX_ATTEMPTS": "dataset_download_max_attempts",
    "DATASET_MAX_ARCHIVE_BYTES": "dataset_max_archive_bytes",
    "DATASET_MAX_ENTRIES": "dataset_max_entries",
    "DATASET_MAX_FILE_BYTES": "dataset_max_file_bytes",
    "DATASET_MAX_TOTAL_BYTES": "dataset_max_total_bytes",
    "DATASET_MAX_COMPRESSION_RATIO": "dataset_max_compression_ratio",
    "TEST_SET_DOWNLOAD_TIMEOUT_SECONDS": "test_set_download_timeout_seconds",
    "TEST_SET_MATERIALIZATION_TIMEOUT_SECONDS": ("test_set_materialization_timeout_seconds"),
    "TEST_SET_DOWNLOAD_MAX_ATTEMPTS": "test_set_download_max_attempts",
    "TEST_SET_MAX_ITEMS": "test_set_max_items",
    "TEST_SET_MAX_ITEM_BYTES": "test_set_max_item_bytes",
    "TEST_SET_MAX_TOTAL_BYTES": "test_set_max_total_bytes",
    "TEST_SET_MAX_DURATION_SECONDS": "test_set_max_duration_seconds",
    "TEST_SET_MAX_TOTAL_DURATION_SECONDS": "test_set_max_total_duration_seconds",
    "TEST_SET_MIN_SAMPLE_RATE_HZ": "test_set_min_sample_rate_hz",
    "TEST_SET_MAX_SAMPLE_RATE_HZ": "test_set_max_sample_rate_hz",
    "TEST_SET_MAX_CHANNELS": "test_set_max_channels",
    "TEST_SET_DURATION_TOLERANCE_SECONDS": "test_set_duration_tolerance_seconds",
    "SHUTDOWN_GRACE_SECONDS": "shutdown_grace_seconds",
    "GPU_QUERY_TIMEOUT_SECONDS": "gpu_query_timeout_seconds",
    "MIN_FREE_DISK_BYTES": "min_free_disk_bytes",
    "TELEMETRY_SPOOL_MAX_BYTES": "telemetry_spool_max_bytes",
    "WORKER_TAGS": "worker_tags",
}
_WORKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_RVC_DEVICE = re.compile(r"^(?:cuda(?::[0-9]+)?|cpu|mps)$")
DEFAULT_RUNTIME_ACTIVATION_PATH = Path("/run/rvc-release/runtime-activation.json")


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    manager_url: str
    worker_name: str
    worker_token: str = field(repr=False)
    data_root: Path
    credential_path: Path | None = None
    ca_bundle_path: Path | None = None
    runner_mode: str = "fake"
    rvc_profile_path: Path | None = None
    rvc_native_source_root: Path = Path("/opt/rvc-webui")
    rvc_native_python_executable: str = sys.executable
    rvc_native_cpu_workers: int = 2
    rvc_native_device: str = "cuda"
    rvc_native_use_half: bool = True
    rvc_native_preprocess_timeout_seconds: float = 3_600.0
    rvc_native_extraction_timeout_seconds: float = 7_200.0
    rvc_native_training_timeout_seconds: float = 7 * 24 * 3_600.0
    rvc_native_index_timeout_seconds: float = 24 * 3_600.0
    rvc_native_small_model_timeout_seconds: float = 3_600.0
    rvc_runtime_activation_path: Path = field(
        default=DEFAULT_RUNTIME_ACTIVATION_PATH,
        init=False,
    )
    heartbeat_interval_seconds: float = 10.0
    system_telemetry_interval_seconds: float = 60.0
    poll_interval_seconds: float = 5.0
    lease_renew_interval_seconds: float = 15.0
    request_timeout_seconds: float = 30.0
    artifact_upload_timeout_seconds: float = 3600.0
    artifact_upload_max_attempts: int = 3
    artifact_max_object_bytes: int = 5 * 1024**3
    artifact_max_files_per_attempt: int = 256
    artifact_max_total_bytes_per_attempt: int = 100 * 1024**3
    artifact_checkpoint_retention: int = 20
    dataset_download_timeout_seconds: float = 3600.0
    dataset_download_max_attempts: int = 3
    dataset_max_archive_bytes: int = 5 * 1024**3
    dataset_max_entries: int = 10_000
    dataset_max_file_bytes: int = 2 * 1024**3
    dataset_max_total_bytes: int = 20 * 1024**3
    dataset_max_compression_ratio: float = 200.0
    test_set_download_timeout_seconds: float = 3600.0
    test_set_materialization_timeout_seconds: float = 7200.0
    test_set_download_max_attempts: int = 3
    test_set_max_items: int = 128
    test_set_max_item_bytes: int = 256 * 1024**2
    test_set_max_total_bytes: int = 2 * 1024**3
    test_set_max_duration_seconds: float = 600.0
    test_set_max_total_duration_seconds: float = 3600.0
    test_set_min_sample_rate_hz: int = 8_000
    test_set_max_sample_rate_hz: int = 192_000
    test_set_max_channels: int = 2
    test_set_duration_tolerance_seconds: float = 0.000001
    shutdown_grace_seconds: float = 20.0
    gpu_query_timeout_seconds: float = 5.0
    min_free_disk_bytes: int = 5 * 1024**3
    telemetry_spool_max_bytes: int = 256 * 1024**2
    worker_tags: tuple[str, ...] = ()

    @classmethod
    def from_sources(
        cls,
        config_path: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> WorkerSettings:
        values: dict[str, Any] = {}
        if config_path is not None:
            values.update(_read_yaml(config_path))

        environment = os.environ if environ is None else environ
        if "RVC_RUNTIME_ACTIVATION_PATH" in environment:
            raise SettingsError(
                "RVC_RUNTIME_ACTIVATION_PATH is release-owned and cannot be overridden"
            )
        for env_name, field_name in _ENV_TO_FIELD.items():
            if env_name in environment:
                values[field_name] = environment[env_name]

        if overrides:
            values.update({key: value for key, value in overrides.items() if value is not None})

        token_file = values.pop("worker_token_file", None)
        if token_file:
            token_path = Path(str(token_file)).expanduser()
            try:
                values["worker_token"] = token_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SettingsError(f"cannot read worker token file: {token_path}") from exc

        return cls._validated(values)

    @classmethod
    def _validated(cls, raw: Mapping[str, Any]) -> WorkerSettings:
        if "rvc_runtime_activation_path" in raw:
            raise SettingsError(
                "rvc_runtime_activation_path is release-owned and cannot be overridden"
            )
        manager_url = str(raw.get("manager_url", "")).strip().rstrip("/")
        parsed = urlparse(manager_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SettingsError("manager_url must be an absolute http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise SettingsError("manager_url cannot contain credentials, query, or fragment")

        worker_name = str(raw.get("worker_name", "")).strip()
        if len(worker_name) > 128 or not _WORKER_NAME.fullmatch(worker_name):
            raise SettingsError(
                "worker_name must start with an alphanumeric and contain only letters, "
                "numbers, '.', '_' or '-'"
            )

        data_root_raw = raw.get("data_root")
        if not data_root_raw:
            raise SettingsError("data_root is required")
        data_root = Path(str(data_root_raw)).expanduser().resolve()
        credential_raw = raw.get("credential_path")
        credential_path = (
            Path(str(credential_raw)).expanduser().resolve()
            if credential_raw
            else data_root / "credentials" / "worker.json"
        )
        ca_bundle_raw = str(raw.get("ca_bundle_path", "")).strip()
        ca_bundle_path: Path | None = None
        if ca_bundle_raw:
            candidate_ca_path = Path(ca_bundle_raw)
            if candidate_ca_path != DEFAULT_CUSTOM_CA_BUNDLE_PATH:
                raise SettingsError(
                    "ca_bundle_path must use the fixed container path "
                    f"{DEFAULT_CUSTOM_CA_BUNDLE_PATH}"
                )
            try:
                read_custom_ca_bundle(
                    candidate_ca_path,
                    required_uid=0,
                    expected_path=DEFAULT_CUSTOM_CA_BUNDLE_PATH,
                )
            except CustomCABundleError as exc:
                raise SettingsError(str(exc)) from exc
            ca_bundle_path = candidate_ca_path
        worker_token = str(raw.get("worker_token", "")).strip()
        if not worker_token and not credential_path.is_file():
            raise SettingsError(
                "worker_token/worker_token_file is required until a persistent credential exists"
            )

        runner_mode = str(raw.get("runner_mode", "fake")).strip().lower()
        if runner_mode not in {"fake", "profile", "native"}:
            raise SettingsError("runner_mode must be 'fake', 'profile', or 'native'")

        profile_raw = raw.get("rvc_profile_path")
        profile = Path(str(profile_raw)).expanduser().resolve() if profile_raw else None
        if runner_mode == "profile" and profile is None:
            raise SettingsError("rvc_profile_path is required in profile mode")

        native_source_raw = raw.get("rvc_native_source_root", "/opt/rvc-webui")
        native_source_unresolved = Path(str(native_source_raw)).expanduser()
        if not native_source_unresolved.is_absolute():
            raise SettingsError("rvc_native_source_root must be an absolute path")
        native_source_root = native_source_unresolved.resolve()
        native_python = str(raw.get("rvc_native_python_executable", sys.executable)).strip()
        native_python_path = Path(native_python).expanduser()
        if not native_python_path.is_absolute() or "\x00" in native_python:
            raise SettingsError("rvc_native_python_executable must be an absolute NUL-free path")
        native_python = str(native_python_path.resolve())
        native_cpu_workers = _bounded_positive_int(
            raw.get("rvc_native_cpu_workers", 2),
            "rvc_native_cpu_workers",
            maximum=256,
        )
        native_device = str(raw.get("rvc_native_device", "cuda")).strip().lower()
        if _RVC_DEVICE.fullmatch(native_device) is None:
            raise SettingsError("rvc_native_device must be cuda, cuda:N, cpu, or mps")
        native_use_half = _boolean(raw.get("rvc_native_use_half", True), "rvc_native_use_half")
        native_timeouts = {
            name: _positive_float(raw.get(name, default), name)
            for name, default in (
                ("rvc_native_preprocess_timeout_seconds", 3_600.0),
                ("rvc_native_extraction_timeout_seconds", 7_200.0),
                ("rvc_native_training_timeout_seconds", 7 * 24 * 3_600.0),
                ("rvc_native_index_timeout_seconds", 24 * 3_600.0),
                ("rvc_native_small_model_timeout_seconds", 3_600.0),
            )
        }

        intervals = {
            name: _positive_float(raw.get(name, default), name)
            for name, default in (
                ("heartbeat_interval_seconds", 10.0),
                ("poll_interval_seconds", 5.0),
                ("lease_renew_interval_seconds", 15.0),
                ("request_timeout_seconds", 30.0),
                ("artifact_upload_timeout_seconds", 3600.0),
                ("dataset_download_timeout_seconds", 3600.0),
                ("test_set_download_timeout_seconds", 3600.0),
                ("test_set_materialization_timeout_seconds", 7200.0),
                ("shutdown_grace_seconds", 20.0),
                ("gpu_query_timeout_seconds", 5.0),
            )
        }
        intervals["system_telemetry_interval_seconds"] = _bounded_float(
            raw.get("system_telemetry_interval_seconds", 60.0),
            "system_telemetry_interval_seconds",
            minimum=10.0,
            maximum=3_600.0,
        )
        min_free_disk_bytes = _non_negative_int(
            raw.get("min_free_disk_bytes", 5 * 1024**3), "min_free_disk_bytes"
        )
        telemetry_spool_max_bytes = _positive_int(
            raw.get("telemetry_spool_max_bytes", 256 * 1024**2),
            "telemetry_spool_max_bytes",
        )
        artifact_upload_max_attempts = _bounded_positive_int(
            raw.get("artifact_upload_max_attempts", 3),
            "artifact_upload_max_attempts",
            maximum=10,
        )
        artifact_max_object_bytes = _positive_int(
            raw.get("artifact_max_object_bytes", 5 * 1024**3),
            "artifact_max_object_bytes",
        )
        artifact_max_files_per_attempt = _bounded_positive_int(
            raw.get("artifact_max_files_per_attempt", 256),
            "artifact_max_files_per_attempt",
            maximum=10_000,
        )
        artifact_max_total_bytes_per_attempt = _positive_int(
            raw.get("artifact_max_total_bytes_per_attempt", 100 * 1024**3),
            "artifact_max_total_bytes_per_attempt",
        )
        artifact_checkpoint_retention = _bounded_positive_int(
            raw.get("artifact_checkpoint_retention", 20),
            "artifact_checkpoint_retention",
            maximum=1_000,
        )
        dataset_download_max_attempts = _bounded_positive_int(
            raw.get("dataset_download_max_attempts", 3),
            "dataset_download_max_attempts",
            maximum=10,
        )
        dataset_max_archive_bytes = _positive_int(
            raw.get("dataset_max_archive_bytes", 5 * 1024**3),
            "dataset_max_archive_bytes",
        )
        dataset_max_entries = _bounded_positive_int(
            raw.get("dataset_max_entries", 10_000),
            "dataset_max_entries",
            maximum=100_000,
        )
        dataset_max_file_bytes = _positive_int(
            raw.get("dataset_max_file_bytes", 2 * 1024**3),
            "dataset_max_file_bytes",
        )
        dataset_max_total_bytes = _positive_int(
            raw.get("dataset_max_total_bytes", 20 * 1024**3),
            "dataset_max_total_bytes",
        )
        dataset_max_compression_ratio = _positive_float(
            raw.get("dataset_max_compression_ratio", 200.0),
            "dataset_max_compression_ratio",
        )
        test_set_download_max_attempts = _bounded_positive_int(
            raw.get("test_set_download_max_attempts", 3),
            "test_set_download_max_attempts",
            maximum=10,
        )
        test_set_max_items = _bounded_positive_int(
            raw.get("test_set_max_items", 128),
            "test_set_max_items",
            maximum=128,
        )
        test_set_max_item_bytes = _bounded_positive_int(
            raw.get("test_set_max_item_bytes", 256 * 1024**2),
            "test_set_max_item_bytes",
            maximum=2 * 1024**3,
        )
        test_set_max_total_bytes = _bounded_positive_int(
            raw.get("test_set_max_total_bytes", 2 * 1024**3),
            "test_set_max_total_bytes",
            maximum=100 * 1024**3,
        )
        test_set_max_duration_seconds = _positive_float(
            raw.get("test_set_max_duration_seconds", 600.0),
            "test_set_max_duration_seconds",
        )
        test_set_max_total_duration_seconds = _positive_float(
            raw.get("test_set_max_total_duration_seconds", 3600.0),
            "test_set_max_total_duration_seconds",
        )
        test_set_min_sample_rate_hz = _bounded_positive_int(
            raw.get("test_set_min_sample_rate_hz", 8_000),
            "test_set_min_sample_rate_hz",
            maximum=384_000,
        )
        test_set_max_sample_rate_hz = _bounded_positive_int(
            raw.get("test_set_max_sample_rate_hz", 192_000),
            "test_set_max_sample_rate_hz",
            maximum=384_000,
        )
        test_set_max_channels = _bounded_positive_int(
            raw.get("test_set_max_channels", 2),
            "test_set_max_channels",
            maximum=32,
        )
        test_set_duration_tolerance_seconds = _positive_float(
            raw.get("test_set_duration_tolerance_seconds", 0.000001),
            "test_set_duration_tolerance_seconds",
        )
        if artifact_max_total_bytes_per_attempt < artifact_max_object_bytes:
            raise SettingsError(
                "artifact_max_total_bytes_per_attempt cannot be smaller than "
                "artifact_max_object_bytes"
            )
        if artifact_checkpoint_retention * 2 > artifact_max_files_per_attempt:
            raise SettingsError(
                "artifact checkpoint retention cannot consume more than the file quota"
            )
        if artifact_max_files_per_attempt < test_set_max_items + 8:
            raise SettingsError(
                "artifact_max_files_per_attempt must reserve at least eight non-Sample "
                "files above test_set_max_items"
            )
        if dataset_max_file_bytes > dataset_max_total_bytes:
            raise SettingsError("dataset_max_file_bytes cannot exceed dataset_max_total_bytes")
        if dataset_max_archive_bytes > 5 * 1024**3:
            raise SettingsError("dataset_max_archive_bytes cannot exceed 5 GiB")
        if dataset_max_compression_ratio < 1:
            raise SettingsError("dataset_max_compression_ratio cannot be smaller than 1")
        if test_set_max_item_bytes < 44:
            raise SettingsError("test_set_max_item_bytes cannot be smaller than 44")
        if test_set_max_item_bytes > test_set_max_total_bytes:
            raise SettingsError("test_set_max_item_bytes cannot exceed test_set_max_total_bytes")
        if test_set_max_duration_seconds > 86_400:
            raise SettingsError("test_set_max_duration_seconds cannot exceed 86400")
        if test_set_max_total_duration_seconds > 86_400:
            raise SettingsError("test_set_max_total_duration_seconds cannot exceed 86400")
        if test_set_max_duration_seconds > test_set_max_total_duration_seconds:
            raise SettingsError(
                "test_set_max_duration_seconds cannot exceed test_set_max_total_duration_seconds"
            )
        if test_set_min_sample_rate_hz > test_set_max_sample_rate_hz:
            raise SettingsError(
                "test_set_min_sample_rate_hz cannot exceed test_set_max_sample_rate_hz"
            )
        if test_set_duration_tolerance_seconds > 1:
            raise SettingsError("test_set_duration_tolerance_seconds cannot exceed 1")
        worker_tags = _parse_tags(raw.get("worker_tags", ()))

        return cls(
            manager_url=manager_url,
            worker_name=worker_name,
            worker_token=worker_token,
            data_root=data_root,
            credential_path=credential_path,
            ca_bundle_path=ca_bundle_path,
            runner_mode=runner_mode,
            rvc_profile_path=profile,
            rvc_native_source_root=native_source_root,
            rvc_native_python_executable=native_python,
            rvc_native_cpu_workers=native_cpu_workers,
            rvc_native_device=native_device,
            rvc_native_use_half=native_use_half,
            min_free_disk_bytes=min_free_disk_bytes,
            telemetry_spool_max_bytes=telemetry_spool_max_bytes,
            artifact_upload_max_attempts=artifact_upload_max_attempts,
            artifact_max_object_bytes=artifact_max_object_bytes,
            artifact_max_files_per_attempt=artifact_max_files_per_attempt,
            artifact_max_total_bytes_per_attempt=artifact_max_total_bytes_per_attempt,
            artifact_checkpoint_retention=artifact_checkpoint_retention,
            dataset_download_max_attempts=dataset_download_max_attempts,
            dataset_max_archive_bytes=dataset_max_archive_bytes,
            dataset_max_entries=dataset_max_entries,
            dataset_max_file_bytes=dataset_max_file_bytes,
            dataset_max_total_bytes=dataset_max_total_bytes,
            dataset_max_compression_ratio=dataset_max_compression_ratio,
            test_set_download_max_attempts=test_set_download_max_attempts,
            test_set_max_items=test_set_max_items,
            test_set_max_item_bytes=test_set_max_item_bytes,
            test_set_max_total_bytes=test_set_max_total_bytes,
            test_set_max_duration_seconds=test_set_max_duration_seconds,
            test_set_max_total_duration_seconds=test_set_max_total_duration_seconds,
            test_set_min_sample_rate_hz=test_set_min_sample_rate_hz,
            test_set_max_sample_rate_hz=test_set_max_sample_rate_hz,
            test_set_max_channels=test_set_max_channels,
            test_set_duration_tolerance_seconds=test_set_duration_tolerance_seconds,
            worker_tags=worker_tags,
            **native_timeouts,
            **intervals,
        )

    def redacted(self) -> dict[str, Any]:
        return {
            "manager_url": self.manager_url,
            "worker_name": self.worker_name,
            "worker_token": "***",
            "data_root": str(self.data_root),
            "credential_path": str(self.credential_path) if self.credential_path else None,
            "ca_bundle_path": str(self.ca_bundle_path) if self.ca_bundle_path else None,
            "runner_mode": self.runner_mode,
            "rvc_profile_path": str(self.rvc_profile_path) if self.rvc_profile_path else None,
            "rvc_native_source_root": str(self.rvc_native_source_root),
            "rvc_native_python_executable": self.rvc_native_python_executable,
            "rvc_native_cpu_workers": self.rvc_native_cpu_workers,
            "rvc_native_device": self.rvc_native_device,
            "rvc_native_use_half": self.rvc_native_use_half,
            "rvc_native_preprocess_timeout_seconds": (self.rvc_native_preprocess_timeout_seconds),
            "rvc_native_extraction_timeout_seconds": (self.rvc_native_extraction_timeout_seconds),
            "rvc_native_training_timeout_seconds": self.rvc_native_training_timeout_seconds,
            "rvc_native_index_timeout_seconds": self.rvc_native_index_timeout_seconds,
            "rvc_native_small_model_timeout_seconds": (self.rvc_native_small_model_timeout_seconds),
            "rvc_runtime_activation_path": str(self.rvc_runtime_activation_path),
            "worker_tags": list(self.worker_tags),
            "system_telemetry_interval_seconds": self.system_telemetry_interval_seconds,
            "telemetry_spool_max_bytes": self.telemetry_spool_max_bytes,
            "artifact_upload_timeout_seconds": self.artifact_upload_timeout_seconds,
            "artifact_upload_max_attempts": self.artifact_upload_max_attempts,
            "artifact_max_object_bytes": self.artifact_max_object_bytes,
            "artifact_max_files_per_attempt": self.artifact_max_files_per_attempt,
            "artifact_max_total_bytes_per_attempt": self.artifact_max_total_bytes_per_attempt,
            "artifact_checkpoint_retention": self.artifact_checkpoint_retention,
            "dataset_download_timeout_seconds": self.dataset_download_timeout_seconds,
            "dataset_download_max_attempts": self.dataset_download_max_attempts,
            "dataset_max_archive_bytes": self.dataset_max_archive_bytes,
            "dataset_max_entries": self.dataset_max_entries,
            "dataset_max_file_bytes": self.dataset_max_file_bytes,
            "dataset_max_total_bytes": self.dataset_max_total_bytes,
            "dataset_max_compression_ratio": self.dataset_max_compression_ratio,
            "test_set_download_timeout_seconds": self.test_set_download_timeout_seconds,
            "test_set_materialization_timeout_seconds": (
                self.test_set_materialization_timeout_seconds
            ),
            "test_set_download_max_attempts": self.test_set_download_max_attempts,
            "test_set_max_items": self.test_set_max_items,
            "test_set_max_item_bytes": self.test_set_max_item_bytes,
            "test_set_max_total_bytes": self.test_set_max_total_bytes,
            "test_set_max_duration_seconds": self.test_set_max_duration_seconds,
            "test_set_max_total_duration_seconds": (self.test_set_max_total_duration_seconds),
            "test_set_min_sample_rate_hz": self.test_set_min_sample_rate_hz,
            "test_set_max_sample_rate_hz": self.test_set_max_sample_rate_hz,
            "test_set_max_channels": self.test_set_max_channels,
            "test_set_duration_tolerance_seconds": (self.test_set_duration_tolerance_seconds),
        }


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SettingsError(f"cannot read worker config: {path}") from exc
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise SettingsError("worker config must be a YAML mapping")
    worker_section = document.get("worker", document)
    if not isinstance(worker_section, dict):
        raise SettingsError("worker config section must be a mapping")
    return dict(worker_section)


def _positive_float(value: Any, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{name} must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise SettingsError(f"{name} must be finite and greater than zero")
    return parsed


def _bounded_float(
    value: Any,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    parsed = _positive_float(value, name)
    if parsed < minimum or parsed > maximum:
        raise SettingsError(f"{name} must be between {minimum:g} and {maximum:g}")
    return parsed


def _boolean(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise SettingsError(f"{name} must be a boolean")


def _non_negative_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise SettingsError(f"{name} cannot be negative")
    return parsed


def _positive_int(value: Any, name: str) -> int:
    parsed = _non_negative_int(value, name)
    if parsed == 0:
        raise SettingsError(f"{name} must be greater than zero")
    return parsed


def _bounded_positive_int(value: Any, name: str, *, maximum: int) -> int:
    parsed = _positive_int(value, name)
    if parsed > maximum:
        raise SettingsError(f"{name} cannot exceed {maximum}")
    return parsed


def _parse_tags(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates: list[Any] = value.split(",")
    elif isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        raise SettingsError("worker_tags must be a list or comma-separated string")
    tags = tuple(dict.fromkeys(str(item).strip() for item in candidates if str(item).strip()))
    if any(len(tag) > 64 for tag in tags):
        raise SettingsError("worker tags cannot exceed 64 characters")
    return tags
