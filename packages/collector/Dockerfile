FROM python:3.11-slim
WORKDIR /app
COPY packages/collector/pyproject.toml packages/collector/
COPY packages/db/ packages/db/
RUN pip install packages/collector/[dev]
COPY packages/collector/ packages/collector/
# `db` must resolve as a package (collector does `from db.models import Tenant`),
# so /app/packages goes on the path, not /app/packages/db.
ENV PYTHONPATH=/app/packages:/app/packages/collector
CMD ["uvicorn", "collector.app:app", "--host", "0.0.0.0", "--port", "8000"]
