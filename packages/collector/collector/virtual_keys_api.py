"""HTTP routes for gateway virtual keys (#343). Logic: collector.virtual_keys.

Tenant admin, under /api/v1/admin, scope ``admin:virtual-keys`` (GET also
with ``admin:read``). Admin tokens only: the scope is not among those a
tenant ingest key reaches while PS_ADMIN_REQUIRE_SCOPED_TOKENS is off.
Through RBAC (#338) the owner and admin roles hold it.

  GET    /virtual-keys                list
  POST   /virtual-keys                issue {name, credential_ids, team_id?,
                                      project_id?, principal_id?,
                                      allowed_models?, strategy?, expires_at?};
                                      the key is in this response only
  GET    /virtual-keys/{id}           read
  PATCH  /virtual-keys/{id}           {name?, credential_ids?, allowed_models?,
                                      strategy?, expires_at?} (the binding is
                                      fixed: issue a new key to change it)
  POST   /virtual-keys/{id}/revoke    revoke (gateways stop accepting it
                                      within their 30 s cache)
  DELETE /virtual-keys/{id}           delete

Every change, and every refused change, writes an ``audit_events`` row
(virtual_key.create | update | revoke | delete) with ids and field names,
never the key.

Gateway, under /api/v1 (tenant ingest key holding ``virtual-keys:resolve``):

  POST   /virtual-keys/resolve        {key} -> {active, ...}: the key's
                                      binding and its pool's credentials,
                                      decrypted (Cache-Control: no-store)

A 422 never echoes the submitted key.
"""

from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from collector import audit_trail, tenant_keys
from collector import virtual_keys as vk
from collector.admin_auth import AdminPrincipal, created_by, require_admin
from collector.audit import record_read
from collector.auth import resolve_tenant
from collector.provider_credentials_api import _SAFE_LOC, _RedactedValidationRoute
from collector.sanitize import BoundedJsonRoute

SCOPE = "admin:virtual-keys"
_ADMIN = Depends(require_admin(SCOPE))

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"], route_class=BoundedJsonRoute)


class _ResolveRoute(_RedactedValidationRoute):
    safe_loc = _SAFE_LOC


gateway_router = APIRouter(prefix="/api/v1", tags=["Virtual keys"], route_class=_ResolveRoute)

class CreateVirtualKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=200)
    credential_ids: list[UUID] = Field(max_length=vk.MAX_POOL_SIZE * 5)
    team_id: UUID | None = None
    project_id: UUID | None = None
    principal_id: UUID | None = None
    allowed_models: list[str] | None = Field(default=None, max_length=vk.MAX_ALLOWED_MODELS * 5)
    strategy: str = vk.ROUND_ROBIN
    expires_at: datetime | None = None


class UpdateVirtualKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    credential_ids: list[UUID] | None = Field(default=None, max_length=vk.MAX_POOL_SIZE * 5)
    allowed_models: list[str] | None = Field(default=None, max_length=vk.MAX_ALLOWED_MODELS * 5)
    strategy: str | None = None
    expires_at: datetime | None = None


class ResolveVirtualKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    key: str = Field(max_length=512)

    def __repr__(self) -> str:  # never print the key
        return "ResolveVirtualKeyBody(key=***)"

    __str__ = __repr__


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


def _bad(exc: ValueError) -> HTTPException:
    return _problem(400, "Bad Request", str(exc))


def _not_found() -> HTTPException:
    return _problem(404, "Not Found", "No virtual key with this id")


async def _one(db, tenant_id: UUID, key_id: UUID):
    row = await vk.find(db, tenant_id, key_id)
    if row is None:
        raise _not_found()
    return row


async def _view(db, tenant_id: UUID, row) -> dict:
    return vk.serialize(row, await vk.pool(db, tenant_id, row.id))


