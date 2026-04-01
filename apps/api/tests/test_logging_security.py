from __future__ import annotations

import json
import logging
import re

from httpx import AsyncClient

from rvc_manager_api.logging_config import RedactingJsonFormatter


def test_json_formatter_redacts_credentials_and_presigned_query() -> None:
    record = logging.LogRecord(
        name="rvc.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=(
            "Authorization: Bearer eyJheader.payload.signature "
            "password=formatter-secret "
            "https://objects.example/model?X-Amz-Signature=signed-secret&X-Amz-Credential=key"
        ),
        args=(),
        exc_info=None,
    )
    record.request_id = "request-1"
    document = json.loads(RedactingJsonFormatter().format(record))
    serialized = json.dumps(document)
    assert document["request_id"] == "request-1"
    assert "[REDACTED]" in document["message"]
    for secret in (
        "eyJheader.payload.signature",
        "formatter-secret",
        "signed-secret",
        "X-Amz-Credential=key",
    ):
        assert secret not in serialized


async def test_http_log_omits_query_and_response_has_security_headers(
    client: AsyncClient,
    caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="rvc_manager_api.http")
    response = await client.get(
        "/health?X-Amz-Signature=must-not-be-logged",
        headers={"X-Request-ID": "safe-request-123"},
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "safe-request-123"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"
    assert response.headers["Content-Security-Policy"] == (
        "default-src 'none'; frame-ancestors 'none'"
    )
    records = [record for record in caplog.records if record.name == "rvc_manager_api.http"]
    assert len(records) == 1
    record = records[0]
    assert record.path == "/health"
    assert record.request_id == "safe-request-123"
    assert record.status_code == 200
    assert "must-not-be-logged" not in caplog.text


async def test_untrusted_request_id_is_replaced(client: AsyncClient) -> None:
    response = await client.get(
        "/health",
        headers={"X-Request-ID": "invalid request id with spaces"},
    )
    request_id = response.headers["X-Request-ID"]
    assert request_id != "invalid request id with spaces"
    assert re.fullmatch(r"[0-9a-f-]{36}", request_id)


async def test_hsts_uses_operator_scheme_and_ignores_forwarded_header(
    app,
    client: AsyncClient,
) -> None:
    app.state.settings.environment = "production"
    app.state.settings.public_scheme = "http"

    spoofed = await client.get("/health", headers={"X-Forwarded-Proto": "https"})
    assert "Strict-Transport-Security" not in spoofed.headers

    app.state.settings.public_scheme = "https"
    normalized = await client.get("/health", headers={"X-Forwarded-Proto": "http"})
    assert normalized.headers["Strict-Transport-Security"] == "max-age=31536000"
