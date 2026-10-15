"""Webhook secret rotation grace period

Adds to webhook_subscriptions: previous_secret_ciphertext and
previous_secret_expires_at. After a rotation with a grace period the
replaced secret is kept (encrypted like the current one) and deliveries
carry a signature for each secret until it expires.

No new table: webhook_subscriptions already has its tenant_isolation
policy (015). Encryption key ids live inside the ciphertext tokens, so the
keyring needs no column.

Revision ID: 016
Revises: 015
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "016"
down_revision: str | None = "015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "webhook_subscriptions",
        sa.Column("previous_secret_ciphertext", sa.Text, nullable=True),
    )
    op.add_column(
        "webhook_subscriptions",
        sa.Column("previous_secret_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("webhook_subscriptions", "previous_secret_expires_at")
    op.drop_column("webhook_subscriptions", "previous_secret_ciphertext")
