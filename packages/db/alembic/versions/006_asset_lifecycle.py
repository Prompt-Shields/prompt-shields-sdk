"""Asset lifecycle — stale and retired detection

Adds telemetry-driven lifecycle tracking to ai_assets:

  - lifecycle_state      : active | stale | retired (default active)
  - lifecycle_changed_at : when lifecycle_state last changed

Existing rows start as `active`; the first lifecycle sweep moves them to
`stale` or `retired` based on `last_seen`.

Revision ID: 006
Revises: 005
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_assets",
        sa.Column("lifecycle_state", sa.Text, nullable=False, server_default="active"),
    )
    op.add_column(
        "ai_assets",
        sa.Column("lifecycle_changed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_ai_assets_tenant_lifecycle", "ai_assets", ["tenant_id", "lifecycle_state"]
    )


def downgrade() -> None:
    op.drop_index("ix_ai_assets_tenant_lifecycle", table_name="ai_assets")
    op.drop_column("ai_assets", "lifecycle_changed_at")
    op.drop_column("ai_assets", "lifecycle_state")
