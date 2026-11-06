"""Microsoft Sentinel / Defender XDR export of per-call routing evidence
and the audit trail (#32).

Two record kinds, one custom Log Analytics table (``PromptShieldsAudit_CL``,
schema in `COLUMNS`, DCR and table JSON in docs/integrations/sentinel/):

  ``AICall``  one row per ``ai_usage_events`` row: the routing evidence of
              one model call (served provider, model and jurisdiction,
              policy version, sensitivity level and its source, route
              mode/group/reason, fallback chain, cost, tokens, latency).
              Metadata only: no prompt, response, prompt hash, session id,
              key fingerprint or flow context.
  ``Audit``   one row per ``audit_events`` row of the tenant's hash chain
              (collector.audit_trail), with its ``seq``, ``prev_hash`` and
              ``row_hash`` so the chain can be re-verified from the SIEM.

Field names follow the Microsoft Sentinel ASIM common fields where one fits
(``TimeGenerated``, ``EventUid``, ``EventType``, ``EventResult``,
``EventSeverity``, ``EventProduct``, ``EventVendor``, ``EventSchemaVersion``,
``Operation``, ``ActorUserType``/``ActorUserId``, ``Object``/``ObjectType``,
``HttpStatusCode``); Prompt Shields specific fields carry a ``Ps`` prefix.

Why the Logs Ingestion API
    Microsoft Purview Audit has no API for third parties to write audit
    records (the Office 365 Management Activity API only reads), and the
    Purview Graph APIs for AI apps (``processContent`` /
    ``contentActivities``) take prompt and response text per signed-in
    user, which Prompt Shields does not hold. The HTTP Data Collector API
    was retired on 2026-09-14. The supported path into a Log Analytics
    workspace, and so into Microsoft Sentinel and Defender XDR advanced
    hunting, is the Logs Ingestion API with a data collection rule (DCR)
    and a ``_CL`` custom table, authenticated with a Microsoft Entra
    application (client credentials) holding *Monitoring Metrics
    Publisher* on the DCR.

Push (opt-in, operator configuration only)::

    PS_SENTINEL_EXPORT='{"<tenant uuid>": {
        "entra_tenant_id": "<directory id>", "client_id": "<app id>",
        "client_secret": "...",
        "ingestion_endpoint": "https://<dcr-or-dce>.<region>-1.ingest.monitor.azure.com",
        "dcr_immutable_id": "dcr-<32 hex>"}}'

``client_secret`` is a ``SecretStr``: never logged, stored or returned.
Token requests go only to the cloud's Entra host and records only to an
``ingestion_endpoint`` under the cloud's ``*.ingest.monitor.azure.*``
suffix, both through the connector SSRF guard (public addresses only, DNS
pinned, no redirects). Run with ``POST
/api/v1/admin/connectors/sentinel/sync`` (operator) or ``python -m
collector.sentinel_export --tenant <uuid>`` / ``--all-tenants`` from cron.
With ``--all-tenants`` each tenant runs on its own: one that fails, even
unexpectedly, is recorded in its export status, the run moves on, and the
command prints a summary and exits non-zero.
Cursors live in the tenant settings and move only past batches the API
accepted (204), so a failed run resends from the first unaccepted batch;
``EventUid`` is stable, so a resend is a duplicate, not a different row.

AI call cursor and late events
    ``ai_usage_events`` has no insertion time, only the call's own
    ``timestamp``. The cursor is ``(timestamp, id)`` and a run exports only
    calls older than ``settle_seconds`` (default 300), so events that the
    SDK or gateway batch and send a little late are still picked up. An
    event that arrives later than that with an older timestamp is not
    pushed; the pull endpoint below can fetch any time range again.

Pull (fallback, Partner API, NDJSON)::

    GET /partner/v1/siem/calls   scope evidence:read
    GET /partner/v1/siem/audit   scope audit:read

Same records as the push, one JSON object per line
(``application/x-ndjson``). ``X-PS-Cursor`` is the position after the last
line (pass it back as ``cursor``) and ``X-PS-Has-More`` says whether to
call again now. Usable from Azure Monitor Agent / Logstash / Cribl or any
script that forwards to the Logs Ingestion API.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Self
from uuid import UUID

import httpx
from db import audit_chain
from db.models import AIAsset, AIUsageEvent, AuditEvent, Tenant
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from collector import audit_trail, rls
from collector.connectors import (
    BearerToken,
    Clock,
    ConnectorError,
    GuardedClient,
    Resolver,
    Sleep,
    default_resolver,
    load_sync_state,
    save_sync_state,
    validate_hostname,
)
from collector.partner_auth import PartnerPrincipal, require_scope
from collector.partner_registry import _audit, _enforce_rate_limit
from collector.provider_usage_admin import require_operator
from collector.sanitize import BoundedJsonRoute
from collector.usage_reports import SOVEREIGN_JURISDICTIONS

logger = logging.getLogger("prompt_shields.sentinel_export")

CONNECTOR = "sentinel"
SCHEMA_VERSION = "1.0"
TABLE_NAME = "PromptShieldsAudit_CL"
DEFAULT_STREAM = "Custom-PromptShieldsAudit"
INGESTION_API_VERSION = "2023-01-01"
EVENT_PRODUCT = "Prompt Shields"
EVENT_VENDOR = "Prompt Shields"
KINDS = ("calls", "audit")

# The Logs Ingestion API refuses calls over 1 MB; keep a margin.
MAX_BODY_BYTES = 950_000
DEFAULT_BATCH_RECORDS = 500
DEFAULT_MAX_RECORDS_PER_RUN = 50_000
DEFAULT_SETTLE_SECONDS = 300

# cloud -> (Entra token host, Logs Ingestion token scope, ingestion host suffix)
CLOUDS: dict[str, tuple[str, str, str]] = {
    "public": (
        "login.microsoftonline.com",
        "https://monitor.azure.com//.default",
        ".ingest.monitor.azure.com",
    ),
    "usgov": (
        "login.microsoftonline.us",
        "https://monitor.azure.us//.default",
        ".ingest.monitor.azure.us",
    ),
    "china": (
        "login.chinacloudapi.cn",
        "https://monitor.azure.cn//.default",
        ".ingest.monitor.azure.cn",
    ),
}

_GUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_DCR_ID = re.compile(r"^dcr-[0-9a-f]{32}$")
_STREAM = re.compile(r"^Custom-[A-Za-z0-9_]{1,60}$")

# ──────────────────────────────────────────────────────────────────────
# Table schema (single source for the mapping, the docs and the tests)
# ──────────────────────────────────────────────────────────────────────

# (column, Log Analytics type, description)
COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("TimeGenerated", "datetime", "Call time (AICall) or audit row time (Audit), UTC"),
    ("EventUid", "string", "Prompt Shields id of the usage event or audit row; stable across resends"),
    ("EventType", "string", "AICall or Audit"),
    ("EventProduct", "string", "Prompt Shields"),
    ("EventVendor", "string", "Prompt Shields"),
    ("EventSchemaVersion", "string", "Version of this mapping (1.0)"),
    ("EventResult", "string", "Success, Failure or NA (ASIM)"),
    ("EventSeverity", "string", "Informational, Low or Medium (ASIM)"),
    ("Operation", "string", "ai.call, or the audit action (e.g. partner_credential.update)"),
    ("ActorUserType", "string", "Audit: tenant_key, admin_token, partner, operator, system or anonymous"),
    ("ActorUserId", "string", "Audit: credential lookup prefix or id, never a secret"),
    ("Object", "string", "Audit: target id"),
    ("ObjectType", "string", "Audit: target type"),
    ("HttpStatusCode", "int", "Audit: HTTP status of the audited request"),
    ("PsTenantId", "string", "Prompt Shields tenant id"),
    ("PsRequestId", "string", "Audit: X-Request-ID of the audited request"),
    ("PsCallId", "string", "AICall: per-call id from the SDK or gateway"),
    ("PsAssetId", "string", "AICall: AI asset id in the registry"),
    ("PsVendor", "string", "AICall: asset vendor"),
    ("PsBusinessUnit", "string", "AICall: asset business unit"),
    ("PsEnvironment", "string", "AICall: asset environment"),
    ("PsSource", "string", "AICall: telemetry source (sdk, gateway, otel, kong, ...)"),
    ("PsApiSurface", "string", "AICall: API the caller spoke (chat, messages, responses, ...)"),
    ("PsRequestedModel", "string", "AICall: model the caller asked for (may be auto)"),
    ("PsServedModel", "string", "AICall: model that served the call"),
    ("PsServedProvider", "string", "AICall: provider that served the call"),
    ("PsServedJurisdiction", "string", "AICall: jurisdiction of the serving tier (EEA, US, DE, ...)"),
    ("PsSovereign", "boolean", "AICall: served in the EEA; empty when the jurisdiction is unknown"),
    ("PsPolicyVersion", "string", "AICall: route policy version that decided the call"),
    ("PsSensitivityLevel", "string", "AICall: sensitivity level the call was routed at"),
    ("PsSensitivitySource", "string", "AICall: declared, pii_escalated or default"),
    ("PsRouteMode", "string", "AICall: gateway, local or none"),
    ("PsRouteGroup", "string", "AICall: route group"),
    ("PsRouteReason", "string", "AICall: why the router chose the model"),
    ("PsFallbackChain", "dynamic", "AICall: error class of each failed attempt, in order"),
    ("PsBaselineId", "string", "AICall: baseline model of the counterfactual cost"),
    ("PsTokensIn", "long", "AICall: input tokens"),
    ("PsTokensOut", "long", "AICall: output tokens"),
    ("PsCostUsd", "real", "AICall: cost in USD"),
    ("PsCounterfactualCostUsd", "real", "AICall: cost on the baseline model in USD"),
    ("PsRouteEstCostUsd", "real", "AICall: router's cost estimate before the call, USD"),
    ("PsLatencyMs", "long", "AICall: latency in milliseconds"),
    ("PsAuditSeq", "long", "Audit: position in the tenant's hash chain"),
    ("PsAuditPrevHash", "string", "Audit: row_hash of the previous row"),
    ("PsAuditRowHash", "string", "Audit: sha256 over prev_hash and the canonical row"),
    ("PsAuditHashVersion", "int", "Audit: hash algorithm version"),
    ("PsAuditDetails", "dynamic", "Audit: ids, counts and names (never secrets or free text)"),
)
COLUMN_NAMES = frozenset(c[0] for c in COLUMNS)


def table_definition() -> dict:
    """Body of ``PUT .../workspaces/<ws>/tables/PromptShieldsAudit_CL``."""
    return {
        "properties": {
            "schema": {
                "name": TABLE_NAME,
                "columns": [
                    {"name": n, "type": t, "description": d} for n, t, d in COLUMNS
                ],
            }
        }
    }


def dcr_definition(
    workspace_resource_id: str = "<workspace resource id>",
    location: str = "<workspace region>",
) -> dict:
    """A ``kind: Direct`` DCR (it gets its own logs ingestion endpoint) that
    passes the stream through to the table unchanged."""
    return {
        "location": location,
        "kind": "Direct",
        "properties": {
            "streamDeclarations": {
                DEFAULT_STREAM: {
                    "columns": [{"name": n, "type": t} for n, t, _ in COLUMNS]
                }
            },
            "destinations": {
                "logAnalytics": [
                    {"workspaceResourceId": workspace_resource_id, "name": "workspace"}
                ]
            },
            "dataFlows": [
                {
                    "streams": [DEFAULT_STREAM],
                    "destinations": ["workspace"],
                    "transformKql": "source",
                    "outputStream": f"Custom-{TABLE_NAME}",
                }
            ],
        },
    }


# ──────────────────────────────────────────────────────────────────────
# Mapping
# ──────────────────────────────────────────────────────────────────────


def _ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, Decimal | int | float) else None


def _compact(record: dict) -> dict:
    """Drop empty fields; the table leaves them empty anyway."""
    return {k: v for k, v in record.items() if v is not None}


def _common(event_type: str, uid: Any, when: datetime | None, tenant_id: Any) -> dict:
    return {
        "TimeGenerated": _ts(when),
        "EventUid": str(uid),
        "EventType": event_type,
        "EventProduct": EVENT_PRODUCT,
        "EventVendor": EVENT_VENDOR,
        "EventSchemaVersion": SCHEMA_VERSION,
        "PsTenantId": str(tenant_id),
    }


def sovereign(jurisdiction: str | None) -> bool | None:
    if not jurisdiction:
        return None
    return jurisdiction.strip().upper() in SOVEREIGN_JURISDICTIONS


def call_record(event: AIUsageEvent, asset: AIAsset | None = None) -> dict:
    """One ``AICall`` row: the routing evidence of one model call."""
    fallback = event.fallback_chain if isinstance(event.fallback_chain, list) else None
    return _compact(
        {
            **_common("AICall", event.id, event.timestamp, event.tenant_id),
            "EventResult": "NA",
            "EventSeverity": "Informational",
            "Operation": "ai.call",
            "PsCallId": event.call_id,
            "PsAssetId": str(event.asset_id) if event.asset_id else None,
            "PsVendor": asset.vendor if asset is not None else None,
            "PsBusinessUnit": asset.business_unit if asset is not None else None,
            "PsEnvironment": asset.environment if asset is not None else None,
            "PsSource": event.source,
            "PsApiSurface": event.api_surface,
            "PsRequestedModel": event.requested_model,
            "PsServedModel": event.served_model,
            "PsServedProvider": event.served_provider,
            "PsServedJurisdiction": event.served_jurisdiction,
            "PsSovereign": sovereign(event.served_jurisdiction),
            "PsPolicyVersion": event.policy_version,
            "PsSensitivityLevel": event.sensitivity_level,
            "PsSensitivitySource": event.sensitivity_source,
            "PsRouteMode": event.route_mode,
            "PsRouteGroup": event.route_group,
            "PsRouteReason": event.route_reason,
            "PsFallbackChain": fallback,
            "PsBaselineId": event.baseline_id,
            "PsTokensIn": event.tokens_in,
            "PsTokensOut": event.tokens_out,
            "PsCostUsd": _num(event.cost),
            "PsCounterfactualCostUsd": _num(event.counterfactual_cost),
            "PsRouteEstCostUsd": _num(event.route_est_cost),
            "PsLatencyMs": event.latency_ms,
        }
    )


def _audit_severity(row: AuditEvent) -> str:
    if row.action == "auth.failure":
        return "Medium"
    return "Low" if row.outcome == audit_trail.FAILURE else "Informational"


def audit_record(row: AuditEvent) -> dict:
    """One ``Audit`` row: an audit trail row with its chain hashes."""
    payload = audit_trail.audit_payload(row)
    return _compact(
        {
            **_common("Audit", row.id, row.created_at, row.tenant_id),
            # The chain's own timestamp form (microseconds, "Z"), so row_hash
            # can be recomputed from the exported fields.
            "TimeGenerated": audit_chain.format_ts(row.created_at),
            "EventResult": "Success" if row.outcome == audit_trail.SUCCESS else "Failure",
            "EventSeverity": _audit_severity(row),
            "Operation": row.action,
            "ActorUserType": row.actor_type,
            "ActorUserId": row.actor_id,
            "Object": row.target_id,
            "ObjectType": row.target_type,
            "HttpStatusCode": row.status_code,
            "PsRequestId": row.request_id,
            "PsAuditSeq": row.seq,
            "PsAuditPrevHash": row.prev_hash,
            "PsAuditRowHash": row.row_hash,
            "PsAuditHashVersion": payload["hash_version"],
            # As stored (null stays null): it is a chained field.
            "PsAuditDetails": row.details,
        }
    )


def _dumps(record: dict) -> bytes:
    return json.dumps(record, separators=(",", ":"), sort_keys=True, default=str).encode()


def size_batches(records: list[dict], max_bytes: int = MAX_BODY_BYTES) -> list[list[dict]]:
    """Split records into JSON arrays of at most ``max_bytes`` each."""
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 2  # "[]"
    for r in records:
        n = len(_dumps(r)) + 1  # comma
        if n + 2 > max_bytes:
            raise ConnectorError("sentinel: one record is over the request size limit")
        if current and size + n > max_bytes:
            batches.append(current)
            current, size = [], 2
        current.append(r)
        size += n
    if current:
        batches.append(current)
    return batches


# ──────────────────────────────────────────────────────────────────────
# Reading
# ──────────────────────────────────────────────────────────────────────


def encode_call_cursor(ts: datetime, event_id: UUID | str) -> str:
    raw = f"{_ts(ts)}|{event_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_call_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Raises ValueError on anything but a cursor this module made."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        ts, _, event_id = raw.partition("|")
        parsed = datetime.fromisoformat(ts)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed, UUID(event_id)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid cursor") from exc


async def load_calls(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    after: tuple[datetime, UUID] | None,
    since: datetime | None = None,
    until: datetime,
    limit: int,
) -> list[tuple[AIUsageEvent, AIAsset | None]]:
    """Calls after the cursor (or from ``since``) up to ``until``, in
    ``(timestamp, id)`` order."""
    conds = [AIUsageEvent.tenant_id == tenant_id, AIUsageEvent.timestamp <= until]
    if after is not None:
        ts, event_id = after
        conds.append(
            or_(
                AIUsageEvent.timestamp > ts,
                and_(AIUsageEvent.timestamp == ts, AIUsageEvent.id > event_id),
            )
        )
    elif since is not None:
        conds.append(AIUsageEvent.timestamp >= since)
    rows = (
        await db.execute(
            select(AIUsageEvent, AIAsset)
            .outerjoin(AIAsset, AIAsset.id == AIUsageEvent.asset_id)
            .where(*conds)
            .order_by(AIUsageEvent.timestamp, AIUsageEvent.id)
            .limit(limit)
        )
    ).all()
    return [(r[0], r[1]) for r in rows]


async def load_audit(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    after_seq: int | None,
    since: datetime | None = None,
    limit: int,
) -> list[AuditEvent]:
    """Audit rows after ``after_seq`` (or from ``since``) in chain order.
    The chain lock makes seq order commit order, so no settle time."""
    conds = [AuditEvent.tenant_id == tenant_id]
    if after_seq is not None:
        conds.append(AuditEvent.seq > after_seq)
    elif since is not None:
        conds.append(AuditEvent.created_at >= since)
    return list(
        (
            await db.execute(
                select(AuditEvent).where(*conds).order_by(AuditEvent.seq).limit(limit)
            )
        ).scalars()
    )


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────


class SentinelConnection(BaseModel):
    entra_tenant_id: str
    client_id: str
    client_secret: SecretStr
    ingestion_endpoint: str
    dcr_immutable_id: str
    stream_name: str = DEFAULT_STREAM
    cloud: Literal["public", "usgov", "china"] = "public"
    include: list[Literal["calls", "audit"]] = Field(default_factory=lambda: list(KINDS))
    batch_records: int = Field(DEFAULT_BATCH_RECORDS, ge=1, le=10_000)
    max_records_per_run: int = Field(DEFAULT_MAX_RECORDS_PER_RUN, ge=1, le=1_000_000)
    settle_seconds: int = Field(DEFAULT_SETTLE_SECONDS, ge=0, le=86_400)

    @field_validator("entra_tenant_id")
    @classmethod
    def _entra_tenant(cls, v: str) -> str:
        v = v.strip()
        if _GUID.match(v):
            return v.lower()
        # A verified domain (contoso.onmicrosoft.com) also names a tenant.
        return validate_hostname(v)

    @field_validator("client_id")
    @classmethod
    def _client_id(cls, v: str) -> str:
        if not _GUID.match(v.strip()):
            raise ValueError("client_id must be the application (client) id, a GUID")
        return v.strip().lower()

    @field_validator("dcr_immutable_id")
    @classmethod
    def _dcr(cls, v: str) -> str:
        if not _DCR_ID.match(v.strip()):
            raise ValueError("dcr_immutable_id must look like dcr-<32 hex digits>")
        return v.strip()

    @field_validator("stream_name")
    @classmethod
    def _stream(cls, v: str) -> str:
        if not _STREAM.match(v):
            raise ValueError("stream_name must look like Custom-<name>")
        return v

    @field_validator("include")
    @classmethod
    def _include(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("include must name calls, audit or both")
        return list(dict.fromkeys(v))

    @field_validator("ingestion_endpoint")
    @classmethod
    def _endpoint(cls, v: str) -> str:
        try:
            url = httpx.URL(v.strip())
            host = validate_hostname(url.host)  # no IP literal
        except (httpx.InvalidURL, ValueError):
            url, host = None, ""
        if (
            url is None
            or url.scheme != "https"
            or not host
            or url.userinfo
            or url.query
            or url.port not in (None, 443)
            or url.path not in ("", "/")
        ):
            raise ValueError(
                "ingestion_endpoint must be https://<host> (the DCR's or DCE's "
                "logs ingestion endpoint), without a path"
            )
        return f"https://{host}"

    @property
    def token_host(self) -> str:
        return CLOUDS[self.cloud][0]

    @property
    def token_scope(self) -> str:
        return CLOUDS[self.cloud][1]

    @property
    def ingestion_host(self) -> str:
        return httpx.URL(self.ingestion_endpoint).host


class SentinelSettings(BaseSettings):
    sentinel_export: dict[str, SentinelConnection] = {}

    # A malformed PS_SENTINEL_EXPORT must not echo a secret in the error.
    model_config = {"env_prefix": "PS_", "extra": "ignore", "hide_input_in_errors": True}


def load_settings() -> SentinelSettings:
    try:
        return SentinelSettings()
    except ValidationError:
        raise ConnectorError("sentinel: PS_SENTINEL_EXPORT is not valid configuration")


def connection_for_tenant(
    tenant_id: UUID, settings: SentinelSettings | None = None
) -> SentinelConnection | None:
    """The tenant's connection with the ingestion host checked against the
    cloud's suffix. Raises ConnectorError on bad configuration."""
    settings = settings or load_settings()
    conn = settings.sentinel_export.get(str(tenant_id))
    if conn is None:
        return None
    suffix = CLOUDS[conn.cloud][2]
    try:
        validate_hostname(conn.ingestion_host, (suffix,))
    except ValueError:
        raise ConnectorError(
            f"sentinel: ingestion_endpoint must be a host under {suffix} "
            f"for cloud {conn.cloud}"
        )
    if conn.ingestion_host == suffix.lstrip("."):
        raise ConnectorError("sentinel: ingestion_endpoint must name a DCR or DCE host")
    return conn


