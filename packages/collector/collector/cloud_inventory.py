"""Platform signal: AI model deployments inventoried from cloud accounts.

A model deployment in a cloud account is an AI asset whether or not any
traffic to it is instrumented. This module reads deployments, read-only,
from the cloud control plane and records each one as an ``AIAsset`` with
``discovery_source=["platform_signal"]`` (``medium`` confidence until
another source corroborates it), its ``region`` for sovereignty reporting,
and owner / environment / business unit taken from resource tags.

Supported so far:

  - Azure: Azure OpenAI and AI Foundry (``AIServices``) accounts and their
    model deployments, via Azure Resource Manager.

Each asset's ``calling_service`` is ``azure:<ARM deployment resource id>``,
which marks where it came from. Asset ids are UUIDv5 values derived from
the tenant and that resource id, so re-runs update the same rows.

Run it with ``POST /api/v1/admin/cloud-inventory/pull`` (operator only:
``Authorization: Bearer $PS_ADMIN_TOKEN``, ``{"tenant_id": ...}``; 404 when
the token is unset) or ``python -m collector.cloud_inventory --tenant <uuid>``.

Credentials come from the operator's environment only, per tenant, in
``PS_CLOUD_INVENTORY`` (JSON, see ``CloudInventorySettings``); never from a
request body. Secrets are held as ``SecretStr`` and are never logged or
returned. HTTP calls go only to the fixed Microsoft hosts below; ARM
``nextLink`` URLs and resource ids are checked before they are followed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import httpx
from db.models import AIAsset, Tenant
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, SecretStr, field_validator
from pydantic_settings import BaseSettings
from sqlalchemy.ext.asyncio import AsyncSession

from collector import rls
from collector.dedup import compute_confidence
from collector.lifecycle import reactivate
from collector.provider_usage_admin import require_operator

logger = logging.getLogger("prompt_shields.cloud_inventory")

SOURCE = "platform_signal"
SUPPORTED_CLOUDS = ("azure",)

# Fixed hosts. Never derived from tenant input.
AZURE_LOGIN_HOST = "login.microsoftonline.com"
AZURE_ARM_HOST = "management.azure.com"
AZURE_ARM_SCOPE = "https://management.azure.com/.default"
ARM_SUBSCRIPTIONS_API = "2022-12-01"
ARM_COGNITIVE_API = "2024-10-01"
# Account kinds that host model deployments.
AZURE_AI_KINDS = {"openai", "aiservices"}
# Deployments in these states are not serving and are not inventoried.
INACTIVE_STATES = {"failed", "deleting", "deleted", "canceled"}

MAX_PAGES = 200
MAX_RETRIES = 4
MAX_RETRY_WAIT_S = 60.0

_NAMESPACE = uuid.UUID("2c0f7a91-5b3e-5d8c-8e41-7a6b9d2f0c13")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_DIRECTORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_ACCOUNT_ID = re.compile(
    r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/"
    r"Microsoft\.CognitiveServices/accounts/[^/]+$",
    re.IGNORECASE,
)

OWNER_TAGS = ("owner", "owner_email", "owneremail", "owner-email", "contact")
ENVIRONMENT_TAGS = ("environment", "env")
BUSINESS_UNIT_TAGS = ("business_unit", "businessunit", "business-unit", "department")
_ENVIRONMENTS = {
    "production": "production",
    "prod": "production",
    "prd": "production",
    "staging": "staging",
    "stage": "staging",
    "stg": "staging",
    "dev": "dev",
    "development": "dev",
    "test": "dev",
}


class AzureInventoryCredentials(BaseModel):
    """A read-only service principal (``Reader`` role, see the guide)."""

    directory_id: str  # Microsoft Entra tenant id or domain
    client_id: str
    client_secret: SecretStr
    # Empty: every subscription the principal can read.
    subscription_ids: list[str] = []

    @field_validator("directory_id")
    @classmethod
    def _directory(cls, v: str) -> str:
        if not _DIRECTORY.match(v):
            raise ValueError("directory_id must be a tenant id or domain")
        return v

    @field_validator("subscription_ids")
    @classmethod
    def _subscriptions(cls, v: list[str]) -> list[str]:
        if not all(_GUID.match(s) for s in v):
            raise ValueError("subscription_ids must be GUIDs")
        return v


class TenantCloudCredentials(BaseModel):
    azure: AzureInventoryCredentials | None = None


class CloudInventorySettings(BaseSettings):
    """``PS_CLOUD_INVENTORY='{"<tenant uuid>": {"azure": {"directory_id": ...,
    "client_id": ..., "client_secret": ..., "subscription_ids": [...]}}}'``"""

    cloud_inventory: dict[str, TenantCloudCredentials] = {}

    # A malformed value must not echo the client secret in the error.
    model_config = {
        "env_prefix": "PS_",
        "extra": "ignore",
        "hide_input_in_errors": True,
    }


