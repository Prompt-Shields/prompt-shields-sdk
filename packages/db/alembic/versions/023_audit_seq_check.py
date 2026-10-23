"""audit_events: CHECK (seq >= 1)

A chain's ``seq`` counts 1, 2, ... (db/audit_chain.py). ``verify_chain``
walks rows from seq 1 up, so a row inserted with seq 0 or below was never
looked at. This adds ``ck_audit_events_seq_positive`` and
``verify_chain`` now reports any such row as a break.

The collector never writes seq < 1. If a database already holds such rows
(only possible by writing to the table directly), the constraint is added
``NOT VALID``: new rows are checked, the existing ones are left for
``python -m collector.audit_trail verify`` to report (rows are append-only,
so the migration does not delete them).

Revision ID: 023
Revises: 022
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "023"
down_revision: str | None = "022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "audit_events"
CONSTRAINT = "ck_audit_events_seq_positive"


def upgrade() -> None:
    conn = op.get_bind()
    # audit_events may be under FORCE ROW LEVEL SECURITY (transaction-local).
    conn.execute(sa.text("SELECT set_config('app.rls_bypass', 'migration-023', true)"))
    bad = conn.execute(sa.text(f"SELECT count(*) FROM {TABLE} WHERE seq < 1")).scalar()
    not_valid = " NOT VALID" if bad else ""
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT} CHECK (seq >= 1){not_valid}"
    )
    conn.execute(sa.text("SELECT set_config('app.rls_bypass', '', true)"))


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
