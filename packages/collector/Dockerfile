# Prompt Shields collector image (ghcr.io/prompt-shields/prompt-shields-sdk/collector).
# Build from the repository root: docker build -f packages/collector/Dockerfile .
#
# Runtime contract (Kubernetes restricted Pod Security Standard):
# - runs as UID/GID 10001; every file is root-owned and world-readable, so an
#   arbitrary UID (OpenShift) works too;
# - works with a read-only root filesystem. Writable paths, if used: /tmp
#   (large multipart uploads spool there) and the fastembed model cache
#   (PS_EMBEDDINGS_CACHE_DIR; fastembed defaults to a directory under /tmp).
#   Mount an emptyDir at /tmp.
#
# Entrypoints (same image):
#   API (default CMD)  uvicorn collector.app:app --host 0.0.0.0 --port 8000
#   migrations         alembic -c packages/db/alembic.ini upgrade head  (WORKDIR /app)
#   RLS                python -m collector.rls enable
#   workers            python -m collector.workers   (see collector/workers.py)
#   jobs               python -m collector.<audit|retention|lifecycle|webhooks|...>

FROM python:3.11-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY packages/collector/pyproject.toml /src/packages/collector/
# Optional extras, comma-separated. Add local-embeddings
# (docker build --build-arg COLLECTOR_EXTRAS=local-embeddings) for
# PS_EMBEDDINGS_BACKEND=local; it is left out by default to keep the image
# small. The dev extras (test tools) are never needed at runtime.
ARG COLLECTOR_EXTRAS=""
# alembic runs the schema migrations (packages/db) from this same image.
RUN pip install --upgrade pip \
 && pip install "/src/packages/collector/${COLLECTOR_EXTRAS:+[${COLLECTOR_EXTRAS}]}" "alembic>=1.13"

FROM python:3.11-slim

RUN apt-get update \
 && apt-get upgrade -y \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
COPY packages/db/ packages/db/
COPY packages/collector/collector/ packages/collector/collector/
# Byte-compile at build time: the root filesystem may be read-only at runtime.
RUN python -m compileall -q /app/packages

# `db` must resolve as a package (collector does `from db.models import Tenant`),
# so /app/packages goes on the path, not /app/packages/db.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/packages:/app/packages/collector \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "collector.app:app", "--host", "0.0.0.0", "--port", "8000"]
