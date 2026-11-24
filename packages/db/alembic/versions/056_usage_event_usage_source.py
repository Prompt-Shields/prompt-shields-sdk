"""usage_source on usage events

Adds ai_usage_events.usage_source (#389): "provider" when the token counts
are the provider's, "estimated" when the SDK or the gateway estimated any of
them (a streamed call whose provider sent no usage, or a stream cut before
its final usage). Older SDKs send only tokens_estimated: true, which the
collector stores as "estimated".

Metadata only, on a table that already has its tenant_isolation policy and
is in the erasure order. Nullable with no default: existing rows and events
without the field are unaffected.

Revision ID: 056
Revises: 044
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "056"
down_revision: str | None = "044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_usage_events", sa.Column("usage_source", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_usage_events", "usage_source")
