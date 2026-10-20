"""Operator trigger, schedule and CLI for the provider usage pull (platform signal).

Ways to run it:

  - ``POST /api/v1/admin/provider-usage/pull`` with
    ``Authorization: Bearer $PS_ADMIN_TOKEN`` and ``{"tenant_id": ...}``
  - ``python -m collector.provider_usage_admin --tenant <uuid> [--days 7]``,
    one tenant
  - ``python -m collector.provider_usage_admin --all-tenants [--days 3]``,
    every tenant with a stored credential or a ``PS_PROVIDER_ADMIN_KEYS``
    entry, for cron / Kubernetes CronJob use (add ``--loop --interval
    86400`` to keep running)
  - in process: ``PS_PROVIDER_USAGE_PULL_INTERVAL_SECONDS`` > 0 runs the
    all-tenants pull from the collector's lifespan, like the webhook worker.
    Off by default (see ``collector.config``).

The all-tenants pull lists tenants through a logged RLS bypass, then pulls
each one in its own session under that tenant's RLS context. A run holds a
PostgreSQL advisory lock, so when several replicas (or a replica and the
CLI) start one at the same time, only one pulls and the others skip that
run. Pulls are idempotent, so runs that follow each other only cost extra
vendor API calls; enable the in-process schedule on one replica.

The HTTP route makes outbound calls with the tenant's admin keys (stored, or
operator-held in PS_PROVIDER_ADMIN_KEYS), so it is
an operator surface, not a tenant one: a tenant ingest key cannot trigger it.
With ``PS_ADMIN_TOKEN`` unset the route answers 404, as if absent.

Both call ``collector.provider_usage.pull_provider_usage``.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from db.models import ProviderCredential, Tenant
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings
from sqlalchemy import select, text

from collector import audit_trail, rls
from collector.provider_usage import (
    SUPPORTED_PROVIDERS,
    ProviderUsageSettings,
    admin_keys_for_tenant,
    pull_provider_usage,
)

logger = logging.getLogger("prompt_shields.provider_usage")

# Reason logged when the scheduled pull lists tenants across RLS.
SCHEDULE_BYPASS_REASON = "provider-usage-schedule"
# PostgreSQL advisory lock key held for an all-tenants run: any fixed bigint
# no other code uses; this one is "PSPUSAGE" in ASCII.
SCHEDULE_LOCK_KEY = 0x5053_5055_5341_4745

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
    """Constant-time check of ``Authorization: Bearer $PS_ADMIN_TOKEN``.

    Audit trail (system chain): ``operator.access`` for every authorised
    call (route and method), ``auth.failure`` (rate-limited) otherwise.
    """
    token = _OperatorSettings().admin_token.get_secret_value()
    if not token:
        raise _problem(404, "Not Found", "Not Found")
    auth = request.headers.get("Authorization", "")
    presented = auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""
    if not presented or not hmac.compare_digest(presented.encode(), token.encode()):
        await audit_trail.record_auth_failure(
            request,
            realm="operator",
            reason="invalid_credentials" if presented else "missing_credentials",
            status_code=401,
        )
        raise _problem(401, "Unauthorized", "Invalid operator token")
    await audit_trail.record_system_event(
        action="operator.access",
        actor=audit_trail.OPERATOR,
        details={
            "route": audit_trail.route_template(request),
            "method": request.method,
        },
    )


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
    # One tenant per pull: scope the session to it instead of bypassing RLS.
    await rls.set_tenant(db, body.tenant_id)
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


# ──────────────────────────────────────────────────────────────────────
# Scheduled pull: every tenant with credentials
# ──────────────────────────────────────────────────────────────────────


def list_session_factory(session_factory):
    """Sessions that bypass RLS, used only to list the tenants to pull.
    The bypass is logged once, when this is called."""
    info = rls.bypass_info(SCHEDULE_BYPASS_REASON)
    return lambda: session_factory(info=info)


async def tenants_to_pull(
    list_factory, settings: ProviderUsageSettings | None = None
) -> list[UUID]:
    """Existing tenants with a stored provider credential or a
    PS_PROVIDER_ADMIN_KEYS entry, sorted."""
    settings = settings or ProviderUsageSettings()
    candidates: set[UUID] = set()
    for raw in settings.provider_admin_keys:
        try:
            tenant_id = UUID(raw)
        except ValueError:
            logger.warning(
                "PS_PROVIDER_ADMIN_KEYS: ignoring an entry whose key is not a tenant uuid"
            )
            continue
        if admin_keys_for_tenant(tenant_id, settings):
            candidates.add(tenant_id)
    async with list_factory() as db:
        candidates.update(
            (
                await db.execute(select(ProviderCredential.tenant_id).distinct())
            ).scalars()
        )
        if not candidates:
            return []
        existing = (
            await db.execute(select(Tenant.id).where(Tenant.id.in_(candidates)))
        ).scalars()
        return sorted(existing, key=str)


async def pull_all_tenants(
    session_factory,
    *,
    days: int,
    list_factory=None,
    settings: ProviderUsageSettings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: datetime | None = None,
) -> dict:
    """Pull the last ``days`` days for every tenant with credentials.

    Each tenant is pulled in its own session under its own RLS context. A
    tenant that fails is reported and does not stop the others.
    """
    if not 1 <= days <= 90:
        raise ValueError("days must be between 1 and 90")
    settings = settings or ProviderUsageSettings()
    list_factory = list_factory or list_session_factory(session_factory)
    end = now or datetime.now(UTC)
    start = end - timedelta(days=days)
    summary = {"start": start.isoformat(), "end": end.isoformat(), "tenants": {}}
    # The lock is transaction-scoped: it is released when lock_db closes,
    # including on error or cancellation. It touches no table, so the
    # session needs no tenant context.
    async with session_factory() as lock_db:
        locked = (
            await lock_db.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": SCHEDULE_LOCK_KEY},
            )
        ).scalar_one()
        if not locked:
            logger.info(
                "scheduled provider usage pull skipped: another run holds the lock"
            )
            return {**summary, "skipped": "another provider usage pull is running"}
        summary["tenants"] = await _pull_tenants(
            session_factory,
            list_factory=list_factory,
            settings=settings,
            start=start,
            end=end,
            transport=transport,
            sleep=sleep,
        )
    return summary


async def _pull_tenants(
    session_factory,
    *,
    list_factory,
    settings: ProviderUsageSettings,
    start: datetime,
    end: datetime,
    transport: httpx.AsyncBaseTransport | None,
    sleep: Callable[[float], Awaitable[None]],
) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for tenant_id in await tenants_to_pull(list_factory, settings):
        try:
            async with session_factory() as db:
                await rls.set_tenant(db, tenant_id)
                results[str(tenant_id)] = await pull_provider_usage(
                    db,
                    tenant_id,
                    start=start,
                    end=end,
                    settings=settings,
                    transport=transport,
                    sleep=sleep,
                )
        except Exception as exc:  # noqa: BLE001 - one tenant must not stop the rest
            # The exception class only: a message could quote a provider reply.
            logger.error(
                "scheduled provider usage pull failed for tenant %s: %s",
                tenant_id,
                type(exc).__name__,
            )
            results[str(tenant_id)] = {
                "tenant_id": str(tenant_id),
                "error": type(exc).__name__,
            }
    return results


async def run_schedule(
    session_factory,
    interval: float,
    days: int,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    **pull_kwargs,
) -> None:
    """Pull every tenant, then wait ``interval`` seconds; forever."""
    list_factory = list_session_factory(session_factory)
    while True:
        try:
            summary = await pull_all_tenants(
                session_factory, days=days, list_factory=list_factory, **pull_kwargs
            )
            if "skipped" not in summary:
                logger.info(
                    "scheduled provider usage pull: %d tenant(s)",
                    len(summary["tenants"]),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed run must not end the schedule
            logger.error("scheduled provider usage pull failed: %s", type(exc).__name__)
        await sleep(interval)


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────


async def _main(tenant: UUID, days: int, providers: list[str] | None) -> dict:
    from collector.app import SessionLocal, engine

    end = datetime.now(UTC)
    try:
        async with SessionLocal() as db:
            await rls.set_tenant(db, tenant)
            return await pull_provider_usage(
                db,
                tenant,
                start=end - timedelta(days=days),
                end=end,
                providers=providers,
            )
    finally:
        await engine.dispose()


async def _main_all(days: int, loop: bool, interval: float) -> dict | None:
    from collector.app import SessionLocal, engine

    try:
        if loop:
            await run_schedule(SessionLocal, interval, days)
            return None
        return await pull_all_tenants(SessionLocal, days=days)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--tenant", type=UUID)
    target.add_argument(
        "--all-tenants",
        action="store_true",
        help="every tenant with a stored credential or a PS_PROVIDER_ADMIN_KEYS entry",
    )
    parser.add_argument(
        "--days", type=int, default=None, help="default 7; 3 with --all-tenants"
    )
    parser.add_argument("--provider", action="append", choices=SUPPORTED_PROVIDERS)
    parser.add_argument(
        "--loop",
        action="store_true",
        help="with --all-tenants: pull again every --interval seconds",
    )
    parser.add_argument("--interval", type=float, default=86400.0)
    args = parser.parse_args(argv)
    if args.all_tenants:
        if args.provider:
            parser.error("--provider applies to --tenant only")
        days = args.days if args.days is not None else 3
        if not 1 <= days <= 90:
            parser.error("--days must be between 1 and 90")
        if args.loop and args.interval <= 0:
            parser.error("--interval must be above 0")
        result = asyncio.run(_main_all(days, args.loop, args.interval))
    else:
        if args.loop:
            parser.error("--loop needs --all-tenants")
        days = args.days if args.days is not None else 7
        result = asyncio.run(_main(args.tenant, days, args.provider))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
