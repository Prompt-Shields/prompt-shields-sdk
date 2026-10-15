"""LeanIX connector: push the AI asset registry into a LeanIX workspace.

Reads the tenant's registry through the Partner API (``/export`` for a
full sync, ``/changes`` for incremental ones, ``/assets/{id}/data-flows``
for flows) and writes it to LeanIX with the Integration API, as LDIF
synchronisation runs processed by the inbound processor configuration in
`processor_configuration`:

  ===================  =========================  ===============================
  Prompt Shields       LeanIX fact sheet          External id
  ===================  =========================  ===============================
  AI asset             IT Component               ``promptshields:asset:<id>``
  Vendor               Provider                   ``promptshields:provider:<v>``
  Business unit        Organization (or UserGroup) ``promptshields:business-unit:<bu>``
  Data flow            Interface                  ``promptshields:flow:<id>``
  ===================  =========================  ===============================

Relations: IT Component -> Provider and Interface -> IT Component. The
external ids match the AI-BOM (CycloneDX) ``bom-ref`` values, so a fact
sheet can be traced to the BOM entry. Fact sheets are matched on external
id, so re-running a sync updates the same fact sheets (idempotent upsert);
runs use ``processingMode: partial`` and never delete. Retired assets get
the ``endOfLife`` lifecycle phase.

Business units go to ``business_unit_type``: ``Organization`` in the
current meta model (v4), ``UserGroup`` in workspaces still on the older
one. ``lx_workspace`` (the workspace UUID) is optional; when set it is
sent as the LDIF ``lxWorkspace`` so a run fails instead of landing in
another workspace than the one intended.

Configuration (operator environment only, per tenant)::

    PS_LEANIX='{"<tenant uuid>": {
        "workspace_host": "acme.leanix.net",
        "api_token": "<LeanIX technical user API token>",
        "partner_api": {"base_url": "https://collector.example.com",
                        "api_key": "<partner API key>"}}}'

The API token and partner key are ``SecretStr``: never logged, stored or
returned. Outbound calls go only to ``https://<workspace_host>``, which
must end in one of ``PS_LEANIX_ALLOWED_HOST_SUFFIXES`` (default
``.leanix.net``) and resolve to public addresses.

Run with ``POST /api/v1/admin/connectors/leanix/sync`` (operator only:
``Authorization: Bearer $PS_ADMIN_TOKEN``) or ``python -m collector.leanix
--tenant <uuid> [--full]``. The cursor is kept in the tenant settings and
only advances after every LDIF run has finished.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any, Literal, Self
from uuid import UUID

import httpx
from db.models import Tenant
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings
from sqlalchemy.ext.asyncio import AsyncSession

from collector import aibom, rls
from collector.connectors import (
    BearerToken,
    Clock,
    ConnectorError,
    GuardedClient,
    PartnerApiClient,
    PartnerApiConnection,
    Resolver,
    Sleep,
    default_resolver,
    load_sync_state,
    next_cursor,  # noqa: F401  (re-exported; moved to connectors)
    read_registry,
    save_sync_state,
    validate_hostname,
)
from collector.provider_usage_admin import require_operator

logger = logging.getLogger("prompt_shields.leanix")

CONNECTOR = "leanix"
CONNECTOR_TYPE = "prompt-shields"
CONNECTOR_VERSION = "1.0.0"
LX_VERSION = "1.0.0"

TOKEN_PATH = "/services/mtm/v1/oauth2/token"
INTEGRATION_API = "/services/integration-api/v1"

# Assets per LDIF run; each run also carries their providers, business
# units and flows.
DEFAULT_BATCH_SIZE = 500
POLL_INTERVAL_S = 5.0
MAX_POLL_S = 900.0
MAX_TEXT = 2000

RUN_DONE = {"FINISHED"}
RUN_FAILED = {"FAILED", "ABORTED", "CANCELLED", "CANCELED", "ERROR"}

_CONNECTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")

NS = aibom.NS


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────


class LeanIXConnection(BaseModel):
    workspace_host: str
    api_token: SecretStr
    partner_api: PartnerApiConnection
    # Distinguishes several Prompt Shields instances feeding one workspace.
    connector_id: str = "prompt-shields"
    include_data_flows: bool = True
    batch_size: int = Field(DEFAULT_BATCH_SIZE, ge=1, le=5000)
    # Fact sheet type for business units: Organization (meta model v4) or
    # UserGroup (earlier meta model).
    business_unit_type: Literal["Organization", "UserGroup"] = "Organization"
    # Workspace UUID, sent as the optional LDIF lxWorkspace.
    lx_workspace: UUID | None = None

    @field_validator("connector_id")
    @classmethod
    def _connector_id(cls, v: str) -> str:
        if not _CONNECTOR_ID.match(v):
            raise ValueError("connector_id must be 1-64 of [A-Za-z0-9._-]")
        return v


class LeanIXSettings(BaseSettings):
    leanix: dict[str, LeanIXConnection] = {}
    leanix_allowed_host_suffixes: list[str] = [".leanix.net"]

    # A malformed PS_LEANIX must not echo the token in the error.
    model_config = {
        "env_prefix": "PS_",
        "extra": "ignore",
        "hide_input_in_errors": True,
    }


def connection_for_tenant(
    tenant_id: UUID, settings: LeanIXSettings | None = None
) -> LeanIXConnection | None:
    """The tenant's LeanIX connection, host validated against the allowed
    suffixes. Raises ConnectorError on a bad host."""
    if settings is None:
        try:
            settings = LeanIXSettings()
        except ValidationError:
            # hide_input_in_errors keeps the token out, but say no more.
            raise ConnectorError("leanix: PS_LEANIX is not valid configuration")
    conn = settings.leanix.get(str(tenant_id))
    if conn is None:
        return None
    suffixes = tuple(s for s in settings.leanix_allowed_host_suffixes if s.strip("."))
    if not suffixes:
        # An empty list must not turn the allowlist off.
        raise ConnectorError("leanix: PS_LEANIX_ALLOWED_HOST_SUFFIXES is empty")
    try:
        host = validate_hostname(conn.workspace_host, suffixes)
    except ValueError as exc:
        raise ConnectorError(f"leanix: workspace_host: {exc}")
    return conn.model_copy(update={"workspace_host": host})


# ──────────────────────────────────────────────────────────────────────
# Mapping: Partner API JSON -> LDIF
# ──────────────────────────────────────────────────────────────────────


def asset_external_id(asset_id: str) -> str:
    return f"{NS}:asset:{asset_id}"


def provider_external_id(vendor: str) -> str:
    return f"{NS}:provider:{vendor}"


def business_unit_external_id(bu: str) -> str:
    return f"{NS}:business-unit:{bu}"


def flow_external_id(flow_id: str) -> str:
    return f"{NS}:flow:{flow_id}"


def _text(value: Any) -> str:
    return str(value)[:MAX_TEXT] if value is not None else ""


def _date(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        return None


def _asset_name(a: dict) -> str:
    base = a.get("use_case_name") or (
        f"{a['vendor']} {a['model']}" if a.get("model") else a.get("vendor")
    )
    # LeanIX names are unique per fact sheet type; the id suffix keeps two
    # use cases with the same name apart.
    return _text(f"{base} [{str(a['id'])[:8]}]")[:250]


def _asset_description(a: dict) -> str:
    parts = [
        f"AI asset discovered by Prompt Shields ({asset_external_id(a['id'])}).",
        f"Vendor: {a.get('vendor')}.",
    ]
    for label, key in (
        ("Model", "model"),
        ("Business unit", "business_unit"),
        ("Owner", "owner_email"),
        ("Environment", "environment"),
        ("Region", "region"),
        ("Data classification", "data_classification"),
        ("Confidence", "confidence"),
        ("Lifecycle", "lifecycle_state"),
    ):
        if a.get(key):
            parts.append(f"{label}: {a[key]}.")
    return _text(" ".join(parts))


def _lifecycle(a: dict) -> tuple[str, str | None]:
    """(LeanIX phase, start date) for an asset."""
    if (a.get("lifecycle_state") or "active") == "retired":
        return "endOfLife", _date(a.get("lifecycle_changed_at")) or _date(
            a.get("last_seen")
        )
    return "active", _date(a.get("first_seen"))


def asset_items(a: dict, business_unit_type: str = "Organization") -> list[dict]:
    """LDIF content items for one asset: its IT Component, Provider and,
    when set, business unit (``business_unit_type`` fact sheet)."""
    phase, start = _lifecycle(a)
    data: dict[str, Any] = {
        "name": _asset_name(a),
        "description": _asset_description(a),
        "providerId": provider_external_id(a["vendor"]),
        "lifecyclePhase": phase,
    }
    if start:
        data["lifecycleStart"] = start
    items = [
        {"type": "ITComponent", "id": asset_external_id(a["id"]), "data": data},
        {
            "type": "Provider",
            "id": provider_external_id(a["vendor"]),
            "data": {
                "name": _text(a["vendor"])[:250],
                "description": "AI vendor in use, reported by Prompt Shields.",
            },
        },
    ]
    if a.get("business_unit"):
        items.append(
            {
                "type": business_unit_type,
                "id": business_unit_external_id(a["business_unit"]),
                "data": {
                    "name": _text(a["business_unit"])[:250],
                    "description": "Business unit using AI, reported by Prompt Shields.",
                },
            }
        )
    return items


def flow_item(asset_id: str, f: dict) -> dict:
    pii = f.get("detected_pii_types") or []
    desc = (
        f"Data flow recorded by Prompt Shields for {asset_external_id(asset_id)}. "
        f"Direction: {f.get('direction') or 'unknown'}. "
        f"Classification: {f.get('data_classification') or 'unclassified'}."
    )
    if pii:
        desc += f" Detected PII types: {', '.join(map(str, pii))}."
    return {
        "type": "Interface",
        "id": flow_external_id(f["id"]),
        "data": {
            "name": _text(
                f"{f.get('source_system')} -> {f.get('destination_system')} "
                f"[{str(f['id'])[:8]}]"
            )[:250],
            "description": _text(desc),
            "itComponentId": asset_external_id(asset_id),
        },
    }


def build_ldif(
    assets: list[dict],
    flows: dict[str, list[dict]] | None = None,
    *,
    connector_id: str,
    description: str = "Prompt Shields AI asset registry",
    business_unit_type: str = "Organization",
    lx_workspace: UUID | str | None = None,
) -> dict:
    """One LDIF document for ``assets`` (Partner API asset dicts) and their
    flows (asset id -> Partner API flow dicts). Shared fact sheets
    (providers, business units) appear once."""
    content: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for a in assets:
        if not a.get("id") or not a.get("vendor"):
            continue
        items = asset_items(a, business_unit_type)
        items += [
            flow_item(a["id"], f) for f in (flows or {}).get(a["id"], []) if f.get("id")
        ]
        for item in items:
            key = (item["type"], item["id"])
            if key not in seen:
                seen.add(key)
                content.append(item)
    ldif: dict[str, Any] = {
        "connectorType": CONNECTOR_TYPE,
        "connectorId": connector_id,
        "connectorVersion": CONNECTOR_VERSION,
        "lxVersion": LX_VERSION,
    }
    if lx_workspace:
        ldif["lxWorkspace"] = str(lx_workspace)
    return {
        **ldif,
        "processingDirection": "inbound",
        "processingMode": "partial",
        "description": description,
        "content": content,
    }


def _by_external_id(expr: str) -> dict:
    return {"external": {"id": {"expr": expr}, "type": {"expr": "externalId"}}}


def _fact_sheet_processor(name: str, fs_type: str, extra_updates: list[dict]) -> dict:
    return {
        "processorType": "inboundFactSheet",
        "processorName": name,
        "processorDescription": f"Upsert {fs_type} fact sheets from Prompt Shields",
        "type": fs_type,
        "filter": {"exactType": fs_type},
        "identifier": _by_external_id("${content.id}"),
        "run": 0,
        "updates": [
            {"key": {"expr": "name"}, "values": [{"expr": "${data.name}"}]},
            {
                "key": {"expr": "description"},
                "values": [{"expr": "${data.description}"}],
                "optional": True,
            },
            *extra_updates,
        ],
        "logLevel": "warning",
    }


def _relation_processor(name: str, rel_type: str, from_type: str, to_expr: str) -> dict:
    return {
        "processorType": "inboundRelation",
        "processorName": name,
        "processorDescription": f"{rel_type} from Prompt Shields",
        "type": rel_type,
        "filter": {"exactType": from_type},
        "from": _by_external_id("${content.id}"),
        "to": _by_external_id(to_expr),
        "run": 1,
        "logLevel": "warning",
    }


def processor_configuration(
    connector_id: str, business_unit_type: str = "Organization"
) -> dict:
    """Inbound processor configuration matching `build_ldif`."""
    lifecycle = {
        "key": {"expr": "lifecycle"},
        "values": [
            {
                "map": [
                    {"key": "phase", "value": "${data.lifecyclePhase}"},
                    {"key": "startDate", "value": "${data.lifecycleStart}"},
                ]
            }
        ],
        "optional": True,
    }
    return {
        "connectorType": CONNECTOR_TYPE,
        "connectorId": connector_id,
        "connectorVersion": CONNECTOR_VERSION,
        "processingDirection": "inbound",
        "processingMode": "partial",
        "processors": [
            _fact_sheet_processor(
                "Prompt Shields AI assets", "ITComponent", [lifecycle]
            ),
            _fact_sheet_processor("Prompt Shields AI vendors", "Provider", []),
            _fact_sheet_processor(
                "Prompt Shields business units", business_unit_type, []
            ),
            _fact_sheet_processor("Prompt Shields data flows", "Interface", []),
            _relation_processor(
                "AI asset to vendor",
                "relITComponentToProvider",
                "ITComponent",
                "${data.providerId}",
            ),
            _relation_processor(
                "Data flow to AI asset",
                "relInterfaceToITComponent",
                "Interface",
                "${data.itComponentId}",
            ),
        ],
    }


# ──────────────────────────────────────────────────────────────────────
# LeanIX Integration API client
# ──────────────────────────────────────────────────────────────────────


class LeanIXClient:
    def __init__(
        self,
        conn: LeanIXConnection,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = default_resolver,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ):
        self._conn = conn
        self._sleep = sleep
        self._http = GuardedClient(
            f"https://{conn.workspace_host}",
            name="leanix",
            transport=transport,
            resolver=resolver,
            sleep=sleep,
        )
        self._token = BearerToken(self._http, self._fetch_token, clock=clock)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.__aexit__(*exc)

    async def _fetch_token(self) -> Any:
        return await self._http.json(
            "POST",
            TOKEN_PATH,
            what="token request",
            auth=("apitoken", self._conn.api_token.get_secret_value()),
            data={"grant_type": "client_credentials"},
        )

    async def put_processor_configuration(self) -> None:
        await self._token.request(
            "PUT",
            f"{INTEGRATION_API}/configurations/",
            what="processor configuration",
            expected=(200, 201, 204),
            json=processor_configuration(
                self._conn.connector_id, self._conn.business_unit_type
            ),
        )

    async def start_run(self, ldif: dict) -> str:
        body = await self._token.json(
            "POST",
            f"{INTEGRATION_API}/synchronizationRuns",
            what="start synchronization run",
            expected=(200, 201),
            params={"start": "true"},
            json=ldif,
        )
        run_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
            raise ConnectorError("leanix: synchronization run returned no valid id")
        return run_id

    async def wait_for_run(self, run_id: str, max_wait_s: float = MAX_POLL_S) -> str:
        waited = 0.0
        while True:
            body = await self._token.json(
                "GET",
                f"{INTEGRATION_API}/synchronizationRuns/{run_id}/status",
                what="synchronization run status",
            )
            status = str((body if isinstance(body, dict) else {}).get("status") or "")
            status = status.upper()
            if status in RUN_DONE:
                return status
            if status in RUN_FAILED:
                raise ConnectorError(f"leanix: synchronization run {run_id} {status}")
            if waited >= max_wait_s:
                raise ConnectorError(
                    f"leanix: synchronization run {run_id} not finished "
                    f"after {int(max_wait_s)}s"
                )
            await self._sleep(POLL_INTERVAL_S)
            waited += POLL_INTERVAL_S


# ──────────────────────────────────────────────────────────────────────
# Sync job
# ──────────────────────────────────────────────────────────────────────


async def sync_leanix(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    full: bool = False,
    settings: LeanIXSettings | None = None,
    partner_transport: httpx.AsyncBaseTransport | None = None,
    leanix_transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver = default_resolver,
    sleep: Sleep = asyncio.sleep,
    now: datetime | None = None,
) -> dict:
    """Push the tenant's registry (or what changed since the last run) to
    LeanIX. Records the outcome in the tenant's connector state; the cursor
    only moves when every run finished."""
    now = now or datetime.now(UTC)
    state = await load_sync_state(db, tenant_id, CONNECTOR)
    if state is None:
        return {"tenant_id": str(tenant_id), "status": "tenant_not_found"}
    try:
        conn = connection_for_tenant(tenant_id, settings)
    except ConnectorError as exc:
        return {"tenant_id": str(tenant_id), "status": "error", "error": str(exc)}
    if conn is None:
        return {"tenant_id": str(tenant_id), "status": "not_configured"}

    since = None if full else state.get("cursor")
    result: dict[str, Any] = {
        "tenant_id": str(tenant_id),
        "mode": "incremental" if since else "full",
        "since": since,
    }
    try:
        async with PartnerApiClient(
            conn.partner_api,
            tenant_id=tenant_id,
            transport=partner_transport,
            resolver=resolver,
            sleep=sleep,
        ) as partner:
            read = await read_registry(
                partner, since, include_data_flows=conn.include_data_flows
            )
        assets, flows, cursor = read.assets, read.flows, read.cursor

        run_ids: list[str] = []
        items = 0
        if assets:
            async with LeanIXClient(
                conn, transport=leanix_transport, resolver=resolver, sleep=sleep
            ) as lx:
                await lx.put_processor_configuration()
                for i in range(0, len(assets), conn.batch_size):
                    batch = assets[i : i + conn.batch_size]
                    ldif = build_ldif(
                        batch,
                        {a["id"]: flows.get(a["id"], []) for a in batch if a.get("id")},
                        connector_id=conn.connector_id,
                        business_unit_type=conn.business_unit_type,
                        lx_workspace=conn.lx_workspace,
                    )
                    items += len(ldif["content"])
                    run_id = await lx.start_run(ldif)
                    run_ids.append(run_id)
                    await lx.wait_for_run(run_id)
    except (ConnectorError, ValueError, TypeError, OverflowError) as exc:
        # Malformed Partner API data is a failed run, not a server error.
        error = str(exc) if isinstance(exc, ConnectorError) else "invalid response data"
        logger.warning("leanix sync failed for tenant %s: %s", tenant_id, error)
        state.update(last_run_at=now.isoformat(), last_status="error", last_error=error)
        await save_sync_state(db, tenant_id, CONNECTOR, state)
        return {**result, "status": "error", "error": error}

    counts = {
        "assets": len(assets),
        "data_flows": sum(len(v) for v in flows.values()),
        "ldif_items": items,
        "runs": len(run_ids),
    }
    state.update(
        cursor=cursor or state.get("cursor"),
        last_run_at=now.isoformat(),
        last_status="ok",
        last_error=None,
        last_counts=counts,
        last_run_ids=run_ids[-10:],
    )
    await save_sync_state(db, tenant_id, CONNECTOR, state)
    logger.info("leanix sync: tenant=%s %s", tenant_id, counts)
    return {
        **result,
        "status": "ok",
        "cursor": state["cursor"],
        "run_ids": run_ids,
        **counts,
    }


# ──────────────────────────────────────────────────────────────────────
# Operator endpoints and CLI
# ──────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/v1/admin/connectors/leanix", tags=["Admin"])


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "type": "about:blank",
            "title": "Not Found",
            "status": 404,
            "detail": "Tenant not found",
        },
    )


class LeanIXSyncBody(BaseModel):
    tenant_id: UUID
    # Ignore the cursor and push the whole registry.
    full: bool = False


@router.post(
    "/sync",
    summary="Push the AI asset registry to LeanIX (operator)",
    dependencies=[Depends(require_operator)],
)
async def sync_endpoint(body: LeanIXSyncBody, request: Request) -> dict:
    db = request.state.db
    # One tenant per sync: scope the session to it instead of bypassing RLS.
    await rls.set_tenant(db, body.tenant_id)
    if await db.get(Tenant, body.tenant_id) is None:
        raise _not_found()
    return await sync_leanix(db, body.tenant_id, full=body.full)


@router.get(
    "/status",
    summary="LeanIX connector state for a tenant (operator)",
    dependencies=[Depends(require_operator)],
)
async def status_endpoint(tenant_id: UUID, request: Request) -> dict:
    await rls.set_tenant(request.state.db, tenant_id)
    state = await load_sync_state(request.state.db, tenant_id, CONNECTOR)
    if state is None:
        raise _not_found()
    try:
        conn = connection_for_tenant(tenant_id)
        config_error = None
    except ConnectorError as exc:
        conn, config_error = None, str(exc)
    return {
        "tenant_id": str(tenant_id),
        "configured": conn is not None,
        # Host only: the API token and partner key are never returned.
        "workspace_host": conn.workspace_host if conn else None,
        "business_unit_type": conn.business_unit_type if conn else None,
        "config_error": config_error,
        **state,
    }


async def _main(tenant: UUID, full: bool) -> dict:
    from collector.app import SessionLocal, engine

    try:
        async with SessionLocal() as db:
            await rls.set_tenant(db, tenant)
            return await sync_leanix(db, tenant, full=full)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", type=UUID, required=True)
    parser.add_argument("--full", action="store_true", help="ignore the cursor")
    args = parser.parse_args(argv)
    print(json.dumps(asyncio.run(_main(args.tenant, args.full)), indent=2))


if __name__ == "__main__":
    main()
