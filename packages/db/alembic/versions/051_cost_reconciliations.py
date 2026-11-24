"""Monthly cost reconciliation results (#348)

Adds ``cost_reconciliations``: one row per (tenant, month, provider,
model) comparing the usage the SDK and gateway reported with the usage the
provider's own usage API reports (collector/cost_reconciliation.py), and
whether the gap is over the tenant's threshold. Numbers only.

The table gets the tenant_isolation policy (same SQL as 015) and, if RLS is
already enforced on ai_assets, enforcement too (same as 020).

Numbered 051 by assignment; chained from 055, main's head at review
(043 -> 054 -> 045 -> 055). Re-chain onto main's head at merge if other
migrations land first.

Revision ID: 051
Revises: 055
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "051"
down_revision: str | None = "055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
TABLE = "cost_reconciliations"


def _expr(column: str) -> str:
    return (
        "(coalesce(current_setting('app.rls_bypass', true), '') <> '' "
        f"OR {column} = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
        ),
        sa.Column("month", sa.Date, nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("estimated_tokens_in", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("estimated_tokens_out", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("estimated_cost", sa.Numeric(20, 6)),
        sa.Column("provider_tokens_in", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("provider_tokens_out", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("basis", sa.Text, nullable=False),
        sa.Column("estimated_value", sa.Numeric(20, 6), nullable=False),
        sa.Column("provider_value", sa.Numeric(20, 6), nullable=False),
        sa.Column("variance_pct", sa.Float),
        sa.Column("threshold_pct", sa.Float, nullable=False),
        sa.Column("flagged", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "month", "provider", "model", name="ux_cost_reconciliations_key"
        ),
        sa.CheckConstraint("basis IN ('price', 'tokens')", name="ck_cost_reconciliations_basis"),
    )
    op.create_index("ix_cost_reconciliations_tenant_month", TABLE, ["tenant_id", "month"])
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
    op.drop_table(TABLE)
