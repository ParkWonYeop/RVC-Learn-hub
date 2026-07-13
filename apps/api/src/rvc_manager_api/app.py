from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .body_limits import (
    BoundedExperimentBodyMiddleware,
    BoundedSampleRegistrationBodyMiddleware,
    BoundedUserLifecycleBodyMiddleware,
    BoundedWorkerTelemetryBodyMiddleware,
)
from .config import Settings
from .database import Database
from .maintenance_queue import RqMaintenanceQueue, RqReadinessProbe
from .rate_limit import (
    RateLimitDecision,
    RateLimiterUnavailable,
    RedisRateLimiter,
    request_rate_limit_identity,
    reset_after_seconds,
    rule_for_request,
)
from .routers import (
    artifact_router,
    auth_router,
    dataset_router,
    health_router,
    job_observability_router,
    maintenance_router,
    manager_router,
    model_registry_router,
    test_set_router,
    user_router,
    worker_router,
)
from .services.artifact_cleanup import ArtifactCleanupReconciler
from .services.maintenance import MaintenanceReconciler
from .services.mlflow import (
    MlflowProjectionRequired,
    create_mlflow_coordinator,
)
from .storage import create_storage_adapter

LOGGER = logging.getLogger("rvc_manager_api.http")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings()
    if resolved_settings.process_role != "api":
        raise ValueError("the HTTP application requires PROCESS_ROLE=api")
    database = Database(resolved_settings)
    storage = create_storage_adapter(resolved_settings)
    mlflow = create_mlflow_coordinator(resolved_settings, database)
    rate_limiter = (
        RedisRateLimiter.from_settings(resolved_settings)
        if resolved_settings.rate_limit_enabled
        else None
    )
    maintenance_queue = (
        RqMaintenanceQueue(resolved_settings) if resolved_settings.rq_enabled else None
    )
    rq_readiness = RqReadinessProbe(resolved_settings) if resolved_settings.rq_enabled else None
    maintenance_reconciler = (
        MaintenanceReconciler(database, maintenance_queue, resolved_settings)
        if maintenance_queue is not None and resolved_settings.maintenance_reconcile_enabled
        else None
    )
    artifact_cleanup_reconciler = (
        ArtifactCleanupReconciler(database, storage, resolved_settings)
        if resolved_settings.artifact_cleanup_reconcile_enabled
        else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        mlflow_task: asyncio.Task[None] | None = None
        maintenance_reconcile_task: asyncio.Task[None] | None = None
        artifact_cleanup_task: asyncio.Task[None] | None = None
        if resolved_settings.auto_create_schema:
            await database.create_all()
        if mlflow.enabled:
            mlflow_task = asyncio.create_task(mlflow.run(), name="mlflow-outbox-projector")
        if maintenance_reconciler is not None:
            maintenance_reconcile_task = asyncio.create_task(
                maintenance_reconciler.run(),
                name="maintenance-ledger-reconciler",
            )
        if artifact_cleanup_reconciler is not None:
            artifact_cleanup_task = asyncio.create_task(
                artifact_cleanup_reconciler.run(),
                name="artifact-cleanup-reconciler",
            )
        try:
            yield
        finally:
            if artifact_cleanup_reconciler is not None:
                artifact_cleanup_reconciler.stop()
            if artifact_cleanup_task is not None:
                await artifact_cleanup_task
            if maintenance_reconciler is not None:
                maintenance_reconciler.stop()
            if maintenance_reconcile_task is not None:
                await maintenance_reconcile_task
            mlflow.stop()
            if mlflow_task is not None:
                await mlflow_task
            await mlflow.close()
            if rate_limiter is not None:
                await rate_limiter.close()
            if rq_readiness is not None:
                await rq_readiness.close()
            if maintenance_queue is not None:
                await maintenance_queue.close()
            await storage.close()
            await database.dispose()

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.database = database
    app.state.storage = storage
    app.state.mlflow = mlflow
    app.state.rate_limiter = rate_limiter
    app.state.maintenance_queue = maintenance_queue
    app.state.rq_readiness = rq_readiness
    app.state.maintenance_reconciler = maintenance_reconciler
    app.state.artifact_cleanup_reconciler = artifact_cleanup_reconciler
    app.state.sample_verification_semaphore = asyncio.Semaphore(
        resolved_settings.sample_verification_max_concurrency
    )
    app.add_middleware(
        BoundedSampleRegistrationBodyMiddleware,
        api_prefix=resolved_settings.api_prefix,
        max_bytes=resolved_settings.sample_registration_json_max_bytes,
    )
    app.add_middleware(
        BoundedExperimentBodyMiddleware,
        api_prefix=resolved_settings.api_prefix,
        max_bytes=resolved_settings.experiment_json_max_bytes,
    )
    app.add_middleware(
        BoundedUserLifecycleBodyMiddleware,
        api_prefix=resolved_settings.api_prefix,
        max_bytes=resolved_settings.user_lifecycle_json_max_bytes,
    )
    app.add_middleware(
        BoundedWorkerTelemetryBodyMiddleware,
        api_prefix=resolved_settings.api_prefix,
        max_bytes=resolved_settings.worker_telemetry_json_max_bytes,
    )

    @app.exception_handler(MlflowProjectionRequired)
    async def mlflow_projection_required(
        _: Request,
        exc: MlflowProjectionRequired,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            headers={
                "Cache-Control": "no-store",
                "Retry-After": str(max(1, int(resolved_settings.mlflow_sync_interval_seconds))),
            },
            content={
                "detail": {
                    "code": "mlflow_projection_deferred",
                    "ledger_committed": True,
                    "resource_type": exc.aggregate_type,
                    "resource_id": exc.aggregate_id,
                }
            },
        )

    if resolved_settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved_settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        )

    @app.middleware("http")
    async def request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        supplied_request_id = request.headers.get("X-Request-ID", "")
        request_id = (
            supplied_request_id if _REQUEST_ID.fullmatch(supplied_request_id) else str(uuid.uuid4())
        )
        request.state.correlation_id = request_id
        started = time.perf_counter()
        response: Response | None = None
        decision: RateLimitDecision | None = None
        rule = rule_for_request(request, resolved_settings)
        if rule is not None and app.state.rate_limiter is not None:
            try:
                decision = await app.state.rate_limiter.check(
                    request_rate_limit_identity(request),
                    rule,
                )
            except RateLimiterUnavailable:
                LOGGER.exception(
                    "rate-limit backend unavailable",
                    extra={
                        "request_id": request_id,
                        "method": request.method,
                        "path": request.url.path,
                        "status_code": 503,
                    },
                )
                if resolved_settings.rate_limit_fail_closed:
                    response = JSONResponse(
                        status_code=503,
                        content={"detail": "request rate limiter is unavailable"},
                    )
            if decision is not None and not decision.allowed:
                response = JSONResponse(
                    status_code=429,
                    content={"detail": "request rate limit exceeded"},
                    headers={"Retry-After": reset_after_seconds(decision)},
                )
        try:
            if response is None:
                response = await call_next(request)
        except Exception:
            LOGGER.exception(
                "request failed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": 500,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                },
            )
            raise
        if decision is not None:
            response.headers["RateLimit-Limit"] = str(decision.limit)
            response.headers["RateLimit-Remaining"] = str(decision.remaining)
            response.headers["RateLimit-Reset"] = reset_after_seconds(decision)
        sample_download_prefix = f"{resolved_settings.api_prefix}/samples/"
        if (
            request.method == "GET"
            and request.url.path.startswith(sample_download_prefix)
            and request.url.path.endswith("/download")
        ):
            # Starlette's FileResponse builds 416 responses inside its ASGI
            # call and does not retain the route-supplied cache headers. Keep
            # every authenticated Sample download outcome private, including
            # invalid/unsatisfied Range requests and owner-hiding 404s.
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Vary"] = "Authorization"
        experiment_prefix = f"{resolved_settings.api_prefix}/experiments/"
        if request.url.path.startswith(experiment_prefix):
            experiment_parts = request.url.path[len(experiment_prefix) :].split("/")
            if (
                len(experiment_parts) >= 2
                and bool(experiment_parts[0])
                and experiment_parts[1] == "model-registry"
            ):
                # Authentication, validation and body-limit failures occur
                # before route-level headers are available. Keep every model
                # registry outcome private at the outer response boundary.
                response.headers["Cache-Control"] = "private, no-store"
                response.headers["Vary"] = "Authorization"
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        if (
            resolved_settings.environment == "production"
            and resolved_settings.public_scheme == "https"
        ):
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        LOGGER.info(
            "request completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            },
        )
        return response

    app.include_router(health_router)
    app.include_router(auth_router, prefix=resolved_settings.api_prefix)
    app.include_router(user_router, prefix=resolved_settings.api_prefix)
    app.include_router(dataset_router, prefix=resolved_settings.api_prefix)
    app.include_router(manager_router, prefix=resolved_settings.api_prefix)
    app.include_router(model_registry_router, prefix=resolved_settings.api_prefix)
    app.include_router(job_observability_router, prefix=resolved_settings.api_prefix)
    app.include_router(maintenance_router, prefix=resolved_settings.api_prefix)
    app.include_router(test_set_router, prefix=resolved_settings.api_prefix)
    app.include_router(artifact_router, prefix=resolved_settings.api_prefix)
    app.include_router(worker_router, prefix=resolved_settings.api_prefix)
    return app
