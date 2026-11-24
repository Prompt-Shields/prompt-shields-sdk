"""Opt-in request/response payload logging (#352).

Off by default. A tenant turns it on through the tenant admin API (scope
``admin:privacy``)::

  GET  /api/v1/admin/payload-logging
  PUT  /api/v1/admin/payload-logging   {"mode": "off|metadata|redacted|full",
                                        "sample_rate": 1.0,
                                        "on_error_only": false,
                                        "routes": ["/v1/chat/completions", "/v1/*"],
                                        "retention_days": 30}

stored in ``tenants.settings["payload_logging"]``. ``mode``:

* ``off`` (the default, also for an absent or invalid setting): nothing is
  stored.
* ``metadata``: route, status, error flag, request and response sizes; no
  content.
* ``redacted``: the request and response with every PII span (email, card,
  IBAN, SSN, phone, IPv4; collector.payload_redaction) replaced by a
  category placeholder **before** anything is encrypted or written.
* ``full``: the request and response as sent. Only an owner may turn it on
  (an admin token issued for a principal with the tenant-wide ``owner``
  role, or an unbound token holding every admin scope, as for owner
  memberships; ``admin_auth.can_manage_owner``). Every change is audited
  (``payload_logging.set``); while it is on, ingest responses carry
  ``x-ps-logging: full``.

``sample_rate`` (0-1) keeps that share of calls, decided from a hash of
tenant and call id, so a resent call gets the same answer. ``on_error_only``
keeps only calls with ``error: true`` or a status of 400 or more.
``routes`` limits logging to those routes (exact, or a prefix ending in
``*``); empty means every route.

Ingest (tenant ingest key, scope ``ingest:write``; one call per request)::

  POST /ingest/payloads  {"call_id": "...", "route": "...", "status_code": 200,
                          "error": false, "user_id": "...",
                          "request": <JSON>, "response": <JSON>}

The tenant's setting is read on every request (no cache), so turning
logging off stops writes at once. The answer (202) says whether the payload
was stored and, if not, why. A call id already stored is not stored again.
Rate limited per tenant with the tenant's ``events`` ingest limit, counted
apart from ``/ingest/events``; the body is bounded like the OTLP receiver's.

Storage: ``call_payloads`` (row-level security), one row per tenant and call
id. Content is the JSON ``{"request", "response"}`` sealed with AES-256-GCM
under a key derived per tenant from the ``PS_WEBHOOK_SECRET_KEYS`` keyring
(collector.secret_keyring), bound to tenant and call id. Without a keyring,
``redacted`` and ``full`` cannot be turned on, and a payload that arrives
anyway is not stored (``reason: encryption_unavailable``). ``user_id`` is
stored only as the tenant-salted hash (collector.user_identity).

Read (Partner API, scope ``logs:read``)::

  GET /partner/v1/calls/{call_id}

Every read, found or not, writes a ``payload.read`` row to the tenant's
hash-chained audit trail before the body is returned; if that write fails
the read fails (503).

Retention per data class (collector.retention): payloads are purged after
the tenant's ``payload_logging.retention_days`` (``PS_PAYLOAD_RETENTION_DAYS_
DEFAULT``, 30 days); usage events, rollups and the audit trail keep their
own periods. A tenant on legal hold (``PUT /api/v1/admin/legal-hold``,
audited ``tenant.legal_hold.set``) is skipped by every purge until the hold
is lifted. Tenant erasure deletes the tenant's payloads; erasure by end user
deletes the user's payloads (by the stored hash, and by the call ids of the
user's usage events).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from db.models import CallPayload, Tenant, uuid7
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from collector import (
    audit_trail,
    secret_keyring,
    tenant_keys,
    tenant_limits,
    user_identity,
)
from collector.admin_auth import (
    AdminPrincipal,
    admin_actor,
    can_manage_owner,
    require_admin,
)
from collector.auth import resolve_tenant
from collector.ingest import _enforce_rate_limit as _enforce_ingest_rate_limit
from collector.otel_ingest import _read_bounded_body
from collector.partner_auth import PartnerPrincipal, require_scope
from collector.partner_rate_limit import RateLimiter, get_payload_rate_limiter
from collector.partner_registry import _enforce_rate_limit
from collector.payload_policy import (  # noqa: F401 - re-exported
    LEGAL_HOLD_KEY,
    MAX_RETENTION_DAYS,
    MAX_ROUTES,
    MIN_RETENTION_DAYS,
    MODES,
    OFF,
    ROUTE_RE,
    SETTINGS_KEY,
    PayloadLoggingConfig,
    config_from_settings,
    default_retention_days,
    legal_hold_since,
    on_legal_hold,
    payload_cutoff,
    retention_days_for,
    route_logged,
    sampled,
)
from collector.payload_redaction import redact_value
from collector.sanitize import (
    BodyTooDeep,
    BoundedJsonRoute,
    loads_bounded,
    too_deep_problem,
)

logger = logging.getLogger("prompt_shields.payload_logging")

CONTENT_MODES = frozenset({"redacted", "full"})
LOGGING_HEADER = "x-ps-logging"
SCOPE = "admin:privacy"
READ_SCOPE = "logs:read"
STORAGE_DATABASE = "database"
MAX_USER_ID_LENGTH = 512

SET_ACTION = "payload_logging.set"
READ_ACTION = "payload.read"
LEGAL_HOLD_ACTION = "tenant.legal_hold.set"

CALL_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")

# Why a payload was not stored.
Reason = Literal[
    "off",
    "not_sampled",
    "not_error",
    "route_not_logged",
    "duplicate",
    "encryption_unavailable",
]

_SEAL_INFO = b"ps-call-payload.v1:"


# ──────────────────────────────────────────────────────────────────────
# Encryption
# ──────────────────────────────────────────────────────────────────────


def keyring_ready() -> bool:
    try:
        return secret_keyring.active_keyring_id() is not None
    except secret_keyring.KeyringNotConfigured:
        return False


def _binding(tenant_id: UUID, call_id: str) -> bytes:
    return f"{tenant_id}:{call_id}".encode()


def _info(tenant_id: UUID) -> bytes:
    # A key per tenant: HKDF info names the tenant.
    return _SEAL_INFO + tenant_id.bytes


def seal_payload(tenant_id: UUID, call_id: str, content: dict) -> str:
    plaintext = json.dumps(content, separators=(",", ":"), sort_keys=True)
    return secret_keyring.seal(plaintext, _binding(tenant_id, call_id), _info(tenant_id))


def unseal_payload(tenant_id: UUID, call_id: str, token: str) -> dict:
    return json.loads(
        secret_keyring.unseal(token, _binding(tenant_id, call_id), _info(tenant_id))
    )


async def _tenant_settings(db: AsyncSession, tenant_id: UUID) -> dict:
    row = (
        await db.execute(select(Tenant.settings).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    return row or {}


# ──────────────────────────────────────────────────────────────────────
# Ingest
# ──────────────────────────────────────────────────────────────────────


@dataclass
class IngestResult:
    call_id: str
    stored: bool = False
    mode: str = "off"
    reason: str | None = None
    redactions: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "stored": self.stored,
            "mode": self.mode,
            "reason": self.reason,
            "redactions": self.redactions,
        }


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


@dataclass
class PayloadIn:
    call_id: str
    route: str | None
    status_code: int | None
    error: bool
    user_id: str | None
    request: Any
    response: Any


def parse_payload(body: Any) -> PayloadIn:
    """Validate an ingest body. Raises a 400 naming the field, never its
    value."""
    if not isinstance(body, dict):
        raise _problem(400, "Bad Request", "Expected a JSON object")
    unknown = sorted(
        set(body) - {"call_id", "route", "status_code", "error", "user_id", "request", "response"}
    )
    if unknown:
        raise _problem(400, "Bad Request", f"Unknown fields: {unknown[:10]}")
    call_id = body.get("call_id")
    if not isinstance(call_id, str) or not CALL_ID_RE.fullmatch(call_id):
        raise _problem(
            400, "Bad Request", "call_id must be 1-128 characters from A-Z a-z 0-9 _ . : -"
        )
    route = body.get("route")
    if route is not None and (not isinstance(route, str) or not ROUTE_RE.fullmatch(route)):
        raise _problem(400, "Bad Request", "route must be 1-256 printable ASCII characters")
    status = body.get("status_code")
    if status is not None and (
        isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599
    ):
        raise _problem(400, "Bad Request", "status_code must be an integer from 100 to 599")
    error = body.get("error", False)
    if not isinstance(error, bool):
        raise _problem(400, "Bad Request", "error must be a boolean")
    user_id = body.get("user_id")
    if user_id is not None and (
        not isinstance(user_id, str)
        or not 1 <= len(user_id) <= MAX_USER_ID_LENGTH
        or "\x00" in user_id
    ):
        raise _problem(
            400, "Bad Request", f"user_id must be a string of 1-{MAX_USER_ID_LENGTH} characters"
        )
    return PayloadIn(
        call_id=call_id,
        route=route,
        status_code=status,
        error=error or (status is not None and status >= 400),
        user_id=user_id,
        request=body.get("request"),
        response=body.get("response"),
    )


def _size(value: Any) -> int:
    if value is None:
        return 0
    return len(json.dumps(value, separators=(",", ":")).encode())


def skip_reason(
    config: PayloadLoggingConfig, tenant_id: UUID, payload: PayloadIn
) -> str | None:
    """Why the tenant's setting keeps this payload out, or None to store it."""
    if config.mode == "off":
        return "off"
    if not route_logged(payload.route, config.routes):
        return "route_not_logged"
    if config.on_error_only and not payload.error:
        return "not_error"
    if not sampled(tenant_id, payload.call_id, config.sample_rate):
        return "not_sampled"
    if config.mode in CONTENT_MODES and not keyring_ready():
        return "encryption_unavailable"
    return None


