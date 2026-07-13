"""Typed Manager HTTP client boundary used by the worker agent."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import ssl
import stat
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

import httpx

from rvc_orchestrator_contracts import (
    ContractModel,
    JobClaim,
    JobClaimRequest,
    JobStatusUpdate,
    LeaseRenewRequest,
    LeaseRenewResponse,
    LogBatch,
    MetricBatch,
    SampleRead,
    SampleRegistrationRequest,
    TestSetTransferItem,
    WorkerHeartbeatRequest,
    WorkerHeartbeatResponse,
    WorkerReEnrollRequest,
    WorkerRegisterRequest,
    WorkerRegisterResponse,
    WorkerSessionResponse,
    WorkerTokenRotationActivated,
    WorkerTokenRotationPrepareResponse,
    WorkerTokenRotationRequest,
    WorkerTokenRotationStatus,
)

from .tls import create_worker_ssl_context
from .uploads import (
    ArtifactUploadFinalizeRequest,
    ArtifactUploadInitRequest,
    ArtifactUploadInitResponse,
    PublishedArtifact,
)

ManagerErrorCategory = Literal["transport", "protocol", "integrity", "configuration"]


class ManagerClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool | None = None,
        category: ManagerErrorCategory | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = _transient_status(status_code) if retryable is None else retryable
        self.category: ManagerErrorCategory = category or (
            "transport" if self.retryable else "protocol"
        )


class ArtifactTransferCancelled(ManagerClientError):
    """Raised after closing an in-flight artifact transfer on cancellation."""


class DatasetTransferError(ManagerClientError):
    """Raised when a canonical Dataset cannot be transferred without ambiguity."""


class DatasetTransferCancelled(DatasetTransferError):
    """Raised after closing a Dataset response and removing its partial file."""


class TestSetTransferError(ManagerClientError):
    """Raised when one immutable TestSet WAV cannot be transferred safely."""


class TestSetTransferCancelled(TestSetTransferError):
    """Raised after closing a TestSet response and removing its partial file."""


class ManagerClient(Protocol):
    async def get_session(self) -> WorkerSessionResponse | None: ...

    async def register(self, request: WorkerRegisterRequest) -> WorkerRegisterResponse: ...

    async def re_enroll(self, request: WorkerReEnrollRequest) -> WorkerRegisterResponse: ...

    def set_worker_token(self, token: str) -> None: ...

    async def get_token_rotation_status(self) -> WorkerTokenRotationStatus: ...

    async def prepare_token_rotation(
        self, request: WorkerTokenRotationRequest
    ) -> WorkerTokenRotationPrepareResponse: ...

    async def activate_token_rotation(
        self,
        request: WorkerTokenRotationRequest,
        *,
        pending_worker_token: str,
    ) -> WorkerTokenRotationActivated: ...

    async def abort_token_rotation(self, request: WorkerTokenRotationRequest) -> None: ...

    async def heartbeat(self, request: WorkerHeartbeatRequest) -> WorkerHeartbeatResponse: ...

    async def claim_job(self, request: JobClaimRequest) -> JobClaim | None: ...

    async def download_dataset(
        self,
        claim: JobClaim,
        destination: Path,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> Path: ...

    async def download_test_set_item(
        self,
        claim: JobClaim,
        item: TestSetTransferItem,
        destination: Path,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> Path: ...

    async def renew_lease(self, job_id: str, request: LeaseRenewRequest) -> LeaseRenewResponse: ...

    async def update_status(self, job_id: str, update: JobStatusUpdate) -> None: ...

    async def send_logs(self, job_id: str, batch: LogBatch) -> None: ...

    async def send_metrics(self, job_id: str, batch: MetricBatch) -> None: ...

    async def publish_artifact(
        self,
        job_id: str,
        request: ArtifactUploadInitRequest,
        source: Path,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> PublishedArtifact: ...

    async def register_sample(
        self,
        job_id: str,
        request: SampleRegistrationRequest,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> SampleRead: ...


ModelT = TypeVar("ModelT", bound=ContractModel)
ResultT = TypeVar("ResultT")
HttpTransportFactory = Callable[[], httpx.AsyncBaseTransport]


def _transport_factory(context: ssl.SSLContext) -> HttpTransportFactory:
    def create() -> httpx.AsyncBaseTransport:
        return httpx.AsyncHTTPTransport(verify=context, trust_env=False)

    return create


class HttpManagerClient:
    """Manager client with cancellable async streams for large artifact transfers."""

    def __init__(
        self,
        base_url: str,
        bootstrap_token: str,
        *,
        worker_token: str | None = None,
        timeout_seconds: float = 30.0,
        artifact_upload_timeout_seconds: float = 3600.0,
        dataset_download_timeout_seconds: float = 3600.0,
        dataset_max_bytes: int = 5 * 1024**3,
        test_set_download_timeout_seconds: float = 3600.0,
        test_set_max_item_bytes: int = 256 * 1024**2,
        ca_bundle_path: Path | None = None,
        user_agent: str = "rvc-orchestrator-worker/0.1.0",
        manager_transport_factory: HttpTransportFactory | None = None,
        object_transport_factory: HttpTransportFactory | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._bootstrap_token = bootstrap_token
        self._worker_token = worker_token
        self.timeout_seconds = timeout_seconds
        self.artifact_upload_timeout_seconds = artifact_upload_timeout_seconds
        self.dataset_download_timeout_seconds = dataset_download_timeout_seconds
        self.dataset_max_bytes = dataset_max_bytes
        self.test_set_download_timeout_seconds = test_set_download_timeout_seconds
        self.test_set_max_item_bytes = test_set_max_item_bytes
        self.user_agent = user_agent
        self._ssl_context = create_worker_ssl_context(ca_bundle_path)
        self._manager_transport_factory = manager_transport_factory or _transport_factory(
            self._ssl_context
        )
        self._object_transport_factory = object_transport_factory or _transport_factory(
            self._ssl_context
        )

    async def register(self, request: WorkerRegisterRequest) -> WorkerRegisterResponse:
        response = await self._send_model(
            "POST",
            "/api/v1/workers/register",
            request,
            WorkerRegisterResponse,
            bootstrap=True,
        )
        assert response is not None
        self._worker_token = response.worker_token
        return response

    async def re_enroll(self, request: WorkerReEnrollRequest) -> WorkerRegisterResponse:
        response = await self._send_model(
            "POST",
            "/api/v1/workers/re-enroll",
            request,
            WorkerRegisterResponse,
            bootstrap=True,
        )
        assert response is not None
        self._worker_token = response.worker_token
        return response

    def set_worker_token(self, token: str) -> None:
        if not token:
            raise ValueError("Worker token must not be empty")
        self._worker_token = token

    async def get_token_rotation_status(self) -> WorkerTokenRotationStatus:
        response = await self._send_model(
            "GET",
            "/api/v1/workers/token-rotation",
            None,
            WorkerTokenRotationStatus,
        )
        assert response is not None
        return response

    async def prepare_token_rotation(
        self,
        request: WorkerTokenRotationRequest,
    ) -> WorkerTokenRotationPrepareResponse:
        response = await self._send_model(
            "POST",
            "/api/v1/workers/token-rotation/prepare",
            request,
            WorkerTokenRotationPrepareResponse,
        )
        assert response is not None
        return response

    async def activate_token_rotation(
        self,
        request: WorkerTokenRotationRequest,
        *,
        pending_worker_token: str,
    ) -> WorkerTokenRotationActivated:
        response = await self._send_model(
            "POST",
            "/api/v1/workers/token-rotation/activate",
            request,
            WorkerTokenRotationActivated,
            pending_worker_token=pending_worker_token,
        )
        assert response is not None
        return response

    async def abort_token_rotation(self, request: WorkerTokenRotationRequest) -> None:
        response = await self._send_model(
            "POST",
            "/api/v1/workers/token-rotation/abort",
            request,
            WorkerTokenRotationStatus,
        )
        assert response is not None

    async def get_session(self) -> WorkerSessionResponse | None:
        if self._worker_token is None:
            return None
        response = await self._send_model("GET", "/api/v1/workers/me", None, WorkerSessionResponse)
        assert response is not None
        return response

    async def heartbeat(self, request: WorkerHeartbeatRequest) -> WorkerHeartbeatResponse:
        response = await self._send_model(
            "POST",
            "/api/v1/workers/heartbeat",
            request,
            WorkerHeartbeatResponse,
        )
        assert response is not None
        return response

    async def claim_job(self, request: JobClaimRequest) -> JobClaim | None:
        return await self._send_model(
            "POST", "/api/v1/workers/jobs/claim", request, JobClaim, allow_no_content=True
        )

    async def download_dataset(
        self,
        claim: JobClaim,
        destination: Path,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> Path:
        transfer = claim.dataset_transfer
        if transfer is None:
            raise DatasetTransferError("Manager claim has no verified Dataset transfer")
        if transfer.size_bytes > self.dataset_max_bytes:
            raise DatasetTransferError("Dataset archive exceeds the Worker transfer limit")
        try:
            return await asyncio.wait_for(
                _await_with_cancellation(
                    self._download_dataset(claim, destination),
                    cancellation,
                    cancelled_error=DatasetTransferCancelled,
                ),
                timeout=self.dataset_download_timeout_seconds,
            )
        except DatasetTransferCancelled:
            raise
        except DatasetTransferError:
            raise
        except TimeoutError as exc:
            raise DatasetTransferError(
                "Dataset download timed out",
                retryable=True,
                category="transport",
            ) from exc
        except httpx.HTTPError as exc:
            raise DatasetTransferError(
                f"Dataset download failed: {type(exc).__name__}",
                retryable=True,
                category="transport",
            ) from exc

    async def _download_dataset(self, claim: JobClaim, destination: Path) -> Path:
        transfer = claim.dataset_transfer
        assert transfer is not None
        manager_url = f"{self.base_url}{transfer.download_path}"
        if self._worker_token is None:
            raise DatasetTransferError("worker must register before Dataset download")
        if await asyncio.to_thread(
            _existing_dataset_is_valid,
            destination,
            transfer.size_bytes,
            transfer.sha256,
        ):
            return destination
        manager_headers = {
            "Accept": transfer.content_type,
            "Authorization": f"Bearer {self._worker_token}",
            "X-RVC-Lease-ID": claim.lease_id,
            "X-RVC-Attempt-ID": claim.attempt_id,
            "User-Agent": self.user_agent,
        }
        redirect_url: str | None = None
        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(self.dataset_download_timeout_seconds),
                transport=self._manager_transport_factory(),
                trust_env=False,
            ) as client:
                async with client.stream(
                    "GET",
                    manager_url,
                    headers=manager_headers,
                ) as response:
                    if response.status_code == 307:
                        redirect_url = _validated_dataset_redirect(
                            response.headers.get("Location"),
                            manager_base_url=self.base_url,
                        )
                    elif response.status_code == 200:
                        await _stream_dataset_response(
                            response,
                            destination,
                            expected_size=transfer.size_bytes,
                            expected_sha256=transfer.sha256,
                            expected_content_type=transfer.content_type,
                        )
                        return destination
                    else:
                        raise DatasetTransferError(
                            f"Manager Dataset request failed with HTTP {response.status_code}",
                            status_code=response.status_code,
                        )
            assert redirect_url is not None
            # A fresh client prevents response cookies and Manager credentials from
            # crossing into the short-lived external object request. Environment
            # proxy credentials are intentionally disabled for the same reason.
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(self.dataset_download_timeout_seconds),
                transport=self._object_transport_factory(),
                trust_env=False,
            ) as object_client:
                async with object_client.stream(
                    "GET",
                    redirect_url,
                    headers={
                        "Accept": transfer.content_type,
                        "User-Agent": self.user_agent,
                    },
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise DatasetTransferError("Dataset redirect chains are forbidden")
                    if response.status_code != 200:
                        raise DatasetTransferError(
                            f"Dataset object request failed with HTTP {response.status_code}",
                            status_code=response.status_code,
                        )
                    await _stream_dataset_response(
                        response,
                        destination,
                        expected_size=transfer.size_bytes,
                        expected_sha256=transfer.sha256,
                        expected_content_type=transfer.content_type,
                    )
                    return destination
        except DatasetTransferError:
            raise

    async def download_test_set_item(
        self,
        claim: JobClaim,
        item: TestSetTransferItem,
        destination: Path,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> Path:
        transfer = claim.test_set_transfer
        if transfer is None or item not in transfer.items:
            raise TestSetTransferError("Manager claim does not contain the requested TestSet item")
        if item.size_bytes > self.test_set_max_item_bytes:
            raise TestSetTransferError("TestSet item exceeds the Worker transfer limit")
        if destination.name != item.filename:
            raise TestSetTransferError("TestSet destination filename does not match the claim")
        try:
            return await asyncio.wait_for(
                _await_with_cancellation(
                    self._download_test_set_item(claim, item, destination),
                    cancellation,
                    cancelled_error=TestSetTransferCancelled,
                ),
                timeout=self.test_set_download_timeout_seconds,
            )
        except TestSetTransferCancelled:
            raise
        except TestSetTransferError:
            raise
        except TimeoutError as exc:
            raise TestSetTransferError(
                "TestSet item download timed out",
                retryable=True,
                category="transport",
            ) from exc
        except httpx.HTTPError as exc:
            raise TestSetTransferError(
                f"TestSet item download failed: {type(exc).__name__}",
                retryable=True,
                category="transport",
            ) from exc

    async def _download_test_set_item(
        self,
        claim: JobClaim,
        item: TestSetTransferItem,
        destination: Path,
    ) -> Path:
        if self._worker_token is None:
            raise TestSetTransferError("worker must register before TestSet download")
        if await asyncio.to_thread(
            _existing_test_set_item_is_valid,
            destination,
            item.size_bytes,
            item.sha256,
        ):
            return destination
        manager_url = f"{self.base_url}{item.download_path}"
        manager_headers = {
            "Accept": item.content_type,
            "Authorization": f"Bearer {self._worker_token}",
            "X-RVC-Lease-ID": claim.lease_id,
            "X-RVC-Attempt-ID": claim.attempt_id,
            "User-Agent": self.user_agent,
        }
        redirect_url: str | None = None
        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(self.test_set_download_timeout_seconds),
                transport=self._manager_transport_factory(),
                trust_env=False,
            ) as client:
                async with client.stream("GET", manager_url, headers=manager_headers) as response:
                    if response.status_code == 307:
                        redirect_url = _validated_test_set_redirect(
                            response.headers.get("Location"),
                            manager_base_url=self.base_url,
                        )
                    elif response.status_code == 200:
                        await _stream_test_set_response(response, destination, item=item)
                        return destination
                    else:
                        raise TestSetTransferError(
                            f"Manager TestSet request failed with HTTP {response.status_code}",
                            status_code=response.status_code,
                        )
            assert redirect_url is not None
            # A separate, cookie-empty client prevents Manager response state as
            # well as bearer/lease headers from reaching the external object host.
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(self.test_set_download_timeout_seconds),
                transport=self._object_transport_factory(),
                trust_env=False,
            ) as object_client:
                async with object_client.stream(
                    "GET",
                    redirect_url,
                    headers={"Accept": item.content_type, "User-Agent": self.user_agent},
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise TestSetTransferError("TestSet redirect chains are forbidden")
                    if response.status_code != 200:
                        raise TestSetTransferError(
                            f"TestSet object request failed with HTTP {response.status_code}",
                            status_code=response.status_code,
                        )
                    await _stream_test_set_response(response, destination, item=item)
                    return destination
        except TestSetTransferError:
            raise

    async def renew_lease(self, job_id: str, request: LeaseRenewRequest) -> LeaseRenewResponse:
        response = await self._send_model(
            "POST",
            f"/api/v1/workers/jobs/{_path_id(job_id)}/lease/renew",
            request,
            LeaseRenewResponse,
        )
        assert response is not None
        return response

    async def update_status(self, job_id: str, update: JobStatusUpdate) -> None:
        await self._send_model(
            "POST",
            f"/api/v1/workers/jobs/{_path_id(job_id)}/status",
            update,
            None,
        )

    async def send_logs(self, job_id: str, batch: LogBatch) -> None:
        await self._send_model("POST", f"/api/v1/workers/jobs/{_path_id(job_id)}/logs", batch, None)

    async def send_metrics(self, job_id: str, batch: MetricBatch) -> None:
        await self._send_model(
            "POST", f"/api/v1/workers/jobs/{_path_id(job_id)}/metrics", batch, None
        )

    async def publish_artifact(
        self,
        job_id: str,
        request: ArtifactUploadInitRequest,
        source: Path,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> PublishedArtifact:
        safe_job_id = _path_id(job_id)
        init_path = f"/api/v1/workers/jobs/{safe_job_id}/artifact-uploads/init"
        initialized = await self._cancellable_artifact_request(
            "POST",
            init_path,
            request,
            ArtifactUploadInitResponse,
            timeout_seconds=self.timeout_seconds,
            cancellation=cancellation,
        )
        initialized = await self._poll_finalizing_upload(
            init_path,
            request,
            initialized,
            cancellation=cancellation,
        )
        if initialized.status == "completed" and initialized.artifact is not None:
            return initialized.artifact
        if (
            initialized.status != "pending"
            or initialized.method != "PUT"
            or initialized.upload_url is None
        ):
            suffix = f" ({initialized.failure_code})" if initialized.failure_code else ""
            raise ManagerClientError(
                f"Manager artifact upload session is {initialized.status}{suffix}",
                status_code=503 if initialized.retryable else 409,
            )
        # The bound method and arguments are wrapped separately so the file is
        # not opened until the cancellable task has started.

        async def put() -> None:
            await self._put_file(
                initialized.upload_url or "",
                initialized.upload_headers,
                source,
                request.size_bytes,
                request.content_type,
            )

        ambiguous_put_error: ManagerClientError | None = None
        try:
            await _await_with_cancellation(put(), cancellation)
        except ManagerClientError as exc:
            # A transport failure can occur after the object store committed the
            # body, while local replay returns 409 once sealed and S3 conditional
            # replay returns 412. Manager finalize is the authority in all three
            # cases because it re-reads and verifies the whole object.
            if not (exc.retryable or exc.status_code in {409, 412}):
                raise
            ambiguous_put_error = exc
        finalize_path = (
            f"/api/v1/workers/jobs/{safe_job_id}/artifact-uploads/"
            f"{_path_id(initialized.upload_session_id)}/finalize"
        )
        try:
            return await self._cancellable_artifact_request(
                "POST",
                finalize_path,
                ArtifactUploadFinalizeRequest(
                    lease_id=request.lease_id,
                    attempt_id=request.attempt_id,
                ),
                PublishedArtifact,
                timeout_seconds=self.artifact_upload_timeout_seconds,
                cancellation=cancellation,
            )
        except ManagerClientError as finalize_error:
            if not _possibly_in_progress(finalize_error):
                raise
            refreshed = await self._cancellable_artifact_request(
                "POST",
                init_path,
                request,
                ArtifactUploadInitResponse,
                timeout_seconds=self.timeout_seconds,
                cancellation=cancellation,
            )
            refreshed = await self._poll_finalizing_upload(
                init_path,
                request,
                refreshed,
                cancellation=cancellation,
            )
            if refreshed.status == "completed" and refreshed.artifact is not None:
                return refreshed.artifact
            if ambiguous_put_error is not None:
                raise ManagerClientError(
                    "artifact upload acknowledgement is ambiguous",
                    status_code=ambiguous_put_error.status_code,
                    retryable=True,
                    category="transport",
                ) from ambiguous_put_error
            raise finalize_error

    async def register_sample(
        self,
        job_id: str,
        request: SampleRegistrationRequest,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> SampleRead:
        """Register one finalized Sample Artifact; create is 201 and replay is 200."""

        return await self._cancellable_artifact_request(
            "POST",
            f"/api/v1/workers/jobs/{_path_id(job_id)}/samples",
            request,
            SampleRead,
            timeout_seconds=self.timeout_seconds,
            cancellation=cancellation,
            allowed_statuses=frozenset({200, 201}),
        )

    async def _poll_finalizing_upload(
        self,
        init_path: str,
        request: ArtifactUploadInitRequest,
        initialized: ArtifactUploadInitResponse,
        *,
        cancellation: asyncio.Event | None,
    ) -> ArtifactUploadInitResponse:
        deadline = asyncio.get_running_loop().time() + self.artifact_upload_timeout_seconds
        current = initialized
        while current.status == "finalizing":
            if asyncio.get_running_loop().time() >= deadline:
                raise ManagerClientError(
                    "artifact finalization polling timed out",
                    retryable=True,
                    category="transport",
                )
            retry_after = current.retry_after_seconds or 2
            await _await_with_cancellation(
                asyncio.sleep(min(30, max(1, retry_after))),
                cancellation,
            )
            current = await self._cancellable_artifact_request(
                "POST",
                init_path,
                request,
                ArtifactUploadInitResponse,
                timeout_seconds=self.timeout_seconds,
                cancellation=cancellation,
            )
        return current

    async def _put_file(
        self,
        upload_url: str,
        supplied_headers: dict[str, str],
        source: Path,
        expected_size: int,
        expected_content_type: str,
    ) -> None:
        parsed = urlparse(upload_url)
        manager_scheme = urlparse(self.base_url).scheme
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (manager_scheme == "https" and parsed.scheme != "https")
        ):
            raise ManagerClientError(
                "Manager returned an unsafe artifact upload URL",
                category="configuration",
            )
        headers = _validated_upload_headers(
            supplied_headers,
            expected_size=expected_size,
            expected_content_type=expected_content_type,
        )
        headers["user-agent"] = self.user_agent
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source, flags)
        except OSError as exc:
            raise ManagerClientError(
                "artifact source cannot be opened safely",
                category="integrity",
            ) from exc
        try:
            source_stat = os.fstat(descriptor)
            if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size != expected_size:
                raise ManagerClientError(
                    "artifact source changed after upload initialization",
                    category="integrity",
                )

            async def body() -> AsyncIterator[bytes]:
                remaining = expected_size
                while remaining:
                    chunk = await asyncio.to_thread(os.read, descriptor, min(1024**2, remaining))
                    if not chunk:
                        raise ManagerClientError(
                            "artifact source changed during upload",
                            category="integrity",
                        )
                    remaining -= len(chunk)
                    yield chunk
                if await asyncio.to_thread(os.read, descriptor, 1):
                    raise ManagerClientError(
                        "artifact source changed during upload",
                        category="integrity",
                    )

            try:
                async with httpx.AsyncClient(
                    follow_redirects=False,
                    timeout=httpx.Timeout(self.artifact_upload_timeout_seconds),
                    transport=self._object_transport_factory(),
                    trust_env=False,
                ) as client:
                    async with client.stream(
                        "PUT",
                        upload_url,
                        headers=headers,
                        content=body(),
                    ) as response:
                        status_code = response.status_code
            except ManagerClientError:
                raise
            except httpx.HTTPError as exc:
                raise ManagerClientError(
                    f"artifact upload failed: {type(exc).__name__}",
                    retryable=True,
                    category="transport",
                ) from exc
            if status_code not in {200, 201, 204}:
                raise ManagerClientError(
                    f"artifact upload returned unexpected HTTP {status_code}",
                    status_code=status_code,
                )
        finally:
            os.close(descriptor)

    async def _cancellable_artifact_request(
        self,
        method: str,
        path: str,
        body: ContractModel,
        response_model: type[ModelT],
        *,
        timeout_seconds: float,
        cancellation: asyncio.Event | None,
        allowed_statuses: frozenset[int] | None = None,
    ) -> ModelT:
        async def bounded_request() -> ModelT:
            try:
                async with asyncio.timeout(timeout_seconds):
                    return await self._artifact_request(
                        method,
                        path,
                        body,
                        response_model,
                        timeout_seconds=timeout_seconds,
                        allowed_statuses=allowed_statuses,
                    )
            except TimeoutError as exc:
                raise ManagerClientError(
                    "Manager artifact operation deadline exceeded",
                    retryable=True,
                    category="transport",
                ) from exc

        return await _await_with_cancellation(
            bounded_request(),
            cancellation,
        )

    async def _artifact_request(
        self,
        method: str,
        path: str,
        body: ContractModel,
        response_model: type[ModelT],
        *,
        timeout_seconds: float,
        allowed_statuses: frozenset[int] | None = None,
    ) -> ModelT:
        if self._worker_token is None:
            raise ManagerClientError("worker must register before artifact requests")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._worker_token}",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(timeout_seconds),
                transport=self._manager_transport_factory(),
                trust_env=False,
            ) as client:
                async with client.stream(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    content=body.model_dump_json().encode(),
                ) as response:
                    valid_status = (
                        response.status_code in allowed_statuses
                        if allowed_statuses is not None
                        else 200 <= response.status_code < 300
                    )
                    if not valid_status:
                        correlation_id = response.headers.get("X-Request-ID")
                        suffix = f" (correlation {correlation_id})" if correlation_id else ""
                        raise ManagerClientError(
                            f"Manager request failed with HTTP {response.status_code}{suffix}",
                            status_code=response.status_code,
                        )
                    response_body = bytearray()
                    async for chunk in response.aiter_bytes():
                        response_body.extend(chunk)
                        if len(response_body) > 1024**2:
                            raise ManagerClientError("Manager artifact response is too large")
        except ManagerClientError:
            raise
        except httpx.HTTPError as exc:
            raise ManagerClientError(
                f"Manager artifact request failed: {type(exc).__name__}",
                retryable=True,
                category="transport",
            ) from exc
        try:
            return response_model.model_validate_json(response_body)
        except Exception as exc:
            raise ManagerClientError(
                f"Manager returned an invalid {response_model.__name__} response"
            ) from exc

    async def _send_model(
        self,
        method: str,
        path: str,
        body: ContractModel | None,
        response_model: type[ModelT] | None,
        *,
        bootstrap: bool = False,
        allow_no_content: bool = False,
        pending_worker_token: str | None = None,
    ) -> ModelT | None:
        payload = body.model_dump_json().encode("utf-8") if body is not None else None
        status, response_body = await asyncio.to_thread(
            self._request,
            method,
            path,
            payload,
            bootstrap,
            pending_worker_token,
        )
        if allow_no_content and status == 204:
            return None
        if response_model is None or status == 204:
            return None
        try:
            return response_model.model_validate_json(response_body)
        except Exception as exc:
            raise ManagerClientError(
                f"Manager returned an invalid {response_model.__name__} response"
            ) from exc

    def _request(
        self,
        method: str,
        path: str,
        payload: bytes | None,
        bootstrap: bool,
        pending_worker_token: str | None,
    ) -> tuple[int, bytes]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        if bootstrap:
            headers["X-Worker-Bootstrap-Token"] = self._bootstrap_token
        else:
            if self._worker_token is None:
                raise ManagerClientError("worker must register before authenticated requests")
            headers["Authorization"] = f"Bearer {self._worker_token}"
        if pending_worker_token is not None:
            headers["X-RVC-Pending-Worker-Token"] = pending_worker_token
        request = Request(f"{self.base_url}{path}", data=payload, headers=headers, method=method)
        try:
            with build_opener(
                ProxyHandler({}),
                HTTPSHandler(context=self._ssl_context),
                _NoRedirectHandler(),
            ).open(request, timeout=self.timeout_seconds) as response:
                response_body = response.read(1024**2 + 1)
                if len(response_body) > 1024**2:
                    raise ManagerClientError("Manager response is too large")
                return response.status, response_body
        except HTTPError as exc:
            correlation_id = exc.headers.get("X-Request-ID")
            suffix = f" (correlation {correlation_id})" if correlation_id else ""
            # Response bodies may reflect a request and therefore are never copied to logs.
            raise ManagerClientError(
                f"Manager request failed with HTTP {exc.code}{suffix}", status_code=exc.code
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ManagerClientError(
                f"Manager request failed: {type(exc).__name__}",
                retryable=True,
                category="transport",
            ) from exc


def _path_id(value: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
    if value in {"", ".", ".."} or any(character not in allowed for character in value):
        raise ManagerClientError("unsafe Manager resource identifier")
    return value


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


async def _await_with_cancellation(
    awaitable: Awaitable[ResultT],
    cancellation: asyncio.Event | None,
    *,
    cancelled_error: type[ManagerClientError] = ArtifactTransferCancelled,
) -> ResultT:
    if cancellation is None:
        return await awaitable
    if cancellation.is_set():
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise cancelled_error("transfer cancelled")
    operation: asyncio.Future[ResultT] = asyncio.ensure_future(awaitable)
    cancelled = asyncio.create_task(cancellation.wait())
    try:
        done, _ = await asyncio.wait(
            (operation, cancelled),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancelled in done and cancellation.is_set():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise cancelled_error("transfer cancelled")
        return await operation
    except asyncio.CancelledError:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        raise
    finally:
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)


async def _join_thread_task_after_cancellation(
    task: asyncio.Task[ResultT],
) -> ResultT:
    """Wait for an already-running thread task despite repeated outer cancellation."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    return task.result()


