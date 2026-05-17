from __future__ import annotations

import asyncio
import hashlib
import io
import os
import stat
import threading
import warnings
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from rvc_orchestrator_contracts import DatasetTransfer, JobStatus
from rvc_worker import client as client_module
from rvc_worker.client import (
    DatasetTransferCancelled,
    DatasetTransferError,
    HttpManagerClient,
)
from rvc_worker.datasets import (
    DatasetMaterializationError,
    DatasetMaterializationLimits,
    DatasetMaterializer,
    DatasetStageRunner,
    inspect_prepared_flat_archive,
    materialize_prepared_flat_archive,
)
from rvc_worker.runner import RvcRunContext, StageResult
from rvc_worker.stages import StageErrorCategory, StageExecutionError, StageExecutor
from rvc_worker.workspace import WorkspaceManager

from .helpers import make_claim


def canonical_zip(entries: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in entries:
            info = zipfile.ZipInfo(name)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            info.create_system = 3
            archive.writestr(info, content)
    return output.getvalue()


def transfer_claim(content: bytes):
    claim = make_claim()
    return claim.model_copy(
        update={
            "dataset_transfer": DatasetTransfer(
                dataset_id=claim.config.dataset_id,
                download_path=f"/api/v1/workers/jobs/{claim.job_id}/dataset",
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        }
    )


class HostBoundTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        expected_host: str,
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> None:
        self.expected_host = expected_host
        self.handler = handler
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == self.expected_host
        return self.handler(request)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_manager_stream_download_is_exact_atomic_and_retry_safe(tmp_path: Path) -> None:
    content = canonical_zip([("prepared_flat/000001.wav", b"RIFF-safe-audio")])
    claim = transfer_claim(content)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer worker-token"
        assert request.headers["X-RVC-Lease-ID"] == claim.lease_id
        assert request.headers["X-RVC-Attempt-ID"] == claim.attempt_id
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(content)),
                "Content-Type": "application/zip",
            },
            content=content,
        )

    client = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
        manager_transport_factory=lambda: httpx.MockTransport(handler),
        object_transport_factory=lambda: httpx.MockTransport(handler),
    )
    destination = tmp_path / "inputs" / "prepared_flat.zip"
    assert await client.download_dataset(claim, destination) == destination
    assert destination.read_bytes() == content
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not list(destination.parent.glob("*.part"))
    # A replay verifies the existing regular file and performs no network request.
    assert await client.download_dataset(claim, destination) == destination
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_external_redirect_never_receives_worker_authorization(tmp_path: Path) -> None:
    content = canonical_zip([("prepared_flat/000001.wav", b"RIFF-safe-audio")])
    claim = transfer_claim(content)
    seen_external = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_external
        if request.url.host == "manager.example":
            assert request.headers["Authorization"] == "Bearer worker-token"
            return httpx.Response(
                307,
                headers={
                    "Location": "https://objects.example/bucket/key?signature=secret",
                    "Set-Cookie": "manager-session=secret; Domain=.example; Secure",
                },
            )
        seen_external = True
        assert request.url.host == "objects.example"
        assert "Authorization" not in request.headers
        assert "X-RVC-Lease-ID" not in request.headers
        assert "X-RVC-Attempt-ID" not in request.headers
        assert "Cookie" not in request.headers
        assert "Proxy-Authorization" not in request.headers
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(content)),
                "Content-Type": "application/zip",
            },
            content=content,
        )

    client = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
        manager_transport_factory=lambda: httpx.MockTransport(handler),
        object_transport_factory=lambda: httpx.MockTransport(handler),
    )
    destination = tmp_path / "prepared_flat.zip"
    await client.download_dataset(claim, destination)
    assert seen_external is True
    assert destination.read_bytes() == content


