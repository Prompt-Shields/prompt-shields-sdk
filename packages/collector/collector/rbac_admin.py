"""Principals, teams, projects and role memberships (#338).

Tenant admin routes, mounted at /api/v1/admin (auth: collector/admin_auth.py;
roles: collector/rbac.py). Every write needs ``admin:members``; GETs also
take ``admin:read``.

  GET    /roles                            built-in roles and their scopes
  POST   /principals                       create a user or service account
  GET    /principals                       list (?kind=, ?status=)
  GET    /principals/{id}                  read, with its memberships
  PATCH  /principals/{id}                  change ids/names, disable, enable
  DELETE /principals/{id}                  delete; revokes its admin tokens
                                           and partner credentials
  POST   /teams | GET /teams               create | list
  GET | PATCH | DELETE /teams/{id}         read | rename | delete (409 while
                                           it has projects)
  POST   /projects | GET /projects         create | list (?team_id=)
  GET | PATCH | DELETE /projects/{id}      read | rename | delete
  POST   /memberships | GET /memberships   grant a role | list (?principal_id=,
                                           ?team_id=, ?project_id=)
  GET | PATCH | DELETE /memberships/{id}   read | change role | remove

Partner read routes, mounted at /partner/v1 with scope ``members:read``:

  GET    /teams                            the tenant's teams
  GET    /projects                         the tenant's projects (?team_id=)

No privilege escalation: granting a role (or changing a membership to it)
needs every admin scope that role carries; granting, changing or removing an
``owner`` membership needs an owner (``admin_auth.can_manage_owner``), and
the tenant's last enabled owner cannot be removed, demoted, disabled or
deleted (409). Every route that can drop an owner (change or remove a
membership, disable or delete a principal) takes a per-tenant transaction
advisory lock (``lock_owners``) before it reads anything, and holds it
through the write and commit, so two concurrent demotions of two different
owners cannot both pass the check (#372). Disabling, enabling,
re-identifying (``external_id``) or deleting a principal needs what granting each of its roles would; issuing
an owner-capable admin token needs an owner (collector/admin_api.py).

Every change writes a hash-chained audit trail row in the same transaction
(``principal.*``, ``team.*``, ``project.*``, ``membership.add``,
``membership.remove``, ``role.change``): ids, kinds, roles and team/project
names; never an email or display name.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from db.models import (
    AdminToken,
    Membership,
    PartnerCredential,
    Principal,
    Project,
    Team,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from collector import rbac
from collector.admin_auth import (
    ADMIN_SCOPES,
    AdminPrincipal,
    add_audit,
    can_manage_owner,
    created_by,
    require_admin,
)
from collector.partner_auth import PartnerPrincipal, require_scope
from collector.partner_registry import _audit, _enforce_rate_limit
from collector.sanitize import BoundedJsonRoute, StorableStr

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"], route_class=BoundedJsonRoute)
partner_router = APIRouter(
    prefix="/partner/v1", tags=["Partner API"], route_class=BoundedJsonRoute
)

_MEMBERS = Depends(require_admin("admin:members"))
_READ = Depends(require_scope(rbac.MEMBERS_READ_SCOPE))

Kind = Literal["user", "service_account"]
Role = Literal["owner", "admin", "developer", "viewer", "auditor", "billing"]
NAME = Field(..., min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _now() -> datetime:
    return datetime.now(UTC)


# ──────────────────────────────────────────────────────────────────────
# Serializers
# ──────────────────────────────────────────────────────────────────────


def serialize_principal(p: Principal) -> dict:
    return {
        "id": str(p.id),
        "kind": p.kind,
        "external_id": p.external_id,
        "email": p.email,
        "display_name": p.display_name,
        "status": "disabled" if p.disabled_at else "active",
        "created_at": _iso(p.created_at),
        "updated_at": _iso(p.updated_at),
        "disabled_at": _iso(p.disabled_at),
    }


def serialize_team(t: Team) -> dict:
    return {
        "id": str(t.id),
        "name": t.name,
        "description": t.description,
        "created_at": _iso(t.created_at),
        "updated_at": _iso(t.updated_at),
    }


def serialize_project(p: Project) -> dict:
    return {
        "id": str(p.id),
        "team_id": str(p.team_id),
        "name": p.name,
        "description": p.description,
        "created_at": _iso(p.created_at),
        "updated_at": _iso(p.updated_at),
    }


def serialize_membership(m: Membership) -> dict:
    return {
        "id": str(m.id),
        "principal_id": str(m.principal_id),
        "team_id": str(m.team_id) if m.team_id else None,
        "project_id": str(m.project_id) if m.project_id else None,
        "role": m.role,
        "created_by": m.created_by,
        "created_at": _iso(m.created_at),
        "updated_at": _iso(m.updated_at),
    }


# ──────────────────────────────────────────────────────────────────────
# Shared checks
# ──────────────────────────────────────────────────────────────────────


async def _get(db: AsyncSession, model, tenant_id: UUID, row_id: UUID, what: str):
    row = (
        await db.execute(select(model).where(model.id == row_id, model.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        raise _problem(404, "Not Found", f"{what} not found")
    return row


def _check_can_grant(principal: AdminPrincipal, role: str) -> None:
    """403 unless the caller holds every admin scope ``role`` carries (no
    escalation through a membership) and, for owner, is an owner."""
    beyond = sorted((rbac.ROLES[role][1] & set(ADMIN_SCOPES)) - principal.scopes)
    if beyond:
        raise _problem(
            403,
            "Forbidden",
            f"Cannot grant the {role} role: it carries scopes the caller does "
            f"not hold: {beyond}",
        )
    if role == rbac.OWNER and not can_manage_owner(principal):
        raise _problem(403, "Forbidden", "Only an owner can grant, change or remove the owner role")


def _check_can_touch(principal: AdminPrincipal, m: Membership) -> None:
    """Changing or removing an existing membership needs what granting it
    would (so an admin cannot remove an owner)."""
    if m.role in rbac.ROLES:
        _check_can_grant(principal, m.role)


async def _other_owners(db: AsyncSession, tenant_id: UUID, exclude_principal: UUID) -> int:
    """Enabled tenant-wide owners other than ``exclude_principal``."""
    return (
        await db.execute(
            select(func.count())
            .select_from(Membership)
            .join(Principal, Principal.id == Membership.principal_id)
            .where(
                Membership.tenant_id == tenant_id,
                Membership.team_id.is_(None),
                Membership.role == rbac.OWNER,
                Membership.principal_id != exclude_principal,
                Principal.disabled_at.is_(None),
            )
        )
    ).scalar_one()


async def _is_tenant_owner(db: AsyncSession, tenant_id: UUID, principal_id: UUID) -> bool:
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


# Advisory lock namespace for a tenant's owner set (the second key is the
# tenant id), as collector/usage_rollups.py keys its per-tenant lock.
_OWNERS_LOCK = "rbac.owners"


async def lock_owners(db: AsyncSession, tenant_id: UUID) -> None:
    """Serialise changes that can drop one of ``tenant_id``'s owners: a
    transaction advisory lock, held until commit or rollback. Taken before
    the route reads the membership or principal it changes, so the
    last-owner check (``_keep_an_owner``) and the write see every owner
    change committed before them (READ COMMITTED re-reads per statement).
    Re-entrant within a transaction. Without it, two concurrent demotions
    of two different owners could each count the other as remaining and
    leave the tenant with none (#372)."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k), hashtext(:t))"),
        {"k": _OWNERS_LOCK, "t": str(tenant_id)},
    )


