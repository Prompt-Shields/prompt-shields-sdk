"""Tenant admin API for automation credentials: admin tokens and tenant
ingest keys. Mounted at /api/v1/admin. Auth and scopes: collector/admin_auth.py.

  GET    /token-scopes                     the admin scopes
  POST   /tokens                           issue (token returned once)   admin:tokens
  GET    /tokens                           list (metadata)               admin:tokens | admin:read
  GET    /tokens/{token_id}                read                          admin:tokens | admin:read
  DELETE /tokens/{token_id}                revoke                        admin:tokens

  POST   /api-keys                         issue (key returned once)     admin:keys
  GET    /api-keys                         list (metadata)               admin:keys | admin:read
  GET    /api-keys/{key_id}                read                          admin:keys | admin:read
  POST   /api-keys/{key_id}/rotate         new secret, same id; optional
                                           grace window for the old one  admin:keys
  DELETE /api-keys/{key_id}                revoke                        admin:keys

Conventions shared with partner credentials:

  - ids are UUIDs and never change (a key keeps its id across rotations);
  - POST with a name that a live (unrevoked) row already has is 409;
  - DELETE revokes: 204, the row is kept and still readable by id with
    ``status: revoked`` (so audit rows resolve), and a repeated DELETE is
    204 too. Lists include revoked rows unless ``?status=`` filters them.

Every change writes a partner_audit_log row in the same transaction (ids,
names and scopes; never a secret).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from db.models import AdminToken, TenantApiKey
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from collector import tenant_keys
from collector.admin_auth import (
    ADMIN_SCOPES,
    TOKEN_WARNING,
    AdminPrincipal,
    add_audit,
    created_by,
    issue_token,
    require_admin,
    serialize_token,
    unknown_scopes,
)
from collector.sanitize import BoundedJsonRoute
from collector.tenant_keys import KEY_WARNING, DuplicateName, NotRotatable

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"], route_class=BoundedJsonRoute)

_TOKENS = Depends(require_admin("admin:tokens"))
_KEYS = Depends(require_admin("admin:keys"))

Status = Literal["active", "expired", "revoked"]
NAME = Field(
    ..., min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._ -]*$"
)


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


def _future(expires_at: datetime | None) -> datetime | None:
    if expires_at is None:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        raise _problem(400, "Bad Request", "expires_at must be in the future")
    return expires_at


def _filter(rows: list[dict], status: str | None) -> list[dict]:
    return [r for r in rows if status is None or r["status"] == status]


# ──────────────────────────────────────────────────────────────────────
# Admin tokens
# ──────────────────────────────────────────────────────────────────────


class CreateAdminTokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = NAME
    scopes: list[str] = Field(..., min_length=1)
    expires_at: datetime | None = None


@router.get("/token-scopes", summary="List the admin token scopes")
async def list_token_scopes(principal: AdminPrincipal = _TOKENS) -> dict:
    return {
        "data": [{"name": k, "description": v} for k, v in ADMIN_SCOPES.items()],
    }


@router.post("/tokens", status_code=201, summary="Issue a scoped admin token")
async def create_token(
    body: CreateAdminTokenBody, request: Request, principal: AdminPrincipal = _TOKENS
) -> dict:
    bad = unknown_scopes(body.scopes)
    if bad:
        raise _problem(
            400, "Bad Request", f"Unknown scopes: {bad}. Allowed: {list(ADMIN_SCOPES)}"
        )
    beyond = sorted(set(body.scopes) - principal.scopes)
    if beyond:
        raise _problem(
            403, "Forbidden", f"Cannot grant scopes the caller does not hold: {beyond}"
        )
    db = request.state.db
    try:
        row, token = await issue_token(
            db,
            principal.tenant_id,
            name=body.name,
            scopes=body.scopes,
            expires_at=_future(body.expires_at),
            created_by=created_by(request, principal),
        )
    except DuplicateName:
        raise _problem(
            409, "Conflict", "An unrevoked admin token with this name already exists"
        ) from None
    out = serialize_token(row)
    add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=201,
        action="admin_token.create",
        token_id=out["id"],
        name=row.name,
        scopes=out["scopes"],
    )
    await db.commit()
    return {**out, "token": token, "warning": TOKEN_WARNING}


async def _get_token(request: Request, tenant_id: UUID, token_id: UUID) -> AdminToken:
    row = (
        await request.state.db.execute(
            select(AdminToken).where(
                AdminToken.id == token_id, AdminToken.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _problem(404, "Not Found", "Admin token not found")
    return row


@router.get("/tokens", summary="List admin tokens (metadata only)")
async def list_tokens(
    request: Request, status: Status | None = None, principal: AdminPrincipal = _TOKENS
) -> dict:
    rows = (
        (
            await request.state.db.execute(
                select(AdminToken)
                .where(AdminToken.tenant_id == principal.tenant_id)
                .order_by(AdminToken.created_at)
            )
        )
        .scalars()
        .all()
    )
    return {"data": _filter([serialize_token(r) for r in rows], status)}


@router.get("/tokens/{token_id}", summary="Get an admin token (metadata only)")
async def get_token(
    token_id: UUID, request: Request, principal: AdminPrincipal = _TOKENS
) -> dict:
    return serialize_token(await _get_token(request, principal.tenant_id, token_id))


@router.delete("/tokens/{token_id}", status_code=204, summary="Revoke an admin token")
async def revoke_token(
    token_id: UUID, request: Request, principal: AdminPrincipal = _TOKENS
) -> None:
    db = request.state.db
    row = await _get_token(request, principal.tenant_id, token_id)
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        add_audit(
            request,
            db,
            tenant_id=principal.tenant_id,
            status_code=204,
            action="admin_token.revoke",
            token_id=str(row.id),
            name=row.name,
        )
        await db.commit()


# ──────────────────────────────────────────────────────────────────────
# Tenant ingest keys
# ──────────────────────────────────────────────────────────────────────


class CreateApiKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = NAME
    expires_at: datetime | None = None


class RotateApiKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grace_period_seconds: int = Field(
        default=0,
        ge=0,
        le=int(tenant_keys.MAX_ROTATION_GRACE.total_seconds()),
        description=(
            "Keep accepting the old key for this long, so senders can switch "
            "without dropping events. 0 (the default) ends it at once."
        ),
    )


async def _get_key(request: Request, tenant_id: UUID, key_id: UUID) -> TenantApiKey:
    row = (
        await request.state.db.execute(
            select(TenantApiKey).where(
                TenantApiKey.id == key_id, TenantApiKey.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _problem(404, "Not Found", "API key not found")
    return row


async def _adopt(request: Request, tenant_id: UUID) -> None:
    """Bring a legacy tenant-row key into the table (tenant_keys module doc)."""
    db = request.state.db
    row = await tenant_keys.adopt_legacy_key(db, tenant_id)
    if row is not None:
        add_audit(
            request,
            db,
            tenant_id=tenant_id,
            status_code=200,
            action="api_key.adopt_legacy",
            key_id=str(row.id),
            name=row.name,
        )
        await db.commit()


@router.post("/api-keys", status_code=201, summary="Issue a tenant ingest key")
async def create_api_key(
    body: CreateApiKeyBody, request: Request, principal: AdminPrincipal = _KEYS
) -> dict:
    db = request.state.db
    await _adopt(request, principal.tenant_id)
    try:
        row, key = await tenant_keys.issue_key(
            db, principal.tenant_id, body.name, expires_at=_future(body.expires_at)
        )
    except DuplicateName:
        raise _problem(
            409, "Conflict", "An unrevoked API key with this name already exists"
        ) from None
    out = tenant_keys.serialize_key(row)
    add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=201,
        action="api_key.create",
        key_id=out["id"],
        name=row.name,
    )
    await db.commit()
    return {**out, "key": key, "warning": KEY_WARNING}


@router.get("/api-keys", summary="List tenant ingest keys (metadata only)")
async def list_api_keys(
    request: Request, status: Status | None = None, principal: AdminPrincipal = _KEYS
) -> dict:
    await _adopt(request, principal.tenant_id)
    rows = (
        (
            await request.state.db.execute(
                select(TenantApiKey)
                .where(TenantApiKey.tenant_id == principal.tenant_id)
                .order_by(TenantApiKey.created_at)
            )
        )
        .scalars()
        .all()
    )
    return {"data": _filter([tenant_keys.serialize_key(r) for r in rows], status)}


@router.get("/api-keys/{key_id}", summary="Get a tenant ingest key (metadata only)")
async def get_api_key(
    key_id: UUID, request: Request, principal: AdminPrincipal = _KEYS
) -> dict:
    return tenant_keys.serialize_key(
        await _get_key(request, principal.tenant_id, key_id)
    )


@router.post(
    "/api-keys/{key_id}/rotate",
    summary="Replace a key's secret (same id), optionally keeping the old one for a grace window",
)
async def rotate_api_key(
    key_id: UUID,
    request: Request,
    body: RotateApiKeyBody | None = None,
    principal: AdminPrincipal = _KEYS,
) -> dict:
    db = request.state.db
    row = await _get_key(request, principal.tenant_id, key_id)
    grace = body.grace_period_seconds if body else 0
    try:
        key = tenant_keys.rotate_key(row, timedelta(seconds=grace))
    except NotRotatable as exc:
        raise _problem(409, "Conflict", f"Cannot rotate a {exc} key") from None
    add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=200,
        action="api_key.rotate",
        key_id=str(row.id),
        name=row.name,
        grace_period_seconds=grace,
    )
    await db.commit()
    await db.refresh(row)
    return {**tenant_keys.serialize_key(row), "key": key, "warning": KEY_WARNING}


@router.delete(
    "/api-keys/{key_id}", status_code=204, summary="Revoke a tenant ingest key"
)
async def revoke_api_key(
    key_id: UUID, request: Request, principal: AdminPrincipal = _KEYS
) -> None:
    db = request.state.db
    row = await _get_key(request, principal.tenant_id, key_id)
    if tenant_keys.revoke_key(row):
        add_audit(
            request,
            db,
            tenant_id=principal.tenant_id,
            status_code=204,
            action="api_key.revoke",
            key_id=str(row.id),
            name=row.name,
        )
        await db.commit()
