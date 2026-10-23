"""call_id on usage events, and call feedback (#43)

Adds ``ai_usage_events.call_id`` (the per-call id the SDK or gateway puts
on each event, indexed per tenant) and ``call_feedback``: one explicit
feedback row per (tenant, call_id), metadata only (score, reason label,
opaque user id, source). Fed by ``POST /ingest/feedback``.

The new table gets the tenant_isolation policy (same SQL as 015) and, if
RLS is already enforced on ai_assets, enforcement too (same as 020).

Revision ID: 027
Revises: 023 (intended: 026; the chain is fixed at merge)
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "027"
down_revision: str | None = "023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
TABLE = "call_feedback"


def _expr(column: str) -> str:
    return (
        "(coalesce(current_setting('app.rls_bypass', true), '') <> '' "
        f"OR {column} = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )


def upgrade() -> None:
    op.add_column("ai_usage_events", sa.Column("call_id", sa.Text))
    op.create_index(
        "ix_usage_events_tenant_call_id", "ai_usage_events", ["tenant_id", "call_id"]
    )

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
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("user_id", sa.Text),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", "call_id", name="ux_call_feedback_call"),
    )
    op.create_index(
        "ix_call_feedback_tenant_updated", TABLE, ["tenant_id", "updated_at"]
    )

    op.execute(
        f"CREATE POLICY {POLICY} ON {TABLE} FOR ALL "
        f"USING {_expr('tenant_id')} WITH CHECK {_expr('tenant_id')}"
    )
    # Match the enforcement state of the existing tenant tables.
    op.execute(
        "DO $$ BEGIN "
        "IF (SELECT relrowsecurity FROM pg_class "
        "WHERE oid = to_regclass('ai_assets')) THEN "
        f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY; "
        f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.drop_index("ix_call_feedback_tenant_updated", table_name=TABLE)
    op.drop_table(TABLE)
    op.drop_index("ix_usage_events_tenant_call_id", table_name="ai_usage_events")
    op.drop_column("ai_usage_events", "call_id")
