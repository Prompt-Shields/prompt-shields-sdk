"""Tenant admin routes for SSO and SCIM (#339), mounted at /api/v1/admin.

  GET    /sso/oidc                   the tenant's OIDC login configuration
  PUT    /sso/oidc                   create or replace it (owner only)
  DELETE /sso/oidc                   remove it (owner only); SSO sessions
                                     already issued keep working until
                                     they expire or are revoked
  POST   /scim-tokens                issue a SCIM token (owner only)
  GET    /scim-tokens                list SCIM tokens (metadata only)
  DELETE /scim-tokens/{id}           revoke a SCIM token
  GET    /idp-group-roles            the roles that come with each group
  PUT    /idp-group-roles/{team_id}  set a group's member and tenant roles
  DELETE /idp-group-roles/{team_id}  back to the default (viewer, no tenant
                                     role)

Every route needs ``admin:members`` (GETs also take ``admin:read``).

Why owner only: whoever sets the OIDC issuer decides which identity logs in
as which principal, owners included; and a SCIM token can disable any
principal but the last enabled owner. So both need an owner
(``admin_auth.can_manage_owner``). Setting a group's roles needs what
granting those roles would (``rbac_admin._check_can_grant``); ``owner`` is
never accepted. Changing or removing a group's roles applies at once to the
members SCIM put in it (collector/idp_sync.py).

Every change is audited (``sso.oidc.update``, ``sso.oidc.delete``,
``scim_token.create``, ``scim_token.revoke``, ``idp_group_role.set``,
``idp_group_role.delete``, and the membership changes they cause). The OIDC
client secret is write-only (sealed with the PS_WEBHOOK_SECRET_KEYS keyring)
and never returned or audited.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from db.models import IdpGroupRole, Membership, ScimToken, Team, TenantOidcConfig
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from collector import idp_sync, oidc, secret_keyring
from collector.admin_auth import (
    AdminPrincipal,
    add_audit,
    can_manage_owner,
    created_by,
    require_admin,
)
from collector.rbac_admin import NAME, _check_can_grant
from collector.sanitize import BoundedJsonRoute, StorableStr
from collector.scim import SCIM_SCOPES, TOKEN_WARNING_SCIM, issue_token, serialize_token
from collector.sso import CLIENT_SECRET_INFO, secret_binding
from collector.tenant_keys import DuplicateName

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"], route_class=BoundedJsonRoute)

_MEMBERS = Depends(require_admin("admin:members"))

GroupRole = Literal["admin", "developer", "viewer", "auditor", "billing"]
Algorithm = Literal["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]
MIN_SESSION_TTL = 300
MAX_SESSION_TTL = 12 * 3600


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _require_owner(principal: AdminPrincipal, what: str) -> None:
    if not can_manage_owner(principal):
        raise _problem(403, "Forbidden", f"Only an owner can {what}")


# ──────────────────────────────────────────────────────────────────────
# OIDC configuration
# ──────────────────────────────────────────────────────────────────────

class OidcConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issuer: StorableStr = Field(..., min_length=9, max_length=500)
    client_id: StorableStr = Field(..., min_length=1, max_length=255)
    # Write-only. Omitted: keep the stored secret; null: none (a public
    # client, PKCE only). Needs PS_WEBHOOK_SECRET_KEYS.
    client_secret: StorableStr | None = Field(default=None, min_length=1, max_length=2000)
    redirect_uri: StorableStr = Field(..., min_length=9, max_length=1000)
    # Expected aud; omitted or null: the client id.
    audience: StorableStr | None = Field(default=None, min_length=1, max_length=255)
    algorithms: list[Algorithm] | None = Field(default=None, min_length=1)
    subject_claim: str = Field(default="sub", pattern=r"^[A-Za-z0-9_:.-]{1,64}$")
    session_ttl_seconds: int = Field(default=3600, ge=MIN_SESSION_TTL, le=MAX_SESSION_TTL)
    enabled: bool = True


def serialize_oidc_config(cfg: TenantOidcConfig) -> dict:
    return {
        "id": str(cfg.id),
        "issuer": cfg.issuer,
        "client_id": cfg.client_id,
        "client_secret_set": cfg.client_secret_ciphertext is not None,
        "redirect_uri": cfg.redirect_uri,
        "audience": cfg.audience,
        "algorithms": list(cfg.algorithms or ()),
        "subject_claim": cfg.subject_claim,
        "session_ttl_seconds": cfg.session_ttl_seconds,
        "enabled": cfg.enabled,
        "login_path": f"/api/v1/sso/{cfg.tenant_id}/login",
        "created_by": cfg.created_by,
        "created_at": _iso(cfg.created_at),
        "updated_at": _iso(cfg.updated_at),
    }


async def _oidc_config(request: Request, tenant_id: UUID) -> TenantOidcConfig | None:
    return (
        await request.state.db.execute(
            select(TenantOidcConfig).where(TenantOidcConfig.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()


@router.get("/sso/oidc", summary="Get the tenant's OIDC login configuration")
async def get_oidc_config(request: Request, principal: AdminPrincipal = _MEMBERS) -> dict:
    cfg = await _oidc_config(request, principal.tenant_id)
    if cfg is None:
        raise _problem(404, "Not Found", "SSO is not configured")
    return serialize_oidc_config(cfg)


@router.put("/sso/oidc", summary="Create or replace the tenant's OIDC login configuration")
async def put_oidc_config(
    body: OidcConfigBody, request: Request, principal: AdminPrincipal = _MEMBERS
) -> dict:
    _require_owner(principal, "configure SSO")
    for field in ("issuer", "redirect_uri"):
        try:
            oidc.https_url(getattr(body, field))
        except ValueError as exc:
            raise _problem(400, "Bad Request", f"{field}: {exc}") from None
    db = request.state.db
    now = _now()
    cfg = await _oidc_config(request, principal.tenant_id)
    created = cfg is None
    if created:
        # The id is the secret's binding, so it is set before sealing.
        cfg = TenantOidcConfig(
            id=uuid4(),
            tenant_id=principal.tenant_id,
            created_by=created_by(request, principal),
            created_at=now,
        )
        db.add(cfg)
    else:
        cfg.updated_at = now
    cfg.issuer = body.issuer
    cfg.client_id = body.client_id
    cfg.redirect_uri = body.redirect_uri
    cfg.audience = body.audience
    cfg.algorithms = list(dict.fromkeys(body.algorithms or oidc.DEFAULT_ALGORITHMS))
    cfg.subject_claim = body.subject_claim
    cfg.session_ttl_seconds = body.session_ttl_seconds
    cfg.enabled = body.enabled
    secret_changed = "client_secret" in body.model_fields_set
    if secret_changed:
        if body.client_secret is None:
            cfg.client_secret_ciphertext = None
        else:
            try:
                cfg.client_secret_ciphertext = secret_keyring.seal(
                    body.client_secret,
                    secret_binding(principal.tenant_id, cfg.id),
                    CLIENT_SECRET_INFO,
                )
            except secret_keyring.KeyringNotConfigured:
                raise _problem(
                    400,
                    "Bad Request",
                    "Storing a client secret needs PS_WEBHOOK_SECRET_KEYS; use a "
                    "public client (PKCE only, no secret) or configure the keyring",
                ) from None
    await db.flush()
    out = serialize_oidc_config(cfg)
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=200,
        action="sso.oidc.update",
        principal=principal,
        target_type="oidc_config",
        target_id=cfg.id,
        created=created,
        issuer=cfg.issuer,
        client_id=cfg.client_id,
        enabled=cfg.enabled,
        client_secret_changed=secret_changed,
    )
    await db.commit()
    return out


@router.delete("/sso/oidc", status_code=204, summary="Remove the tenant's OIDC login configuration")
async def delete_oidc_config(request: Request, principal: AdminPrincipal = _MEMBERS) -> Response:
    _require_owner(principal, "configure SSO")
    db = request.state.db
    cfg = await _oidc_config(request, principal.tenant_id)
    if cfg is None:
        return Response(status_code=204)
    await db.delete(cfg)
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=204,
        action="sso.oidc.delete",
        principal=principal,
        target_type="oidc_config",
        target_id=cfg.id,
    )
    await db.commit()
    return Response(status_code=204)


# ──────────────────────────────────────────────────────────────────────
# SCIM tokens
# ──────────────────────────────────────────────────────────────────────


class CreateScimTokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = NAME
    scopes: list[Literal["scim:users", "scim:groups"]] = Field(
        default_factory=lambda: list(SCIM_SCOPES), min_length=1
    )
    expires_at: datetime | None = None


@router.post("/scim-tokens", status_code=201, summary="Issue a SCIM token")
async def create_scim_token(
    body: CreateScimTokenBody, request: Request, principal: AdminPrincipal = _MEMBERS
) -> dict:
    _require_owner(principal, "issue a SCIM token")
    expires_at = body.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= _now():
            raise _problem(400, "Bad Request", "expires_at must be in the future")
    db = request.state.db
    try:
        row, token = await issue_token(
            db,
            principal.tenant_id,
            name=body.name,
            scopes=list(body.scopes),
            expires_at=expires_at,
            created_by=created_by(request, principal),
        )
    except DuplicateName:
        raise _problem(409, "Conflict", "An unrevoked SCIM token with this name exists") from None
    out = serialize_token(row)
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=201,
        action="scim_token.create",
        principal=principal,
        target_type="scim_token",
        target_id=row.id,
        name=row.name,
        scopes=out["scopes"],
    )
    await db.commit()
    return {**out, "token": token, "warning": TOKEN_WARNING_SCIM}


@router.get("/scim-tokens", summary="List SCIM tokens (metadata only)")
async def list_scim_tokens(request: Request, principal: AdminPrincipal = _MEMBERS) -> dict:
    rows = (
        (
            await request.state.db.execute(
                select(ScimToken)
                .where(ScimToken.tenant_id == principal.tenant_id)
                .order_by(ScimToken.created_at, ScimToken.id)
            )
        )
        .scalars()
        .all()
    )
    return {"data": [serialize_token(r) for r in rows]}


@router.delete("/scim-tokens/{token_id}", status_code=204, summary="Revoke a SCIM token")
async def revoke_scim_token(
    token_id: UUID, request: Request, principal: AdminPrincipal = _MEMBERS
) -> Response:
    db = request.state.db
    row = (
        await db.execute(
            select(ScimToken).where(
                ScimToken.id == token_id, ScimToken.tenant_id == principal.tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _problem(404, "Not Found", "SCIM token not found")
    if row.revoked_at is None:
        row.revoked_at = _now()
        await add_audit(
            request,
            db,
            tenant_id=principal.tenant_id,
            status_code=204,
            action="scim_token.revoke",
            principal=principal,
            target_type="scim_token",
            target_id=row.id,
            name=row.name,
        )
        await db.commit()
    return Response(status_code=204)


# ──────────────────────────────────────────────────────────────────────
# Group roles
# ──────────────────────────────────────────────────────────────────────


class GroupRolesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_role: GroupRole = idp_sync.DEFAULT_MEMBER_ROLE
    tenant_role: GroupRole | None = None


def serialize_group_roles(row: IdpGroupRole) -> dict:
    return {
        "team_id": str(row.team_id),
        "member_role": row.member_role,
        "tenant_role": row.tenant_role,
        "created_by": row.created_by,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


@router.get("/idp-group-roles", summary="List the roles that come with identity provider groups")
async def list_group_roles(request: Request, principal: AdminPrincipal = _MEMBERS) -> dict:
    rows = (
        (
            await request.state.db.execute(
                select(IdpGroupRole)
                .where(IdpGroupRole.tenant_id == principal.tenant_id)
                .order_by(IdpGroupRole.created_at, IdpGroupRole.id)
            )
        )
        .scalars()
        .all()
    )
    return {"data": [serialize_group_roles(r) for r in rows]}


async def _team(request: Request, tenant_id: UUID, team_id: UUID) -> Team:
    team = (
        await request.state.db.execute(
            select(Team).where(Team.id == team_id, Team.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if team is None:
        raise _problem(404, "Not Found", "Team not found")
    return team


async def _resync_team(
    request: Request, principal: AdminPrincipal, team: Team, member_role: str, status: int
) -> None:
    """Apply a group's (new) roles to the members SCIM put in it."""
    db = request.state.db
    now = _now()

    async def audit(action: str, target_type: str, target_id, **details) -> None:
        await add_audit(
            request,
            db,
            tenant_id=principal.tenant_id,
            status_code=status,
            action=action,
            principal=principal,
            target_type=target_type,
            target_id=target_id,
            **details,
        )

    members = (
        (
            await db.execute(
                select(Membership).where(
                    Membership.team_id == team.id,
                    Membership.project_id.is_(None),
                    Membership.created_by == idp_sync.SCIM_SOURCE,
                )
            )
        )
        .scalars()
        .all()
    )
    for m in members:
        if m.role != member_role:
            before = m.role
            m.role = member_role
            m.updated_at = now
            await audit(
                "role.change",
                "membership",
                m.id,
                principal_id=str(m.principal_id),
                team_id=str(team.id),
                role=member_role,
                previous_role=before,
                source=idp_sync.SCIM_SOURCE,
            )
    await db.flush()
    for pid in sorted({m.principal_id for m in members}):
        await idp_sync.recompute_tenant_role(db, principal.tenant_id, pid, now, audit)