async def _audited(
    request: Request,
    principal: AdminPrincipal,
    action: str,
    target_id: UUID | None,
    details: dict,
    work,
):
    """Run ``work()`` -> (status, body, target id); audit and commit, or on
    an HTTPException roll back and audit the refusal."""
    db = request.state.db
    tenant_id = principal.tenant_id
    actor = audit_trail.tenant_key_actor(request)
    try:
        status, body, target_id = await work()
        await audit_trail.record(
            db,
            tenant_id=tenant_id,
            action=action,
            actor=actor,
            target_type=vk.TARGET_TYPE,
            target_id=target_id,
            status_code=status,
            details=details,
        )
        await db.commit()
    except HTTPException as exc:
        await db.rollback()
        await audit_trail.record_committed(
            db,
            tenant_id=tenant_id,
            action=action,
            actor=actor,
            outcome=audit_trail.FAILURE,
            target_type=vk.TARGET_TYPE,
            target_id=target_id,
            status_code=exc.status_code,
            details=details,
        )
        await record_read(request, tenant_id=tenant_id, status_code=exc.status_code)
        raise
    await record_read(request, tenant_id=tenant_id, status_code=status)
    return status, body


_BASE = "/virtual-keys"
_ONE = _BASE + "/{key_id}"


@router.get(_BASE, summary="List virtual keys")
async def list_virtual_keys(request: Request, principal: AdminPrincipal = _ADMIN):
    db = request.state.db
    rows = await vk.list_keys(db, principal.tenant_id)
    by_key = await vk.pools(db, principal.tenant_id, [r.id for r in rows])
    out = {"data": [vk.serialize(r, by_key[r.id]) for r in rows]}
    await record_read(request, tenant_id=principal.tenant_id, status_code=200)
    return out


@router.post(_BASE, status_code=201, summary="Issue a virtual key (shown once)")
async def create_virtual_key(
    body: CreateVirtualKeyBody,
    request: Request,
    response: Response,
    principal: AdminPrincipal = _ADMIN,
):
    db = request.state.db
    tenant_id = principal.tenant_id

    async def work():
        try:
            name = vk.validate_name(body.name)
            strategy = vk.validate_strategy(body.strategy)
            allowed = vk.validate_allowed_models(body.allowed_models)
            expires_at = vk.validate_expires_at(body.expires_at)
            binding = await vk.check_binding(
                db, tenant_id, body.team_id, body.project_id, body.principal_id
            )
            cred_ids = await vk.check_credentials(db, tenant_id, body.credential_ids)
        except ValueError as exc:
            raise _bad(exc) from exc
        if await vk.name_taken(db, tenant_id, name):
            raise _problem(409, "Conflict", "A virtual key with this name exists")
        if await vk.count(db, tenant_id) >= vk.MAX_VIRTUAL_KEYS_PER_TENANT:
            raise _problem(
                409, "Conflict", f"At most {vk.MAX_VIRTUAL_KEYS_PER_TENANT} virtual keys"
            )
        row, plaintext = await vk.create(
            db,
            tenant_id,
            name=name,
            binding=binding,
            credential_ids=cred_ids,
            allowed_models=allowed,
            strategy=strategy,
            expires_at=expires_at,
            created_by=created_by(request, principal),
        )
        await db.flush()
        out = await _view(db, tenant_id, row)
        out["key"] = plaintext
        out["warning"] = vk.KEY_WARNING
        return 201, out, row.id

    details = {
        "team_id": str(body.team_id) if body.team_id else None,
        "project_id": str(body.project_id) if body.project_id else None,
        "principal_id": str(body.principal_id) if body.principal_id else None,
        "credential_ids": sorted({str(i) for i in body.credential_ids}),
    }
    status, out = await _audited(request, principal, "virtual_key.create", None, details, work)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return out


@router.get(_ONE, summary="Get a virtual key")
async def get_virtual_key(key_id: UUID, request: Request, principal: AdminPrincipal = _ADMIN):
    db = request.state.db
    try:
        row = await _one(db, principal.tenant_id, key_id)
    except HTTPException as exc:
        await record_read(request, tenant_id=principal.tenant_id, status_code=exc.status_code)
        raise
    out = await _view(db, principal.tenant_id, row)
    await record_read(request, tenant_id=principal.tenant_id, status_code=200)
    return out


