from __future__ import annotations

import asyncio
import hashlib
import json
import unittest
import wave
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

from rvc_orchestrator_contracts import (
    RVC_REVIEWED_COMMIT,
    ArtifactType,
    InferenceF0Method,
    InferencePresetConfig,
    JobClaim,
    JobStatus,
    SampleMetricsEvidence,
    SampleMetricValues,
    SampleRead,
    SampleRegistrationRequest,
)
from rvc_worker.agent import ActiveJob, WorkerAgent
from rvc_worker.artifacts import sha256_file
from rvc_worker.client import (
    ArtifactTransferCancelled,
    HttpManagerClient,
    ManagerClientError,
)
from rvc_worker.gpu import GpuCollection
from rvc_worker.native_inference import (
    NativeInferencePublication,
    NativeInferencePublishedFile,
    NativeInferencePublishedSample,
)
from rvc_worker.native_runner import NativeSampleInferenceRuntimeEvidence
from rvc_worker.runner import RvcRuntimeIntegrityError, StageResult
from rvc_worker.sample_publication import (
    NativeSamplePublicationError,
    build_sample_registration_requests,
    expand_finalized_artifacts,
    prepare_native_sample_publication,
    validate_finalized_artifact,
    validate_registered_sample,
)
from rvc_worker.settings import WorkerSettings
from rvc_worker.stages import StageExecutionCancelled, StageExecutionError, StageExecutor
from rvc_worker.uploads import (
    ArtifactUploadCandidate,
    ArtifactUploadInitRequest,
    PublishedArtifact,
    collect_artifact_candidates,
)
from rvc_worker.workspace import JobWorkspace, WorkspaceManager

from .helpers import make_claim

_JOB_ID = "10000000-0000-4000-8000-000000000001"
_ATTEMPT_ID = "10000000-0000-4000-8000-000000000002"
_LEASE_ID = "10000000-0000-4000-8000-000000000003"
_TEST_SET_ID = "10000000-0000-4000-8000-000000000004"
_FAMILY_ID = "10000000-0000-4000-8000-000000000005"
_ITEM_IDS = (
    "10000000-0000-4000-8000-000000000006",
    "10000000-0000-4000-8000-000000000007",
)
_RUNTIME_IMAGE_DIGEST = f"sha256:{'a' * 64}"
_RUNTIME_ASSET_MANIFEST_SHA256 = "b" * 64


class SampleRegistrationClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_register_sample_accepts_create_and_idempotent_replay(self) -> None:
        request = _registration_request()
        response = _sample_response(_JOB_ID, request, sequence=1)
        statuses = [201, 200]
        captured: list[httpx.Request] = []
        factory_calls = 0

        async def handle(http_request: httpx.Request) -> httpx.Response:
            captured.append(http_request)
            return httpx.Response(
                statuses.pop(0),
                json=response.model_dump(mode="json"),
            )

        def transport_factory() -> httpx.AsyncBaseTransport:
            nonlocal factory_calls
            factory_calls += 1
            return httpx.MockTransport(handle)

        client = HttpManagerClient(
            "https://manager.example",
            "bootstrap",
            worker_token="worker-token",
            manager_transport_factory=transport_factory,
        )

        created = await client.register_sample(_JOB_ID, request)
        replayed = await client.register_sample(_JOB_ID, request)

        self.assertEqual(created, response)
        self.assertEqual(replayed, response)
        self.assertEqual(factory_calls, 2)
        self.assertEqual(len(captured), 2)
        for observed in captured:
            self.assertEqual(
                observed.url.path,
                f"/api/v1/workers/jobs/{_JOB_ID}/samples",
            )
            self.assertEqual(observed.headers["Authorization"], "Bearer worker-token")
            self.assertEqual(
                json.loads((await observed.aread()).decode()),
                request.model_dump(mode="json"),
            )

    async def test_register_sample_classifies_retryable_and_permanent_failures(self) -> None:
        request = _registration_request()
        for status, retryable in (
            (202, False),
            (409, False),
            (422, False),
            (429, True),
            (503, True),
        ):
            with self.subTest(status=status):

                async def handle(
                    http_request: httpx.Request,
                    response_status: int = status,
                ) -> httpx.Response:
                    del http_request
                    return httpx.Response(response_status, json={"detail": "rejected"})

                client = HttpManagerClient(
                    "https://manager.example",
                    "bootstrap",
                    worker_token="worker-token",
                    manager_transport_factory=lambda: httpx.MockTransport(handle),
                )
                with self.assertRaises(ManagerClientError) as raised:
                    await client.register_sample(_JOB_ID, request)
                self.assertEqual(raised.exception.status_code, status)
                self.assertEqual(raised.exception.retryable, retryable)

    async def test_register_sample_transport_timeout_is_retryable(self) -> None:
        request = _registration_request()

        async def handle(http_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=http_request)

        client = HttpManagerClient(
            "https://manager.example",
            "bootstrap",
            worker_token="worker-token",
            manager_transport_factory=lambda: httpx.MockTransport(handle),
        )

        with self.assertRaises(ManagerClientError) as raised:
            await client.register_sample(_JOB_ID, request)

        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.category, "transport")

    async def test_register_sample_slow_trickle_has_absolute_deadline(self) -> None:
        request = _registration_request()
        encoded = _sample_response(_JOB_ID, request, sequence=1).model_dump_json().encode()

        class SlowBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                for offset in range(0, len(encoded), 32):
                    await asyncio.sleep(0.02)
                    yield encoded[offset : offset + 32]

        async def handle(http_request: httpx.Request) -> httpx.Response:
            del http_request
            return httpx.Response(201, stream=SlowBody())

        client = HttpManagerClient(
            "https://manager.example",
            "bootstrap",
            worker_token="worker-token",
            timeout_seconds=0.05,
            manager_transport_factory=lambda: httpx.MockTransport(handle),
        )
        with self.assertRaises(ManagerClientError) as raised:
            await client.register_sample(_JOB_ID, request)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.category, "transport")

    async def test_register_sample_cancellation_closes_in_flight_request(self) -> None:
        request = _registration_request()
        started = asyncio.Event()
        handler_cancelled = asyncio.Event()

        async def handle(http_request: httpx.Request) -> httpx.Response:
            del http_request
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                handler_cancelled.set()
                raise
            raise AssertionError("unreachable")

        client = HttpManagerClient(
            "https://manager.example",
            "bootstrap",
            worker_token="worker-token",
            manager_transport_factory=lambda: httpx.MockTransport(handle),
        )
        cancellation = asyncio.Event()
        task = asyncio.create_task(
            client.register_sample(_JOB_ID, request, cancellation=cancellation)
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        cancellation.set()

        with self.assertRaises(ArtifactTransferCancelled):
            await asyncio.wait_for(task, timeout=1)
        self.assertTrue(handler_cancelled.is_set())


class NativeSamplePublicationTests(unittest.TestCase):
    def test_two_identical_pcm_outputs_remain_distinct_ordered_samples(self) -> None:
        with TemporaryDirectory() as temporary:
            claim, workspace, publication = _publication_fixture(Path(temporary))
            candidates = collect_artifact_candidates(claim, workspace, is_fake=False)

            plan = prepare_native_sample_publication(
                claim,
                workspace,
                publication,
                candidates,
            )

            self.assertIsNone(plan.index_candidate)
            self.assertEqual(len(plan.sample_candidates), 2)
            self.assertEqual(
                len({candidate.sha256 for candidate in plan.sample_candidates}),
                1,
            )
            self.assertEqual(
                len(
                    [
                        candidate
                        for candidate in plan.upload_candidates
                        if candidate.artifact_type is ArtifactType.SAMPLE
                    ]
                ),
                1,
            )
            self.assertEqual(
                len(set(plan.sample_canonical_relative_paths)),
                1,
            )
            self.assertTrue(
                all(
                    "sample_registration" not in candidate.metadata
                    for candidate in plan.sample_candidates
                )
            )
            for candidate in (
                plan.model_candidate,
                *plan.sample_candidates,
            ):
                self.assertEqual(candidate.metadata["rvc_commit_hash"], RVC_REVIEWED_COMMIT)
                self.assertEqual(
                    candidate.metadata["runtime_image_digest"],
                    _RUNTIME_IMAGE_DIGEST,
                )

            finalized_uploads = {
                candidate.relative_path: _published_artifact(
                    claim,
                    candidate,
                    sequence=position,
                )
                for position, candidate in enumerate(plan.upload_candidates, start=1)
            }
            finalized = expand_finalized_artifacts(claim, plan, finalized_uploads)
            requests = build_sample_registration_requests(claim, plan, finalized)

            self.assertEqual(
                [request.test_set_item_id for request in requests],
                list(_ITEM_IDS),
            )
            self.assertTrue(all(request.index_sha256 is None for request in requests))
            self.assertEqual(requests[0].output_sha256, requests[1].output_sha256)
            self.assertEqual(requests[0].artifact_id, requests[1].artifact_id)
            for sequence, request in enumerate(requests, start=1):
                validate_registered_sample(
                    claim,
                    request,
                    _sample_response(claim.job_id, request, sequence=sequence),
                )

    def test_tampered_publication_artifact_and_response_fail_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            claim, workspace, publication = _publication_fixture(Path(temporary))
            candidates = collect_artifact_candidates(claim, workspace, is_fake=False)
            with self.assertRaises(NativeSamplePublicationError):
                prepare_native_sample_publication(
                    claim,
                    workspace,
                    replace(publication, attempt_id=_JOB_ID),
                    candidates,
                )
            with self.assertRaises(NativeSamplePublicationError):
                prepare_native_sample_publication(
                    claim,
                    workspace,
                    replace(publication, metrics_algorithm="unreviewed"),
                    candidates,
                )

            plan = prepare_native_sample_publication(
                claim,
                workspace,
                publication,
                candidates,
            )
            candidate = plan.sample_candidates[0]
            finalized = _published_artifact(claim, candidate, sequence=1)
            tampered_artifact = finalized.model_copy(
                update={"sha256": "0" * 64},
            )
            with self.assertRaises(NativeSamplePublicationError):
                validate_finalized_artifact(claim, candidate, tampered_artifact)

            finalized_uploads = {
                item.relative_path: _published_artifact(
                    claim,
                    item,
                    sequence=position,
                )
                for position, item in enumerate(plan.upload_candidates, start=1)
            }
            finalized_by_path = expand_finalized_artifacts(
                claim,
                plan,
                finalized_uploads,
            )
            tampered_aliases = dict(finalized_by_path)
            alias_candidate = plan.sample_candidates[1]
            tampered_aliases[alias_candidate.relative_path] = _published_artifact(
                claim,
                alias_candidate,
                sequence=99,
            )
            with self.assertRaises(NativeSamplePublicationError):
                build_sample_registration_requests(
                    claim,
                    plan,
                    tampered_aliases,
                )
            request = build_sample_registration_requests(
                claim,
                plan,
                finalized_by_path,
            )[0]
            response = _sample_response(claim.job_id, request, sequence=1)
            bad_metrics = response.metrics.model_copy(
                update={"worker_reported_duration_seconds": (request.output_duration_seconds + 1)}
            )
            with self.assertRaises(NativeSamplePublicationError):
                validate_registered_sample(
                    claim,
                    request,
                    response.model_copy(update={"metrics": bad_metrics}),
                )


class WorkerSamplePublicationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_artifacts_finalize_before_ordered_registration_and_retry(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim, workspace, publication = _publication_fixture(root)
            manager = _PublicationManager(
                registration_failures=[
                    ManagerClientError("temporarily unavailable", status_code=503)
                ]
            )
            loader = _PublicationLoader(publication)
            agent = _publication_agent(root, claim, manager, loader, max_attempts=3)

            capabilities = await agent._capabilities()  # noqa: SLF001
            self.assertTrue(capabilities.fixed_test_set_inference_ready)
            self.assertEqual(
                capabilities.supported_inference_f0_methods,
                list(InferenceF0Method),
            )
            self.assertEqual(
                capabilities.runtime_image_digest,
                publication.runtime_image_digest,
            )
            self.assertEqual(
                capabilities.runtime_asset_manifest_sha256,
                publication.runtime_asset_manifest_sha256,
            )
            statuses: list[JobStatus] = []

            async def update_status(job_id, update) -> None:
                del job_id
                statuses.append(update.status)

            assert agent.active_job is not None
            executor = StageExecutor(
                _AllStagesRunner(),
                update_status,
                agent._report_stage,  # noqa: SLF001
            )
            with patch("rvc_worker.agent._wait_any", new=_no_wait):
                await executor.execute(
                    claim,
                    workspace,
                    agent.active_job.cancellation,
                )

            artifact_events = [event for event in manager.events if event[0] == "artifact"]
            registration_events = [event for event in manager.events if event[0] == "sample"]
            self.assertEqual(len(artifact_events), 2)
            self.assertEqual(
                manager.events.index(registration_events[0]),
                len(artifact_events),
            )
            self.assertEqual(
                [event[1] for event in registration_events],
                [_ITEM_IDS[0], _ITEM_IDS[0], _ITEM_IDS[1]],
            )
            self.assertEqual(
                manager.registration_requests[0],
                manager.registration_requests[1],
            )
            self.assertTrue(
                all(request.index_sha256 is None for request in manager.registration_requests)
            )
            sample_uploads = [
                request
                for request in manager.artifact_requests
                if request.artifact_type is ArtifactType.SAMPLE
            ]
            self.assertEqual(len(sample_uploads), 1)
            self.assertEqual(
                len({request.artifact_id for request in manager.registration_requests}),
                1,
            )
            self.assertEqual(loader.calls, 1)
            self.assertEqual(statuses[-1], JobStatus.COMPLETED)

    async def test_distinct_pcm_outputs_publish_distinct_sample_artifacts(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim, workspace, publication = _publication_fixture(
                root,
                identical_outputs=False,
            )
            manager = _PublicationManager()
            loader = _PublicationLoader(publication)
            agent = _publication_agent(root, claim, manager, loader, max_attempts=1)

            await agent._report_stage(  # noqa: SLF001
                claim,
                workspace,
                JobStatus.UPLOADING_ARTIFACTS,
                StageResult(),
                1,
            )

            sample_uploads = [
                request
                for request in manager.artifact_requests
                if request.artifact_type is ArtifactType.SAMPLE
            ]
            self.assertEqual(len(sample_uploads), 2)
            self.assertNotEqual(sample_uploads[0].sha256, sample_uploads[1].sha256)
            self.assertEqual(len(manager.registration_requests), 2)
            self.assertEqual(
                len({request.artifact_id for request in manager.registration_requests}),
                2,
            )

    async def test_permanent_registration_conflict_is_not_retried(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim, workspace, publication = _publication_fixture(root)
            manager = _PublicationManager(
                registration_failures=[ManagerClientError("conflict", status_code=409)]
            )
            agent = _publication_agent(
                root,
                claim,
                manager,
                _PublicationLoader(publication),
                max_attempts=3,
            )
            statuses: list[JobStatus] = []

            async def update_status(job_id, update) -> None:
                del job_id
                statuses.append(update.status)

            executor = StageExecutor(
                _AllStagesRunner(),
                update_status,
                agent._report_stage,  # noqa: SLF001
            )
            with self.assertRaises(StageExecutionError) as raised:
                await executor.execute(
                    claim,
                    workspace,
                    agent.active_job.cancellation,
                )

            self.assertEqual(raised.exception.error_code, "stage_remote_rejected")
            self.assertEqual(len(manager.registration_requests), 1)
            self.assertNotIn(JobStatus.COMPLETED, statuses)

    async def test_transport_timeout_exhausts_bounded_registration_attempts(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim, workspace, publication = _publication_fixture(root)
            manager = _PublicationManager(
                registration_failures=[
                    ManagerClientError(
                        "timeout",
                        retryable=True,
                        category="transport",
                    )
                    for _ in range(3)
                ]
            )
            agent = _publication_agent(
                root,
                claim,
                manager,
                _PublicationLoader(publication),
                max_attempts=3,
            )

            with patch("rvc_worker.agent._wait_any", new=_no_wait):
                with self.assertRaises(ManagerClientError) as raised:
                    await agent._report_stage(  # noqa: SLF001
                        claim,
                        workspace,
                        JobStatus.UPLOADING_ARTIFACTS,
                        StageResult(),
                        1,
                    )

            self.assertTrue(raised.exception.retryable)
            self.assertEqual(len(manager.registration_requests), 3)
            self.assertEqual(
                manager.registration_requests,
                [manager.registration_requests[0]] * 3,
            )

    async def test_registration_cancellation_becomes_stage_cancellation(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim, workspace, publication = _publication_fixture(root)
            manager = _PublicationManager(cancel_registration=True)
            agent = _publication_agent(
                root,
                claim,
                manager,
                _PublicationLoader(publication),
                max_attempts=3,
            )

            with self.assertRaises(StageExecutionCancelled):
                await agent._report_stage(  # noqa: SLF001
                    claim,
                    workspace,
                    JobStatus.UPLOADING_ARTIFACTS,
                    StageResult(),
                    1,
                )

            self.assertEqual(len(manager.registration_requests), 1)

    async def test_publication_mismatch_prevents_every_artifact_upload(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim, workspace, publication = _publication_fixture(root)
            manager = _PublicationManager()
            loader = _PublicationLoader(publication)
            agent = _publication_agent(root, claim, manager, loader, max_attempts=1)
            assert agent.active_job is not None
            agent.active_job.claim_runtime_image_digest = f"sha256:{'c' * 64}"

            with self.assertRaises(RvcRuntimeIntegrityError):
                await agent._report_stage(  # noqa: SLF001
                    claim,
                    workspace,
                    JobStatus.UPLOADING_ARTIFACTS,
                    StageResult(),
                    1,
                )

            self.assertFalse(manager.artifact_requests)
            self.assertFalse(manager.registration_requests)


class _PublicationLoader:
    def __init__(self, publication: NativeInferencePublication) -> None:
        self.publication = publication
        self.calls = 0
        self.runtime_image_digest = publication.runtime_image_digest

    def load_publication(self, context) -> NativeInferencePublication:
        self.calls += 1
        if context.claim.job_id != self.publication.job_id:
            raise AssertionError("publication loader received another Job")
        return self.publication


class _NativeRunner:
    verified_commit_hash = RVC_REVIEWED_COMMIT
    assets_ready = True

    def __init__(self, loader: _PublicationLoader) -> None:
        self.sample_inference_dependency = loader
        self.asset_manifest_sha256 = loader.publication.runtime_asset_manifest_sha256

    @property
    def sample_inference_runtime_evidence(self) -> NativeSampleInferenceRuntimeEvidence:
        return NativeSampleInferenceRuntimeEvidence(
            runtime_image_digest=self.sample_inference_dependency.runtime_image_digest,
            runtime_asset_manifest_sha256=self.asset_manifest_sha256,
        )

    async def run_stage(self, stage, context, cancellation):
        del stage, context, cancellation
        raise AssertionError("publication tests do not execute native stages")


class _AllStagesRunner:
    async def run_stage(self, stage, context, cancellation) -> StageResult:
        del stage, context, cancellation
        return StageResult()


class _NoGpuCollector:
    def collect(self) -> GpuCollection:
        return GpuCollection((), False, "test has no GPU")


class _PublicationManager:
    def __init__(
        self,
        *,
        registration_failures: list[ManagerClientError] | None = None,
        cancel_registration: bool = False,
    ) -> None:
        self.registration_failures = list(registration_failures or [])
        self.cancel_registration = cancel_registration
        self.events: list[tuple[str, str]] = []
        self.artifact_requests: list[ArtifactUploadInitRequest] = []
        self.registration_requests: list[SampleRegistrationRequest] = []
        self.registered: dict[str, SampleRead] = {}
        self.artifacts_by_dedupe_key: dict[
            tuple[str, ArtifactType, str],
            tuple[ArtifactUploadInitRequest, PublishedArtifact],
        ] = {}

    async def send_logs(self, job_id, batch) -> None:
        del job_id, batch

    async def send_metrics(self, job_id, batch) -> None:
        del job_id, batch

    async def publish_artifact(
        self,
        job_id,
        request,
        source,
        *,
        cancellation=None,
    ) -> PublishedArtifact:
        del source, cancellation
        self.artifact_requests.append(request)
        self.events.append(("artifact", request.metadata["source_relative_path"]))
        dedupe_key = (request.attempt_id, request.artifact_type, request.sha256)
        existing = self.artifacts_by_dedupe_key.get(dedupe_key)
        if existing is not None:
            existing_request, existing_artifact = existing
            if existing_request != request:
                raise ManagerClientError("Artifact fingerprint conflict", status_code=409)
            return existing_artifact
        sequence = len(self.artifacts_by_dedupe_key) + 1
        verification = {
            "algorithm": "sha256",
            "bounded_stream": True,
            "upload_session_id": f"upload-{sequence}",
            "storage_backend": "local",
        }
        artifact = PublishedArtifact(
            id=_uuid(2, sequence),
            job_id=job_id,
            attempt_id=request.attempt_id,
            artifact_type=request.artifact_type,
            filename=request.filename,
            size_bytes=request.size_bytes,
            sha256=request.sha256,
            mime_type=request.content_type,
            metadata_json={**request.metadata, "manager_verification": verification},
            created_at=datetime.now(UTC),
        )
        self.artifacts_by_dedupe_key[dedupe_key] = (request, artifact)
        return artifact

    async def register_sample(
        self,
        job_id,
        request,
        *,
        cancellation=None,
    ) -> SampleRead:
        self.registration_requests.append(request)
        self.events.append(("sample", request.test_set_item_id))
        if self.cancel_registration:
            if cancellation is not None:
                cancellation.set()
            raise ArtifactTransferCancelled("cancelled")
        if self.registration_failures:
            raise self.registration_failures.pop(0)
        response = self.registered.get(request.test_set_item_id)
        if response is None:
            response = _sample_response(
                job_id,
                request,
                sequence=len(self.registered) + 1,
            )
            self.registered[request.test_set_item_id] = response
        return response


def _sample_claim(*, build_index: bool = False) -> JobClaim:
    payload = make_claim(samples=True).model_dump(mode="json")
    payload["job_id"] = _JOB_ID
    payload["attempt_id"] = _ATTEMPT_ID
    payload["lease_id"] = _LEASE_ID
    payload["lease_expires_at"] = datetime.now(UTC) + timedelta(minutes=5)
    payload["dataset_transfer"]["download_path"] = f"/api/v1/workers/jobs/{_JOB_ID}/dataset"
    config = payload["config"]
    config["rvc_backend"]["rvc_commit_hash"] = RVC_REVIEWED_COMMIT
    config["index"]["build_index"] = build_index
    config["auto_inference_samples"]["test_set_id"] = _TEST_SET_ID
    if not build_index:
        config["auto_inference_samples"]["index_rate"] = 0
    inference = {
        key: value
        for key, value in config["auto_inference_samples"].items()
        if key not in {"enabled", "test_set_id"}
    }
    inference = InferencePresetConfig.model_validate(inference).model_dump(mode="json")
    canonical = json.dumps(
        inference,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    items = []
    for order, item_id in enumerate(_ITEM_IDS):
        items.append(
            {
                "test_set_item_id": item_id,
                "item_key": f"sample-{order + 1}",
                "sort_order": order,
                "download_path": (f"/api/v1/workers/jobs/{_JOB_ID}/test-set/items/{item_id}"),
                "filename": f"{item_id}.wav",
                "content_type": "audio/wav",
                "size_bytes": 44,
                "sha256": str(order + 1) * 64,
                "sample_rate_hz": 8_000,
                "channels": 1,
                "duration_seconds": 0.001,
            }
        )
    payload["test_set_transfer"] = {
        "test_set_id": _TEST_SET_ID,
        "family_id": _FAMILY_ID,
        "revision": 2,
        "manifest_sha256": "d" * 64,
        "sample_plan_sha256": "e" * 64,
        "inference_config": inference,
        "inference_config_sha256": hashlib.sha256(canonical).hexdigest(),
        "items": items,
    }
    return JobClaim.model_validate(payload)


def _publication_fixture(
    root: Path,
    *,
    identical_outputs: bool = True,
) -> tuple[JobClaim, JobWorkspace, NativeInferencePublication]:
    claim = _sample_claim(build_index=False)
    workspace = WorkspaceManager(root / "jobs", min_free_bytes=0).prepare(
        claim.job_id,
        claim.attempt_id,
    )
    model = workspace.outputs / "model" / "final_small_model.pth"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"reviewed-small-model")
    sample_root = workspace.outputs / "samples"
    sample_root.mkdir(parents=True)
    manifest = sample_root / "inference_manifest.json"
    manifest.write_bytes(b'{"schema_version":1}\n')
    sample_paths = []
    sample_frame_counts = []
    for order, item_id in enumerate(_ITEM_IDS):
        path = sample_root / f"{item_id}.wav"
        frame_count = 800 if identical_outputs else 800 + order
        _write_silent_wave(path, frame_count=frame_count)
        sample_paths.append(path)
        sample_frame_counts.append(frame_count)
    transfer = claim.test_set_transfer
    assert transfer is not None
    sample_evidence = tuple(
        NativeInferencePublishedSample(
            test_set_item_id=item.test_set_item_id,
            item_key=item.item_key,
            sort_order=item.sort_order,
            input_sha256=item.sha256,
            output=_file_evidence(workspace, output),
            output_sample_rate_hz=40_000,
            output_channels=1,
            output_sample_width_bytes=2,
            output_frame_count=frame_count,
            output_duration_seconds=frame_count / 40_000,
            metrics={
                "peak_amplitude": 0.0,
                "rms": 0.0,
                "clipping_ratio": 0.0,
                "silence_ratio": 1.0,
            },
        )
        for item, output, frame_count in zip(
            transfer.items,
            sample_paths,
            sample_frame_counts,
            strict=True,
        )
    )
    publication = NativeInferencePublication(
        manifest=_file_evidence(workspace, manifest),
        job_id=claim.job_id,
        attempt_id=claim.attempt_id,
        test_set_id=transfer.test_set_id,
        family_id=transfer.family_id,
        revision=transfer.revision,
        test_set_manifest_sha256=transfer.manifest_sha256,
        sample_plan_sha256=transfer.sample_plan_sha256,
        inference_config_sha256=transfer.inference_config_sha256,
        inference_request_sha256="f" * 64,
        inference_f0_method=transfer.inference_config.inference_f0_method.value,
        metrics_algorithm="pcm-normalized-v2",
        model=_file_evidence(workspace, model),
        index=None,
        rvc_commit_hash=RVC_REVIEWED_COMMIT,
        runtime_image_digest=_RUNTIME_IMAGE_DIGEST,
        runtime_asset_manifest_sha256=_RUNTIME_ASSET_MANIFEST_SHA256,
        crepe_model=None,
        samples=sample_evidence,
    )
    return claim, workspace, publication


def _file_evidence(
    workspace: JobWorkspace,
    path: Path,
) -> NativeInferencePublishedFile:
    return NativeInferencePublishedFile(
        path=path,
        workspace_relative_path=path.relative_to(workspace.root).as_posix(),
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
    )


def _write_silent_wave(path: Path, *, frame_count: int) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(40_000)
        audio.writeframes(b"\x00\x00" * frame_count)


def _published_artifact(
    claim: JobClaim,
    candidate: ArtifactUploadCandidate,
    *,
    sequence: int,
) -> PublishedArtifact:
    return PublishedArtifact(
        id=_uuid(2, sequence),
        job_id=claim.job_id,
        attempt_id=claim.attempt_id,
        artifact_type=candidate.artifact_type,
        filename=candidate.path.name,
        size_bytes=candidate.size_bytes,
        sha256=candidate.sha256,
        mime_type=candidate.content_type,
        metadata_json={
            **candidate.metadata,
            "manager_verification": {
                "algorithm": "sha256",
                "bounded_stream": True,
                "upload_session_id": f"upload-{sequence}",
                "storage_backend": "local",
            },
        },
        created_at=datetime.now(UTC),
    )


def _registration_request() -> SampleRegistrationRequest:
    return SampleRegistrationRequest(
        lease_id=_LEASE_ID,
        attempt_id=_ATTEMPT_ID,
        test_set_id=_TEST_SET_ID,
        test_set_item_id=_ITEM_IDS[0],
        artifact_id=_uuid(2, 1),
        sample_plan_sha256="e" * 64,
        input_sha256="1" * 64,
        model_sha256="2" * 64,
        index_sha256=None,
        inference_f0_method="rmvpe",
        inference_config_sha256="3" * 64,
        native_inference_manifest_sha256="5" * 64,
        native_inference_request_sha256="6" * 64,
        output_size_bytes=1_644,
        output_sha256="4" * 64,
        output_sample_rate_hz=40_000,
        output_channels=1,
        output_duration_seconds=0.02,
        metrics=SampleMetricValues(
            peak_amplitude=0.0,
            rms=0.0,
            clipping_ratio=0.0,
            silence_ratio=1.0,
        ),
        rvc_commit_hash=RVC_REVIEWED_COMMIT,
        runtime_image_digest=_RUNTIME_IMAGE_DIGEST,
        runtime_asset_manifest_sha256=_RUNTIME_ASSET_MANIFEST_SHA256,
    )


def _sample_response(
    job_id: str,
    request: SampleRegistrationRequest,
    *,
    sequence: int,
) -> SampleRead:
    return SampleRead(
        id=_uuid(3, sequence),
        job_id=job_id,
        attempt_id=request.attempt_id,
        test_set_id=request.test_set_id,
        test_set_item_id=request.test_set_item_id,
        artifact_id=request.artifact_id,
        input_sha256=request.input_sha256,
        model_sha256=request.model_sha256,
        index_sha256=request.index_sha256,
        inference_f0_method=request.inference_f0_method,
        inference_config_sha256=request.inference_config_sha256,
        native_inference_manifest_sha256=request.native_inference_manifest_sha256,
        native_inference_request_sha256=request.native_inference_request_sha256,
        output_size_bytes=request.output_size_bytes,
        output_sha256=request.output_sha256,
        output_sample_rate_hz=request.output_sample_rate_hz,
        output_channels=request.output_channels,
        output_duration_seconds=request.output_duration_seconds,
        metrics=SampleMetricsEvidence(
            worker_reported=request.metrics,
            manager_computed=request.metrics,
            worker_reported_duration_seconds=request.output_duration_seconds,
            manager_computed_sample_rate_hz=request.output_sample_rate_hz,
            manager_computed_channels=request.output_channels,
            manager_computed_duration_seconds=request.output_duration_seconds,
        ),
        rvc_commit_hash=request.rvc_commit_hash,
        runtime_image_digest=request.runtime_image_digest,
        runtime_asset_manifest_sha256=request.runtime_asset_manifest_sha256,
        created_at=datetime.now(UTC),
    )


def _publication_agent(
    root: Path,
    claim: JobClaim,
    manager: _PublicationManager,
    loader: _PublicationLoader,
    *,
    max_attempts: int,
) -> WorkerAgent:
    settings = WorkerSettings(
        manager_url="https://manager.example",
        worker_name="gpu-01",
        worker_token="bootstrap-secret",
        data_root=root,
        runner_mode="native",
        min_free_disk_bytes=0,
        artifact_upload_max_attempts=max_attempts,
    )
    agent = WorkerAgent(
        settings,
        manager,
        _NativeRunner(loader),
        gpu_collector=_NoGpuCollector(),
    )
    cancellation = asyncio.Event()
    agent.active_job = ActiveJob(
        claim=claim,
        cancellation=cancellation,
        lease_expires_at=claim.lease_expires_at,
        lease_deadline=asyncio.get_running_loop().time() + 300,
        lease_changed=asyncio.Event(),
        claim_runtime_image_digest=loader.publication.runtime_image_digest,
        claim_runtime_asset_manifest_sha256=(loader.publication.runtime_asset_manifest_sha256),
    )
    return agent


async def _no_wait(events: tuple[asyncio.Event, ...], delay: float) -> None:
    del events, delay


def _uuid(namespace: int, sequence: int) -> str:
    return f"{namespace:08d}-0000-4000-8000-{sequence:012d}"


if __name__ == "__main__":
    unittest.main()
