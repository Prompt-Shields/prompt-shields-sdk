"""Hash tenant ingest keys, drop the plaintext key

Adds tenants.api_key_prefix (SHA-256 lookup prefix, unique index) and
tenants.api_key_hash (bcrypt). Every existing settings->>'api_key' is
hashed into those columns and then removed from settings, so no plaintext
key is left in the database.

Downgrade drops the two columns. The plaintext keys cannot be recovered,
so after a downgrade every tenant needs a new key.

Revision ID: 004
Revises: 003
Create Date: 2026-10-13
"""

import hashlib
from collections.abc import Sequence

import bcrypt
import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Inlined rather than imported from db.api_keys so this migration keeps
# producing the same rows if that module changes later.
def _lookup_prefix(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _bcrypt(key: str) -> str:
    return bcrypt.hashpw(key.encode(), bcrypt.gensalt(rounds=10)).decode()


def upgrade() -> None:
    op.add_column("tenants", sa.Column("api_key_prefix", sa.Text, nullable=True))
    op.add_column("tenants", sa.Column("api_key_hash", sa.Text, nullable=True))

    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, settings->>'api_key' AS api_key FROM tenants "
            "WHERE settings->'api_key' IS NOT NULL"
        )
    ).fetchall()
    for row in rows:
        if row.api_key:
            conn.execute(
                sa.text(
                    "UPDATE tenants SET api_key_prefix = :prefix, "
                    "api_key_hash = :hash, settings = settings - 'api_key' "
                    "WHERE id = :id"
                ),
                {
                    "prefix": _lookup_prefix(row.api_key),
                    "hash": _bcrypt(row.api_key),
                    "id": row.id,
                },
            )
        else:
            conn.execute(
                sa.text(
                    "UPDATE tenants SET settings = settings - 'api_key' WHERE id = :id"
                ),
                {"id": row.id},
            )

    # Created after the backfill: two tenants sharing one plaintext key fail
    # here, and need distinct keys before this migration can run.
    op.create_index(
        "ix_tenants_api_key_prefix", "tenants", ["api_key_prefix"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_tenants_api_key_prefix", table_name="tenants")
    op.drop_column("tenants", "api_key_hash")
    op.drop_column("tenants", "api_key_prefix")