async def _keep_an_owner(db: AsyncSession, tenant_id: UUID, principal_id: UUID) -> None:
    """409 when ``principal_id`` is the tenant's last enabled owner. Holds
    the tenant's owner lock (``lock_owners``) until the transaction ends;
    callers take it before reading the rows they change."""
    await lock_owners(db, tenant_id)
    if await _is_tenant_owner(db, tenant_id, principal_id) and not await _other_owners(
        db, tenant_id, principal_id
    ):
        raise _problem(
            409,
            "Conflict",
            "This is the tenant's last enabled owner; make another principal "
            "owner first",
        )


# ──────────────────────────────────────────────────────────────────────
# Roles
# ──────────────────────────────────────────────────────────────────────


@router.get("/roles", summary="List the built-in roles and their scopes")
async def list_roles(principal: AdminPrincipal = _MEMBERS) -> dict:
    return {
        "data": [
            {
                "name": name,
                "description": desc,
                "admin_scopes": [s for s in ADMIN_SCOPES if s in scopes],
                "partner_scopes": sorted(scopes - set(ADMIN_SCOPES)),
            }
            for name, (desc, scopes) in rbac.ROLES.items()
        ]
    }


# ──────────────────────────────────────────────────────────────────────
# Principals
# ──────────────────────────────────────────────────────────────────────


class CreatePrincipalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    external_id: StorableStr | None = Field(default=None, min_length=1, max_length=255)
    email: StorableStr | None = Field(default=None, min_length=3, max_length=320)
    display_name: StorableStr | None = Field(default=None, min_length=1, max_length=200)


class UpdatePrincipalBody(BaseModel):
    """Fields left out are unchanged; null clears an optional field."""

    model_config = ConfigDict(extra="forbid")

    external_id: StorableStr | None = Field(default=None, min_length=1, max_length=255)
    email: StorableStr | None = Field(default=None, min_length=3, max_length=320)
    display_name: StorableStr | None = Field(default=None, min_length=1, max_length=200)
    disabled: bool | None = None


async def _external_id_taken(
    db: AsyncSession, tenant_id: UUID, external_id: str | None, exclude: UUID | None = None
) -> bool:
    if external_id is None:
        return False
    stmt = select(Principal.id).where(
        Principal.tenant_id == tenant_id, Principal.external_id == external_id
    )
    if exclude is not None:
        stmt = stmt.where(Principal.id != exclude)
    return (await db.execute(stmt)).first() is not None


@router.post("/principals", status_code=201, summary="Create a principal")
async def create_principal(
    body: CreatePrincipalBody, request: Request, principal: AdminPrincipal = _MEMBERS
) -> dict:
    db = request.state.db
    if await _external_id_taken(db, principal.tenant_id, body.external_id):
        raise _problem(409, "Conflict", "A principal with this external_id already exists")
    row = Principal(
        tenant_id=principal.tenant_id,
        kind=body.kind,
        external_id=body.external_id,
        email=body.email,
        display_name=body.display_name,
        created_at=_now(),
    )
    db.add(row)
    await db.flush()
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=201,
        action="principal.create",
        principal=principal,
        target_type="principal",
        target_id=row.id,
        kind=row.kind,
    )
    out = serialize_principal(row)
    await db.commit()
    return out


@router.get("/principals", summary="List principals")
async def list_principals(
    request: Request,
    kind: Kind | None = None,
    status: Literal["active", "disabled"] | None = None,
    principal: AdminPrincipal = _MEMBERS,
) -> dict:
    stmt = select(Principal).where(Principal.tenant_id == principal.tenant_id)
    if kind:
        stmt = stmt.where(Principal.kind == kind)
    if status == "active":
        stmt = stmt.where(Principal.disabled_at.is_(None))
    elif status == "disabled":
        stmt = stmt.where(Principal.disabled_at.is_not(None))
    rows = (
        (await request.state.db.execute(stmt.order_by(Principal.created_at, Principal.id)))
        .scalars()
        .all()
    )
    return {"data": [serialize_principal(r) for r in rows]}


