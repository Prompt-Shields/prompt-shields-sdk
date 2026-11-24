"""Who created a credential, and who provisioned a principal (#396)

Disabling or deleting a principal revokes what its tokens created
(collector/idp_sync.py ``revoke_created_credentials``). Admin tokens and
SCIM tokens already record their creator (``created_by``: ``operator``,
``token:<lookup prefix>`` or ``key:<lookup prefix>``); tenant ingest keys
and partner credentials did not, so a key minted through an SSO session
outlived the principal. Adds:

  - ``credential_creators``: one row per ingest key or partner credential
    issued from now on (``credential_type``, ``credential_id``,
    ``created_by``). A table of its own rather than a column on the
    credential tables, so reading a credential never depends on it.
    Existing credentials get no row: their creator is unknown, and no
    cascade reaches them. Tenant table: the tenant_isolation policy (same
    SQL as 015) and, if RLS is already enforced on ai_assets, enforcement
    (same as 020).
  - ``principals.provisioned_by``: ``admin`` (admin API) or ``scim``
    (SCIM ``POST /Users``). A SCIM token may deactivate any user but only
    re-identify, re-enable or delete one SCIM provisioned
    (collector/scim.py). Existing principals with a SCIM ``user_name`` are
    backfilled as ``scim`` (the admin API cannot set ``user_name``).

Numbered 057 by assignment; chained from 055, main's head when written.
Re-chain onto main's real head at merge.

Revision ID: 057
Revises: 055
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "057"
down_revision: str | None = "055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
TABLE = "credential_creators"


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
        sa.Column("credential_type", sa.Text, nullable=False),
        sa.Column("credential_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "credential_type IN ('tenant_api_key', 'partner_credential')",
            name="ck_credential_creators_type",
        ),
    )
    op.create_index(
        "ux_credential_creators_credential",
        TABLE,
        ["credential_type", "credential_id"],
        unique=True,
    )
    op.create_index(
        "ix_credential_creators_tenant_created_by", TABLE, ["tenant_id", "created_by"]
    )
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

    op.add_column(
        "principals",
        sa.Column("provisioned_by", sa.Text, nullable=False, server_default="admin"),
    )
    op.create_check_constraint(
        "ck_principals_provisioned_by",
        "principals",
        "provisioned_by IN ('admin', 'scim')",
    )
    op.execute("UPDATE principals SET provisioned_by = 'scim' WHERE user_name IS NOT NULL")


def downgrade() -> None:
    op.drop_constraint("ck_principals_provisioned_by", "principals", type_="check")
    op.drop_column("principals", "provisioned_by")
    op.drop_table(TABLE)
