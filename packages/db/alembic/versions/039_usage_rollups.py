"""Usage rollups: hourly and daily aggregates that outlive raw events (#346)

Adds:

* ``ai_usage_events.ingested_at``: the database's now() when the row was
  stored (server default; existing rows get the migration time, which makes
  the first rollup run a full backfill of the raw events still kept). Index
  on (tenant_id, ingested_at) so the rollup job finds new events per tenant.
* ``usage_rollup_hourly`` and ``usage_rollup_daily``: one row per (tenant,
  UTC hour or day, asset, model, provider, API surface, user id hash) with
  request, token, cost, cache and error counts. Numbers and HMAC hashes
  only, never raw user ids or content. Kept PS_USAGE_ROLLUP_RETENTION_DAYS
  (default 760, about 25 months), separately from the raw events.
* ``usage_rollup_state``: per-tenant watermark of the rollup job.

The new tables get the tenant_isolation policy (same SQL as 015) and, if
RLS is already enforced on ai_assets, enforcement too (same as 020).

Numbered 039 by assignment; chained from 037 (main's head when written).
Re-chain at merge if 038 (#338) lands first.

Revision ID: 039
Revises: 037
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "039"
down_revision: str | None = "037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
ROLLUP_TABLES = ("usage_rollup_hourly", "usage_rollup_daily")
STATE_TABLE = "usage_rollup_state"
COUNTS = (
    "requests",
    "tokens_in",
    "tokens_out",
    "tokens_cached",
    "tokens_cache_write",
    "tokens_reasoning",
    "cost_requests",
    "cache_lookups",
    "cache_hits",
    "attempts_reported",
    "error_requests",
)


def _expr(column: str) -> str:
    return (
        "(coalesce(current_setting('app.rls_bypass', true), '') <> '' "
        f"OR {column} = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )


def _protect(table: str) -> None:
    op.execute(
        f"CREATE POLICY {POLICY} ON {table} FOR ALL "
        f"USING {_expr('tenant_id')} WITH CHECK {_expr('tenant_id')}"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF (SELECT relrowsecurity FROM pg_class "
        "WHERE oid = to_regclass('ai_assets')) THEN "
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY; "
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY; "
        "END IF; END $$"
    )


def _rollup_table(name: str) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column(
            "tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
        ),
        sa.Column("bucket_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("asset_id", UUID(as_uuid=True)),
        sa.Column("model", sa.Text),
        sa.Column("provider", sa.Text),
        sa.Column("api_surface", sa.Text),
        sa.Column("user_id_hash", sa.Text),
        *(
            sa.Column(c, sa.BigInteger, nullable=False, server_default="0")
            for c in COUNTS
        ),
        sa.Column("cost", sa.Numeric(20, 6), nullable=False, server_default="0"),
        sa.Column(
            "cache_saved_cost", sa.Numeric(20, 6), nullable=False, server_default="0"
        ),
    )
    op.create_index(f"ix_{name}_tenant_bucket", name, ["tenant_id", "bucket_start"])
    _protect(name)


def upgrade() -> None:
    # now() is stable, so this is a fast default: no table rewrite.
    op.add_column(
        "ai_usage_events",
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_usage_events_tenant_ingested", "ai_usage_events", ["tenant_id", "ingested_at"]
    )
    for name in ROLLUP_TABLES:
        _rollup_table(name)
    op.create_table(
        STATE_TABLE,
        sa.Column(
            "tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True
        ),
        sa.Column("watermark", sa.TIMESTAMP(timezone=True)),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    _protect(STATE_TABLE)


def downgrade() -> None:
    op.drop_table(STATE_TABLE)
    for name in reversed(ROLLUP_TABLES):
        op.drop_table(name)
    op.drop_index("ix_usage_events_tenant_ingested", table_name="ai_usage_events")
    op.drop_column("ai_usage_events", "ingested_at")
