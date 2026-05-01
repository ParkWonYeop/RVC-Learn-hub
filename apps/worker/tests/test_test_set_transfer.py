from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import stat
import struct
import threading
import wave
from pathlib import Path

import httpx
import pytest

from rvc_orchestrator_contracts import (
    InferencePresetConfig,
    JobClaim,
    JobStatus,
)
from rvc_orchestrator_contracts import (
    TestSetTransfer as TransferDescriptor,
)
from rvc_orchestrator_contracts import (
    TestSetTransferItem as TransferItemDescriptor,
)
from rvc_worker import client as client_module
from rvc_worker.client import (
    HttpManagerClient,
)
from rvc_worker.client import (
    TestSetTransferCancelled as TransferCancelled,
)
from rvc_worker.client import (
    TestSetTransferError as TransferError,
)
from rvc_worker.runner import RvcRunContext, StageResult
from rvc_worker.stages import (
    STAGE_EXECUTION_POLICIES,
    InternalRetryScope,
    StageErrorCategory,
    StageFailureCause,
    classify_stage_exception,
)
from rvc_worker.test_sets import (
    TestSetMaterializationError as MaterializationError,
)
from rvc_worker.test_sets import (
    TestSetMaterializationLimits as MaterializationLimits,
)
from rvc_worker.test_sets import (
    TestSetMaterializer as Materializer,
)
from rvc_worker.test_sets import (
    TestSetStageRunner as StageRunner,
)
from rvc_worker.workspace import WorkspaceManager

from .helpers import make_claim


def pcm_wave(
    *,
    sample_rate: int = 16_000,
    channels: int = 1,
    frames: int = 160,
) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x00\x00" * channels * frames)
    return output.getvalue()


def transfer_claim(contents: list[bytes]) -> JobClaim:
    claim = make_claim(samples=True)
    config = claim.config.auto_inference_samples
    inference = InferencePresetConfig.model_validate(
        config.model_dump(mode="json", exclude={"enabled", "test_set_id"})
    )
    canonical = json.dumps(
        inference.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    items = [
        TransferItemDescriptor(
            test_set_item_id=f"item-{index}",
            item_key=f"key-{index}",
            sort_order=index,
            download_path=(f"/api/v1/workers/jobs/{claim.job_id}/test-set/items/item-{index}"),
            filename=f"item-{index}.wav",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            sample_rate_hz=16_000,
            channels=1,
            duration_seconds=0.01,
        )
        for index, content in enumerate(contents)
    ]
    transfer = TransferDescriptor(
        test_set_id="fixed-v1",
        family_id="fixed-family",
        revision=7,
        manifest_sha256="a" * 64,
        sample_plan_sha256="b" * 64,
        inference_config=inference,
        inference_config_sha256=hashlib.sha256(canonical).hexdigest(),
        items=items,
    )
    document = claim.model_dump(mode="json")
    document["test_set_transfer"] = transfer.model_dump(mode="json")
    return JobClaim.model_validate(document)


@pytest.mark.asyncio
async def test_http_item_download_is_lease_bound_atomic_and_replay_safe(
    tmp_path: Path,
) -> None:
    content = pcm_wave()
    claim = transfer_claim([content])
    item = claim.test_set_transfer.items[0]  # type: ignore[union-attr]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == item.download_path
        assert request.headers["Authorization"] == "Bearer worker-token"
        assert request.headers["X-RVC-Lease-ID"] == claim.lease_id
        assert request.headers["X-RVC-Attempt-ID"] == claim.attempt_id
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(content)),
                "Content-Type": "audio/wav",
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
    destination = tmp_path / item.filename
    assert await client.download_test_set_item(claim, item, destination) == destination
    assert destination.read_bytes() == content
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.part")))
    assert await client.download_test_set_item(claim, item, destination) == destination
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_http_item_requires_registered_worker_exact_member_and_filename(
    tmp_path: Path,
) -> None:
    content = pcm_wave()
    claim = transfer_claim([content])
    item = claim.test_set_transfer.items[0]  # type: ignore[union-attr]
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    unregistered = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        manager_transport_factory=lambda: httpx.MockTransport(handler),
        object_transport_factory=lambda: httpx.MockTransport(handler),
    )
    with pytest.raises(TransferError, match="register"):
        await unregistered.download_test_set_item(claim, item, tmp_path / item.filename)

    registered = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
        manager_transport_factory=lambda: httpx.MockTransport(handler),
        object_transport_factory=lambda: httpx.MockTransport(handler),
    )
    foreign = item.model_copy(update={"test_set_item_id": "foreign"})
    with pytest.raises(TransferError, match="contain"):
        await registered.download_test_set_item(
            claim,
            foreign,
            tmp_path / foreign.filename,
        )
    with pytest.raises(TransferError, match="filename"):
        await registered.download_test_set_item(
            claim,
            item,
            tmp_path / "wrong.wav",
        )
    assert calls == 0