# ──────────────────────────────────────────────────────────────────────
# Client
# ──────────────────────────────────────────────────────────────────────


class SentinelClient:
    """Entra client credentials token + Logs Ingestion API upload."""

    def __init__(
        self,
        conn: SentinelConnection,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = default_resolver,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ):
        self._conn = conn
        self._entra = GuardedClient(
            f"https://{conn.token_host}",
            name="entra",
            transport=transport,
            resolver=resolver,
            sleep=sleep,
        )
        self._ingest = GuardedClient(
            conn.ingestion_endpoint,
            name="sentinel",
            transport=transport,
            resolver=resolver,
            sleep=sleep,
        )
        self._token = BearerToken(self._ingest, self._fetch_token, clock=clock)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._entra.__aexit__(*exc)
        await self._ingest.__aexit__(*exc)

    async def _fetch_token(self) -> Any:
        conn = self._conn
        return await self._entra.json(
            "POST",
            f"/{conn.entra_tenant_id}/oauth2/v2.0/token",
            what="token request",
            data={
                "grant_type": "client_credentials",
                "client_id": conn.client_id,
                "client_secret": conn.client_secret.get_secret_value(),
                "scope": conn.token_scope,
            },
        )

    async def upload(self, records: list[dict]) -> None:
        conn = self._conn
        body = b"[" + b",".join(_dumps(r) for r in records) + b"]"
        await self._token.request(
            "POST",
            f"/dataCollectionRules/{conn.dcr_immutable_id}/streams/{conn.stream_name}",
            what="logs ingestion upload",
            expected=(204,),
            params={"api-version": INGESTION_API_VERSION},
            content=body,
            headers={"Content-Type": "application/json"},
        )


