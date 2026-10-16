"""Extended usage fields on usage events, modality on assets

Adds nullable columns to ai_usage_events for the usage fields the SDK and
gateway already send, which used to be dropped (and counted in
ignored_fields): requested_model, served_model, tokens_cached,
tokens_cache_write, tokens_cache_write_1h, tokens_reasoning, cost_source,
service_tier, framework, modality, usage_units and usage_unit_type.

Adds ai_assets.modality so the registry shows what an asset works on
(text, audio, image, document).

No new table: both tables already have their tenant_isolation policy (015)
and are in the erasure order. All columns are nullable with no default, so
existing rows and old events are unaffected.

Revision ID: 017
Revises: 016
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "017"
down_revision: str | None = "016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USAGE_EVENT_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("requested_model", sa.Text()),
    ("served_model", sa.Text()),
    ("tokens_cached", sa.Integer()),
    ("tokens_cache_write", sa.Integer()),
    ("tokens_cache_write_1h", sa.Integer()),
    ("tokens_reasoning", sa.Integer()),
    ("cost_source", sa.Text()),
    ("service_tier", sa.Text()),
    ("framework", sa.Text()),
    ("modality", sa.Text()),
    ("usage_units", sa.Numeric(20, 6)),
    ("usage_unit_type", sa.Text()),
)


def upgrade() -> None:
    for name, type_ in USAGE_EVENT_COLUMNS:
        op.add_column("ai_usage_events", sa.Column(name, type_, nullable=True))
    op.add_column("ai_assets", sa.Column("modality", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_assets", "modality")
    for name, _ in reversed(USAGE_EVENT_COLUMNS):
        op.drop_column("ai_usage_events", name)
