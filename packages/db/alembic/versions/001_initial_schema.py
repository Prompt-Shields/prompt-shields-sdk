"""Initial schema — tenants, AI assets, usage events, data flows, risk mappings

This is the base revision the chain has always assumed but never contained:
002 declares `down_revision = '001'`, and no such file was committed, so
`alembic upgrade head` failed with KeyError: '001' on every clean clone.

Reconstructed from packages/db/models.py as it stands before the two later
migrations, so that the chain composes correctly:

  001 (here) : uuid-ossp extension, the five core tables and their indexes
  002        : pgvector extension, ai_assets.embedding, HNSW index
  003        : partner_credentials, partner_audit_log

The ai_assets.embedding column and its index are therefore deliberately
absent here; 002 adds them. The uuid-ossp extension is created here because
003 relies on uuid_generate_v4() as a server default and nothing else
installs it.

Revision ID: 001
Revises:
Create Date: 2026-08-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')

    op.create_table(
        "tenants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("domain", sa.Text),
        sa.Column("settings", JSONB),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True)),
    )

    op.create_table(
        "ai_assets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("vendor", sa.Text, nullable=False),
        sa.Column("model", sa.Text),
        sa.Column("use_case_name", sa.Text),
        sa.Column("business_unit", sa.Text),
        sa.Column("owner_email", sa.Text),
        sa.Column("environment", sa.Text),
        sa.Column("status", sa.Text),
        sa.Column("data_classification", sa.Text),
        sa.Column("discovery_source", ARRAY(sa.Text), nullable=False),
        sa.Column("confidence", sa.Text),
        sa.Column("calling_service", sa.Text),
        sa.Column("first_seen", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_seen", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True)),
    )
    op.create_index("ix_ai_assets_tenant", "ai_assets", ["tenant_id"])
    op.create_index("ix_ai_assets_tenant_bu", "ai_assets", ["tenant_id", "business_unit"])
    op.create_index(
        "ix_ai_assets_tenant_vendor_model", "ai_assets", ["tenant_id", "vendor", "model"]
    )
    op.create_index("ix_ai_assets_tenant_status", "ai_assets", ["tenant_id", "status"])
    op.create_index(
        "ix_ai_assets_discovery_source",
        "ai_assets",
        ["discovery_source"],
        postgresql_using="gin",
    )

    op.create_table(
        "ai_usage_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
        ),
        sa.Column(
            "asset_id", UUID(as_uuid=True), sa.ForeignKey("ai_assets.id"), nullable=False
        ),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("tokens_in", sa.Integer),
        sa.Column("tokens_out", sa.Integer),
        sa.Column("cost", sa.Numeric(10, 6)),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("session_id", sa.Text),
        sa.Column("tool_calls_used", JSONB),
        sa.Column("prompt_hash", sa.Text),
    )
    op.create_index(
        "ix_usage_events_tenant_ts", "ai_usage_events", ["tenant_id", "timestamp"]
    )
    op.create_index(
        "ix_usage_events_asset_ts", "ai_usage_events", ["asset_id", "timestamp"]
    )

    op.create_table(
        "data_flows",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
        ),
        sa.Column(
            "asset_id", UUID(as_uuid=True), sa.ForeignKey("ai_assets.id"), nullable=False
        ),
        sa.Column("source_system", sa.Text, nullable=False),
        sa.Column("destination_system", sa.Text, nullable=False),
        sa.Column("data_classification", sa.Text),
        sa.Column("direction", sa.Text, nullable=False),
        sa.Column("detected_pii_types", JSONB),
    )
    op.create_index(
        "ix_data_flows_tenant_asset", "data_flows", ["tenant_id", "asset_id"]
    )

    op.create_table(
        "risk_mappings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
        ),
        sa.Column(
            "asset_id", UUID(as_uuid=True), sa.ForeignKey("ai_assets.id"), nullable=False
        ),
        sa.Column("risk_category", sa.Text, nullable=False),
        sa.Column("risk_level", sa.Text, nullable=False),
        sa.Column("framework", sa.Text),
    )
    op.create_index(
        "ix_risk_mappings_tenant_asset", "risk_mappings", ["tenant_id", "asset_id"]
    )
    op.create_index(
        "ix_risk_mappings_tenant_cat", "risk_mappings", ["tenant_id", "risk_category"]
    )


def downgrade() -> None:
    op.drop_table("risk_mappings")
    op.drop_table("data_flows")
    op.drop_table("ai_usage_events")
    op.drop_table("ai_assets")
    op.drop_table("tenants")
