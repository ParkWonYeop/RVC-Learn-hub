from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from rvc_manager_api.config import Settings
from rvc_manager_api.storage import S3StorageAdapter
from rvc_orchestrator_contracts import utc_now
from rvc_worker.client import HttpManagerClient


class _PresignClient:
    def __init__(self) -> None:
        self.parameters: dict[str, Any] | None = None

    def generate_presigned_url(self, operation: str, **kwargs: Any) -> str:
        assert operation == "put_object"
        self.parameters = kwargs["Params"]
        return "https://objects.example.test/upload?X-Amz-Signature=redacted"

    def close(self) -> None:
        return None


async def test_manager_s3_target_headers_are_accepted_and_sent_by_worker(
    tmp_path: Path,
) -> None:
    content = b"manager-worker-artifact-contract"
    source = tmp_path / "model.pth"
    source.write_bytes(content)
    sha256 = hashlib.sha256(content).hexdigest()
    settings = Settings(
        environment="test",
        storage_backend="s3",
        s3_endpoint_url="https://minio.example.test",
        s3_access_key_id="access-key",
        s3_secret_access_key="secret-key",
        s3_bucket="artifact-contract-bucket",
        jwt_secret="test-jwt-secret-with-at-least-thirty-two-characters",
    )
    presign_client = _PresignClient()
    storage = S3StorageAdapter(settings, client=presign_client)
    observed: dict[str, str] = {}

    async def object_handler(request: httpx.Request) -> httpx.Response:
        observed.update(dict(request.headers))
        assert await request.aread() == content
        return httpx.Response(204)

    worker = HttpManagerClient(
        "https://manager.example.test",
        "bootstrap",
        worker_token="worker-token",
        object_transport_factory=lambda: httpx.MockTransport(object_handler),
    )
    try:
        target = await storage.create_upload_target(
            session_id="00000000-0000-4000-8000-000000000001",
            object_key="artifacts/staging/attempt/session",
            public_api_base_url="https://manager.example.test",
            content_type="application/x-pytorch",
            content_length=len(content),
            sha256=sha256,
            expires_at=utc_now() + timedelta(minutes=5),
            local_upload_token=None,
        )
        await worker._put_file(  # noqa: SLF001 - cross-side wire contract
            target.url,
            target.headers,
            source,
            len(content),
            "application/x-pytorch",
        )
    finally:
        await storage.close()

    assert presign_client.parameters is not None
    assert presign_client.parameters["IfNoneMatch"] == "*"
    assert observed["if-none-match"] == "*"
    assert observed["x-amz-meta-sha256"] == sha256
    assert "authorization" not in observed
