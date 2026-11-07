"""Built-in roles for principals, teams and projects (#338).

A role is a named set of existing scopes: admin scopes
(collector/admin_auth.py ``ADMIN_SCOPES``) for ``/api/v1/admin`` and
partner scopes (collector/partner_scopes.py ``SCOPES``) for
``/partner/v1``. Enforcement does not change: routes keep using
``require_admin`` and ``require_scope``. What changes is where a
credential's scopes come from.

  - An admin token or partner credential issued **for** a principal stores
    the scopes asked for intersected with the principal's role scopes at
    issue time, and on every request it reaches at most the intersection of
    its stored scopes with what the principal's roles grant **now**. Demoting
    or disabling the principal takes effect at once; promoting it does not
    widen tokens already issued.
  - Tokens and credentials issued for no principal (every one issued before
    roles existed, and automation) keep their stored scopes, unchanged.

Only tenant-wide memberships (``team_id`` NULL) grant scopes, because every
admin and partner route is tenant-wide today. Team and project memberships
are recorded (and listed) for team-scoped resources; they grant nothing on
tenant-wide routes, so a team-level ``admin`` is not a tenant admin.

``owner`` and ``admin`` hold the same scopes. What sets an owner apart: only
an owner (an admin token issued for a principal with the tenant-wide owner
role, or an unbound token holding every admin scope, such as the one the
operator bootstraps) may grant, change or remove an ``owner`` membership.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from db.models import Membership, Principal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from collector.partner_scopes import SCOPES as PARTNER_SCOPES

# Lists and reads the tenant's principals, teams, projects and memberships
# (/api/v1/admin/... GET with admin:read, or /partner/v1/teams|projects).
MEMBERS_READ_SCOPE = "members:read"

_ALL_ADMIN = (
    "admin:read",
    "admin:keys",
    "admin:tokens",
    "admin:provider-credentials",
    "admin:webhooks",
    "admin:partners",
    "admin:registry",
    "admin:privacy",
    "admin:members",
)

# Partner read scopes that expose no audit trail, call evidence or cost
# export: what a viewer may read.
_PARTNER_READS = (
    "assets:read",
    "data-flows:read",
    "risks:read",
    "usage:read",
    "changes:read",
    "export:read",
    "mcp:read",
    "learner:read",
    "spend-alerts:read",
    MEMBERS_READ_SCOPE,
)

# Role -> (description, scopes). Order is the order GET /roles shows.
ROLES: dict[str, tuple[str, frozenset[str]]] = {
    "owner": (
        "Everything an admin can do, and grant, change or remove the owner role",
        frozenset(_ALL_ADMIN) | frozenset(PARTNER_SCOPES),
    ),
    "admin": (
        "Every admin and partner scope; cannot touch owner memberships",
        frozenset(_ALL_ADMIN) | frozenset(PARTNER_SCOPES),
    ),
    "developer": (
        (
            "Read the registry, usage and call evidence (not the audit trail "
            "or cost export); manage webhooks, registry lifecycle, "
            "attestations and risk and learner reviews"
        ),
        frozenset(
            {
                "admin:read",
                "admin:webhooks",
                "admin:registry",
                *_PARTNER_READS,
                "evidence:read",
                "webhooks:manage",
                "attestations:write",
                "risks:review",
                "learner:review",
            }
        ),
    ),
    "viewer": (
        (
            "Read-only: GET on the admin API and the partner read scopes "
            "(no audit trail, call evidence or cost export); every write is 403"
        ),
        frozenset({"admin:read", *_PARTNER_READS}),
    ),
    "auditor": (
        "Read the audit trail (audit:read) and nothing else",
        frozenset({"audit:read"}),
    ),
    "billing": (
        (
            "Read usage and cost, export FOCUS cost data, read and acknowledge "
            "spend alerts"
        ),
        frozenset(
            {"usage:read", "usage:export", "spend-alerts:read", "spend-alerts:acknowledge"}
        ),
    ),
}

OWNER = "owner"
PRINCIPAL_KINDS = ("user", "service_account")


def role_scopes(roles: Iterable[str]) -> frozenset[str]:
    """Union of the scopes the named roles grant; unknown names grant nothing."""
    out: set[str] = set()
    for role in roles:
        if role in ROLES:
            out |= ROLES[role][1]
    return frozenset(out)


class PrincipalDisabled(Exception):
    """The principal a credential was issued for is disabled or gone."""


async def tenant_roles(
    db: AsyncSession, tenant_id: UUID, principal_id: UUID
) -> frozenset[str]:
    """The principal's tenant-wide roles. Raises PrincipalDisabled when the
    principal is disabled or does not exist in ``tenant_id`` (checked
    explicitly, not only by row-level security, which is opt-in)."""
    row = (
        await db.execute(
            select(Principal.disabled_at).where(
                Principal.id == principal_id, Principal.tenant_id == tenant_id
            )
        )
    ).one_or_none()
    if row is None or row.disabled_at is not None:
        raise PrincipalDisabled(str(principal_id))
    roles = (
        await db.execute(
            select(Membership.role).where(
                Membership.tenant_id == tenant_id,
                Membership.principal_id == principal_id,
                Membership.team_id.is_(None),
            )
        )
    ).scalars()
    return frozenset(roles)


async def is_tenant_owner(
    db: AsyncSession, tenant_id: UUID, principal_id: UUID | None
) -> bool:
    """Whether ``principal_id`` holds the tenant-wide owner role (disabled
    or not: a re-enabled owner's credentials work again)."""
    if principal_id is None:
        return False
    return (
        await db.execute(
            select(Membership.id).where(
                Membership.tenant_id == tenant_id,
                Membership.principal_id == principal_id,
                Membership.team_id.is_(None),
                Membership.role == OWNER,
            )
        )
    ).first() is not None


async def granted_scopes(
    db: AsyncSession, tenant_id: UUID, principal_id: UUID
) -> frozenset[str]:
    """What the principal's tenant-wide roles grant now. Raises
    PrincipalDisabled like ``tenant_roles``."""
    return role_scopes(await tenant_roles(db, tenant_id, principal_id))


def ordered(scopes: Iterable[str], order: Iterable[str]) -> list[str]:
    """``scopes`` in the order of ``order`` (a scope registry's keys)."""
    want = set(scopes)
    return [s for s in order if s in want]
