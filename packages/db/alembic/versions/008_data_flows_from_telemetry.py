"""Derive data flows from telemetry

Adds ai_usage_events.flow_context (JSONB: data sources, output destination,
PII categories, region, classification, calling service) so flows can be
rebuilt from stored events, and data_flows.event_count / first_seen /
last_seen. A unique index on (tenant_id, asset_id, source_system,
destination_system, direction) is the upsert key; duplicate rows that would
violate it are collapsed to one first.

Revision ID: 008
Revises: 007
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_usage_events", sa.Column("flow_context", JSONB, nullable=True))

    op.add_column(
        "data_flows",
        sa.Column("event_count", sa.BigInteger, nullable=False, server_default="0"),
    )
    op.add_column(
        "data_flows", sa.Column("first_seen", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column(
        "data_flows", sa.Column("last_seen", sa.TIMESTAMP(timezone=True), nullable=True)
    )

    op.execute(
        "DELETE FROM data_flows a USING data_flows b "
        "WHERE a.ctid > b.ctid "
        "AND a.tenant_id = b.tenant_id AND a.asset_id = b.asset_id "
        "AND a.source_system = b.source_system "
        "AND a.destination_system = b.destination_system "
        "AND a.direction = b.direction"
    )
    op.create_index(
        "ux_data_flows_flow_key",
        "data_flows",
        ["tenant_id", "asset_id", "source_system", "destination_system", "direction"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_data_flows_flow_key", table_name="data_flows")
    op.drop_column("data_flows", "last_seen")
    op.drop_column("data_flows", "first_seen")
    op.drop_column("data_flows", "event_count")
    op.drop_column("ai_usage_events", "flow_context")
