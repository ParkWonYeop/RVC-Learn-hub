"""Crash-recoverable two-phase Worker bearer-token rotation."""

from __future__ import annotations

import uuid
from typing import Protocol

from rvc_orchestrator_contracts import (
    WorkerSessionResponse,
    WorkerStatus,
    WorkerTokenRotationActivated,
    WorkerTokenRotationPrepareResponse,
    WorkerTokenRotationRequest,
    WorkerTokenRotationStatus,
    utc_now,
)

from .client import ManagerClientError
from .credentials import CredentialError, CredentialStore, WorkerCredential


class TokenRotationClient(Protocol):
    def set_worker_token(self, token: str) -> None: ...

    async def get_session(self) -> WorkerSessionResponse | None: ...

    async def get_token_rotation_status(self) -> WorkerTokenRotationStatus: ...

    async def prepare_token_rotation(
        self,
        request: WorkerTokenRotationRequest,
    ) -> WorkerTokenRotationPrepareResponse: ...

    async def activate_token_rotation(
        self,
        request: WorkerTokenRotationRequest,
        *,
        pending_worker_token: str,
    ) -> WorkerTokenRotationActivated: ...

    async def abort_token_rotation(self, request: WorkerTokenRotationRequest) -> None: ...


async def reconcile_worker_token_rotation(
    client: TokenRotationClient,
    store: CredentialStore,
    credential: WorkerCredential,
) -> WorkerCredential:
    """Recover a crash at any rotation boundary without guessing the active token."""

    client.set_worker_token(credential.worker_token)
    if credential.pending_rotation_id is not None:
        return await _complete_pending_rotation(
            client,
            store,
            credential,
            require_activation=False,
        )

    status = await client.get_token_rotation_status()
    _validate_status_owner(status, credential)
    if status.pending:
        if status.rotation_id is None:
            raise CredentialError("Manager returned an incomplete Worker token rotation")
        # The Manager committed prepare but the one-time response was lost before
        # the pending secret reached durable local storage. The old token remains
        # valid, so aborting the unrecoverable secret is the only safe action.
        await client.abort_token_rotation(
            WorkerTokenRotationRequest(rotation_id=status.rotation_id)
        )
    return credential


async def rotate_worker_token(
    client: TokenRotationClient,
    store: CredentialStore,
    credential: WorkerCredential,
) -> WorkerCredential:
    """Rotate one idle Worker's token and persist each crash-recovery boundary."""

    credential = await reconcile_worker_token_rotation(client, store, credential)
    client.set_worker_token(credential.worker_token)
    session = await client.get_session()
    _validate_session_owner(session, credential)
    assert session is not None
    if session.current_job_id is not None or session.status is not WorkerStatus.IDLE:
        raise CredentialError("Worker token rotation requires an idle Worker")

    rotation_id = str(uuid.uuid4())
    request = WorkerTokenRotationRequest(rotation_id=rotation_id)
    try:
        prepared = await client.prepare_token_rotation(request)
    except ManagerClientError:
        # A transport failure can be post-commit. Reconciliation observes and
        # aborts a server-side pending rotation whose one-time token was lost.
        await reconcile_worker_token_rotation(client, store, credential)
        raise
    if prepared.worker_id != credential.worker_id or prepared.rotation_id != rotation_id:
        try:
            await client.abort_token_rotation(request)
        except ManagerClientError:
            pass
        raise CredentialError("Manager returned a mismatched Worker token rotation")

    staged = credential.stage_rotation(
        rotation_id=rotation_id,
        worker_token=prepared.worker_token,
        expires_at=prepared.expires_at,
    )
    try:
        store.save(staged)
    except CredentialError:
        try:
            await client.abort_token_rotation(request)
        except ManagerClientError:
            pass
        raise
    return await _complete_pending_rotation(
        client,
        store,
        staged,
        require_activation=True,
    )


async def _complete_pending_rotation(
    client: TokenRotationClient,
    store: CredentialStore,
    credential: WorkerCredential,
    *,
    require_activation: bool,
) -> WorkerCredential:
    rotation_id = credential.pending_rotation_id
    pending_token = credential.pending_worker_token
    expires_at = credential.pending_rotation_expires_at
    if rotation_id is None or pending_token is None or expires_at is None:
        raise CredentialError("Worker pending credential fields are incomplete")
    request = WorkerTokenRotationRequest(rotation_id=rotation_id)
    client.set_worker_token(credential.worker_token)
    try:
        await client.activate_token_rotation(
            request,
            pending_worker_token=pending_token,
        )
    except ManagerClientError as activation_error:
        # Activation may have committed even if the response was lost. Prove the
        # pending token before deciding whether old-token recovery is possible.
        pending_session = await _session_for_token(client, pending_token)
        if pending_session is not None:
            _validate_session_owner(pending_session, credential)
            return _persist_activated(client, store, credential)

        client.set_worker_token(credential.worker_token)
        status = await client.get_token_rotation_status()
        _validate_status_owner(status, credential)
        if not status.pending:
            cleared = credential.clear_pending()
            store.save(cleared)
            if require_activation:
                raise CredentialError(
                    "Worker token rotation was not activated"
                ) from activation_error
            return cleared
        if status.rotation_id != rotation_id:
            raise CredentialError(
                "Manager has a different pending Worker token rotation"
            ) from activation_error
        if status.expires_at is None or status.expires_at <= utc_now():
            await client.abort_token_rotation(request)
            cleared = credential.clear_pending()
            store.save(cleared)
            if require_activation:
                raise CredentialError(
                    "Worker token rotation expired before activation"
                ) from activation_error
            return cleared
        # The old credential and exact pending record are both still valid. One
        # bounded retry resolves a pre-commit transport failure without looping.
        await client.activate_token_rotation(
            request,
            pending_worker_token=pending_token,
        )

    client.set_worker_token(pending_token)
    session = await client.get_session()
    _validate_session_owner(session, credential)
    return _persist_activated(client, store, credential)


async def _session_for_token(
    client: TokenRotationClient,
    token: str,
) -> WorkerSessionResponse | None:
    client.set_worker_token(token)
    try:
        return await client.get_session()
    except ManagerClientError as exc:
        if exc.status_code == 401:
            return None
        raise


def _persist_activated(
    client: TokenRotationClient,
    store: CredentialStore,
    credential: WorkerCredential,
) -> WorkerCredential:
    activated = credential.activate_pending()
    store.save(activated)
    client.set_worker_token(activated.worker_token)
    return activated


def _validate_status_owner(
    status: WorkerTokenRotationStatus,
    credential: WorkerCredential,
) -> None:
    if status.worker_id != credential.worker_id:
        raise CredentialError("Worker token rotation status belongs to another Worker")


def _validate_session_owner(
    session: WorkerSessionResponse | None,
    credential: WorkerCredential,
) -> None:
    if (
        session is None
        or session.worker_id != credential.worker_id
        or session.name != credential.worker_name
    ):
        raise CredentialError("Worker token belongs to another Worker")
