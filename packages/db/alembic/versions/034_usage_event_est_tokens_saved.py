"""Estimated tokens saved by the gateway cache on usage events

Adds a nullable est_tokens_saved column to ai_usage_events: the gateway's
estimate of the prompt tokens a cache lookup saved (0 on a miss), which the
collector used to drop and count in ignored_fields (#33).

Metadata only: no prompt or response content. No new table: ai_usage_events
already has its tenant_isolation policy (015) and is in the erasure order.
Nullable with no default, so existing rows and old events are unaffected.

Revision ID: 034
Revises: 032
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "034"
down_revision: str | None = "032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_usage_events", sa.Column("est_tokens_saved", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_usage_events", "est_tokens_saved")
