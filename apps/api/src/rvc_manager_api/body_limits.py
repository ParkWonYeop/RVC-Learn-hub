from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BoundedSampleRegistrationBodyMiddleware:
    """Reject oversized Sample JSON before FastAPI authentication or parsing."""

    def __init__(self, app: ASGIApp, *, api_prefix: str, max_bytes: int) -> None:
        self.app = app
        self.api_prefix = api_prefix.rstrip("/")
        self.max_bytes = max_bytes

    def _applies(self, scope: Scope) -> bool:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return False
        path = str(scope.get("path", ""))
        prefix = f"{self.api_prefix}/workers/jobs/"
        if not path.startswith(prefix) or not path.endswith("/samples"):
            return False
        job_id = path[len(prefix) : -len("/samples")]
        return bool(job_id) and "/" not in job_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._applies(scope):
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared = int(content_length.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                await self._reject(send, 400, "invalid Content-Length")
                return
            if declared < 0:
                await self._reject(send, 400, "invalid Content-Length")
                return
            if declared > self.max_bytes:
                await self._reject(send, 413, "sample registration body is too large")
                return

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = bytes(message.get("body", b""))
            total += len(chunk)
            if total > self.max_bytes:
                await self._reject(send, 413, "sample registration body is too large")
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        body_error = self._body_error(body)
        if body_error is not None:
            status_code, detail = body_error
            await self._reject(send, status_code, detail)
            return
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

    def _body_error(self, body: bytes) -> tuple[int, str] | None:
        return None

    @staticmethod
    async def _reject(send: Send, status_code: int, detail: str) -> None:
        body = json.dumps({"detail": detail}, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class BoundedExperimentBodyMiddleware(BoundedSampleRegistrationBodyMiddleware):
    """Bound Experiment create/PATCH JSON before authentication and parsing."""

    def _applies(self, scope: Scope) -> bool:
        if scope.get("type") != "http":
            return False
        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))
        collection_path = f"{self.api_prefix}/experiments"
        if method == "POST":
            if path == collection_path:
                return True
            prefix = f"{collection_path}/"
            if not path.startswith(prefix):
                return False
            parts = path[len(prefix) :].split("/")
            if len(parts) == 3:
                return bool(parts[0]) and parts[1:] == ["model-registry", "candidates"]
            if len(parts) == 5:
                return (
                    bool(parts[0])
                    and parts[1:3] == ["model-registry", "entries"]
                    and bool(parts[3])
                    and parts[4] in {"promote", "revoke"}
                )
            return False
        if method != "PATCH" or not path.startswith(f"{collection_path}/"):
            return False
        experiment_id = path[len(collection_path) + 1 :]
        return bool(experiment_id) and "/" not in experiment_id

    @staticmethod
    async def _reject(send: Send, status_code: int, detail: str) -> None:
        if detail == "sample registration body is too large":
            detail = "experiment request body is too large"
        await BoundedSampleRegistrationBodyMiddleware._reject(send, status_code, detail)


class BoundedUserLifecycleBodyMiddleware(BoundedSampleRegistrationBodyMiddleware):
    """Bound administrator user mutation JSON before auth and parsing."""

    def _applies(self, scope: Scope) -> bool:
        if scope.get("type") != "http":
            return False
        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))
        collection_path = f"{self.api_prefix}/admin/users"
        if method == "POST" and path == collection_path:
            return True
        if not path.startswith(f"{collection_path}/"):
            return False
        remainder = path[len(collection_path) + 1 :]
        if method == "PATCH":
            return bool(remainder) and "/" not in remainder
        if method == "POST" and remainder.endswith("/password-reset"):
            user_id = remainder[: -len("/password-reset")]
            return bool(user_id) and "/" not in user_id
        return False

    @staticmethod
    async def _reject(send: Send, status_code: int, detail: str) -> None:
        if detail == "sample registration body is too large":
            detail = "user lifecycle request body is too large"
        await BoundedSampleRegistrationBodyMiddleware._reject(send, status_code, detail)


class BoundedWorkerTelemetryBodyMiddleware(BoundedSampleRegistrationBodyMiddleware):
    """Bound Worker status/log/metric JSON before auth and model parsing."""

    def _applies(self, scope: Scope) -> bool:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return False
        path = str(scope.get("path", ""))
        prefix = f"{self.api_prefix}/workers/jobs/"
        if not path.startswith(prefix):
            return False
        remainder = path[len(prefix) :]
        job_id, separator, operation = remainder.partition("/")
        return (
            bool(job_id)
            and separator == "/"
            and operation
            in {
                "status",
                "logs",
                "metrics",
            }
        )

    @staticmethod
    async def _reject(send: Send, status_code: int, detail: str) -> None:
        if detail == "sample registration body is too large":
            detail = "worker telemetry request body is too large"
        await BoundedSampleRegistrationBodyMiddleware._reject(send, status_code, detail)

    def _body_error(self, body: bytes) -> tuple[int, str] | None:
        def reject_non_finite(_: str) -> None:
            raise ValueError("non-finite JSON number")

        try:
            json.loads(body.decode("utf-8"), parse_constant=reject_non_finite)
        except (UnicodeDecodeError, ValueError):
            return 422, "worker telemetry request body is not strict JSON"
        return None
