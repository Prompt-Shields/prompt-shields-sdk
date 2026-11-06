"""Routing learner snapshots and recommendations (#47, #48)

Adds ``learner_snapshots`` (one versioned, content-addressed snapshot of a
tenant's offline routing learner per row) and ``learner_recommendations``
(the route changes a snapshot proposes, each with its off-policy
projection and the human accept/reject decision). Fed by
``POST /ingest/learner-snapshots``; read and reviewed on the Partner API.

Both tables get the tenant_isolation policy (same SQL as 015) and, if RLS
is already enforced on ai_assets, enforcement too (same as 020/027).

Revision ID: 030
Revises: 029
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "030"
down_revision: str | None = "029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
SNAPSHOTS = "learner_snapshots"
RECOMMENDATIONS = "learner_recommendations"


def _expr(column: str) -> str:
    return (
        "(coalesce(current_setting('app.rls_bypass', true), '') <> '' "
        f"OR {column} = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )


def _tenant_fk() -> sa.Column:
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id"),
        nullable=False,
    )


def _created_at() -> sa.Column:
    return sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False)


def upgrade() -> None:
    op.create_table(
        SNAPSHOTS,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column("learner_version", sa.Text, nullable=False),
        sa.Column("parent_version", sa.Text),
        sa.Column("mode", sa.Text, nullable=False),
        sa.Column("algorithm", sa.Text, nullable=False),
        sa.Column("feature_schema", sa.Text),
        sa.Column("policy_version", sa.Text),
        sa.Column("data_fingerprint", sa.Text, nullable=False),
        sa.Column("training_rows", sa.Integer, nullable=False),
        sa.Column("scored_rows", sa.Integer, nullable=False),
        sa.Column("snapshot", postgresql.JSONB, nullable=False),
        _created_at(),
        sa.UniqueConstraint(
            "tenant_id", "learner_version", name="ux_learner_snapshots_version"
        ),
    )
    op.create_index(
        "ix_learner_snapshots_tenant_created", SNAPSHOTS, ["tenant_id", "created_at"]
    )

    op.create_table(
        RECOMMENDATIONS,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("learner_snapshots.id"),
            nullable=False,
        ),
        sa.Column("learner_version", sa.Text, nullable=False),
        sa.Column("use_case", sa.Text),
        sa.Column("sensitivity_level", sa.Text, nullable=False),
        sa.Column("current_model", sa.Text, nullable=False),
        sa.Column("recommended_model", sa.Text, nullable=False),
        sa.Column("candidates", postgresql.JSONB, nullable=False),
        sa.Column("projection", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("reviewed_by", sa.Text),
        sa.Column("reviewed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("review_comment", sa.Text),
        sa.Column("reviewer_actor_type", sa.Text),
        sa.Column(
            "reviewer_partner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("partner_credentials.id"),
            nullable=True,
        ),
        _created_at(),
    )
    op.create_index(
        "ix_learner_recommendations_tenant_status",
        RECOMMENDATIONS,
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_learner_recommendations_snapshot", RECOMMENDATIONS, ["snapshot_id"]
    )

    for table in (SNAPSHOTS, RECOMMENDATIONS):
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} FOR ALL "
            f"USING {_expr('tenant_id')} WITH CHECK {_expr('tenant_id')}"
        )
        # Match the enforcement state of the existing tenant tables.
        op.execute(
            "DO $$ BEGIN "
            "IF (SELECT relrowsecurity FROM pg_class "
            "WHERE oid = to_regclass('ai_assets')) THEN "
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY; "
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY; "
            "END IF; END $$"
        )


def downgrade() -> None:
    op.drop_index("ix_learner_recommendations_snapshot", table_name=RECOMMENDATIONS)
    op.drop_index("ix_learner_recommendations_tenant_status", table_name=RECOMMENDATIONS)
    op.drop_table(RECOMMENDATIONS)
    op.drop_index("ix_learner_snapshots_tenant_created", table_name=SNAPSHOTS)
    op.drop_table(SNAPSHOTS)
