from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, cast

import anyio
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rvc_orchestrator_contracts import utc_now

from ..database import Database
from ..dependencies import CurrentUserDep, SessionDep, SettingsDep, UserAuthDep
from ..models import JobAttempt, Metric, RevokedAccessToken, User
from ..schemas import JobLogList, MetricList, MetricRead
from ..services.authorization import require_job_owner_or_admin
from ..services.job_observability import (
    InvalidLogCursor,
    JobLogFilters,
    LogCursor,
    count_job_logs,
    decode_log_cursor,
    encode_log_cursor,
    fetch_job_logs,
    job_log_to_read,
)

router = APIRouter(tags=["job-observability"])

_READ_LIMIT_MAX = 500
_PRIVATE_NO_STORE = "private, no-store"


def _set_private_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = _PRIVATE_NO_STORE
    response.headers["Vary"] = "Authorization"
    response.headers["X-Content-Type-Options"] = "nosniff"


def _decode_cursor_or_422(value: str | None) -> LogCursor | None:
    if value is None:
        return None
    try:
        return decode_log_cursor(value)
    except InvalidLogCursor as exc:
        raise HTTPException(status_code=422, detail="invalid log cursor") from exc


def _aware_utc_or_422(value: datetime | None, *, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(status_code=422, detail=f"{field} must include a timezone")
    return value.astimezone(UTC)


def _validate_log_ranges(filters: JobLogFilters) -> None:
    if (
        filters.sequence_gte is not None
        and filters.sequence_lte is not None
        and filters.sequence_gte > filters.sequence_lte
    ):
        raise HTTPException(status_code=422, detail="sequence_gte must not exceed sequence_lte")
    if (
        filters.occurred_at_gte is not None
        and filters.occurred_at_lte is not None
        and filters.occurred_at_gte > filters.occurred_at_lte
    ):
        raise HTTPException(
            status_code=422,
            detail="occurred_at_gte must not exceed occurred_at_lte",
        )


@router.get("/jobs/{job_id}/logs", response_model=JobLogList)
async def list_job_logs(
    job_id: str,
    user: CurrentUserDep,
    session: SessionDep,
    response: Response,
    attempt_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    sequence_gte: Annotated[int | None, Query(ge=0)] = None,
    sequence_lte: Annotated[int | None, Query(ge=0)] = None,
    occurred_at_gte: datetime | None = None,
    occurred_at_lte: datetime | None = None,
    after: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    tail: bool = False,
    limit: Annotated[int, Query(ge=1, le=_READ_LIMIT_MAX)] = 100,
) -> JobLogList:
    await require_job_owner_or_admin(session, job_id=job_id, user=user)
    if tail and after is not None:
        raise HTTPException(status_code=422, detail="tail and after cannot be combined")
    filters = JobLogFilters(
        attempt_id=attempt_id,
        sequence_gte=sequence_gte,
        sequence_lte=sequence_lte,
        occurred_at_gte=_aware_utc_or_422(occurred_at_gte, field="occurred_at_gte"),
        occurred_at_lte=_aware_utc_or_422(occurred_at_lte, field="occurred_at_lte"),
    )
    _validate_log_ranges(filters)
    cursor = _decode_cursor_or_422(after)
    total = await count_job_logs(session, job_id=job_id, filters=filters)
    records, has_more = await fetch_job_logs(
        session,
        job_id=job_id,
        filters=filters,
        after=cursor,
        limit=limit,
        tail=tail,
    )
    next_cursor = encode_log_cursor(records[-1].cursor) if records else after
    _set_private_no_store(response)
    return JobLogList(
        items=[job_log_to_read(record) for record in records],
        total=total,
        limit=limit,
        has_more=has_more,
        next_cursor=next_cursor,
    )


def _metric_conditions(
    *,
    job_id: str,
    attempt_id: str | None,
    key: str | None,
    epoch: int | None,
    step: int | None,
) -> list[Any]:
    conditions: list[Any] = [Metric.job_id == job_id]
    if attempt_id is not None:
        conditions.append(Metric.attempt_id == attempt_id)
    if key is not None:
        conditions.append(Metric.key == key)
    if epoch is not None:
        conditions.append(Metric.epoch == epoch)
    if step is not None:
        conditions.append(Metric.step == step)
    return conditions


@router.get("/jobs/{job_id}/metrics", response_model=MetricList)
async def list_job_metrics(
    job_id: str,
    user: CurrentUserDep,
    session: SessionDep,
    response: Response,
    attempt_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    key: Annotated[
        str | None,
        Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$"),
    ] = None,
    epoch: Annotated[int | None, Query(ge=0)] = None,
    step: Annotated[int | None, Query(ge=0)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    tail: bool = False,
    limit: Annotated[int, Query(ge=1, le=_READ_LIMIT_MAX)] = 100,
) -> MetricList:
    await require_job_owner_or_admin(session, job_id=job_id, user=user)
    if tail and offset != 0:
        raise HTTPException(status_code=422, detail="tail and offset cannot be combined")
    conditions = _metric_conditions(
        job_id=job_id,
        attempt_id=attempt_id,
        key=key,
        epoch=epoch,
        step=step,
    )
    total = await session.scalar(select(func.count()).select_from(Metric).where(*conditions)) or 0
    statement = (
        select(Metric, JobAttempt.attempt_number)
        .join(JobAttempt, JobAttempt.id == Metric.attempt_id)
        .where(*conditions)
    )
    if tail:
        statement = statement.order_by(
            JobAttempt.attempt_number.desc(),
            Metric.sequence.desc(),
            Metric.id.desc(),
        ).limit(limit)
    else:
        statement = statement.order_by(
            JobAttempt.attempt_number.asc(),
            Metric.sequence.asc(),
            Metric.id.asc(),
        ).offset(offset).limit(limit)
    rows = list((await session.execute(statement)).all())
    if tail:
        rows.reverse()
    response_offset = max(int(total) - len(rows), 0) if tail else offset
    _set_private_no_store(response)
    return MetricList(
        items=[
            MetricRead(
                id=metric.id,
                job_id=metric.job_id,
                attempt_id=metric.attempt_id,
                attempt_number=attempt_number,
                sequence=metric.sequence,
                epoch=metric.epoch,
                step=metric.step,
                key=metric.key,
                value=metric.value,
                occurred_at=metric.occurred_at,
            )
            for metric, attempt_number in rows
        ],
        total=int(total),
        offset=response_offset,
        limit=limit,
    )


async def _stream_access_is_active(
    session: AsyncSession,
    *,
    user_id: str,
    token_jti: str,
    job_id: str,
) -> bool:
    if await session.get(RevokedAccessToken, token_jti) is not None:
        return False
    user = await session.get(User, user_id)
    if user is None or user.disabled:
        return False
    try:
        await require_job_owner_or_admin(session, job_id=job_id, user=user)
    except HTTPException:
        return False
    return True


@router.get("/jobs/{job_id}/logs/stream")
async def stream_job_logs(
    job_id: str,
    request: Request,
    auth: UserAuthDep,
    session: SessionDep,
    settings: SettingsDep,
    attempt_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    after: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    last_event_id: Annotated[
        str | None,
        Header(alias="Last-Event-ID", min_length=1, max_length=512),
    ] = None,
) -> StreamingResponse:
    await require_job_owner_or_admin(session, job_id=job_id, user=auth.user)
    if after is not None and last_event_id is not None and after != last_event_id:
        raise HTTPException(status_code=422, detail="after and Last-Event-ID must match")
    initial_cursor = _decode_cursor_or_422(after or last_event_id)
    token_remaining = (auth.claims.expires_at - utc_now()).total_seconds()
    max_duration = min(settings.log_stream_max_connection_seconds, token_remaining)
    if max_duration <= 0:
        raise HTTPException(status_code=401, detail="access token expired for stream")
    stream_user_id = auth.user.id
    stream_token_jti = auth.claims.jti
    database = cast(Database, request.app.state.database)
    filters = JobLogFilters(attempt_id=attempt_id)
    # UserAuthDep and SessionDep share the cached get_session dependency. Release
    # that request-scoped connection before the long-lived StreamingResponse starts;
    # every poll below uses its own short session.
    await session.close()

    async def event_source() -> AsyncIterator[str]:
        cursor = initial_cursor
        started = anyio.current_time()
        deadline = started + max_duration
        next_heartbeat = started + settings.log_stream_heartbeat_seconds
        while anyio.current_time() < deadline:
            if await request.is_disconnected():
                return
            async with database.session_factory() as polling_session:
                if not await _stream_access_is_active(
                    polling_session,
                    user_id=stream_user_id,
                    token_jti=stream_token_jti,
                    job_id=job_id,
                ):
                    return
                records, has_more = await fetch_job_logs(
                    polling_session,
                    job_id=job_id,
                    filters=filters,
                    after=cursor,
                    limit=settings.log_stream_batch_limit,
                    tail=False,
                )
            if records:
                for record in records:
                    cursor = record.cursor
                    encoded_cursor = encode_log_cursor(cursor)
                    data = json.dumps(
                        job_log_to_read(record).model_dump(mode="json"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    yield f"id: {encoded_cursor}\nevent: log\ndata: {data}\n\n"
                next_heartbeat = anyio.current_time() + settings.log_stream_heartbeat_seconds
                if has_more:
                    continue
            now = anyio.current_time()
            if now >= next_heartbeat:
                yield ": heartbeat\n\n"
                next_heartbeat = now + settings.log_stream_heartbeat_seconds
            sleep_seconds = min(
                settings.log_stream_poll_interval_seconds,
                max(0.0, deadline - anyio.current_time()),
                max(0.0, next_heartbeat - anyio.current_time()),
            )
            if sleep_seconds > 0:
                await anyio.sleep(sleep_seconds)

    headers = {
        "Cache-Control": "private, no-cache, no-store, must-revalidate",
        "Content-Security-Policy": "default-src 'none'",
        "Referrer-Policy": "no-referrer",
        "Vary": "Authorization",
        "X-Accel-Buffering": "no",
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers=headers,
    )