@pytest.mark.asyncio
async def test_manager_and_object_transports_are_role_bound_fresh_and_closed(
    tmp_path: Path,
) -> None:
    content = canonical_zip([("prepared_flat/000001.wav", b"RIFF-safe-audio")])
    claim = transfer_claim(content)
    manager_transports: list[HostBoundTransport] = []
    object_transports: list[HostBoundTransport] = []

    def manager_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer worker-token"
        return httpx.Response(
            307,
            headers={"Location": "https://objects.example/bucket/object?signature=secret"},
        )

    def object_handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers
        assert "Proxy-Authorization" not in request.headers
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(content)),
                "Content-Type": "application/zip",
            },
            content=content,
        )

    def manager_factory() -> httpx.AsyncBaseTransport:
        transport = HostBoundTransport("manager.example", manager_handler)
        manager_transports.append(transport)
        return transport

    def object_factory() -> httpx.AsyncBaseTransport:
        transport = HostBoundTransport("objects.example", object_handler)
        object_transports.append(transport)
        return transport

    client = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
        manager_transport_factory=manager_factory,
        object_transport_factory=object_factory,
    )
    for index in range(2):
        await client.download_dataset(claim, tmp_path / f"prepared-{index}.zip")

    assert len(manager_transports) == 2
    assert len(object_transports) == 2
    assert manager_transports[0] is not manager_transports[1]
    assert object_transports[0] is not object_transports[1]
    assert all(transport.closed for transport in (*manager_transports, *object_transports))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "http://objects.example/key?signature=secret",
        "https://user:password@objects.example/key",
        "https://objects.example/key#fragment",
        "/relative/object/key",
    ],
)
async def test_unsafe_redirect_is_rejected_without_partial_file(
    tmp_path: Path,
    location: str,
) -> None:
    content = canonical_zip([("prepared_flat/000001.wav", b"RIFF-audio")])
    claim = transfer_claim(content)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"Location": location})

    client = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
        manager_transport_factory=lambda: httpx.MockTransport(handler),
        object_transport_factory=lambda: httpx.MockTransport(handler),
    )
    destination = tmp_path / "prepared_flat.zip"
    with pytest.raises(DatasetTransferError):
        await client.download_dataset(claim, destination)
    assert not destination.exists()
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.part")))


@pytest.mark.asyncio
async def test_redirect_chain_and_corrupt_body_fail_closed(tmp_path: Path) -> None:
    content = canonical_zip([("prepared_flat/000001.wav", b"RIFF-audio")])
    claim = transfer_claim(content)

    def chain(request: httpx.Request) -> httpx.Response:
        if request.url.host == "manager.example":
            return httpx.Response(307, headers={"Location": "https://objects.example/one"})
        return httpx.Response(302, headers={"Location": "https://objects.example/two"})

    destination = tmp_path / "chain.zip"
    client = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
        manager_transport_factory=lambda: httpx.MockTransport(chain),
        object_transport_factory=lambda: httpx.MockTransport(chain),
    )
    with pytest.raises(DatasetTransferError, match="chains"):
        await client.download_dataset(claim, destination)
    assert not destination.exists()

    corrupt = bytearray(content)
    corrupt[-1] ^= 0x01

    def mismatch(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(corrupt)),
                "Content-Type": "application/zip",
            },
            content=bytes(corrupt),
        )

    client = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
        manager_transport_factory=lambda: httpx.MockTransport(mismatch),
        object_transport_factory=lambda: httpx.MockTransport(mismatch),
    )
    destination = tmp_path / "corrupt.zip"
    with pytest.raises(DatasetTransferError, match="checksum"):
        await client.download_dataset(claim, destination)
    assert not destination.exists()
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.part")))


@pytest.mark.asyncio
async def test_content_length_and_symlink_destination_are_rejected(tmp_path: Path) -> None:
    content = canonical_zip([("prepared_flat/000001.wav", b"RIFF-audio")])
    claim = transfer_claim(content)

    def oversized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(content) + 1),
                "Content-Type": "application/zip",
            },
            content=content,
        )

    client = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
        manager_transport_factory=lambda: httpx.MockTransport(oversized),
        object_transport_factory=lambda: httpx.MockTransport(oversized),
    )
    destination = tmp_path / "oversized.zip"
    with pytest.raises(DatasetTransferError, match="Content-Length"):
        await client.download_dataset(claim, destination)
    assert not destination.exists()

    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"do-not-touch")
    destination = tmp_path / "symlink.zip"
    destination.symlink_to(outside)
    with pytest.raises(DatasetTransferError, match="unsafe"):
        await client.download_dataset(claim, destination)
    assert outside.read_bytes() == b"do-not-touch"
    assert destination.is_symlink()


class SlowDatasetStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        midpoint = max(1, len(self.content) // 2)
        yield self.content[:midpoint]
        self.started.set()
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed.set()


class TrickleDatasetStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.closed = asyncio.Event()

    async def __aiter__(self):
        for byte in self.content:
            await asyncio.sleep(0.02)
            yield bytes((byte,))

    async def aclose(self) -> None:
        self.closed.set()


@pytest.mark.asyncio
async def test_download_absolute_timeout_stops_slow_trickle_and_cleans_partial(
    tmp_path: Path,
) -> None:
    content = canonical_zip([("prepared_flat/000001.wav", b"RIFF" + b"a" * 100)])
    claim = transfer_claim(content)
    stream = TrickleDatasetStream(content)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(content)),
                "Content-Type": "application/zip",
            },
            stream=stream,
        )

    client = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
        dataset_download_timeout_seconds=0.15,
        manager_transport_factory=lambda: httpx.MockTransport(handler),
    )
    destination = tmp_path / "prepared_flat.zip"
    with pytest.raises(DatasetTransferError, match="timed out") as captured:
        await client.download_dataset(claim, destination)

    assert captured.value.retryable is True
    assert captured.value.category == "transport"
    await asyncio.wait_for(stream.closed.wait(), timeout=1)
    assert not destination.exists()
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.part")))
    state = await asyncio.to_thread(lambda: tuple(tmp_path.iterdir()))
    await asyncio.sleep(0.08)
    assert await asyncio.to_thread(lambda: tuple(tmp_path.iterdir())) == state


@pytest.mark.asyncio
async def test_download_cancellation_closes_stream_and_removes_partial(tmp_path: Path) -> None:
    content = canonical_zip([("prepared_flat/000001.wav", b"RIFF" + b"a" * 100)])
    claim = transfer_claim(content)
    stream = SlowDatasetStream(content)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(content)),
                "Content-Type": "application/zip",
            },
            stream=stream,
        )

    client = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
        manager_transport_factory=lambda: httpx.MockTransport(handler),
        object_transport_factory=lambda: httpx.MockTransport(handler),
    )
    cancellation = asyncio.Event()
    destination = tmp_path / "prepared_flat.zip"
    task = asyncio.create_task(
        client.download_dataset(claim, destination, cancellation=cancellation)
    )
    await asyncio.wait_for(stream.started.wait(), timeout=2)
    cancellation.set()
    with pytest.raises(DatasetTransferCancelled):
        await asyncio.wait_for(task, timeout=2)
    await asyncio.wait_for(stream.closed.wait(), timeout=2)
    assert not destination.exists()
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.part")))


@pytest.mark.asyncio
async def test_dataset_pre_cancel_has_no_request_or_filesystem_side_effect(
    tmp_path: Path,
) -> None:
    content = canonical_zip([("prepared_flat/000001.wav", b"RIFF-safe")])
    claim = transfer_claim(content)
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    client = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
        manager_transport_factory=lambda: httpx.MockTransport(handler),
        object_transport_factory=lambda: httpx.MockTransport(handler),
    )
    cancellation = asyncio.Event()
    cancellation.set()
    destination = tmp_path / "not-created" / "prepared_flat.zip"

    with pytest.raises(DatasetTransferCancelled):
        await client.download_dataset(
            claim,
            destination,
            cancellation=cancellation,
        )

    assert calls == 0
    assert not destination.parent.exists()


