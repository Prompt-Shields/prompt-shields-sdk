"""Ingest key scopes and named operator tokens (#341)

Adds:

  - ``tenant_api_keys.scopes``: what an ingest key may do
    (``ingest:write``, ``registry:read``, ``registry:attest``; see
    collector/tenant_keys.py). Every existing key is given all three, so
    SDKs, gateways and scripts keep working. The column default is then
    set to ``{ingest:write}``: a key issued from now on can only send
    events unless more scopes are asked for.
  - ``operator_tokens``: named, scoped, expiring operator tokens for
    /api/v1/operator and the operator-only admin routes
    (collector/operator_auth.py). Stored as a SHA-256 lookup prefix +
    bcrypt hash, like the ingest keys. Not a tenant table, so no row-level
    security policy. ``PS_ADMIN_TOKEN`` keeps working as break-glass.

Downgrade drops both. Every operator token is lost, and narrowed ingest
keys get their full access back (the column goes, and with it the check).

Revision ID: 037
Revises: 035
Create Date: 2026-11-06

Numbered 037 by plan; 036 is taken by an open PR (#338). It chains from
035 (main's head at merge); re-chain it onto 036 if that lands first.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "037"
down_revision: str | None = "035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, nullable: bool = True) -> sa.Column:
    kwargs = {} if nullable else {"server_default": sa.func.now()}
    return sa.Column(name, sa.TIMESTAMP(timezone=True), nullable=nullable, **kwargs)


def upgrade() -> None:
    # Existing keys keep every scope (backwards compatible) ...
    op.add_column(
        "tenant_api_keys",
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text(
                "'{ingest:write,registry:read,registry:attest}'::text[]"
            ),
        ),
    )
    # ... and keys added from now on default to ingest:write only.
    op.alter_column(
        "tenant_api_keys",
        "scopes",
        server_default=sa.text("'{ingest:write}'::text[]"),
    )

    op.create_table(
        "operator_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("token_prefix", sa.Text, nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False),
        sa.Column("token_hint", sa.Text, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        _ts("created_at", nullable=False),
        _ts("rotated_at"),
        _ts("expires_at"),
        _ts("revoked_at"),
    )
    op.create_index(
        "ux_operator_tokens_prefix", "operator_tokens", ["token_prefix"], unique=True
    )
    op.create_index("ux_operator_tokens_name", "operator_tokens", ["name"], unique=True)


def downgrade() -> None:
    op.drop_index("ux_operator_tokens_name", table_name="operator_tokens")
    op.drop_index("ux_operator_tokens_prefix", table_name="operator_tokens")
    op.drop_table("operator_tokens")
    op.drop_column("tenant_api_keys", "scopes")
