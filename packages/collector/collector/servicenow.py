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
  Data flow peer   ``data_system_class`` CI (opt-in)   ``promptshields:system:<hash>``
  Data flow        ``cmdb_rel_ci`` "Sends data to::Receives data from"
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
collector.servicenow --tenant <uuid> [--full] [--dry-run]
[--verify | --no-verify]``.
The cursor is kept in the tenant settings and only advances when IRE
accepted every item.

With ``dry_run`` (``--dry-run`` / ``"dry_run": true``, per run) the sync
reads the Partner API and builds the IRE payloads but sends nothing to
ServiceNow and leaves the tenant's connector state (cursor included)
untouched; it returns what a real run would send, counted per class and
relation type.

With ``verify`` (per tenant in ``PS_SERVICENOW``, or ``--verify`` /
``"verify": true`` per run) the sync reads every CI and relation it just
wrote back through the Table API (``GET /api/now/table/cmdb_ci`` by the
sys_ids IRE returned, ``GET /api/now/table/cmdb_rel_ci`` by parent and
child), matches the rows to the CIs sent on ``correlation_id`` (not on
the order of IRE's answers) and checks each CI has the class sent and
each relation exists with its type. A CI IRE reclassified to a known
subclass of the class sent (``KNOWN_SUBCLASSES``) counts as found and is
listed under ``reclassified``. Reads only: the connector never writes
through the Table API. A CI or relation that is not there, or a CI of
any other class, fails the sync and the cursor stays where it was.
``--no-verify`` / ``"verify": false`` skips the read-back for one run
even when the tenant enables it.
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
# Table API, used read-only by the optional read-back verification.
TABLE_PATH = "/api/now/table"
CI_TABLE = "cmdb_ci"
REL_TABLE = "cmdb_rel_ci"
# sys_ids per Table API query (keeps the URL well under 8 KB), rows per
# page, and pages per query before the read-back gives up.
VERIFY_CHUNK = 100
VERIFY_PAGE = 1000
VERIFY_MAX_PAGES = 50
MAX_MISSING_REPORTED = 20
# CMDB classes IRE may reclassify a CI to (a class -> every class known to
# extend it, at any depth), for the classes this connector writes by
# default. The read-back reports a CI found under one of these as present
# but reclassified; a CI under any other class than the one sent fails.
# Explicit on purpose: the connector does not read sys_db_object.
KNOWN_SUBCLASSES: dict[str, frozenset[str]] = {
    # Business service (CSDM): technical service, service offering and
    # the application service family.
    "cmdb_ci_service": frozenset(
        {
            "cmdb_ci_service_technical",
            "service_offering",
            "cmdb_ci_service_auto",
            "cmdb_ci_service_discovered",
            "cmdb_ci_service_calculated",
            "cmdb_ci_service_by_tags",
            "cmdb_ci_query_based_service",
        }
    ),
    "cmdb_ci_service_auto": frozenset(
        {
            "cmdb_ci_service_discovered",
            "cmdb_ci_service_calculated",
            "cmdb_ci_service_by_tags",
            "cmdb_ci_query_based_service",
        }
    ),
    # Application -> AI Application (CMDB CI Class Models).
    "cmdb_ci_appl": frozenset({"cmdb_ci_appl_ai_application"}),
}

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
FLOW_RELATION_TYPE = "Sends data to::Receives data from"
# Assets per IRE request; each request also carries their vendors, the
# systems their data flows connect to and the relations between them.
DEFAULT_BATCH_SIZE = 50
# Items (CIs) per IRE request. With data flows an asset can bring many
# systems, so a request is closed early once it would exceed this.
MAX_ITEMS_PER_REQUEST = 500
# Distinct flow systems pushed per asset; the rest are counted as skipped.
MAX_SYSTEMS_PER_ASSET = 100
# Flow directions (hand-entered and derived, see collector.data_flows):
# which side is the external system and who sends data to whom.
_SYSTEM_SENDS = frozenset({"input", "inbound"})
_ASSET_SENDS = frozenset({"output", "outbound"})
_BOTH_SEND = frozenset({"bi-directional", "bidirectional", "both"})
# data_flows.UNKNOWN_SOURCE: a flow whose calling service is not known.
_UNKNOWN_SYSTEM = "unknown"
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
    # Opt-in: class for the systems the assets' data flows connect to
    # (needs ``data-flows:read`` on the partner key and an identification
    # rule on correlation_id). None: data flows are not read or pushed.
    data_system_class: str | None = None
    # Opt-in: non-CMDB table for business units (e.g. cmn_department, with
    # an IRE identification rule on name) and the asset CI's reference
    # field pointing at it. None: business units are not pushed.
    business_unit_table: str | None = None
    business_unit_field: str = DEFAULT_BUSINESS_UNIT_FIELD
    # Opt-in: confirmed risk mappings as cmdb_key_value related entries of
    # the asset CI (needs risks:read on the partner key and a Related Entry
    # rule). False: risk mappings are not read or pushed.
    push_risk_mappings: bool = False
    # Read every written CI and relation back through the Table API after
    # the sync and fail the run if any is missing (the integration user
    # needs read access to cmdb_ci and cmdb_rel_ci).
    verify: bool = False
    batch_size: int = Field(DEFAULT_BATCH_SIZE, ge=1, le=100)

    @field_validator(
        "asset_class",
        "self_hosted_asset_class",
        "provider_class",
        "data_system_class",
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


def system_key(name: str) -> str:
    """How flow system names are compared: whitespace collapsed,
    case-insensitive."""
    return " ".join(name.split()).lower()


def system_ref(name: str) -> str:
    """Identifier of a data flow system. System names are free text of up
    to 200 characters and ``correlation_id`` holds 100, so the name is
    hashed (the name itself goes into ``name``)."""
    digest = hashlib.sha256(system_key(name).encode()).hexdigest()[:32]
    return f"{NS}:system:{digest}"


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


def _asset_description(a: dict, flows: list[dict] | None = None) -> str:
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
    if flows:
        parts.append(f"Data flows: {len(flows)}.")
        pii = sorted(
            {
                str(p)
                for f in flows
                if isinstance(f.get("detected_pii_types"), list)
                for p in f["detected_pii_types"]
            }
        )
        if pii:
            parts.append(f"Detected PII types: {', '.join(pii)}.")
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


def asset_item(
    a: dict, conn: ServiceNowConnection, flows: list[dict] | None = None
) -> dict:
    install, operational = _status(a)
    ref = asset_ref(a["id"])
    return {
        "className": conn.class_for(a.get("vendor")),
        "values": {
            "name": _asset_name(a),
            "short_description": _asset_description(a, flows),
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


def system_item(name: str, conn: ServiceNowConnection) -> dict:
    ref = system_ref(name)
    return {
        "className": conn.data_system_class,
        "values": {
            "name": _text(name, MAX_NAME),
            "short_description": "System exchanging data with AI assets, "
            "reported by Prompt Shields.",
            "correlation_id": ref,
        },
        "sys_object_source_info": _source_info(conn.data_source, ref, None),
    }


def flow_edge(flow: dict) -> tuple[str, bool, bool] | None:
    """(system name, system sends to asset, asset sends to system) for a
    Partner API data flow; None when it cannot be mapped (unknown
    direction, no usable system name).

    Derived flows run ``<source> -> <asset's model>`` (``input``) and
    ``<asset's model> -> <destination>`` (``output``), so the external
    system is the source of an input and the destination of an output;
    hand-entered ``inbound`` / ``outbound`` read the same way. For a
    bi-directional flow the source is taken as the external system."""
    direction = str(flow.get("direction") or "").strip().lower()
    if direction in _SYSTEM_SENDS:
        name, system_sends, asset_sends = flow.get("source_system"), True, False
    elif direction in _ASSET_SENDS:
        name, system_sends, asset_sends = flow.get("destination_system"), False, True
    elif direction in _BOTH_SEND:
        name, system_sends, asset_sends = flow.get("source_system"), True, True
    else:
        return None
    if not isinstance(name, str):
        return None
    name = " ".join(name.split())
    if not name or system_key(name) == _UNKNOWN_SYSTEM:
        return None
    return name, system_sends, asset_sends


def _asset_systems(
    flows: list[dict],
) -> tuple[dict[str, tuple[str, bool, bool]], int]:
    """Distinct systems of one asset's flows (key -> name, directions) and
    the number of flows not pushed (unmappable, or past the cap)."""
    systems: dict[str, tuple[str, bool, bool]] = {}
    skipped = 0
    for f in flows:
        edge = flow_edge(f)
        if edge is None:
            skipped += 1
            continue
        name, system_sends, asset_sends = edge
        key = system_key(name)
        if key in systems:
            first, s, a = systems[key]
            systems[key] = (first, s or system_sends, a or asset_sends)
        elif len(systems) < MAX_SYSTEMS_PER_ASSET:
            systems[key] = edge
        else:
            skipped += 1
    return systems, skipped


class PayloadBuilder:
    """One IRE payload: CIs, plus relations referencing them by index.
    A vendor or flow system appears once per payload."""

    def __init__(self, conn: ServiceNowConnection):
        self.conn = conn
        self.items: list[dict] = []
        self.relations: list[dict] = []
        self.assets = 0
        self.flows_skipped = 0
        self._shared: dict[str, int] = {}

    def _shared_item(self, ref: str, make: Any) -> int:
        if ref not in self._shared:
            self.items.append(make())
            self._shared[ref] = len(self.items) - 1
        return self._shared[ref]

    def _relate(self, parent: int, child: int, rel_type: str) -> None:
        self.relations.append({"type": rel_type, "parent": parent, "child": child})

    def add(self, a: dict, flows: list[dict] | None = None) -> None:
        conn = self.conn
        self.items.append(asset_item(a, conn, flows))
        self.assets += 1
        asset_idx = len(self.items) - 1
        if conn.provider_class is not None:
            vendor = str(a["vendor"])
            idx = self._shared_item(
                provider_ref(vendor), lambda: provider_item(vendor, conn)
            )
            self._relate(asset_idx, idx, RELATION_TYPE)
        if conn.data_system_class is None or not flows:
            return
        systems, skipped = _asset_systems(flows)
        self.flows_skipped += skipped
        for name, system_sends, asset_sends in systems.values():
            idx = self._shared_item(
                system_ref(name), lambda n=name: system_item(n, conn)
            )
            if system_sends:
                self._relate(idx, asset_idx, FLOW_RELATION_TYPE)
            if asset_sends:
                self._relate(asset_idx, idx, FLOW_RELATION_TYPE)

    def payload(self) -> dict:
        return {"items": self.items, "relations": self.relations}


def _pushable(a: dict) -> bool:
    return bool(a.get("id")) and bool(a.get("vendor"))


def _cost(flows: list[dict] | None, conn: ServiceNowConnection) -> int:
    """Upper bound of the items one asset adds to a payload."""
    n = 1 + (conn.provider_class is not None)
    if conn.data_system_class is not None and flows:
        n += min(len(flows), MAX_SYSTEMS_PER_ASSET)
    if conn.business_unit_table is not None:
        n += 1  # its business unit record, added after the payload is built
    return n


def build_payloads(
    assets: list[dict],
    conn: ServiceNowConnection,
    flows: dict[str, list[dict]] | None = None,
) -> list[PayloadBuilder]:
    """IRE payloads for ``assets`` (Partner API asset dicts) and their
    data flows (asset id -> flows): at most ``batch_size`` assets and,
    unless a single asset needs more, ``MAX_ITEMS_PER_REQUEST`` items
    each. Assets without id or vendor are skipped."""
    flows = flows or {}
    out: list[PayloadBuilder] = []
    current = PayloadBuilder(conn)
    for a in assets:
        if not _pushable(a):
            continue
        asset_flows = flows.get(a["id"])
        cost = _cost(asset_flows, conn)
        if current.assets and (
            current.assets >= conn.batch_size
            or len(current.items) + cost > MAX_ITEMS_PER_REQUEST
        ):
            out.append(current)
            current = PayloadBuilder(conn)
        current.add(a, asset_flows)
    if current.assets:
        out.append(current)
    return out


def build_payload(
    assets: list[dict],
    conn: ServiceNowConnection,
    flows: dict[str, list[dict]] | None = None,
) -> dict:
    """One IRE payload for all of ``assets``: one CI per asset, one per
    vendor and per data flow system (once per payload), asset -> vendor
    relations and data flow relations, referencing items by index."""
    builder = PayloadBuilder(conn)
    for a in assets:
        if _pushable(a):
            builder.add(a, (flows or {}).get(a["id"]))
    return builder.payload()


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
    return parse_ire_result(body, sent)[0]


def parse_ire_result(
    body: Any, sent: dict
) -> tuple[dict[str, int], list[tuple[str, str]]]:
    """`check_ire_result` plus the class and sys_id IRE reported for every
    item. No order is assumed: the read-back matches them to the items
    sent on ``correlation_id``."""
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
    results: list[tuple[str, str]] = []
    for entry in items:
        if not isinstance(entry, dict):
            failed.append("ERROR")
            continue
        codes = _error_codes(entry)
        if codes or not _SYS_ID.match(str(entry.get("sysId") or "")):
            failed.extend(codes or ["NO_SYS_ID"])
            continue
        cls = entry.get("className")
        results.append((cls if isinstance(cls, str) else "", entry["sysId"]))
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
    return ops, results


# ──────────────────────────────────────────────────────────────────────
# Read-back verification (Table API, read only)
# ──────────────────────────────────────────────────────────────────────


class Expected:
    """What a sync wrote: the CIs sent (correlation_id -> class), the
    relations between them (parent, child correlation_id, type) and what
    IRE answered (sys_id -> class). Keyed on correlation_id, not on the
    position of IRE's answers, so a reordered response changes nothing."""

    def __init__(self) -> None:
        self.cis: dict[str, str] = {}
        self.relations: dict[tuple[str, str, str], None] = {}
        self.returned: dict[str, str] = {}

    def add(
        self,
        payload: dict,
        results: list[tuple[str, str]],
        conn: ServiceNowConnection,
    ) -> None:
        refs: list[str | None] = []
        for item in payload["items"]:
            ref = (item.get("values") or {}).get("correlation_id")
            if item.get("className") == conn.business_unit_table or not ref:
                ref = None  # non-CMDB records (business units) are not CIs
            else:
                self.cis[ref] = item["className"]
            refs.append(ref)
        for rel in payload["relations"]:
            parent, child = refs[rel["parent"]], refs[rel["child"]]
            if parent and child:
                self.relations[(parent, child, rel["type"])] = None
        for cls, sys_id in results:
            if cls != conn.business_unit_table:
                self.returned[sys_id] = cls


def class_status(sent: str, got: str) -> str:
    """``same``, ``subclass`` (``got`` is a known subclass of ``sent``:
    IRE reclassified the CI) or ``other``."""
    if got == sent:
        return "same"
    return "subclass" if got in KNOWN_SUBCLASSES.get(sent, ()) else "other"


def _class_label(cls: str) -> str:
    """A class name from a response, safe to report."""
    return cls if _TABLE.match(cls) else "?"


def _field(row: dict, name: str) -> str:
    """A Table API field value: a plain string, or ``{"value": ...}`` for
    a reference returned with its link."""
    v = row.get(name)
    if isinstance(v, dict):
        v = v.get("value")
    return v if isinstance(v, str) else ""


def table_rows(body: Any, what: str) -> list[dict]:
    """Rows of a Table API GET response (``{"result": [...]}``)."""
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, list):
        raise ConnectorError(f"servicenow: {what} returned an unexpected body")
    return [r for r in result if isinstance(r, dict)]


class VerificationFailed(ConnectorError):
    def __init__(self, verification: dict):
        self.verification = verification
        super().__init__(
            f"servicenow: {verification['missing_count']} of "
            f"{verification['checked']} CIs and relations not found after sync"
        )


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

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._token is not None:
            return await self._token.json(method, path, **kwargs)
        conn = self._conn
        auth = (conn.username, conn.password.get_secret_value())
        return await self._http.json(method, path, auth=auth, **kwargs)

    async def identify_reconcile_ids(
        self, payload: dict
    ) -> tuple[dict[str, int], list[tuple[str, str]]]:
        """IRE upsert; counts per operation and each item's class and
        sys_id as IRE reported them."""
        body = await self._call(
            "POST",
            IRE_PATH,
            what="identify and reconcile",
            expected=(200, 201),
            params={"sysparm_data_source": self._conn.data_source},
            json=payload,
            headers={"Accept": "application/json"},
        )
        return parse_ire_result(body, payload)

    async def identify_reconcile(self, payload: dict) -> dict[str, int]:
        return (await self.identify_reconcile_ids(payload))[0]

    async def query_table(
        self, table: str, query: str, fields: list[str], what: str
    ) -> list[dict]:
        """Every row of ``table`` matching the encoded ``query`` (Table API
        GET, paged), with ``fields`` only. Read only."""
        if table not in (CI_TABLE, REL_TABLE):
            raise ConnectorError("servicenow: refusing to read an unexpected table")
        rows: list[dict] = []
        for page in range(VERIFY_MAX_PAGES):
            body = await self._call(
                "GET",
                f"{TABLE_PATH}/{table}",
                what=what,
                params={
                    "sysparm_query": query,
                    "sysparm_fields": ",".join(fields),
                    "sysparm_exclude_reference_link": "true",
                    "sysparm_limit": str(VERIFY_PAGE),
                    "sysparm_offset": str(page * VERIFY_PAGE),
                },
                headers={"Accept": "application/json"},
            )
            batch = table_rows(body, what)
            rows += batch
            if len(batch) < VERIFY_PAGE:
                return rows
        raise ConnectorError(f"servicenow: {what} returned too many rows")

    async def verify(self, expected: Expected) -> dict:
        """Read the expected CIs and relations back; counts and (capped)
        missing identifiers. Every sys_id IRE returned is read from
        cmdb_ci and its row matched to the CI sent on correlation_id. A CI
        is found when exactly one row carries its correlation_id and both
        that row and IRE's answer have the class sent, or a known subclass
        of it (reported as reclassified); any other class fails. A
        relation is found when a cmdb_rel_ci row links the CIs found for
        its parent and child with its type. Queries only carry sys_ids IRE
        returned (32 hex characters)."""
        missing: list[str] = []
        reclassified: list[str] = []
        by_ref: dict[str, set[tuple[str, str]]] = {}
        ids = list(expected.returned)
        for i in range(0, len(ids), VERIFY_CHUNK):
            chunk = ids[i : i + VERIFY_CHUNK]
            rows = await self.query_table(
                CI_TABLE,
                "sys_idIN" + ",".join(chunk),
                ["sys_id", "sys_class_name", "correlation_id"],
                "CI lookup",
            )
            for r in rows:
                sys_id = _field(r, "sys_id")
                if sys_id in expected.returned:
                    by_ref.setdefault(_field(r, "correlation_id"), set()).add(
                        (sys_id, _field(r, "sys_class_name"))
                    )
        found_ci: dict[str, str] = {}
        for ref, sent in expected.cis.items():
            hits = by_ref.get(ref, set())
            if not hits:
                missing.append(ref)
                continue
            if len({sys_id for sys_id, _ in hits}) > 1:
                missing.append(f"{ref} (duplicate CIs)")
                continue
            ((sys_id, got),) = hits
            # The row's class is what the CMDB holds; IRE's answer for the
            # same sys_id (when it named a class) must agree as well.
            classes = [got] + [c for c in (expected.returned[sys_id],) if c]
            other = [c for c in classes if class_status(sent, c) == "other"]
            if other:
                missing.append(f"{ref} (class {_class_label(other[0])})")
                continue
            sub = [c for c in classes if c != sent]
            if sub:
                reclassified.append(f"{ref} ({sent} -> {_class_label(sub[0])})")
            found_ci[ref] = sys_id
        rels = list(expected.relations)
        resolved = [
            (found_ci[p], found_ci[c], t)
            for p, c, t in rels
            if p in found_ci and c in found_ci
        ]
        found_rels: set[tuple[str, str, str]] = set()
        step = VERIFY_CHUNK // 2
        for i in range(0, len(resolved), step):
            chunk = resolved[i : i + step]
            parents = ",".join(sorted({p for p, _, _ in chunk}))
            children = ",".join(sorted({c for _, c, _ in chunk}))
            rows = await self.query_table(
                REL_TABLE,
                f"parentIN{parents}^childIN{children}",
                ["parent", "child", "type.name"],
                "relationship lookup",
            )
            found_rels |= {
                (_field(r, "parent"), _field(r, "child"), _field(r, "type.name"))
                for r in rows
            }
        missing += [
            f"{p} {t} {c}"
            for p, c, t in rels
            if (found_ci.get(p), found_ci.get(c), t) not in found_rels
        ]
        checked = len(expected.cis) + len(rels)
        if reclassified:
            logger.info(
                "servicenow read-back: %d CIs reclassified by IRE", len(reclassified)
            )
        return {
            "checked": checked,
            "found": checked - len(missing),
            "missing_count": len(missing),
            "missing": missing[:MAX_MISSING_REPORTED],
            "reclassified_count": len(reclassified),
            "reclassified": reclassified[:MAX_MISSING_REPORTED],
            "cis_checked": len(expected.cis),
            "relations_checked": len(rels),
        }


# ──────────────────────────────────────────────────────────────────────
# Sync job
# ──────────────────────────────────────────────────────────────────────


def payload_summary(payloads: list[dict]) -> dict:
    """What a set of IRE payloads would write, counted per item class and
    relation type (no values: a dry run result carries no asset data).
    Shared CIs (a vendor in several requests) count once per request."""
    classes: dict[str, int] = {}
    rel_types: dict[str, int] = {}
    for p in payloads:
        for item in p["items"]:
            classes[item["className"]] = classes.get(item["className"], 0) + 1
            for rel in item.get("related", []):
                classes[rel["className"]] = classes.get(rel["className"], 0) + 1
        for rel in p["relations"]:
            rel_types[rel["type"]] = rel_types.get(rel["type"], 0) + 1
    return {
        "item_classes": dict(sorted(classes.items())),
        "relation_types": dict(sorted(rel_types.items())),
    }


async def sync_servicenow(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    full: bool = False,
    dry_run: bool = False,
    verify: bool | None = None,
    settings: ServiceNowSettings | None = None,
    partner_transport: httpx.AsyncBaseTransport | None = None,
    servicenow_transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver = default_resolver,
    sleep: Sleep = asyncio.sleep,
    now: datetime | None = None,
) -> dict:
    """Push the tenant's registry (or what changed since the last run) to
    the ServiceNow CMDB. Records the outcome in the tenant's connector
    state; the cursor only moves when IRE accepted every item (and, with
    ``verify``, every CI and relation was read back). ``verify=None`` uses
    the tenant's setting. ``dry_run`` builds the payloads without calling
    ServiceNow and records nothing."""
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
    verify = conn.verify if verify is None else verify
    verification: dict | None = None
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
                include_data_flows=conn.data_system_class is not None,
                include_risks=conn.push_risk_mappings,
            )
        assets = read.assets
        by_ref = assets_by_ref(assets)

        requests = items = relations = flows_skipped = 0
        operations: dict[str, int] = {}
        extras = {"business_units": 0, "risk_entries": 0}
        batches = build_payloads(assets, conn, read.flows)
        payloads = []
        for batch in batches:
            payload = batch.payload()
            added = add_business_units_and_risks(payload, by_ref, read.risks, conn)
            payloads.append(payload)
            requests += 1
            items += len(payload["items"])
            relations += len(payload["relations"])
            flows_skipped += batch.flows_skipped
            for k, n in added.items():
                extras[k] += n
        if payloads and not dry_run:
            expected = Expected()
            async with ServiceNowClient(
                conn, transport=servicenow_transport, resolver=resolver, sleep=sleep
            ) as sn:
                for payload in payloads:
                    ops, results = await sn.identify_reconcile_ids(payload)
                    for op, n in ops.items():
                        operations[op] = operations.get(op, 0) + n
                    if verify:
                        expected.add(payload, results, conn)
                if verify:
                    verification = await sn.verify(expected)
                    if verification["missing_count"]:
                        raise VerificationFailed(verification)
    except (ConnectorError, ValueError, TypeError, OverflowError) as exc:
        # Malformed Partner API data is a failed run, not a server error.
        error = str(exc) if isinstance(exc, ConnectorError) else "invalid response data"
        logger.warning("servicenow sync failed for tenant %s: %s", tenant_id, error)
        out = {**result, "status": "error", "error": error}
        if dry_run:
            # A dry run records nothing, not even its failure.
            return {**out, "dry_run": True}
        state.update(last_run_at=now.isoformat(), last_status="error", last_error=error)
        if isinstance(exc, VerificationFailed):
            state["last_verification"] = exc.verification
            out["verification"] = exc.verification
        await save_sync_state(db, tenant_id, CONNECTOR, state)
        return out

    counts = {
        "assets": len(assets),
        "ci_items": items,
        "relations": relations,
        "requests": requests,
        "operations": operations,
    }
    if conn.data_system_class is not None:
        counts["data_flows"] = sum(len(v) for v in read.flows.values())
        counts["data_flows_skipped"] = flows_skipped
    if conn.business_unit_table is not None:
        counts["business_units"] = extras["business_units"]
    if conn.push_risk_mappings:
        counts["risk_entries"] = extras["risk_entries"]
    if dry_run:
        # Nothing was sent: what a real run would send, and no state change.
        del counts["operations"]
        logger.info("servicenow dry run: tenant=%s %s", tenant_id, counts)
        return {
            **result,
            "status": "ok",
            "dry_run": True,
            "cursor": read.cursor or state.get("cursor"),
            **counts,
            **payload_summary(payloads),
        }
    state.update(
        cursor=read.cursor or state.get("cursor"),
        last_run_at=now.isoformat(),
        last_status="ok",
        last_error=None,
        last_counts=counts,
    )
    if verification is not None:
        state["last_verification"] = verification
    await save_sync_state(db, tenant_id, CONNECTOR, state)
    logger.info("servicenow sync: tenant=%s %s", tenant_id, counts)
    return {
        **result,
        "status": "ok",
        "cursor": state["cursor"],
        **counts,
        **({"verification": verification} if verification is not None else {}),
    }


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
    # Build the payloads, send nothing, record nothing.
    dry_run: bool = False
    # Read the written CIs and relations back; null uses the tenant's
    # setting.
    verify: bool | None = None


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
    return await sync_servicenow(
        db, body.tenant_id, full=body.full, dry_run=body.dry_run, verify=body.verify
    )


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
        "data_system_class": conn.data_system_class if conn else None,
        "business_unit_table": conn.business_unit_table if conn else None,
        "business_unit_field": conn.business_unit_field if conn else None,
        "push_risk_mappings": conn.push_risk_mappings if conn else None,
        "verify": conn.verify if conn else None,
        "data_source": conn.data_source if conn else None,
        "config_error": config_error,
        **state,
    }


async def _main(
    tenant: UUID, full: bool, dry_run: bool = False, verify: bool | None = None
) -> dict:
    from collector.app import SessionLocal, engine

    try:
        async with SessionLocal() as db:
            await rls.set_tenant(db, tenant)
            return await sync_servicenow(
                db, tenant, full=full, dry_run=dry_run, verify=verify
            )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", type=UUID, required=True)
    parser.add_argument("--full", action="store_true", help="ignore the cursor")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build the payloads and print what would be sent; send nothing",
    )
    verify = parser.add_mutually_exclusive_group()
    verify.add_argument(
        "--verify",
        action="store_const",
        const=True,
        default=None,
        help="read the written CIs and relations back (round-trip check)",
    )
    verify.add_argument(
        "--no-verify",
        dest="verify",
        action="store_const",
        const=False,
        help="skip the read-back for this run even if the tenant enables it",
    )
    args = parser.parse_args(argv)
    out = asyncio.run(_main(args.tenant, args.full, args.dry_run, args.verify))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