# ──────────────────────────────────────────────────────────────────────
# Export job
# ──────────────────────────────────────────────────────────────────────


async def _push(client: SentinelClient, records: list[dict]) -> int:
    requests = 0
    for batch in size_batches(records):
        await client.upload(batch)
        requests += 1
    return requests


async def export_sentinel(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    settings: SentinelSettings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver = default_resolver,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
    now: datetime | None = None,
) -> dict:
    """Push the tenant's new AI call evidence and audit rows to its Log
    Analytics workspace. Cursors move only past accepted batches and are
    saved even when a later batch fails."""
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

    counts = {"calls": 0, "audit": 0, "requests": 0}
    until = now - timedelta(seconds=conn.settle_seconds)
    error: str | None = None
    try:
        async with SentinelClient(
            conn, transport=transport, resolver=resolver, sleep=sleep, clock=clock
        ) as client:
            if "calls" in conn.include:
                cursor = state.get("calls_cursor")
                after = decode_call_cursor(cursor) if cursor else None
                while counts["calls"] < conn.max_records_per_run:
                    limit = min(conn.batch_records, conn.max_records_per_run - counts["calls"])
                    rows = await load_calls(db, tenant_id, after=after, until=until, limit=limit)
                    if not rows:
                        break
                    counts["requests"] += await _push(
                        client, [call_record(e, a) for e, a in rows]
                    )
                    last = rows[-1][0]
                    after = (last.timestamp, last.id)
                    state["calls_cursor"] = encode_call_cursor(*after)
                    counts["calls"] += len(rows)
                    if len(rows) < limit:
                        break
            if "audit" in conn.include:
                seq = state.get("audit_seq")
                while counts["audit"] < conn.max_records_per_run:
                    limit = min(conn.batch_records, conn.max_records_per_run - counts["audit"])
                    rows = await load_audit(db, tenant_id, after_seq=seq, limit=limit)
                    if not rows:
                        break
                    counts["requests"] += await _push(client, [audit_record(r) for r in rows])
                    seq = rows[-1].seq
                    state["audit_seq"] = seq
                    counts["audit"] += len(rows)
                    if len(rows) < limit:
                        break
    except (ConnectorError, ValueError) as exc:
        error = str(exc) if isinstance(exc, ConnectorError) else "invalid saved cursor"

    state.update(
        last_run_at=now.isoformat(),
        last_status="error" if error else "ok",
        last_error=error,
        last_counts=counts,
    )
    await save_sync_state(db, tenant_id, CONNECTOR, state)
    result = {"tenant_id": str(tenant_id), **counts}
    if error:
        logger.warning("sentinel export failed for tenant %s: %s", tenant_id, error)
        return {**result, "status": "error", "error": error}
    logger.info("sentinel export: tenant=%s %s", tenant_id, counts)
    return {**result, "status": "ok"}


