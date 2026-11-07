"""Central governance policies (#340)

Adds two tenant tables:

  - ``policies``: a named policy bound to the tenant (scope ``tenant``), a
    team or a project, with the ``active_version`` in force (NULL: inactive).
    One policy per binding.
  - ``policy_versions``: immutable ps-policy.v1 documents, numbered 1, 2, ...
    per policy, with their SHA-256. A trigger refuses UPDATE.

Each gets the tenant_isolation policy (same SQL as 015) and, if RLS is
already enforced on ai_assets, enforcement too (same as 020).

Scopes:

  - the admin scope ``admin:policies`` goes to every admin token holding all
    nine admin scopes that existed before it (as 038/039 did), so nothing
    that was "full" loses ground;
  - the ingest key scope ``policy:read`` (the gateway's policy pull,
    GET /api/v1/policy/effective) is NOT granted to existing keys: ingest
    keys also live in SDK clients, and the compiled policy names the
    tenant's teams, projects and identity-provider groups, so the gateway's
    key is issued with it explicitly. A key stays "full" with the three
    scopes from before (collector.tenant_keys.ALL_KEY_SCOPES).

Downgrade removes both scopes and the tables.

Follows 046 (cost attribution, #347), main's head.

Revision ID: 043
Revises: 046
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "043"
down_revision: str | None = "046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
TABLES = ("policies", "policy_versions")

NEW_ADMIN_SCOPE = "admin:policies"
# Every admin scope before admin:policies (frozen here, not imported).
PREVIOUS_ADMIN_SCOPES = (
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
# Removed from every key on downgrade; never granted on upgrade.
NEW_KEY_SCOPE = "policy:read"

IMMUTABLE_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION policy_versions_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'policy_versions rows are immutable; create a new version instead';
END;
$$ LANGUAGE plpgsql
"""
IMMUTABLE_TRIGGER_SQL = """
CREATE TRIGGER policy_versions_no_update
BEFORE UPDATE ON policy_versions
FOR EACH ROW EXECUTE FUNCTION policy_versions_immutable()
"""


def _expr(column: str) -> str:
    return (
        "(coalesce(current_setting('app.rls_bypass', true), '') <> '' "
        f"OR {column} = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )


def _bypass() -> None:
    # admin_tokens and tenant_api_keys may be under FORCE ROW LEVEL SECURITY.
    op.execute("SELECT set_config('app.rls_bypass', 'migration-043', true)")


def _uuid(name: str, *fk, nullable: bool = False, **kw) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *fk, nullable=nullable, **kw)


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.TIMESTAMP(timezone=True), nullable=nullable)


def _grant(table: str, scope: str, previous: tuple[str, ...]) -> None:
    held = ",".join(previous)
    op.execute(
        f"UPDATE {table} SET scopes = array_append(scopes, '{scope}') "
        f"WHERE scopes @> '{{{held}}}'::text[] AND NOT ('{scope}' = ANY(scopes))"
    )


def upgrade() -> None:
    op.create_table(
        "policies",
        _uuid("id", primary_key=True),
        _uuid("tenant_id", sa.ForeignKey("tenants.id")),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("scope", sa.Text, nullable=False),
        _uuid("team_id", sa.ForeignKey("teams.id"), nullable=True),
        _uuid("project_id", sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("active_version", sa.Integer),
        sa.Column("created_by", sa.Text, nullable=False),
        _ts("created_at", nullable=False),
        _ts("updated_at"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_policies_tenant_name"),
        sa.CheckConstraint(
            "(scope = 'tenant' AND team_id IS NULL AND project_id IS NULL) OR "
            "(scope = 'team' AND team_id IS NOT NULL AND project_id IS NULL) OR "
            "(scope = 'project' AND project_id IS NOT NULL AND team_id IS NULL)",
            name="ck_policies_scope_binding",
        ),
    )
    op.create_index(
        "ux_policies_tenant_wide",
        "policies",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("scope = 'tenant'"),
    )
    op.create_index(
        "ux_policies_team",
        "policies",
        ["team_id"],
        unique=True,
        postgresql_where=sa.text("team_id IS NOT NULL"),
    )
    op.create_index(
        "ux_policies_project",
        "policies",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
    )

    op.create_table(
        "policy_versions",
        _uuid("id", primary_key=True),
        _uuid("tenant_id", sa.ForeignKey("tenants.id")),
        _uuid("policy_id", sa.ForeignKey("policies.id", ondelete="CASCADE")),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("document", postgresql.JSONB, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("comment", sa.Text),
        sa.Column("created_by", sa.Text, nullable=False),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint(
            "policy_id", "version", name="uq_policy_versions_policy_version"
        ),
    )
    op.create_index("ix_policy_versions_tenant", "policy_versions", ["tenant_id"])
    op.execute(IMMUTABLE_FUNCTION_SQL)
    op.execute(IMMUTABLE_TRIGGER_SQL)

    for table in TABLES:
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} FOR ALL "
            f"USING {_expr('tenant_id')} WITH CHECK {_expr('tenant_id')}"
        )
        op.execute(
            "DO $$ BEGIN "
            "IF (SELECT relrowsecurity FROM pg_class "
            "WHERE oid = to_regclass('ai_assets')) THEN "
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY; "
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY; "
            "END IF; END $$"
        )

    _bypass()
    _grant("admin_tokens", NEW_ADMIN_SCOPE, PREVIOUS_ADMIN_SCOPES)


def downgrade() -> None:
    _bypass()
    for table, scope in (
        ("admin_tokens", NEW_ADMIN_SCOPE),
        ("tenant_api_keys", NEW_KEY_SCOPE),
    ):
        op.execute(
            f"UPDATE {table} SET scopes = array_remove(scopes, '{scope}') "
            f"WHERE '{scope}' = ANY(scopes)"
        )
    op.drop_table("policy_versions")
    op.execute("DROP FUNCTION IF EXISTS policy_versions_immutable()")
    op.drop_table("policies")
