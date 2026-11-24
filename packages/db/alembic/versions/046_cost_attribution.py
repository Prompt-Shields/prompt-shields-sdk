"""Cost attribution: team, project, calling service and tags per event (#347)

Adds nullable columns, no new tables (so no new row-level security policy):

* ``ai_usage_events.team_id``, ``project_id``: the team and project the
  call is attributed to, resolved at ingest against the tenant's own teams
  and projects (#338). No foreign key: events outlive a deleted team or
  project, and the reports then show the id without a name.
* ``ai_usage_events.calling_service``: the service that made the call, as
  sent (it was only kept on the asset before).
* ``ai_usage_events.tags``: up to 20 validated ``key: value`` cost tags.
* the same four columns on ``usage_rollup_hourly`` and ``usage_rollup_daily``
  as rollup dimensions (rollup ``tags`` keep only the tenant's
  ``settings.cost_tag_keys``).

Existing rows keep NULL (unattributed). Run
``python -m collector.usage_rollups rebuild --all-tenants`` only if events
already carry attribution, which they cannot before this migration.

Numbered 046 by assignment; chained from 039, main's head when written.
Re-chain onto main's real head at merge.

Revision ID: 046
Revises: 039
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "046"
down_revision: str | None = "039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("ai_usage_events", "usage_rollup_hourly", "usage_rollup_daily")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("team_id", UUID(as_uuid=True), nullable=True))
        op.add_column(table, sa.Column("project_id", UUID(as_uuid=True), nullable=True))
        op.add_column(table, sa.Column("calling_service", sa.Text(), nullable=True))
        op.add_column(table, sa.Column("tags", JSONB(), nullable=True))


def downgrade() -> None:
    for table in TABLES:
        for column in ("tags", "calling_service", "project_id", "team_id"):
            op.drop_column(table, column)
