"""Worker command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from collections.abc import Sequence
from pathlib import Path

from rvc_orchestrator_contracts import InferenceF0Method, WorkerReEnrollRequest

from .agent import WorkerAgent, _sample_runtime_evidence
from .client import HttpManagerClient, ManagerClientError
from .credentials import CredentialError, CredentialStore, WorkerCredential
from .gpu import NvidiaSmiCollector
from .runner import CommandProfile, RvcRunner, RvcRunnerError, create_runner
from .settings import SettingsError, WorkerSettings
from .token_rotation import reconcile_worker_token_rotation, rotate_worker_token
from .workspace import WorkspaceManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RVC Training Orchestrator GPU worker")
    parser.add_argument("--config", type=Path, help="YAML worker configuration")
    parser.add_argument("--manager-url")
    parser.add_argument("--worker-name")
    parser.add_argument("--worker-token-file", type=Path)
    parser.add_argument("--worker-credential-path", type=Path)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--runner-mode", choices=("fake", "profile", "native"))
    parser.add_argument("--rvc-profile-path", type=Path)
    parser.add_argument("--rvc-native-source-root", type=Path)
    parser.add_argument("--rvc-native-python-executable")
    parser.add_argument("--rvc-native-cpu-workers", type=int)
    parser.add_argument("--rvc-native-device")
    parser.add_argument(
        "--rvc-native-use-half",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    parser.add_argument("--rvc-native-preprocess-timeout-seconds", type=float)
    parser.add_argument("--rvc-native-extraction-timeout-seconds", type=float)
    parser.add_argument("--rvc-native-training-timeout-seconds", type=float)
    parser.add_argument("--rvc-native-index-timeout-seconds", type=float)
    parser.add_argument("--rvc-native-small-model-timeout-seconds", type=float)
    parser.add_argument("--test-set-materialization-timeout-seconds", type=float)
    parser.add_argument("--test-set-max-total-duration-seconds", type=float)
    parser.add_argument("--check", action="store_true", help="run local preflight and exit")
    parser.add_argument("--once", action="store_true", help="exit after one claimed job")
    parser.add_argument(
        "--rotate-token",
        action="store_true",
        help="rotate the persisted per-Worker bearer token while the Worker is idle",
    )
    parser.add_argument(
        "--re-enroll",
        action="store_true",
        help="re-enroll an administrator-revoked Worker using the bootstrap credential",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    overrides = {
        "manager_url": args.manager_url,
        "worker_name": args.worker_name,
        "worker_token_file": args.worker_token_file,
        "credential_path": args.worker_credential_path,
        "data_root": args.data_root,
        "runner_mode": args.runner_mode,
        "rvc_profile_path": args.rvc_profile_path,
        "rvc_native_source_root": args.rvc_native_source_root,
        "rvc_native_python_executable": args.rvc_native_python_executable,
        "rvc_native_cpu_workers": args.rvc_native_cpu_workers,
        "rvc_native_device": args.rvc_native_device,
        "rvc_native_use_half": args.rvc_native_use_half,
        "rvc_native_preprocess_timeout_seconds": (args.rvc_native_preprocess_timeout_seconds),
        "rvc_native_extraction_timeout_seconds": (args.rvc_native_extraction_timeout_seconds),
        "rvc_native_training_timeout_seconds": args.rvc_native_training_timeout_seconds,
        "rvc_native_index_timeout_seconds": args.rvc_native_index_timeout_seconds,
        "rvc_native_small_model_timeout_seconds": (args.rvc_native_small_model_timeout_seconds),
        "test_set_materialization_timeout_seconds": (args.test_set_materialization_timeout_seconds),
        "test_set_max_total_duration_seconds": (args.test_set_max_total_duration_seconds),
    }
    try:
        if sum((args.check, args.rotate_token, args.re_enroll)) > 1:
            raise SettingsError("--check, --rotate-token, and --re-enroll are mutually exclusive")
        settings = WorkerSettings.from_sources(args.config, overrides=overrides)
        runner = create_runner(
            settings.runner_mode,
            profile_path=settings.rvc_profile_path,
            native_source_root=settings.rvc_native_source_root,
            native_python_executable=settings.rvc_native_python_executable,
            native_cpu_workers=settings.rvc_native_cpu_workers,
            native_device=settings.rvc_native_device,
            native_use_half=settings.rvc_native_use_half,
            native_preprocess_timeout_seconds=(settings.rvc_native_preprocess_timeout_seconds),
            native_extraction_timeout_seconds=(settings.rvc_native_extraction_timeout_seconds),
            native_training_timeout_seconds=settings.rvc_native_training_timeout_seconds,
            native_index_timeout_seconds=settings.rvc_native_index_timeout_seconds,
            native_small_model_timeout_seconds=(settings.rvc_native_small_model_timeout_seconds),
            runtime_activation_path=settings.rvc_runtime_activation_path,
        )
        if args.check:
            return _run_check(settings, runner)
        if settings.credential_path is None:
            raise SettingsError("credential_path is required")
        credential_store = CredentialStore(settings.credential_path)
        credential = credential_store.load(
            manager_url=settings.manager_url, worker_name=settings.worker_name
        )
        client = HttpManagerClient(
            settings.manager_url,
            settings.worker_token,
            worker_token=credential.worker_token if credential else None,
            timeout_seconds=settings.request_timeout_seconds,
            artifact_upload_timeout_seconds=settings.artifact_upload_timeout_seconds,
            dataset_download_timeout_seconds=settings.dataset_download_timeout_seconds,
            dataset_max_bytes=settings.dataset_max_archive_bytes,
            test_set_download_timeout_seconds=(settings.test_set_download_timeout_seconds),
            test_set_max_item_bytes=settings.test_set_max_item_bytes,
            ca_bundle_path=settings.ca_bundle_path,
        )
        agent = WorkerAgent(settings, client, runner, credential_store=credential_store)
        if args.re_enroll:
            if credential is None:
                raise CredentialError(
                    "the revoked Worker's persistent credential metadata is required"
                )
            asyncio.run(
                _re_enroll_worker(
                    agent,
                    client,
                    credential_store,
                    credential,
                )
            )
            logging.getLogger(__name__).info("Worker re-enrollment completed")
            return 0
        if args.rotate_token:
            if credential is None:
                raise CredentialError(
                    "a persistent Worker credential is required for token rotation"
                )
            asyncio.run(
                rotate_worker_token(
                    client,
                    credential_store,
                    credential,
                )
            )
            logging.getLogger(__name__).info("Worker bearer token rotation completed")
            return 0
        return asyncio.run(
            _recover_rotation_and_run_agent(
                agent,
                client,
                credential_store,
                credential,
                max_jobs=1 if args.once else None,
            )
        )
    except (SettingsError, RvcRunnerError, CredentialError, ManagerClientError) as exc:
        logging.getLogger(__name__).error("worker configuration error: %s", exc)
        return 2


async def _run_agent(agent: WorkerAgent, *, max_jobs: int | None) -> int:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, agent.request_shutdown)
        except NotImplementedError:
            pass
    await agent.run(max_jobs=max_jobs)
    return 0


async def _recover_rotation_and_run_agent(
    agent: WorkerAgent,
    client: HttpManagerClient,
    credential_store: CredentialStore,
    credential: WorkerCredential | None,
    *,
    max_jobs: int | None,
) -> int:
    if credential is not None:
        await reconcile_worker_token_rotation(client, credential_store, credential)
    return await _run_agent(agent, max_jobs=max_jobs)


async def _re_enroll_worker(
    agent: WorkerAgent,
    client: HttpManagerClient,
    credential_store: CredentialStore,
    credential: WorkerCredential,
) -> None:
    response = await client.re_enroll(
        WorkerReEnrollRequest(
            worker_id=credential.worker_id,
            name=credential.worker_name,
            capabilities=await agent.registration_capabilities(),
        )
    )
    if response.worker_id != credential.worker_id:
        raise CredentialError("Manager re-enrolled a different Worker identity")
    credential_store.save(
        WorkerCredential(
            manager_url=credential.manager_url,
            worker_id=credential.worker_id,
            worker_name=credential.worker_name,
            worker_token=response.worker_token,
        )
    )


def _run_check(settings: WorkerSettings, runner: RvcRunner) -> int:
    workspace = WorkspaceManager(
        settings.data_root / "jobs", min_free_bytes=settings.min_free_disk_bytes
    )
    free_disk = workspace.check_disk()
    gpu = NvidiaSmiCollector(timeout_seconds=settings.gpu_query_timeout_seconds).collect()
    profile_revision = None
    if settings.runner_mode == "profile" and settings.rvc_profile_path:
        profile_revision = CommandProfile.load(settings.rvc_profile_path).expected_commit_hash
    native_revision: str | None = None
    native_assets_ready = False
    if settings.runner_mode == "native":
        revision_value: object = getattr(runner, "verified_commit_hash", None)
        if isinstance(revision_value, str):
            native_revision = revision_value
        native_assets_ready = getattr(runner, "assets_ready", False) is True
    runtime_evidence = _sample_runtime_evidence(settings, runner)
    sample_ready = runtime_evidence is not None
    print(
        json.dumps(
            {
                "ok": True,
                "settings": settings.redacted(),
                "disk_free_bytes": free_disk,
                "gpu_available": bool(gpu.gpus),
                "gpu_telemetry_available": gpu.available,
                "gpu_count": len(gpu.gpus),
                "gpu_error": gpu.error,
                "rvc_profile_revision": profile_revision,
                "rvc_native_revision": native_revision,
                "rvc_native_assets_ready": native_assets_ready,
                "fixed_test_set_inference_ready": sample_ready,
                "supported_inference_f0_methods": (
                    [method.value for method in InferenceF0Method] if sample_ready else []
                ),
                "runtime_image_digest": (
                    runtime_evidence.runtime_image_digest if runtime_evidence else None
                ),
                "runtime_asset_manifest_sha256": (
                    runtime_evidence.runtime_asset_manifest_sha256 if runtime_evidence else None
                ),
                "test_set_limits": {
                    "download_timeout_seconds": (settings.test_set_download_timeout_seconds),
                    "materialization_timeout_seconds": (
                        settings.test_set_materialization_timeout_seconds
                    ),
                    "download_max_attempts": settings.test_set_download_max_attempts,
                    "max_items": settings.test_set_max_items,
                    "max_item_bytes": settings.test_set_max_item_bytes,
                    "max_total_bytes": settings.test_set_max_total_bytes,
                    "max_duration_seconds": settings.test_set_max_duration_seconds,
                    "max_total_duration_seconds": (settings.test_set_max_total_duration_seconds),
                    "min_sample_rate_hz": settings.test_set_min_sample_rate_hz,
                    "max_sample_rate_hz": settings.test_set_max_sample_rate_hz,
                    "max_channels": settings.test_set_max_channels,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0
