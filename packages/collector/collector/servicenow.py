"""ServiceNow connector: push the AI asset registry into a ServiceNow CMDB.

Reads the tenant's registry through the Partner API (``/export`` for a
full sync, ``/changes`` for incremental ones) and writes it to the CMDB
through the Identification and Reconciliation Engine (IRE) REST API,
``POST /api/now/identifyreconcile``:

  ===============  ==================================  ==============================
  Prompt Shields   CMDB                                Identifier
  ===============  ==================================  ==============================
  AI asset         ``asset_class`` CI                  ``promptshields:asset:<id>``
                   (default ``cmdb_ci_function_ai``,
                   ``self_hosted_asset_class`` for
                   self-hosted vendors)
  Vendor           ``provider_class`` CI               ``promptshields:provider:<v>``
                   (default ``cmdb_ci_service``)
  Asset -> vendor  ``cmdb_rel_ci`` "Depends on::Used by"
  Business unit    ``business_unit_table`` record      source native key
                   (opt-in, e.g. ``cmn_department``)   ``promptshields:business-unit:<bu>``
                   referenced from the asset CI's      (IRE identifies it on ``name``)
                   ``business_unit_field``
                   (default ``department``)
  Risk mapping     ``cmdb_key_value`` related entry    key ``promptshields:risk:<fw>:<cat>``
                   of the asset CI (opt-in)
  ===============  ==================================  ==============================

Business units and risk mappings are opt-in per tenant (off by default):

- Business units are not CIs. IRE supports a fixed set of non-CMDB tables
  in the global scope, Department (``cmn_department``) among them, but a
  payload relationship cannot point at a non-CMDB record. So a business
  unit is sent as a ``cmn_department`` item and linked to the asset CI with
  a ``referenceItems`` entry that fills the CI's ``department`` reference
  field (a ``cmdb_ci`` base field). CSDM's ``business_unit`` table is not on
  IRE's global non-CMDB list; ``cmn_department.business_unit`` can point a
  department at it inside ServiceNow.
- Risk mappings (confirmed ones, from ``/assets/{id}/risks``) go into the
  ``related`` array of the asset CI as ``cmdb_key_value`` entries, one per
  framework and category with the highest level as value, matching the
  AI-BOM ``risk:<framework>:<category>`` properties. IRE writes related
  entries from the same payload under a Related Entry rule, without using
  them for identification. GRC / IRM records (``sn_risk_risk``,
  ``sn_compliance_*``) are not CMDB tables IRE identifies: they need the
  IRM application and its own APIs, and IRM picks CIs up as entities
  (entity filters on the CI class or its key values) instead.

Why IRE and not the Table API: the Table API inserts rows as given, so a
connector writing ``cmdb_ci_*`` with it creates duplicates of CIs that
Discovery or Service Graph connectors already own and bypasses the
reconciliation rules that decide which source may update which attribute.
IRE identifies each item against the class's identification rules (and
the ``sys_object_source`` record for our data source and native key)
before inserting or updating, applies reconciliation and data refresh
rules, creates relationships from the same payload and tags every write
with the data source. ServiceNow asks CMDB integrations to write through
IRE for those reasons.

The AI classes come from the CMDB CI Class Models store app (1.68.0 and
later): ``cmdb_ci_function_ai`` (AI Function) for AI hosted or developed
outside the organisation, which covers the SaaS model APIs Prompt Shields
mostly sees, and ``cmdb_ci_appl_ai_application`` (AI Application, under
``cmdb_ci_appl``) for AI the organisation hosts itself. An asset whose
vendor is in ``self_hosted_vendors`` goes to ``self_hosted_asset_class``,
every other asset to ``asset_class``.

Every CI carries the AI-BOM (CycloneDX) ``bom-ref`` twice: as
``correlation_id`` (the CMDB field meant for an external system's id, and
what the recommended identification rule matches on) and as the
``source_native_key`` of its ``sys_object_source_info``. Re-running a sync
therefore updates the same CIs (idempotent upsert); nothing is ever
deleted. Retired assets get ``install_status`` 7 / ``operational_status``
6 (Retired).

Configuration (operator environment only, per tenant)::

    PS_SERVICENOW='{"<tenant uuid>": {
        "instance_host": "acme.service-now.com",
        "oauth_client_id": "...", "oauth_client_secret": "...",
        "partner_api": {"base_url": "https://collector.example.com",
                        "api_key": "<partner API key>"}}}'

or ``"username"`` / ``"password"`` for basic auth instead of the OAuth
client credentials. Secrets are ``SecretStr``: never logged, stored or
returned. Calls go only to ``https://<instance_host>``, which must end in
one of ``PS_SERVICENOW_ALLOWED_HOST_SUFFIXES`` (default
``.service-now.com``) and resolve to public addresses.

Run with ``POST /api/v1/admin/connectors/servicenow/sync`` (operator only:
``Authorization: Bearer $PS_ADMIN_TOKEN``) or ``python -m
collector.servicenow --tenant <uuid> [--full]``. The cursor is kept in the
tenant settings and only advances when IRE accepted every item.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any, Self
from uuid import UUID

import httpx
from db.models import Tenant
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
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
    read_registry,
    save_sync_state,
    validate_hostname,
)
from collector.provider_usage_admin import require_operator
from collector.sanitize import BoundedJsonRoute

logger = logging.getLogger("prompt_shields.servicenow")

CONNECTOR = "servicenow"
TOKEN_PATH = "/oauth_token.do"
IRE_PATH = "/api/now/identifyreconcile"

# CMDB CI Class Models (store app, 1.68.0+): externally hosted AI (SaaS
# model APIs) and AI the organisation hosts itself.
DEFAULT_ASSET_CLASS = "cmdb_ci_function_ai"
DEFAULT_SELF_HOSTED_ASSET_CLASS = "cmdb_ci_appl_ai_application"
# Vendors (as the registry names them) that mean a self-hosted model
# server rather than a SaaS API.
DEFAULT_SELF_HOSTED_VENDORS = (
    "ollama",
    "vllm",
    "llamacpp",
    "localai",
    "tgi",
    "triton",
    "self_hosted",
)
DEFAULT_PROVIDER_CLASS = "cmdb_ci_service"
DEFAULT_DATA_SOURCE = "PromptShields"
RELATION_TYPE = "Depends on::Used by"
# Business units (opt-in): the asset CI's reference field pointing at the
# non-CMDB business unit table.
DEFAULT_BUSINESS_UNIT_FIELD = "department"
# Risk mappings (opt-in): related entries of the asset CI.
RISK_TABLE = "cmdb_key_value"
MAX_BUSINESS_UNIT_NAME = 100
MAX_KEY = 100
RISK_LEVEL_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
# Assets per IRE request; each request also carries their vendors and
# relations, so a batch is at most 2x items plus 1x relations.
DEFAULT_BATCH_SIZE = 50
MAX_NAME = 255
MAX_DESCRIPTION = 1000

# cmdb_ci choice values.
INSTALL_INSTALLED, INSTALL_RETIRED = "1", "7"
OPERATIONAL, NON_OPERATIONAL, OPERATIONAL_RETIRED = "1", "2", "6"

_TABLE = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
_DATA_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,39}$")
_SYS_ID = re.compile(r"^[0-9a-f]{32}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

NS = aibom.NS


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────


class ServiceNowConnection(BaseModel):
    instance_host: str
    partner_api: PartnerApiConnection
    # OAuth 2.0 client credentials (instance must allow the grant), or
    # basic auth for an integration user. Exactly one pair.
    oauth_client_id: str | None = None
    oauth_client_secret: SecretStr | None = None
    username: str | None = None
    password: SecretStr | None = None
    # Must exist in the cmdb_ci.discovery_source choice list.
    data_source: str = DEFAULT_DATA_SOURCE
    # Class for assets of a SaaS / externally hosted vendor.
    asset_class: str = DEFAULT_ASSET_CLASS
    # Class for assets whose vendor is in self_hosted_vendors; None sends
    # them to asset_class too.
    self_hosted_asset_class: str | None = DEFAULT_SELF_HOSTED_ASSET_CLASS
    self_hosted_vendors: list[str] = list(DEFAULT_SELF_HOSTED_VENDORS)
    # None: push assets only, no vendor CIs or relations.
    provider_class: str | None = DEFAULT_PROVIDER_CLASS
    # Opt-in: non-CMDB table for business units (e.g. cmn_department, with
    # an IRE identification rule on name) and the asset CI's reference
    # field pointing at it. None: business units are not pushed.
    business_unit_table: str | None = None
    business_unit_field: str = DEFAULT_BUSINESS_UNIT_FIELD
    # Opt-in: confirmed risk mappings as cmdb_key_value related entries of
    # the asset CI (needs risks:read on the partner key and a Related Entry
    # rule). False: risk mappings are not read or pushed.
    push_risk_mappings: bool = False
    batch_size: int = Field(DEFAULT_BATCH_SIZE, ge=1, le=100)

    @field_validator(
        "asset_class",
        "self_hosted_asset_class",
        "provider_class",
        "business_unit_table",
        "business_unit_field",
    )
    @classmethod
    def _table(cls, v: str | None) -> str | None:
        if v is not None and not _TABLE.match(v):
            raise ValueError("must be a table or field name, e.g. cmdb_ci_appl")
        return v

    @field_validator("self_hosted_vendors")
    @classmethod
    def _vendors(cls, v: list[str]) -> list[str]:
        return [s.strip().lower() for s in v if s.strip()]

    @field_validator("data_source")
    @classmethod
    def _data_source(cls, v: str) -> str:
        if not _DATA_SOURCE.match(v):
            raise ValueError("data_source must be 1-40 of [A-Za-z0-9_. -]")
        return v

    @model_validator(mode="after")
    def _one_auth_method(self) -> Self:
        oauth = self.oauth_client_id is not None or self.oauth_client_secret is not None
        basic = self.username is not None or self.password is not None
        if oauth == basic:
            raise ValueError(
                "set either oauth_client_id/oauth_client_secret or username/password"
            )
        if oauth and not (self.oauth_client_id and self.oauth_client_secret):
            raise ValueError(
                "oauth_client_id and oauth_client_secret are both required"
            )
        if basic and not (self.username and self.password):
            raise ValueError("username and password are both required")
        return self

    @property
    def auth_method(self) -> str:
        return "oauth_client_credentials" if self.oauth_client_id else "basic"

    def class_for(self, vendor: Any) -> str:
        """CMDB class of an asset of ``vendor``."""
        if (
            self.self_hosted_asset_class
            and str(vendor).strip().lower() in self.self_hosted_vendors
        ):
            return self.self_hosted_asset_class
        return self.asset_class


class ServiceNowSettings(BaseSettings):
    servicenow: dict[str, ServiceNowConnection] = {}
    servicenow_allowed_host_suffixes: list[str] = [".service-now.com"]

    # A malformed PS_SERVICENOW must not echo a secret in the error.
    model_config = {
        "env_prefix": "PS_",
        "extra": "ignore",
        "hide_input_in_errors": True,
    }


def connection_for_tenant(
    tenant_id: UUID, settings: ServiceNowSettings | None = None
) -> ServiceNowConnection | None:
    """The tenant's ServiceNow connection, host validated against the
    allowed suffixes. Raises ConnectorError on bad configuration."""
    if settings is None:
        try:
            settings = ServiceNowSettings()
        except ValidationError:
            raise ConnectorError("servicenow: PS_SERVICENOW is not valid configuration")
    conn = settings.servicenow.get(str(tenant_id))
    if conn is None:
        return None
    suffixes = tuple(
        s for s in settings.servicenow_allowed_host_suffixes if s.strip(".")
    )
    if not suffixes:
        # An empty list must not turn the allowlist off.
        raise ConnectorError("servicenow: PS_SERVICENOW_ALLOWED_HOST_SUFFIXES is empty")
    try:
        host = validate_hostname(conn.instance_host, suffixes)
    except ValueError as exc:
        raise ConnectorError(f"servicenow: instance_host: {exc}")
    return conn.model_copy(update={"instance_host": host})


# ──────────────────────────────────────────────────────────────────────
# Mapping: Partner API JSON -> IRE payload
# ──────────────────────────────────────────────────────────────────────


def asset_ref(asset_id: str) -> str:
    """Same as the AI-BOM ``bom-ref`` of the asset."""
    return f"{NS}:asset:{asset_id}"


def provider_ref(vendor: str) -> str:
    """Same as the AI-BOM ``bom-ref`` of the provider."""
    return f"{NS}:provider:{vendor}"


def _text(value: Any, limit: int) -> str:
    return str(value)[:limit] if value is not None else ""


def _sn_datetime(value: Any) -> str | None:
    """ISO 8601 -> ServiceNow ``yyyy-MM-dd HH:mm:ss`` (UTC). Anything else
    (missing, not a string, not a date) -> None."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
        dt = dt.astimezone(UTC) if dt.tzinfo else dt
    except (ValueError, OverflowError):  # e.g. 0001-01-01T00:00:00+05:00
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _asset_name(a: dict) -> str:
    base = a.get("use_case_name") or (
        f"{a['vendor']} {a['model']}" if a.get("model") else a.get("vendor")
    )
    # The id prefix keeps two use cases with the same name apart in lists.
    return _text(f"{base} [{str(a['id'])[:8]}]", MAX_NAME)


