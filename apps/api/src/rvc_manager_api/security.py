from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from pwdlib import PasswordHash

from .config import Settings

JWT_ALGORITHM = "HS256"
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PASSWORD_HASH = PasswordHash.recommended()
# A process-local Argon2 hash ensures an unknown email still performs the same
# expensive verification operation as a known email with a wrong password.
_DUMMY_PASSWORD_HASH = _PASSWORD_HASH.hash("rvc-login-enumeration-dummy-password")


class InvalidAccessToken(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    subject: str
    jti: str
    issued_at: datetime
    expires_at: datetime
    access_token_version: int


def normalize_email(value: str) -> str:
    normalized = value.strip().casefold()
    if not 3 <= len(normalized) <= 320 or _EMAIL_PATTERN.fullmatch(normalized) is None:
        raise ValueError("email must be a valid address")
    return normalized


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 1_024:
        raise ValueError("password must contain between 12 and 1024 characters")
    return _PASSWORD_HASH.hash(password)


def validate_management_password(password: str, *, email: str) -> None:
    """Apply the stronger policy used for administrator-created credentials.

    Existing credentials keep the original twelve-character compatibility
    boundary, while newly created/reset credentials must be long, non-trivial,
    and unrelated to the account identifier. Complexity-class requirements are
    deliberately avoided so passphrases remain valid.
    """

    if not 16 <= len(password) <= 1_024:
        raise ValueError("password does not satisfy the administrator password policy")
    if any(ord(character) < 32 or ord(character) == 127 for character in password):
        raise ValueError("password does not satisfy the administrator password policy")
    folded = password.casefold()
    local_part = email.partition("@")[0].casefold()
    if len(set(password)) < 8 or (len(local_part) >= 3 and local_part in folded):
        raise ValueError("password does not satisfy the administrator password policy")
    compact = re.sub(r"[^a-z0-9]", "", folded)
    if compact in {
        "passwordpassword",
        "administrator",
        "changemechangeme",
        "correcthorsebatterystaple",
    }:
        raise ValueError("password does not satisfy the administrator password policy")


def verify_password(password: str, encoded_hash: str | None) -> bool:
    candidate_hash = encoded_hash or _DUMMY_PASSWORD_HASH
    try:
        verified = _PASSWORD_HASH.verify(password, candidate_hash)
    except Exception:  # malformed stored hashes must not expose an internal error
        if encoded_hash is not None:
            _PASSWORD_HASH.verify(password, _DUMMY_PASSWORD_HASH)
        return False
    return encoded_hash is not None and verified


def audit_email_fingerprint(email: str) -> str:
    return hashlib.sha256(email.encode("utf-8")).hexdigest()


def issue_access_token(
    user_id: str,
    settings: Settings,
    *,
    access_token_version: int = 1,
    now: datetime | None = None,
    jti: str | None = None,
) -> tuple[str, AccessTokenClaims]:
    if access_token_version < 1:
        raise ValueError("access token version must be positive")
    issued_at = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    expires_at = issued_at + timedelta(seconds=settings.jwt_access_ttl_seconds)
    token_jti = jti or str(uuid.uuid4())
    payload: dict[str, Any] = {
        "sub": user_id,
        "jti": token_jti,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "ver": access_token_version,
    }
    token = jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=JWT_ALGORITHM,
    )
    return token, AccessTokenClaims(
        user_id,
        token_jti,
        issued_at,
        expires_at,
        access_token_version,
    )


def decode_access_token(token: str, settings: Settings) -> AccessTokenClaims:
    try:
        raw = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[JWT_ALGORITHM],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=settings.jwt_leeway_seconds,
            options={"require": ["sub", "jti", "iat", "exp", "iss", "aud", "ver"]},
        )
    except jwt.InvalidTokenError as exc:
        raise InvalidAccessToken("invalid or expired access token") from exc
    payload: dict[str, Any] = raw
    subject = payload.get("sub")
    token_jti = payload.get("jti")
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    access_token_version = payload.get("ver")
    if not isinstance(subject, str) or not isinstance(token_jti, str):
        raise InvalidAccessToken("invalid or expired access token")
    if not isinstance(issued_at, int) or not isinstance(expires_at, int):
        raise InvalidAccessToken("invalid or expired access token")
    if type(access_token_version) is not int or access_token_version < 1:
        raise InvalidAccessToken("invalid or expired access token")
    try:
        uuid.UUID(subject)
        uuid.UUID(token_jti)
    except ValueError as exc:
        raise InvalidAccessToken("invalid or expired access token") from exc
    return AccessTokenClaims(
        subject=subject,
        jti=token_jti,
        issued_at=datetime.fromtimestamp(issued_at, UTC),
        expires_at=datetime.fromtimestamp(expires_at, UTC),
        access_token_version=access_token_version,
    )


def issue_worker_token() -> str:
    return f"rvcw_{secrets.token_urlsafe(32)}"


def hash_worker_token(token: str, settings: Settings) -> str:
    return hmac.new(
        settings.worker_token_pepper.get_secret_value().encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_bootstrap_token(provided: str | None, settings: Settings) -> bool:
    configured = settings.worker_bootstrap_token
    if configured is None or provided is None:
        return False
    return hmac.compare_digest(configured.get_secret_value(), provided)
