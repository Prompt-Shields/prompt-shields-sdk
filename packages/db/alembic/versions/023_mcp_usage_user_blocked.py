"""MCP tool usage: blocked outcome and pseudonymous user attribution (#63, #77)

Adds to ``mcp_tool_usage``:

- ``blocked_count``: calls a guardrail blocked at the gateway's MCP proxy.
  Before this revision the gateway reported them as errors, so existing
  ``error_count`` values include them; new ones do not.
- ``user_hash`` and ``user_group``: the caller as the gateway's OIDC subject
  hash (hex) and policy group name, "" when not reported. Both join the
  upsert key, so the registry can say who uses a tool. Never a raw subject
  or an email address: ``/ingest/mcp`` drops a ``user_id`` that is not a
  hex hash.

The table already has its tenant_isolation policy (020); added columns are
covered by it.

Revision ID: 023
Revises: 022
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "023"
down_revision: str | None = "022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "mcp_tool_usage"
KEY_INDEX = "ux_mcp_tool_usage_key"
OLD_KEY = [
    "tenant_id",
    "source",
    "server_name",
    "tool_name",
    "calling_service",
    "environment",
]
NEW_KEY = [*OLD_KEY, "user_hash", "user_group"]


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("blocked_count", sa.BigInteger, nullable=False, server_default="0"),
    )
    op.add_column(
        TABLE, sa.Column("user_hash", sa.Text, nullable=False, server_default="")
    )
    op.add_column(
        TABLE, sa.Column("user_group", sa.Text, nullable=False, server_default="")
    )
    op.drop_index(KEY_INDEX, table_name=TABLE)
    op.create_index(KEY_INDEX, TABLE, NEW_KEY, unique=True)


def downgrade() -> None:
    # Fold per-user rows back into one row per old key before the old
    # unique index is restored; blocked calls count as errors again.
    keys = ", ".join(OLD_KEY)
    op.execute(
        f"""
        WITH merged AS (
            SELECT {keys},
                   (array_agg(id ORDER BY last_seen DESC, id))[1] AS keep_id,
                   SUM(call_count) AS call_count,
                   SUM(error_count + blocked_count) AS error_count,
                   SUM(latency_ms_total) AS latency_ms_total,
                   SUM(latency_count) AS latency_count,
                   MIN(first_seen) AS first_seen,
                   MAX(last_seen) AS last_seen
            FROM {TABLE}
            GROUP BY {keys}
        ),
        updated AS (
            UPDATE {TABLE} t
            SET call_count = m.call_count,
                error_count = m.error_count,
                latency_ms_total = m.latency_ms_total,
                latency_count = m.latency_count,
                first_seen = m.first_seen,
                last_seen = m.last_seen
            FROM merged m
            WHERE t.id = m.keep_id
            RETURNING t.id
        )
        DELETE FROM {TABLE}
        WHERE id NOT IN (SELECT keep_id FROM merged)
        """
    )
    op.drop_index(KEY_INDEX, table_name=TABLE)
    op.create_index(KEY_INDEX, TABLE, OLD_KEY, unique=True)
    op.drop_column(TABLE, "user_group")
    op.drop_column(TABLE, "user_hash")
    op.drop_column(TABLE, "blocked_count")