async def _record_unexpected_failure(
    session_factory, tenant_id: UUID, exc: Exception, now: datetime
) -> dict:
    """Save an unexpected failure in the tenant's export status, in a fresh
    session (the run's own may be unusable). Only the exception type is
    stored and logged: its message can carry an upstream body or a secret."""
    error = f"unexpected error ({type(exc).__name__})"
    logger.error("sentinel export failed for tenant %s: %s", tenant_id, error)
    try:
        async with session_factory() as db:
            await rls.set_tenant(db, tenant_id)
            state = await load_sync_state(db, tenant_id, CONNECTOR)
            if state is not None:
                state.update(last_run_at=now.isoformat(), last_status="error", last_error=error)
                await save_sync_state(db, tenant_id, CONNECTOR, state)
    except Exception as save_exc:  # noqa: BLE001 - still report and move on
        logger.error(
            "sentinel export: could not save the failure for tenant %s: %s",
            tenant_id,
            type(save_exc).__name__,
        )
    return {"tenant_id": str(tenant_id), "status": "error", "error": error}


async def export_all_tenants(session_factory, **kwargs) -> list[dict]:
    """Every configured tenant, each in its own session scoped to it. A
    tenant that fails, even unexpectedly, is recorded in its own export
    status and does not stop the others."""
    settings = kwargs.pop("settings", None) or load_settings()
    out = []
    for tenant in sorted(settings.sentinel_export):
        try:
            tenant_id = UUID(tenant)
        except ValueError:
            out.append({"tenant_id": tenant, "status": "error", "error": "not a UUID"})
            continue
        try:
            async with session_factory() as db:
                await rls.set_tenant(db, tenant_id)
                out.append(await export_sentinel(db, tenant_id, settings=settings, **kwargs))
        except Exception as exc:  # noqa: BLE001 - one tenant must not stop the rest
            now = kwargs.get("now") or datetime.now(UTC)
            out.append(await _record_unexpected_failure(session_factory, tenant_id, exc, now))
    return out


