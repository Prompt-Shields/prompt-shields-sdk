"""Gateway security events in the collector (#353)

Adds ``gateway_security_events``: one row per security event the gateway
sends (schema v1: guardrail verdicts, PII and injection detections, auth
failures, policy denials, rate limits, payload rejections), metadata only.
Written by ``POST /ingest/otel/v1/logs`` and ``POST /ingest/security-events``,
read through ``GET /partner/v1/security-events`` and pushed by the SIEM
connectors (collector.siem_export). Rows older than the tenant's retention
period are purged.

The new table gets the tenant_isolation policy (same SQL as 015) and, if
RLS is already enforced on ai_assets, enforcement too (same as 020).

Chained from 038 (main's head when written); 039-041 are reserved for other
open changes, so this may be re-chained at merge.

Revision ID: 042
Revises: 038
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "042"
down_revision: str | None = "038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
TABLE = "gateway_security_events"


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
        sa.Column("event_id", sa.Text, nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("kind", sa.Text),
        sa.Column("outcome", sa.Text),
        sa.Column("severity", sa.Text),
        sa.Column("action", sa.Text),
        sa.Column("signature", sa.Text),
        sa.Column("request_id", sa.Text),
        sa.Column("route", sa.Text),
        sa.Column("method", sa.Text),
        sa.Column("business_unit", sa.Text),
        sa.Column("environment", sa.Text),
        sa.Column("provider", sa.Text),
        sa.Column("model", sa.Text),
        sa.Column("status_code", sa.Integer),
        sa.Column("user_hash", sa.Text),
        sa.Column("user_id_hash", sa.Text),
        sa.Column("schema_version", sa.Text),
        sa.Column(
            "attributes",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.UniqueConstraint(
            "tenant_id", "event_id", name="ux_gateway_security_events_event"
        ),
    )
    op.create_index(
        "ix_gateway_security_events_tenant_occurred", TABLE, ["tenant_id", "occurred_at"]
    )
    op.create_index(
        "ix_gateway_security_events_tenant_received",
        TABLE,
        ["tenant_id", "received_at", "id"],
    )
    op.create_index(
        "ix_gateway_security_events_tenant_user",
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
    op.drop_index("ix_gateway_security_events_tenant_user", table_name=TABLE)
    op.drop_index("ix_gateway_security_events_tenant_received", table_name=TABLE)
    op.drop_index("ix_gateway_security_events_tenant_occurred", table_name=TABLE)
    op.drop_table(TABLE)
