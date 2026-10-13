"""Audit tenant registry reads in partner_audit_log

partner_audit_log already records every Partner API read. This widens it
so tenant `/api/v1/registry/*` reads can be recorded in the same table:

  - partner_id becomes nullable (tenant reads have no partner)
  - api_key_prefix : SHA-256 lookup prefix of the caller's key, never the key
  - asset_ids      : asset ids in the path or returned by the read (capped)
  - query_params   : query string with secrets redacted, free text hashed
  - ix_partner_audit_ts : lets the retention purge delete by timestamp

Downgrade deletes tenant-read rows (partner_id IS NULL) before restoring
NOT NULL, since they cannot satisfy it.

Revision ID: 005_registry_read_audit
Revises: 004
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "005_registry_read_audit"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("partner_audit_log", "partner_id", nullable=True)
    op.add_column("partner_audit_log", sa.Column("api_key_prefix", sa.Text))
    op.add_column(
        "partner_audit_log",
        sa.Column("asset_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True))),
    )
    op.add_column("partner_audit_log", sa.Column("query_params", postgresql.JSONB))
    op.create_index("ix_partner_audit_ts", "partner_audit_log", ["timestamp"])


def downgrade() -> None:
    op.drop_index("ix_partner_audit_ts", table_name="partner_audit_log")
    op.drop_column("partner_audit_log", "query_params")
    op.drop_column("partner_audit_log", "asset_ids")
    op.drop_column("partner_audit_log", "api_key_prefix")
    op.execute("DELETE FROM partner_audit_log WHERE partner_id IS NULL")
    op.alter_column("partner_audit_log", "partner_id", nullable=False)