@pytest.mark.asyncio
async def test_http_redirect_strips_worker_credentials_and_rejects_chain(
    tmp_path: Path,
) -> None:
    content = pcm_wave()
    claim = transfer_claim([content])
    item = claim.test_set_transfer.items[0]  # type: ignore[union-attr]
    external_headers: httpx.Headers | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal external_headers
        if request.url.host == "manager.example":
            return httpx.Response(
                307,
                headers={
                    "Location": "https://objects.example/item?signature=secret",
                    "Set-Cookie": "manager-session=secret; Domain=.example; Secure",
                },
            )
        external_headers = request.headers
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(content)),
                "Content-Type": "audio/wav",
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
    await client.download_test_set_item(claim, item, tmp_path / item.filename)
    assert external_headers is not None
    assert "Authorization" not in external_headers
    assert "X-RVC-Lease-ID" not in external_headers
    assert "X-RVC-Attempt-ID" not in external_headers
    assert "Cookie" not in external_headers
    assert "Proxy-Authorization" not in external_headers

    def chain(request: httpx.Request) -> httpx.Response:
        if request.url.host == "manager.example":
            return httpx.Response(
                307,
                headers={"Location": "https://objects.example/first"},
            )
        return httpx.Response(307, headers={"Location": "https://objects.example/next"})

    chained = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
        manager_transport_factory=lambda: httpx.MockTransport(chain),
        object_transport_factory=lambda: httpx.MockTransport(chain),
    )
    with pytest.raises(TransferError, match="chains"):
        await chained.download_test_set_item(claim, item, tmp_path / "chain" / item.filename)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "http://objects.example/item",
        "https://user:password@objects.example/item",
        "https://objects.example/item#fragment",
        "/relative/item",
    ],
)
async def test_http_item_unsafe_redirect_is_rejected(
    tmp_path: Path,
    location: str,
) -> None:
    content = pcm_wave()
    claim = transfer_claim([content])
    item = claim.test_set_transfer.items[0]  # type: ignore[union-attr]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"Location": location})

    client = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
        manager_transport_factory=lambda: httpx.MockTransport(handler),
        object_transport_factory=lambda: httpx.MockTransport(handler),
    )
    destination = tmp_path / item.filename
    with pytest.raises(TransferError):
        await client.download_test_set_item(claim, item, destination)
    assert not destination.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["length", "mime", "checksum", "encoding"])
async def test_http_item_response_metadata_and_checksum_are_exact(
    tmp_path: Path,
    fault: str,
) -> None:
    content = pcm_wave()
    claim = transfer_claim([content])
    item = claim.test_set_transfer.items[0]  # type: ignore[union-attr]

    def handler(_: httpx.Request) -> httpx.Response:
        headers = {
            "Content-Length": str(len(content)),
            "Content-Type": "audio/wav",
        }
        body = content
        if fault == "length":
            headers["Content-Length"] = str(len(content) + 1)
        elif fault == "mime":
            headers["Content-Type"] = "application/octet-stream"
        elif fault == "checksum":
            body = content[:-1] + bytes([content[-1] ^ 1])
        else:
            headers["Content-Encoding"] = "gzip"
        return httpx.Response(200, headers=headers, content=body)

    client = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
        manager_transport_factory=lambda: httpx.MockTransport(handler),
        object_transport_factory=lambda: httpx.MockTransport(handler),
    )
    destination = tmp_path / item.filename
    with pytest.raises(TransferError):
        await client.download_test_set_item(claim, item, destination)
    assert not destination.exists()
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.part")))


