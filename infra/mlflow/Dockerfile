ARG MLFLOW_BASE_IMAGE=ghcr.io/mlflow/mlflow:v3.1.1
FROM ${MLFLOW_BASE_IMAGE}

ARG RVC_RELEASE_VERSION=dev
ARG RVC_SOURCE_COMMIT=uncommitted
LABEL org.opencontainers.image.version="${RVC_RELEASE_VERSION}" \
      org.opencontainers.image.revision="${RVC_SOURCE_COMMIT}"

COPY infra/mlflow/requirements.lock /tmp/rvc-mlflow-requirements.lock
RUN python -m pip install --no-cache-dir \
      --no-deps \
      --only-binary=:all: \
      --requirement /tmp/rvc-mlflow-requirements.lock \
    && rm -f /tmp/rvc-mlflow-requirements.lock

RUN groupadd --gid 10002 rvc-mlflow \
    && useradd --no-log-init \
      --uid 10002 \
      --gid 10002 \
      --home-dir /home/rvc-mlflow \
      --create-home \
      --shell /usr/sbin/nologin \
      rvc-mlflow

ENV HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10002:10002
WORKDIR /home/rvc-mlflow
