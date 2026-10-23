"""Risk mapping review history and reviewer comment (#98)

Adds ``risk_mappings.review_comment`` (the comment on the latest review)
and ``risk_mapping_reviews``: an append-only history of human reviews of a
risk mapping. Each row records who (the named reviewer plus the tenant key,
admin token or partner that called), when, the status chosen and the one it
replaced, an optional comment and a snapshot of the mapping. Triggers reject
UPDATE, and DELETE unless the transaction has set
prompt_shields.erasing_tenant to the row's tenant (tenant erasure), the same
as asset_attestations (012).

``risk_mapping_id`` and ``asset_id`` deliberately have no foreign key: a
duplicate-asset merge can drop a mapping or move it to another asset, and
the history must outlive both.

Existing reviews (``reviewed_at`` set) are backfilled as one history row
each, with ``previous_status`` NULL since the earlier state is unknown.

The table gets the tenant_isolation policy (same SQL as 015), enforced if
RLS is already enforced on ai_assets (same as 020).

Revision ID: 025
Revises: 022 (renumbered at merge; other branches add 023 and 024)
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "025"
down_revision: str | None = "022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
TABLE = "risk_mapping_reviews"


def _expr(column: str) -> str:
    return (
        "(coalesce(current_setting('app.rls_bypass', true), '') <> '' "
        f"OR {column} = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )


def upgrade() -> None:
    op.add_column("risk_mappings", sa.Column("review_comment", sa.Text))
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("risk_mapping_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("previous_status", sa.Text),
        sa.Column("comment", sa.Text),
        sa.Column("reviewed_by", sa.Text, nullable=False),
        sa.Column("actor_type", sa.Text, nullable=False),
        sa.Column(
            "partner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("partner_credentials.id"),
            nullable=True,
        ),
        sa.Column("api_key_prefix", sa.Text),
        sa.Column("snapshot", postgresql.JSONB),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_risk_mapping_reviews_tenant_mapping_ts",
        TABLE,
        ["tenant_id", "risk_mapping_id", "created_at"],
    )

    # Backfill reviews made before this table existed (one row each).
    op.execute("SELECT set_config('app.rls_bypass', 'migration-025', true)")
    op.execute(
        f"""
        INSERT INTO {TABLE} (
            id, tenant_id, risk_mapping_id, asset_id, status, previous_status,
            comment, reviewed_by, actor_type, api_key_prefix, snapshot, created_at
        )
        SELECT gen_random_uuid(), tenant_id, id, asset_id, status, NULL,
               NULL, coalesce(reviewed_by, 'unknown'), 'tenant_key',
               reviewed_key_prefix,
               jsonb_build_object(
                   'risk_category', risk_category,
                   'risk_level', risk_level,
                   'framework', framework,
                   'rule_id', rule_id,
                   'ruleset_version', ruleset_version,
                   'framework_ref', framework_ref
               ),
               reviewed_at
        FROM risk_mappings
        WHERE reviewed_at IS NOT NULL AND status IN ('confirmed', 'rejected')
        """
    )
    op.execute("SELECT set_config('app.rls_bypass', '', true)")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION risk_mapping_reviews_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'risk_mapping_reviews rows are immutable; insert a new row instead';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER risk_mapping_reviews_no_update
        BEFORE UPDATE ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION risk_mapping_reviews_immutable()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION risk_mapping_reviews_no_delete() RETURNS trigger AS $$
        BEGIN
            IF current_setting('prompt_shields.erasing_tenant', true) = OLD.tenant_id::text THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'risk_mapping_reviews rows are append-only; only tenant erasure may delete them';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER risk_mapping_reviews_no_delete
        BEFORE DELETE ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION risk_mapping_reviews_no_delete()
        """
    )

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
    op.execute(f"DROP TRIGGER IF EXISTS risk_mapping_reviews_no_delete ON {TABLE}")
    op.execute("DROP FUNCTION IF EXISTS risk_mapping_reviews_no_delete()")
    op.execute(f"DROP TRIGGER IF EXISTS risk_mapping_reviews_no_update ON {TABLE}")
    op.execute("DROP FUNCTION IF EXISTS risk_mapping_reviews_immutable()")
    op.drop_index("ix_risk_mapping_reviews_tenant_mapping_ts", table_name=TABLE)
    op.drop_table(TABLE)
    op.drop_column("risk_mappings", "review_comment")