async def store_payload(
    db: AsyncSession, tenant_id: UUID, payload: PayloadIn, *, now: datetime | None = None
) -> IngestResult:
    """Apply the tenant's setting (read now) and store the payload if it
    says so. Commits."""
    config = config_from_settings(await _tenant_settings(db, tenant_id))
    result = IngestResult(call_id=payload.call_id, mode=config.mode)
    reason = skip_reason(config, tenant_id, payload)
    if reason is not None:
        result.reason = reason
        if reason == "encryption_unavailable":
            logger.warning(
                "payload not stored: PS_WEBHOOK_SECRET_KEYS is not set tenant=%s", tenant_id
            )
        return result

    values: dict[str, Any] = {
        "tenant_id": tenant_id,
        "call_id": payload.call_id,
        "received_at": now or datetime.now(UTC),
        "mode": config.mode,
        "route": payload.route,
        "status_code": payload.status_code,
        "is_error": payload.error,
        "request_bytes": _size(payload.request),
        "response_bytes": _size(payload.response),
        "redactions": {},
        "storage": STORAGE_DATABASE,
        "key_id": None,
        "ciphertext": None,
        "user_id_hash": None,
    }
    if payload.user_id is not None:
        salt = await user_identity.tenant_salt(db, tenant_id)
        values["user_id_hash"] = user_identity.hash_user_id(salt, payload.user_id)
    if config.mode in CONTENT_MODES:
        content = {"request": payload.request, "response": payload.response}
        if config.mode == "redacted":
            # Before anything is encrypted or written.
            content, counts = redact_value(content)
            values["redactions"] = counts
            result.redactions = counts
        values["ciphertext"] = seal_payload(tenant_id, payload.call_id, content)
        values["key_id"] = secret_keyring.token_key_id(values["ciphertext"])

    stmt = (
        pg_insert(CallPayload)
        .values(id=uuid7(), **values)
        .on_conflict_do_nothing(constraint="ux_call_payloads_call")
        .returning(CallPayload.id)
    )
    inserted = (await db.execute(stmt)).scalar_one_or_none()
    await db.commit()
    if inserted is None:
        result.reason = "duplicate"
        result.redactions = {}
        return result
    result.stored = True
    return result


