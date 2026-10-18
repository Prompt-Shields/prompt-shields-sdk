"""Encrypted provider admin credentials and key fingerprint links

Adds, for the provider org usage pull (platform signal, #93):

  - provider_credentials: a tenant's OpenAI / Anthropic admin key, sealed
    with the PS_WEBHOOK_SECRET_KEYS keyring (never stored in plaintext),
    one row per tenant and provider;
  - provider_key_fingerprints: tenant-registered links from a
    provider-reported API key id to the SDK's api_key_fingerprint;
  - ai_usage_events.api_key_fingerprint (nullable), which the SDK already
    sends and the collector used to drop.

Both new tables get the tenant_isolation policy (same SQL as 015). If RLS
is already enforced on ai_assets (``python -m collector.rls enable`` was
run), it is enforced on the new tables too, so they are never the one
unprotected table; otherwise the policies stay inert like the rest.

Revision ID: 018
Revises: 017
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "018"
down_revision: str | None = "017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
NEW_TABLES = ("provider_credentials", "provider_key_fingerprints")


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


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def upgrade() -> None:
    op.create_table(
        "provider_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("secret_ciphertext", sa.Text, nullable=False),
        sa.Column("key_hint", sa.Text, nullable=False),
        _created_at(),
        sa.Column("rotated_at", sa.TIMESTAMP(timezone=True)),
        sa.UniqueConstraint(
            "tenant_id", "provider", name="uq_provider_credentials_tenant_provider"
        ),
    )
    op.create_table(
        "provider_key_fingerprints",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("api_key_id", sa.Text, nullable=False),
        sa.Column("api_key_fingerprint", sa.Text, nullable=False),
        _created_at(),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "api_key_id",
            name="uq_provider_key_fingerprints_key",
        ),
    )
    op.create_index(
        "ix_provider_key_fingerprints_tenant_fp",
        "provider_key_fingerprints",
        ["tenant_id", "api_key_fingerprint"],
    )
    op.add_column(
        "ai_usage_events", sa.Column("api_key_fingerprint", sa.Text(), nullable=True)
    )
    op.create_index(
        "ix_usage_events_tenant_key_fp",
        "ai_usage_events",
        ["tenant_id", "api_key_fingerprint"],
    )

    for table in NEW_TABLES:
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} FOR ALL "
            f"USING {_expr('tenant_id')} WITH CHECK {_expr('tenant_id')}"
        )
    # Match the enforcement state of the existing tenant tables.
    alters = " ".join(
        f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY; ALTER TABLE {t} FORCE ROW LEVEL SECURITY;"
        for t in NEW_TABLES
    )
    op.execute(
        "DO $$ BEGIN "
        "IF (SELECT relrowsecurity FROM pg_class "
        "WHERE oid = to_regclass('ai_assets')) THEN "
        f"{alters} "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.drop_index("ix_usage_events_tenant_key_fp", table_name="ai_usage_events")
    op.drop_column("ai_usage_events", "api_key_fingerprint")
    op.drop_index(
        "ix_provider_key_fingerprints_tenant_fp", table_name="provider_key_fingerprints"
    )
    op.drop_table("provider_key_fingerprints")
    op.drop_table("provider_credentials")