@pytest.mark.asyncio
async def test_http_item_symlink_destination_is_rejected(tmp_path: Path) -> None:
    content = pcm_wave()
    claim = transfer_claim([content])
    item = claim.test_set_transfer.items[0]  # type: ignore[union-attr]
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"do-not-touch")
    destination = tmp_path / item.filename
    destination.symlink_to(outside)

    client = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
        manager_transport_factory=lambda: httpx.MockTransport(lambda _: httpx.Response(500)),
        object_transport_factory=lambda: httpx.MockTransport(lambda _: httpx.Response(500)),
    )
    with pytest.raises(TransferError, match="unsafe"):
        await client.download_test_set_item(claim, item, destination)
    assert destination.is_symlink()
    assert outside.read_bytes() == b"do-not-touch"


class SlowTestSetStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        yield self.content[: len(self.content) // 2]
        self.started.set()
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed.set()


class TrickleTestSetStream(httpx.AsyncByteStream):
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
async def test_http_item_absolute_timeout_stops_slow_trickle_and_cleans_partial(
    tmp_path: Path,
) -> None:
    content = pcm_wave(frames=1_000)
    claim = transfer_claim([content])
    item = claim.test_set_transfer.items[0]  # type: ignore[union-attr]
    stream = TrickleTestSetStream(content)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(content)),
                "Content-Type": "audio/wav",
            },
            stream=stream,
        )

    client = HttpManagerClient(
        "https://manager.example",
        "bootstrap",
        worker_token="worker-token",
        test_set_download_timeout_seconds=0.15,
        manager_transport_factory=lambda: httpx.MockTransport(handler),
    )
    destination = tmp_path / item.filename
    with pytest.raises(TransferError, match="timed out") as captured:
        await client.download_test_set_item(claim, item, destination)

    assert captured.value.retryable is True
    assert captured.value.category == "transport"
    await asyncio.wait_for(stream.closed.wait(), timeout=1)
    assert not destination.exists()
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.part")))
    state = await asyncio.to_thread(lambda: tuple(tmp_path.iterdir()))
    await asyncio.sleep(0.08)
    assert await asyncio.to_thread(lambda: tuple(tmp_path.iterdir())) == state


@pytest.mark.asyncio
async def test_http_item_cancellation_closes_and_removes_partial(tmp_path: Path) -> None:
    content = pcm_wave(frames=1_000)
    claim = transfer_claim([content])
    item = claim.test_set_transfer.items[0]  # type: ignore[union-attr]
    stream = SlowTestSetStream(content)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(content)),
                "Content-Type": "audio/wav",
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
    destination = tmp_path / item.filename
    task = asyncio.create_task(
        client.download_test_set_item(
            claim,
            item,
            destination,
            cancellation=cancellation,
        )
    )
    await asyncio.wait_for(stream.started.wait(), timeout=2)
    cancellation.set()
    with pytest.raises(TransferCancelled):
        await asyncio.wait_for(task, timeout=2)
    await asyncio.wait_for(stream.closed.wait(), timeout=2)
    assert not destination.exists()
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.part")))


@pytest.mark.asyncio
async def test_http_item_pre_cancel_has_no_request_or_filesystem_side_effect(
    tmp_path: Path,
) -> None:
    content = pcm_wave()
    claim = transfer_claim([content])
    item = claim.test_set_transfer.items[0]  # type: ignore[union-attr]
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
    destination = tmp_path / "not-created" / item.filename

    with pytest.raises(TransferCancelled):
        await client.download_test_set_item(
            claim,
            item,
            destination,
            cancellation=cancellation,
        )

    assert calls == 0
    assert not destination.parent.exists()


