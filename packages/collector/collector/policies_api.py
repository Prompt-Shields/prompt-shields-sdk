"""Central governance policies: admin API and the gateway pull (#340).

Tenant admin routes, mounted at /api/v1/admin (auth: collector/admin_auth.py).
Every write needs ``admin:policies``; GETs also take ``admin:read``.

  POST   /policies                          create (tenant, team or project
                                            binding), optionally with version 1
  GET    /policies                          list (?scope=, ?include_deleted=)
  GET    /policies/{id}                     read, with the active document
                                            (also a deleted policy)
  PATCH  /policies/{id}                     rename / describe
  DELETE /policies/{id}                     delete (soft: the policy and its
                                            versions are kept, #383)
  POST   /policies/{id}/versions            add an immutable version
                                            (optionally activate it)
  GET    /policies/{id}/versions            list versions (no documents)
  GET    /policies/{id}/versions/{version}  one version, with its document
  POST   /policies/{id}/activate            make a version active (or none:
                                            ``{"version": null}``); rollback
                                            is activating an older version
  GET    /policy/effective                  the compiled bundle, as the
                                            gateway sees it

Gateway pull, mounted at /api/v1 (tenant ingest key with ``policy:read``):

  GET    /policy/effective                  the compiled bundle with an
                                            ETag; If-None-Match -> 304

The document format, inheritance and the bundle are described in
collector/policy_doc.py. Versions are immutable (no route changes one, and a
database trigger refuses UPDATE). Every change writes a hash-chained audit
trail row in the same transaction: ``policy.create``, ``policy.update``,
``policy.delete``, ``policy.version_create``, ``policy.activate``; with the
version numbers and document SHA-256 before and after, and the sections
that changed. Documents themselves stay in ``policy_versions``, where the
audited digests point.

Deleting a policy (#383) marks it deleted (``deleted_at``) instead of
removing it: it leaves listings and the bundle, its name and binding are
free again, and it and every version stay readable (GET by id, versions) so
the history the audit rows point to cannot be erased outside tenant
erasure, which is the only DELETE the database allows on these tables. A
deleted policy cannot be changed (409).

Moving a project to another team (collector/rbac_admin.py) can change the
project's effective policy; ``audit_project_move`` then writes a
``policy.effective_change`` row with the effective version and digest
before and after.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from db.models import Policy, PolicyVersion, Project, Team
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from collector import policy_doc, tenant_keys
from collector.admin_auth import AdminPrincipal, add_audit, created_by, require_admin
from collector.auth import resolve_tenant
from collector.sanitize import BoundedJsonRoute, StorableStr

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"], route_class=BoundedJsonRoute)
gateway_router = APIRouter(prefix="/api/v1", tags=["Policy"])

_POLICIES = Depends(require_admin("admin:policies"))

Scope = Literal["tenant", "team", "project"]
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


def _problem(status: int, title: str, detail: Any) -> HTTPException:
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


def serialize_policy(p: Policy) -> dict:
    return {
        "id": str(p.id),
        "name": p.name,
        "description": p.description,
        "scope": p.scope,
        "team_id": str(p.team_id) if p.team_id else None,
        "project_id": str(p.project_id) if p.project_id else None,
        "active_version": p.active_version,
        "created_by": p.created_by,
        "created_at": _iso(p.created_at),
        "updated_at": _iso(p.updated_at),
        "deleted_at": _iso(p.deleted_at),
    }


def serialize_version_summary(v: PolicyVersion) -> dict:
    return {
        "policy_id": str(v.policy_id),
        "version": v.version,
        "sha256": v.sha256,
        "sections": policy_doc.section_summary(v.document),
        "comment": v.comment,
        "created_by": v.created_by,
        "created_at": _iso(v.created_at),
    }


def serialize_version(v: PolicyVersion) -> dict:
    return {**serialize_version_summary(v), "document": v.document}


def _ref(v: PolicyVersion | None) -> dict:
    """A version as audit details: number and digest, never the document."""
    return {"version": v.version, "sha256": v.sha256} if v else {"version": None}


def _sections_changed(before: PolicyVersion | None, after: PolicyVersion | None) -> list[str]:
    b = ((before.document if before else {}) or {}).get("sections") or {}
    a = ((after.document if after else {}) or {}).get("sections") or {}
    return sorted(
        k for k in set(a) | set(b) if policy_doc.digest(a.get(k)) != policy_doc.digest(b.get(k))
    )


# ──────────────────────────────────────────────────────────────────────
# Shared lookups
# ──────────────────────────────────────────────────────────────────────


async def _policy(
    db: AsyncSession,
    tenant_id: UUID,
    policy_id: UUID,
    *,
    lock: bool = False,
    write: bool = False,
) -> Policy:
    """The policy, deleted ones included for reads; ``write`` refuses a
    deleted policy with 409 (it is kept for history, not to be changed)."""
    stmt = select(Policy).where(Policy.id == policy_id, Policy.tenant_id == tenant_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise _problem(404, "Not Found", "Policy not found")
    if write and row.deleted_at is not None:
        raise _problem(409, "Conflict", "This policy was deleted; it is kept for its history only")
    return row


_LIVE = Policy.deleted_at.is_(None)


async def _version(
    db: AsyncSession, policy: Policy, version: int | None
) -> PolicyVersion | None:
    if version is None:
        return None
    return (
        await db.execute(
            select(PolicyVersion).where(
                PolicyVersion.policy_id == policy.id, PolicyVersion.version == version
            )
        )
    ).scalar_one_or_none()


def _validated(document: dict) -> dict:
    try:
        return policy_doc.validate_document(document)
    except policy_doc.PolicyDocumentError as e:
        raise _problem(422, "Unprocessable Entity", e.problems) from None


async def _add_version(
    request: Request,
    db: AsyncSession,
    principal: AdminPrincipal,
    policy: Policy,
    document: dict,
    comment: str | None,
) -> PolicyVersion:
    current = (
        await db.execute(
            select(func.max(PolicyVersion.version)).where(PolicyVersion.policy_id == policy.id)
        )
    ).scalar_one()
    row = PolicyVersion(
        tenant_id=principal.tenant_id,
        policy_id=policy.id,
        version=(current or 0) + 1,
        document=document,
        sha256=policy_doc.digest(document),
        comment=comment,
        created_by=created_by(request, principal),
        created_at=_now(),
    )
    db.add(row)
    await db.flush()
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=201,
        action="policy.version_create",
        principal=principal,
        target_type="policy",
        target_id=policy.id,
        name=policy.name,
        version=row.version,
        sha256=row.sha256,
        sections=policy_doc.section_summary(document),
    )
    return row


async def _activate(
    request: Request,
    db: AsyncSession,
    principal: AdminPrincipal,
    policy: Policy,
    target: PolicyVersion | None,
    status_code: int,
) -> None:
    before = await _version(db, policy, policy.active_version)
    if (before.version if before else None) == (target.version if target else None):
        return
    policy.active_version = target.version if target else None
    policy.updated_at = _now()
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=status_code,
        action="policy.activate",
        principal=principal,
        target_type="policy",
        target_id=policy.id,
        name=policy.name,
        scope=policy.scope,
        before=_ref(before),
        after=_ref(target),
        sections_changed=_sections_changed(before, target),
    )


# ──────────────────────────────────────────────────────────────────────
# Admin routes
# ──────────────────────────────────────────────────────────────────────


class CreatePolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=100, pattern=NAME_PATTERN)
    description: StorableStr | None = Field(default=None, max_length=1000)
    scope: Scope
    team_id: UUID | None = None
    project_id: UUID | None = None
    # Version 1's ps-policy.v1 document; left out, the policy starts empty.
    document: dict | None = None
    comment: StorableStr | None = Field(default=None, max_length=1000)
    # Make version 1 active at once (needs a document).
    activate: bool = False

    @model_validator(mode="after")
    def _binding(self):
        want = {"tenant": (False, False), "team": (True, False), "project": (False, True)}
        if (self.team_id is not None, self.project_id is not None) != want[self.scope]:
            raise ValueError(
                "scope tenant takes neither team_id nor project_id; scope team takes "
                "team_id only; scope project takes project_id only"
            )
        if self.activate and self.document is None:
            raise ValueError("activate needs a document")
        return self


@router.post("/policies", status_code=201, summary="Create a policy")
async def create_policy(
    body: CreatePolicyBody, request: Request, principal: AdminPrincipal = _POLICIES
) -> dict:
    db = request.state.db
    tid = principal.tenant_id
    document = _validated(body.document) if body.document is not None else None
    # FOR SHARE on the team or project: a concurrent team/project delete
    # (which takes FOR UPDATE, rbac_admin.py) waits for this policy and then
    # refuses with 409, or ran first and the team is gone here (404),
    # instead of failing on the foreign key with a 500 (#383).
    if body.team_id is not None:
        team = (
            await db.execute(
                select(Team.id)
                .where(Team.id == body.team_id, Team.tenant_id == tid)
                .with_for_update(read=True)
            )
        ).first()
        if team is None:
            raise _problem(404, "Not Found", "Team not found")
    if body.project_id is not None:
        project = (
            await db.execute(
                select(Project.id)
                .where(Project.id == body.project_id, Project.tenant_id == tid)
                .with_for_update(read=True)
            )
        ).first()
        if project is None:
            raise _problem(404, "Not Found", "Project not found")
    clash = (
        await db.execute(
            select(Policy.name, Policy.scope, Policy.team_id, Policy.project_id).where(
                Policy.tenant_id == tid, _LIVE
            )
        )
    ).all()
    for c in clash:
        if c.name == body.name:
            raise _problem(409, "Conflict", "A policy with this name already exists")
        if (c.scope, c.team_id, c.project_id) == (body.scope, body.team_id, body.project_id):
            raise _problem(
                409, "Conflict", f"This {body.scope} already has a policy ({c.name}); version it"
            )
    row = Policy(
        tenant_id=tid,
        name=body.name,
        description=body.description,
        scope=body.scope,
        team_id=body.team_id,
        project_id=body.project_id,
        created_by=created_by(request, principal),
        created_at=_now(),
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise _problem(
            409,
            "Conflict",
            "A policy with this name or binding exists, or its team or project was just deleted",
        ) from None
    await add_audit(
        request,
        db,
        tenant_id=tid,
        status_code=201,
        action="policy.create",
        principal=principal,
        target_type="policy",
        target_id=row.id,
        name=row.name,
        scope=row.scope,
        team_id=row.team_id,
        project_id=row.project_id,
    )
    if document is not None:
        version = await _add_version(request, db, principal, row, document, body.comment)
        if body.activate:
            await _activate(request, db, principal, row, version, 201)
    out = serialize_policy(row)
    await db.commit()
    return out


@router.get("/policies", summary="List policies")
async def list_policies(
    request: Request,
    principal: AdminPrincipal = _POLICIES,
    scope: Scope | None = None,
    include_deleted: bool = False,
) -> dict:
    """Live policies; ``include_deleted=true`` adds deleted ones (with
    ``deleted_at`` set), whose versions stay readable."""
    stmt = select(Policy).where(Policy.tenant_id == principal.tenant_id)
    if not include_deleted:
        stmt = stmt.where(_LIVE)
    if scope is not None:
        stmt = stmt.where(Policy.scope == scope)
    rows = (await request.state.db.execute(stmt.order_by(Policy.name, Policy.id))).scalars().all()
    return {"data": [serialize_policy(r) for r in rows]}


@router.get("/policies/{policy_id}", summary="Get a policy")
async def get_policy(
    policy_id: UUID, request: Request, principal: AdminPrincipal = _POLICIES
) -> dict:
    db = request.state.db
    row = await _policy(db, principal.tenant_id, policy_id)
    active = await _version(db, row, row.active_version)
    return {**serialize_policy(row), "active_document": active.document if active else None}


class UpdatePolicyBody(BaseModel):
    """Fields left out are unchanged. The binding cannot change: create a
    new policy instead."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100, pattern=NAME_PATTERN)
    description: StorableStr | None = Field(default=None, max_length=1000)


