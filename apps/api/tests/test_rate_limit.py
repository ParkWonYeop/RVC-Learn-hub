from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import ValidationError

from rvc_manager_api.config import Settings
from rvc_manager_api.rate_limit import (
    RateLimitDecision,
    RateLimiterUnavailable,
    RateLimitRule,
    RedisRateLimiter,
)


class FakeRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.keys: list[str] = []
        self.closed = False

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object:
        assert "INCR" in script
        assert numkeys == 1
        key = str(keys_and_args[0])
        ttl = int(keys_and_args[1])
        self.keys.append(key)
        self.counts[key] = self.counts.get(key, 0) + 1
        return [self.counts[key], ttl]

    async def aclose(self) -> None:
        self.closed = True


class StubLimiter:
    def __init__(
        self,
        decision: RateLimitDecision | None = None,
        error: Exception | None = None,
    ) -> None:
        self.decision = decision
        self.error = error
        self.identities: list[str] = []
        self.rules: list[RateLimitRule] = []

    async def check(self, identity: str, rule: RateLimitRule) -> RateLimitDecision:
        self.identities.append(identity)
        self.rules.append(rule)
        if self.error is not None:
            raise self.error
        assert self.decision is not None
        return self.decision


async def test_redis_limiter_is_atomic_bounded_and_hashes_identity() -> None:
    backend = FakeRedis()
    limiter = RedisRateLimiter(backend, b"test-rate-limit-secret")
    rule = RateLimitRule("login", requests=2, window_seconds=60)
    first = await limiter.check("client:203.0.113.7", rule)
    second = await limiter.check("client:203.0.113.7", rule)
    denied = await limiter.check("client:203.0.113.7", rule)
    assert (first.allowed, first.remaining) == (True, 1)
    assert (second.allowed, second.remaining) == (True, 0)
    assert (denied.allowed, denied.remaining, denied.retry_after_seconds) == (False, 0, 60)
    assert len(set(backend.keys)) == 1
    assert all("203.0.113.7" not in key for key in backend.keys)
    await limiter.close()
    assert backend.closed is True


async def test_login_rate_limit_returns_retry_metadata_and_security_headers(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    limiter = StubLimiter(
        RateLimitDecision(
            allowed=False,
            limit=10,
            remaining=0,
            retry_after_seconds=47,
        )
    )
    app.state.settings.rate_limit_enabled = True
    app.state.rate_limiter = limiter
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.test", "password": "wrong-password"},
    )
    assert response.status_code == 429
    assert response.json() == {"detail": "request rate limit exceeded"}
    assert response.headers["Retry-After"] == "47"
    assert response.headers["RateLimit-Limit"] == "10"
    assert response.headers["RateLimit-Remaining"] == "0"
    assert response.headers["RateLimit-Reset"] == "47"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert limiter.rules[0].name == "login"
    assert limiter.identities[0].startswith("client:")


async def test_sample_registration_uses_dedicated_low_rate_rule(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    limiter = StubLimiter(
        RateLimitDecision(
            allowed=True,
            limit=30,
            remaining=29,
            retry_after_seconds=60,
        )
    )
    app.state.settings.rate_limit_enabled = True
    app.state.rate_limiter = limiter
    response = await client.post("/api/v1/workers/jobs/rate-test/samples", json={})
    assert response.status_code == 401
    assert limiter.rules[0].name == "sample-register"
    assert limiter.rules[0].requests == 30


async def test_sample_download_has_distinct_exact_path_rate_rule(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    limiter = StubLimiter(
        RateLimitDecision(
            allowed=True,
            limit=60,
            remaining=59,
            retry_after_seconds=60,
        )
    )
    app.state.settings.rate_limit_enabled = True
    app.state.rate_limiter = limiter
    response = await client.get("/api/v1/samples/sample-1/download")
    assert response.status_code == 401
    assert limiter.rules[0].name == "sample-download"
    assert limiter.rules[0].requests == 60

    limiter.rules.clear()
    await client.get("/api/v1/samples/unsafe%2Fid/download")
    assert limiter.rules[0].name == "api"


async def test_worker_token_rotation_and_revoke_share_dedicated_low_rate_rule(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    limiter = StubLimiter(
        RateLimitDecision(
            allowed=True,
            limit=6,
            remaining=5,
            retry_after_seconds=60,
        )
    )
    app.state.settings.rate_limit_enabled = True
    app.state.rate_limiter = limiter
    await client.post(
        "/api/v1/workers/token-rotation/prepare",
        json={"rotation_id": "12345678-1234-4123-8123-123456789abc"},
    )
    await client.post(
        "/api/v1/workers/worker-1/token/revoke",
        json={
            "expected_worker_name": "worker-1",
            "reason_code": "suspected_compromise",
        },
    )
    assert [rule.name for rule in limiter.rules] == [
        "worker-token-rotation",
        "worker-token-rotation",
    ]
    assert all(rule.requests == 6 for rule in limiter.rules)


@pytest.mark.parametrize("fail_closed, expected_status", [(True, 503), (False, 401)])
async def test_rate_limit_backend_failure_policy_is_explicit(
    app: FastAPI,
    client: AsyncClient,
    fail_closed: bool,
    expected_status: int,
) -> None:
    app.state.settings.rate_limit_enabled = True
    app.state.settings.rate_limit_fail_closed = fail_closed
    app.state.rate_limiter = StubLimiter(error=RateLimiterUnavailable("injected"))
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == expected_status
    if fail_closed:
        assert response.json() == {"detail": "request rate limiter is unavailable"}
    else:
        assert response.json()["detail"] == "user bearer token required"


def test_enabled_rate_limit_requires_redis_url() -> None:
    with pytest.raises(ValidationError, match="REDIS_URL"):
        Settings(
            environment="test",
            jwt_secret="test-jwt-secret-with-at-least-thirty-two-characters",
            rate_limit_enabled=True,
        )
