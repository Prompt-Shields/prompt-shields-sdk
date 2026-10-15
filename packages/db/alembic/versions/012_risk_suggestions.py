"""Rule-based risk classification suggestions

Adds to risk_mappings: status (suggested | confirmed | rejected, existing rows
become 'confirmed'), source (manual | rules, existing rows 'manual'),
rule_id, ruleset_version, framework_ref, confidence, evidence (JSONB),
created_at, updated_at and reviewed_at. A partial unique index on
(tenant_id, asset_id, rule_id) is the suggestion engine's upsert key.

Revision ID: 012
Revises: 011
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "012"
down_revision: str | None = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "risk_mappings",
        sa.Column("status", sa.Text, nullable=False, server_default="confirmed"),
    )
    op.add_column(
        "risk_mappings",
        sa.Column("source", sa.Text, nullable=False, server_default="manual"),
    )
    for name in ("rule_id", "ruleset_version", "framework_ref", "confidence"):
        op.add_column("risk_mappings", sa.Column(name, sa.Text, nullable=True))
    op.add_column("risk_mappings", sa.Column("evidence", JSONB, nullable=True))
    for name in ("created_at", "updated_at", "reviewed_at"):
        op.add_column(
            "risk_mappings", sa.Column(name, sa.TIMESTAMP(timezone=True), nullable=True)
        )
    op.create_index(
        "ix_risk_mappings_tenant_status", "risk_mappings", ["tenant_id", "status"]
    )
    op.create_index(
        "ux_risk_mappings_rule",
        "risk_mappings",
        ["tenant_id", "asset_id", "rule_id"],
        unique=True,
        postgresql_where=sa.text("rule_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_risk_mappings_rule", table_name="risk_mappings")
    op.drop_index("ix_risk_mappings_tenant_status", table_name="risk_mappings")
    # Without a status column, unreviewed or rejected suggestions would read
    # as confirmed risks, so they go; confirmed and manual rows stay.
    op.execute("DELETE FROM risk_mappings WHERE status IN ('suggested', 'rejected')")
    for name in (
        "reviewed_at",
        "updated_at",
        "created_at",
        "evidence",
        "confidence",
        "framework_ref",
        "ruleset_version",
        "rule_id",
        "source",
        "status",
    ):
        op.drop_column("risk_mappings", name)
