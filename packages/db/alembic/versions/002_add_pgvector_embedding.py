"""Add pgvector embedding column to ai_assets

Revision ID: 002
Revises: 001
Create Date: 2026-03-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = '002'
down_revision: str | None = '001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Enable pgvector extension
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')

    # Add embedding column
    op.add_column('ai_assets', sa.Column('embedding', Vector(1536)))

    # Add HNSW index for cosine similarity search (works on empty tables, unlike IVFFlat)
    op.create_index(
        'ix_ai_assets_embedding',
        'ai_assets',
        ['embedding'],
        postgresql_using='hnsw',
        postgresql_ops={'embedding': 'vector_cosine_ops'},
    )


def downgrade() -> None:
    op.drop_index('ix_ai_assets_embedding')
    op.drop_column('ai_assets', 'embedding')