@router.patch(_ONE, summary="Rename a virtual key or change its pool, models, strategy or expiry")
async def update_virtual_key(
    key_id: UUID,
    body: UpdateVirtualKeyBody,
    request: Request,
    principal: AdminPrincipal = _ADMIN,
):
    db = request.state.db
    tenant_id = principal.tenant_id
    fields = body.model_fields_set

    async def work():
        row = await _one(db, tenant_id, key_id)
        try:
            if "name" in fields:
                if body.name is None:
                    raise ValueError("name cannot be null")
                name = vk.validate_name(body.name)
                if await vk.name_taken(db, tenant_id, name, exclude=row.id):
                    raise _problem(409, "Conflict", "A virtual key with this name exists")
                row.name = name
            if "strategy" in fields:
                if body.strategy is None:
                    raise ValueError("strategy cannot be null")
                row.strategy = vk.validate_strategy(body.strategy)
            if "allowed_models" in fields:
                row.allowed_models = vk.validate_allowed_models(body.allowed_models)
            if "expires_at" in fields:
                row.expires_at = vk.validate_expires_at(body.expires_at)
            if "credential_ids" in fields:
                if body.credential_ids is None:
                    raise ValueError("credential_ids cannot be null")
                cred_ids = await vk.check_credentials(db, tenant_id, body.credential_ids)
                await vk.set_pool(db, tenant_id, row.id, cred_ids)
        except ValueError as exc:
            raise _bad(exc) from exc
        row.updated_at = vk._utcnow()
        await db.flush()
        return 200, await _view(db, tenant_id, row), row.id

    _, out = await _audited(
        request, principal, "virtual_key.update", key_id, {"fields": sorted(fields)}, work
    )
    return out


@router.post(_ONE + "/revoke", summary="Revoke a virtual key")
async def revoke_virtual_key(key_id: UUID, request: Request, principal: AdminPrincipal = _ADMIN):
    db = request.state.db
    tenant_id = principal.tenant_id

    async def work():
        row = await _one(db, tenant_id, key_id)
        if row.revoked_at is None:
            row.revoked_at = vk._utcnow()
            row.updated_at = row.revoked_at
        await db.flush()
        return 200, await _view(db, tenant_id, row), row.id

    _, out = await _audited(request, principal, "virtual_key.revoke", key_id, {}, work)
    return out


@router.delete(_ONE, status_code=204, summary="Delete a virtual key")
async def delete_virtual_key(key_id: UUID, request: Request, principal: AdminPrincipal = _ADMIN):
    db = request.state.db
    tenant_id = principal.tenant_id

    async def work():
        row = await _one(db, tenant_id, key_id)
        await db.delete(row)
        await db.flush()
        return 204, None, key_id

    await _audited(request, principal, "virtual_key.delete", key_id, {}, work)
    return Response(status_code=204)


@gateway_router.post(
    "/virtual-keys/resolve", summary="Resolve a virtual key (gateway; returns provider keys)"
)
async def resolve_virtual_key(body: ResolveVirtualKeyBody, request: Request) -> Response:
    """The key's binding and its pool's active credentials, decrypted, for
    the gateway to use in memory. Needs an ingest key holding
    ``virtual-keys:resolve``. An unknown, revoked or expired key, or one
    bound to a disabled principal, is ``{"active": false, "reason": ...}``
    (200). Never cached by intermediaries (no-store)."""
    tenant_id = await resolve_tenant(request, tenant_keys.VIRTUAL_KEYS_RESOLVE)
    db = request.state.db
    actor = audit_trail.tenant_key_actor(request)
    result = await vk.resolve(db, tenant_id, body.key, actor=actor)
    await db.commit()
    return Response(
        content=json.dumps(result, separators=(",", ":")),
        media_type="application/json",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