@router.patch("/policies/{policy_id}", summary="Rename or describe a policy")
async def update_policy(
    policy_id: UUID,
    body: UpdatePolicyBody,
    request: Request,
    principal: AdminPrincipal = _POLICIES,
) -> dict:
    db = request.state.db
    row = await _policy(db, principal.tenant_id, policy_id, lock=True, write=True)
    sent = body.model_fields_set
    before = {"name": row.name}
    changed = []
    if "name" in sent and body.name and body.name != row.name:
        taken = (
            await db.execute(
                select(Policy.id).where(
                    Policy.tenant_id == principal.tenant_id,
                    Policy.name == body.name,
                    Policy.id != row.id,
                    _LIVE,
                )
            )
        ).first()
        if taken is not None:
            raise _problem(409, "Conflict", "A policy with this name already exists")
        row.name = body.name
        changed.append("name")
    if "description" in sent and body.description != row.description:
        row.description = body.description
        changed.append("description")
    if not changed:
        return serialize_policy(row)
    row.updated_at = _now()
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=200,
        action="policy.update",
        principal=principal,
        target_type="policy",
        target_id=row.id,
        fields=changed,
        before=before,
        after={"name": row.name},
    )
    out = serialize_policy(row)
    await db.commit()
    return out


@router.delete("/policies/{policy_id}", status_code=204, summary="Delete a policy")
async def delete_policy(
    policy_id: UUID, request: Request, principal: AdminPrincipal = _POLICIES
) -> Response:
    """Marks the policy deleted (#383): gateways stop applying it at their
    next pull, its name and binding are free again, and it and every
    version are kept (readable by id) until tenant erasure."""
    db = request.state.db
    row = await _policy(db, principal.tenant_id, policy_id, lock=True, write=True)
    active = await _version(db, row, row.active_version)
    versions = (
        await db.execute(
            select(func.count()).select_from(PolicyVersion).where(PolicyVersion.policy_id == row.id)
        )
    ).scalar_one()
    now = _now()
    row.deleted_at = now
    row.updated_at = now
    row.active_version = None
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=204,
        action="policy.delete",
        principal=principal,
        target_type="policy",
        target_id=policy_id,
        name=row.name,
        scope=row.scope,
        team_id=row.team_id,
        project_id=row.project_id,
        versions_kept=versions,
        before=_ref(active),
        after={"version": None},
        sections_changed=_sections_changed(active, None),
    )
    await db.commit()
    return Response(status_code=204)


class CreateVersionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: dict
    comment: StorableStr | None = Field(default=None, max_length=1000)
    activate: bool = False


@router.post("/policies/{policy_id}/versions", status_code=201, summary="Add a policy version")
async def create_version(
    policy_id: UUID,
    body: CreateVersionBody,
    request: Request,
    principal: AdminPrincipal = _POLICIES,
) -> dict:
    db = request.state.db
    document = _validated(body.document)
    # The row lock numbers concurrent versions one after another.
    row = await _policy(db, principal.tenant_id, policy_id, lock=True, write=True)
    version = await _add_version(request, db, principal, row, document, body.comment)
    if body.activate:
        await _activate(request, db, principal, row, version, 201)
    out = serialize_version_summary(version)
    await db.commit()
    return out


@router.get("/policies/{policy_id}/versions", summary="List a policy's versions")
async def list_versions(
    policy_id: UUID, request: Request, principal: AdminPrincipal = _POLICIES
) -> dict:
    db = request.state.db
    row = await _policy(db, principal.tenant_id, policy_id)
    rows = (
        (
            await db.execute(
                select(PolicyVersion)
                .where(PolicyVersion.policy_id == row.id)
                .order_by(PolicyVersion.version.desc())
            )
        )
        .scalars()
        .all()
    )
    return {"data": [serialize_version_summary(v) for v in rows]}