def _asset_description(a: dict) -> str:
    parts = [
        f"AI asset discovered by Prompt Shields ({asset_ref(a['id'])}).",
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
    return _text(" ".join(parts), MAX_DESCRIPTION)


def _status(a: dict) -> tuple[str, str]:
    """(install_status, operational_status) for an asset's lifecycle."""
    state = a.get("lifecycle_state") or "active"
    if state == "retired":
        return INSTALL_RETIRED, OPERATIONAL_RETIRED
    if state == "stale":
        return INSTALL_INSTALLED, NON_OPERATIONAL
    return INSTALL_INSTALLED, OPERATIONAL


def _source_info(data_source: str, native_key: str, seen: str | None) -> dict:
    info = {"source_name": data_source, "source_native_key": native_key}
    if seen:
        info["source_recency_timestamp"] = seen
    return info


def asset_item(a: dict, conn: ServiceNowConnection) -> dict:
    install, operational = _status(a)
    ref = asset_ref(a["id"])
    return {
        "className": conn.class_for(a.get("vendor")),
        "values": {
            "name": _asset_name(a),
            "short_description": _asset_description(a),
            "correlation_id": ref,
            "install_status": install,
            "operational_status": operational,
        },
        "sys_object_source_info": _source_info(
            conn.data_source, ref, _sn_datetime(a.get("last_seen"))
        ),
    }


def provider_item(vendor: str, conn: ServiceNowConnection) -> dict:
    ref = provider_ref(vendor)
    return {
        "className": conn.provider_class,
        "values": {
            "name": _text(vendor, MAX_NAME),
            "short_description": "AI vendor in use, reported by Prompt Shields.",
            "correlation_id": ref,
        },
        "sys_object_source_info": _source_info(conn.data_source, ref, None),
    }


def build_payload(assets: list[dict], conn: ServiceNowConnection) -> dict:
    """One IRE payload for ``assets`` (Partner API asset dicts): one CI per
    asset, one per vendor (once per payload) and an asset -> vendor
    relation referencing them by index."""
    items: list[dict] = []
    relations: list[dict] = []
    provider_index: dict[str, int] = {}
    for a in assets:
        if not a.get("id") or not a.get("vendor"):
            continue
        items.append(asset_item(a, conn))
        asset_idx = len(items) - 1
        if conn.provider_class is None:
            continue
        vendor = str(a["vendor"])
        if vendor not in provider_index:
            items.append(provider_item(vendor, conn))
            provider_index[vendor] = len(items) - 1
        relations.append(
            {
                "type": RELATION_TYPE,
                "parent": asset_idx,
                "child": provider_index[vendor],
            }
        )
    return {"items": items, "relations": relations}


# ──────────────────────────────────────────────────────────────────────
# Business units and risk mappings (opt-in): added to a built payload
# ──────────────────────────────────────────────────────────────────────


def business_unit_ref(bu: str) -> str:
    """Same external id as the LeanIX connector's business unit."""
    return f"{NS}:business-unit:{bu}"


def _business_unit(a: dict) -> str | None:
    bu = a.get("business_unit")
    if not isinstance(bu, str):
        return None
    return " ".join(bu.split())[:MAX_BUSINESS_UNIT_NAME] or None


def business_unit_item(bu: str, conn: ServiceNowConnection) -> dict:
    """A business unit record. Only ``name`` is sent, so linking to a
    department that already exists under that name changes nothing else
    on it; the identifier travels as the source native key."""
    ref = business_unit_ref(bu)
    return {
        "className": conn.business_unit_table,
        "internal_id": ref,
        "values": {"name": bu},
        "sys_object_source_info": _source_info(conn.data_source, ref, None),
    }


def risk_key(framework: Any, category: str) -> str:
    """``cmdb_key_value.key`` of a risk mapping; hashed when too long, so
    two mappings never collapse into one truncated key."""
    key = f"{NS}:risk:{framework or 'custom'}:{category}"
    if len(key) <= MAX_KEY:
        return key
    return f"{NS}:risk:{hashlib.sha256(key.encode()).hexdigest()[:32]}"


def risk_entries(risks: list[dict]) -> list[dict]:
    """``cmdb_key_value`` related entries for an asset's confirmed risk
    mappings: one per framework and category, valued with the highest
    level (as the AI-BOM risk properties). Anything else is skipped."""
    levels: dict[str, str] = {}
    for r in risks:
        if (r.get("status") or "confirmed") != "confirmed":
            continue
        category = r.get("risk_category")
        if not isinstance(category, str) or not category.strip():
            continue
        level = str(r.get("risk_level") or "").strip().lower()
        if level not in RISK_LEVEL_RANK:
            continue
        key = risk_key(r.get("framework"), category.strip())
        if RISK_LEVEL_RANK[level] > RISK_LEVEL_RANK.get(levels.get(key, ""), -1):
            levels[key] = level
    return [
        {"className": RISK_TABLE, "values": {"key": k, "value": levels[k]}}
        for k in sorted(levels)
    ]


def assets_by_ref(assets: list[dict]) -> dict[str, dict]:
    """Asset dicts keyed on the ``correlation_id`` of their CI."""
    return {asset_ref(a["id"]): a for a in assets if a.get("id")}


def add_business_units_and_risks(
    payload: dict,
    assets: dict[str, dict],
    risks: dict[str, list[dict]],
    conn: ServiceNowConnection,
) -> dict[str, int]:
    """Extend a built IRE payload in place: one business unit record per
    distinct business unit, referenced from its assets' CIs
    (``referenceItems``), and risk mappings as related entries of the
    asset CIs. ``assets`` comes from `assets_by_ref`; ``risks`` maps asset
    id -> risk mappings. Leaves the payload untouched when neither option
    is enabled. Returns the counts added."""
    counts = {"business_units": 0, "risk_entries": 0}
    if conn.business_unit_table is None and not conn.push_risk_mappings:
        return counts
    added: set[str] = set()
    references: list[dict] = []
    for item in list(payload["items"]):
        a = assets.get((item.get("values") or {}).get("correlation_id"))
        if a is None:
            continue  # a vendor or other non-asset CI
        bu = _business_unit(a) if conn.business_unit_table else None
        if bu is not None:
            ref = business_unit_ref(bu)
            if ref not in added:
                payload["items"].append(business_unit_item(bu, conn))
                added.add(ref)
            item["internal_id"] = item["values"]["correlation_id"]
            references.append(
                {
                    "referencedBy": item["internal_id"],
                    "referenced": ref,
                    "referenceField": conn.business_unit_field,
                }
            )
        if conn.push_risk_mappings:
            entries = risk_entries(risks.get(a["id"]) or [])
            if entries:
                item["related"] = entries
                counts["risk_entries"] += len(entries)
    if references:
        payload["referenceItems"] = references
    counts["business_units"] = len(added)
    return counts


# ──────────────────────────────────────────────────────────────────────
# IRE response
# ──────────────────────────────────────────────────────────────────────


def _error_codes(entry: dict) -> list[str]:
    """Error codes of one IRE result entry. Messages are not kept: they
    can quote payload values."""
    codes = []
    for e in entry.get("errors") or []:
        code = e.get("error") if isinstance(e, dict) else None
        codes.append(
            code if isinstance(code, str) and _ERROR_CODE.match(code) else "ERROR"
        )
    return codes


def check_ire_result(body: Any, sent: dict) -> dict[str, int]:
    """Validate an IRE response against the payload sent; returns counts
    per operation. Any item or relation error fails the whole request."""
    result = body.get("result") if isinstance(body, dict) else None
    if isinstance(result, str):
        # Some releases return the result as a JSON string.
        try:
            result = json.loads(result)
        except ValueError:
            result = None
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise ConnectorError("servicenow: IRE returned an unexpected body")
    items = result["items"]
    if len(items) != len(sent["items"]):
        raise ConnectorError(
            f"servicenow: IRE answered {len(items)} of {len(sent['items'])} items"
        )
    failed: list[str] = []
    ops: dict[str, int] = {}
    for entry in items:
        if not isinstance(entry, dict):
            failed.append("ERROR")
            continue
        codes = _error_codes(entry)
        if codes or not _SYS_ID.match(str(entry.get("sysId") or "")):
            failed.extend(codes or ["NO_SYS_ID"])
            continue
        op = str(entry.get("operation") or "UNKNOWN").upper()
        ops[op] = ops.get(op, 0) + 1
    for entry in result.get("relations") or []:
        if isinstance(entry, dict):
            failed.extend(_error_codes(entry))
    if failed:
        distinct = sorted(set(failed))[:5]
        raise ConnectorError(
            f"servicenow: IRE rejected {len(failed)} entries ({', '.join(distinct)})"
        )
    return ops


# ──────────────────────────────────────────────────────────────────────
# ServiceNow client
# ──────────────────────────────────────────────────────────────────────


class ServiceNowClient:
    def __init__(
        self,
        conn: ServiceNowConnection,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = default_resolver,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ):
        self._conn = conn
        self._http = GuardedClient(
            f"https://{conn.instance_host}",
            name="servicenow",
            transport=transport,
            resolver=resolver,
            sleep=sleep,
        )
        self._token = (
            BearerToken(self._http, self._fetch_token, clock=clock)
            if conn.oauth_client_id
            else None
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.__aexit__(*exc)

    async def _fetch_token(self) -> Any:
        conn = self._conn
        return await self._http.json(
            "POST",
            TOKEN_PATH,
            what="token request",
            data={
                "grant_type": "client_credentials",
                "client_id": conn.oauth_client_id,
                "client_secret": conn.oauth_client_secret.get_secret_value(),
            },
        )

    async def identify_reconcile(self, payload: dict) -> dict[str, int]:
        kwargs: dict[str, Any] = {
            "what": "identify and reconcile",
            "expected": (200, 201),
            "params": {"sysparm_data_source": self._conn.data_source},
            "json": payload,
            "headers": {"Accept": "application/json"},
        }
        if self._token is not None:
            body = await self._token.json("POST", IRE_PATH, **kwargs)
        else:
            conn = self._conn
            auth = (conn.username, conn.password.get_secret_value())
            body = await self._http.json("POST", IRE_PATH, auth=auth, **kwargs)
        return check_ire_result(body, payload)


# ──────────────────────────────────────────────────────────────────────
# Sync job
# ──────────────────────────────────────────────────────────────────────


async def sync_servicenow(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    full: bool = False,
    settings: ServiceNowSettings | None = None,
    partner_transport: httpx.AsyncBaseTransport | None = None,
    servicenow_transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver = default_resolver,
    sleep: Sleep = asyncio.sleep,
    now: datetime | None = None,
) -> dict:
    """Push the tenant's registry (or what changed since the last run) to
    the ServiceNow CMDB. Records the outcome in the tenant's connector
    state; the cursor only moves when IRE accepted every item."""
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
                partner,
                since,
                include_data_flows=False,
                include_risks=conn.push_risk_mappings,
            )
        assets = read.assets
        by_ref = assets_by_ref(assets)

        requests = items = relations = 0
        operations: dict[str, int] = {}
        extras = {"business_units": 0, "risk_entries": 0}
        if assets:
            async with ServiceNowClient(
                conn, transport=servicenow_transport, resolver=resolver, sleep=sleep
            ) as sn:
                for i in range(0, len(assets), conn.batch_size):
                    payload = build_payload(assets[i : i + conn.batch_size], conn)
                    if not payload["items"]:
                        continue
                    added = add_business_units_and_risks(
                        payload, by_ref, read.risks, conn
                    )
                    ops = await sn.identify_reconcile(payload)
                    requests += 1
                    items += len(payload["items"])
                    relations += len(payload["relations"])
                    for op, n in ops.items():
                        operations[op] = operations.get(op, 0) + n
                    for k, n in added.items():
                        extras[k] += n
    except (ConnectorError, ValueError, TypeError, OverflowError) as exc:
        # Malformed Partner API data is a failed run, not a server error.
        error = str(exc) if isinstance(exc, ConnectorError) else "invalid response data"
        logger.warning("servicenow sync failed for tenant %s: %s", tenant_id, error)
        state.update(last_run_at=now.isoformat(), last_status="error", last_error=error)
        await save_sync_state(db, tenant_id, CONNECTOR, state)
        return {**result, "status": "error", "error": error}

    counts = {
        "assets": len(assets),
        "ci_items": items,
        "relations": relations,
        "requests": requests,
        "operations": operations,
    }
    if conn.business_unit_table is not None:
        counts["business_units"] = extras["business_units"]
    if conn.push_risk_mappings:
        counts["risk_entries"] = extras["risk_entries"]
    state.update(
        cursor=read.cursor or state.get("cursor"),
        last_run_at=now.isoformat(),
        last_status="ok",
        last_error=None,
        last_counts=counts,
    )
    await save_sync_state(db, tenant_id, CONNECTOR, state)
    logger.info("servicenow sync: tenant=%s %s", tenant_id, counts)
    return {**result, "status": "ok", "cursor": state["cursor"], **counts}


# ──────────────────────────────────────────────────────────────────────
# Operator endpoints and CLI
# ──────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/v1/admin/connectors/servicenow", tags=["Admin"], route_class=BoundedJsonRoute)


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


class ServiceNowSyncBody(BaseModel):
    tenant_id: UUID
    # Ignore the cursor and push the whole registry.
    full: bool = False


@router.post(
    "/sync",
    summary="Push the AI asset registry to the ServiceNow CMDB (operator)",
    dependencies=[Depends(require_operator)],
)
async def sync_endpoint(body: ServiceNowSyncBody, request: Request) -> dict:
    db = request.state.db
    # One tenant per sync: scope the session to it instead of bypassing RLS.
    await rls.set_tenant(db, body.tenant_id)
    if await db.get(Tenant, body.tenant_id) is None:
        raise _not_found()
    return await sync_servicenow(db, body.tenant_id, full=body.full)


@router.get(
    "/status",
    summary="ServiceNow connector state for a tenant (operator)",
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
        # Host, method and classes only: secrets are never returned.
        "instance_host": conn.instance_host if conn else None,
        "auth_method": conn.auth_method if conn else None,
        "asset_class": conn.asset_class if conn else None,
        "self_hosted_asset_class": conn.self_hosted_asset_class if conn else None,
        "provider_class": conn.provider_class if conn else None,
        "business_unit_table": conn.business_unit_table if conn else None,
        "business_unit_field": conn.business_unit_field if conn else None,
        "push_risk_mappings": conn.push_risk_mappings if conn else None,
        "data_source": conn.data_source if conn else None,
        "config_error": config_error,
        **state,
    }


async def _main(tenant: UUID, full: bool) -> dict:
    from collector.app import SessionLocal, engine

    try:
        async with SessionLocal() as db:
            await rls.set_tenant(db, tenant)
            return await sync_servicenow(db, tenant, full=full)
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
