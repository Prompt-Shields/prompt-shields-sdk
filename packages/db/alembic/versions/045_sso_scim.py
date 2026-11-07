"""SSO and SCIM: OIDC admin login, SCIM tokens, IdP group roles (#339)

Adds four tenant tables:

  - ``tenant_oidc_configs``: a tenant's OpenID Connect identity provider
    for admin API login (one per tenant; client secret sealed with the
    PS_WEBHOOK_SECRET_KEYS keyring, or none for a PKCE-only public client);
  - ``oidc_login_states``: pending logins (state and browser binding by
    SHA-256, nonce, PKCE code verifier), single use, short-lived;
  - ``scim_tokens``: per-tenant SCIM 2.0 bearer tokens (SHA-256 lookup
    prefix + bcrypt hash, scopes ``scim:users`` / ``scim:groups``);
  - ``idp_group_roles``: the team role (and optional tenant-wide role) that
    members of an identity provider group get; never ``owner`` (CHECK).

and two nullable columns: ``principals.user_name`` (SCIM userName, unique
per tenant case-insensitively) and ``teams.external_id`` (SCIM Group
externalId). Existing rows keep NULL.

Each new table gets the tenant_isolation policy (same SQL as 015) and, if
RLS is already enforced on ai_assets, enforcement too (same as 020).

Follows 039 (RBAC, #338). Must be re-chained onto main's head at merge if
another migration lands first.

Revision ID: 045
Revises: 039
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "045"
down_revision: str | None = "039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
TABLES = ("tenant_oidc_configs", "oidc_login_states", "scim_tokens", "idp_group_roles")


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
    op.add_column("principals", sa.Column("user_name", sa.Text, nullable=True))
    op.create_index(
        "ux_principals_tenant_user_name",
        "principals",
        ["tenant_id", sa.text("lower(user_name)")],
        unique=True,
        postgresql_where=sa.text("user_name IS NOT NULL"),
    )
    op.add_column("teams", sa.Column("external_id", sa.Text, nullable=True))

    op.create_table(
        "tenant_oidc_configs",
        _uuid("id", primary_key=True),
        _tenant(),
        sa.Column("issuer", sa.Text, nullable=False),
        sa.Column("client_id", sa.Text, nullable=False),
        sa.Column("client_secret_ciphertext", sa.Text),
        sa.Column("redirect_uri", sa.Text, nullable=False),
        sa.Column("audience", sa.Text),
        sa.Column("algorithms", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("subject_claim", sa.Text, nullable=False, server_default="sub"),
        sa.Column("session_ttl_seconds", sa.Integer, nullable=False, server_default="3600"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Text, nullable=False),
        _ts("created_at", nullable=False),
        _ts("updated_at"),
    )
    op.create_index(
        "ux_tenant_oidc_configs_tenant", "tenant_oidc_configs", ["tenant_id"], unique=True
    )

    op.create_table(
        "oidc_login_states",
        _uuid("id", primary_key=True),
        _tenant(),
        sa.Column("state_hash", sa.Text, nullable=False),
        sa.Column("browser_hash", sa.Text, nullable=False),
        sa.Column("nonce", sa.Text, nullable=False),
        sa.Column("code_verifier", sa.Text, nullable=False),
        _ts("created_at", nullable=False),
        _ts("expires_at", nullable=False),
    )
    op.create_index(
        "ux_oidc_login_states_state", "oidc_login_states", ["state_hash"], unique=True
    )
    op.create_index(
        "ix_oidc_login_states_tenant", "oidc_login_states", ["tenant_id", "expires_at"]
    )

    op.create_table(
        "scim_tokens",
        _uuid("id", primary_key=True),
        _tenant(),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("token_prefix", sa.Text, nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False),
        sa.Column("token_hint", sa.Text, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        _ts("created_at", nullable=False),
        _ts("expires_at"),
        _ts("revoked_at"),
    )
    op.create_index("ux_scim_tokens_prefix", "scim_tokens", ["token_prefix"], unique=True)
    op.create_index(
        "ux_scim_tokens_tenant_name",
        "scim_tokens",
        ["tenant_id", "name"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "idp_group_roles",
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("team_id", sa.ForeignKey("teams.id", ondelete="CASCADE")),
        sa.Column("member_role", sa.Text, nullable=False, server_default="viewer"),
        sa.Column("tenant_role", sa.Text),
        sa.Column("created_by", sa.Text, nullable=False),
        _ts("created_at", nullable=False),
        _ts("updated_at"),
        sa.CheckConstraint(
            "member_role <> 'owner' AND (tenant_role IS NULL OR tenant_role <> 'owner')",
            name="ck_idp_group_roles_not_owner",
        ),
    )
    op.create_index("ux_idp_group_roles_team", "idp_group_roles", ["team_id"], unique=True)
    op.create_index("ix_idp_group_roles_tenant", "idp_group_roles", ["tenant_id"])

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
    op.drop_table("idp_group_roles")
    op.drop_table("scim_tokens")
    op.drop_table("oidc_login_states")
    op.drop_table("tenant_oidc_configs")
    op.drop_column("teams", "external_id")
    op.drop_index("ux_principals_tenant_user_name", table_name="principals")
    op.drop_column("principals", "user_name")
