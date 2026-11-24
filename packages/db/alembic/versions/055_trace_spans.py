"""Agent trace spans (#385)

Adds ``trace_spans``: one row per span the OTLP trace receiver
(``POST /ingest/otel/v1/traces``) keeps for a trace, with its trace, span
and parent span ids, name, kind, status, timings and a GenAI metadata
allowlist (operation, provider, models, tool name, token counts, session
id and the salted user id hash) and ``pending_until`` (a span held until
its trace gains a GenAI span). No content. Read through
``GET /partner/v1/traces`` and ``GET /partner/v1/traces/{trace_id}``;
purged with the tenant's usage events, removed by tenant erasure, and user
erasure clears ``user_id_hash`` and ``session_id``.

The new table gets the tenant_isolation policy (same SQL as 015) and, if
RLS is already enforced on ai_assets, enforcement too (same as 020).

Chained from 054 (policy soft delete, #383).

Revision ID: 055
Revises: 054
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "055"
down_revision: str | None = "054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
TABLE = "trace_spans"


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
        sa.Column("trace_id", sa.Text, nullable=False),
        sa.Column("span_id", sa.Text, nullable=False),
        sa.Column("parent_span_id", sa.Text),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("kind", sa.Text),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error_type", sa.Text),
        sa.Column("start_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("end_time", sa.TIMESTAMP(timezone=True)),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("service_name", sa.Text),
        sa.Column("environment", sa.Text),
        sa.Column("operation", sa.Text),
        sa.Column("provider", sa.Text),
        sa.Column("request_model", sa.Text),
        sa.Column("response_model", sa.Text),
        sa.Column("tool_name", sa.Text),
        sa.Column("tokens_in", sa.Integer),
        sa.Column("tokens_out", sa.Integer),
        sa.Column("session_id", sa.Text),
        sa.Column("user_id_hash", sa.Text),
        sa.Column("pending_until", sa.TIMESTAMP(timezone=True)),
        sa.UniqueConstraint("tenant_id", "trace_id", "span_id", name="ux_trace_spans_span"),
    )
    op.create_index("ix_trace_spans_tenant_start", TABLE, ["tenant_id", "start_time"])
    op.create_index(
        "ix_trace_spans_tenant_user",
        TABLE,
        ["tenant_id", "user_id_hash"],
        postgresql_where=sa.text("user_id_hash IS NOT NULL"),
    )
    op.create_index(
        "ix_trace_spans_tenant_session",
        TABLE,
        ["tenant_id", "session_id"],
        postgresql_where=sa.text("session_id IS NOT NULL"),
    )
    op.create_index(
        "ix_trace_spans_pending",
        TABLE,
        ["tenant_id", "pending_until"],
        postgresql_where=sa.text("pending_until IS NOT NULL"),
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
    op.drop_index("ix_trace_spans_pending", table_name=TABLE)
    op.drop_index("ix_trace_spans_tenant_session", table_name=TABLE)
    op.drop_index("ix_trace_spans_tenant_user", table_name=TABLE)
    op.drop_index("ix_trace_spans_tenant_start", table_name=TABLE)
    op.drop_table(TABLE)