@pytest.mark.asyncio
async def test_http_item_cancel_waits_for_blocked_write_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = pcm_wave(frames=1_000)
    claim = transfer_claim([content])
    item = claim.test_set_transfer.items[0]  # type: ignore[union-attr]
    write_started = threading.Event()
    release_write = threading.Event()
    original_write = client_module._write_all

    def blocked_write(descriptor: int, chunk: bytes) -> None:
        write_started.set()
        if not release_write.wait(timeout=2):
            raise AssertionError("blocked TestSet write was not released")
        original_write(descriptor, chunk)

    monkeypatch.setattr(client_module, "_write_all", blocked_write)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(content)),
                "Content-Type": "audio/wav",
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
    destination = tmp_path / item.filename
    task = asyncio.create_task(
        client.download_test_set_item(
            claim,
            item,
            destination,
            cancellation=cancellation,
        )
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(write_started.wait, 1), timeout=2)
        cancellation.set()
        await asyncio.sleep(0.02)
        assert not task.done()
    finally:
        release_write.set()

    with pytest.raises(TransferCancelled):
        await asyncio.wait_for(task, timeout=2)
    assert not destination.exists()
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.part")))


class MemoryTestSetManager:
    def __init__(self, contents: dict[str, bytes]) -> None:
        self.contents = contents
        self.calls: list[str] = []
        self.failures = 0
        self.retryable = True

    async def download_test_set_item(
        self,
        claim,
        item,
        destination,
        *,
        cancellation=None,
    ):
        del claim, cancellation
        self.calls.append(item.test_set_item_id)
        if self.failures:
            self.failures -= 1
            raise TransferError(
                "secret object URL",
                status_code=503 if self.retryable else 409,
                retryable=self.retryable,
            )
        destination.write_bytes(self.contents[item.test_set_item_id])
        os.chmod(destination, 0o600)
        return destination


def materializer_context(tmp_path: Path, contents: list[bytes]):
    claim = transfer_claim(contents)
    workspace = WorkspaceManager(tmp_path / "jobs").prepare(
        claim.job_id,
        claim.attempt_id,
    )
    manager = MemoryTestSetManager(
        {f"item-{index}": content for index, content in enumerate(contents)}
    )
    return claim, workspace, manager, RvcRunContext(claim, workspace)


