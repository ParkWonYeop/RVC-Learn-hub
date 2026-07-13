from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from rvc_orchestrator_contracts import ArtifactType
from rvc_worker.client import (
    ArtifactTransferCancelled,
    HttpManagerClient,
    ManagerClientError,
)
from rvc_worker.runner import FakeRvcRunner, RvcRunContext
from rvc_worker.stages import build_stage_plan
from rvc_worker.uploads import (
    ArtifactUploadInitResponse,
    PublishedArtifact,
    collect_artifact_candidates,
)
from rvc_worker.workspace import WorkspaceManager

from .helpers import make_claim


class ArtifactPutTests(unittest.IsolatedAsyncioTestCase):
    async def test_put_streams_exact_file_with_manager_supplied_binding_headers(self) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "model.pth"
            source.write_bytes(b"model-payload")
            captured: dict[str, object] = {}

            async def handle(request: httpx.Request) -> httpx.Response:
                captured["body"] = await request.aread()
                captured["headers"] = dict(request.headers)
                return httpx.Response(204)

            client = HttpManagerClient(
                "https://manager.example",
                "bootstrap",
                worker_token="worker",
                artifact_upload_timeout_seconds=123.0,
                object_transport_factory=lambda: httpx.MockTransport(handle),
            )
            headers = {
                "Content-Type": "application/x-pytorch",
                "Content-Length": str(source.stat().st_size),
                "If-None-Match": "*",
                "x-amz-meta-sha256": "a" * 64,
            }

            await client._put_file(  # noqa: SLF001 - security boundary unit test
                "https://objects.example/upload?X-Amz-Signature=secret",
                headers,
                source,
                source.stat().st_size,
                "application/x-pytorch",
            )

            observed_headers = captured["headers"]
            assert isinstance(observed_headers, dict)
            self.assertEqual(captured["body"], b"model-payload")
            self.assertEqual(observed_headers["content-length"], str(len(b"model-payload")))
            self.assertEqual(observed_headers["content-type"], "application/x-pytorch")
            self.assertEqual(observed_headers["if-none-match"], "*")
            self.assertNotIn("authorization", observed_headers)

    async def test_artifact_manager_and_object_requests_use_distinct_factories(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "model.pth"
            source.write_bytes(b"model")
            claim = make_claim(build_index=False)
            request = _candidate_request(claim, source)
            pending = ArtifactUploadInitResponse(
                upload_session_id="upload-1",
                status="pending",
                method="PUT",
                upload_url="https://objects.example/upload",
                upload_headers={
                    "Content-Type": request.content_type,
                    "Content-Length": str(request.size_bytes),
                },
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            manager_calls = 0
            object_calls = 0

            async def manager_handler(http_request: httpx.Request) -> httpx.Response:
                nonlocal manager_calls
                manager_calls += 1
                self.assertEqual(http_request.url.host, "manager.example")
                self.assertEqual(
                    http_request.headers["Authorization"],
                    "Bearer worker",
                )
                return httpx.Response(200, json=pending.model_dump(mode="json"))

            async def object_handler(http_request: httpx.Request) -> httpx.Response:
                nonlocal object_calls
                object_calls += 1
                self.assertEqual(http_request.url.host, "objects.example")
                self.assertNotIn("Authorization", http_request.headers)
                self.assertNotIn("Cookie", http_request.headers)
                self.assertNotIn("Proxy-Authorization", http_request.headers)
                self.assertEqual(await http_request.aread(), b"model")
                return httpx.Response(204)

            client = HttpManagerClient(
                "https://manager.example",
                "bootstrap",
                worker_token="worker",
                manager_transport_factory=lambda: httpx.MockTransport(manager_handler),
                object_transport_factory=lambda: httpx.MockTransport(object_handler),
            )
            initialized = await client._artifact_request(  # noqa: SLF001
                "POST",
                "/api/v1/workers/jobs/job-1/artifact-uploads/init",
                request,
                ArtifactUploadInitResponse,
                timeout_seconds=1,
            )
            await client._put_file(  # noqa: SLF001
                initialized.upload_url or "",
                initialized.upload_headers,
                source,
                source.stat().st_size,
                request.content_type,
            )

            self.assertEqual(manager_calls, 1)
            self.assertEqual(object_calls, 1)

    async def test_put_rejects_downgrade_dangerous_headers_and_changed_source(self) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "index.bin"
            source.write_bytes(b"index")
            client = HttpManagerClient(
                "https://manager.example", "bootstrap", worker_token="worker"
            )
            valid = {
                "Content-Type": "application/octet-stream",
                "Content-Length": "5",
            }
            with self.assertRaisesRegex(ManagerClientError, "unsafe artifact upload URL"):
                await client._put_file(  # noqa: SLF001
                    "https://user:password@objects.example/upload",
                    valid,
                    source,
                    5,
                    "application/octet-stream",
                )
            with self.assertRaisesRegex(ManagerClientError, "unsafe artifact upload URL"):
                await client._put_file(  # noqa: SLF001
                    "http://objects.example/upload",
                    valid,
                    source,
                    5,
                    "application/octet-stream",
                )
            with self.assertRaisesRegex(ManagerClientError, "unsafe artifact upload headers"):
                await client._put_file(  # noqa: SLF001
                    "https://objects.example/upload",
                    {**valid, "Authorization": "Bearer must-not-forward"},
                    source,
                    5,
                    "application/octet-stream",
                )
            with self.assertRaisesRegex(ManagerClientError, "unsafe artifact upload headers"):
                await client._put_file(  # noqa: SLF001
                    "https://objects.example/upload",
                    {**valid, "If-None-Match": '"untrusted-etag"'},
                    source,
                    5,
                    "application/octet-stream",
                )
            with self.assertRaisesRegex(ManagerClientError, "source changed"):
                await client._put_file(  # noqa: SLF001
                    "https://objects.example/upload",
                    {**valid, "Content-Length": "6"},
                    source,
                    6,
                    "application/octet-stream",
                )

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "platform has no O_NOFOLLOW")
    async def test_put_refuses_symbolic_link_source(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_bytes(b"data")
            source = root / "link"
            source.symlink_to(target)
            client = HttpManagerClient(
                "https://manager.example", "bootstrap", worker_token="worker"
            )
            with self.assertRaisesRegex(ManagerClientError, "opened safely"):
                await client._put_file(  # noqa: SLF001
                    "https://objects.example/upload",
                    {"Content-Type": "application/octet-stream", "Content-Length": "4"},
                    source,
                    4,
                    "application/octet-stream",
                )


class ArtifactCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_fake_outputs_become_verified_upload_candidates(self) -> None:
        with TemporaryDirectory() as temporary:
            claim = make_claim(samples=True)
            workspace = WorkspaceManager(Path(temporary)).prepare(claim.job_id, claim.attempt_id)
            runner = FakeRvcRunner()
            context = RvcRunContext(claim, workspace)
            for stage in build_stage_plan(claim):
                await runner.run_stage(stage, context, asyncio.Event())

            candidates = collect_artifact_candidates(claim, workspace, is_fake=True)
            by_type = {candidate.artifact_type for candidate in candidates}

            self.assertIn(ArtifactType.FINAL_SMALL_MODEL, by_type)
            self.assertIn(ArtifactType.FINAL_INDEX, by_type)
            self.assertIn(ArtifactType.GENERATOR_CHECKPOINT, by_type)
            self.assertIn(ArtifactType.DISCRIMINATOR_CHECKPOINT, by_type)
            self.assertIn(ArtifactType.SAMPLE, by_type)
            self.assertTrue(all(candidate.size_bytes > 0 for candidate in candidates))
            self.assertTrue(all(len(candidate.sha256) == 64 for candidate in candidates))
            self.assertTrue(all(not candidate.path.is_symlink() for candidate in candidates))
            model = next(
                item for item in candidates if item.artifact_type is ArtifactType.FINAL_SMALL_MODEL
            )
            self.assertEqual(model.content_type, "application/x-pytorch")
            request = model.init_request(claim)
            self.assertNotIn("storage_uri", request.model_dump())
            self.assertEqual(request.metadata["runner_fake"], True)

    async def test_empty_artifact_is_rejected_before_network_upload(self) -> None:
        with TemporaryDirectory() as temporary:
            claim = make_claim(build_index=False)
            workspace = WorkspaceManager(Path(temporary)).prepare(claim.job_id, claim.attempt_id)
            model = workspace.outputs / "model/final_small_model.pth"
            model.parent.mkdir(parents=True, exist_ok=True)
            model.touch()

            with self.assertRaisesRegex(RuntimeError, "artifact is empty"):
                collect_artifact_candidates(claim, workspace, is_fake=False)

    async def test_checkpoint_retention_keeps_latest_epochs_and_enforces_quotas(self) -> None:
        with TemporaryDirectory() as temporary:
            claim = make_claim(build_index=False)
            workspace = WorkspaceManager(Path(temporary)).prepare(claim.job_id, claim.attempt_id)
            model = workspace.outputs / "model/final_small_model.pth"
            model.parent.mkdir(parents=True, exist_ok=True)
            model.write_bytes(b"model")
            logs = workspace.work / "rvc/logs" / claim.config.job_name
            logs.mkdir(parents=True)
            for epoch in range(1, 26):
                (logs / f"G_{epoch}.pth").write_bytes(f"G-{epoch}".encode())
                (logs / f"D_{epoch}.pth").write_bytes(f"D-{epoch}".encode())

            candidates = collect_artifact_candidates(
                claim,
                workspace,
                is_fake=False,
                checkpoint_retention=2,
            )
            checkpoint_names = {
                item.path.name
                for item in candidates
                if item.artifact_type
                in {
                    ArtifactType.GENERATOR_CHECKPOINT,
                    ArtifactType.DISCRIMINATOR_CHECKPOINT,
                }
            }
            self.assertEqual(
                checkpoint_names,
                {"G_24.pth", "G_25.pth", "D_24.pth", "D_25.pth"},
            )

            with self.assertRaisesRegex(RuntimeError, "file count"):
                collect_artifact_candidates(
                    claim,
                    workspace,
                    is_fake=False,
                    max_files=4,
                    checkpoint_retention=2,
                )
            with self.assertRaisesRegex(RuntimeError, "object size"):
                collect_artifact_candidates(
                    claim,
                    workspace,
                    is_fake=False,
                    max_object_bytes=4,
                    checkpoint_retention=1,
                )
            with self.assertRaisesRegex(RuntimeError, "artifact bytes"):
                collect_artifact_candidates(
                    claim,
                    workspace,
                    is_fake=False,
                    max_total_bytes=10,
                    checkpoint_retention=1,
                )


class ArtifactPublishSequenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_put_finalize_sequence_and_completed_replay(self) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "model.pth"
            source.write_bytes(b"model")
            claim = make_claim(build_index=False)
            candidate_request = _candidate_request(claim, source)
            artifact = _published_artifact(claim, candidate_request)
            pending = ArtifactUploadInitResponse(
                upload_session_id="upload-1",
                status="pending",
                method="PUT",
                upload_url="https://objects.example/upload",
                upload_headers={
                    "Content-Type": candidate_request.content_type,
                    "Content-Length": str(candidate_request.size_bytes),
                },
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            client = _ScriptedClient([pending, artifact])

            result = await client.publish_artifact(claim.job_id, candidate_request, source)

            self.assertEqual(result.id, "artifact-1")
            self.assertEqual([call[0] for call in client.calls], ["POST", "POST"])
            self.assertIn("artifact-uploads/init", client.calls[0][1])
            self.assertIn("upload-1/finalize", client.calls[1][1])
            self.assertEqual(client.puts, [(source, b"model")])

            completed = ArtifactUploadInitResponse(
                upload_session_id="upload-1",
                status="completed",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
                artifact=artifact,
            )
            replay = _ScriptedClient([completed])
            replayed = await replay.publish_artifact(claim.job_id, candidate_request, source)
            self.assertEqual(replayed.id, artifact.id)
            self.assertFalse(replay.puts)

    async def test_cancellation_closes_in_flight_put(self) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "model.pth"
            source.write_bytes(b"model")
            claim = make_claim(build_index=False)
            request = _candidate_request(claim, source)
            pending = ArtifactUploadInitResponse(
                upload_session_id="upload-cancel",
                status="pending",
                method="PUT",
                upload_url="https://objects.example/upload",
                upload_headers={
                    "Content-Type": request.content_type,
                    "Content-Length": str(request.size_bytes),
                },
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            client = _ScriptedClient([pending], block_put=True)
            cancellation = asyncio.Event()
            task = asyncio.create_task(
                client.publish_artifact(
                    claim.job_id,
                    request,
                    source,
                    cancellation=cancellation,
                )
            )
            await asyncio.wait_for(client.put_started.wait(), timeout=1)

            cancellation.set()

            with self.assertRaises(ArtifactTransferCancelled):
                await asyncio.wait_for(task, timeout=1)
            self.assertTrue(client.put_cancelled)

    async def test_ambiguous_put_acknowledgement_proceeds_to_manager_finalize(self) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "model.pth"
            source.write_bytes(b"model")
            claim = make_claim(build_index=False)
            request = _candidate_request(claim, source)
            pending = ArtifactUploadInitResponse(
                upload_session_id="upload-ambiguous",
                status="pending",
                method="PUT",
                upload_url="https://objects.example/upload",
                upload_headers={
                    "Content-Type": request.content_type,
                    "Content-Length": str(request.size_bytes),
                    "If-None-Match": "*",
                },
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            artifact = _published_artifact(claim, request)
            for put_error in (
                ManagerClientError(
                    "response lost",
                    retryable=True,
                    category="transport",
                ),
                ManagerClientError("already sealed", status_code=409),
                ManagerClientError("precondition failed", status_code=412),
            ):
                client = _ScriptedClient([pending, artifact], put_error=put_error)
                result = await client.publish_artifact(claim.job_id, request, source)
                self.assertEqual(result.id, artifact.id)
                self.assertEqual([call[0] for call in client.calls], ["POST", "POST"])

    async def test_ambiguous_put_without_object_remains_bounded_retryable(self) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "model.pth"
            source.write_bytes(b"model")
            claim = make_claim(build_index=False)
            request = _candidate_request(claim, source)
            pending = ArtifactUploadInitResponse(
                upload_session_id="upload-missing",
                status="pending",
                method="PUT",
                upload_url="https://objects.example/upload",
                upload_headers={
                    "Content-Type": request.content_type,
                    "Content-Length": str(request.size_bytes),
                },
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            client = _ScriptedClient(
                [
                    pending,
                    ManagerClientError("object not found", status_code=409),
                    pending,
                ],
                put_error=ManagerClientError(
                    "response lost",
                    retryable=True,
                    category="transport",
                ),
            )
            with self.assertRaises(ManagerClientError) as raised:
                await client.publish_artifact(claim.job_id, request, source)
            self.assertTrue(raised.exception.retryable)
            self.assertIn("acknowledgement is ambiguous", str(raised.exception))


class _ScriptedClient(HttpManagerClient):
    def __init__(self, responses, *, block_put: bool = False, put_error=None) -> None:
        super().__init__("https://manager.example", "bootstrap", worker_token="worker")
        self.responses = list(responses)
        self.calls = []
        self.puts = []
        self.block_put = block_put
        self.put_error = put_error
        self.put_started = asyncio.Event()
        self.put_cancelled = False

    async def _cancellable_artifact_request(
        self,
        method,
        path,
        body,
        response_model,
        *,
        timeout_seconds,
        cancellation,
    ):
        self.calls.append((method, path, body, response_model, timeout_seconds, cancellation))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def _put_file(self, upload_url, headers, source, expected_size, expected_content_type):
        del upload_url, headers, expected_size, expected_content_type
        self.put_started.set()
        try:
            if self.block_put:
                await asyncio.Event().wait()
            if self.put_error is not None:
                raise self.put_error
            self.puts.append((source, source.read_bytes()))
        except asyncio.CancelledError:
            self.put_cancelled = True
            raise


def _candidate_request(claim, source):
    from rvc_worker.uploads import ArtifactUploadInitRequest

    return ArtifactUploadInitRequest(
        lease_id=claim.lease_id,
        attempt_id=claim.attempt_id,
        idempotency_key="artifact-key-0001",
        artifact_type=ArtifactType.FINAL_SMALL_MODEL,
        filename=source.name,
        content_type="application/x-pytorch",
        size_bytes=source.stat().st_size,
        sha256="a" * 64,
    )


def _published_artifact(claim, request):
    return PublishedArtifact(
        id="artifact-1",
        job_id=claim.job_id,
        attempt_id=claim.attempt_id,
        artifact_type=request.artifact_type,
        filename=request.filename,
        size_bytes=request.size_bytes,
        sha256=request.sha256,
        mime_type=request.content_type,
        metadata_json={},
        created_at=datetime.now(UTC),
    )


if __name__ == "__main__":
    unittest.main()
