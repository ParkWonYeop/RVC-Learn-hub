FROM python:3.13-slim AS runtime

ARG RVC_RELEASE_VERSION=dev
ARG RVC_SOURCE_COMMIT=uncommitted
LABEL org.opencontainers.image.version="${RVC_RELEASE_VERSION}" \
      org.opencontainers.image.revision="${RVC_SOURCE_COMMIT}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid 10001 rvc \
    && useradd --no-log-init \
      --uid 10001 \
      --gid 10001 \
      --home-dir /nonexistent \
      --no-create-home \
      --shell /usr/sbin/nologin \
      rvc

COPY packages/contracts /build/contracts
COPY apps/api /build/api
RUN python -m pip install -r /build/api/requirements.lock \
    && python -m pip install --no-deps /build/contracts /build/api \
    && rm -rf /build

COPY --chown=rvc:rvc apps/api/alembic.ini /app/alembic.ini
COPY --chown=rvc:rvc apps/api/alembic /app/alembic

USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "rvc_manager_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
