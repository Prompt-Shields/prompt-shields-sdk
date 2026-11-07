"""Inference credential vault (#342)

Adds ``inference_credentials``: a tenant's stored inference API keys,
envelope-encrypted (a data key per row, wrapped by a key-encryption key in
Azure Key Vault or, for development, a local keyring; collector/kms.py).
The table gets the tenant_isolation policy (same SQL as 015) and, if RLS is
already enforced on ai_assets, enforcement too (same as 020).

The new admin scope ``admin:inference-credentials`` is granted to every
admin token that holds all nine admin scopes that existed before it (as
038 and 039 did): those tokens were full admin and stay full admin.
Narrower tokens are unchanged. Downgrade removes the scope from every
token and drops the table.

Revision ID: 044
Revises: 039
Create Date: 2026-11-07

Chained from main's head when written (039); re-chain at merge if another
migration lands first.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "044"
down_revision: str | None = "039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "inference_credentials"
POLICY = "tenant_isolation"
NEW_SCOPE = "admin:inference-credentials"
# Every admin scope before admin:inference-credentials (frozen here).
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
)

_EXPR = (
    "(coalesce(current_setting('app.rls_bypass', true), '') <> '' "
    "OR tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
)


def _bypass() -> None:
    # admin_tokens is under FORCE ROW LEVEL SECURITY (binds the owner too).
    op.execute("SELECT set_config('app.rls_bypass', 'migration-044', true)")


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.TIMESTAMP(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("sealed_key", sa.Text, nullable=False),
        sa.Column("wrapped_dek", sa.Text, nullable=False),
        sa.Column("kek_ref", sa.Text, nullable=False),
        sa.Column("key_hint", sa.Text, nullable=False),
        sa.Column("key_fingerprint", sa.Text, nullable=False),
        _ts("expires_at"),
        _ts("disabled_at"),
        _ts("created_at", nullable=False),
        _ts("updated_at"),
        _ts("rotated_at"),
        _ts("rewrapped_at"),
        _ts("last_used_at"),
        sa.UniqueConstraint(
            "tenant_id", "provider", "label", name="uq_inference_credentials_label"
        ),
    )
    op.create_index("ix_inference_credentials_kek_ref", TABLE, ["kek_ref"])

    op.execute(
        f"CREATE POLICY {POLICY} ON {TABLE} FOR ALL USING {_EXPR} WITH CHECK {_EXPR}"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF (SELECT relrowsecurity FROM pg_class "
        "WHERE oid = to_regclass('ai_assets')) THEN "
        f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY; "
        f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY; "
        "END IF; END $$"
    )

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
    op.drop_table(TABLE)