async def _critical_to_thread(
    function: Callable[..., ResultT],
    *args: Any,
    cancel_cleanup: Callable[[ResultT], None] | None = None,
) -> ResultT:
    """Join non-cancellable filesystem work before propagating task cancellation."""

    operation = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError as cancelled:
        try:
            result = await _join_thread_task_after_cancellation(operation)
        except BaseException:
            raise cancelled from None
        if cancel_cleanup is not None:
            cleanup = asyncio.create_task(asyncio.to_thread(cancel_cleanup, result))
            try:
                await _join_thread_task_after_cancellation(cleanup)
            except BaseException:
                pass
        raise cancelled


def _possibly_in_progress(exc: ManagerClientError) -> bool:
    return exc.retryable or exc.status_code == 409


def _transient_status(status_code: int | None) -> bool:
    return status_code in {408, 425, 429} or (status_code is not None and status_code >= 500)


def _validated_upload_headers(
    supplied: dict[str, str],
    *,
    expected_size: int,
    expected_content_type: str,
) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name, value in supplied.items():
        lowered = name.strip().lower()
        if (
            not lowered
            or lowered in normalized
            or (
                lowered
                not in {
                    "content-type",
                    "content-length",
                    "if-none-match",
                    "x-rvc-upload-token",
                }
                and not lowered.startswith("x-amz-")
            )
            or any(character in name + value for character in "\r\n\x00")
        ):
            raise ManagerClientError("Manager returned unsafe artifact upload headers")
        normalized[lowered] = value.strip()
    if "if-none-match" in normalized and normalized["if-none-match"] != "*":
        raise ManagerClientError("Manager returned unsafe artifact upload headers")
    if normalized.get("content-length") != str(expected_size):
        raise ManagerClientError("artifact upload Content-Length does not match the source")
    if normalized.get("content-type", "").lower() != expected_content_type:
        raise ManagerClientError("artifact upload Content-Type does not match initialization")
    return normalized


