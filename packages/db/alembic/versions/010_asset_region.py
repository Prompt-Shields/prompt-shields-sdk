"""Asset region

Adds ai_assets.region: the cloud region an asset is deployed in (e.g.
``swedencentral``), filled by the cloud AI inventory platform signal and
used for sovereignty reporting (#94). Nullable; existing rows stay NULL.

Revision ID: 010
Revises: 009
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "010"
down_revision: str | None = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_assets", sa.Column("region", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("ai_assets", "region")
