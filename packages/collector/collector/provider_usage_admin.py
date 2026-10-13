"""Admin trigger and CLI for the provider usage pull (platform signal).

There is no scheduler in the collector, so the pull runs on demand:

  - ``POST /api/v1/admin/provider-usage/pull`` for the calling tenant
  - ``python -m collector.provider_usage_admin --tenant <uuid> [--days 7]``
    for cron / Kubernetes CronJob use

Both call ``collector.provider_usage.pull_provider_usage``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from collector.auth import resolve_tenant
from collector.provider_usage import SUPPORTED_PROVIDERS, pull_provider_usage

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])


class ProviderUsagePullBody(BaseModel):
    days: int = Field(default=7, ge=1, le=90)
    providers: list[str] | None = None


@router.post("/provider-usage/pull", summary="Pull org usage from provider admin APIs")
async def pull_provider_usage_endpoint(
    body: ProviderUsagePullBody, request: Request
) -> dict:
    tenant_id = await resolve_tenant(request)
    end = datetime.now(UTC)
    return await pull_provider_usage(
        request.state.db,
        tenant_id,
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