def _succeeded(result: dict) -> bool:
    return result.get("status") in ("ok", "not_configured")


def summarize(results: list[dict]) -> str:
    """One line for the end of an ``--all-tenants`` run."""
    failed = [r for r in results if not _succeeded(r)]
    line = f"sentinel export: {len(results)} tenants, {len(results) - len(failed)} ok"
    if failed:
        detail = ", ".join(
            f"{r.get('tenant_id')} ({r.get('error') or r.get('status')})" for r in failed
        )
        line += f", {len(failed)} failed: {detail}"
    return line


# ──────────────────────────────────────────────────────────────────────
# Operator endpoints
# ──────────────────────────────────────────────────────────────────────

router = APIRouter(
    prefix="/api/v1/admin/connectors/sentinel", tags=["Admin"], route_class=BoundedJsonRoute
)


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


class SentinelSyncBody(BaseModel):
    tenant_id: UUID


@router.post(
    "/sync",
    summary="Push AI call evidence and the audit trail to Microsoft Sentinel (operator)",
    dependencies=[Depends(require_operator)],
)
async def sync_endpoint(body: SentinelSyncBody, request: Request) -> dict:
    db = request.state.db
    await rls.set_tenant(db, body.tenant_id)
    if await db.get(Tenant, body.tenant_id) is None:
        raise _problem(404, "Not Found", "Tenant not found")
    return await export_sentinel(db, body.tenant_id)