@pytest.mark.asyncio
async def test_materializer_publishes_ordered_directory_and_revalidates_replay(
    tmp_path: Path,
) -> None:
    contents = [pcm_wave(), pcm_wave(frames=320)]
    claim, workspace, manager, context = materializer_context(tmp_path, contents)
    second = claim.test_set_transfer.items[1]  # type: ignore[union-attr]
    transfer_data = claim.test_set_transfer.model_dump(mode="json")  # type: ignore[union-attr]
    transfer_data["items"][1]["duration_seconds"] = 0.02
    claim_data = claim.model_dump(mode="json")
    claim_data["test_set_transfer"] = transfer_data
    claim = JobClaim.model_validate(claim_data)
    context = RvcRunContext(claim, workspace)
    assert second.filename == "item-1.wav"

    materializer = Materializer(manager)  # type: ignore[arg-type]
    result = await materializer.materialize(context, asyncio.Event())
    expected = [workspace.inputs / "test_set" / f"item-{index}.wav" for index in range(2)]
    assert list(result.created_paths[:2]) == expected
    assert [path.read_bytes() for path in expected] == contents
    assert manager.calls == ["item-0", "item-1"]
    assert not list(workspace.inputs.glob(".test_set.*.partial"))
    marker = json.loads((workspace.outputs / "test_set_transfer.json").read_text(encoding="utf-8"))
    assert marker["sample_plan_sha256"] == "b" * 64
    assert marker["sample_plan_revalidation"] == "manager_claim_snapshot"
    assert marker["inference_config"]["inference_f0_method"] == "rmvpe"
    assert marker["items"][1]["duration_seconds"] == 0.02
    marker_text = json.dumps(marker, sort_keys=True)
    assert "download_path" not in marker_text
    assert "/api/" not in marker_text

    replay = await materializer.materialize(context, asyncio.Event())
    assert replay.created_paths == result.created_paths
    assert manager.calls == ["item-0", "item-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["extra", "corrupt", "symlink", "stale"])
async def test_materializer_replay_rejects_extra_corrupt_symlink_and_stale(
    tmp_path: Path,
    attack: str,
) -> None:
    contents = [pcm_wave()]
    _, workspace, manager, context = materializer_context(tmp_path, contents)
    materializer = Materializer(manager)  # type: ignore[arg-type]
    await materializer.materialize(context, asyncio.Event())
    destination = workspace.inputs / "test_set"
    item = destination / "item-0.wav"
    if attack == "extra":
        (destination / "extra.wav").write_bytes(b"extra")
    elif attack == "corrupt":
        item.write_bytes(b"corrupt")
        os.chmod(item, 0o600)
    elif attack == "symlink":
        outside = tmp_path / "outside.wav"
        outside.write_bytes(contents[0])
        item.unlink()
        item.symlink_to(outside)
    else:
        (workspace.inputs / ".test_set.abandoned.partial").mkdir()
    with pytest.raises(MaterializationError):
        await materializer.materialize(context, asyncio.Event())


@pytest.mark.asyncio
async def test_materializer_rejects_destination_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    contents = [pcm_wave()]
    _, workspace, manager, context = materializer_context(tmp_path, contents)
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = workspace.inputs / "test_set"
    destination.symlink_to(outside, target_is_directory=True)
    with pytest.raises(MaterializationError):
        await Materializer(manager).materialize(  # type: ignore[arg-type]
            context,
            asyncio.Event(),
        )
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_kind", ["item", "count", "total", "duration"])
async def test_materializer_enforces_preflight_limits_before_download(
    tmp_path: Path,
    limit_kind: str,
) -> None:
    contents = [pcm_wave()] if limit_kind == "item" else [pcm_wave(), pcm_wave()]
    _, _, manager, context = materializer_context(tmp_path, contents)
    item_size = len(contents[0])
    limits = {
        "item": MaterializationLimits(max_item_bytes=44, max_total_bytes=44),
        "count": MaterializationLimits(max_items=1),
        "total": MaterializationLimits(
            max_item_bytes=item_size,
            max_total_bytes=item_size * 2 - 1,
        ),
        "duration": MaterializationLimits(
            max_duration_seconds=0.01,
            max_total_duration_seconds=0.015,
        ),
    }
    materializer = Materializer(
        manager,  # type: ignore[arg-type]
        limits=limits[limit_kind],
    )
    with pytest.raises(MaterializationError):
        await materializer.materialize(context, asyncio.Event())
    assert manager.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_kind",
    ["not_riff", "compressed", "truncated", "metadata"],
)
async def test_materializer_rejects_invalid_pcm_wave_and_descriptor_metadata(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    if invalid_kind == "not_riff":
        content = b"not-a-riff-wave".ljust(44, b"\x00")
    elif invalid_kind == "compressed":
        samples = b"\x00"
        body = (
            b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 6, 1, 8_000, 8_000, 1, 8)
            + b"data"
            + struct.pack("<I", len(samples))
            + samples
        )
        content = b"RIFF" + struct.pack("<I", len(body)) + body
    elif invalid_kind == "truncated":
        content = pcm_wave()[:-2]
    else:
        content = pcm_wave(sample_rate=8_000, frames=80)
    _, workspace, manager, context = materializer_context(tmp_path, [content])
    with pytest.raises(MaterializationError):
        await Materializer(manager).materialize(  # type: ignore[arg-type]
            context,
            asyncio.Event(),
        )
    assert not (workspace.inputs / "test_set").exists()
    assert not await asyncio.to_thread(lambda: list(workspace.inputs.glob(".test_set.*.partial")))


class ImmediateBackoffEvent(asyncio.Event):
    async def wait(self) -> bool:
        raise TimeoutError


@pytest.mark.asyncio
async def test_materializer_retries_only_transient_item_transfer(tmp_path: Path) -> None:
    contents = [pcm_wave()]
    _, _, manager, context = materializer_context(tmp_path, contents)
    manager.failures = 2
    materializer = Materializer(manager)  # type: ignore[arg-type]
    await materializer.materialize(context, ImmediateBackoffEvent())
    assert manager.calls == ["item-0", "item-0", "item-0"]

    other = tmp_path / "other"
    _, _, manager, context = materializer_context(other, contents)
    manager.failures = 2
    manager.retryable = False
    with pytest.raises(MaterializationError):
        await Materializer(manager).materialize(  # type: ignore[arg-type]
            context,
            ImmediateBackoffEvent(),
        )
    assert manager.calls == ["item-0"]


class DelegateRunner:
    def __init__(self) -> None:
        self.calls: list[JobStatus] = []

    async def run_stage(self, stage, context, cancellation):
        del context, cancellation
        self.calls.append(stage)
        return StageResult(metadata={"delegate": stage.value})


