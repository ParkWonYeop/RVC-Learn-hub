# syntax=docker/dockerfile:1.7
FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build

COPY packages/contracts/pyproject.toml packages/contracts/pyproject.toml
COPY packages/contracts/src packages/contracts/src
COPY apps/worker/pyproject.toml apps/worker/pyproject.toml
COPY apps/worker/requirements.lock apps/worker/requirements.lock
COPY apps/worker/README.md apps/worker/README.md
COPY apps/worker/src apps/worker/src

RUN python -m pip wheel \
    --wheel-dir /wheels \
    --requirement ./apps/worker/requirements.lock \
    && python -m pip wheel \
    --no-deps \
    --wheel-dir /wheels \
    ./packages/contracts \
    ./apps/worker

FROM python:3.11-slim-bookworm AS runtime

ARG RVC_RELEASE_VERSION=dev
ARG RVC_SOURCE_COMMIT=uncommitted
LABEL org.opencontainers.image.version="${RVC_RELEASE_VERSION}" \
      org.opencontainers.image.revision="${RVC_SOURCE_COMMIT}"

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_ROOT=/var/lib/rvc-worker \
    RVC_RUNNER_MODE=fake

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates git tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 rvc-worker \
    && useradd --uid 10001 --gid 10001 --home-dir /var/lib/rvc-worker rvc-worker \
    && install -d -o rvc-worker -g rvc-worker -m 0700 /var/lib/rvc-worker

COPY --from=builder /wheels /wheels
RUN python -m pip install \
    --no-index \
    --find-links=/wheels \
    rvc-orchestrator-worker==0.1.0 \
    && rm -rf /wheels

USER 10001:10001
WORKDIR /var/lib/rvc-worker

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD ["python", "-m", "rvc_worker", "--check"]

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "rvc_worker"]
