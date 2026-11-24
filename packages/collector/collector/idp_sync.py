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
(an owner, say) is never touched, and neither is any tenant-wide owner
membership. ``owner`` can never come from a group (a
CHECK constraint on ``idp_group_roles`` and the API both refuse it).

Deprovisioning
--------------
Disabling a principal already makes every admin token and partner
credential issued for it fail (collector/rbac.py, checked on every request),
and re-enabling it brings them back. What those tokens created may be
unbound or bound to someone else, and would otherwise outlive the
principal's access until it expires. So disabling or deleting a principal,
through SCIM or the admin API (collector/rbac_admin.py), calls
``revoke_created_credentials``, which revokes for good:

  - its SSO sessions (admin tokens with ``created_by = 'sso'``);
  - every admin token, tenant ingest key, SCIM token and partner credential
    created by a token issued for it, and everything those created in turn
    (``created_by = token:<prefix>`` or, for a full-scope ingest key while
    PS_ADMIN_REQUIRE_SCOPED_TOKENS is off, ``key:<prefix>``), walking
    through already revoked credentials too.

Through the admin API the owner rules apply (#374, #382): a caller that
cannot manage owners leaves live an admin token that is owner-bound or
owner-capable (unbound, every admin scope), with everything below it, and
a partner credential issued for an owner; the audit row lists them
(``cascade_skipped``, ``partner_credentials_skipped``) and an owner can
revoke them. SCIM deactivation skips nothing (the identity provider is the
authority on who has left). Ingest keys and partner credentials issued
before migration 056 have no recorded creator and are not reached; nor is
anything created by an ingest key under a lookup prefix it has since
rotated away from.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from db.models import (
    AdminToken,
    IdpGroupRole,
    Membership,
    PartnerCredential,
    ScimToken,
    TenantApiKey,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from collector import auth_cache, rbac, tenant_keys
from collector.admin_auth import ADMIN_SCOPES, AdminPrincipal, can_manage_owner

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
    if existing is not None and (existing.created_by != SCIM_SOURCE or existing.role == rbac.OWNER):
        # Granted through the admin API: never touched by sync. Nor is an
        # owner: a membership sync created and an owner later raised to
        # owner through the admin API (PATCH keeps created_by) is out of
        # sync's reach, or removing someone from a group would demote or
        # drop an owner, the last one included.
        return
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


@dataclass
class Revoked:
    """What ``revoke_created_credentials`` revoked, and what it left live."""

    tokens: list[AdminToken] = field(default_factory=list)
    api_keys: list[TenantApiKey] = field(default_factory=list)
    scim_tokens: list[ScimToken] = field(default_factory=list)
    partner_credentials: list[PartnerCredential] = field(default_factory=list)
    skipped_tokens: list[AdminToken] = field(default_factory=list)
    skipped_partner_credentials: list[PartnerCredential] = field(default_factory=list)

    def audit_details(self) -> dict:
        """Ids only. Key names end in ``_ids`` or avoid secret markers, so
        db.audit_chain.sanitize_details keeps them."""
        out = {
            "revoked_token_ids": sorted(str(t.id) for t in self.tokens),
            "revoked_api_key_ids": sorted(str(k.id) for k in self.api_keys),
            "revoked_scim_token_ids": sorted(str(t.id) for t in self.scim_tokens),
            "partner_credentials_revoked": sorted(
                str(c.id) for c in self.partner_credentials
            ),
        }
        if self.skipped_tokens:
            out["cascade_skipped"] = sorted(str(t.id) for t in self.skipped_tokens)
        if self.skipped_partner_credentials:
            out["partner_credentials_skipped"] = sorted(
                str(c.id) for c in self.skipped_partner_credentials
            )
        return out


async def revoke_created_credentials(
    db: AsyncSession,
    tenant_id: UUID,
    principal_id: UUID,
    now: datetime,
    caller: AdminPrincipal | None = None,
) -> Revoked:
    """Revoke the principal's SSO sessions and every credential created,
    directly or not, by a token issued for it (module doc). With an admin
    API ``caller`` that cannot manage owners, owner-bound or owner-capable
    tokens (and everything below them) and owner-bound partner credentials
    stay live and are returned as skipped. ``caller`` None (SCIM) skips
    nothing. Leaves the principal's own tokens and partner credentials
    alone apart from SSO sessions. Flushes nothing; the caller audits and
    commits."""
    out = Revoked()
    owner_rules = caller is not None and not can_manage_owner(caller)
    owners: dict[UUID | None, bool] = {}

    async def owner_bound(pid: UUID | None) -> bool:
        if pid not in owners:
            owners[pid] = await rbac.is_tenant_owner(db, tenant_id, pid)
        return owners[pid]

    roots = (
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
    seen: set[UUID] = set()
    frontier: set[str] = set()
    for t in roots:
        seen.add(t.id)
        frontier.add(f"token:{t.token_prefix}")
        if t.created_by == SSO_CREATED_BY and t.revoked_at is None:
            t.revoked_at = now
            out.tokens.append(t)

    def created_by_frontier(model):
        return select(model).where(
            model.tenant_id == tenant_id, model.created_by.in_(sorted(frontier))
        )

    visited: set[str] = set()
    while frontier:
        visited |= frontier
        nxt: set[str] = set()
        for t in (await db.execute(created_by_frontier(AdminToken))).scalars().all():
            if t.id in seen:
                continue
            seen.add(t.id)
            if (
                t.revoked_at is None
                and owner_rules
                and (
                    await owner_bound(t.principal_id)
                    or (t.principal_id is None and set(ADMIN_SCOPES) <= set(t.scopes or ()))
                )
            ):
                # Needs an owner to revoke (admin_api._revoke_refusal): it
                # stays live, with everything below it.
                out.skipped_tokens.append(t)
                continue
            nxt.add(f"token:{t.token_prefix}")
            if t.revoked_at is None:
                t.revoked_at = now
                out.tokens.append(t)
        for k in (await db.execute(created_by_frontier(TenantApiKey))).scalars().all():
            if k.id in seen:
                continue
            seen.add(k.id)
            # A full-scope key reaches admin routes while
            # PS_ADMIN_REQUIRE_SCOPED_TOKENS is off, and is never
            # owner-capable (admin_auth.TENANT_KEY_SCOPES lacks
            # admin:members), so no owner rule.
            nxt.add(f"key:{k.key_prefix}")
            if k.previous_key_prefix:
                nxt.add(f"key:{k.previous_key_prefix}")
            if tenant_keys.revoke_key(k):
                k.revoked_at = now
                auth_cache.get_key_cache().invalidate_key(k.id)
                out.api_keys.append(k)
        for st in (await db.execute(created_by_frontier(ScimToken))).scalars().all():
            if st.revoked_at is None:
                st.revoked_at = now
                out.scim_tokens.append(st)
        for c in (await db.execute(created_by_frontier(PartnerCredential))).scalars().all():
            if c.id in seen:
                continue
            seen.add(c.id)
            if c.is_active == "false":
                continue
            if owner_rules and await owner_bound(c.principal_id):
                out.skipped_partner_credentials.append(c)
                continue
            c.is_active = "false"
            out.partner_credentials.append(c)
        frontier = nxt - visited
    return out
