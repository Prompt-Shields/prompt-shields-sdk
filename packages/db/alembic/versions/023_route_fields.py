"""Routing, sovereignty and savings fields on usage events

Adds nullable columns to ai_usage_events for the PRD 0001 §7.12 routing
metadata (#25), which used to be dropped (and counted in ignored_fields):

- api_surface: the API the caller spoke (chat, messages, responses, ...).
- route_mode, route_group, route_reason, route_est_cost: routing evidence.
- sensitivity_level, sensitivity_source: the data sensitivity that applied
  and where it came from (declared, pii_escalated, default).
- served_provider, served_jurisdiction, policy_version: where the call was
  served and under which route policy.
- fallback_chain: the error class of each failed attempt, in order (JSONB
  list of short class names, never messages).
- counterfactual_cost, baseline_id: what the call would have cost on the
  baseline model, for verified savings.

Metadata only: no prompt or response content. No new table: ai_usage_events
already has its tenant_isolation policy (015) and is in the erasure order.
All columns are nullable with no default, so existing rows and old events
are unaffected.

Revision ID: 023
Revises: 022
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "023"
down_revision: str | None = "022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROUTE_EVENT_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("api_surface", sa.Text()),
    ("route_mode", sa.Text()),
    ("route_group", sa.Text()),
    ("route_reason", sa.Text()),
    ("route_est_cost", sa.Numeric(10, 6)),
    ("sensitivity_level", sa.Text()),
    ("sensitivity_source", sa.Text()),
    ("served_provider", sa.Text()),
    ("served_jurisdiction", sa.Text()),
    ("policy_version", sa.Text()),
    ("fallback_chain", JSONB()),
    ("counterfactual_cost", sa.Numeric(10, 6)),
    ("baseline_id", sa.Text()),
)


def upgrade() -> None:
    for name, type_ in ROUTE_EVENT_COLUMNS:
        op.add_column("ai_usage_events", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(ROUTE_EVENT_COLUMNS):
        op.drop_column("ai_usage_events", name)
