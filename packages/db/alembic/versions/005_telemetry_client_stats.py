"""Telemetry loss accounting

Adds telemetry_client_stats: per tenant and SDK client, the events the
collector received, the cumulative enqueued and dropped counts the SDK
reported in batch metadata, batches the collector rejected, and unknown
event fields it ignored (#87).

Chained onto 004; other open PRs also add a 005, so this may be renumbered
at merge.

Revision ID: 005
Revises: 004
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telemetry_client_stats",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            primary_key=True,
        ),
        sa.Column("client_id", sa.Text, primary_key=True),
        sa.Column("sdk", sa.Text),
        sa.Column("last_seq", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("received_events", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "received_batches", sa.BigInteger, nullable=False, server_default="0"
        ),
        sa.Column(
            "sdk_enqueued_total", sa.BigInteger, nullable=False, server_default="0"
        ),
        sa.Column("sdk_dropped", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "collector_rejected", postgresql.JSONB, nullable=False, server_default="{}"
        ),
        sa.Column(
            "ignored_fields", postgresql.JSONB, nullable=False, server_default="{}"
        ),
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


def downgrade() -> None:
    op.drop_table("telemetry_client_stats")