def _validated_test_set_redirect(location: str | None, *, manager_base_url: str) -> str:
    if not location or any(character in location for character in "\r\n\x00"):
        raise TestSetTransferError("Manager returned an invalid TestSet redirect")
    parsed = urlparse(location)
    manager_scheme = urlparse(manager_base_url).scheme
    try:
        _ = parsed.port
    except ValueError as exc:
        raise TestSetTransferError("Manager returned an invalid TestSet redirect") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (manager_scheme == "https" and parsed.scheme != "https")
    ):
        raise TestSetTransferError("Manager returned an unsafe TestSet redirect")
    return location


async def _stream_test_set_response(
    response: httpx.Response,
    destination: Path,
    *,
    item: TestSetTransferItem,
) -> None:
    raw_length = response.headers.get("Content-Length")
    try:
        declared_length = int(raw_length) if raw_length is not None else -1
    except ValueError as exc:
        raise TestSetTransferError("TestSet response has an invalid Content-Length") from exc
    if declared_length != item.size_bytes:
        raise TestSetTransferError("TestSet response Content-Length does not match the claim")
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type != item.content_type:
        raise TestSetTransferError("TestSet response Content-Type does not match the claim")
    content_encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if content_encoding not in {"", "identity"}:
        raise TestSetTransferError("encoded TestSet responses are forbidden")

    descriptor: int | None = None
    partial: Path | None = None
    digest = hashlib.sha256()
    total = 0
    try:
        active_descriptor, active_partial = await _critical_to_thread(
            _open_test_set_partial,
            destination,
            cancel_cleanup=_discard_opened_partial,
        )
        descriptor, partial = active_descriptor, active_partial
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            total += len(chunk)
            if total > item.size_bytes:
                raise TestSetTransferError("TestSet response exceeds the declared size")
            digest.update(chunk)
            await _critical_to_thread(_write_all, active_descriptor, chunk)
        if total != item.size_bytes:
            raise TestSetTransferError("TestSet response ended before the declared size")
        if digest.hexdigest() != item.sha256:
            raise TestSetTransferError("TestSet response checksum does not match the claim")
        await _critical_to_thread(os.fsync, active_descriptor)
        await _critical_to_thread(os.close, active_descriptor)
        descriptor = None
        await _critical_to_thread(
            _publish_test_set_partial,
            active_partial,
            destination,
            item.size_bytes,
            item.sha256,
        )
        partial = None
    except TestSetTransferError:
        raise
    except OSError as exc:
        raise TestSetTransferError("TestSet file could not be published safely") from exc
    finally:
        if descriptor is not None or partial is not None:
            await _critical_to_thread(_cleanup_transfer_partial, descriptor, partial)