router = APIRouter()

_LIMITER = Depends(get_payload_rate_limiter)


@router.post("/ingest/payloads", status_code=202)
async def ingest_payload(
    request: Request,
    response: Response,
    limiter: Annotated[RateLimiter, _LIMITER],
) -> dict:
    """One call's request and response, stored as the tenant's payload
    logging setting says (nothing while it is off)."""
    tenant_id = await resolve_tenant(request, tenant_keys.INGEST_WRITE)
    limit = await tenant_limits.limit_for(request.state.db, tenant_id, "events")
    await _enforce_ingest_rate_limit(limiter, tenant_id, response, limit)
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in ("", "application/json"):
        raise _problem(415, "Unsupported Media Type", "Use application/json")
    raw = await _read_bounded_body(request)
    try:
        body = loads_bounded(raw)
    except BodyTooDeep:
        raise too_deep_problem() from None
    except ValueError:
        raise _problem(400, "Bad Request", "Body is not valid JSON") from None
    payload = parse_payload(body)
    result = await store_payload(request.state.db, tenant_id, payload)
    if result.mode == "full":
        response.headers[LOGGING_HEADER] = "full"
    return result.as_dict()


# ──────────────────────────────────────────────────────────────────────
# Tenant admin: setting and legal hold
# ──────────────────────────────────────────────────────────────────────

