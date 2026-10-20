"""Operator endpoints: tenants, data retention and tenant erasure (#85).

Mounted at /api/v1/operator. These act across tenants, so they are not
behind a tenant API key: they take ``Authorization: Bearer $PS_ADMIN_TOKEN``.
With ``PS_ADMIN_TOKEN`` unset every route answers 404, as if absent.

Tenants:

  POST   /tenants                     create (optionally with a first ingest
                                      key, returned once); 409 on a name in use
  GET    /tenants                     list (?status=active|disabled)
  GET    /tenants/{id}                read
  PATCH  /tenants/{id}                name, domain, settings (merged; null
                                      removes a key), disabled
  DELETE /tenants/{id}?confirm={id}   erase the tenant and all its data
                                      (below; unchanged)
  POST   /tenants/{id}/admin-tokens   bootstrap a scoped admin token
  GET|PUT /tenants/{id}/retention     retention override (below)

Disabling a tenant keeps its data and stops its ingest keys, admin tokens
and Partner API credentials (403) until it is enabled again.

Audit trail (collector.audit_trail): purges, retention changes and
erasures are recorded in the tenant's chain by collector.retention;
reading a tenant's retention is recorded here; a wrong or missing operator
token is an ``auth.failure`` on the system chain (rate-limited).
``GET /audit/verify`` checks the hash chains.
"""

from __future__ import annotations

import hmac
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from db.models import Tenant
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select

from collector import audit_trail, retention, rls, tenant_keys
from collector.admin_auth import (
    ADMIN_SCOPES,
    TOKEN_WARNING,
    add_audit,
    issue_token,
    serialize_token,
    unknown_scopes,
)
from collector.config import settings
from collector.sanitize import BoundedJsonRoute, StorableJson, StorableStr

