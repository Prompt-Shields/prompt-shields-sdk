"""Operator trigger and CLI for the provider usage pull (platform signal).

There is no scheduler in the collector, so the pull runs on demand:

  - ``POST /api/v1/admin/provider-usage/pull`` with
    ``Authorization: Bearer $PS_ADMIN_TOKEN`` and ``{"tenant_id": ...}``
  - ``python -m collector.provider_usage_admin --tenant <uuid> [--days 7]``
    for cron / Kubernetes CronJob use

The HTTP route makes outbound calls with operator-held admin keys, so it is
an operator surface, not a tenant one: a tenant ingest key cannot trigger it.
With ``PS_ADMIN_TOKEN`` unset the route answers 404, as if absent.

Both call ``collector.provider_usage.pull_provider_usage``.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from db.models import Tenant
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings

from collector.provider_usage import SUPPORTED_PROVIDERS, pull_provider_usage

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])


class _OperatorSettings(BaseSettings):
    # Same PS_ADMIN_TOKEN as the /api/v1/operator routes (#85).
    admin_token: SecretStr = SecretStr("")

    model_config = {"env_prefix": "PS_", "extra": "ignore"}


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail,
        },
    )


async def require_operator(request: Request) -> None:
    """Constant-time check of ``Authorization: Bearer $PS_ADMIN_TOKEN``."""
    token = _OperatorSettings().admin_token.get_secret_value()
    if not token:
        raise _problem(404, "Not Found", "Not Found")
    auth = request.headers.get("Authorization", "")
    presented = auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""
    if not presented or not hmac.compare_digest(presented.encode(), token.encode()):
        raise _problem(401, "Unauthorized", "Invalid operator token")


class ProviderUsagePullBody(BaseModel):
    tenant_id: UUID
    days: int = Field(default=7, ge=1, le=90)
    providers: list[str] | None = None


@router.post(
    "/provider-usage/pull",
    summary="Pull org usage from provider admin APIs (operator)",
    dependencies=[Depends(require_operator)],
)
async def pull_provider_usage_endpoint(
    body: ProviderUsagePullBody, request: Request
) -> dict:
    db = request.state.db
    if await db.get(Tenant, body.tenant_id) is None:
        raise _problem(404, "Not Found", "Tenant not found")
    end = datetime.now(UTC)
    return await pull_provider_usage(
        db,
        body.tenant_id,
        start=end - timedelta(days=body.days),
        end=end,
        providers=body.providers,
    )


async def _main(tenant: UUID, days: int, providers: list[str] | None) -> dict:
    from collector.app import SessionLocal, engine

    end = datetime.now(UTC)
    try:
        async with SessionLocal() as db:
            return await pull_provider_usage(
                db,
                tenant,
                start=end - timedelta(days=days),
                end=end,
                providers=providers,
            )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", type=UUID, required=True)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--provider", action="append", choices=SUPPORTED_PROVIDERS)
    args = parser.parse_args(argv)
    print(
        json.dumps(asyncio.run(_main(args.tenant, args.days, args.provider)), indent=2)
    )


if __name__ == "__main__":
    main()