@router.get("/principals/{principal_id}", summary="Get a principal and its memberships")
async def get_principal(
    principal_id: UUID, request: Request, principal: AdminPrincipal = _MEMBERS
) -> dict:
    db = request.state.db
    row = await _get(db, Principal, principal.tenant_id, principal_id, "Principal")
    memberships = (
        (
            await db.execute(
                select(Membership)
                .where(Membership.principal_id == row.id)
                .order_by(Membership.created_at, Membership.id)
            )
        )
        .scalars()
        .all()
    )
    return {
        **serialize_principal(row),
        "memberships": [serialize_membership(m) for m in memberships],
    }


@router.patch("/principals/{principal_id}", summary="Change or disable a principal")
async def update_principal(
    principal_id: UUID,
    body: UpdatePrincipalBody,
    request: Request,
    principal: AdminPrincipal = _MEMBERS,
) -> dict:
    db = request.state.db
    if body.disabled:
        await lock_owners(db, principal.tenant_id)
    row = await _get(db, Principal, principal.tenant_id, principal_id, "Principal")
    sent = body.model_fields_set
    changed: list[str] = []
    toggles = body.disabled is not None and body.disabled != (row.disabled_at is not None)
    if toggles or ("external_id" in sent and body.external_id != row.external_id):
        # Disabling or re-enabling a principal (which re-activates its
        # credentials) or changing its identity-provider subject needs what
        # granting each of its roles would, like deleting it does.
        memberships = (
            (await db.execute(select(Membership).where(Membership.principal_id == row.id)))
            .scalars()
            .all()
        )
        for m in memberships:
            if m.role == rbac.OWNER and not can_manage_owner(principal):
                raise _problem(
                    403, "Forbidden", "Only an owner can disable, enable or re-identify an owner"
                )
            _check_can_touch(principal, m)
    if "external_id" in sent and body.external_id != row.external_id:
        if await _external_id_taken(db, principal.tenant_id, body.external_id, row.id):
            raise _problem(409, "Conflict", "A principal with this external_id already exists")
        row.external_id = body.external_id
        changed.append("external_id")
    for field in ("email", "display_name"):
        if field in sent and getattr(body, field) != getattr(row, field):
            setattr(row, field, getattr(body, field))
            changed.append(field)
    action = "principal.update"
    if toggles:
        if body.disabled:
            await _keep_an_owner(db, principal.tenant_id, row.id)
        row.disabled_at = _now() if body.disabled else None
        action = "principal.disable" if body.disabled else "principal.enable"
    elif not changed:
        return serialize_principal(row)
    row.updated_at = _now()
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=200,
        action=action,
        principal=principal,
        target_type="principal",
        target_id=row.id,
        fields=changed,
    )
    out = serialize_principal(row)
    await db.commit()
    return out


@router.delete(
    "/principals/{principal_id}", status_code=204, summary="Delete a principal"
)
async def delete_principal(
    principal_id: UUID, request: Request, principal: AdminPrincipal = _MEMBERS
) -> Response:
    """Removes its memberships, revokes the admin tokens and partner
    credentials issued for it (rows kept, unlinked), then deletes it."""
    db = request.state.db
    await lock_owners(db, principal.tenant_id)
    row = await _get(db, Principal, principal.tenant_id, principal_id, "Principal")
    memberships = (
        (await db.execute(select(Membership).where(Membership.principal_id == row.id)))
        .scalars()
        .all()
    )
    for m in memberships:
        _check_can_touch(principal, m)
    await _keep_an_owner(db, principal.tenant_id, row.id)
    now = _now()
    tokens = (
        (await db.execute(select(AdminToken).where(AdminToken.principal_id == row.id)))
        .scalars()
        .all()
    )
    for t in tokens:
        if t.revoked_at is None:
            t.revoked_at = now
        t.principal_id = None
    creds = (
        (
            await db.execute(
                select(PartnerCredential).where(PartnerCredential.principal_id == row.id)
            )
        )
        .scalars()
        .all()
    )
    for c in creds:
        c.is_active = "false"
        c.principal_id = None
    for m in memberships:
        await db.delete(m)
    await db.flush()
    await db.delete(row)
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=204,
        action="principal.delete",
        principal=principal,
        target_type="principal",
        target_id=principal_id,
        memberships_removed=[str(m.id) for m in memberships],
        admin_tokens_revoked=[str(t.id) for t in tokens],
        partner_credentials_revoked=[str(c.id) for c in creds],
    )
    await db.commit()
    return Response(status_code=204)