router = APIRouter(
    prefix="/api/v1/operator", tags=["Operator"], route_class=BoundedJsonRoute
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


async def require_operator(request: Request) -> str:
    """Check the operator token. Returns the actor name used in the audit log.

    Operator routes act across tenants, so the request's session bypasses
    row-level security (logged on ``collector.rls``).
    """
    token = settings.admin_token
    if not token:
        raise _problem(404, "Not Found", "Not Found")
    auth = request.headers.get("Authorization", "")
    presented = (
        auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    )
    if not presented or not hmac.compare_digest(presented.encode(), token.encode()):
        await audit_trail.record_auth_failure(
            request,
            realm="operator",
            reason="invalid_credentials" if presented else "missing_credentials",
            status_code=401,
        )
        raise _problem(401, "Unauthorized", "Invalid operator token")
    actor = audit_trail.OPERATOR.id
    await rls.bypass(
        request.state.db, f"operator:{request.method} {request.url.path}", actor=actor
    )
    return actor


class PurgeBody(BaseModel):
    tenant_id: UUID | None = None
    dry_run: bool = False


@router.post("/retention/purge", summary="Delete usage events past retention")
async def purge(
    body: PurgeBody, request: Request, actor: str = Depends(require_operator)
):
    db = request.state.db
    if body.tenant_id is not None:
        result = await retention.purge_tenant_events(
            db, body.tenant_id, dry_run=body.dry_run, actor=actor
        )
        if result is None:
            raise _problem(404, "Not Found", "Tenant not found")
        results = [result]
    else:
        results = await retention.purge_expired_events(
            db, dry_run=body.dry_run, actor=actor
        )
    return {"data": [asdict(r) for r in results]}


class RetentionBody(BaseModel):
    # None clears the override, falling back to PS_RETENTION_DAYS_DEFAULT.
    retention_days: int | None = Field(
        None, ge=retention.MIN_RETENTION_DAYS, le=retention.MAX_RETENTION_DAYS
    )


@router.get("/tenants/{tenant_id}/retention", summary="Show a tenant's retention")
async def get_retention(
    tenant_id: UUID, request: Request, actor: str = Depends(require_operator)
):
    row = (
        await request.state.db.execute(
            select(Tenant.settings).where(Tenant.id == tenant_id)
        )
    ).one_or_none()
    if row is None:
        raise _problem(404, "Not Found", "Tenant not found")
    tenant_settings = row[0] or {}
    await audit_trail.record_committed(
        request.state.db,
        tenant_id=tenant_id,
        action="tenant.retention.read",
        actor=audit_trail.OPERATOR,
        target_type="tenant",
        target_id=tenant_id,
        status_code=200,
    )
    return {
        "tenant_id": str(tenant_id),
        "retention_days": retention.retention_days_for(tenant_settings),
        "cutoff": retention.retention_cutoff(tenant_settings).isoformat(),
    }


@router.put("/tenants/{tenant_id}/retention", summary="Set a tenant's retention")
async def put_retention(
    tenant_id: UUID,
    body: RetentionBody,
    request: Request,
    actor: str = Depends(require_operator),
):
    days = await retention.set_tenant_retention(
        request.state.db, tenant_id, body.retention_days, actor=actor
    )
    if days is None:
        raise _problem(404, "Not Found", "Tenant not found")
    return {"tenant_id": str(tenant_id), "retention_days": days}


@router.delete("/tenants/{tenant_id}", summary="Erase a tenant and all its data")
async def erase(
    tenant_id: UUID,
    request: Request,
    confirm: str = Query(..., description="Must repeat the tenant id"),
    actor: str = Depends(require_operator),
):
    if confirm.strip().lower() != str(tenant_id):
        raise _problem(400, "Bad Request", "confirm must equal the tenant id")
    result = await retention.erase_tenant(request.state.db, tenant_id, actor=actor)
    return asdict(result)


# ──────────────────────────────────────────────────────────────────────
# Tenants
# ──────────────────────────────────────────────────────────────────────

# Settings keys the tenants API never stores: a plaintext ingest key lived
# under "api_key" before migration 004.
_FORBIDDEN_SETTINGS = frozenset({"api_key"})


def _tenant_dict(t: Tenant) -> dict:
    tid = str(t.id)
    return {
        "id": tid,
        "name": t.name,
        "domain": t.domain,
        "settings": dict(t.settings or {}),
        "status": "disabled" if t.disabled_at else "active",
        "disabled_at": t.disabled_at.isoformat() if t.disabled_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        # Related operator routes; erasure is the existing DELETE.
        "links": {
            "retention": f"/api/v1/operator/tenants/{tid}/retention",
            "erase": f"/api/v1/operator/tenants/{tid}?confirm={tid}",
        },
    }


def _check_settings(values: dict) -> None:
    bad = sorted(_FORBIDDEN_SETTINGS & set(values))
    if bad:
        raise _problem(400, "Bad Request", f"settings may not contain {bad}")
    if retention.RETENTION_SETTING_KEY in values:
        days = values[retention.RETENTION_SETTING_KEY]
        if days is not None and (
            not isinstance(days, int)
            or isinstance(days, bool)
            or not retention.MIN_RETENTION_DAYS <= days <= retention.MAX_RETENTION_DAYS
        ):
            raise _problem(
                400,
                "Bad Request",
                f"settings.retention_days must be an integer between "
                f"{retention.MIN_RETENTION_DAYS} and {retention.MAX_RETENTION_DAYS}",
            )


def _future(expires_at: datetime | None) -> datetime | None:
    if expires_at is None:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        raise _problem(400, "Bad Request", "expires_at must be in the future")
    return expires_at


async def _name_in_use(db, name: str, exclude: UUID | None = None) -> bool:
    stmt = select(Tenant.id).where(func.lower(Tenant.name) == name.strip().lower())
    if exclude is not None:
        stmt = stmt.where(Tenant.id != exclude)
    return (await db.execute(stmt)).first() is not None


async def _get_tenant(db, tenant_id: UUID) -> Tenant:
    row = (
        await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        raise _problem(404, "Not Found", "Tenant not found")
    return row


class CreateTenantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: StorableStr = Field(..., min_length=1, max_length=200)
    domain: StorableStr | None = Field(default=None, max_length=253)
    settings: StorableJson = Field(default_factory=dict)
    # Issue a first ingest key (returned once) named api_key_name.
    issue_api_key: bool = True
    api_key_name: StorableStr = Field(default="default", min_length=1, max_length=100)


class UpdateTenantBody(BaseModel):
    """Fields to change; omitted fields keep their value."""

    model_config = ConfigDict(extra="forbid")

    name: StorableStr | None = Field(default=None, min_length=1, max_length=200)
    domain: StorableStr | None = Field(default=None, max_length=253)
    # Merged into the stored settings; a key set to null is removed.
    settings: StorableJson | None = None
    # true disables the tenant (keys, tokens and partners refused), false
    # enables it again.
    disabled: bool | None = None

    @model_validator(mode="after")
    def _not_empty(self):
        if not self.model_fields_set:
            raise ValueError("give at least one of name, domain, settings, disabled")
        for f in ("name", "settings", "disabled"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class BootstrapAdminTokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: StorableStr = Field(..., min_length=1, max_length=100)
    scopes: list[str] = Field(..., min_length=1)
    expires_at: datetime | None = None


@router.post("/tenants", status_code=201, summary="Create a tenant")
async def create_tenant(
    body: CreateTenantBody, request: Request, actor: str = Depends(require_operator)
):
    db = request.state.db
    _check_settings(body.settings)
    if await _name_in_use(db, body.name):
        raise _problem(409, "Conflict", "A tenant with this name already exists")
    tenant = Tenant(
        name=body.name.strip(),
        domain=body.domain,
        settings={k: v for k, v in body.settings.items() if v is not None},
        created_at=datetime.now(UTC),
    )
    db.add(tenant)
    await db.flush()
    api_key = None
    if body.issue_api_key:
        row, plaintext = await tenant_keys.issue_key(db, tenant.id, body.api_key_name)
        api_key = {**tenant_keys.serialize_key(row), "key": plaintext}
    await add_audit(
        request,
        db,
        tenant_id=tenant.id,
        status_code=201,
        action="tenant.create",
        principal=None,
        target_type="tenant",
        target_id=tenant.id,
        api_key_id=api_key["id"] if api_key else None,
    )
    out = _tenant_dict(tenant)
    await db.commit()
    return {
        **out,
        "api_key": api_key,
        "warning": tenant_keys.KEY_WARNING if api_key else None,
    }


@router.get("/tenants", summary="List tenants")
async def list_tenants(
    request: Request,
    status: Literal["active", "disabled"] | None = None,
    actor: str = Depends(require_operator),
):
    stmt = select(Tenant).order_by(Tenant.created_at, Tenant.id)
    if status == "active":
        stmt = stmt.where(Tenant.disabled_at.is_(None))
    elif status == "disabled":
        stmt = stmt.where(Tenant.disabled_at.is_not(None))
    rows = (await request.state.db.execute(stmt)).scalars().all()
    return {"data": [_tenant_dict(t) for t in rows]}


@router.get("/tenants/{tenant_id}", summary="Get a tenant")
async def get_tenant(
    tenant_id: UUID, request: Request, actor: str = Depends(require_operator)
):
    return _tenant_dict(await _get_tenant(request.state.db, tenant_id))


@router.patch(
    "/tenants/{tenant_id}",
    summary="Change a tenant's name, domain or settings, or disable and enable it",
)
async def update_tenant(
    tenant_id: UUID,
    body: UpdateTenantBody,
    request: Request,
    actor: str = Depends(require_operator),
):
    db = request.state.db
    tenant = await _get_tenant(db, tenant_id)
    changed: list[str] = []
    if body.name is not None and body.name.strip() != tenant.name:
        if await _name_in_use(db, body.name, exclude=tenant.id):
            raise _problem(409, "Conflict", "A tenant with this name already exists")
        tenant.name = body.name.strip()
        changed.append("name")
    if "domain" in body.model_fields_set and body.domain != tenant.domain:
        tenant.domain = body.domain
        changed.append("domain")
    if body.settings is not None:
        _check_settings(body.settings)
        merged = dict(tenant.settings or {})
        for key, value in body.settings.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        if merged != (tenant.settings or {}):
            tenant.settings = merged
            changed.append("settings")
    if body.disabled is not None and body.disabled != (tenant.disabled_at is not None):
        tenant.disabled_at = datetime.now(UTC) if body.disabled else None
        changed.append("disabled")
    if changed:
        await add_audit(
            request,
            db,
            tenant_id=tenant.id,
            status_code=200,
            action="tenant.update",
            principal=None,
            target_type="tenant",
            target_id=tenant.id,
            changed_fields=changed,
            disabled=tenant.disabled_at is not None,
        )
        await db.commit()
        await db.refresh(tenant)
    return _tenant_dict(tenant)


@router.post(
    "/tenants/{tenant_id}/admin-tokens",
    status_code=201,
    summary="Issue a scoped admin token for a tenant (bootstrap)",
)
async def bootstrap_admin_token(
    tenant_id: UUID,
    body: BootstrapAdminTokenBody,
    request: Request,
    actor: str = Depends(require_operator),
):
    db = request.state.db
    tenant = await _get_tenant(db, tenant_id)
    bad = unknown_scopes(body.scopes)
    if bad:
        raise _problem(
            400, "Bad Request", f"Unknown scopes: {bad}. Allowed: {list(ADMIN_SCOPES)}"
        )
    try:
        row, token = await issue_token(
            db,
            tenant.id,
            name=body.name,
            scopes=body.scopes,
            expires_at=_future(body.expires_at),
            created_by="operator",
        )
    except tenant_keys.DuplicateName:
        raise _problem(
            409, "Conflict", "An unrevoked admin token with this name already exists"
        ) from None
    out = serialize_token(row)
    await add_audit(
        request,
        db,
        tenant_id=tenant.id,
        status_code=201,
        action="admin_token.create",
        principal=None,
        target_type="admin_token",
        target_id=out["id"],
        name=row.name,
        scopes=out["scopes"],
    )
    await db.commit()
    return {**out, "token": token, "warning": TOKEN_WARNING}
@router.get("/audit/verify", summary="Verify the audit trail hash chains")
async def verify_audit(
    request: Request,
    tenant_id: UUID | None = None,
    actor: str = Depends(require_operator),
):
    """Walks each chain (one tenant's, or every chain including the system
    chain) and reports the first break. ``ok`` is false if any chain is
    broken; ``head_hash`` is worth recording outside the database."""
    results = await audit_trail.verify_all(request.state.db, tenant_id)
    return {
        "ok": all(r.ok for r in results),
        "data": [asdict(r) for r in results],
    }
