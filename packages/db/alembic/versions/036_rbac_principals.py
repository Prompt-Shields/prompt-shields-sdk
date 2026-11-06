"""RBAC: principals, teams, projects and memberships (#338)

Adds four tenant tables:

  - ``principals``: users and service accounts (kind, external_id, email,
    display_name, disabled_at);
  - ``teams`` and ``projects`` (a project belongs to a team);
  - ``memberships``: a principal's built-in role (owner, admin, developer,
    viewer, auditor, billing; collector/rbac.py) tenant-wide (team_id NULL),
    in a team, or in one of the team's projects. One role per principal
    per place.

and a nullable ``principal_id`` on ``admin_tokens`` and
``partner_credentials``: a credential issued for a principal reaches at
most what the principal's roles grant. Existing rows keep NULL and work
unchanged.

Each new table gets the tenant_isolation policy (same SQL as 015) and, if
RLS is already enforced on ai_assets, enforcement too (same as 020).

Follows 035 (identity fields, #334).

Revision ID: 036
Revises: 035
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "036"
down_revision: str | None = "035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
TABLES = ("principals", "teams", "projects", "memberships")


def _expr(column: str) -> str:
    return (
        "(coalesce(current_setting('app.rls_bypass', true), '') <> '' "
        f"OR {column} = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )


def _uuid(name: str, *fk, nullable: bool = False, **kw) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *fk, nullable=nullable, **kw)


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.TIMESTAMP(timezone=True), nullable=nullable)


def _tenant() -> sa.Column:
    return _uuid("tenant_id", sa.ForeignKey("tenants.id"))


def upgrade() -> None:
    op.create_table(
        "principals",
        _uuid("id", primary_key=True),
        _tenant(),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("external_id", sa.Text),
        sa.Column("email", sa.Text),
        sa.Column("display_name", sa.Text),
        _ts("created_at", nullable=False),
        _ts("updated_at"),
        _ts("disabled_at"),
        sa.CheckConstraint(
            "kind IN ('user', 'service_account')", name="ck_principals_kind"
        ),
    )
    op.create_index(
        "ux_principals_tenant_external_id",
        "principals",
        ["tenant_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
    op.create_index("ix_principals_tenant", "principals", ["tenant_id", "created_at"])

    op.create_table(
        "teams",
        _uuid("id", primary_key=True),
        _tenant(),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        _ts("created_at", nullable=False),
        _ts("updated_at"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_teams_tenant_name"),
    )

    op.create_table(
        "projects",
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("team_id", sa.ForeignKey("teams.id")),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        _ts("created_at", nullable=False),
        _ts("updated_at"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_projects_tenant_name"),
    )
    op.create_index("ix_projects_team", "projects", ["team_id"])

    op.create_table(
        "memberships",
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("principal_id", sa.ForeignKey("principals.id", ondelete="CASCADE")),
        _uuid("team_id", sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=True),
        _uuid(
            "project_id", sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        _ts("created_at", nullable=False),
        _ts("updated_at"),
        sa.CheckConstraint(
            "project_id IS NULL OR team_id IS NOT NULL",
            name="ck_memberships_project_needs_team",
        ),
    )
    op.create_index(
        "ux_memberships_tenant_wide",
        "memberships",
        ["principal_id"],
        unique=True,
        postgresql_where=sa.text("team_id IS NULL"),
    )
    op.create_index(
        "ux_memberships_team",
        "memberships",
        ["principal_id", "team_id"],
        unique=True,
        postgresql_where=sa.text("team_id IS NOT NULL AND project_id IS NULL"),
    )
    op.create_index(
        "ux_memberships_project",
        "memberships",
        ["principal_id", "project_id"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
    )
    op.create_index("ix_memberships_tenant", "memberships", ["tenant_id"])
    op.create_index("ix_memberships_team", "memberships", ["team_id"])

    for table in ("admin_tokens", "partner_credentials"):
        op.add_column(
            table,
            sa.Column(
                "principal_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(
                    "principals.id",
                    ondelete="SET NULL",
                    name=f"fk_{table}_principal_id",
                ),
                nullable=True,
            ),
        )

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


def downgrade() -> None:
    for table in ("partner_credentials", "admin_tokens"):
        op.drop_constraint(f"fk_{table}_principal_id", table, type_="foreignkey")
        op.drop_column(table, "principal_id")
    op.drop_table("memberships")
    op.drop_table("projects")
    op.drop_table("teams")
    op.drop_table("principals")
