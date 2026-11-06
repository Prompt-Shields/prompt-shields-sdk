"""spend anomaly alerts (#30)

Adds ``spend_alerts``: one row per (tenant, group_by, group_key, day) whose
spend the anomaly job (``python -m collector.spend_anomalies``) found far
above its EWMA baseline. Numbers only; acknowledged through the Partner API.

The new table gets the tenant_isolation policy (same SQL as 015) and, if
RLS is already enforced on ai_assets, enforcement too (same as 020).

Revision ID: 029
Revises: 027
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "029"
# Chains from 027 until #262 (028, outcome signals) merges; re-chain to
# "028" then.
down_revision: str | None = "027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
TABLE = "spend_alerts"


def _expr(column: str) -> str:
    return (
        "(coalesce(current_setting('app.rls_bypass', true), '') <> '' "
        f"OR {column} = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("group_by", sa.Text, nullable=False),
        sa.Column("group_key", sa.Text, nullable=False),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("observed_cost", sa.Numeric(14, 6), nullable=False),
        sa.Column("baseline_cost", sa.Float, nullable=False),
        sa.Column("baseline_sigma", sa.Float, nullable=False),
        sa.Column("z_score", sa.Float, nullable=False),
        sa.Column("threshold_sigma", sa.Float, nullable=False),
        sa.Column("history_days", sa.Integer, nullable=False),
        sa.Column("severity", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("acknowledged_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("acknowledged_by", sa.Text),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "group_by", "group_key", "day", name="ux_spend_alerts_group_day"
        ),
        sa.CheckConstraint(
            "status IN ('open', 'acknowledged')", name="ck_spend_alerts_status"
        ),
    )
    op.create_index("ix_spend_alerts_tenant_created", TABLE, ["tenant_id", "created_at"])
    op.create_index("ix_spend_alerts_tenant_day", TABLE, ["tenant_id", "day"])

    op.execute(
        f"CREATE POLICY {POLICY} ON {TABLE} FOR ALL "
        f"USING {_expr('tenant_id')} WITH CHECK {_expr('tenant_id')}"
    )
    # Match the enforcement state of the existing tenant tables.
    op.execute(
        "DO $$ BEGIN "
        "IF (SELECT relrowsecurity FROM pg_class "
        "WHERE oid = to_regclass('ai_assets')) THEN "
        f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY; "
        f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.drop_index("ix_spend_alerts_tenant_day", table_name=TABLE)
    op.drop_index("ix_spend_alerts_tenant_created", table_name=TABLE)
    op.drop_table(TABLE)
