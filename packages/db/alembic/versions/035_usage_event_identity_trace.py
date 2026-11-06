"""Gateway identity and trace fields on usage events

Adds to ai_usage_events the fields the gateway sends that the collector used
to drop and count in ignored_fields (#334):

* user_id_hash: HMAC-SHA256 of the caller's user_id with a per-tenant salt
  (tenants.settings["user_id_salt"], generated on first use). The default.
* user_id_raw: the user_id as sent, only for a tenant whose
  settings["store_raw_user_ids"] is true (then instead of the hash).
* user_group: the gateway policy group (OIDC) that applied.
* trace_id, retry_count and attempts: the request's retries and fallbacks
  (attempts is the gateway's per-attempt metadata, never content).
* stream_truncated, stream_cancelled: streamed response flags.

Metadata only. No new table: ai_usage_events already has its
tenant_isolation policy (015) and is in the erasure order. All nullable
with no default, so existing rows and old events are unaffected. Partial
indexes on (tenant_id, user_id_hash) and (tenant_id, trace_id) for per-user
usage and erasure, and for looking up one request's events.

Revision ID: 035
Revises: 034
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "035"
down_revision: str | None = "034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IDENTITY_TRACE_COLUMNS = (
    ("user_id_hash", sa.Text()),
    ("user_id_raw", sa.Text()),
    ("user_group", sa.Text()),
    ("trace_id", sa.Text()),
    ("retry_count", sa.Integer()),
    ("attempts", JSONB()),
    ("stream_truncated", sa.Boolean()),
    ("stream_cancelled", sa.Boolean()),
)

INDEXES = (
    ("ix_usage_events_tenant_user_hash", "user_id_hash"),
    ("ix_usage_events_tenant_trace_id", "trace_id"),
)


def upgrade() -> None:
    for name, type_ in IDENTITY_TRACE_COLUMNS:
        op.add_column("ai_usage_events", sa.Column(name, type_, nullable=True))
    for index, column in INDEXES:
        op.create_index(
            index,
            "ai_usage_events",
            ["tenant_id", column],
            postgresql_where=sa.text(f"{column} IS NOT NULL"),
        )


def downgrade() -> None:
    for index, _ in INDEXES:
        op.drop_index(index, table_name="ai_usage_events")
    for name, _ in reversed(IDENTITY_TRACE_COLUMNS):
        op.drop_column("ai_usage_events", name)
