"""Operator credentials: named operator tokens and the break-glass token (#341).

Operator routes (/api/v1/operator and the operator-only admin routes:
provider usage pull, cloud inventory, LeanIX, ServiceNow, Sentinel) act
across tenants. Two credentials reach them:

  - a named operator token (``ps-ot-...``, table ``operator_tokens``):
    a name, explicit scopes (``OPERATOR_SCOPES``), an optional expiry,
    rotation and revocation. Issued through ``/api/v1/operator/tokens``.
    Every action it takes is attributed to it in the audit trail
    (actor ``operator_token``, id = its name; names are never reused).
  - ``PS_ADMIN_TOKEN``: the static break-glass token. It holds every scope.
    Every use is an ``operator.break_glass`` row on the system chain
    (route and method), so its use stands out next to named tokens.

Each route declares one scope with ``Depends(require_operator_scope(...))``;
GET routes also accept ``operator:read``. With ``PS_ADMIN_TOKEN`` unset and
no ``ps-ot-`` token presented, operator routes answer 404 as before, so a
collector that never set up an operator token shows no operator surface.
Once named tokens exist, ``PS_ADMIN_TOKEN`` can be unset: they keep working.

A wrong, revoked or expired operator token is a 401 and an ``auth.failure``
(realm ``operator``) on the system chain; a token without the route's scope
is a 403 and an ``auth.failure`` with reason ``insufficient_scope``.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from db.api_keys import api_key_lookup_prefix, hash_api_key, verify_api_key
from db.models import OperatorToken
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from collector import audit_trail
from collector.auth import (
    _DUMMY_HASH,
    check_failed_auth_limit,
    failed_auth_client,
    record_failed_auth,
)
from collector.tenant_keys import key_status

TOKEN_PREFIX = "ps-ot-"
TOKEN_WARNING = "Save the token now. It will not be shown again."
ACTOR_TYPE = audit_trail.OPERATOR_TOKEN
BREAK_GLASS_ACTION = "operator.break_glass"
ACCESS_ACTION = "operator.access"

# Scope -> what it grants. Order is the order shown by GET /token-scopes.
OPERATOR_SCOPES: dict[str, str] = {
    "operator:read": "Read (GET) every operator resource; changes nothing",
    "operator:tenants": (
        "Create, change, disable and enable tenants, and bootstrap tenant admin tokens"
    ),
    "operator:retention": "Set tenant retention and run the usage-event purge",
    "operator:erase": "Erase a tenant and all its data",
    "operator:audit": "Verify the audit trail hash chains",
    "operator:integrations": (
        "Run provider usage pulls, cloud inventory, LeanIX, ServiceNow and "
        "Sentinel operator routes"
    ),
    "operator:tokens": (
        "Issue, rotate and revoke operator tokens (only with scopes the caller holds)"
    ),
}
READ_SCOPE = "operator:read"
_READ_METHODS = frozenset({"GET", "HEAD"})


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


@dataclass(frozen=True)
class OperatorPrincipal:
    """Who called an operator route."""

    # "operator_token" or "break_glass"
    kind: str
    scopes: frozenset[str]
    token_id: object | None = None
    name: str | None = None
    # A named token's own expiry (None: it does not expire). Tokens it
    # issues or rotates expire no later than this (``cap_expiry``).
    expires_at: datetime | None = None

    @property
    def break_glass(self) -> bool:
        return self.kind == "break_glass"

    @property
    def actor(self) -> audit_trail.Actor:
        """The audit trail actor: the token by name, or the operator."""
        if self.break_glass:
            return audit_trail.OPERATOR
        return audit_trail.Actor(ACTOR_TYPE, self.name)

    @property
    def label(self) -> str:
        """The actor as one string (``created_by``, retention ``actor=``):
        ``operator-api`` or ``operator_token:<name>``."""
        if self.break_glass:
            return audit_trail.OPERATOR.id
        return f"{ACTOR_TYPE}:{self.name}"

    def cap_expiry(self, expires_at: datetime | None) -> datetime | None:
        """``expires_at`` for a token this caller issues or rotates: no later
        than the caller's own expiry, so a short-lived token cannot mint or
        renew a longer-lived (or never-expiring) one. An omitted or later
        expiry is capped to the caller's, as for tenant admin tokens
        (``admin_auth.cap_child_expiry``). Break-glass and named tokens
        without an expiry are not capped."""
        if self.break_glass or self.expires_at is None:
            return expires_at
        if expires_at is None or expires_at > self.expires_at:
            return self.expires_at
        return expires_at

    def lacks(self, scopes) -> list[str]:
        """The scopes in ``scopes`` this caller does not hold, sorted."""
        return sorted(set(scopes or ()) - self.scopes)

    def allows(self, scope: str, method: str) -> bool:
        if scope in self.scopes:
            return True
        return method in _READ_METHODS and READ_SCOPE in self.scopes


BREAK_GLASS = OperatorPrincipal(kind="break_glass", scopes=frozenset(OPERATOR_SCOPES))


def unknown_scopes(requested) -> list[str]:
    return sorted(set(requested) - set(OPERATOR_SCOPES))


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def _presented(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    return auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""


async def _fail(request: Request, reason: str, status: int) -> None:
    await audit_trail.record_auth_failure(
        request, realm="operator", reason=reason, status_code=status
    )


async def _resolve_named(request: Request, token: str) -> OperatorPrincipal:
    db = request.state.db
    row = (
        await db.execute(
            select(
                OperatorToken.id,
                OperatorToken.name,
                OperatorToken.scopes,
                OperatorToken.token_hash,
                OperatorToken.expires_at,
                OperatorToken.revoked_at,
            ).where(OperatorToken.token_prefix == api_key_lookup_prefix(token))
        )
    ).one_or_none()
    # As for admin tokens: 429 without bcrypt for a client that failed too
    # often, unless the token is found and active.
    if row is None or key_status(row) != "active":
        client = check_failed_auth_limit(request)
    else:
        client = failed_auth_client(request)
    stored = row.token_hash if row is not None else _DUMMY_HASH
    valid = await asyncio.to_thread(verify_api_key, token, stored)
    if row is None or not valid:
        record_failed_auth(client)
        await _fail(request, "invalid_credentials", 401)
        raise _problem(401, "Unauthorized", "Invalid operator token")
    if key_status(row) != "active":
        record_failed_auth(client)
        await _fail(request, "invalid_credentials", 401)
        raise _problem(401, "Unauthorized", "Operator token revoked or expired")
    return OperatorPrincipal(
        kind=ACTOR_TYPE,
        scopes=frozenset(row.scopes or ()),
        token_id=row.id,
        name=row.name,
        expires_at=row.expires_at,
    )


async def resolve_operator(request: Request, break_glass_token: str) -> OperatorPrincipal:
    """Authenticate an operator route caller (named token or break-glass).

    Sets ``request.state.operator`` to the principal, so audit rows written
    later in the request name it (``current_actor``)."""
    presented = _presented(request)
    if presented.startswith(TOKEN_PREFIX):
        principal = await _resolve_named(request, presented)
    else:
        if not break_glass_token:
            raise _problem(404, "Not Found", "Not Found")
        if not presented or not hmac.compare_digest(
            presented.encode(), break_glass_token.encode()
        ):
            await _fail(
                request,
                "invalid_credentials" if presented else "missing_credentials",
                401,
            )
            raise _problem(401, "Unauthorized", "Invalid operator token")
        principal = BREAK_GLASS
    request.state.operator = principal
    return principal


async def record_use(request: Request, principal: OperatorPrincipal) -> None:
    """System chain row for an authorised operator call: ``operator.break_glass``
    for PS_ADMIN_TOKEN, ``operator.access`` for a named token."""
    await audit_trail.record_system_event(
        action=BREAK_GLASS_ACTION if principal.break_glass else ACCESS_ACTION,
        actor=principal.actor,
        details={
            "route": audit_trail.route_template(request),
            "method": request.method,
        },
    )


def require_operator_scope(
    scope: str, break_glass_token: Callable[[], str]
) -> Callable:
    """Dependency factory for an operator route that needs ``scope`` (GET
    routes also take ``operator:read``). ``break_glass_token`` returns the
    configured PS_ADMIN_TOKEN ("" when unset). A typo fails at import time."""
    if scope not in OPERATOR_SCOPES or scope == READ_SCOPE:
        raise ValueError(f"Unknown operator scope: {scope!r}")

    async def _dep(request: Request) -> OperatorPrincipal:
        principal = await resolve_operator(request, break_glass_token())
        if not principal.allows(scope, request.method):
            await _fail(request, "insufficient_scope", 403)
            raise _problem(403, "Forbidden", f"Token lacks required scope: {scope}")
        await record_use(request, principal)
        return principal

    # Read by the spec contract test (tests/test_openapi_spec.py).
    _dep.operator_scope = scope
    return _dep


def current_actor(request: Request) -> audit_trail.Actor:
    """The operator actor of this request (``OPERATOR`` when unset)."""
    principal = getattr(getattr(request, "state", None), "operator", None)
    return principal.actor if principal is not None else audit_trail.OPERATOR


def current_label(request: Request) -> str:
    principal = getattr(getattr(request, "state", None), "operator", None)
    return principal.label if principal is not None else audit_trail.OPERATOR.id


# ──────────────────────────────────────────────────────────────────────
# Token records
# ──────────────────────────────────────────────────────────────────────


def serialize_token(row: OperatorToken) -> dict:
    """Metadata only: never the token or its hash."""

    def iso(v):
        return v.isoformat() if v else None

    held = set(row.scopes or ())
    return {
        "id": str(row.id),
        "name": row.name,
        "scopes": [s for s in OPERATOR_SCOPES if s in held],
        "token_hint": row.token_hint,
        "status": key_status(row),
        "created_by": row.created_by,
        "created_at": iso(row.created_at),
        "rotated_at": iso(row.rotated_at),
        "expires_at": iso(row.expires_at),
        "revoked_at": iso(row.revoked_at),
    }


class NameTaken(Exception):
    """An operator token (live or revoked) already has this name."""


async def issue_token(
    db: AsyncSession,
    *,
    name: str,
    scopes: list[str],
    expires_at: datetime | None,
    created_by: str,
) -> tuple[OperatorToken, str]:
    """Create a token. Returns (row, plaintext). Flushes; the caller commits."""
    taken = (
        await db.execute(select(OperatorToken.id).where(OperatorToken.name == name))
    ).first()
    if taken is not None:
        raise NameTaken(name)
    plaintext = generate_token()
    held = set(scopes)
    row = OperatorToken(
        name=name,
        scopes=[s for s in OPERATOR_SCOPES if s in held],
        token_prefix=api_key_lookup_prefix(plaintext),
        token_hash=hash_api_key(plaintext),
        token_hint=plaintext[-4:],
        created_by=created_by,
        created_at=datetime.now(UTC),
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    return row, plaintext


def rotate_token(row: OperatorToken) -> str:
    """New secret, same id and name; the old secret stops working at once.
    Raises ValueError for a revoked or expired token."""
    status = key_status(row)
    if status != "active":
        raise ValueError(status)
    plaintext = generate_token()
    row.token_prefix = api_key_lookup_prefix(plaintext)
    row.token_hash = hash_api_key(plaintext)
    row.token_hint = plaintext[-4:]
    row.rotated_at = datetime.now(UTC)
    return plaintext
