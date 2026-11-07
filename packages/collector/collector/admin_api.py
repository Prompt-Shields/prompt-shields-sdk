"""Tenant admin API for automation credentials: admin tokens and tenant
ingest keys. Mounted at /api/v1/admin. Auth and scopes: collector/admin_auth.py.

  GET    /token-scopes                     the admin scopes
  POST   /tokens                           issue (token returned once)   admin:tokens
  GET    /tokens                           list (metadata)               admin:tokens | admin:read
  GET    /tokens/{token_id}                read                          admin:tokens | admin:read
  DELETE /tokens/{token_id}                revoke, with every token it
                                           created (cascade)             admin:tokens **

  ** Revoking another token needs every scope it holds, and an owner
     (admin_auth.can_manage_owner) when it is owner-bound (issued for a
     tenant-wide owner) or owner-capable (every admin scope, unbound): an
     ``admin:tokens`` holder must not be able to cut the owner off (#372).
     A token may always revoke itself. Refusals are 403 and audited as
     ``admin_token.revoke`` with outcome ``failure``.

  A token issued by another token expires no later than that token (an
  expiry past it, or none, is capped to it).

  POST   /api-keys                         issue (key returned once)     admin:keys *
  GET    /api-keys                         list (metadata)               admin:keys | admin:read
  GET    /api-keys/{key_id}                read                          admin:keys | admin:read
  POST   /api-keys/{key_id}/rotate         new secret, same id; optional
                                           grace window for the old one  admin:keys *
  DELETE /api-keys/{key_id}                revoke                        admin:keys

  * While PS_ADMIN_REQUIRE_SCOPED_TOKENS is off, a full-scope ingest key is
    full admin, so issuing or rotating one needs every admin scope
    (require_full_admin). A key with fewer scopes (the default for a new
    key is ``ingest:write`` only, #341) needs just ``admin:keys``.

Conventions shared with partner credentials:

  - ids are UUIDs and never change (a key keeps its id across rotations);
  - POST with a name that a live (unrevoked) row already has is 409;
  - DELETE revokes: 204, the row is kept and still readable by id with
    ``status: revoked`` (so audit rows resolve), and a repeated DELETE is
    204 too. Lists include revoked rows unless ``?status=`` filters them.

Every change writes an audit trail row (collector.audit_trail,
``audit_events``) in the same transaction: ids, names and scopes, never a
secret.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from db.models import AdminToken, Membership, TenantApiKey
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from collector import audit_trail, auth_cache, rbac, tenant_keys
from collector.admin_auth import (
    ADMIN_SCOPES,
    TOKEN_WARNING,
    AdminPrincipal,
    add_audit,
    admin_actor,
    can_manage_owner,
    cap_child_expiry,
    created_by,
    issue_token,
    require_admin,
    require_full_admin,
    revoke_descendants,
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
    # Issue it for a principal (#338): it gets the requested scopes its
    # tenant-wide roles grant, and on every request no more than they grant
    # then. Omitted: an automation token that belongs to no one.
    principal_id: UUID | None = None


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
    scopes = body.scopes
    roles: frozenset[str] = frozenset()
    if body.principal_id is not None:
        try:
            roles = await rbac.tenant_roles(db, principal.tenant_id, body.principal_id)
            granted = rbac.role_scopes(roles)
        except rbac.PrincipalDisabled:
            raise _problem(
                400, "Bad Request", "principal_id: no such principal, or it is disabled"
            ) from None
        scopes = rbac.ordered(set(body.scopes) & granted, body.scopes)
        if not scopes:
            raise _problem(
                400,
                "Bad Request",
                "None of the requested scopes is granted by the principal's "
                "tenant-wide roles",
            )
    # A token that could manage owners (every admin scope, and unbound or
    # issued for an owner; admin_auth.can_manage_owner) only from a caller
    # that can: otherwise an admin could mint one and make itself owner.
    if set(ADMIN_SCOPES) <= set(scopes) and (
        body.principal_id is None or rbac.OWNER in roles
    ) and not can_manage_owner(principal):
        raise _problem(
            403,
            "Forbidden",
            "Only an owner can issue a token holding every admin scope that "
            "is unbound or issued for an owner",
        )
    try:
        row, token = await issue_token(
            db,
            principal.tenant_id,
            name=body.name,
            scopes=scopes,
            expires_at=await cap_child_expiry(db, principal, _future(body.expires_at)),
            created_by=created_by(request, principal),
            principal_id=body.principal_id,
        )
    except DuplicateName:
        raise _problem(
            409, "Conflict", "An unrevoked admin token with this name already exists"
        ) from None
    out = serialize_token(row)
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=201,
        action="admin_token.create",
        principal=principal,
        target_type="admin_token",
        target_id=out["id"],
        name=row.name,
        scopes=out["scopes"],
        principal_id=out["principal_id"],
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


async def _bound_to_owner(db, tenant_id: UUID, principal_id: UUID | None) -> bool:
    """Whether ``principal_id`` holds the tenant-wide owner role (disabled
    or not: a re-enabled owner's tokens work again)."""
    if principal_id is None:
        return False
    return (
        await db.execute(
            select(Membership.id).where(
                Membership.tenant_id == tenant_id,
                Membership.principal_id == principal_id,
                Membership.team_id.is_(None),
                Membership.role == rbac.OWNER,
            )
        )
    ).first() is not None


async def _check_can_revoke(request: Request, principal: AdminPrincipal, row: AdminToken) -> None:
    """403 (audited) unless the caller may revoke ``row``: its own token
    always; another token only when the caller holds every scope it holds
    and, when it is owner-bound or owner-capable, can manage owners (#372).
    Otherwise an ``admin:tokens`` holder could revoke the owner's tokens."""
    if principal.token_id is not None and principal.token_id == row.id:
        return
    db = request.state.db
    reason, detail = None, None
    beyond = sorted(set(row.scopes or ()) - principal.scopes)
    if beyond:
        reason = "scope_not_held"
        detail = f"Cannot revoke a token holding scopes the caller does not hold: {beyond}"
    elif not can_manage_owner(principal):
        if await _bound_to_owner(db, principal.tenant_id, row.principal_id):
            reason = "owner_required"
            detail = "Only an owner can revoke a token issued for an owner"
        elif row.principal_id is None and set(ADMIN_SCOPES) <= set(row.scopes or ()):
            reason = "owner_required"
            detail = (
                "Only an owner can revoke an unbound token holding every admin "
                "scope (it can manage owners)"
            )
    if reason is None:
        return
    # Nothing has changed in the request's session yet: the refusal row
    # commits on its own.
    await audit_trail.record_committed(
        db,
        tenant_id=principal.tenant_id,
        action="admin_token.revoke",
        outcome=audit_trail.FAILURE,
        actor=admin_actor(request, principal),
        target_type="admin_token",
        target_id=str(row.id),
        status_code=403,
        details={"name": row.name, "reason": reason},
    )
    raise _problem(403, "Forbidden", detail)


@router.delete("/tokens/{token_id}", status_code=204, summary="Revoke an admin token")
async def revoke_token(
    token_id: UUID, request: Request, principal: AdminPrincipal = _TOKENS
) -> None:
    db = request.state.db
    row = await _get_token(request, principal.tenant_id, token_id)
    await _check_can_revoke(request, principal, row)
    now = datetime.now(UTC)
    changed = False
    if row.revoked_at is None:
        row.revoked_at = now
        await add_audit(
            request,
            db,
            tenant_id=principal.tenant_id,
            status_code=204,
            action="admin_token.revoke",
            principal=principal,
            target_type="admin_token",
            target_id=str(row.id),
            name=row.name,
        )
        changed = True
    # The tokens it created go with it (also on a repeated DELETE, for
    # children made before revocation cascaded).
    for child in await revoke_descendants(db, row, now):
        await add_audit(
            request,
            db,
            tenant_id=principal.tenant_id,
            status_code=204,
            action="admin_token.revoke",
            principal=principal,
            target_type="admin_token",
            target_id=str(child.id),
            name=child.name,
            cascade_from=str(row.id),
        )
        changed = True
    if changed:
        await db.commit()


# ──────────────────────────────────────────────────────────────────────
# Tenant ingest keys
# ──────────────────────────────────────────────────────────────────────


class CreateApiKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = NAME
    expires_at: datetime | None = None
    # tenant_keys.KEY_SCOPES; ingest:write only unless more are asked for.
    scopes: list[str] = Field(
        default_factory=lambda: list(tenant_keys.DEFAULT_KEY_SCOPES), min_length=1
    )


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


async def _adopt(request: Request, principal: AdminPrincipal) -> None:
    """Bring a legacy tenant-row key into the table (tenant_keys module doc)."""
    db = request.state.db
    tenant_id = principal.tenant_id
    row = await tenant_keys.adopt_legacy_key(db, tenant_id)
    if row is not None:
        await add_audit(
            request,
            db,
            tenant_id=tenant_id,
            status_code=200,
            action="api_key.adopt_legacy",
            principal=principal,
            target_type="tenant_api_key",
            target_id=str(row.id),
            name=row.name,
        )
        await db.commit()


@router.post("/api-keys", status_code=201, summary="Issue a tenant ingest key")
async def create_api_key(
    body: CreateApiKeyBody, request: Request, principal: AdminPrincipal = _KEYS
) -> dict:
    bad = tenant_keys.unknown_key_scopes(body.scopes)
    if bad:
        raise _problem(
            400,
            "Bad Request",
            f"Unknown scopes: {bad}. Allowed: {list(tenant_keys.KEY_SCOPES)}",
        )
    await require_full_admin(request, principal, body.scopes)
    db = request.state.db
    await _adopt(request, principal)
    try:
        row, key = await tenant_keys.issue_key(
            db,
            principal.tenant_id,
            body.name,
            expires_at=_future(body.expires_at),
            scopes=body.scopes,
        )
    except DuplicateName:
        raise _problem(
            409, "Conflict", "An unrevoked API key with this name already exists"
        ) from None
    out = tenant_keys.serialize_key(row)
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=201,
        action="api_key.create",
        principal=principal,
        target_type="tenant_api_key",
        target_id=out["id"],
        name=row.name,
        scopes=out["scopes"],
    )
    await db.commit()
    return {**out, "key": key, "warning": KEY_WARNING}


@router.get("/api-keys", summary="List tenant ingest keys (metadata only)")
async def list_api_keys(
    request: Request, status: Status | None = None, principal: AdminPrincipal = _KEYS
) -> dict:
    # Read only: a key still on the tenant row is moved by migration 022 or
    # by the first key write (_adopt), never by a GET.
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
    await require_full_admin(request, principal, row.scopes or ())
    grace = body.grace_period_seconds if body else 0
    try:
        key = tenant_keys.rotate_key(row, timedelta(seconds=grace))
    except NotRotatable as exc:
        raise _problem(409, "Conflict", f"Cannot rotate a {exc} key") from None
    # The hash changed, so cached verifications no longer match; drop them
    # now rather than at their TTL.
    auth_cache.get_key_cache().invalidate_key(row.id)
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=200,
        action="api_key.rotate",
        principal=principal,
        target_type="tenant_api_key",
        target_id=str(row.id),
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
        auth_cache.get_key_cache().invalidate_key(row.id)
        await add_audit(
            request,
            db,
            tenant_id=principal.tenant_id,
            status_code=204,
            action="api_key.revoke",
            principal=principal,
            target_type="tenant_api_key",
            target_id=str(row.id),
            name=row.name,
        )
        await db.commit()
