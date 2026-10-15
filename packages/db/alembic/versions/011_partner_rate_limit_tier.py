"""Partner rate-limit tiers

Adds partner_credentials.rate_limit_tier. NULL (every existing row) keeps
the credential's own rate_limit, so existing partners are unaffected.
Scopes need no schema change: the existing scopes array now holds
granular scopes, and `registry:read` stays valid as an alias (#106).

Revision ID: 011
Revises: 010
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "011"
down_revision: str | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "partner_credentials", sa.Column("rate_limit_tier", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("partner_credentials", "rate_limit_tier")