@router.get(
    "/status",
    summary="Microsoft Sentinel export state for a tenant (operator)",
    dependencies=[Depends(require_operator)],
)
async def status_endpoint(tenant_id: UUID, request: Request) -> dict:
    await rls.set_tenant(request.state.db, tenant_id)
    state = await load_sync_state(request.state.db, tenant_id, CONNECTOR)
    if state is None:
        raise _problem(404, "Not Found", "Tenant not found")
    try:
        conn = connection_for_tenant(tenant_id)
        config_error = None
    except ConnectorError as exc:
        conn, config_error = None, str(exc)
    return {
        "tenant_id": str(tenant_id),
        "configured": conn is not None,
        # Hosts and ids only: the client secret is never returned.
        "cloud": conn.cloud if conn else None,
        "ingestion_host": conn.ingestion_host if conn else None,
        "dcr_immutable_id": conn.dcr_immutable_id if conn else None,
        "stream_name": conn.stream_name if conn else None,
        "include": conn.include if conn else None,
        "config_error": config_error,
        **state,
    }


# ──────────────────────────────────────────────────────────────────────
# Partner pull endpoints (NDJSON)
# ──────────────────────────────────────────────────────────────────────

partner_router = APIRouter(prefix="/partner/v1/siem", tags=["Partner API"])