@router.get("/policies/{policy_id}/versions/{version}", summary="Get a policy version")
async def get_version(
    policy_id: UUID, version: int, request: Request, principal: AdminPrincipal = _POLICIES
) -> dict:
    db = request.state.db
    row = await _policy(db, principal.tenant_id, policy_id)
    v = await _version(db, row, version)
    if v is None:
        raise _problem(404, "Not Found", "Policy version not found")
    return serialize_version(v)


class ActivateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # null deactivates the policy (its level falls back to the one above).
    version: int | None = Field(..., ge=1)


@router.post("/policies/{policy_id}/activate", summary="Activate a policy version")
async def activate_version(
    policy_id: UUID,
    body: ActivateBody,
    request: Request,
    principal: AdminPrincipal = _POLICIES,
) -> dict:
    db = request.state.db
    row = await _policy(db, principal.tenant_id, policy_id, lock=True, write=True)
    target = await _version(db, row, body.version)
    if body.version is not None and target is None:
        raise _problem(404, "Not Found", "Policy version not found")
    await _activate(request, db, principal, row, target, 200)
    out = serialize_policy(row)
    await db.commit()
    return out


# ──────────────────────────────────────────────────────────────────────
# Effective policy
# ──────────────────────────────────────────────────────────────────────


async def effective_bundle(db: AsyncSession, tenant_id: UUID) -> dict:
    """Every effective policy of the tenant (policy_doc.build_bundle)."""
    active = (
        await db.execute(
            select(
                Policy.scope,
                Policy.team_id,
                Policy.project_id,
                Policy.name,
                PolicyVersion.version,
                PolicyVersion.document,
            )
            .join(
                PolicyVersion,
                and_(
                    PolicyVersion.policy_id == Policy.id,
                    PolicyVersion.version == Policy.active_version,
                ),
            )
            .where(Policy.tenant_id == tenant_id, _LIVE)
        )
    ).all()
    tenant = None
    by_team: dict[str, policy_doc.ActivePolicy] = {}
    by_project: dict[str, policy_doc.ActivePolicy] = {}
    for r in active:
        p = policy_doc.ActivePolicy(r.name, r.version, (r.document or {}).get("sections") or {})
        if r.scope == "tenant":
            tenant = p
        elif r.scope == "team":
            by_team[str(r.team_id)] = p
        else:
            by_project[str(r.project_id)] = p
    teams = {
        str(t.id): (t.name, by_team.get(str(t.id)))
        for t in (
            await db.execute(select(Team.id, Team.name).where(Team.tenant_id == tenant_id))
        ).all()
    }
    projects = {
        str(p.id): (p.name, str(p.team_id), by_project.get(str(p.id)))
        for p in (
            await db.execute(
                select(Project.id, Project.name, Project.team_id).where(
                    Project.tenant_id == tenant_id
                )
            )
        ).all()
    }
    return policy_doc.build_bundle(tenant, teams, projects)


