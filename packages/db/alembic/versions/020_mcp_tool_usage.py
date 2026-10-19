"""MCP tool usage (observe-only discovery, #63 Phase 0)

Adds ``mcp_tool_usage``: observed MCP tool calls aggregated per tenant,
source, server, tool, calling service and environment (call and error
counts, latency sum, first/last seen). Names only; arguments, results and
server URLs are never stored. Fed by ``POST /ingest/mcp`` (SDK) and MCP
spans on ``/ingest/otel/v1/traces``.

The table gets the tenant_isolation policy (same SQL as 015). If RLS is
already enforced on ai_assets (``python -m collector.rls enable`` was run),
it is enforced here too, so this is never the one unprotected table;
otherwise the policy stays inert like the rest (same as 018).

Revision ID: 020
Revises: 019
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "020"
down_revision: str | None = "019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
TABLE = "mcp_tool_usage"


def _expr(column: str) -> str:
    return (
        "(coalesce(current_setting('app.rls_bypass', true), '') <> '' "
        f"OR {column} = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )


def _count(name: str) -> sa.Column:
    return sa.Column(name, sa.BigInteger, nullable=False, server_default="0")


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
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("server_name", sa.Text, nullable=False),
        sa.Column("tool_name", sa.Text, nullable=False),
        sa.Column("calling_service", sa.Text, nullable=False, server_default=""),
        sa.Column("environment", sa.Text, nullable=False, server_default=""),
        sa.Column("transport", sa.Text),
        _count("call_count"),
        _count("error_count"),
        _count("latency_ms_total"),
        _count("latency_count"),
        sa.Column(
            "first_seen",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ux_mcp_tool_usage_key",
        TABLE,
        [
            "tenant_id",
            "source",
            "server_name",
            "tool_name",
            "calling_service",
            "environment",
        ],
        unique=True,
    )
    op.create_index(
        "ix_mcp_tool_usage_tenant_last_seen", TABLE, ["tenant_id", "last_seen"]
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
    op.drop_index("ix_mcp_tool_usage_tenant_last_seen", table_name=TABLE)
    op.drop_index("ux_mcp_tool_usage_key", table_name=TABLE)
    op.drop_table(TABLE)