NDJSON = "application/x-ndjson"
MAX_PULL_LIMIT = 5000


def _parse_since(since: str | None) -> datetime | None:
    if since is None:
        return None
    try:
        parsed = datetime.fromisoformat(since)
    except ValueError:
        raise _problem(400, "Bad Request", "Invalid `since` timestamp") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _ndjson(
    request: Request,
    response: Response,
    principal: PartnerPrincipal,
    endpoint: str,
    records: list[dict],
    cursor: str | None,
    has_more: bool,
) -> Response:
    await _audit(request, principal, endpoint, "GET", 200, len(records))
    headers = {k: v for k, v in response.headers.items() if k.startswith("x-ratelimit-")}
    headers["X-PS-Has-More"] = "true" if has_more else "false"
    if cursor:
        headers["X-PS-Cursor"] = cursor
    body = b"".join(_dumps(r) + b"\n" for r in records)
    return Response(content=body, media_type=NDJSON, headers=headers)


@partner_router.get("/calls", summary="Per-call routing evidence as NDJSON (SIEM export)")
async def pull_calls(
    request: Request,
    response: Response,
    cursor: str | None = None,
    since: str | None = Query(default=None, description="ISO 8601; ignored with a cursor"),
    limit: int = Query(default=1000, ge=1, le=MAX_PULL_LIMIT),
    settle_seconds: int = Query(default=DEFAULT_SETTLE_SECONDS, ge=0, le=86_400),
    principal: PartnerPrincipal = Depends(require_scope("evidence:read")),  # noqa: B008 - FastAPI dependency marker, by design
) -> Response:
    await _enforce_rate_limit(request, response, principal)
    try:
        after = decode_call_cursor(cursor) if cursor else None
    except ValueError:
        raise _problem(400, "Bad Request", "Invalid `cursor`") from None
    until = datetime.now(UTC) - timedelta(seconds=settle_seconds)
    rows = await load_calls(
        request.state.db,
        principal.tenant_id,
        after=after,
        since=_parse_since(since),
        until=until,
        limit=limit,
    )
    if rows:
        last = rows[-1][0]
        cursor = encode_call_cursor(last.timestamp, last.id)
    return await _ndjson(
        request,
        response,
        principal,
        "/partner/v1/siem/calls",
        [call_record(e, a) for e, a in rows],
        cursor,
        len(rows) == limit,
    )


