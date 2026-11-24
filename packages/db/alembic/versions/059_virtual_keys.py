"""Gateway virtual keys and credential pools (#343)

Adds ``virtual_keys`` (``ps-vk-...`` keys, stored as a SHA-256 hash, bound
to a team, project and/or principal, with an optional model allowlist and a
pool strategy) and ``virtual_key_credentials`` (the pool: inference
credentials from the vault, #342, in order). Both get the tenant_isolation
policy (same SQL as 015) and, if RLS is already enforced on ai_assets,
enforcement too (same as 020).

The new admin scope ``admin:virtual-keys`` is granted to every admin token
that holds all eleven admin scopes that existed before it (as 044 did):
those tokens were full admin and stay full admin. Narrower tokens are
unchanged. The ingest key scope ``virtual-keys:resolve`` (the gateway's
resolution credential) is never granted implicitly. Downgrade removes the
admin scope from every token and the ingest scope from every key, and drops
both tables.

Revision ID: 059
Revises: 058
Create Date: 2026-11-24

Chained from main's head (058); re-chain at merge if another migration
lands first.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "059"
down_revision: str | None = "058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEYS = "virtual_keys"
POOL = "virtual_key_credentials"
POLICY = "tenant_isolation"
NEW_SCOPE = "admin:virtual-keys"
NEW_KEY_SCOPE = "virtual-keys:resolve"
# Every admin scope before admin:virtual-keys (frozen here).
PREVIOUS_SCOPES = (
    "admin:read",
    "admin:keys",
    "admin:tokens",
    "admin:provider-credentials",
    "admin:webhooks",
    "admin:partners",
    "admin:registry",
    "admin:privacy",
    "admin:members",
    "admin:policies",
    "admin:inference-credentials",
)

_EXPR = (
    "(coalesce(current_setting('app.rls_bypass', true), '') <> '' "
    "OR tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
)


def _bypass() -> None:
    # admin_tokens and tenant_api_keys are under FORCE ROW LEVEL SECURITY.
    op.execute("SELECT set_config('app.rls_bypass', 'migration-059', true)")


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.TIMESTAMP(timezone=True), nullable=nullable)


def _uuid_fk(name: str, target: str, nullable: bool = True, **kw) -> sa.Column:
    return sa.Column(
        name, postgresql.UUID(as_uuid=True), sa.ForeignKey(target, **kw), nullable=nullable
    )


def _isolate(table: str) -> None:
    op.execute(
        f"CREATE POLICY {POLICY} ON {table} FOR ALL USING {_EXPR} WITH CHECK {_EXPR}"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF (SELECT relrowsecurity FROM pg_class "
        "WHERE oid = to_regclass('ai_assets')) THEN "
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY; "
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY; "
        "END IF; END $$"
    )


def upgrade() -> None:
    op.create_table(
        KEYS,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid_fk("tenant_id", "tenants.id", nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("key_hash", sa.Text, nullable=False),
        sa.Column("key_hint", sa.Text, nullable=False),
        _uuid_fk("team_id", "teams.id"),
        _uuid_fk("project_id", "projects.id"),
        _uuid_fk("principal_id", "principals.id", ondelete="SET NULL"),
        sa.Column("allowed_models", postgresql.ARRAY(sa.Text)),
        sa.Column(
            "strategy", sa.Text, nullable=False, server_default="round_robin"
        ),
        sa.Column("created_by", sa.Text),
        _ts("expires_at"),
        _ts("revoked_at"),
        _ts("created_at", nullable=False),
        _ts("updated_at"),
        _ts("last_resolved_at"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_virtual_keys_tenant_name"),
        sa.UniqueConstraint("key_hash", name="uq_virtual_keys_key_hash"),
        sa.CheckConstraint(
            "strategy IN ('round_robin', 'least_recently_limited')",
            name="ck_virtual_keys_strategy",
        ),
    )
    op.create_index("ix_virtual_keys_team", KEYS, ["team_id"])
    op.create_index("ix_virtual_keys_project", KEYS, ["project_id"])
    op.create_index("ix_virtual_keys_principal", KEYS, ["principal_id"])

    op.create_table(
        POOL,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid_fk("tenant_id", "tenants.id", nullable=False),
        _uuid_fk("virtual_key_id", "virtual_keys.id", nullable=False, ondelete="CASCADE"),
        _uuid_fk(
            "credential_id", "inference_credentials.id", nullable=False, ondelete="CASCADE"
        ),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "virtual_key_id", "credential_id", name="uq_virtual_key_credentials_pair"
        ),
    )
    op.create_index("ix_virtual_key_credentials_credential", POOL, ["credential_id"])

    _isolate(KEYS)
    _isolate(POOL)

    _bypass()
    previous = ",".join(PREVIOUS_SCOPES)
    op.execute(
        f"UPDATE admin_tokens SET scopes = array_append(scopes, '{NEW_SCOPE}') "
        f"WHERE scopes @> '{{{previous}}}'::text[] "
        f"AND NOT ('{NEW_SCOPE}' = ANY(scopes))"
    )


def downgrade() -> None:
    _bypass()
    op.execute(
        f"UPDATE admin_tokens SET scopes = array_remove(scopes, '{NEW_SCOPE}') "
        f"WHERE '{NEW_SCOPE}' = ANY(scopes)"
    )
    op.execute(
        f"UPDATE tenant_api_keys SET scopes = array_remove(scopes, '{NEW_KEY_SCOPE}') "
        f"WHERE '{NEW_KEY_SCOPE}' = ANY(scopes)"
    )
    op.drop_table(POOL)
    op.drop_table(KEYS)
