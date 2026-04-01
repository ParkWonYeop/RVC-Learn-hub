from __future__ import annotations

import hashlib
import hmac
import math
import re
from dataclasses import dataclass
from typing import Protocol, cast

from fastapi import Request

from .config import Settings

_INCREMENT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {count, ttl}
"""
_SAFE_RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class RateLimitBackend(Protocol):
    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object: ...

    async def aclose(self) -> None: ...


class RateLimiterUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    name: str
    requests: int
    window_seconds: int = 60


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class RedisRateLimiter:
    def __init__(self, backend: RateLimitBackend, key_secret: bytes) -> None:
        if not key_secret:
            raise ValueError("rate-limit key secret must not be empty")
        self.backend = backend
        self.key_secret = key_secret

    @classmethod
    def from_settings(cls, settings: Settings) -> RedisRateLimiter:
        if not settings.redis_url:
            raise ValueError("REDIS_URL is required when rate limiting is enabled")
        from redis.asyncio import Redis

        backend = cast(RateLimitBackend, Redis.from_url(settings.redis_url))
        return cls(backend, settings.jwt_secret.get_secret_value().encode("utf-8"))

    async def check(self, identity: str, rule: RateLimitRule) -> RateLimitDecision:
        if rule.requests <= 0 or rule.window_seconds <= 0:
            raise ValueError("rate-limit values must be positive")
        digest = hmac.new(
            self.key_secret,
            f"rate-limit\x1f{rule.name}\x1f{identity}".encode("utf-8", errors="replace"),
            hashlib.sha256,
        ).hexdigest()
        key = f"rvc:rate-limit:{rule.name}:{digest}"
        try:
            raw = await self.backend.eval(
                _INCREMENT_SCRIPT,
                1,
                key,
                str(rule.window_seconds),
            )
            if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                raise TypeError("unexpected Redis rate-limit response")
            count = int(raw[0])
            ttl = int(raw[1])
        except Exception as exc:
            raise RateLimiterUnavailable("Redis rate-limit check failed") from exc
        retry_after = max(1, ttl if ttl > 0 else rule.window_seconds)
        return RateLimitDecision(
            allowed=count <= rule.requests,
            limit=rule.requests,
            remaining=max(0, rule.requests - count),
            retry_after_seconds=retry_after,
        )

    async def close(self) -> None:
        await self.backend.aclose()


def rule_for_request(request: Request, settings: Settings) -> RateLimitRule | None:
    if not settings.rate_limit_enabled:
        return None
    path = request.url.path
    if not path.startswith(f"{settings.api_prefix}/"):
        return None
    method = request.method.upper()
    if method == "POST" and path == f"{settings.api_prefix}/auth/login":
        return RateLimitRule("login", settings.rate_limit_login_requests_per_minute)
    if method == "POST" and path in {
        f"{settings.api_prefix}/workers/register",
        f"{settings.api_prefix}/workers/re-enroll",
    }:
        return RateLimitRule("worker-register", settings.rate_limit_register_requests_per_minute)
    if method == "POST" and path.startswith(f"{settings.api_prefix}/workers/token-rotation/"):
        return RateLimitRule(
            "worker-token-rotation",
            settings.rate_limit_worker_token_rotation_requests_per_minute,
        )
    if (
        method == "POST"
        and path.startswith(f"{settings.api_prefix}/workers/")
        and path.endswith("/token/revoke")
    ):
        return RateLimitRule(
            "worker-token-rotation",
            settings.rate_limit_worker_token_rotation_requests_per_minute,
        )
    sample_download_prefix = f"{settings.api_prefix}/samples/"
    if method == "GET" and path.startswith(sample_download_prefix):
        suffix = path.removeprefix(sample_download_prefix)
        sample_id, separator, action = suffix.partition("/")
        if (
            separator
            and action == "download"
            and _SAFE_RESOURCE_ID.fullmatch(sample_id) is not None
        ):
            return RateLimitRule(
                "sample-download",
                settings.rate_limit_sample_download_requests_per_minute,
            )
    if method == "POST" and (
        path == f"{settings.api_prefix}/datasets/uploads/init"
        or path.endswith("/artifact-uploads/init")
        or path.endswith("/item-uploads/init")
    ):
        return RateLimitRule("upload-init", settings.rate_limit_upload_requests_per_minute)
    if method == "POST" and (
        "/datasets/uploads/" in path
        and path.endswith("/finalize")
        or "/artifact-uploads/" in path
        and path.endswith("/finalize")
        or "/test-sets/" in path
        and path.endswith("/finalize")
    ):
        return RateLimitRule("upload-finalize", settings.rate_limit_finalize_requests_per_minute)
    if (
        method == "POST"
        and path.startswith(f"{settings.api_prefix}/workers/jobs/")
        and path.endswith("/samples")
    ):
        return RateLimitRule("sample-register", settings.rate_limit_sample_requests_per_minute)
    return RateLimitRule("api", settings.rate_limit_default_requests_per_minute)


def request_rate_limit_identity(request: Request) -> str:
    authorization = request.headers.get("Authorization")
    if authorization:
        return f"authorization:{authorization}"
    client_host = request.client.host if request.client is not None else "unknown"
    return f"client:{client_host}"


def reset_after_seconds(decision: RateLimitDecision) -> str:
    return str(max(1, math.ceil(decision.retry_after_seconds)))
