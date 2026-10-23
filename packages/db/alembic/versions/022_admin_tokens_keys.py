"""Scoped admin tokens, tenant ingest key table, tenant disable

Adds:

  - ``admin_tokens``: tenant-scoped automation tokens for /api/v1/admin
    with explicit scopes, an optional expiry and revocation. Stored as a
    SHA-256 lookup prefix + bcrypt hash, like the ingest keys.
  - ``tenant_api_keys``: one row per tenant ingest key, with a stable id,
    a name (unique among unrevoked keys of a tenant), expiry, revocation and
    a rotation grace window (``previous_key_*``).
  - ``tenants.disabled_at``: set by the operator API; a disabled tenant's
    keys, tokens and partner credentials are refused.

Both tables get the tenant_isolation policy (same SQL as 015). If RLS is
already enforced on ai_assets it is enforced here too (same as 020).

Data: every legacy key on ``tenants.api_key_prefix`` / ``api_key_hash`` is
moved into ``tenant_api_keys`` as ``legacy-ingest-key`` (origin 'legacy')
and the tenant columns are cleared. The key does not change, so SDKs and
gateways keep working; it can now be listed, rotated and revoked. The
collector still accepts a key written to the tenant columns later (older
seeds and fixtures do this) and moves it when that tenant first issues a key
through the key API; listing keys does not move it.

Downgrade puts one live key per tenant back on the tenant row (the legacy
one if still live, else the oldest live key) so every tenant that could
ingest still can, then drops the tables and the column. Other keys and all
admin tokens are lost. So is ``tenants.disabled_at``: a tenant disabled
through the operator API is enabled again by a downgrade (its restored key
works), and stays enabled after a later upgrade. List such tenants before
downgrading (``SELECT id FROM tenants WHERE disabled_at IS NOT NULL``) and
disable them again after the next upgrade
(``PATCH /api/v1/operator/tenants/{id}`` with ``{"disabled": true}``).

Revision ID: 022
Revises: 021
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "022"
down_revision: str | None = "021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
TABLES = ("tenant_api_keys", "admin_tokens")


def _expr(column: str) -> str:
    return (
        "(coalesce(current_setting('app.rls_bypass', true), '') <> '' "
        f"OR {column} = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )


def _tenant_fk() -> sa.Column:
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id"),
        nullable=False,
    )


def _ts(name: str, nullable: bool = True) -> sa.Column:
    kwargs = {} if nullable else {"server_default": sa.func.now()}
    return sa.Column(name, sa.TIMESTAMP(timezone=True), nullable=nullable, **kwargs)


def upgrade() -> None:
    op.add_column(
        "tenants", sa.Column("disabled_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )

    op.create_table(
        "tenant_api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("key_prefix", sa.Text, nullable=False),
        sa.Column("key_hash", sa.Text, nullable=False),
        sa.Column("key_hint", sa.Text),
        sa.Column("previous_key_prefix", sa.Text),
        sa.Column("previous_key_hash", sa.Text),
        _ts("previous_expires_at"),
        sa.Column("origin", sa.Text, nullable=False, server_default="api"),
        _ts("created_at", nullable=False),
        _ts("rotated_at"),
        _ts("expires_at"),
        _ts("revoked_at"),
    )
    op.create_index(
        "ux_tenant_api_keys_prefix", "tenant_api_keys", ["key_prefix"], unique=True
    )
    op.create_index(
        "ux_tenant_api_keys_previous_prefix",
        "tenant_api_keys",
        ["previous_key_prefix"],
        unique=True,
        postgresql_where=sa.text("previous_key_prefix IS NOT NULL"),
    )
    op.create_index(
        "ux_tenant_api_keys_tenant_name",
        "tenant_api_keys",
        ["tenant_id", "name"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_tenant_api_keys_tenant", "tenant_api_keys", ["tenant_id", "created_at"]
    )

    op.create_table(
        "admin_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
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
    op.create_index(
        "ux_admin_tokens_prefix", "admin_tokens", ["token_prefix"], unique=True
    )
    op.create_index(
        "ux_admin_tokens_tenant_name",
        "admin_tokens",
        ["tenant_id", "name"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_admin_tokens_tenant", "admin_tokens", ["tenant_id", "created_at"]
    )

    for table in TABLES:
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} FOR ALL "
            f"USING {_expr('tenant_id')} WITH CHECK {_expr('tenant_id')}"
        )
        # Match the enforcement state of the existing tenant tables.
        op.execute(
            "DO $$ BEGIN "
            "IF (SELECT relrowsecurity FROM pg_class "
            "WHERE oid = to_regclass('ai_assets')) THEN "
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY; "
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY; "
            "END IF; END $$"
        )

    # Move legacy keys. The bypass covers a database where RLS is already
    # enforced and the migration role is bound by it (transaction-local).
    op.execute("SELECT set_config('app.rls_bypass', 'migration-022', true)")
    op.execute(
        "INSERT INTO tenant_api_keys "
        "(id, tenant_id, name, key_prefix, key_hash, key_hint, origin, created_at) "
        "SELECT gen_random_uuid(), id, 'legacy-ingest-key', api_key_prefix, "
        "api_key_hash, NULL, 'legacy', coalesce(created_at, now()) "
        "FROM tenants "
        "WHERE api_key_prefix IS NOT NULL AND api_key_hash IS NOT NULL"
    )
    op.execute(
        "UPDATE tenants SET api_key_prefix = NULL, api_key_hash = NULL "
        "WHERE api_key_prefix IS NOT NULL AND api_key_hash IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("SELECT set_config('app.rls_bypass', 'migration-022', true)")
    op.execute(
        "UPDATE tenants t SET api_key_prefix = k.key_prefix, api_key_hash = k.key_hash "
        "FROM ("
        "  SELECT DISTINCT ON (tenant_id) tenant_id, key_prefix, key_hash "
        "  FROM tenant_api_keys "
        "  WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now()) "
        "  ORDER BY tenant_id, (origin = 'legacy') DESC, created_at"
        ") k "
        "WHERE t.id = k.tenant_id AND t.api_key_hash IS NULL"
    )
    op.drop_index("ix_admin_tokens_tenant", table_name="admin_tokens")
    op.drop_index("ux_admin_tokens_tenant_name", table_name="admin_tokens")
    op.drop_index("ux_admin_tokens_prefix", table_name="admin_tokens")
    op.drop_table("admin_tokens")
    op.drop_index("ix_tenant_api_keys_tenant", table_name="tenant_api_keys")
    op.drop_index("ux_tenant_api_keys_tenant_name", table_name="tenant_api_keys")
    op.drop_index("ux_tenant_api_keys_previous_prefix", table_name="tenant_api_keys")
    op.drop_index("ux_tenant_api_keys_prefix", table_name="tenant_api_keys")
    op.drop_table("tenant_api_keys")
    op.drop_column("tenants", "disabled_at")
