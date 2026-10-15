"""Human attestation workflow

Adds asset_attestations: an append-only history of human sign-offs on an
asset field (the asset itself, its owner, its data classification, and any
field registered later such as risk classification). Each row records who,
when, the state (unreviewed | attested | disputed | retired), an optional
comment and a snapshot of the attested value. Triggers reject UPDATE, and
DELETE unless the transaction has set prompt_shields.erasing_tenant to the
row's tenant (tenant erasure), so history cannot be rewritten (#98).

Revision ID: 010
Revises: 009
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "010"
down_revision: str | None = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "asset_attestations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_assets.id"),
            nullable=False,
        ),
        sa.Column("field", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("value", postgresql.JSONB),
        sa.Column("comment", sa.Text),
        sa.Column("attested_by", sa.Text, nullable=False),
        sa.Column("actor_type", sa.Text, nullable=False),
        sa.Column(
            "partner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("partner_credentials.id"),
            nullable=True,
        ),
        sa.Column("api_key_prefix", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_asset_attestations_tenant_asset_field_ts",
        "asset_attestations",
        ["tenant_id", "asset_id", "field", "created_at"],
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION asset_attestations_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'asset_attestations rows are immutable; insert a new row instead';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER asset_attestations_no_update
        BEFORE UPDATE ON asset_attestations
        FOR EACH ROW EXECUTE FUNCTION asset_attestations_immutable()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION asset_attestations_no_delete() RETURNS trigger AS $$
        BEGIN
            IF current_setting('prompt_shields.erasing_tenant', true) = OLD.tenant_id::text THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'asset_attestations rows are append-only; only tenant erasure may delete them';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER asset_attestations_no_delete
        BEFORE DELETE ON asset_attestations
        FOR EACH ROW EXECUTE FUNCTION asset_attestations_no_delete()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS asset_attestations_no_delete ON asset_attestations")
    op.execute("DROP FUNCTION IF EXISTS asset_attestations_no_delete()")
    op.execute("DROP TRIGGER IF EXISTS asset_attestations_no_update ON asset_attestations")
    op.execute("DROP FUNCTION IF EXISTS asset_attestations_immutable()")
    op.drop_index(
        "ix_asset_attestations_tenant_asset_field_ts", table_name="asset_attestations"
    )
    op.drop_table("asset_attestations")