def credentials_for_tenant(
    tenant_id: UUID, settings: CloudInventorySettings | None = None
) -> TenantCloudCredentials:
    settings = settings or CloudInventorySettings()
    return settings.cloud_inventory.get(str(tenant_id)) or TenantCloudCredentials()


class CloudInventoryError(Exception):
    """A cloud call failed. The message never contains credentials."""


@dataclass(frozen=True)
class CloudDeployment:
    cloud: str
    resource_id: str
    region: str | None
    model: str | None
    model_format: str | None
    tags: dict[str, str] = field(default_factory=dict)


Sleep = Callable[[float], Awaitable[None]]


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), MAX_RETRY_WAIT_S))
    except ValueError:
        return None


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    what: str,
    sleep: Sleep,
    **kwargs,
) -> dict:
    """Request with retry on 429/5xx, honouring Retry-After."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt == MAX_RETRIES:
                raise CloudInventoryError(
                    f"azure: {what}: transport error ({type(exc).__name__})"
                )
            await sleep(min(2.0**attempt, MAX_RETRY_WAIT_S))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES:
                break
            await sleep(_retry_after(resp) or min(2.0**attempt, MAX_RETRY_WAIT_S))
            continue
        if resp.status_code != 200:
            # The body is not echoed: token errors can quote the request.
            raise CloudInventoryError(f"azure: {what} returned HTTP {resp.status_code}")
        return resp.json()
    raise CloudInventoryError(
        f"azure: {what} still failing after {MAX_RETRIES} retries"
    )


def _arm_url(next_link: str) -> str:
    """Only follow ``nextLink`` back to ARM over https, so the bearer token
    is never sent to another host."""
    url = httpx.URL(next_link)
    if url.scheme != "https" or url.host != AZURE_ARM_HOST:
        raise CloudInventoryError("azure: unexpected nextLink host")
    return str(url)


async def _arm_list(
    client: httpx.AsyncClient,
    path: str,
    api_version: str,
    headers: dict[str, str],
    what: str,
    sleep: Sleep,
):
    url: str | None = f"https://{AZURE_ARM_HOST}{path}"
    params: dict | None = {"api-version": api_version}
    for _ in range(MAX_PAGES):
        body = await _request(
            client, "GET", url, what=what, sleep=sleep, params=params, headers=headers
        )
        for item in body.get("value") or []:
            yield item
        next_link = body.get("nextLink")
        if not next_link:
            return
        url, params = _arm_url(next_link), None
    raise CloudInventoryError(f"azure: {what}: more than {MAX_PAGES} pages")


async def _azure_token(
    client: httpx.AsyncClient, creds: AzureInventoryCredentials, sleep: Sleep
) -> str:
    body = await _request(
        client,
        "POST",
        f"https://{AZURE_LOGIN_HOST}/{creds.directory_id}/oauth2/v2.0/token",
        what="token request",
        sleep=sleep,
        data={
            "grant_type": "client_credentials",
            "client_id": creds.client_id,
            "client_secret": creds.client_secret.get_secret_value(),
            "scope": AZURE_ARM_SCOPE,
        },
    )
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise CloudInventoryError("azure: token response had no access_token")
    return token


def _tags(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if v is not None}


async def fetch_azure_deployments(
    creds: AzureInventoryCredentials,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
) -> tuple[int, list[CloudDeployment]]:
    """Return (AI accounts seen, model deployments) for one service principal."""
    deployments: list[CloudDeployment] = []
    accounts_seen = 0
    async with httpx.AsyncClient(
        transport=transport, timeout=30.0, follow_redirects=False
    ) as client:
        headers = {
            "Authorization": f"Bearer {await _azure_token(client, creds, sleep)}"
        }
        subscriptions = list(creds.subscription_ids)
        if not subscriptions:
            async for sub in _arm_list(
                client,
                "/subscriptions",
                ARM_SUBSCRIPTIONS_API,
                headers,
                "subscription list",
                sleep,
            ):
                sid = sub.get("subscriptionId")
                if isinstance(sid, str) and _GUID.match(sid):
                    subscriptions.append(sid)

        for sid in subscriptions:
            async for account in _arm_list(
                client,
                f"/subscriptions/{sid}/providers/Microsoft.CognitiveServices/accounts",
                ARM_COGNITIVE_API,
                headers,
                "account list",
                sleep,
            ):
                if str(account.get("kind", "")).lower() not in AZURE_AI_KINDS:
                    continue
                account_id = account.get("id")
                if not isinstance(account_id, str) or not _ACCOUNT_ID.match(account_id):
                    logger.warning("azure: skipping account with unexpected id")
                    continue
                accounts_seen += 1
                region = account.get("location")
                account_tags = _tags(account.get("tags"))
                async for dep in _arm_list(
                    client,
                    f"{account_id}/deployments",
                    ARM_COGNITIVE_API,
                    headers,
                    "deployment list",
                    sleep,
                ):
                    props = dep.get("properties") or {}
                    state = str(props.get("provisioningState") or "").lower()
                    name = dep.get("name")
                    if (
                        state in INACTIVE_STATES
                        or not isinstance(name, str)
                        or not name
                    ):
                        continue
                    model = props.get("model") or {}
                    deployments.append(
                        CloudDeployment(
                            cloud="azure",
                            resource_id=f"{account_id}/deployments/{name}",
                            region=(region or "").lower() or None,
                            model=model.get("name"),
                            model_format=model.get("format"),
                            # Deployment tags override the account's.
                            tags={**account_tags, **_tags(dep.get("tags"))},
                        )
                    )
    return accounts_seen, deployments


def _tag(tags: dict[str, str], names: tuple[str, ...]) -> str | None:
    lowered = {k.lower(): v.strip() for k, v in tags.items()}
    for n in names:
        if lowered.get(n):
            return lowered[n]
    return None


def owner_from_tags(tags: dict[str, str]) -> str | None:
    owner = _tag(tags, OWNER_TAGS)
    return owner if owner and "@" in owner else None


def environment_from_tags(tags: dict[str, str]) -> str | None:
    env = _tag(tags, ENVIRONMENT_TAGS)
    return _ENVIRONMENTS.get(env.lower()) if env else None


def vendor_for(d: CloudDeployment) -> str:
    # The cloud provider is the supplier (and processor) of a hosted model.
    return {"azure": "microsoft"}[d.cloud]


def asset_id_for(tenant_id: UUID, d: CloudDeployment) -> UUID:
    return uuid.uuid5(_NAMESPACE, f"{tenant_id}|{d.cloud}|{d.resource_id.lower()}")


async def store_inventory(
    db: AsyncSession,
    tenant_id: UUID,
    deployments: list[CloudDeployment],
    now: datetime,
) -> dict:
    """Upsert one asset per deployment. Does not commit.

    The cloud is authoritative for model and region. Owner, environment and
    business unit are filled from tags only while empty, so values curated
    in the registry are not overwritten.
    """
    created = updated = 0
    for d in deployments:
        aid = asset_id_for(tenant_id, d)
        owner = owner_from_tags(d.tags)
        environment = environment_from_tags(d.tags)
        business_unit = _tag(d.tags, BUSINESS_UNIT_TAGS)
        asset = await db.get(AIAsset, aid)
        if asset is None:
            db.add(
                AIAsset(
                    id=aid,
                    tenant_id=tenant_id,
                    vendor=vendor_for(d),
                    model=d.model,
                    region=d.region,
                    owner_email=owner,
                    environment=environment,
                    business_unit=business_unit,
                    calling_service=f"{d.cloud}:{d.resource_id}",
                    status="active",
                    discovery_source=[SOURCE],
                    confidence=compute_confidence([SOURCE]),
                    first_seen=now,
                    last_seen=now,
                )
            )
            created += 1
            continue
        asset.model = d.model
        asset.region = d.region
        asset.owner_email = asset.owner_email or owner
        asset.environment = asset.environment or environment
        asset.business_unit = asset.business_unit or business_unit
        if SOURCE not in (asset.discovery_source or []):
            asset.discovery_source = list(asset.discovery_source or []) + [SOURCE]
            asset.confidence = compute_confidence(asset.discovery_source)
        if asset.last_seen is None or now > asset.last_seen:
            asset.last_seen = now
        reactivate(asset, now)
        updated += 1
    await db.flush()
    return {"assets_created": created, "assets_updated": updated}


async def pull_cloud_inventory(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    clouds: list[str] | None = None,
    settings: CloudInventorySettings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
    now: datetime | None = None,
) -> dict:
    """Inventory every configured cloud for one tenant and commit.

    A failing cloud is reported in the result and does not stop the others.
    """
    now = now or datetime.now(UTC)
    creds = credentials_for_tenant(tenant_id, settings)
    wanted = [c for c in (clouds or SUPPORTED_CLOUDS) if c in SUPPORTED_CLOUDS]
    result: dict[str, dict] = {}
    for cloud in wanted:
        cloud_creds = getattr(creds, cloud)
        if cloud_creds is None:
            result[cloud] = {"status": "not_configured"}
            continue
        try:
            accounts, deployments = await fetch_azure_deployments(
                cloud_creds, transport=transport, sleep=sleep
            )
        except CloudInventoryError as exc:
            logger.warning("cloud inventory failed for tenant %s: %s", tenant_id, exc)
            result[cloud] = {"status": "error", "error": str(exc)}
            continue
        counts = await store_inventory(db, tenant_id, deployments, now)
        await db.commit()
        logger.info("cloud inventory: tenant=%s cloud=%s %s", tenant_id, cloud, counts)
        result[cloud] = {
            "status": "ok",
            "accounts": accounts,
            "deployments": len(deployments),
            **counts,
        }
    return {"tenant_id": str(tenant_id), "clouds": result}


router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])


class CloudInventoryPullBody(BaseModel):
    tenant_id: UUID
    clouds: list[str] | None = None


@router.post(
    "/cloud-inventory/pull",
    summary="Inventory AI model deployments in cloud accounts (operator)",
    dependencies=[Depends(require_operator)],
)
async def pull_cloud_inventory_endpoint(
    body: CloudInventoryPullBody, request: Request
) -> dict:
    db = request.state.db
    # One tenant per pull: scope the session to it instead of bypassing RLS.
    await rls.set_tenant(db, body.tenant_id)
    if await db.get(Tenant, body.tenant_id) is None:
        raise HTTPException(
            status_code=404,
            detail={
                "type": "about:blank",
                "title": "Not Found",
                "status": 404,
                "detail": "Tenant not found",
            },
        )
    return await pull_cloud_inventory(db, body.tenant_id, clouds=body.clouds)


async def _main(tenant: UUID, clouds: list[str] | None) -> dict:
    from collector.app import SessionLocal, engine

    try:
        async with SessionLocal() as db:
            await rls.set_tenant(db, tenant)
            return await pull_cloud_inventory(db, tenant, clouds=clouds)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", type=UUID, required=True)
    parser.add_argument("--cloud", action="append", choices=SUPPORTED_CLOUDS)
    args = parser.parse_args(argv)
    print(json.dumps(asyncio.run(_main(args.tenant, args.cloud)), indent=2))


if __name__ == "__main__":
    main()
