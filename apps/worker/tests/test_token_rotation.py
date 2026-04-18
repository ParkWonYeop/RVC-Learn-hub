from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from rvc_orchestrator_contracts import (
    WorkerSessionResponse,
    WorkerStatus,
    WorkerTokenRotationActivated,
    WorkerTokenRotationPrepareResponse,
    WorkerTokenRotationRequest,
    WorkerTokenRotationStatus,
    utc_now,
)
from rvc_worker.client import ManagerClientError
from rvc_worker.credentials import CredentialStore, WorkerCredential
from rvc_worker.token_rotation import (
    reconcile_worker_token_rotation,
    rotate_worker_token,
)

OLD_TOKEN = "rvcw_" + "o" * 43


class FakeRotationClient:
    def __init__(self, *, active_token: str = OLD_TOKEN) -> None:
        self.active_token = active_token
        self.current_token = active_token
        self.pending_id: str | None = None
        self.pending_token: str | None = None
        self.pending_expires_at = None
        self.lose_activation_response = False
        self.revoked = False
        self.abort_count = 0

    def set_worker_token(self, token: str) -> None:
        self.current_token = token

    async def get_session(self) -> WorkerSessionResponse | None:
        self._require_active()
        return WorkerSessionResponse(
            worker_id="worker-1",
            name="gpu-01",
            status=WorkerStatus.IDLE,
        )

    async def get_token_rotation_status(self) -> WorkerTokenRotationStatus:
        self._require_active()
        return WorkerTokenRotationStatus(
            worker_id="worker-1",
            token_issued_at=utc_now() - timedelta(days=1),
            pending=self.pending_id is not None,
            rotation_id=self.pending_id,
            started_at=(utc_now() if self.pending_id is not None else None),
            expires_at=self.pending_expires_at,
        )

    async def prepare_token_rotation(
        self,
        request: WorkerTokenRotationRequest,
    ) -> WorkerTokenRotationPrepareResponse:
        self._require_active()
        assert self.pending_id is None
        self.pending_id = request.rotation_id
        self.pending_token = "rvcw_" + "n" * 43
        self.pending_expires_at = utc_now() + timedelta(minutes=10)
        return WorkerTokenRotationPrepareResponse(
            worker_id="worker-1",
            rotation_id=request.rotation_id,
            worker_token=self.pending_token,
            expires_at=self.pending_expires_at,
        )

    async def activate_token_rotation(
        self,
        request: WorkerTokenRotationRequest,
        *,
        pending_worker_token: str,
    ) -> WorkerTokenRotationActivated:
        self._require_active()
        if request.rotation_id != self.pending_id or pending_worker_token != self.pending_token:
            raise ManagerClientError("invalid pending rotation", status_code=409)
        self.active_token = pending_worker_token
        self.pending_id = None
        self.pending_token = None
        self.pending_expires_at = None
        if self.lose_activation_response:
            self.lose_activation_response = False
            raise ManagerClientError(
                "activation response lost",
                retryable=True,
                category="transport",
            )
        return WorkerTokenRotationActivated(
            worker_id="worker-1",
            rotation_id=request.rotation_id,
            token_issued_at=utc_now(),
        )

    async def abort_token_rotation(self, request: WorkerTokenRotationRequest) -> None:
        self._require_active()
        if self.pending_id is not None and self.pending_id != request.rotation_id:
            raise ManagerClientError("different rotation", status_code=409)
        self.pending_id = None
        self.pending_token = None
        self.pending_expires_at = None
        self.abort_count += 1

    def _require_active(self) -> None:
        if self.revoked or self.current_token != self.active_token:
            raise ManagerClientError("invalid Worker token", status_code=401)


def _credential() -> WorkerCredential:
    return WorkerCredential(
        manager_url="https://manager.example",
        worker_id="worker-1",
        worker_name="gpu-01",
        worker_token=OLD_TOKEN,
    )


@pytest.mark.asyncio
async def test_rotation_persists_pending_before_activation_and_commits_new_token(
    tmp_path: Path,
) -> None:
    store = CredentialStore(tmp_path / "worker.json")
    credential = _credential()
    store.save(credential)
    client = FakeRotationClient()

    rotated = await rotate_worker_token(client, store, credential)

    assert rotated.worker_token == "rvcw_" + "n" * 43
    assert rotated.pending_worker_token is None
    assert client.current_token == rotated.worker_token
    loaded = store.load(
        manager_url=credential.manager_url,
        worker_name=credential.worker_name,
    )
    assert loaded == rotated
    assert OLD_TOKEN not in store.path.read_text(encoding="utf-8")
    assert rotated.worker_token not in repr(rotated)


@pytest.mark.asyncio
async def test_activation_response_loss_is_resolved_by_proving_pending_token(
    tmp_path: Path,
) -> None:
    store = CredentialStore(tmp_path / "worker.json")
    credential = _credential()
    store.save(credential)
    client = FakeRotationClient()
    client.lose_activation_response = True

    rotated = await rotate_worker_token(client, store, credential)

    assert rotated.worker_token == client.active_token
    assert rotated.pending_rotation_id is None
    assert store.load(
        manager_url=credential.manager_url,
        worker_name=credential.worker_name,
    ) == rotated


@pytest.mark.asyncio
async def test_lost_prepare_secret_is_aborted_using_still_valid_old_token(
    tmp_path: Path,
) -> None:
    store = CredentialStore(tmp_path / "worker.json")
    credential = _credential()
    store.save(credential)
    client = FakeRotationClient()
    request = WorkerTokenRotationRequest(
        rotation_id="12345678-1234-4123-8123-123456789abc"
    )
    await client.prepare_token_rotation(request)

    reconciled = await reconcile_worker_token_rotation(client, store, credential)

    assert reconciled == credential
    assert client.pending_id is None
    assert client.abort_count == 1


@pytest.mark.asyncio
async def test_admin_revoke_during_staged_rotation_keeps_both_local_secrets_fail_closed(
    tmp_path: Path,
) -> None:
    store = CredentialStore(tmp_path / "worker.json")
    credential = _credential().stage_rotation(
        rotation_id="12345678-1234-4123-8123-123456789abc",
        worker_token="rvcw_" + "p" * 43,
        expires_at=utc_now() + timedelta(minutes=5),
    )
    store.save(credential)
    client = FakeRotationClient()
    client.pending_id = credential.pending_rotation_id
    client.pending_token = credential.pending_worker_token
    client.pending_expires_at = credential.pending_rotation_expires_at
    client.revoked = True

    with pytest.raises(ManagerClientError) as raised:
        await reconcile_worker_token_rotation(client, store, credential)

    assert raised.value.status_code == 401
    loaded = store.load(
        manager_url=credential.manager_url,
        worker_name=credential.worker_name,
    )
    assert loaded == credential
    assert loaded.pending_worker_token is not None
