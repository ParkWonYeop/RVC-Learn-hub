from __future__ import annotations

import argparse

import pytest

from rvc_manager_api import storage_adoption
from rvc_manager_api.storage import StorageError


def test_cli_masks_adapter_failures_and_secret_urls(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail_with_secret(
        _args: argparse.Namespace,
        _session_ids: tuple[str, ...],
    ) -> object:
        raise StorageError(
            "https://objects.example.test/bucket/key?X-Amz-Credential=secret"
        )

    monkeypatch.setattr(storage_adoption, "_run", fail_with_secret)

    with pytest.raises(SystemExit) as exc_info:
        storage_adoption.main(["--kind", "dataset"])

    assert str(exc_info.value) == "storage namespace adoption failed safely"
    captured = capsys.readouterr()
    assert "secret" not in captured.out
    assert "secret" not in captured.err
    assert "objects.example.test" not in captured.out
    assert "objects.example.test" not in captured.err


def test_cli_help_discloses_that_preview_writes_audit_events(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        storage_adoption.main(["--help"])

    assert exc_info.value.code == 0
    assert "writes an audit event" in capsys.readouterr().out