@partner_router.get("/audit", summary="Audit trail rows with chain hashes as NDJSON (SIEM export)")
async def pull_audit(
    request: Request,
    response: Response,
    cursor: int | None = Query(default=None, ge=0, description="X-PS-Cursor of the last call"),
    since: str | None = Query(default=None, description="ISO 8601; ignored with a cursor"),
    limit: int = Query(default=1000, ge=1, le=MAX_PULL_LIMIT),
    principal: PartnerPrincipal = Depends(require_scope("audit:read")),  # noqa: B008 - FastAPI dependency marker, by design
) -> Response:
    await _enforce_rate_limit(request, response, principal)
    rows = await load_audit(
        request.state.db,
        principal.tenant_id,
        after_seq=cursor,
        since=_parse_since(since),
        limit=limit,
    )
    next_cursor = str(rows[-1].seq) if rows else (str(cursor) if cursor is not None else None)
    return await _ndjson(
        request,
        response,
        principal,
        "/partner/v1/siem/audit",
        [audit_record(r) for r in rows],
        next_cursor,
        len(rows) == limit,
    )


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────


async def _main(tenant: UUID | None, all_tenants: bool) -> Any:
    from collector.app import SessionLocal, engine

    try:
        if all_tenants:
            return await export_all_tenants(SessionLocal)
        async with SessionLocal() as db:
            await rls.set_tenant(db, tenant)
            return await export_sentinel(db, tenant)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m collector.sentinel_export",
        description="Push AI call evidence and the audit trail to Microsoft Sentinel.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tenant", type=UUID)
    group.add_argument("--all-tenants", action="store_true", help="every PS_SENTINEL_EXPORT tenant")
    group.add_argument(
        "--print-schema",
        choices=("table", "dcr"),
        help="print the custom table or DCR JSON and exit",
    )
    args = parser.parse_args(argv)
    if args.print_schema:
        doc = table_definition() if args.print_schema == "table" else dcr_definition()
        print(json.dumps(doc, indent=2))
        return 0
    logging.basicConfig(level=logging.INFO)
    out = asyncio.run(_main(args.tenant, args.all_tenants))
    print(json.dumps(out, indent=2))
    results = out if isinstance(out, list) else [out]
    if args.all_tenants:
        print(summarize(results), file=sys.stderr)
    return 0 if all(_succeeded(r) for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
