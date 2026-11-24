"""Opt-in payload logging: call_payloads (#352)

Adds ``call_payloads``: one row per logged model call (tenant, call id)
written by ``POST /ingest/payloads`` when the tenant turned payload logging
on (off by default): sizes, route, status, per-category redaction counts
and, in ``redacted`` / ``full`` mode, the request and response sealed with
a per-tenant key. Read through ``GET /partner/v1/calls/{call_id}``
(``logs:read``, every read audited), purged after the tenant's payload
retention (default 30 days) unless the tenant is on legal hold, removed by
tenant erasure and by end-user erasure.

The new table gets the tenant_isolation policy (same SQL as 015) and, if
RLS is already enforced on ai_assets, enforcement too (same as 020).

Chained from 045 (main's head at the time); may be re-chained at merge.

Revision ID: 056
Revises: 045
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "056"
down_revision: str | None = "045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
TABLE = "call_payloads"


def _expr(column: str) -> str:
    return (
        "(coalesce(current_setting('app.rls_bypass', true), '') <> '' "
        f"OR {column} = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )


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
        sa.Column("call_id", sa.Text, nullable=False),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("mode", sa.Text, nullable=False),
        sa.Column("route", sa.Text),
        sa.Column("status_code", sa.Integer),
        sa.Column("is_error", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("request_bytes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("response_bytes", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "redactions",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("storage", sa.Text, nullable=False, server_default="database"),
        sa.Column("key_id", sa.Text),
        sa.Column("ciphertext", sa.Text),
        sa.Column("user_id_hash", sa.Text),
        sa.UniqueConstraint("tenant_id", "call_id", name="ux_call_payloads_call"),
    )
    op.create_index(
        "ix_call_payloads_tenant_received", TABLE, ["tenant_id", "received_at"]
    )
    op.create_index(
        "ix_call_payloads_tenant_user",
        TABLE,
        ["tenant_id", "user_id_hash"],
        postgresql_where=sa.text("user_id_hash IS NOT NULL"),
    )

    op.execute(
        f"CREATE POLICY {POLICY} ON {TABLE} FOR ALL "
        f"USING {_expr('tenant_id')} WITH CHECK {_expr('tenant_id')}"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF (SELECT relrowsecurity FROM pg_class "
        "WHERE oid = to_regclass('ai_assets')) THEN "
        f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY; "
        f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.drop_index("ix_call_payloads_tenant_user", table_name=TABLE)
    op.drop_index("ix_call_payloads_tenant_received", table_name=TABLE)
    op.drop_table(TABLE)