@pytest.mark.asyncio
async def test_dataset_cancel_waits_for_blocked_publish_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = canonical_zip([("prepared_flat/000001.wav", b"RIFF-safe")])
    claim = transfer_claim(content)
    publish_started = threading.Event()
    release_publish = threading.Event()
    original_publish = client_module._publish_dataset_partial

    def blocked_publish(*args):
        publish_started.set()
        if not release_publish.wait(timeout=2):
            raise AssertionError("blocked Dataset publish was not released")
        return original_publish(*args)

    monkeypatch.setattr(client_module, "_publish_dataset_partial", blocked_publish)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(content)),
                "Content-Type": "application/zip",
            },
            content=content,
        )

    client = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
        manager_transport_factory=lambda: httpx.MockTransport(handler),
        object_transport_factory=lambda: httpx.MockTransport(handler),
    )
    cancellation = asyncio.Event()
    destination = tmp_path / "prepared_flat.zip"
    task = asyncio.create_task(
        client.download_dataset(
            claim,
            destination,
            cancellation=cancellation,
        )
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(publish_started.wait, 1), timeout=2)
        cancellation.set()
        await asyncio.sleep(0.02)
        assert not task.done()
    finally:
        release_publish.set()

    with pytest.raises(DatasetTransferCancelled):
        await asyncio.wait_for(task, timeout=2)
    assert destination.read_bytes() == content
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.part")))
    await asyncio.sleep(0.02)
    assert destination.read_bytes() == content


def test_safe_archive_is_revalidated_and_materialized_flat(tmp_path: Path) -> None:
    content = canonical_zip(
        [
            ("prepared_flat/000001.wav", b"RIFF-one"),
            ("prepared_flat/000002.wav", b"RIFF-two"),
        ]
    )
    archive = tmp_path / "prepared_flat.zip"
    archive.write_bytes(content)
    checksum = hashlib.sha256(content).hexdigest()
    limits = DatasetMaterializationLimits(max_total_bytes=1024, max_file_bytes=512)
    inspection = inspect_prepared_flat_archive(
        archive,
        expected_size=len(content),
        expected_sha256=checksum,
        limits=limits,
    )
    assert [entry.filename for entry in inspection.entries] == ["000001.wav", "000002.wav"]
    destination = tmp_path / "inputs" / "prepared_flat"
    files = materialize_prepared_flat_archive(
        archive,
        destination,
        expected_size=len(content),
        expected_sha256=checksum,
        limits=limits,
    )
    assert [path.name for path in files] == ["000001.wav", "000002.wav"]
    assert [path.read_bytes() for path in files] == [b"RIFF-one", b"RIFF-two"]
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)
    # A repeated materialization verifies the existing directory rather than replacing it.
    assert (
        materialize_prepared_flat_archive(
            archive,
            destination,
            expected_size=len(content),
            expected_sha256=checksum,
            limits=limits,
        )
        == files
    )
    assert not list(destination.parent.glob("*.partial"))


@pytest.mark.parametrize("kind", ["traversal", "symlink", "duplicate", "bomb", "corrupt"])
def test_archive_attacks_are_rejected_without_materialized_output(
    tmp_path: Path,
    kind: str,
) -> None:
    if kind == "traversal":
        content = canonical_zip([("../escape.wav", b"RIFF")])
    elif kind == "symlink":
        output = io.BytesIO()
        with zipfile.ZipFile(output, mode="w") as archive:
            info = zipfile.ZipInfo("prepared_flat/000001.wav")
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, b"../../outside")
        content = output.getvalue()
    elif kind == "duplicate":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            content = canonical_zip(
                [
                    ("prepared_flat/000001.wav", b"RIFF-one"),
                    ("prepared_flat/000001.wav", b"RIFF-two"),
                ]
            )
    elif kind == "bomb":
        output = io.BytesIO()
        with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            info = zipfile.ZipInfo("prepared_flat/000001.wav")
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, b"0" * 100_000)
        content = output.getvalue()
    else:
        valid = canonical_zip([("prepared_flat/000001.wav", b"RIFF-unique-payload")])
        corrupt = bytearray(valid)
        offset = valid.index(b"RIFF-unique-payload")
        corrupt[offset] ^= 0x01
        content = bytes(corrupt)
    archive_path = tmp_path / f"{kind}.zip"
    archive_path.write_bytes(content)
    destination = tmp_path / "prepared_flat"
    with pytest.raises(DatasetMaterializationError):
        materialize_prepared_flat_archive(
            archive_path,
            destination,
            expected_size=len(content),
            expected_sha256=hashlib.sha256(content).hexdigest(),
            limits=DatasetMaterializationLimits(
                max_total_bytes=10_000 if kind == "bomb" else 1024**2,
                max_file_bytes=10_000 if kind == "bomb" else 1024**2,
                max_compression_ratio=10,
            ),
        )
    assert not destination.exists()
    assert not (tmp_path / "escape.wav").exists()
    assert not list(tmp_path.glob("*.partial"))