async def project_effective(
    db: AsyncSession, tenant_id: UUID, project_id: UUID, team_id: UUID | None
) -> dict:
    """The effective policy (policy_doc.compile_effective) a project has
    under ``team_id``: tenant, then that team's, then its own active policy."""
    rows = (
        await db.execute(
            select(Policy.scope, Policy.name, PolicyVersion.version, PolicyVersion.document)
            .join(
                PolicyVersion,
                and_(
                    PolicyVersion.policy_id == Policy.id,
                    PolicyVersion.version == Policy.active_version,
                ),
            )
            .where(
                Policy.tenant_id == tenant_id,
                _LIVE,
                or_(
                    Policy.scope == "tenant",
                    and_(Policy.scope == "team", Policy.team_id == team_id),
                    and_(Policy.scope == "project", Policy.project_id == project_id),
                ),
            )
        )
    ).all()
    layers = {
        r.scope: policy_doc.ActivePolicy(
            r.name, r.version, (r.document or {}).get("sections") or {}
        )
        for r in rows
    }
    return policy_doc.compile_effective(
        [layers.get("tenant"), layers.get("team"), layers.get("project")]
    )


def _effective_ref(effective: dict) -> dict:
    return {"version": effective["version"], "sha256": policy_doc.digest(effective["sections"])}


async def audit_project_move(
    request: Request,
    db: AsyncSession,
    principal: AdminPrincipal,
    *,
    project_id: UUID,
    project_name: str,
    before: dict,
    after: dict,
    previous_team_id: UUID,
    team_id: UUID,
) -> None:
    """A ``policy.effective_change`` audit row when moving a project from
    one team to another changed its effective policy (``before`` and
    ``after`` from ``project_effective``); nothing when it did not."""
    if policy_doc.digest(before) == policy_doc.digest(after):
        return
    b, a = before["sections"], after["sections"]
    await add_audit(
        request,
        db,
        tenant_id=principal.tenant_id,
        status_code=200,
        action="policy.effective_change",
        principal=principal,
        target_type="project",
        target_id=project_id,
        name=project_name,
        cause="project.move",
        previous_team_id=str(previous_team_id),
        team_id=str(team_id),
        before=_effective_ref(before),
        after=_effective_ref(after),
        sections_changed=sorted(
            k for k in set(a) | set(b) if policy_doc.digest(a.get(k)) != policy_doc.digest(b.get(k))
        ),
    )


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    for tag in header.split(","):
        tag = tag.strip()
        if tag == "*" or tag.removeprefix("W/") == etag:
            return True
    return False


def _bundle_response(request: Request, bundle: dict) -> Response:
    etag = f'"{policy_doc.digest(bundle)}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(
        content=policy_doc.canonical_json(bundle),
        media_type="application/json",
        headers=headers,
    )


@router.get("/policy/effective", summary="Preview the compiled policy bundle")
async def admin_effective(request: Request, principal: AdminPrincipal = _POLICIES) -> Response:
    """Exactly what GET /api/v1/policy/effective returns to a gateway."""
    return _bundle_response(request, await effective_bundle(request.state.db, principal.tenant_id))


@gateway_router.get("/policy/effective", summary="Compiled policy bundle (gateway pull)")
async def gateway_effective(request: Request) -> Response:
    """The tenant's compiled policies, for gateways to poll. Needs an ingest
    key holding ``policy:read``. Send the last ETag as If-None-Match to get
    304 when nothing changed."""
    tenant_id = await resolve_tenant(request, tenant_keys.POLICY_READ)
    return _bundle_response(request, await effective_bundle(request.state.db, tenant_id))
