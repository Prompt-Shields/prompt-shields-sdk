"""Data retention: audit log for purges and tenant erasure

Adds retention_audit_log, an append-only record of every retention purge,
tenant erasure and retention-setting change (#85). tenant_id has no foreign
key on purpose: the audit row must survive the erasure of its tenant.

Per-tenant retention itself needs no schema change; it lives in
tenants.settings->'retention_days'.

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
        "retention_audit_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("cutoff", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "details",
            postgresql.JSONB,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_retention_audit_tenant_ts",
        "retention_audit_log",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_retention_audit_tenant_ts", table_name="retention_audit_log")
    op.drop_table("retention_audit_log")