class HangingTestSetManager(MemoryTestSetManager):
    def __init__(self, contents: dict[str, bytes]) -> None:
        super().__init__(contents)
        self.started = asyncio.Event()
        self.finished = asyncio.Event()

    async def download_test_set_item(
        self,
        claim,
        item,
        destination,
        *,
        cancellation=None,
    ):
        del claim, cancellation
        self.calls.append(item.test_set_item_id)
        destination.write_bytes(b"partial")
        os.chmod(destination, 0o600)
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.finished.set()


@pytest.mark.asyncio
async def test_materialization_absolute_timeout_joins_and_removes_staging(
    tmp_path: Path,
) -> None:
    contents = [pcm_wave()]
    claim, workspace, _, context = materializer_context(tmp_path, contents)
    manager = HangingTestSetManager({"item-0": contents[0]})
    materializer = Materializer(
        manager,  # type: ignore[arg-type]
        limits=MaterializationLimits(materialization_timeout_seconds=0.05),
    )

    with pytest.raises(MaterializationError, match="timed out"):
        await materializer.materialize(context, asyncio.Event())

    assert manager.started.is_set()
    assert manager.finished.is_set()
    assert not (workspace.inputs / "test_set").exists()
    assert not list(workspace.inputs.glob(".test_set.*.partial"))
    state = tuple(workspace.inputs.iterdir())
    await asyncio.sleep(0.05)
    assert tuple(workspace.inputs.iterdir()) == state
    assert claim.test_set_transfer is not None


@pytest.mark.asyncio
async def test_stage_wrapper_preflights_test_set_before_dataset_delegate_or_download(
    tmp_path: Path,
) -> None:
    contents = [pcm_wave(), pcm_wave()]
    _, _, manager, context = materializer_context(tmp_path, contents)
    delegate = DelegateRunner()
    runner = StageRunner(
        delegate,  # type: ignore[arg-type]
        Materializer(
            manager,  # type: ignore[arg-type]
            limits=MaterializationLimits(
                max_duration_seconds=0.01,
                max_total_duration_seconds=0.015,
            ),
        ),
    )

    with pytest.raises(MaterializationError, match="total-duration"):
        await runner.run_stage(
            JobStatus.DOWNLOADING_DATASET,
            context,
            asyncio.Event(),
        )

    assert delegate.calls == []
    assert manager.calls == []


@pytest.mark.asyncio
async def test_stage_wrapper_materializes_only_at_dataset_receipt(tmp_path: Path) -> None:
    contents = [pcm_wave()]
    _, workspace, manager, context = materializer_context(tmp_path, contents)
    delegate = DelegateRunner()
    runner = StageRunner(
        delegate,  # type: ignore[arg-type]
        Materializer(manager),  # type: ignore[arg-type]
    )
    result = await runner.run_stage(
        JobStatus.DOWNLOADING_DATASET,
        context,
        asyncio.Event(),
    )
    assert result.metadata is not None
    assert result.metadata["delegate"] == JobStatus.DOWNLOADING_DATASET.value
    assert result.metadata["test_set"]["item_count"] == 1
    await runner.run_stage(JobStatus.VALIDATING_DATASET, context, asyncio.Event())
    assert manager.calls == ["item-0"]
    assert delegate.calls == [
        JobStatus.DOWNLOADING_DATASET,
        JobStatus.VALIDATING_DATASET,
    ]


def test_test_set_errors_have_stable_integrity_classification_and_retry_scope() -> None:
    classified = classify_stage_exception(
        JobStatus.DOWNLOADING_DATASET,
        MaterializationError("Bearer secret at https://objects.example/item?signature=secret"),
    )
    assert classified.error_code == "stage_integrity_failed"
    assert classified.category is StageErrorCategory.INTEGRITY
    assert classified.cause is StageFailureCause.TEST_SET_INTEGRITY
    assert "secret" not in classified.safe_message
    assert (
        InternalRetryScope.TEST_SET_TRANSFER
        in STAGE_EXECUTION_POLICIES[JobStatus.DOWNLOADING_DATASET].internal_retry_scopes
    )
