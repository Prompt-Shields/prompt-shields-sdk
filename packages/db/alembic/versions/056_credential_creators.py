"""Who created a credential, and who provisioned a principal (#396)

Disabling or deleting a principal revokes what its tokens created
(collector/idp_sync.py ``revoke_created_credentials``). Admin tokens and
SCIM tokens already record their creator (``created_by``: ``operator``,
``token:<lookup prefix>`` or ``key:<lookup prefix>``); tenant ingest keys
and partner credentials did not, so a key minted through an SSO session
outlived the principal. Adds:

  - ``tenant_api_keys.created_by`` and ``partner_credentials.created_by``
    (nullable text, same values as ``admin_tokens.created_by``). Existing
    rows keep NULL: their creator is unknown, and no cascade reaches them.
  - ``principals.provisioned_by``: ``admin`` (admin API) or ``scim``
    (SCIM ``POST /Users``). A SCIM token may deactivate any user but only
    re-identify, re-enable or delete one SCIM provisioned
    (collector/scim.py). Existing principals with a SCIM ``user_name`` are
    backfilled as ``scim`` (the admin API cannot set ``user_name``).

No new tables, so no RLS changes.

Numbered 056 by assignment; chained from 045, main's head when written.
Re-chain onto main's real head at merge.

Revision ID: 056
Revises: 045
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "056"
down_revision: str | None = "045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenant_api_keys", sa.Column("created_by", sa.Text, nullable=True))
    op.add_column("partner_credentials", sa.Column("created_by", sa.Text, nullable=True))
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
    op.drop_column("partner_credentials", "created_by")
    op.drop_column("tenant_api_keys", "created_by")