@router.put("/idp-group-roles/{team_id}", summary="Set the roles that come with a group")
async def put_group_roles(
    team_id: UUID, body: GroupRolesBody, request: Request, principal: AdminPrincipal = _MEMBERS
) -> dict:
    db = request.state.db
    team = await _team(request, principal.tenant_id, team_id)
    row = (
        await db.execute(select(IdpGroupRole).where(IdpGroupRole.team_id == team.id))
    ).scalar_one_or_none()
    # Changing a mapping touches the roles it gave as well as the new ones.
    for role in {body.member_role, body.tenant_role} | (
        {row.member_role, row.tenant_role} if row is not None else set()
    ):
        if role is not None:
            _check_can_grant(principal, role)
    now = _now()
    if row is None:
        row = IdpGroupRole(
            tenant_id=principal.tenant_id,
            team_id=team.id,
            created_by=created_by(request, principal),
            created_at=now,
        )
        db.add(row)
    else:
        row.updated_at = now
    row.member_role = body.member_role
    row.tenant_role = body.tenant_role
    await db.flush()
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=200,
        action="idp_group_role.set",
        principal=principal,
        target_type="team",
        target_id=team.id,
        member_role=row.member_role,
        tenant_role=row.tenant_role,
    )
    await _resync_team(request, principal, team, row.member_role, 200)
    out = serialize_group_roles(row)
    await db.commit()
    return out


@router.delete(
    "/idp-group-roles/{team_id}", status_code=204, summary="Reset the roles that come with a group"
)
async def delete_group_roles(
    team_id: UUID, request: Request, principal: AdminPrincipal = _MEMBERS
) -> Response:
    db = request.state.db
    team = await _team(request, principal.tenant_id, team_id)
    row = (
        await db.execute(select(IdpGroupRole).where(IdpGroupRole.team_id == team.id))
    ).scalar_one_or_none()
    if row is None:
        return Response(status_code=204)
    for role in (row.member_role, row.tenant_role):
        if role is not None:
            _check_can_grant(principal, role)
    await db.delete(row)
    await db.flush()
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=204,
        action="idp_group_role.delete",
        principal=principal,
        target_type="team",
        target_id=team.id,
    )
    await _resync_team(request, principal, team, idp_sync.DEFAULT_MEMBER_ROLE, 204)
    await db.commit()
    return Response(status_code=204)