def _open_test_set_partial(destination: Path) -> tuple[int, Path]:
    if not destination.name or destination.name in {".", ".."} or "\x00" in str(destination):
        raise TestSetTransferError("TestSet destination is unsafe")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_stat = destination.parent.lstat()
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise TestSetTransferError("TestSet destination directory is unsafe")
    partial = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    descriptor = os.open(
        partial,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        os.close(descriptor)
        partial.unlink(missing_ok=True)
        raise TestSetTransferError("TestSet partial file is not regular")
    os.fchmod(descriptor, 0o600)
    return descriptor, partial


def _existing_test_set_item_is_valid(
    destination: Path,
    expected_size: int,
    expected_sha256: str,
) -> bool:
    try:
        descriptor = os.open(destination, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise TestSetTransferError("existing TestSet destination is unsafe") from exc
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != expected_size
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise TestSetTransferError("existing TestSet destination is invalid")
        while chunk := os.read(descriptor, 1024**2):
            digest.update(chunk)
    except OSError as exc:
        raise TestSetTransferError("existing TestSet destination cannot be verified") from exc
    finally:
        os.close(descriptor)
    if digest.hexdigest() != expected_sha256:
        raise TestSetTransferError("existing TestSet destination checksum is invalid")
    return True


def _publish_test_set_partial(
    partial: Path,
    destination: Path,
    expected_size: int,
    expected_sha256: str,
) -> None:
    try:
        os.link(partial, destination, follow_symlinks=False)
    except FileExistsError:
        _existing_test_set_item_is_valid(destination, expected_size, expected_sha256)
    except OSError as exc:
        raise TestSetTransferError("TestSet file could not be atomically published") from exc
    finally:
        partial.unlink(missing_ok=True)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_descriptor = os.open(destination.parent, directory_flags)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _validated_dataset_redirect(location: str | None, *, manager_base_url: str) -> str:
    if not location or any(character in location for character in "\r\n\x00"):
        raise DatasetTransferError("Manager returned an invalid Dataset redirect")
    parsed = urlparse(location)
    manager_scheme = urlparse(manager_base_url).scheme
    try:
        _ = parsed.port
    except ValueError as exc:
        raise DatasetTransferError("Manager returned an invalid Dataset redirect") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (manager_scheme == "https" and parsed.scheme != "https")
    ):
        raise DatasetTransferError("Manager returned an unsafe Dataset redirect")
    return location


async def _stream_dataset_response(
    response: httpx.Response,
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    expected_content_type: str,
) -> None:
    raw_length = response.headers.get("Content-Length")
    try:
        declared_length = int(raw_length) if raw_length is not None else -1
    except ValueError as exc:
        raise DatasetTransferError("Dataset response has an invalid Content-Length") from exc
    if declared_length != expected_size:
        raise DatasetTransferError("Dataset response Content-Length does not match the claim")
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type != expected_content_type:
        raise DatasetTransferError("Dataset response Content-Type does not match the claim")
    content_encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if content_encoding not in {"", "identity"}:
        raise DatasetTransferError("encoded Dataset responses are forbidden")

    descriptor: int | None = None
    partial: Path | None = None
    digest = hashlib.sha256()
    total = 0
    try:
        active_descriptor, active_partial = await _critical_to_thread(
            _open_dataset_partial,
            destination,
            cancel_cleanup=_discard_opened_partial,
        )
        descriptor, partial = active_descriptor, active_partial
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            total += len(chunk)
            if total > expected_size:
                raise DatasetTransferError("Dataset response exceeds the declared size")
            digest.update(chunk)
            await _critical_to_thread(_write_all, active_descriptor, chunk)
        if total != expected_size:
            raise DatasetTransferError("Dataset response ended before the declared size")
        if digest.hexdigest() != expected_sha256:
            raise DatasetTransferError("Dataset response checksum does not match the claim")
        await _critical_to_thread(os.fsync, active_descriptor)
        await _critical_to_thread(os.close, active_descriptor)
        descriptor = None
        await _critical_to_thread(
            _publish_dataset_partial,
            active_partial,
            destination,
            expected_size,
            expected_sha256,
        )
        partial = None
    except DatasetTransferError:
        raise
    except OSError as exc:
        raise DatasetTransferError("Dataset file could not be published safely") from exc
    finally:
        if descriptor is not None or partial is not None:
            await _critical_to_thread(_cleanup_transfer_partial, descriptor, partial)


def _open_dataset_partial(destination: Path) -> tuple[int, Path]:
    rendered_name = destination.name
    if not rendered_name or rendered_name in {".", ".."} or "\x00" in str(destination):
        raise DatasetTransferError("Dataset destination is unsafe")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_stat = destination.parent.lstat()
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise DatasetTransferError("Dataset destination directory is unsafe")
    partial = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(partial, flags, 0o600)
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        os.close(descriptor)
        partial.unlink(missing_ok=True)
        raise DatasetTransferError("Dataset partial file is not regular")
    os.fchmod(descriptor, 0o600)
    return descriptor, partial


def _discard_opened_partial(opened: tuple[int, Path]) -> None:
    _cleanup_transfer_partial(opened[0], opened[1])


def _cleanup_transfer_partial(descriptor: int | None, partial: Path | None) -> None:
    if descriptor is not None:
        try:
            os.close(descriptor)
        except OSError:
            pass
    if partial is not None:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "short Dataset write")
        view = view[written:]


