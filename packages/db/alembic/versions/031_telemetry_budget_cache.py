"""Spend budget and cache fields on usage events

Adds nullable columns to ai_usage_events for telemetry fields that used to
be dropped (and counted in ignored_fields):

- budget_state, budget_id, budget_action: the spend budget decision the SDK
  (#29) and the gateway (#28) attach to a call (schemas/budget/README.md).
- cache_status, cache_saved_cost: the gateway cache status of the call and,
  on a hit, the upstream cost it avoided (#33).

Metadata only: no prompt or response content. No new table: ai_usage_events
already has its tenant_isolation policy (015) and is in the erasure order.
All columns are nullable with no default, so existing rows and old events
are unaffected.

Revision ID: 031
Revises: 030
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "031"
down_revision: str | None = "030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BUDGET_CACHE_EVENT_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("budget_state", sa.Text()),
    ("budget_id", sa.Text()),
    ("budget_action", sa.Text()),
    ("cache_status", sa.Text()),
    ("cache_saved_cost", sa.Numeric(10, 6)),
)


def upgrade() -> None:
    for name, type_ in BUDGET_CACHE_EVENT_COLUMNS:
        op.add_column("ai_usage_events", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(BUDGET_CACHE_EVENT_COLUMNS):
        op.drop_column("ai_usage_events", name)
