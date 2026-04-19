from __future__ import annotations

import base64
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from rvc_orchestrator_contracts import LogLevel

from ..models import JobAttempt, JobLog
from ..schemas import JobLogRead

_SENSITIVE_KEY_PARTS = frozenset(
    {
        "api-key",
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "password",
        "passwd",
        "private-key",
        "proxy-authorization",
        "secret",
        "set-cookie",
        "signature",
        "token",
    }
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_RVC_TOKEN_PATTERN = re.compile(r"\brvc[uw]_[A-Za-z0-9_-]+\b")
_URL_QUERY_PATTERN = re.compile(r"(?i)\b(https?://[^\s?#]+)\?[^\s#]*")
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)\b(authorization|proxy[_-]?authorization)\s*([=:])\s*[^\r\n,;]+"
)
_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_-]?key|cookie|credential|private[_-]?key)"
    r"\s*([=:])\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_MAX_REDACTION_DEPTH = 12


class InvalidLogCursor(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LogCursor:
    attempt_number: int
    sequence: int
    log_id: str


@dataclass(frozen=True, slots=True)
class JobLogFilters:
    attempt_id: str | None = None
    sequence_gte: int | None = None
    sequence_lte: int | None = None
    occurred_at_gte: datetime | None = None
    occurred_at_lte: datetime | None = None


@dataclass(frozen=True, slots=True)
class JobLogRecord:
    log: JobLog
    attempt_number: int

    @property
    def cursor(self) -> LogCursor:
        return LogCursor(self.attempt_number, self.log.sequence, self.log.id)


def encode_log_cursor(cursor: LogCursor) -> str:
    raw = json.dumps(
        ["v1", cursor.attempt_number, cursor.sequence, cursor.log_id],
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_log_cursor(value: str) -> LogCursor:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidLogCursor("invalid log cursor") from exc
    if (
        not isinstance(payload, list)
        or len(payload) != 4
        or payload[0] != "v1"
        or not isinstance(payload[1], int)
        or isinstance(payload[1], bool)
        or payload[1] < 1
        or not isinstance(payload[2], int)
        or isinstance(payload[2], bool)
        or payload[2] < 0
        or not isinstance(payload[3], str)
    ):
        raise InvalidLogCursor("invalid log cursor")
    try:
        uuid.UUID(payload[3])
    except ValueError as exc:
        raise InvalidLogCursor("invalid log cursor") from exc
    return LogCursor(
        attempt_number=payload[1],
        sequence=payload[2],
        log_id=payload[3],
    )


def redact_log_text(value: str) -> str:
    redacted = _URL_QUERY_PATTERN.sub(r"\1?[REDACTED]", value)
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", redacted)
    redacted = _JWT_PATTERN.sub("[REDACTED]", redacted)
    redacted = _RVC_TOKEN_PATTERN.sub("[REDACTED]", redacted)
    redacted = _AUTHORIZATION_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        redacted,
    )
    return _ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        redacted,
    )


def redact_log_fields(value: Any, *, _depth: int = 0) -> Any:
    if _depth >= _MAX_REDACTION_DEPTH:
        return "[REDACTED]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized_parts = [part for part in re.split(r"[^a-z0-9]+", key.casefold()) if part]
            normalized_key = "-".join(normalized_parts)
            if normalized_key in _SENSITIVE_KEY_PARTS or set(normalized_parts).intersection(
                _SENSITIVE_KEY_PARTS
            ):
                result[key] = "[REDACTED]"
            else:
                result[key] = redact_log_fields(item, _depth=_depth + 1)
        return result
    if isinstance(value, list):
        return [redact_log_fields(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return redact_log_text(value)
    return value


def job_log_to_read(record: JobLogRecord) -> JobLogRead:
    return JobLogRead(
        id=record.log.id,
        job_id=record.log.job_id,
        attempt_id=record.log.attempt_id,
        attempt_number=record.attempt_number,
        sequence=record.log.sequence,
        level=LogLevel(record.log.level),
        message=redact_log_text(record.log.message),
        fields=redact_log_fields(record.log.fields_json),
        occurred_at=record.log.occurred_at,
    )


def _filter_conditions(job_id: str, filters: JobLogFilters) -> list[Any]:
    conditions: list[Any] = [JobLog.job_id == job_id]
    if filters.attempt_id is not None:
        conditions.append(JobLog.attempt_id == filters.attempt_id)
    if filters.sequence_gte is not None:
        conditions.append(JobLog.sequence >= filters.sequence_gte)
    if filters.sequence_lte is not None:
        conditions.append(JobLog.sequence <= filters.sequence_lte)
    if filters.occurred_at_gte is not None:
        conditions.append(JobLog.occurred_at >= filters.occurred_at_gte)
    if filters.occurred_at_lte is not None:
        conditions.append(JobLog.occurred_at <= filters.occurred_at_lte)
    return conditions


def _after_condition(cursor: LogCursor) -> Any:
    return or_(
        JobAttempt.attempt_number > cursor.attempt_number,
        and_(
            JobAttempt.attempt_number == cursor.attempt_number,
            JobLog.sequence > cursor.sequence,
        ),
        and_(
            JobAttempt.attempt_number == cursor.attempt_number,
            JobLog.sequence == cursor.sequence,
            JobLog.id > cursor.log_id,
        ),
    )


async def count_job_logs(
    session: AsyncSession,
    *,
    job_id: str,
    filters: JobLogFilters,
) -> int:
    value = await session.scalar(
        select(func.count())
        .select_from(JobLog)
        .join(JobAttempt, JobAttempt.id == JobLog.attempt_id)
        .where(*_filter_conditions(job_id, filters))
    )
    return int(value or 0)


async def fetch_job_logs(
    session: AsyncSession,
    *,
    job_id: str,
    filters: JobLogFilters,
    after: LogCursor | None,
    limit: int,
    tail: bool,
) -> tuple[list[JobLogRecord], bool]:
    conditions = _filter_conditions(job_id, filters)
    if after is not None:
        conditions.append(_after_condition(after))
    statement = (
        select(JobLog, JobAttempt.attempt_number)
        .join(JobAttempt, JobAttempt.id == JobLog.attempt_id)
        .where(*conditions)
    )
    if tail:
        statement = statement.order_by(
            JobAttempt.attempt_number.desc(),
            JobLog.sequence.desc(),
            JobLog.id.desc(),
        )
    else:
        statement = statement.order_by(
            JobAttempt.attempt_number.asc(),
            JobLog.sequence.asc(),
            JobLog.id.asc(),
        )
    rows = list((await session.execute(statement.limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    records = [JobLogRecord(log=row[0], attempt_number=row[1]) for row in rows]
    if tail:
        records.reverse()
    return records, has_more