class MemoryDatasetManager:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def download_dataset(self, claim, destination, *, cancellation=None):
        del claim, cancellation
        destination.write_bytes(self.content)
        os.chmod(destination, 0o600)
        return destination


class FailingDatasetManager:
    def __init__(self, status_code: int | None) -> None:
        self.status_code = status_code
        self.attempts = 0

    async def download_dataset(self, claim, destination, *, cancellation=None):
        del claim, destination, cancellation
        self.attempts += 1
        raise DatasetTransferError(
            "Dataset transport exposed https://objects.example/key?token=secret",
            status_code=self.status_code,
        )


class UnusedDatasetRunner:
    async def run_stage(self, stage, context, cancellation):
        del stage, context, cancellation
        return StageResult()


class ImmediateBackoffEvent(asyncio.Event):
    async def wait(self) -> bool:
        raise TimeoutError


@pytest.mark.asyncio
async def test_materializer_owns_real_dataset_stages(tmp_path: Path) -> None:
    content = canonical_zip([("prepared_flat/000001.wav", b"RIFF-stage")])
    claim = transfer_claim(content)
    workspace = WorkspaceManager(tmp_path / "jobs").prepare(claim.job_id, claim.attempt_id)
    context = RvcRunContext(claim, workspace)
    materializer = DatasetMaterializer(
        MemoryDatasetManager(content),  # type: ignore[arg-type]
        limits=DatasetMaterializationLimits(max_total_bytes=1024, max_file_bytes=1024),
    )
    cancellation = asyncio.Event()
    downloaded = await materializer.run_stage(JobStatus.DOWNLOADING_DATASET, context, cancellation)
    validated = await materializer.run_stage(JobStatus.VALIDATING_DATASET, context, cancellation)
    prepared = await materializer.run_stage(
        JobStatus.PREPARING_FLAT_DATASET,
        context,
        cancellation,
    )
    assert workspace.inputs / "prepared_flat.zip" in downloaded.created_paths
    assert workspace.outputs / "dataset_report.json" in validated.created_paths
    assert [path.name for path in prepared.created_paths] == ["000001.wav"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_attempts", "error_code", "category"),
    [
        (503, 3, "exhausted_transient", StageErrorCategory.TRANSIENT),
        (None, 1, "stage_integrity_failed", StageErrorCategory.INTEGRITY),
    ],
)
async def test_dataset_retry_is_bounded_and_integrity_is_never_retried(
    tmp_path: Path,
    status_code: int | None,
    expected_attempts: int,
    error_code: str,
    category: StageErrorCategory,
) -> None:
    content = canonical_zip([("prepared_flat/000001.wav", b"RIFF-stage")])
    claim = transfer_claim(content)
    workspace = WorkspaceManager(tmp_path / "jobs").prepare(
        claim.job_id,
        claim.attempt_id,
    )
    manager = FailingDatasetManager(status_code)
    runner = DatasetStageRunner(
        UnusedDatasetRunner(),  # type: ignore[arg-type]
        DatasetMaterializer(
            manager,  # type: ignore[arg-type]
            limits=DatasetMaterializationLimits(
                max_total_bytes=1024,
                max_file_bytes=1024,
                download_attempts=3,
            ),
        ),
    )

    async def update_status(job_id: str, update: object) -> None:
        del job_id, update

    with pytest.raises(StageExecutionError) as raised:
        await StageExecutor(runner, update_status).execute(
            claim,
            workspace,
            ImmediateBackoffEvent(),
        )

    assert manager.attempts == expected_attempts
    assert raised.value.stage is JobStatus.DOWNLOADING_DATASET
    assert raised.value.error_code == error_code
    assert raised.value.category is category
    assert "objects.example" not in raised.value.safe_message
    assert "secret" not in raised.value.safe_message
