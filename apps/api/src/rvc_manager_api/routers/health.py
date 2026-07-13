from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..schemas import HealthResponse, ReadinessResponse

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    return HealthResponse(status="ok", service=request.app.state.settings.app_name)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readiness(request: Request) -> ReadinessResponse | JSONResponse:
    checks: dict[str, str] = {}
    ready = True
    try:
        async with request.app.state.database.session_factory() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"
        ready = False

    settings = request.app.state.settings
    if settings.readiness_check_redis or settings.environment == "production":
        if not settings.redis_url:
            checks["redis"] = "not_configured"
            ready = False
        else:
            try:
                from redis.asyncio import Redis

                client = Redis.from_url(settings.redis_url)
                async with asyncio.timeout(2):
                    await client.ping()
                await client.aclose()
                checks["redis"] = "ok"
            except Exception:
                checks["redis"] = "unavailable"
                ready = False
    else:
        checks["redis"] = "disabled"

    if settings.rq_enabled:
        probe = request.app.state.rq_readiness
        if probe is None:
            checks["rq_worker"] = "not_configured"
            ready = False
        else:
            rq_status, rq_ready = await probe.readiness()
            checks["rq_worker"] = rq_status
            ready = ready and rq_ready
    elif settings.environment == "production":
        checks["rq_worker"] = "not_configured"
        ready = False
    else:
        checks["rq_worker"] = "disabled"

    if settings.rq_enabled and settings.maintenance_reconcile_enabled:
        reconciler = request.app.state.maintenance_reconciler
        if reconciler is None:
            checks["maintenance_reconciler"] = "not_configured"
            ready = False
        else:
            reconcile_status, reconcile_ready = reconciler.readiness()
            checks["maintenance_reconciler"] = reconcile_status
            ready = ready and reconcile_ready
    elif settings.environment == "production":
        checks["maintenance_reconciler"] = "not_configured"
        ready = False
    else:
        checks["maintenance_reconciler"] = "disabled"

    if settings.artifact_cleanup_reconcile_enabled:
        artifact_cleanup_reconciler = request.app.state.artifact_cleanup_reconciler
        if artifact_cleanup_reconciler is None:
            checks["artifact_cleanup_reconciler"] = "not_configured"
            if settings.environment == "production":
                ready = False
        else:
            cleanup_status, cleanup_ready = artifact_cleanup_reconciler.readiness()
            checks["artifact_cleanup_reconciler"] = cleanup_status
            # Test clients may intentionally bypass ASGI lifespan. Production
            # always gates on this reconciler; non-production gates once its
            # background task has actually started.
            if settings.environment == "production" or artifact_cleanup_reconciler.running:
                ready = ready and cleanup_ready
    else:
        checks["artifact_cleanup_reconciler"] = "disabled"
        if settings.environment == "production":
            ready = False

    mlflow_status, mlflow_ready = await request.app.state.mlflow.readiness()
    checks["mlflow"] = mlflow_status
    ready = ready and mlflow_ready

    payload = ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)
    if ready:
        return payload
    return JSONResponse(status_code=503, content=payload.model_dump(mode="json"))