# ──────────────────────────────────────────────────────────────────────
# Teams
# ──────────────────────────────────────────────────────────────────────


class CreateTeamBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = NAME
    description: StorableStr | None = Field(default=None, max_length=1000)


class UpdateTeamBody(BaseModel):
    """Fields left out are unchanged."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._ -]*$"
    )
    description: StorableStr | None = Field(default=None, max_length=1000)


async def _name_taken(
    db: AsyncSession, model, tenant_id: UUID, name: str, exclude: UUID | None = None
) -> bool:
    stmt = select(model.id).where(model.tenant_id == tenant_id, model.name == name)
    if exclude is not None:
        stmt = stmt.where(model.id != exclude)
    return (await db.execute(stmt)).first() is not None


@router.post("/teams", status_code=201, summary="Create a team")
async def create_team(
    body: CreateTeamBody, request: Request, principal: AdminPrincipal = _MEMBERS
) -> dict:
    db = request.state.db
    if await _name_taken(db, Team, principal.tenant_id, body.name):
        raise _problem(409, "Conflict", "A team with this name already exists")
    row = Team(
        tenant_id=principal.tenant_id,
        name=body.name,
        description=body.description,
        created_at=_now(),
    )
    db.add(row)
    await db.flush()
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=201,
        action="team.create",
        principal=principal,
        target_type="team",
        target_id=row.id,
        name=row.name,
    )
    out = serialize_team(row)
    await db.commit()
    return out


@router.get("/teams", summary="List teams")
async def list_teams(request: Request, principal: AdminPrincipal = _MEMBERS) -> dict:
    rows = (
        (
            await request.state.db.execute(
                select(Team)
                .where(Team.tenant_id == principal.tenant_id)
                .order_by(Team.name, Team.id)
            )
        )
        .scalars()
        .all()
    )
    return {"data": [serialize_team(r) for r in rows]}


@router.get("/teams/{team_id}", summary="Get a team")
async def get_team(team_id: UUID, request: Request, principal: AdminPrincipal = _MEMBERS) -> dict:
    return serialize_team(await _get(request.state.db, Team, principal.tenant_id, team_id, "Team"))


@router.patch("/teams/{team_id}", summary="Rename or describe a team")
async def update_team(
    team_id: UUID, body: UpdateTeamBody, request: Request, principal: AdminPrincipal = _MEMBERS
) -> dict:
    db = request.state.db
    row = await _get(db, Team, principal.tenant_id, team_id, "Team")
    before = row.name
    changed = await _apply_name_description(db, Team, principal.tenant_id, row, body, "team")
    if not changed:
        return serialize_team(row)
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=200,
        action="team.update",
        principal=principal,
        target_type="team",
        target_id=row.id,
        name=row.name,
        previous_name=before,
        fields=changed,
    )
    out = serialize_team(row)
    await db.commit()
    return out


async def _apply_name_description(db, model, tenant_id, row, body, what: str) -> list[str]:
    sent = body.model_fields_set
    changed: list[str] = []
    if "name" in sent and body.name is not None and body.name != row.name:
        if await _name_taken(db, model, tenant_id, body.name, row.id):
            raise _problem(409, "Conflict", f"A {what} with this name already exists")
        row.name = body.name
        changed.append("name")
    if "description" in sent and body.description != row.description:
        row.description = body.description
        changed.append("description")
    if changed:
        row.updated_at = _now()
    return changed


@router.delete("/teams/{team_id}", status_code=204, summary="Delete a team")
async def delete_team(
    team_id: UUID, request: Request, principal: AdminPrincipal = _MEMBERS
) -> Response:
    """409 while the team has projects. Its memberships go with it."""
    db = request.state.db
    row = await _get(db, Team, principal.tenant_id, team_id, "Team")
    has_projects = (
        await db.execute(select(Project.id).where(Project.team_id == row.id))
    ).first()
    if has_projects is not None:
        raise _problem(409, "Conflict", "Delete or move the team's projects first")
    memberships = (
        (await db.execute(select(Membership).where(Membership.team_id == row.id)))
        .scalars()
        .all()
    )
    for m in memberships:
        _check_can_touch(principal, m)
        await db.delete(m)
    await db.flush()
    await db.delete(row)
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=204,
        action="team.delete",
        principal=principal,
        target_type="team",
        target_id=team_id,
        name=row.name,
        memberships_removed=[str(m.id) for m in memberships],
    )
    await db.commit()
    return Response(status_code=204)


# ──────────────────────────────────────────────────────────────────────
# Projects
# ──────────────────────────────────────────────────────────────────────


class CreateProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: UUID
    name: str = NAME
    description: StorableStr | None = Field(default=None, max_length=1000)


class UpdateProjectBody(UpdateTeamBody):
    """Fields left out are unchanged. A project stays in its team."""


@router.post("/projects", status_code=201, summary="Create a project in a team")
async def create_project(
    body: CreateProjectBody, request: Request, principal: AdminPrincipal = _MEMBERS
) -> dict:
    db = request.state.db
    team = await _get(db, Team, principal.tenant_id, body.team_id, "Team")
    if await _name_taken(db, Project, principal.tenant_id, body.name):
        raise _problem(409, "Conflict", "A project with this name already exists")
    row = Project(
        tenant_id=principal.tenant_id,
        team_id=team.id,
        name=body.name,
        description=body.description,
        created_at=_now(),
    )
    db.add(row)
    await db.flush()
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=201,
        action="project.create",
        principal=principal,
        target_type="project",
        target_id=row.id,
        name=row.name,
        team_id=str(team.id),
    )
    out = serialize_project(row)
    await db.commit()
    return out


async def _projects(db: AsyncSession, tenant_id: UUID, team_id: UUID | None) -> list[dict]:
    stmt = select(Project).where(Project.tenant_id == tenant_id)
    if team_id is not None:
        stmt = stmt.where(Project.team_id == team_id)
    rows = (await db.execute(stmt.order_by(Project.name, Project.id))).scalars().all()
    return [serialize_project(r) for r in rows]


@router.get("/projects", summary="List projects")
async def list_projects(
    request: Request, team_id: UUID | None = None, principal: AdminPrincipal = _MEMBERS
) -> dict:
    return {"data": await _projects(request.state.db, principal.tenant_id, team_id)}


@router.get("/projects/{project_id}", summary="Get a project")
async def get_project(
    project_id: UUID, request: Request, principal: AdminPrincipal = _MEMBERS
) -> dict:
    return serialize_project(
        await _get(request.state.db, Project, principal.tenant_id, project_id, "Project")
    )


@router.patch("/projects/{project_id}", summary="Rename or describe a project")
async def update_project(
    project_id: UUID,
    body: UpdateProjectBody,
    request: Request,
    principal: AdminPrincipal = _MEMBERS,
) -> dict:
    db = request.state.db
    row = await _get(db, Project, principal.tenant_id, project_id, "Project")
    before = row.name
    changed = await _apply_name_description(
        db, Project, principal.tenant_id, row, body, "project"
    )
    if not changed:
        return serialize_project(row)
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=200,
        action="project.update",
        principal=principal,
        target_type="project",
        target_id=row.id,
        name=row.name,
        previous_name=before,
        fields=changed,
    )
    out = serialize_project(row)
    await db.commit()
    return out


@router.delete("/projects/{project_id}", status_code=204, summary="Delete a project")
async def delete_project(
    project_id: UUID, request: Request, principal: AdminPrincipal = _MEMBERS
) -> Response:
    """Its memberships go with it."""
    db = request.state.db
    row = await _get(db, Project, principal.tenant_id, project_id, "Project")
    memberships = (
        (await db.execute(select(Membership).where(Membership.project_id == row.id)))
        .scalars()
        .all()
    )
    for m in memberships:
        _check_can_touch(principal, m)
        await db.delete(m)
    await db.flush()
    await db.delete(row)
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=204,
        action="project.delete",
        principal=principal,
        target_type="project",
        target_id=project_id,
        name=row.name,
        memberships_removed=[str(m.id) for m in memberships],
    )
    await db.commit()
    return Response(status_code=204)


# ──────────────────────────────────────────────────────────────────────
# Memberships
# ──────────────────────────────────────────────────────────────────────


class CreateMembershipBody(BaseModel):
    """Leave out team_id and project_id for a tenant-wide role (the only
    kind that grants scopes on the tenant-wide APIs); give a project_id for
    a project role (team_id is then taken from the project)."""

    model_config = ConfigDict(extra="forbid")

    principal_id: UUID
    role: Role
    team_id: UUID | None = None
    project_id: UUID | None = None


class UpdateMembershipBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role


def _place(m: Membership) -> dict:
    return {
        "principal_id": str(m.principal_id),
        "team_id": str(m.team_id) if m.team_id else None,
        "project_id": str(m.project_id) if m.project_id else None,
    }


@router.post("/memberships", status_code=201, summary="Grant a principal a role")
async def create_membership(
    body: CreateMembershipBody, request: Request, principal: AdminPrincipal = _MEMBERS
) -> dict:
    db = request.state.db
    tenant_id = principal.tenant_id
    _check_can_grant(principal, body.role)
    member = await _get(db, Principal, tenant_id, body.principal_id, "Principal")
    team_id = body.team_id
    if body.project_id is not None:
        project = await _get(db, Project, tenant_id, body.project_id, "Project")
        if team_id is not None and team_id != project.team_id:
            raise _problem(400, "Bad Request", "project_id is not in team_id")
        team_id = project.team_id
    elif team_id is not None:
        await _get(db, Team, tenant_id, team_id, "Team")
    dup = select(Membership.id).where(Membership.principal_id == member.id)
    if body.project_id is not None:
        dup = dup.where(Membership.project_id == body.project_id)
    elif team_id is not None:
        dup = dup.where(Membership.team_id == team_id, Membership.project_id.is_(None))
    else:
        dup = dup.where(Membership.team_id.is_(None))
    if (await db.execute(dup)).first() is not None:
        raise _problem(
            409,
            "Conflict",
            "The principal already has a role here; change it with PATCH",
        )
    row = Membership(
        tenant_id=tenant_id,
        principal_id=member.id,
        team_id=team_id,
        project_id=body.project_id,
        role=body.role,
        created_by=created_by(request, principal),
        created_at=_now(),
    )
    db.add(row)
    await db.flush()
    await add_audit(
        request,
        db,
        tenant_id=tenant_id,
        status_code=201,
        action="membership.add",
        principal=principal,
        target_type="membership",
        target_id=row.id,
        role=row.role,
        **_place(row),
    )
    out = serialize_membership(row)
    await db.commit()
    return out


@router.get("/memberships", summary="List memberships")
async def list_memberships(
    request: Request,
    principal_id: UUID | None = None,
    team_id: UUID | None = None,
    project_id: UUID | None = None,
    principal: AdminPrincipal = _MEMBERS,
) -> dict:
    stmt = select(Membership).where(Membership.tenant_id == principal.tenant_id)
    if principal_id is not None:
        stmt = stmt.where(Membership.principal_id == principal_id)
    if team_id is not None:
        stmt = stmt.where(Membership.team_id == team_id)
    if project_id is not None:
        stmt = stmt.where(Membership.project_id == project_id)
    rows = (
        (await request.state.db.execute(stmt.order_by(Membership.created_at, Membership.id)))
        .scalars()
        .all()
    )
    return {"data": [serialize_membership(r) for r in rows]}


@router.get("/memberships/{membership_id}", summary="Get a membership")
async def get_membership(
    membership_id: UUID, request: Request, principal: AdminPrincipal = _MEMBERS
) -> dict:
    return serialize_membership(
        await _get(request.state.db, Membership, principal.tenant_id, membership_id, "Membership")
    )


@router.patch("/memberships/{membership_id}", summary="Change a membership's role")
async def update_membership(
    membership_id: UUID,
    body: UpdateMembershipBody,
    request: Request,
    principal: AdminPrincipal = _MEMBERS,
) -> dict:
    db = request.state.db
    await lock_owners(db, principal.tenant_id)
    row = await _get(db, Membership, principal.tenant_id, membership_id, "Membership")
    if body.role == row.role:
        return serialize_membership(row)
    _check_can_touch(principal, row)
    _check_can_grant(principal, body.role)
    if row.role == rbac.OWNER and row.team_id is None:
        await _keep_an_owner(db, principal.tenant_id, row.principal_id)
    before = row.role
    row.role = body.role
    row.updated_at = _now()
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=200,
        action="role.change",
        principal=principal,
        target_type="membership",
        target_id=row.id,
        role=row.role,
        previous_role=before,
        **_place(row),
    )
    out = serialize_membership(row)
    await db.commit()
    return out


@router.delete(
    "/memberships/{membership_id}", status_code=204, summary="Remove a membership"
)
async def delete_membership(
    membership_id: UUID, request: Request, principal: AdminPrincipal = _MEMBERS
) -> Response:
    db = request.state.db
    await lock_owners(db, principal.tenant_id)
    row = await _get(db, Membership, principal.tenant_id, membership_id, "Membership")
    _check_can_touch(principal, row)
    if row.role == rbac.OWNER and row.team_id is None:
        await _keep_an_owner(db, principal.tenant_id, row.principal_id)
    await db.delete(row)
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=204,
        action="membership.remove",
        principal=principal,
        target_type="membership",
        target_id=membership_id,
        role=row.role,
        **_place(row),
    )
    await db.commit()
    return Response(status_code=204)


# ──────────────────────────────────────────────────────────────────────
# Partner API: /partner/v1/teams, /partner/v1/projects (members:read)
# ──────────────────────────────────────────────────────────────────────


@partner_router.get("/teams", summary="List the tenant's teams")
async def partner_list_teams(
    request: Request, response: Response, principal: PartnerPrincipal = _READ
) -> dict:
    await _enforce_rate_limit(request, response, principal)
    rows = (
        (
            await request.state.db.execute(
                select(Team)
                .where(Team.tenant_id == principal.tenant_id)
                .order_by(Team.name, Team.id)
            )
        )
        .scalars()
        .all()
    )
    data = [serialize_team(r) for r in rows]
    await _audit(request, principal, "/partner/v1/teams", "GET", 200, len(data))
    return {"data": data}


@partner_router.get("/projects", summary="List the tenant's projects")
async def partner_list_projects(
    request: Request,
    response: Response,
    team_id: UUID | None = Query(default=None, description="Only this team's projects"),  # noqa: B008 - FastAPI dependency marker, by design
    principal: PartnerPrincipal = _READ,
) -> dict:
    await _enforce_rate_limit(request, response, principal)
    data = await _projects(request.state.db, principal.tenant_id, team_id)
    await _audit(request, principal, "/partner/v1/projects", "GET", 200, len(data))
    return {"data": data}
