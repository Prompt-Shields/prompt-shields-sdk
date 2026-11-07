"""What identity provider changes do to roles and credentials (#339).

Shared by SCIM provisioning (collector/scim.py), admin API SSO
(collector/sso.py) and its configuration routes (collector/sso_admin.py).

Group roles
-----------
A SCIM Group is a team. Its members get a team membership (created by
``scim``) with the group's ``member_role`` (``idp_group_roles``, default
``viewer``). A group whose ``idp_group_roles.tenant_role`` is set also gives
its members that tenant-wide role. A principal has one tenant-wide
membership, so when several of its groups carry a tenant role the first in
TENANT_ROLE_RANK wins.

Sync only ever creates, changes or removes a tenant-wide membership that
sync created (``created_by = 'scim'``): one granted through the admin API
(an owner, say) is never touched. ``owner`` can never come from a group (a
CHECK constraint on ``idp_group_roles`` and the API both refuse it).

Deprovisioning
--------------
Disabling a principal already makes every admin token and partner
credential issued for it fail (collector/rbac.py, checked on every request).
``revoke_principal_sessions`` also revokes its SSO sessions for good, and
every token those tokens (or any other token issued for it) created, which
may be unbound and would otherwise outlive the principal's access until
they expire.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from db.models import AdminToken, IdpGroupRole, Membership
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from collector.admin_auth import revoke_descendants

# Membership.created_by for rows SCIM manages.
SCIM_SOURCE = "scim"
# AdminToken.created_by for SSO sessions.
SSO_CREATED_BY = "sso"
DEFAULT_MEMBER_ROLE = "viewer"
# Highest first. owner is never a group role.
TENANT_ROLE_RANK = ("admin", "developer", "billing", "auditor", "viewer")
GROUP_ROLES = TENANT_ROLE_RANK

Audit = Callable[..., Awaitable[None]]


async def member_role(db: AsyncSession, team_id: UUID) -> str:
    role = (
        await db.execute(select(IdpGroupRole.member_role).where(IdpGroupRole.team_id == team_id))
    ).scalar_one_or_none()
    return role or DEFAULT_MEMBER_ROLE


async def recompute_tenant_role(
    db: AsyncSession, tenant_id: UUID, principal_id: UUID, now: datetime, audit: Audit
) -> None:
    """Bring the principal's sync-managed tenant-wide membership in line
    with the tenant roles of the groups SCIM put it in."""
    candidates = set(
        (
            await db.execute(
                select(IdpGroupRole.tenant_role)
                .join(Membership, Membership.team_id == IdpGroupRole.team_id)
                .where(
                    Membership.tenant_id == tenant_id,
                    Membership.principal_id == principal_id,
                    Membership.project_id.is_(None),
                    Membership.created_by == SCIM_SOURCE,
                    IdpGroupRole.tenant_role.is_not(None),
                )
            )
        ).scalars()
    )
    best = next((r for r in TENANT_ROLE_RANK if r in candidates), None)
    existing = (
        await db.execute(
            select(Membership).where(
                Membership.tenant_id == tenant_id,
                Membership.principal_id == principal_id,
                Membership.team_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None and existing.created_by != SCIM_SOURCE:
        return  # granted through the admin API: never touched by sync
    if best is None:
        if existing is not None:
            await db.delete(existing)
            await db.flush()
            await audit(
                "membership.remove",
                "membership",
                existing.id,
                principal_id=str(principal_id),
                role=existing.role,
                source=SCIM_SOURCE,
            )
        return
    if existing is None:
        row = Membership(
            tenant_id=tenant_id,
            principal_id=principal_id,
            role=best,
            created_by=SCIM_SOURCE,
            created_at=now,
        )
        db.add(row)
        await db.flush()
        await audit(
            "membership.add",
            "membership",
            row.id,
            principal_id=str(principal_id),
            role=best,
            source=SCIM_SOURCE,
        )
    elif existing.role != best:
        before = existing.role
        existing.role = best
        existing.updated_at = now
        await audit(
            "role.change",
            "membership",
            existing.id,
            principal_id=str(principal_id),
            role=best,
            previous_role=before,
            source=SCIM_SOURCE,
        )


async def revoke_principal_sessions(
    db: AsyncSession, tenant_id: UUID, principal_id: UUID, now: datetime
) -> list[AdminToken]:
    """Revoke the principal's SSO sessions and every token created by a
    token issued for it. Returns the tokens revoked here. Flushes nothing."""
    tokens = (
        (
            await db.execute(
                select(AdminToken).where(
                    AdminToken.tenant_id == tenant_id, AdminToken.principal_id == principal_id
                )
            )
        )
        .scalars()
        .all()
    )
    revoked: list[AdminToken] = []
    for t in tokens:
        if t.created_by == SSO_CREATED_BY and t.revoked_at is None:
            t.revoked_at = now
            revoked.append(t)
        revoked.extend(await revoke_descendants(db, t, now))
    return revoked
