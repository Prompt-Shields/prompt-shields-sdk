"""Implicit outcome signals on usage events (#44)

Adds two nullable columns to ai_usage_events for the SDK's (#249) and the
gateway's outcome signals, which used to be dropped (and counted in
ignored_fields):

- outcome_signals: JSONB list of known class names (``max_tokens``,
  ``refusal``, ``fallback``, ...; collector.ingest.OUTCOME_SIGNAL_CLASSES),
  sorted and de-duplicated, at most one of each. ``[]`` means measured and
  nothing happened; NULL means not reported.
- outcome_signals_version: the OUTCOME_SIGNALS_VERSION that defined them.

Metadata only: class names, never prompt or response content. No new
table: ai_usage_events already has its tenant_isolation policy (015) and is
in the erasure order. Existing rows and old events are unaffected.

Chained from 027 (main's head when written); 028 and 029 are assigned to
other work, so this is renumbered to follow them at merge.

Revision ID: 030
Revises: 027
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "030"
down_revision: str | None = "027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OUTCOME_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("outcome_signals", JSONB()),
    ("outcome_signals_version", sa.SmallInteger()),
)


def upgrade() -> None:
    for name, type_ in OUTCOME_COLUMNS:
        op.add_column("ai_usage_events", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(OUTCOME_COLUMNS):
        op.drop_column("ai_usage_events", name)
