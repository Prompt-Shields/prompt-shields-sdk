"""Generic SIEM connectors: Splunk HEC, syslog (CEF or JSON over TLS) and an
HTTPS webhook (#353), next to the Microsoft Sentinel push
(collector.sentinel_export).

Three record kinds, pushed per tenant from a saved cursor:

  ``calls``     per-call routing evidence, one record per ``ai_usage_events``
                row, in the Sentinel field names (``sentinel_export.call_record``:
                ``TimeGenerated``, ``EventUid``, ``Operation: ai.call``,
                ``Ps*`` fields). Metadata only: no prompt, response, prompt
                hash, session id or user id.
  ``audit``     the tenant's hash-chained audit trail
                (``sentinel_export.audit_record``) with ``PsAuditSeq``,
                ``PsAuditPrevHash`` and ``PsAuditRowHash``, so the chain can be
                re-verified from the SIEM.
  ``security``  gateway security events stored by collector.security_events,
                in the gateway's own v1 field names (``@timestamp``,
                ``event.name``, CIM ``action`` / ``signature`` / ``severity``,
                ``ps.*``) plus ``ps.tenant_id``.

Configuration (opt-in, operator environment only)::

    PS_SIEM_EXPORT='{"<tenant uuid>": {
      "splunk_hec": {"url": "https://splunk.example.com:8088", "token": "...",
                     "index": "prompt_shields"},
      "syslog":     {"host": "siem.example.com", "port": 6514, "format": "cef"},
      "webhook":    {"url": "https://hooks.example.com/ps", "signing_secret": "..."}
    }}'

Each connector is optional. Credentials (``token``, ``signing_secret``,
``bearer_token``) are ``SecretStr``: never logged, stored or returned; the
status route reports hosts and options only (for the webhook, its host, not
its path). A malformed ``PS_SIEM_EXPORT`` is reported without echoing it.

Egress: every HTTP request goes through ``connectors.GuardedClient`` (https
only, one host, every resolved address public, the connection pinned to the
checked address, no redirects, bounded retries, errors carry the status code
only). Syslog uses the same check (`connectors.assert_public_host`) and
connects to the checked address over TLS with the certificate verified for
the configured host name (RFC 5425 octet-counted framing; plain TCP and UDP
are not offered). ``allow_private_network: true`` admits private addresses
for a SIEM on an internal network (operator choice, per connector);
loopback, link-local and cloud metadata addresses (169.254.169.254,
fd00:ec2::254) stay refused.

Splunk HEC
    ``POST <url>/services/collector/event`` with ``Authorization: Splunk
    <token>``, events batched (at most ~900 kB per request), each with
    ``time``, ``source`` (``prompt-shields``), ``sourcetype``
    (``prompt_shields:call``, ``prompt_shields:audit``,
    ``prompt_shields:security``) and the optional ``index`` / ``host``.
Syslog
    One RFC 5424 message per record (facility ``facility``, default 13 log
    audit; severity from the record), ``MSGID`` = ``AICall``, ``Audit`` or
    ``SecurityEvent``. ``format: cef`` (default) sends ArcSight CEF:
    ``CEF:0|Prompt Shields|<product>|1.0|<signature id>|<name>|<severity>|``
    with ``rt``, ``externalId``, ``cat``, ``outcome`` (and ``act`` for
    security events) plus every other field as an extension whose key is
    the field name with non-alphanumerics replaced by ``_``. ``format:
    json`` sends the record as JSON. Delivery counts as done once the TLS
    socket accepted the bytes; syslog has no application acknowledgement.
Webhook
    ``POST <url>`` with ``{"source": "prompt-shields", "schema_version":
    "1.0", "kind": ..., "tenant_id": ..., "records": [...]}`` (at most
    ~900 kB per request); any 2xx counts as accepted. With a
    ``signing_secret`` the body is signed like partner webhooks
    (``Prompt-Shields-Signature: t=<unix>,v1=<hex HMAC-SHA256 of
    "<t>.<body>">``); with a ``bearer_token`` it is sent as
    ``Authorization: Bearer``.

Run with ``POST /api/v1/admin/connectors/{splunk-hec|syslog|webhook}/sync``
(operator, ``operator:integrations``) or ``python -m collector.siem_export
--tenant <uuid> | --all-tenants [--connector ...]`` from cron. Cursors live
in the tenant settings (``connectors.siem_<connector>``) and move only past
batches the receiver accepted, so a failed run resends from the first
unaccepted batch; ``EventUid`` / ``event.id`` are stable, so a resend is a
duplicate. Calls are pushed once older than ``settle_seconds`` (default
300) by their own timestamp, security events once that long after they
arrived; audit rows need no settle time (the chain lock orders them).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import re
import ssl
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Self
from uuid import UUID

import httpx
from db.models import Tenant
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsError
from sqlalchemy.ext.asyncio import AsyncSession

from collector import rls, security_events
from collector.connectors import (
    ConnectorError,
    GuardedClient,
    Resolver,
    Sleep,
    assert_public_host,
    default_resolver,
    load_sync_state,
    save_sync_state,
    validate_hostname,
)
from collector.provider_usage_admin import require_operator
from collector.sanitize import BoundedJsonRoute
from collector.sentinel_export import (
    SCHEMA_VERSION,
    audit_record,
    call_record,
    decode_call_cursor,
    encode_call_cursor,
    load_audit,
    load_calls,
)
from collector.webhooks import sign_payload

logger = logging.getLogger("prompt_shields.siem_export")

CONNECTORS = ("splunk_hec", "syslog", "webhook")
SLUGS = {"splunk_hec": "splunk-hec", "syslog": "syslog", "webhook": "webhook"}
KINDS = ("calls", "audit", "security")
EVENT_TYPES = {"calls": "AICall", "audit": "Audit", "security": "SecurityEvent"}
SOURCETYPES = {"calls": "call", "audit": "audit", "security": "security"}
SOURCE = "prompt-shields"
MAX_BODY_BYTES = 900_000
DEFAULT_BATCH_RECORDS = 500
DEFAULT_MAX_RECORDS_PER_RUN = 50_000
DEFAULT_SETTLE_SECONDS = 300
SYSLOG_TIMEOUT_S = 30.0
SYSLOG_CONNECT_RETRIES = 2

_SPLUNK_INDEX = re.compile(r"^[a-z0-9_][a-z0-9_-]{0,79}$")
_SPLUNK_FIELD = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SYSLOG_NAME = re.compile(r"^[!-~]{1,48}$")
_SYSLOG_HOSTNAME = re.compile(r"^[!-~]{1,255}$")


def state_key(connector: str) -> str:
    return f"siem_{connector}"


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────


def _https_url(v: str, *, allow_path: bool) -> str:
    """``https://<dns host>[:port][/path]`` without userinfo, query or
    fragment; normalised."""
    try:
        url = httpx.URL(v.strip())
        host = validate_hostname(url.host)  # no IP literal
    except (httpx.InvalidURL, ValueError):
        url, host = None, ""
    path = url.path if url is not None else ""
    if (
        url is None
        or url.scheme != "https"
        or not host
        or url.userinfo
        or url.query
        or url.fragment
        or (not allow_path and path not in ("", "/"))
    ):
        what = "https://<host>[:port][/path]" if allow_path else "https://<host>[:port]"
        raise ValueError(f"must be {what}, without credentials or a query")
    port = f":{url.port}" if url.port not in (None, 443) else ""
    return f"https://{host}{port}{path if allow_path and path != '/' else ''}"


class _Common(BaseModel):
    include: list[Literal["calls", "audit", "security"]] = Field(
        default_factory=lambda: list(KINDS)
    )
    batch_records: int = Field(DEFAULT_BATCH_RECORDS, ge=1, le=10_000)
    max_records_per_run: int = Field(DEFAULT_MAX_RECORDS_PER_RUN, ge=1, le=1_000_000)
    settle_seconds: int = Field(DEFAULT_SETTLE_SECONDS, ge=0, le=86_400)
    # For a SIEM on an internal network. Still TLS with a verified certificate.
    allow_private_network: bool = False

    @field_validator("include")
    @classmethod
    def _include(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("include must name calls, audit, security or several")
        return list(dict.fromkeys(v))


class SplunkHecConnection(_Common):
    url: str
    token: SecretStr
    index: str | None = None
    source: str = SOURCE
    sourcetype_prefix: str = "prompt_shields"
    host: str | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _https_url(v, allow_path=False)

    @field_validator("index")
    @classmethod
    def _index(cls, v: str | None) -> str | None:
        if v is not None and not _SPLUNK_INDEX.match(v):
            raise ValueError("index must be a Splunk index name")
        return v

    @field_validator("source", "sourcetype_prefix", "host")
    @classmethod
    def _field(cls, v: str | None) -> str | None:
        if v is not None and not _SPLUNK_FIELD.match(v):
            raise ValueError("must be 1-128 letters, digits or _ . : -")
        return v


class SyslogConnection(_Common):
    host: str
    port: int = Field(6514, ge=1, le=65535)
    format: Literal["cef", "json"] = "cef"
    facility: int = Field(13, ge=0, le=23)
    app_name: str = SOURCE
    hostname: str = "prompt-shields-collector"
    # A PEM CA bundle for a SIEM with a private CA (operator file path).
    ca_file: str | None = None

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        return validate_hostname(v)

    @field_validator("app_name")
    @classmethod
    def _app(cls, v: str) -> str:
        if not _SYSLOG_NAME.match(v):
            raise ValueError("app_name must be 1-48 printable ASCII characters")
        return v

    @field_validator("hostname")
    @classmethod
    def _hostname(cls, v: str) -> str:
        if not _SYSLOG_HOSTNAME.match(v):
            raise ValueError("hostname must be printable ASCII without spaces")
        return v


class WebhookConnection(_Common):
    url: str
    signing_secret: SecretStr | None = None
    bearer_token: SecretStr | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _https_url(v, allow_path=True)

    @property
    def host(self) -> str:
        return httpx.URL(self.url).host


class SiemTenantConfig(BaseModel):
    splunk_hec: SplunkHecConnection | None = None
    syslog: SyslogConnection | None = None
    webhook: WebhookConnection | None = None


class SiemSettings(BaseSettings):
    siem_export: dict[str, SiemTenantConfig] = {}

    # A malformed PS_SIEM_EXPORT must not echo a secret in the error.
    model_config = {"env_prefix": "PS_", "extra": "ignore", "hide_input_in_errors": True}


def load_settings() -> SiemSettings:
    try:
        return SiemSettings()
    except (ValidationError, SettingsError):
        raise ConnectorError("siem: PS_SIEM_EXPORT is not valid configuration")


Connection = SplunkHecConnection | SyslogConnection | WebhookConnection


def connection_for_tenant(
    tenant_id: UUID, connector: str, settings: SiemSettings | None = None
) -> Connection | None:
    settings = settings or load_settings()
    cfg = settings.siem_export.get(str(tenant_id))
    return getattr(cfg, connector) if cfg is not None else None


def public_config(conn: Connection | None) -> dict:
    """What the status route shows: hosts and options, never a credential
    (and not the webhook path, which may carry a token)."""
    if conn is None:
        return {}
    out: dict[str, Any] = {"include": conn.include}
    if isinstance(conn, SplunkHecConnection):
        url = httpx.URL(conn.url)
        out.update(host=url.host, port=url.port or 443, index=conn.index)
    elif isinstance(conn, SyslogConnection):
        out.update(host=conn.host, port=conn.port, format=conn.format)
    else:
        out.update(
            host=conn.host,
            signed=conn.signing_secret is not None,
            bearer_token=conn.bearer_token is not None,
        )
    return out


# ──────────────────────────────────────────────────────────────────────
# Records
# ──────────────────────────────────────────────────────────────────────


def security_record(row) -> dict:
    """A stored gateway security event as the gateway wrote it (v1), with
    the tenant id."""
    return {
        **security_events.event_fields(row),
        "log.logger": security_events.LOGGER_NAME,
        "service.name": "prompt-shields-gateway",
        "vendor_product": "Prompt Shields Gateway",
        "ps.tenant_id": str(row.tenant_id),
    }


def _record_time(kind: str, rec: dict) -> str | None:
    return rec.get("@timestamp") if kind == "security" else rec.get("TimeGenerated")


def _epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _severity(kind: str, rec: dict) -> str:
    """informational | low | medium | high."""
    raw = rec.get("severity") if kind == "security" else rec.get("EventSeverity")
    value = str(raw or "informational").lower()
    return value if value in ("informational", "low", "medium", "high") else "informational"


def _dumps(record: Any) -> bytes:
    return json.dumps(record, separators=(",", ":"), sort_keys=True, default=str).encode()


def size_batches(items: list[bytes], max_bytes: int = MAX_BODY_BYTES) -> list[list[bytes]]:
    """Group encoded items into batches of at most ``max_bytes`` (plus one
    separator byte per item)."""
    batches: list[list[bytes]] = []
    current: list[bytes] = []
    size = 0
    for item in items:
        n = len(item) + 1
        if n > max_bytes:
            raise ConnectorError("siem: one record is over the request size limit")
        if current and size + n > max_bytes:
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += n
    if current:
        batches.append(current)
    return batches


# CEF ---------------------------------------------------------------------

CEF_SEVERITY = {"informational": 1, "low": 3, "medium": 6, "high": 8}
_CEF_KEY = re.compile(r"[^A-Za-z0-9]")


def cef_header_escape(value: Any) -> str:
    text = str(value).replace("\\", "\\\\").replace("|", "\\|")
    return text.replace("\r", " ").replace("\n", " ")


def cef_value_escape(value: Any) -> str:
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, dict | list):
        text = json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)
    else:
        text = str(value)
    return (
        text.replace("\\", "\\\\")
        .replace("=", "\\=")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def cef_key(field: str) -> str:
    return _CEF_KEY.sub("_", field.lstrip("@"))


def cef_message(kind: str, rec: dict) -> str:
    """One record as an ArcSight CEF:0 line."""
    if kind == "security":
        product = "Prompt Shields Gateway"
        signature_id = rec.get("event.name") or "security"
        name = f"{signature_id} {rec.get('signature', '')}".strip()
        uid, outcome, used = rec.get("event.id"), rec.get("event.outcome"), {
            "@timestamp",
            "event.id",
            "event.outcome",
            "action",
            "log.logger",
            "vendor_product",
            "service.name",
        }
    else:
        product = "Prompt Shields"
        signature_id = rec.get("Operation") or EVENT_TYPES[kind]
        name = "AI call" if kind == "calls" else f"Audit {signature_id}"
        uid, outcome, used = rec.get("EventUid"), rec.get("EventResult"), {
            "TimeGenerated",
            "EventUid",
            "EventResult",
            "EventProduct",
            "EventVendor",
            "EventType",
        }
    header = "|".join(
        cef_header_escape(v)
        for v in (
            "CEF:0",
            "Prompt Shields",
            product,
            SCHEMA_VERSION,
            signature_id,
            name,
            CEF_SEVERITY[_severity(kind, rec)],
        )
    )
    ext: list[tuple[str, Any]] = []
    epoch = _epoch(_record_time(kind, rec))
    if epoch is not None:
        ext.append(("rt", int(epoch * 1000)))
    if uid:
        ext.append(("externalId", uid))
    ext.append(("cat", EVENT_TYPES[kind]))
    if outcome:
        ext.append(("outcome", outcome))
    if kind == "security" and rec.get("action"):
        ext.append(("act", rec["action"]))
    for key in sorted(rec):
        if key in used or rec[key] is None:
            continue
        ext.append((cef_key(key), rec[key]))
    return header + "|" + " ".join(f"{k}={cef_value_escape(v)}" for k, v in ext)


# Syslog ------------------------------------------------------------------

SYSLOG_SEVERITY = {"informational": 6, "low": 5, "medium": 4, "high": 3}


def syslog_message(conn: SyslogConnection, kind: str, rec: dict) -> bytes:
    """One RFC 5424 message (without the RFC 5425 length prefix)."""
    pri = conn.facility * 8 + SYSLOG_SEVERITY[_severity(kind, rec)]
    epoch = _epoch(_record_time(kind, rec))
    ts = (
        datetime.fromtimestamp(epoch, UTC).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        if epoch is not None
        else "-"
    )
    body = cef_message(kind, rec) if conn.format == "cef" else _dumps(rec).decode()
    head = f"<{pri}>1 {ts} {conn.hostname} {conn.app_name} - {EVENT_TYPES[kind]} - "
    return (head + body).encode()


def syslog_frame(message: bytes) -> bytes:
    """RFC 5425 octet counting: ``<length> <message>``."""
    return f"{len(message)} ".encode() + message


# ──────────────────────────────────────────────────────────────────────
# Sinks
# ──────────────────────────────────────────────────────────────────────

Opener = Callable[
    [str, int, ssl.SSLContext, str], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
]
WallClock = Callable[[], float]


async def default_opener(
    address: str, port: int, context: ssl.SSLContext, server_hostname: str
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.wait_for(
        asyncio.open_connection(
            address, port, ssl=context, server_hostname=server_hostname
        ),
        SYSLOG_TIMEOUT_S,
    )


def _http_client(
    base_url: str,
    name: str,
    conn: _Common,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver = default_resolver,
    sleep: Sleep = asyncio.sleep,
    **_ignored: Any,
) -> GuardedClient:
    return GuardedClient(
        base_url,
        name=name,
        transport=transport,
        resolver=resolver,
        sleep=sleep,
        allow_private_network=conn.allow_private_network,
        # Never this host or its cloud metadata service, even with the opt-in.
        allow_local_network=False,
    )


class SplunkHecSink:
    def __init__(self, conn: SplunkHecConnection, tenant_id: UUID, **kwargs: Any):
        self._conn = conn
        self._http = _http_client(conn.url, "splunk_hec", conn, **kwargs)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.__aexit__(*exc)

    def _event(self, kind: str, rec: dict) -> bytes:
        conn = self._conn
        event: dict[str, Any] = {
            "event": rec,
            "source": conn.source,
            "sourcetype": f"{conn.sourcetype_prefix}:{SOURCETYPES[kind]}",
        }
        epoch = _epoch(_record_time(kind, rec))
        if epoch is not None:
            event["time"] = round(epoch, 6)
        if conn.index:
            event["index"] = conn.index
        if conn.host:
            event["host"] = conn.host
        return _dumps(event)

    async def send(self, kind: str, records: list[dict]) -> int:
        requests = 0
        for batch in size_batches([self._event(kind, r) for r in records], MAX_BODY_BYTES):
            await self._http.request(
                "POST",
                "/services/collector/event",
                what="HEC upload",
                expected=(200,),
                content=b"\n".join(batch),
                headers={
                    "Authorization": f"Splunk {self._conn.token.get_secret_value()}",
                    "Content-Type": "application/json",
                },
            )
            requests += 1
        return requests


class WebhookSink:
    def __init__(
        self,
        conn: WebhookConnection,
        tenant_id: UUID,
        *,
        wall_clock: WallClock = time.time,
        **kwargs: Any,
    ):
        self._conn = conn
        self._tenant = str(tenant_id)
        self._wall = wall_clock
        url = httpx.URL(conn.url)
        port = f":{url.port}" if url.port else ""
        self._path = url.path or "/"
        self._http = _http_client(f"https://{url.host}{port}", "siem_webhook", conn, **kwargs)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.__aexit__(*exc)

    def _envelope(self, kind: str) -> tuple[bytes, bytes]:
        head = _dumps(
            {
                "kind": kind,
                "schema_version": SCHEMA_VERSION,
                "source": SOURCE,
                "tenant_id": self._tenant,
            }
        )
        # {"kind":...,"tenant_id":"...","records":[ ... ]}
        return head[:-1] + b',"records":[', b"]}"

    async def send(self, kind: str, records: list[dict]) -> int:
        start, end = self._envelope(kind)
        limit = MAX_BODY_BYTES - len(start) - len(end)
        requests = 0
        for batch in size_batches([_dumps(r) for r in records], limit):
            body = start + b",".join(batch) + end
            headers = {"Content-Type": "application/json"}
            if self._conn.signing_secret is not None:
                headers["Prompt-Shields-Signature"] = sign_payload(
                    self._conn.signing_secret.get_secret_value(), body, int(self._wall())
                )
            if self._conn.bearer_token is not None:
                headers["Authorization"] = (
                    f"Bearer {self._conn.bearer_token.get_secret_value()}"
                )
            await self._http.request(
                "POST",
                self._path,
                what="webhook delivery",
                expected=(200, 201, 202, 204),
                content=body,
                headers=headers,
            )
            requests += 1
        return requests


class SyslogSink:
    """RFC 5424 over TLS (RFC 5425) to one host, connected on first send."""

    def __init__(
        self,
        conn: SyslogConnection,
        tenant_id: UUID,
        *,
        resolver: Resolver = default_resolver,
        sleep: Sleep = asyncio.sleep,
        opener: Opener = default_opener,
        ssl_context: ssl.SSLContext | None = None,
        **_ignored: Any,
    ):
        self._conn = conn
        self._resolver = resolver
        self._sleep = sleep
        self._opener = opener
        self._ssl = ssl_context
        self._writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._writer.wait_closed(), 5)
            self._writer = None

    def _context(self) -> ssl.SSLContext:
        if self._ssl is None:
            try:
                self._ssl = ssl.create_default_context(cafile=self._conn.ca_file)
            except (OSError, ssl.SSLError):
                raise ConnectorError("syslog: ca_file could not be loaded") from None
        return self._ssl

    async def _connect(self) -> asyncio.StreamWriter:
        if self._writer is not None:
            return self._writer
        conn = self._conn
        context = self._context()
        for attempt in range(SYSLOG_CONNECT_RETRIES + 1):
            # Resolve and check on every attempt, then connect to the checked
            # address with SNI and certificate check for the host name.
            address = await assert_public_host(
                conn.host,
                conn.port,
                self._resolver,
                allow_private=conn.allow_private_network,
                allow_local=False,
            )
            try:
                _, writer = await self._opener(address, conn.port, context, conn.host)
            except (OSError, ssl.SSLError, TimeoutError) as exc:
                if attempt == SYSLOG_CONNECT_RETRIES:
                    raise ConnectorError(
                        f"syslog: connection failed ({type(exc).__name__})"
                    ) from None
                await self._sleep(2.0**attempt)
                continue
            self._writer = writer
            return writer
        raise ConnectorError("syslog: connection failed")  # pragma: no cover

    async def send(self, kind: str, records: list[dict]) -> int:
        writer = await self._connect()
        data = b"".join(syslog_frame(syslog_message(self._conn, kind, r)) for r in records)
        try:
            writer.write(data)
            await asyncio.wait_for(writer.drain(), SYSLOG_TIMEOUT_S)
        except (OSError, ssl.SSLError, TimeoutError) as exc:
            self._writer = None
            raise ConnectorError(f"syslog: send failed ({type(exc).__name__})") from None
        return 1


SINKS = {"splunk_hec": SplunkHecSink, "syslog": SyslogSink, "webhook": WebhookSink}


# ──────────────────────────────────────────────────────────────────────
# Export job
# ──────────────────────────────────────────────────────────────────────


async def export_siem(
    db: AsyncSession,
    tenant_id: UUID,
    connector: str,
    *,
    settings: SiemSettings | None = None,
    now: datetime | None = None,
    **sink_kwargs: Any,
) -> dict:
    """Push the tenant's new records of the connector's kinds. Cursors move
    only past accepted batches and are saved even when a later batch fails.

    ``sink_kwargs`` (tests): ``transport``, ``resolver``, ``sleep``,
    ``opener``, ``ssl_context``, ``wall_clock``."""
    if connector not in CONNECTORS:
        raise ValueError(f"unknown connector {connector!r}")
    now = now or datetime.now(UTC)
    key = state_key(connector)
    base = {"tenant_id": str(tenant_id), "connector": connector}
    state = await load_sync_state(db, tenant_id, key)
    if state is None:
        return {**base, "status": "tenant_not_found"}
    try:
        conn = connection_for_tenant(tenant_id, connector, settings)
    except ConnectorError as exc:
        return {**base, "status": "error", "error": str(exc)}
    if conn is None:
        return {**base, "status": "not_configured"}

    counts = {"calls": 0, "audit": 0, "security": 0, "requests": 0}
    until = now - timedelta(seconds=conn.settle_seconds)
    cap = conn.max_records_per_run
    error: str | None = None
    try:
        async with SINKS[connector](conn, tenant_id, **sink_kwargs) as sink:
            if "calls" in conn.include:
                cursor = state.get("calls_cursor")
                after = decode_call_cursor(cursor) if cursor else None
                while counts["calls"] < cap:
                    limit = min(conn.batch_records, cap - counts["calls"])
                    rows = await load_calls(db, tenant_id, after=after, until=until, limit=limit)
                    if not rows:
                        break
                    counts["requests"] += await sink.send(
                        "calls", [call_record(e, a) for e, a in rows]
                    )
                    last = rows[-1][0]
                    after = (last.timestamp, last.id)
                    state["calls_cursor"] = encode_call_cursor(*after)
                    counts["calls"] += len(rows)
                    if len(rows) < limit:
                        break
            if "audit" in conn.include:
                seq = state.get("audit_seq")
                while counts["audit"] < cap:
                    limit = min(conn.batch_records, cap - counts["audit"])
                    rows = await load_audit(db, tenant_id, after_seq=seq, limit=limit)
                    if not rows:
                        break
                    counts["requests"] += await sink.send(
                        "audit", [audit_record(r) for r in rows]
                    )
                    seq = rows[-1].seq
                    state["audit_seq"] = seq
                    counts["audit"] += len(rows)
                    if len(rows) < limit:
                        break
            if "security" in conn.include:
                cursor = state.get("security_cursor")
                after = security_events.decode_cursor(cursor) if cursor else None
                while counts["security"] < cap:
                    limit = min(conn.batch_records, cap - counts["security"])
                    rows = await security_events.load_security_events(
                        db, tenant_id, after=after, limit=limit, received_until=until
                    )
                    if not rows:
                        break
                    counts["requests"] += await sink.send(
                        "security", [security_record(r) for r in rows]
                    )
                    after = (rows[-1].received_at, rows[-1].id)
                    state["security_cursor"] = security_events.encode_cursor(*after)
                    counts["security"] += len(rows)
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
    await save_sync_state(db, tenant_id, key, state)
    result = {**base, **counts}
    if error:
        logger.warning("%s export failed for tenant %s: %s", connector, tenant_id, error)
        return {**result, "status": "error", "error": error}
    logger.info("%s export: tenant=%s %s", connector, tenant_id, counts)
    return {**result, "status": "ok"}


async def _record_unexpected_failure(
    session_factory, tenant_id: UUID, connector: str, exc: Exception, now: datetime
) -> dict:
    """Save an unexpected failure in a fresh session. Only the exception
    type is stored and logged: its message can carry a body or a secret."""
    error = f"unexpected error ({type(exc).__name__})"
    logger.error("%s export failed for tenant %s: %s", connector, tenant_id, error)
    try:
        async with session_factory() as db:
            await rls.set_tenant(db, tenant_id)
            key = state_key(connector)
            state = await load_sync_state(db, tenant_id, key)
            if state is not None:
                state.update(last_run_at=now.isoformat(), last_status="error", last_error=error)
                await save_sync_state(db, tenant_id, key, state)
    except Exception as save_exc:  # noqa: BLE001 - still report and move on
        logger.error(
            "%s export: could not save the failure for tenant %s: %s",
            connector,
            tenant_id,
            type(save_exc).__name__,
        )
    return {
        "tenant_id": str(tenant_id),
        "connector": connector,
        "status": "error",
        "error": error,
    }


async def export_all_tenants(
    session_factory, connectors: tuple[str, ...] = CONNECTORS, **kwargs
) -> list[dict]:
    """Every configured (tenant, connector), each in its own session scoped
    to the tenant. One that fails, even unexpectedly, is recorded in its own
    status and does not stop the others."""
    settings = kwargs.pop("settings", None) or load_settings()
    out = []
    for tenant in sorted(settings.siem_export):
        try:
            tenant_id = UUID(tenant)
        except ValueError:
            out.append({"tenant_id": tenant, "status": "error", "error": "not a UUID"})
            continue
        cfg = settings.siem_export[tenant]
        for connector in connectors:
            if getattr(cfg, connector) is None:
                continue
            try:
                async with session_factory() as db:
                    await rls.set_tenant(db, tenant_id)
                    out.append(
                        await export_siem(
                            db, tenant_id, connector, settings=settings, **kwargs
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - one must not stop the rest
                now = kwargs.get("now") or datetime.now(UTC)
                out.append(
                    await _record_unexpected_failure(
                        session_factory, tenant_id, connector, exc, now
                    )
                )
    return out


def _succeeded(result: dict) -> bool:
    return result.get("status") in ("ok", "not_configured")


def summarize(results: list[dict]) -> str:
    failed = [r for r in results if not _succeeded(r)]
    line = f"siem export: {len(results)} runs, {len(results) - len(failed)} ok"
    if failed:
        detail = ", ".join(
            f"{r.get('tenant_id')}/{r.get('connector', '-')} "
            f"({r.get('error') or r.get('status')})"
            for r in failed
        )
        line += f", {len(failed)} failed: {detail}"
    return line


# ──────────────────────────────────────────────────────────────────────
# Operator endpoints
# ──────────────────────────────────────────────────────────────────────

router = APIRouter(
    prefix="/api/v1/admin/connectors", tags=["Admin"], route_class=BoundedJsonRoute
)


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


class SiemSyncBody(BaseModel):
    tenant_id: UUID


def _add_routes(connector: str) -> None:
    slug = SLUGS[connector]
    label = {"splunk_hec": "Splunk HEC", "syslog": "syslog", "webhook": "the SIEM webhook"}[
        connector
    ]

    async def sync_endpoint(body: SiemSyncBody, request: Request) -> dict:
        db = request.state.db
        await rls.set_tenant(db, body.tenant_id)
        if await db.get(Tenant, body.tenant_id) is None:
            raise _problem(404, "Not Found", "Tenant not found")
        return await export_siem(db, body.tenant_id, connector)

    async def status_endpoint(tenant_id: UUID, request: Request) -> dict:
        await rls.set_tenant(request.state.db, tenant_id)
        state = await load_sync_state(request.state.db, tenant_id, state_key(connector))
        if state is None:
            raise _problem(404, "Not Found", "Tenant not found")
        try:
            conn = connection_for_tenant(tenant_id, connector)
            config_error = None
        except ConnectorError as exc:
            conn, config_error = None, str(exc)
        return {
            "tenant_id": str(tenant_id),
            "connector": connector,
            "configured": conn is not None,
            **public_config(conn),
            "config_error": config_error,
            **state,
        }

    router.add_api_route(
        f"/{slug}/sync",
        sync_endpoint,
        methods=["POST"],
        summary=f"Push AI call evidence, the audit trail and security events to {label} (operator)",
        dependencies=[Depends(require_operator)],
        name=f"siem_{connector}_sync",
    )
    router.add_api_route(
        f"/{slug}/status",
        status_endpoint,
        methods=["GET"],
        summary=f"Export state for {label} (operator)",
        dependencies=[Depends(require_operator)],
        name=f"siem_{connector}_status",
    )


for _connector in CONNECTORS:
    _add_routes(_connector)


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────


async def _main(tenant: UUID | None, connectors: tuple[str, ...]) -> Any:
    from collector.app import SessionLocal, engine

    try:
        if tenant is None:
            return await export_all_tenants(SessionLocal, connectors)
        out = []
        for connector in connectors:
            async with SessionLocal() as db:
                await rls.set_tenant(db, tenant)
                out.append(await export_siem(db, tenant, connector))
        return out
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m collector.siem_export",
        description="Push AI call evidence, the audit trail and gateway security "
        "events to Splunk HEC, syslog or a webhook.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tenant", type=UUID)
    group.add_argument("--all-tenants", action="store_true", help="every PS_SIEM_EXPORT tenant")
    parser.add_argument(
        "--connector",
        action="append",
        choices=CONNECTORS,
        help="limit to this connector (repeatable); default: all",
    )
    args = parser.parse_args(argv)
    connectors = tuple(dict.fromkeys(args.connector or CONNECTORS))
    logging.basicConfig(level=logging.INFO)
    results = asyncio.run(_main(None if args.all_tenants else args.tenant, connectors))
    print(json.dumps(results, indent=2))
    if args.all_tenants:
        print(summarize(results), file=sys.stderr)
    return 0 if all(_succeeded(r) for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
