FROM python:3.11-slim
WORKDIR /app
COPY packages/collector/pyproject.toml packages/collector/
COPY packages/db/ packages/db/
# Add local-embeddings (docker build --build-arg COLLECTOR_EXTRAS=dev,local-embeddings)
# for PS_EMBEDDINGS_BACKEND=local; it is left out by default to keep the image small.
ARG COLLECTOR_EXTRAS=dev
RUN pip install "packages/collector/[${COLLECTOR_EXTRAS}]"
COPY packages/collector/ packages/collector/
# `db` must resolve as a package (collector does `from db.models import Tenant`),
# so /app/packages goes on the path, not /app/packages/db.
ENV PYTHONPATH=/app/packages:/app/packages/collector
CMD ["uvicorn", "collector.app:app", "--host", "0.0.0.0", "--port", "8000"]
