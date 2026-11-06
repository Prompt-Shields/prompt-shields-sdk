"""Routing learner auto-mode fields on usage events

Adds nullable columns to ai_usage_events for the per-call fields the SDK
router sends when the routing learner decided a call in auto mode (#34) and
the collector used to drop (and count in ignored_fields):

- route_learner_mode: policy, exploit or explore.
- route_propensity: the probability the learner picked the served model.
- route_learner_version: the active snapshot's learner_version
  (lv1-<16 hex>), null when no snapshot was active (e.g. after a rollback
  to the plain policy).
- route_learner_frozen: learning was frozen (drift or poisoning) when the
  call was routed.

Metadata only: no prompt or response content. No new table: ai_usage_events
already has its tenant_isolation policy (015) and is in the erasure order.
All columns are nullable with no default, so existing rows and old events
are unaffected.

Revision ID: 033
Revises: 032
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "033"
down_revision: str | None = "032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEARNER_EVENT_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("route_learner_mode", sa.Text()),
    ("route_propensity", sa.Numeric(7, 6)),
    ("route_learner_version", sa.Text()),
    ("route_learner_frozen", sa.Boolean()),
)


def upgrade() -> None:
    for name, type_ in LEARNER_EVENT_COLUMNS:
        op.add_column("ai_usage_events", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(LEARNER_EVENT_COLUMNS):
        op.drop_column("ai_usage_events", name)
