"""MCP tool-call metering: per-day aggregates (#77)

Adds ``mcp_tool_usage_daily``: one row per (tenant, UTC day, source, MCP
server, tool) with call, error and blocked counts, latency sum/count and
request/response bytes (the gateway's MCP proxy reports bytes). Names and
numbers only, like ``mcp_tool_usage``. Read through
``GET /partner/v1/mcp/usage``; rows older than the tenant's retention
period are purged.

The new table gets the tenant_isolation policy (same SQL as 015) and, if
RLS is already enforced on ai_assets, enforcement too (same as 020).

Assigned number 032 (030 = learner #266, 031 = telemetry columns #274).
Chained from main's current head (029) so the branch migrates in CI; it
will be re-chained to "031" at merge.

Revision ID: 032
Revises: 029
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "032"
# Re-chained to "031" at merge (main's head is "029" while this PR is open).
down_revision: str | None = "029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
TABLE = "mcp_tool_usage_daily"


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
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("server_name", sa.Text, nullable=False),
        sa.Column("tool_name", sa.Text, nullable=False),
        _count("call_count"),
        _count("error_count"),
        _count("blocked_count"),
        _count("latency_ms_total"),
        _count("latency_count"),
        _count("request_bytes"),
        _count("response_bytes"),
        _count("bytes_count"),
    )
    op.create_index(
        "ux_mcp_tool_usage_daily_key",
        TABLE,
        ["tenant_id", "day", "source", "server_name", "tool_name"],
        unique=True,
    )
    op.create_index("ix_mcp_tool_usage_daily_tenant_day", TABLE, ["tenant_id", "day"])

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
    op.drop_index("ix_mcp_tool_usage_daily_tenant_day", table_name=TABLE)
    op.drop_index("ux_mcp_tool_usage_daily_key", table_name=TABLE)
    op.drop_table(TABLE)
