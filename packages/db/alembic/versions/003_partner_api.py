"""Partner API — credentials and audit log

Adds the two tables that back the /partner/v1/* surface:

  - partner_credentials : OAuth client + optional API key per tenant/partner
  - partner_audit_log   : append-only per-request log for compliance

Revision ID: 003
Revises: 002
Create Date: 2026-05-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "partner_credentials",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("partner_name", sa.Text, nullable=False),
        sa.Column("client_id", sa.Text, nullable=False, unique=True),
        sa.Column("client_secret_hash", sa.Text, nullable=False),
        sa.Column("api_key_hash", sa.Text, nullable=True),
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("ARRAY['registry:read']::text[]"),
        ),
        sa.Column("rate_limit", sa.Integer, server_default="1000"),
        sa.Column("is_active", sa.Text, server_default="true"),
        sa.Column(
            "partner_metadata",
            postgresql.JSONB,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_partner_credentials_tenant", "partner_credentials", ["tenant_id"])
    op.create_index("ix_partner_credentials_client_id", "partner_credentials", ["client_id"])
    op.create_index("ix_partner_credentials_name", "partner_credentials", ["partner_name"])

    op.create_table(
        "partner_audit_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "partner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("partner_credentials.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("endpoint", sa.Text, nullable=False),
        sa.Column("method", sa.Text, nullable=False),
        sa.Column("status_code", sa.Integer, nullable=False),
        sa.Column(
            "timestamp",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("response_count", sa.Integer),
        sa.Column("ip_address", sa.Text),
        sa.Column("user_agent", sa.Text),
    )
    op.create_index(
        "ix_partner_audit_tenant_ts",
        "partner_audit_log",
        ["tenant_id", "timestamp"],
    )
    op.create_index(
        "ix_partner_audit_partner_ts",
        "partner_audit_log",
        ["partner_id", "timestamp"],
    )


def downgrade() -> None:
    op.drop_table("partner_audit_log")
    op.drop_table("partner_credentials")
