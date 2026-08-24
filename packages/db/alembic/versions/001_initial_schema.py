"""Initial schema: tenants, ai_assets, ai_usage_events, data_flows, risk_mappings

Revision ID: 001
Revises:
Create Date: 2026-03-20

The pgvector `embedding` column and its HNSW index are added separately in
revision 002, so this revision runs on a stock PostgreSQL instance.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tenants',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('domain', sa.Text(), nullable=True),
        sa.Column('settings', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'ai_assets',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('vendor', sa.Text(), nullable=False),
        sa.Column('model', sa.Text(), nullable=True),
        sa.Column('use_case_name', sa.Text(), nullable=True),
        sa.Column('business_unit', sa.Text(), nullable=True),
        sa.Column('owner_email', sa.Text(), nullable=True),
        sa.Column('environment', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), nullable=True),
        sa.Column('data_classification', sa.Text(), nullable=True),
        sa.Column('discovery_source', postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column('confidence', sa.Text(), nullable=True),
        sa.Column('calling_service', sa.Text(), nullable=True),
        sa.Column('first_seen', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('last_seen', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ai_assets_tenant', 'ai_assets', ['tenant_id'])
    op.create_index('ix_ai_assets_tenant_bu', 'ai_assets', ['tenant_id', 'business_unit'])
    op.create_index('ix_ai_assets_tenant_vendor_model', 'ai_assets',
                    ['tenant_id', 'vendor', 'model'])
    op.create_index('ix_ai_assets_tenant_status', 'ai_assets', ['tenant_id', 'status'])
    op.create_index('ix_ai_assets_discovery_source', 'ai_assets', ['discovery_source'],
                    postgresql_using='gin')

    op.create_table(
        'ai_usage_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('asset_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('timestamp', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('tokens_in', sa.Integer(), nullable=True),
        sa.Column('tokens_out', sa.Integer(), nullable=True),
        sa.Column('cost', sa.Numeric(10, 6), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('source', sa.Text(), nullable=False),
        sa.Column('session_id', sa.Text(), nullable=True),
        sa.Column('tool_calls_used', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('prompt_hash', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['asset_id'], ['ai_assets.id']),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_usage_events_tenant_ts', 'ai_usage_events', ['tenant_id', 'timestamp'])
    op.create_index('ix_usage_events_asset_ts', 'ai_usage_events', ['asset_id', 'timestamp'])

    op.create_table(
        'data_flows',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('asset_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('source_system', sa.Text(), nullable=False),
        sa.Column('destination_system', sa.Text(), nullable=False),
        sa.Column('data_classification', sa.Text(), nullable=True),
        sa.Column('direction', sa.Text(), nullable=False),
        sa.Column('detected_pii_types', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(['asset_id'], ['ai_assets.id']),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_data_flows_tenant_asset', 'data_flows', ['tenant_id', 'asset_id'])

    op.create_table(
        'risk_mappings',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('asset_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('risk_category', sa.Text(), nullable=False),
        sa.Column('risk_level', sa.Text(), nullable=False),
        sa.Column('framework', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['asset_id'], ['ai_assets.id']),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_risk_mappings_tenant_asset', 'risk_mappings', ['tenant_id', 'asset_id'])
    op.create_index('ix_risk_mappings_tenant_cat', 'risk_mappings', ['tenant_id', 'risk_category'])


def downgrade() -> None:
    op.drop_table('risk_mappings')
    op.drop_table('data_flows')
    op.drop_table('ai_usage_events')
    op.drop_table('ai_assets')
    op.drop_table('tenants')
