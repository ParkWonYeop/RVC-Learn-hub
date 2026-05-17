from __future__ import annotations

import os
import stat
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from rvc_orchestrator_contracts import utc_now
from rvc_worker.credentials import CredentialError, CredentialStore, WorkerCredential

ISSUED_TOKEN = "rvcw_" + "i" * 43
OLD_TOKEN = "rvcw_" + "o" * 43
PENDING_TOKEN = "rvcw_" + "p" * 43


class CredentialStoreTests(unittest.TestCase):
    def test_round_trip_uses_private_file_mode_and_hides_token(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "credentials" / "worker.json"
            store = CredentialStore(path)
            credential = WorkerCredential(
                manager_url="https://manager.example",
                worker_id="worker-1",
                worker_name="gpu-01",
                worker_token=ISSUED_TOKEN,
            )
            store.save(credential)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            loaded = store.load(manager_url="https://manager.example", worker_name="gpu-01")
            self.assertEqual(loaded, credential)
            self.assertNotIn(ISSUED_TOKEN, repr(loaded))

    def test_permissive_credential_file_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "worker.json"
            path.write_text("{}", encoding="utf-8")
            os.chmod(path, 0o644)
            with self.assertRaises(CredentialError):
                CredentialStore(path).load(
                    manager_url="https://manager.example", worker_name="gpu-01"
                )

    def test_pending_rotation_round_trip_hides_both_tokens(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "worker.json"
            store = CredentialStore(path)
            credential = WorkerCredential(
                manager_url="https://manager.example",
                worker_id="worker-1",
                worker_name="gpu-01",
                worker_token=OLD_TOKEN,
            ).stage_rotation(
                rotation_id="12345678-1234-4123-8123-123456789abc",
                worker_token=PENDING_TOKEN,
                expires_at=utc_now() + timedelta(minutes=5),
            )
            store.save(credential)
            loaded = store.load(
                manager_url="https://manager.example",
                worker_name="gpu-01",
            )
            self.assertEqual(loaded, credential)
            self.assertNotIn(OLD_TOKEN, repr(loaded))
            self.assertNotIn(PENDING_TOKEN, repr(loaded))

    def test_malformed_active_or_pending_token_is_rejected_before_use(self) -> None:
        with TemporaryDirectory() as temporary:
            store = CredentialStore(Path(temporary) / "worker.json")
            malformed_active = WorkerCredential(
                manager_url="https://manager.example",
                worker_id="worker-1",
                worker_name="gpu-01",
                worker_token="not-a-worker-token",
            )
            with self.assertRaisesRegex(CredentialError, "token format"):
                store.save(malformed_active)
            malformed_pending = WorkerCredential(
                manager_url="https://manager.example",
                worker_id="worker-1",
                worker_name="gpu-01",
                worker_token=OLD_TOKEN,
            ).stage_rotation(
                rotation_id="12345678-1234-4123-8123-123456789abc",
                worker_token="not-a-pending-token",
                expires_at=utc_now() + timedelta(minutes=5),
            )
            with self.assertRaisesRegex(CredentialError, "pending credential token"):
                store.save(malformed_pending)

    def test_credential_file_symlink_is_rejected_without_following(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            os.chmod(target, 0o600)
            link = root / "worker.json"
            link.symlink_to(target)
            with self.assertRaises(CredentialError):
                CredentialStore(link).load(
                    manager_url="https://manager.example",
                    worker_name="gpu-01",
                )


if __name__ == "__main__":
    unittest.main()
