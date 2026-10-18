"""Owner-retired assets

Adds two nullable columns to ai_assets for owner-initiated retirement
(an `asset` attestation with state `retired`, #100):

  - retired_by        : who retired the asset (the attestation's
                        attested_by). Non-null means owner-retired: the
                        lifecycle sweep leaves the asset alone and new
                        telemetry does not reactivate it.
  - retired_in_use_at : first sighting after the owner retired it (the
                        "retired asset still in use" signal). Cleared when
                        the owner retires it again or un-retires it.

When it was retired is `lifecycle_changed_at`. No new table: ai_assets
already has its tenant_isolation policy (015) and is in the erasure order.
Both columns are nullable with no default, so existing rows are unaffected.

Revision ID: 019
Revises: 018
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "019"
down_revision: str | None = "018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_assets", sa.Column("retired_by", sa.Text(), nullable=True))
    op.add_column(
        "ai_assets",
        sa.Column("retired_in_use_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_assets", "retired_in_use_at")
    op.drop_column("ai_assets", "retired_by")