def _existing_dataset_is_valid(
    destination: Path,
    expected_size: int,
    expected_sha256: str,
) -> bool:
    try:
        descriptor = os.open(destination, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DatasetTransferError("existing Dataset destination is unsafe") from exc
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
            raise DatasetTransferError("existing Dataset destination is invalid")
        while chunk := os.read(descriptor, 1024**2):
            digest.update(chunk)
    except OSError as exc:
        raise DatasetTransferError("existing Dataset destination cannot be verified") from exc
    finally:
        os.close(descriptor)
    if digest.hexdigest() != expected_sha256:
        raise DatasetTransferError("existing Dataset destination checksum is invalid")
    return True


def _publish_dataset_partial(
    partial: Path,
    destination: Path,
    expected_size: int,
    expected_sha256: str,
) -> None:
    try:
        # A same-directory hard link publishes the fully fsynced inode atomically
        # without ever following or replacing an attacker-controlled destination.
        os.link(partial, destination, follow_symlinks=False)
    except FileExistsError:
        _existing_dataset_is_valid(destination, expected_size, expected_sha256)
    except OSError as exc:
        raise DatasetTransferError("Dataset file could not be atomically published") from exc
    finally:
        partial.unlink(missing_ok=True)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_descriptor = os.open(destination.parent, directory_flags)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