admin_router = APIRouter(
    prefix="/api/v1/admin", tags=["Admin"], route_class=BoundedJsonRoute
)

_PRIVACY = Depends(require_admin(SCOPE))


class PayloadLoggingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["off", "metadata", "redacted", "full"]
    sample_rate: float = Field(default=1.0, ge=0, le=1)
    on_error_only: bool = False
    routes: list[Annotated[str, Field(pattern=r"^[\x21-\x7e]{1,256}$")]] = Field(
        default_factory=list, max_length=MAX_ROUTES
    )
    retention_days: int | None = Field(
        default=None, ge=MIN_RETENTION_DAYS, le=MAX_RETENTION_DAYS
    )


class LegalHoldBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


async def _tenant_for_update(db: AsyncSession, tenant_id: UUID) -> Tenant:
    tenant = (
        await db.execute(
            select(Tenant)
            .where(Tenant.id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if tenant is None:
        raise _problem(404, "Not Found", "Tenant not found")
    return tenant


@admin_router.get("/payload-logging", summary="Get the tenant's payload logging setting")
async def get_payload_logging(request: Request, principal: AdminPrincipal = _PRIVACY) -> dict:
    return config_from_settings(
        await _tenant_settings(request.state.db, principal.tenant_id)
    ).as_dict()


@admin_router.put("/payload-logging", summary="Set the tenant's payload logging setting")
async def put_payload_logging(
    body: PayloadLoggingBody, request: Request, principal: AdminPrincipal = _PRIVACY
) -> dict:
    if body.mode == "full" and not can_manage_owner(principal):
        raise _problem(
            403,
            "Forbidden",
            "Only a tenant owner may turn on full payload logging (an admin token "
            "issued for an owner, or one holding every admin scope)",
        )
    if body.mode in CONTENT_MODES and not keyring_ready():
        raise _problem(
            409,
            "Conflict",
            "Payloads are stored encrypted: set PS_WEBHOOK_SECRET_KEYS on the "
            "collector before turning on redacted or full logging",
        )
    db = request.state.db
    tenant = await _tenant_for_update(db, principal.tenant_id)
    previous = config_from_settings(tenant.settings or {})
    stored = {
        "mode": body.mode,
        "sample_rate": body.sample_rate,
        "on_error_only": body.on_error_only,
        "routes": list(body.routes),
    }
    if body.retention_days is not None:
        stored["retention_days"] = body.retention_days
    new_settings = dict(tenant.settings or {})
    new_settings[SETTINGS_KEY] = stored
    tenant.settings = new_settings  # reassign so the JSONB change is flushed
    config = config_from_settings(new_settings)
    await audit_trail.record(
        db,
        tenant_id=principal.tenant_id,
        action=SET_ACTION,
        actor=admin_actor(request, principal),
        target_type="tenant",
        target_id=principal.tenant_id,
        status_code=200,
        details={
            "mode": config.mode,
            "previous_mode": previous.mode,
            "sample_rate": config.sample_rate,
            "on_error_only": config.on_error_only,
            "routes": len(config.routes),
            "retention_days": config.as_dict()["retention_days"],
        },
    )
    await db.commit()
    return config.as_dict()


def _legal_hold_dict(tenant_settings: Any) -> dict:
    since = legal_hold_since(tenant_settings)
    return {"legal_hold": since is not None, "since": since}


@admin_router.get("/legal-hold", summary="Get the tenant's legal hold")
async def get_legal_hold(request: Request, principal: AdminPrincipal = _PRIVACY) -> dict:
    return _legal_hold_dict(await _tenant_settings(request.state.db, principal.tenant_id))


@admin_router.put("/legal-hold", summary="Place or lift the tenant's legal hold")
async def put_legal_hold(
    body: LegalHoldBody, request: Request, principal: AdminPrincipal = _PRIVACY
) -> dict:
    db = request.state.db
    tenant = await _tenant_for_update(db, principal.tenant_id)
    new_settings = dict(tenant.settings or {})
    was_held = on_legal_hold(new_settings)
    if body.enabled and not was_held:
        new_settings[LEGAL_HOLD_KEY] = {
            "since": datetime.now(UTC).isoformat().replace("+00:00", "Z")
        }
    elif not body.enabled:
        new_settings.pop(LEGAL_HOLD_KEY, None)
    tenant.settings = new_settings
    await audit_trail.record(
        db,
        tenant_id=principal.tenant_id,
        action=LEGAL_HOLD_ACTION,
        actor=admin_actor(request, principal),
        target_type="tenant",
        target_id=principal.tenant_id,
        status_code=200,
        details={"legal_hold": body.enabled, "was_held": was_held},
    )
    await db.commit()
    return _legal_hold_dict(new_settings)


# ──────────────────────────────────────────────────────────────────────
# Partner read
# ──────────────────────────────────────────────────────────────────────

partner_router = APIRouter(prefix="/partner/v1", tags=["Partner API"])

_READ = Depends(require_scope(READ_SCOPE))


def _ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def call_payload_dict(row: CallPayload, content: dict | None) -> dict:
    return {
        "call_id": row.call_id,
        "mode": row.mode,
        "received_at": _ts(row.received_at),
        "route": row.route,
        "status_code": row.status_code,
        "error": bool(row.is_error),
        "request_bytes": row.request_bytes or 0,
        "response_bytes": row.response_bytes or 0,
        "redactions": dict(row.redactions or {}),
        "request": content.get("request") if content else None,
        "response": content.get("response") if content else None,
    }


async def _audit_read(
    request: Request, principal: PartnerPrincipal, call_id: str, status: int, mode: str | None
) -> None:
    """The ``payload.read`` audit row, committed. Raises 503 when it cannot
    be written: no payload leaves without its audit row."""
    db = request.state.db
    try:
        await audit_trail.record(
            db,
            tenant_id=principal.tenant_id,
            action=READ_ACTION,
            actor=audit_trail.partner_actor(principal.partner_id),
            outcome=audit_trail.SUCCESS if status == 200 else audit_trail.FAILURE,
            target_type="call",
            target_id=call_id,
            status_code=status,
            details={"mode": mode} if mode else {},
        )
        await db.commit()
    except Exception:
        logger.exception("payload read audit write failed tenant=%s", principal.tenant_id)
        try:
            await db.rollback()
        except Exception:
            logger.debug("rollback after the read audit failure also failed", exc_info=True)
        raise _problem(
            503, "Service Unavailable", "The read could not be audited; try again"
        ) from None


@partner_router.get("/calls/{call_id}", summary="Get one call's logged payload")
async def get_call_payload(
    call_id: str,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = _READ,
) -> dict:
    await _enforce_rate_limit(request, response, principal)
    if not CALL_ID_RE.fullmatch(call_id):
        raise _problem(400, "Bad Request", "Invalid call_id")
    db = request.state.db
    tenant_settings = await _tenant_settings(db, principal.tenant_id)
    row = (
        await db.execute(
            select(CallPayload).where(
                CallPayload.tenant_id == principal.tenant_id, CallPayload.call_id == call_id
            )
        )
    ).scalar_one_or_none()
    # Past retention and not yet purged: gone as far as readers go.
    if (
        row is not None
        and not on_legal_hold(tenant_settings)
        and row.received_at < payload_cutoff(tenant_settings)
    ):
        row = None
    if row is None:
        await _audit_read(request, principal, call_id, 404, None)
        raise _problem(404, "Not Found", "No payload is stored for this call")
    content = None
    if row.ciphertext:
        try:
            content = unseal_payload(principal.tenant_id, row.call_id, row.ciphertext)
        except (ValueError, secret_keyring.KeyringNotConfigured):
            logger.exception("payload could not be decrypted tenant=%s", principal.tenant_id)
            await _audit_read(request, principal, call_id, 500, row.mode)
            raise _problem(
                500, "Internal Server Error", "The payload could not be decrypted"
            ) from None
    body = call_payload_dict(row, content)
    await _audit_read(request, principal, call_id, 200, row.mode)
    return body
